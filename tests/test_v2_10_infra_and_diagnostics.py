"""v2.10 基础设施修复与科学体检工具专项单元测试。"""
import gzip
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.policy import _compute_ioc_buy_limit, _walk_ask_vwap
from core.types import Decision, OrderBook, SpotState
from runtime.market_feed import MarketFeed
from runtime.runner import PaperRunner
from runtime.trade_stream import TradeStreamClient
from runtime.ws_feeds import BinanceSpotWSClient, PolymarketBookWSClient
from sim.audit import audit_ledger, format_audit_report
from sim.replay import summarize_replay_by_day


def _book(bid: float, ask: float, sz: float = 50.0, t: float = 1000.0) -> OrderBook:
    return OrderBook(bids=[(bid, sz)], asks=[(ask, sz)], source_t=t, received_at=t)


class TestV210InfraAndDiagnostics(unittest.TestCase):
    def test_basis_timestamp_alignment_ignores_post_request_spike(self):
        """Chainlink TWAP 请求发起于 req_t，后续 1.2s 内现货暴涨不得污染 [req_t-60, req_t] 基差计算。"""
        tmp = tempfile.mkdtemp(prefix="pm_basis_align_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)

            # 前 60 秒现货稳定在 65020（相对 Chainlink TWAP 65000 真实基差 = +20）
            with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="w")):
                for i in range(60):
                    ti = t0 + i * 1.0
                    sp = SpotState(timestamp=ti, price=65020.0, open_price=65000.0, realized_vol_bps=12.0)
                    runner.step(ti, sp, _book(0.49, 0.51, t=ti), _book(0.49, 0.51, t=ti), poly_spot=None, replay_mode=False)

                # t = t0 + 61.2s 现货突然暴涨至 65300，但此时收到的是 req_t = t0 + 59.5s 的 Chainlink TWAP (65000)
                t_now = t0 + 61.2
                req_t = t0 + 59.5
                sp_spike = SpotState(timestamp=t_now, price=65300.0, open_price=65000.0, realized_vol_bps=18.0)
                runner.step(
                    t_now, sp_spike, _book(0.49, 0.51, t=t_now), _book(0.49, 0.51, t=t_now),
                    poly_spot=65000.0, poly_spot_ts=req_t, replay_mode=False,
                )

            self.assertIsNotNone(runner._basis_ema)
            # 严格截取 [req_t - 60, req_t] 时均价为 65020 -> 基差必须精确等于 20.0，不受 t_now=61.2 暴涨影响
            self.assertAlmostEqual(runner._basis_ema, 20.0, places=2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_market_feed_exposes_req_t_while_checking_recv_t_freshness(self):
        """MarketFeed.get_polymarket_btc_price 应以接收时刻判鲜，同时向外暴露请求参考时刻 req_t。"""
        feed = MarketFeed.__new__(MarketFeed)
        feed._poly_btc_poller_active = True  # 禁用后台线程唤醒
        feed._last_poly_poll_t = time.monotonic()
        feed._poly_lock = threading.Lock()
        now_wall = time.time()
        feed._last_poly_btc = 70000.0
        feed._last_poly_btc_t = now_wall - 0.2
        feed._last_poly_btc_req_t = now_wall - 1.5

        px = feed.get_polymarket_btc_price(now=now_wall)
        self.assertEqual(px, 70000.0)
        self.assertAlmostEqual(feed.last_poly_btc_ts, now_wall - 1.5, places=3)

    def test_book_ws_single_token_stall_self_heal_and_top_size_wakeup(self):
        """单 Token 断流应自动排队原地重订；顶档量变 >=25% 或 >=20 股应立即唤醒主循环。"""
        wake = threading.Event()
        ws = PolymarketBookWSClient(wake_event=wake)
        ws.connected = True
        ws._active_tokens = ("tok_up", "tok_dn")

        t0 = 1000.0
        ws.apply_message({
            "event_type": "book",
            "asset_id": "tok_up",
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        }, now=t0)
        self.assertTrue(wake.is_set())

        # 1. 微小量变（-2 股，2%）不应误唤醒
        wake.clear()
        ws.apply_message({
            "event_type": "price_change",
            "price_changes": [{"asset_id": "tok_up", "price": "0.50", "size": "98", "side": "BUY"}],
        }, now=t0 + 1.0)
        self.assertFalse(wake.is_set())

        # 2. 顶档深度剧变（98 -> 60，降幅 > 25% 且 >= 20 股）必须立即唤醒主循环
        ws.apply_message({
            "event_type": "price_change",
            "price_changes": [{"asset_id": "tok_up", "price": "0.50", "size": "60", "side": "BUY"}],
        }, now=t0 + 2.0)
        self.assertTrue(wake.is_set())

        # 3. 超过 15 秒无新推送 -> 返回 None 并自动加入原地重订队列
        ws._last_ws_msg_t = t0 + 20.0
        bk = ws.get_order_book("tok_up", max_age_s=3.0, now=t0 + 20.0)
        self.assertIsNone(bk)
        self.assertIn("tok_up", ws._pending_resub_tokens)

        # 4. 连续 3 次原地重订均未恢复 -> 递增 _token_gen 升级整链平滑重连
        gen_before = ws._token_gen
        ws._pending_resub_tokens.clear()
        ws._schedule_token_resubscribe_locked("tok_up", t0 + 35.0)
        ws._schedule_token_resubscribe_locked("tok_up", t0 + 50.0)
        self.assertGreater(ws._token_gen, gen_before)

        # 5. 换窗增量退订旧 token 时，必须同步清理该 token 在 _pending_resub_tokens 中的残留
        ws._pending_resub_tokens = ["tok_up"]
        ws.set_tokens(["tok_dn", "tok_next_up"])
        self.assertNotIn("tok_up", ws._pending_resub_tokens)
        self.assertNotIn("tok_up", ws._resub_attempts)

    def test_async_taker_delay_nonblocking_and_capital_reservation(self):
        """async_taker_delay=True 时不得 sleep 阻塞主循环，冻结资金并在到站 tick 诚实撮合。"""
        tmp = tempfile.mkdtemp(prefix="pm_async_tk_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            runner._basis_ema = 0.0
            runner._basis_updated_t = t0 + 60.0

            sp = SpotState(timestamp=t0 + 60.0, price=65090.0, open_price=65000.0, realized_vol_bps=14.0)
            b_up = _book(0.51, 0.52, sz=50.0, t=t0 + 60.0)
            b_dn = _book(0.47, 0.49, sz=50.0, t=t0 + 60.0)
            dec = Decision(action="taker_buy", side="Up", price=0.52, size=5.0, limit_price=0.54, reason="Taker 顺势开仓")

            with patch("runtime.runner.time.sleep", side_effect=AssertionError("async mode must NOT call time.sleep")), \
                 patch.object(runner, "_effective_wire_latency", return_value=0.30), \
                 patch("runtime.runner.plan_decision", return_value=dec):
                runner.step(
                    t0 + 60.0, sp, b_up, b_dn,
                    delayed_book_fetcher=lambda _s: (_book(0.52, 0.53, t=t0 + 60.35), _book(0.46, 0.48, t=t0 + 60.35)),
                    async_taker_delay=True,
                )

            # 发单瞬间尚未成交，订单在途且 live.json 准确扣除挂单/在途冻结资金
            self.assertEqual(len(runner.pending_takers), 1)
            self.assertEqual(runner.pos_up.shares, 0.0)
            live = json.loads(runner.live_file.read_text(encoding="utf-8"))
            import config
            from core.execution import taker_fee_per_share
            exp_reserved = 5.0 * (0.54 + taker_fee_per_share(0.54, config.PARAMS["TAKER_FEE_RATE"]))
            self.assertAlmostEqual(live["equity"], 100.0, places=2)
            self.assertAlmostEqual(live["reserved_orders_cost"], exp_reserved, places=2)
            self.assertAlmostEqual(live["available_capital"], 100.0 - exp_reserved, places=2)

            # 0.15s 时仍未到达，保持等待不重复下单
            runner.step(
                t0 + 60.15, sp, b_up, b_dn,
                delayed_book_fetcher=lambda _s: (_book(0.52, 0.53, t=t0 + 60.15), _book(0.46, 0.48, t=t0 + 60.15)),
                async_taker_delay=True,
            )
            self.assertEqual(len(runner.pending_takers), 1)
            self.assertEqual(runner.pos_up.shares, 0.0)

            # 0.35s 到达交易所 -> 按到站盘口 0.53 诚实成交并清空在途队列
            with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="hold")):
                runner.step(
                    t0 + 60.35, sp, _book(0.52, 0.53, t=t0 + 60.35), _book(0.46, 0.48, t=t0 + 60.35),
                    delayed_book_fetcher=lambda _s: (_book(0.52, 0.53, t=t0 + 60.35), _book(0.46, 0.48, t=t0 + 60.35)),
                    async_taker_delay=True,
                )
            self.assertEqual(len(runner.pending_takers), 0)
            self.assertAlmostEqual(runner.pos_up.shares, 5.0, places=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_multi_provider_spot_ws_parsers(self):
        """验证 Binance / Kraken v2 / Coinbase / OKX 四种现货 WS 消息均可正确解析中间价。"""
        cases = [
            ("binance", {"b": "70000.0", "a": "70002.0"}, 70001.0, "binance_ws"),
            ("kraken", {"channel": "ticker", "data": [{"bid": 70010.0, "ask": 70014.0, "last": 70011.0}]}, 70012.0, "kraken_ws"),
            ("coinbase", {"type": "ticker", "best_bid": "70020.0", "best_ask": "70024.0", "price": "70021.0"}, 70022.0, "coinbase_ws"),
            ("okx", {"arg": {"channel": "bbo-tbt"}, "data": [{"bids": [["70030.0", "1.2"]], "asks": [["70034.0", "0.8"]]}]}, 70032.0, "okx_ws"),
        ]
        for prov, payload, expected_px, expected_src in cases:
            wake = threading.Event()
            ws = BinanceSpotWSClient(provider=prov, wake_event=wake)
            ok = ws.apply_message(payload, now=1000.0)
            self.assertTrue(ok, f"Provider {prov} failed to parse")
            self.assertTrue(wake.is_set())
            latest = ws.get_latest(max_age_s=5.0, now=1001.0)
            self.assertIsNotNone(latest)
            self.assertAlmostEqual(latest[0], expected_px, places=2)
            self.assertEqual(latest[2], expected_src)

    def test_scientific_audit_and_walk_forward_daily_split(self):
        """验证账本科学体检引擎（含旧账本 Brier 回溯补算）与回放按日分片统计。"""
        tmp = Path(tempfile.mkdtemp(prefix="pm_audit_test_"))
        try:
            win_dir = tmp / "windows"
            win_dir.mkdir(parents=True, exist_ok=True)
            t_day1 = 1763856000.0  # 2025-11-23 UTC+8
            t_day2 = t_day1 + 86400.0

            recs = [
                {
                    "slug": "btc-updown-5m-1", "start": t_day1, "end": t_day1 + 300.0,
                    "winner": "Up", "winner_source": "gamma", "traded": True,
                    "fills_count": 1, "total_cost": 2.5, "net_pnl": 2.5,
                    "mid_p_up": 0.75, "mid_mkt_up": 0.60,
                    "brier_model": 0.0625, "brier_market": 0.16,
                },
                {
                    # 模拟无 brier 字段的旧账本记录，依赖 windows/*.json.gz 自动补算
                    "slug": "btc-updown-5m-2", "start": t_day2, "end": t_day2 + 300.0,
                    "winner": "Down", "winner_source": "gamma", "traded": True,
                    "fills_count": 2, "total_cost": 5.0, "net_pnl": -1.0,
                },
            ]
            (tmp / "summary.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

            det1 = dict(recs[0], fills=[{"kind": "maker", "purpose": "entry", "side": "Up", "order_id": 1}])
            det2 = dict(
                recs[1],
                fills=[
                    {"kind": "taker", "purpose": "entry", "side": "Down", "order_id": 2},
                    {"kind": "taker", "purpose": "entry", "side": "Down", "order_id": 3, "reason": "顺势加仓"},
                ],
                price_history=[{"t": t_day2 + 160.0, "prob_up": 0.30, "poly_up": 0.45}],
            )
            with gzip.open(win_dir / "btc-updown-5m-1.json.gz", "wt", encoding="utf-8") as f:
                json.dump(det1, f)
            with gzip.open(win_dir / "btc-updown-5m-2.json.gz", "wt", encoding="utf-8") as f:
                json.dump(det2, f)

            report = audit_ledger(tmp)
            self.assertEqual(report["traded"], 2)
            self.assertAlmostEqual(report["total_pnl"], 1.5, places=4)
            self.assertEqual(report["brier"]["n"], 2)
            # 窗1 model=(0.75-1)^2=0.0625, 窗2 model=(0.30-0)^2=0.09 -> avg=0.07625
            self.assertAlmostEqual(report["brier"]["model"], 0.07625, places=5)
            self.assertLess(report["brier"]["diff"], 0.0)
            self.assertEqual(report["execution_breakdown"]["maker_leg1"]["n"], 1)
            self.assertEqual(report["execution_breakdown"]["taker_leg1"]["n"], 1)
            self.assertEqual(report["execution_breakdown"]["pyramid_windows"]["n"], 1)
            self.assertEqual(len(report["daily"]), 2)

            txt = format_audit_report(report)
            self.assertIn("Brier 基准", txt)
            self.assertIn("执行结构拆解", txt)

            # 同步验证 replay 按日分片统计
            replay_res = [
                {"slug": f"btc-updown-5m-{int(t_day1)}", "replayed": True, "replay_traded": True, "replay_pnl": 2.0},
                {"slug": f"btc-updown-5m-{int(t_day2)}", "replayed": True, "replay_traded": True, "replay_pnl": -1.5},
            ]
            daily_rep = summarize_replay_by_day(replay_res)
            self.assertEqual(len(daily_rep), 2)
            self.assertAlmostEqual(daily_rep[0]["net_pnl"], 2.0, places=4)
            self.assertAlmostEqual(daily_rep[1]["net_pnl"], -1.5, places=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_multilevel_fractional_cent_vwap_ioc_limit_covers_worst_level(self):
        """穿两档卖盘算出非整分均价(如 0.484)时，IOC 限价不得向下舍入到 0.48 导致无法扫满 5 股。"""
        book = OrderBook(bids=[(0.47, 50.0)], asks=[(0.48, 3.0), (0.49, 50.0)], source_t=1000.0, received_at=1000.0)
        filled, vwap, worst_px = _walk_ask_vwap(book, 5.0)
        self.assertAlmostEqual(filled, 5.0, places=4)
        self.assertAlmostEqual(vwap, 0.484, places=4)
        self.assertAlmostEqual(worst_px, 0.49, places=4)
        # 即使在 0.49 单点边际略低于阈值，限价下限仍需覆盖已通过 VWAP 校验的 worst_px=0.49
        lim = _compute_ioc_buy_limit(
            sig_px=vwap, p_side=0.568, base_edge=0.065, tau_s=120.0, fee_rate=0.07,
            params={"TAKER_IOC_SLIPPAGE_TICKS": 0.02, "PRICE_EDGE_PIVOT": 0.54},
            max_cap_px=0.80, min_sweep_px=worst_px,
        )
        self.assertGreaterEqual(lim, 0.49)

    def test_brier_prefers_unbiased_mid_snapshot_over_entry_snapshot(self):
        """交易窗同时有中段(tau<=150s)与开仓快照时，brier_model 必须统一采用无偏中段截面。"""
        tmp = tempfile.mkdtemp(prefix="pm_brier_mid_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-2000", 2000.0, 2300.0, 65000.0)
            runner._mid_p_up = 0.70
            runner._mid_mkt_up = 0.55
            runner._entry_p_up = 0.90
            runner._entry_mkt_up = 0.80
            runner.close_window(close_px=65100.0, official_winner="Up", winner_source="gamma")
            rec = json.loads(runner.summary_file.read_text(encoding="utf-8").splitlines()[-1])
            self.assertAlmostEqual(rec["brier_model"], (0.70 - 1.0) ** 2, places=5)
            self.assertAlmostEqual(rec["brier_market"], (0.55 - 1.0) ** 2, places=5)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_book_ws_detects_server_timestamp_buffering_and_trade_divergence(self):
        """盘口 WS 应免疫固定本机时钟偏移，但对相对基线 >3.5s 的积压旧帧或 >=$0.05 成交价背离立刻失效重连。"""
        ws = PolymarketBookWSClient()
        ws.connected = True
        ws._active_tokens = ("tok_up",)

        # 模拟本机时钟比交易所慢 1.7s（delta = -1.7s 固定基线）：正常推送必须被接受
        srv_t0 = 1795436100.0
        ok0 = ws.apply_message({
            "event_type": "book",
            "asset_id": "tok_up",
            "timestamp": str(int(srv_t0 * 1000)),
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.56", "size": "100"}],
        }, now=srv_t0 - 1.7)
        self.assertTrue(ok0)
        self.assertIsNotNone(ws.get_order_book("tok_up", now=srv_t0 - 1.5))

        # 1. 独立逐笔成交流已在 $0.72 成交，而 WS 盘口仍报卖一 $0.56 -> 必须判定积压失效并重连
        gen_before = ws._token_gen
        ts_client = TradeStreamClient(record_raw=False)
        ts_client.set_active_targets(["btc-updown-5m-1795436100"], ["tok_up"])
        ts_client._ingest({
            "slug": "btc-updown-5m-1795436100",
            "asset": "tok_up",
            "outcome": "Up",
            "price": 0.72,
            "size": 10.0,
            "side": "BUY",
            "timestamp": srv_t0 + 5.0,
        })
        live_px = ts_client.get_latest_price("tok_up", max_age_s=5.0)
        self.assertAlmostEqual(live_px, 0.72, places=4)
        self.assertTrue(ws.invalidate_on_trade_divergence("tok_up", live_px, max_gap=0.05))
        self.assertGreater(ws._token_gen, gen_before)
        self.assertIsNone(ws.get_order_book("tok_up", now=srv_t0 - 1.0))

        # 2. 重新收到正常帧后，若下一帧相对基线滞后 6 秒（代理缓冲旧帧）-> 必须丢弃并触发重连
        ws.apply_message({
            "event_type": "book",
            "asset_id": "tok_up",
            "timestamp": str(int((srv_t0 + 10.0) * 1000)),
            "bids": [{"price": "0.70", "size": "100"}],
            "asks": [{"price": "0.71", "size": "100"}],
        }, now=srv_t0 + 10.0 - 1.7)
        gen_before_lag = ws._token_gen
        ok_lag = ws.apply_message({
            "event_type": "price_change",
            "timestamp": str(int((srv_t0 + 11.0) * 1000)),
            "price_changes": [{"asset_id": "tok_up", "price": "0.70", "size": "50", "side": "BUY"}],
        }, now=srv_t0 + 17.0 - 1.7)
        self.assertFalse(ok_lag)
        self.assertGreater(ws._token_gen, gen_before_lag)

        # 3. 静态初始 book 快照带 12 秒前旧 timestamp（安静/新预热盘口常见）不得误杀自杀重连
        ws2 = PolymarketBookWSClient()
        ws2.connected = True
        ws2._active_tokens = ("tok_up", "tok_quiet")
        ws2.apply_message({
            "event_type": "book",
            "asset_id": "tok_up",
            "timestamp": str(int(srv_t0 * 1000)),
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.56", "size": "100"}],
        }, now=srv_t0)
        gen_quiet_before = ws2._token_gen
        ok_quiet = ws2.apply_message({
            "event_type": "book",
            "asset_id": "tok_quiet",
            "timestamp": str(int((srv_t0 - 12.0) * 1000)),
            "bids": [{"price": "0.01", "size": "500"}],
            "asks": [{"price": "0.99", "size": "500"}],
        }, now=srv_t0 + 1.0)
        self.assertTrue(ok_quiet)
        self.assertEqual(ws2._token_gen, gen_quiet_before)

        # 4. 盘口已先于老成交价更新（st["t"] > trade_recv_t + 0.15）时，不得用老成交价误杀正常 WS
        ws2.apply_message({
            "event_type": "book",
            "asset_id": "tok_up",
            "timestamp": str(int((srv_t0 + 2.0) * 1000)),
            "bids": [{"price": "0.64", "size": "100"}],
            "asks": [{"price": "0.65", "size": "100"}],
        }, now=srv_t0 + 2.0)
        self.assertFalse(
            ws2.invalidate_on_trade_divergence("tok_up", 0.55, max_gap=0.06, trade_recv_t=srv_t0 + 0.5)
        )
        self.assertEqual(ws2._token_gen, gen_quiet_before)

    def test_post_only_arrival_reject_runs_before_trade_prints_and_clock_offset_aligns(self):
        """刚到站且已穿盘的 Post-Only Maker 挂单不得被逐笔成交打印绕过拒单白捡免费成交。"""
        tmp = tempfile.mkdtemp(prefix="pm_po_guard_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            sp = SpotState(timestamp=t0 + 60.0, price=65060.0, open_price=65000.0, realized_vol_bps=14.0)

            # Tick 1 (t=60.0): 挂出 Maker 买单 Up @ 0.55，假设网络延迟 0.20s -> live_at = 60.20
            dec_quote = Decision(action="quote", side="Up", price=0.55, size=5.0, reason="Maker首仓")
            with patch.object(runner, "_effective_wire_latency", return_value=0.20), \
                 patch("runtime.runner.plan_decision", return_value=dec_quote):
                runner.step(t0 + 60.0, sp, _book(0.55, 0.56, t=t0 + 60.0), _book(0.44, 0.45, t=t0 + 60.0))
            self.assertEqual(len(runner.working_orders), 1)
            self.assertFalse(runner.working_orders[0].activated)

            # Tick 2 (t=60.25 刚到达): 此时卖一已跌至 0.54（穿盘），且同时收到一笔 0.54 卖单成交打印
            # 真实交易所必须 Post-Only 穿盘拒单，绝不能在 on_trade_print 里白捡 0.55 免费成交！
            prints = [{"outcome": "Up", "price": 0.54, "size": 20.0, "side": "SELL", "timestamp": t0 + 60.24, "received_at": t0 + 60.25}]
            with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="w")):
                runner.step(
                    t0 + 60.25, sp,
                    _book(0.53, 0.54, t=t0 + 60.25),
                    _book(0.46, 0.47, t=t0 + 60.25),
                    trade_prints=prints,
                )
            self.assertEqual(runner.pos_up.shares, 0.0)
            self.assertEqual(len(runner.fills), 0)
            self.assertTrue(any("Post-Only" in (a.get("reason") or "") for a in runner.actions))

            # 验证本机时钟偏快 +2.0s 时，亚秒级 exch_t 正确投影到本机坐标系
            adj = PaperRunner._resolve_print_timestamp(
                {"timestamp": 1800000100.25, "received_at": 1800000102.45},
                fallback_now=1800000102.50,
                clock_offset_s=2.0,
            )
            self.assertAlmostEqual(adj, 1800000102.25, places=3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pending_taker_fetcher_exception_never_wedges_trading(self):
        """在途 Taker 到站拉盘口抛异常时，必须干净拒单并出队，绝不能永久阻塞后续交易。"""
        tmp = tempfile.mkdtemp(prefix="pm_pt_exc_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            sp = SpotState(timestamp=t0 + 60.0, price=65080.0, open_price=65000.0, realized_vol_bps=14.0)
            dec = Decision(action="taker_buy", side="Up", price=0.52, size=5.0, limit_price=0.54, reason="test")

            def _boom(_side):
                raise RuntimeError("simulated proxy hang")

            with patch.object(runner, "_effective_wire_latency", return_value=0.20), \
                 patch("runtime.runner.plan_decision", return_value=dec):
                runner.step(t0 + 60.0, sp, _book(0.51, 0.52, t=t0 + 60.0), _book(0.47, 0.49, t=t0 + 60.0),
                            delayed_book_fetcher=_boom, async_taker_delay=True)
            self.assertEqual(len(runner.pending_takers), 1)

            # 到站 tick 触发 _boom 异常：不得崩溃，且 pending_takers 必须清空并记入 taker_reject
            with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="w")):
                runner.step(t0 + 60.25, sp, _book(0.51, 0.52, t=t0 + 60.25), _book(0.47, 0.49, t=t0 + 60.25),
                            delayed_book_fetcher=_boom, async_taker_delay=True)
            self.assertEqual(len(runner.pending_takers), 0)
            self.assertTrue(any(a.get("action") == "taker_reject" and "异常" in (a.get("reason") or "") for a in runner.actions))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rtds_chainlink_ws_primary_and_rest_fallback(self):
        import threading
        import time
        from unittest.mock import patch
        from runtime.trade_stream import TradeStreamClient
        from runtime.market_feed import MarketFeed

        wake = threading.Event()
        ts_client = TradeStreamClient(record_raw=False, wake_event=wake)
        ts_client.connected = True
        ts_client.rtt_ms = 7.8

        # 非 btc/usd 币种或异常值应被忽略
        self.assertFalse(ts_client._ingest_chainlink({"symbol": "eth/usd", "value": 1727.5, "timestamp": 1795750193000}, 120))
        self.assertFalse(ts_client._ingest_chainlink({"symbol": "btc/usd", "value": 50.0, "timestamp": 1795750193000}, 120))
        self.assertFalse(wake.is_set())

        # 有效 btc/usd 毫秒级推送应被接收并立即唤醒主循环
        now_s = time.time()
        self.assertTrue(ts_client._ingest_chainlink({
            "symbol": "btc/usd",
            "value": 61525.22,
            "timestamp": int(now_s * 1000),
        }, raw_bytes=180))
        self.assertTrue(wake.is_set())
        # 乱序过期旧时间戳（比已接收更老）必须丢弃，杜绝曲线锯齿回跳
        self.assertFalse(ts_client._ingest_chainlink({
            "symbol": "btc/usd",
            "value": 61520.00,
            "timestamp": int((now_s - 2.0) * 1000),
        }, raw_bytes=180))
        self.assertAlmostEqual(ts_client.get_latest_chainlink_btc(now=now_s)[0], 61525.22, places=2)

        feed = MarketFeed.__new__(MarketFeed)
        feed._trade_ws = ts_client
        feed._spot_ws = None
        feed._book_ws = None
        feed._poly_lock = threading.Lock()
        feed._net_lock = threading.Lock()
        feed._schema_lock = threading.Lock()
        feed._schema_counts = {}
        feed._schema_guard = {"paused": False, "source": "", "reason": "", "detected_at": 0.0}
        feed._last_poly_btc = None
        feed._last_poly_btc_t = 0.0
        feed._last_poly_btc_req_t = 0.0
        feed._last_poly_poll_t = 0.0
        feed._poly_btc_poller_active = False
        feed._chainlink_ws_active = False
        feed._last_cl_ws_msg_count = 0
        feed._last_cl_ws_bytes = 0
        feed.last_poly_btc_ts = 0.0
        feed.clock_offset_s = 0.0
        feed.net_stats = {
            "poly_latency_ms": 6.3, "binance_latency_ms": 99.0, "chainlink_latency_ms": 0.0,
            "downstream_kbps": 0.0, "total_bytes": 0, "bytes_window": 0,
            "last_calc_t": now_s, "msg_rate": 0.0, "msgs_window": 0,
        }
        feed._poly_poller_wake = threading.Event()
        recorded_oracle = []
        feed.record_raw_event = lambda cat, ev: recorded_oracle.append((cat, ev))

        with patch.object(feed, "_ensure_poly_poller") as mock_poller:
            # 1) 冷启动仅 1 帧瞬时价（未攒满 55s TWAP 窗口）必须走 REST 60s TWAP 兜底，严禁把瞬时价伪装成 60s TWAP
            px_cold = feed.get_polymarket_btc_price(now=now_s)
            self.assertIsNone(px_cold)
            mock_poller.assert_called_once()
            self.assertFalse(feed.get_net_summary()["chainlink_ws_connected"])

        # 2) 喂入完整 60s Chainlink WS 序列后，本地梯形积分输出真实 60s TWAP，不再启动 REST 轮询
        ts_full = TradeStreamClient(record_raw=False, wake_event=wake)
        ts_full.connected = True
        ts_full.rtt_ms = 7.8
        t_start = now_s - 60.0
        for i in range(61):
            # 前 30s 为 61500，后 30s 为 61550 -> 60s 时间加权均值为 ~61525
            val = 61500.0 if i < 30 else 61550.0
            ts_full._ingest_chainlink({
                "symbol": "btc/usd",
                "value": val,
                "timestamp": int((t_start + i) * 1000),
            }, raw_bytes=180)
        feed._trade_ws = ts_full
        feed._poly_btc_poller_active = False
        feed._last_poly_poll_t = 0.0
        feed._last_cl_ws_msg_count = 0
        recorded_oracle.clear()

        with patch.object(feed, "_ensure_poly_poller") as mock_poller:
            px = feed.get_polymarket_btc_price(now=now_s)
            self.assertIsNotNone(px)
            self.assertAlmostEqual(px, 61525.0, delta=1.5)
            # WS 攒满 60s 且健康时绝不启动 HTTP REST 轮询
            mock_poller.assert_not_called()
            summary = feed.get_net_summary()
            self.assertTrue(summary["chainlink_ws_connected"])
            self.assertAlmostEqual(summary["chainlink_latency_ms"], 7.8, places=1)
            self.assertEqual(len(recorded_oracle), 1)
            self.assertEqual(recorded_oracle[0][1]["source"], "rtds_ws_twap60")

            # WS 断连后自动回退到 REST 轮询兜底
            ts_full.connected = False
            px_fallback = feed.get_polymarket_btc_price(now=now_s + 0.5)
            self.assertAlmostEqual(px_fallback, px, places=2)  # 复用 10s 内缓存同时触发 REST 刷新
            mock_poller.assert_called_once()
            self.assertFalse(feed.get_net_summary()["chainlink_ws_connected"])

    def test_async_live_writer_and_batched_raw_recorder(self):
        import json
        import queue
        import shutil
        import tempfile
        import threading
        import time
        from pathlib import Path
        from unittest.mock import patch
        import config
        from runtime.runner import PaperRunner
        from runtime.market_feed import MarketFeed
        from core.types import SpotState

        tmp = Path(tempfile.mkdtemp(prefix="pm_async_io_"))
        try:
            # 1. PaperRunner async live.json coalescing writer + compact JSON
            runner = PaperRunner(ledger_dir=tmp / "ledger", async_live_write=True)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            for i in range(15):
                sp = SpotState(timestamp=t0 + 10.0 + i * 0.2, price=65000.0 + i, open_price=65000.0, realized_vol_bps=14.0)
                runner.last_spot_price = sp.price
                runner._write_live(t0 + 10.0 + i * 0.2, sp, _book(0.50, 0.51, t=t0 + 10.0 + i), _book(0.49, 0.50, t=t0 + 10.0 + i))
            runner.flush_live(timeout=2.0)
            raw_live = runner.live_file.read_text(encoding="utf-8")
            self.assertNotIn("\n  ", raw_live)  # compact JSON without indent=2 bloat
            parsed = json.loads(raw_live)
            self.assertAlmostEqual(parsed["spot"], 65014.0, places=2)
            self.assertAlmostEqual(parsed["now"], round(t0 + 10.0 + 14 * 0.2, 2), places=2)
            runner.close()
            self.assertFalse(runner._live_writer_thread.is_alive())

            # 2. MarketFeed async batched raw event writer flushes completely on close()
            feed = MarketFeed.__new__(MarketFeed)
            feed._record_lock = threading.Lock()
            feed._raw_write_queue = queue.Queue(maxsize=256)
            feed._raw_writer_thread = threading.Thread(target=feed._raw_disk_writer_loop, daemon=True)
            feed._raw_writer_thread.start()
            with patch.object(config, "MARKET_DIR", tmp / "market"), \
                 patch("runtime.market_feed.is_raw_recording_allowed", return_value=True):
                for i in range(25):
                    feed.record_raw_event("oracle", {"t": t0 + i, "kind": "poly_twap_live", "price": 65000.0 + i})
                feed.close()
            self.assertFalse(feed._raw_writer_thread.is_alive())
            today = time.strftime("%Y-%m-%d", time.gmtime())
            out_file = tmp / "market" / "oracle" / today / f"oracle_{today}.jsonl"
            lines = out_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 25)
            self.assertEqual(json.loads(lines[-1])["price"], 65024.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_book_ws_dirty_cache_reuses_sorted_levels_and_invalidates_on_delta(self):
        from runtime.ws_feeds import PolymarketBookWSClient

        cli = PolymarketBookWSClient()
        cli.connected = True
        t0 = 1800000100.0
        cli.apply_message({
            "event_type": "book",
            "asset_id": "tok_cache",
            "bids": [{"price": "0.48", "size": "50"}, {"price": "0.49", "size": "30"}],
            "asks": [{"price": "0.52", "size": "40"}, {"price": "0.51", "size": "25"}],
            "timestamp": int(t0 * 1000),
        }, now=t0)

        b1 = cli.get_order_book("tok_cache", now=t0 + 0.1)
        b2 = cli.get_order_book("tok_cache", now=t0 + 0.2)
        self.assertIsNotNone(b1)
        self.assertIs(b1.bids, b2.bids)
        self.assertIs(b1.asks, b2.asks)
        self.assertAlmostEqual(b2.source_t, t0 + 0.2, places=3)

        # last_trade_price 仅更新最新成交价，不触发无谓重排
        cli.apply_message({
            "event_type": "last_trade_price",
            "asset_id": "tok_cache",
            "price": "0.50",
        }, now=t0 + 0.3)
        b3 = cli.get_order_book("tok_cache", now=t0 + 0.35)
        self.assertIs(b3.bids, b1.bids)
        self.assertAlmostEqual(b3.last_trade_price, 0.50, places=4)

        # price_change 标脏并重排，立刻反映最新最优价与深度
        cli.apply_message({
            "event_type": "price_change",
            "price_changes": [{"asset_id": "tok_cache", "price": "0.50", "size": "88", "side": "BUY"}],
            "timestamp": int((t0 + 0.4) * 1000),
        }, now=t0 + 0.4)
        b4 = cli.get_order_book("tok_cache", now=t0 + 0.45)
        self.assertIsNot(b4.bids, b1.bids)
        self.assertAlmostEqual(b4.best_bid, 0.50, places=4)
        self.assertAlmostEqual(b4.bids[0][1], 88.0, places=2)


if __name__ == "__main__":
    unittest.main()
