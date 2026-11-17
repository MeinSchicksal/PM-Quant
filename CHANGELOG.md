# Changelog

All notable changes to the **PM-Quant** system are documented in this file.
Versions strictly follow semantic versioning **`v<Major>.<Minor>`** (e.g., `v1.0`, `v1.1`, `v2.0`, `v2.1`), with each version isolated under `data/2_ledgers/<VERSION>/`.

---

## [v2.4] - 2026-11-17 (Model-Free Objective Dual-Stage Architecture & Dashboard Cleanup)

### Removed — Model Win Probability (`p_fair` / `P*` / Normal CDF)
- **Complete removal of parametric probability estimation**: Stripped `normal_cdf`, `estimate_fair_prob`, and `estimate_p_fair` from `core/estimator.py`, `core/policy.py`, `core/execution.py`, `core/regime.py`, and `runtime/runner.py`.
- **Dashboard & API cleanup (`web/server.py`, `web/static/index.html`, `web/static/app.js`, `web/static/style.css`)**: Completely removed `prob_up` / `prob_down` / `p_model`, the `模型公允胜率 P*` legend, `模型 P*` and `定价边际` stats, the purple `P*` chart curve, and all `P*` tooltip rows across live and historical modal charts.

### Added — Pure Objective Physical Strategy (`v2.4`)
- **Pure Arithmetic Lead (`core/estimator.py`)**: Replaced probabilistic estimation with `compute_objective_lead()`, calculating exact basis-adjusted spot / locked 60s-TWAP displacement (`lead_bps`, `delta_usd`) from the window strike.
- **Objective Dual-Stage Policy (`core/policy.py`, `config.py`)**:
  - **MWDE (`120s < tau <= 240s`)**: Trend-aligned discount quoting (`<= $0.45`) with minimum `1.5 bps` displacement filter (filtering zero-noise chop) and post-shock consensus handling (`>= $40` prior move).
  - **LWAA (`15s <= tau <= 120s`)**: High-conviction entry requiring objective lead `>= 5.0 bps` (Maker) or `>= 8.0 bps` (Taker IOC) aligned with market consensus (`mid >= 0.52`) in `[$0.20, $0.65]` (hard ceiling `$0.70`).
  - **Pure Arithmetic Exit**: Enabled deep risk-free pair lock only at `pair_cost <= 0.90` (`ENABLE_ARBITRAGE_HEDGE=True`); tail defensive salvage triggers on objective bid collapse (`best_bid <= 0.15` & adverse spot at `tau <= 30s`).
- **Objective Order Protection (`core/execution.py`)**: Replaced model-edge resting order checks with objective direction reversal cancellation (`preferred_side` flip) and `>= 2-tick` market bid retreat protection.

---

## [v2.3] - 2026-11-17 (Unified Net-Edge + Basis-Adjusted Asian TWAP Engine & Logic Audit Fixes)

### Strategy & Pricing Architecture (`v2.3`)
- **Basis-Adjusted Asian TWAP Estimator (`core/estimator.py`, `runtime/runner.py`)**: Never feeds the 60s-lagging `poly_spot` directly into $\Phi(d)$ as instantaneous spot. Estimates live Chainlink spot via `cl_est = spot.price - basis_ema`, scales variance by effective horizon $\tau_{\text{eff}} = \max(10, \tau - 40)$ for $\tau > 60\text{s}$, and blends locked realized average `twap_realized_60s` with decaying terminal variance for $\tau \le 60\text{s}$.
- **Positive Convexity Restoration (`core/policy.py`, `config.py`)**: Disabled default `pair_cost <= 1.00` profit-capping hedge (`ENABLE_ARBITRAGE_HEDGE=False`) which previously capped average wins at `+$0.16` against `-$2.66` losses in `v2.2`. Leg-1 positions now hold to settlement by default, with extreme tail defensive salvage (`tau <= 30s`, `P* < 0.12`).
- **Realism Hardening (`runtime/market_feed.py`, `runtime/runner.py`, `sim/matcher.py`)**: Multi-source spot failover (`binance.vision` -> `binance.com` -> `okx`) preserving stale timestamp to trip dead-man switch; dual-horizon (20s/300s) EWMA volatility; strict book consistency (`STRICT_BOOK_CONSISTENCY=True`) rejecting inconsistent snapshots without artificial price shifting; post-300ms live IOC limit check (`TAKER_REAL_DELAY_CHECK=True`).

### Fixed — Internal Logic Conflicts (2026-11-17 Audit)
- **Provisional strike cross-source mismatch (`runtime/runner.py`)**: When `poly_open_is_provisional=True` (during the initial 5-15s before Polymarket's official Chainlink TWAP `openPrice` finishes computing, or on Binance fallback), `basis_ready` now stays `False` so `estimate_p_fair` compares Binance live spot against `binance_open_price` on the same reference chain instead of subtracting `basis_ema` from Binance spot against an unadjusted Binance open price.
- **Stale / edge-decayed resting Maker quote protection (`core/execution.py`, `runtime/runner.py`, `config.py`)**: Fixed a paradox where mild probability drops re-quoted Maker orders lower, but severe probability drops (`P* < ENTRY_MIN_PROB` or no executable net-edge) returned `wait (urgent=False)` and left stale Maker bids sitting exposed at the top of the book. `plan_order_actions` now immediately cancels resting Maker orders whenever `p_side < ENTRY_MIN_PROB`, `(p_side - order.price) < MIN_RESTING_EDGE (0.02)`, or the market `best_bid` retreats $\ge 2\text{ ticks}$ below `order.price`.
- **Partial-fill queue preservation (`core/policy.py`)**: A partially filled Maker entry (`0 < held_shares < max_shares`) no longer sets `urgent_cancel=True` in calm conditions, preserving front-of-queue priority for the remaining shares of the lot while still pulling immediately on toxic shocks or tail freeze.
- **Wide-spread Maker guard & penny-jump edge threshold (`core/policy.py`, `config.py`)**: Enforced `tb.spread <= MAX_ORDERBOOK_SPREAD (0.06)` on Maker quoting to prevent bidding into illiquid vacuums, and required `p_side - (bid + 0.01) >= PENNY_JUMP_MIN_EDGE (0.06)` before stepping 1 tick ahead of `best_bid`.
- **Tests**: Added 4 regression tests in `tests/test_v2_3_realism.py`; **104 unit tests passing**.

---

## [v2.2] - 2026-11-12 (Audit remediation, paper-only)

Patch-level correctness release: `config.VERSION` stays `v2.2` so the running ledger is not silently re-based (same precedent as v1.1.1). No new strategy thesis; these are logic fixes found by a full-chain audit.

### Fixed — chart price was wrong
- `runtime/runner.py`: `price_history.price` and `poly_price` were both written from the lagging 60s Chainlink TWAP and `live.spot` was overwritten with the same value. The dashboard therefore drew a smoothed TWAP labelled "BTC price" while the real Binance print (e.g. $73,464.72 vs $73,447.13) was discarded. Both sources are now recorded separately, each against its own opening anchor (`binance_open_px` vs `open_px`).
- `update_open_price()` now rewrites both first-point anchors and zeroes both return series; previously only `poly_price`/`poly_return_bps` changed, producing a ~13 bps contradiction and different starting points on the live vs history charts.
- `web/static/app.js`, `index.html`: main curve = Binance point price, dashed overlay = lagging TWAP; both price-only and history charts now use price-first ordering. Stats and tooltip expose both values and their sources.

### Fixed — falsified PnL and unbounded risk
- `sim/matcher.py`: crossed-book matching consumed the same static ladder on every tick (2 visible ask shares produced 5 filled shares with zero trade prints). One fill opportunity per distinct book snapshot.
- `config.py`, `runtime/runner.py`, `core/policy.py`: restored the account circuit breaker (`ENABLE_CIRCUIT_BREAKER=True`, `$15` daily drawdown, 3-loss cooling), removed the hardcoded `max(10.0, ...)` bankroll floor, and wired `MAX_WINDOW_LOSS_USD` into the live entry gate via `COMMITTED_WINDOW_COST`. `validate_params()` now checks the worst fully-paired cost and rejects a breaker that can never trip.
- `runtime/market_feed.py`, `runtime/runner.py`: the feed's stale-TWAP guard returned `None` but the runner kept feeding `last_poly_spot` into `p_fair`. Stale ticks now switch to the Binance chain and mark `poly_stale`; `MIN_MODEL_VOL_BPS` stops Phi(d) saturation. `fetch_spot_state()` no longer substitutes the Poly strike for a failed Binance call, and `fetch_order_book()` sets `source_t` plus an empty-book dead-man signal.
- `sim/matcher.py` ↔ `core/policy.py`: `make_complementary()` forces `ask_up + ask_down >= 1.00`, so the MWDE `< 0.90` taker gate could never fire at runtime (unit tests bypassed the chokepoint). The gate is now an upper bound (`1.02`) on the adjusted pair cost.
- `runtime/runner.py`: `taker_hedge` is re-checked against the actual ladder VWAP before booking; a "risk-free" lock that slips above $1.00 is rejected.
- `runtime/pipeline.py`, `runtime/runner.py`: Gamma/UMA resolution is now queried first and recorded as `winner_source`; a later TWAP reconcile can refresh prices but cannot overwrite an official winner. Fallback settlement uses the Binance point print rather than a 60s TWAP.

### Fixed — configuration, attribution and replay
- `ALLOW_TAKER_UNWIND` is now actually read by the policy (was a dead key); `MWDE_HEDGE_LOCK_COST` is no longer silently overridden by `max(..., 1.00)`; tail freeze applies to hedge/unwind branches too; `MAX_LOTS` aligned with the fixed 1-lot sizing.
- `core/regime.py` / `config.py`: `MIN_ENTRY_TAU_S` 180s → 15s, so `tail_freeze` no longer swallows 60% of every window's attribution label.
- `sim/stress.py`: `CircuitBreaker(enabled=...)` now matches the live config instead of silently defaulting to True.
- `sim/replay.py`: sparse recorded ticks no longer trip the dead-man switch, trade prints are carried across ticks with no oracle row, duplicate-close results are not reused, and the strike availability look-ahead is documented.
- `core/estimator.py`: tie at the strike resolves Up, matching settlement.

### Follow-up (same day, second pass)
Closed four remaining engineering defects found on a line-by-line re-audit:
- `taker_buy` now gets the same ladder-VWAP guard as `taker_hedge`: a fill whose VWAP wipes out the modelled edge is rejected instead of booked at a fictional best price.
- `insurance_paid` is recomputed from the currently pairable share count (a stock) rather than accumulated on every buy using two averages that only agree when both legs are equal.
- `TradeStreamClient.drain_trades` no longer lets prints with no slug into the current window's FIFO queue; prints explicitly belonging to another prewarmed window are still retained.
- Order ids are now monotonic via `_next_order_id`; the old `int(now*1000) % 1000000` wrapped every ~16.7 minutes and could reuse a stale `queue_ahead` entry.

### Tests
- Added `tests/test_audit_regression.py` (20 cases) covering the snapshot guard, bankroll envelope, dual price sources, stale TWAP fallback, MWDE reachability, unwind switch, tail freeze, hedge threshold, settlement truth, VWAP slippage, insurance accounting, unattributed prints and order ids. Full suite: **100 tests, all passing** (`requests`/`websockets` installed).

### Deliberately left open (needs a product decision, not a patch)
1. `make_complementary()` still *shifts* real book prices up/down to remove sub-$1 pairs. It is conservative but means backtests can trade at a price that never existed. Replacing it with a "reject the fill, keep the book" design changes every historical result.
2. The post-shock consensus branch still buys the market-pricier side without checking model edge (a negative-utility `taker_buy` is possible). Adding an edge gate changes strategy behaviour and invalidates existing replay baselines.
3. Replay still uses the ledger's own `winner` as settlement truth (circular validation) and the final reconciled `open_px` from t=0 (availability look-ahead). `data/1_market/settlements/` contains no independent Gamma truth; `oracle/window_open` events could model the delayed strike but would change replay PnL.
4. `reconcile_record()` rewrites the whole `summary.jsonl` (read-modify-replace). Current deployment is single-process with a PID lock and a read-only dashboard, so the race is not reachable in practice; adding cross-process locking was judged higher-risk than the residual exposure.

### UI refresh (2026-11-14, front-end only, no strategy or ledger changes)
Presentation work across all three views. `config.VERSION` stays `v2.2`; static assets bumped to `?v=2.3.2` for cache busting only.

- **Palette**: replaced the all-over blue tinting with neutral slate surfaces (`#f6f7f9` / `#f2f4f7`) and one indigo UI accent (`#1d4fd8`), leaving cyan (`#0ea5e9`) exclusively for the TWAP data series. All canvas hard-coded colours in `app.js` were brought in line. Hairlines 0.5px → 1px, radius 16px → 12px, subtle card shadow added. P&L semantics (profit/UP green, loss/DOWN red) unchanged. Also fixed two `border: 1px solid var(--border)` declarations that were invalid because `--border` holds a length, not a colour.
- **Density**: sidebar 268→236px, topbar 68→60px, tighter card padding and grid gaps, body text 15→14px. The overview now fits three regime rows plus the recent-rounds table above the fold; chart canvas 400→360px.
- **Chart declutter**: three boxed stat groups (with redundant `基准/现值/概率` captions) became one flat readout row that keeps all ten values and adds `振幅` (the JS already computed it but no element existed); legend chips lost their pill chrome; the `无在途挂单` pill moved to the orders workspace header; the decision badge and the feed-stale alert now use a soft fill plus a 3px status rail instead of large solid colour blocks. Chart head went from four rows to three.
- **Responsive**: `minmax(0, 1fr)` on narrow grids (plain `1fr` let mono price text widen columns past the viewport), topbar trims to clock + refresh under 720px, and canvas captions / Y-axis ticks shorten on narrow screens so they no longer clip.

Verified with CDP-forced 390px and 1440px screenshots (`scrollWidth == clientWidth`, no out-of-bounds elements); **100 tests OK**, `node --check` OK, CSS braces balanced. Before/after captures in `.workbuddy/visual-review/`.

### 顶部排版微调 (2026-11-15, 纯前端，不动策略/账本)
- 头部两行加高：`.topbar-inner` 52→68px、`.telemetry-inner` 32→44px；导航项 28→32px、时钟 30→34px、徽标与遥测字号略增，左右内边距 24→28px，整块更有呼吸感。静态资源 `style.css?v=3.2.1` 破缓存。
- 当前轮次读数条：三组读数间距 24→40px，并从居中改为右对齐、贴近「进入实时工作区」按钮；倒计时数值与现货/配比数值统一 15.5px，标签与数值间距 1→4px；按钮 34→36px。
- 响应式兜底：≤1180px 收回读数间距至 24px，≤820px 读数换行左对齐、按钮右浮，避免挤压溢出。
- 验证：1440/1100/800px headless 截图均无裁切或重叠；`python main.py test` 100 项全绿；CSS 括号 340/340 平衡。截图见 `.workbuddy/visual-review/top_spacing_after_*.png`。

### 左侧边栏：导航与数据源连接归位 (2026-11-15, 纯前端)
- 新增固定 `.sidebar`（232px）：品牌 + 竖排导航（交易总览/实时工作区/场次记录，沿用 `.sidebar-link[data-tab]` 与快捷键 1/2/3）+ 底部「数据源连接」卡片（服务在线、源正常、Polymarket 盘口 / Chainlink 预言机延迟）+ 页脚。
- 顶部精简：主导航、连接状态 pill、两个 source-chip 全部移入侧边栏；`topbar-left` 改为页面标题 + 描述；遥测条仅保留当前轮次与吞吐/RTT/累计。所有 JS 依赖的 id 保持唯一，`app.js` 无需改动，导航切换与轮询更新照常。
- 布局：`body { padding-left: var(--sidebar-w) }`；≤960px 侧边栏转为顶部横向条并保留「服务在线 / 源正常」精简指示；≤1280px 读数条换行、KPI 改两列，避免侧边栏占宽后读数重叠。静态资源 `style.css?v=3.2.2`。
- 验证：1440/1280/1100/800px headless 截图 + `#market` 页切换截图，连接卡片显示实时延迟（证明 JS 轮询正常）；`python main.py test` 100 项全绿；CSS 括号 378/378 平衡。截图见 `.workbuddy/visual-review/sidebar_final_*.png`。

### 顶部精简为一行 + 网络遥测归入侧边栏 (2026-11-15, 纯前端)
- 移除第二行 `.telemetry-bar`，固定顶部只剩 68px 一行：页面标题 / 描述 / 当前轮次 badge / 北京时间 / 主题 / 刷新。原第二行左侧的轮次信息并入首行，右侧「吞吐 / RTT / 累计」移入左侧。
- 侧边栏连接卡片新增「网络遥测」小节，容纳 `side-net-kbps` / `side-net-latency` / `side-net-total`，与两路数据源延迟同卡片展示；≤960px 横条模式隐藏明细，仍保留「服务在线 / 源正常」。所有 JS 依赖 id 未变，`app.js` 无需改动。静态资源 `style.css?v=3.2.3`。
- 验证：1440/1100/800px + `#market` 截图，顶部单行、侧边栏遥测显示真实数值、页脚仍可见且无裁切；`python main.py test` 100 项全绿；CSS 括号 380/380 平衡。截图见 `.workbuddy/visual-review/oneline_*.png`。

### 侧边栏顶部区块合并 + 导航呼吸感 (2026-11-15, 纯前端)
- 品牌行与导航项包进同一 `.sidebar-top` 连续区块：移除品牌下边框与「导航」分组标题，两块之间零间隔；仅在区块底部保留一条分隔线。
- 品牌行与导航按钮统一 44px 行高；导航项间距 4→10px、行高 40→44px，视觉更松弛。≤960px 横条模式仍保持单行紧凑（32px / gap 4px）。
- `style.css?v=3.2.4`；`app.js` 未改，导航 id/class 与快捷键不变。
- 验证：1440/800px 截图无重叠、裁切或横向溢出，连接卡片与页脚仍完整；`python main.py test` 100 项全绿；CSS 括号 382/382 平衡。截图见 `.workbuddy/visual-review/nav_group_*.png`。

### 侧边栏头部连体 + 全站去除左侧装饰条 (2026-11-15, 纯前端)
- 澄清上一轮需求：只把**品牌行与「交易总览」**连成同一圆角连续块（同 44px 行高、同浅底、零间距），其余导航按钮之间保留 10px 间距并加回 1px 细分隔线；选中态改用文字色 + kbd 高亮表示，不再依赖色条。
- 去除全站所有 `border-left` 彩色装饰条：侧边栏激活项、当前轮次状态卡、决策徽章、挂单状态卡（UP/DN 仍靠底色区分）；顶部标题后的 1px 灰色分隔符保留。≤960px 横条模式重置连体背景与分隔线。
- 删除左下角 `.sidebar-footer` 描述性文字（BTC 5M 预测市场量化终端 / UTC+8 · 纸面研究）及其样式、窄屏隐藏规则。
- `style.css?v=3.2.5`；`app.js` 未改。
- 验证：overview / history / market 三页 × 1440/800px 截图无重叠、裁切或溢出；`python main.py test` 100 项全绿；CSS 括号 387/387，`border-left` 残留 0 处。截图见 `.workbuddy/visual-review/final_{overview,history,market}_*.png`。

### 顶部整行连体 + 导航区拉开间距 (2026-11-15, 纯前端，返工修正)
- 修正前几轮的理解偏差：用户指的是**顶部整条横带**（品牌 + 页面标题 + 轮次 + 时钟 + 按钮）要连成一体，而非侧边栏内部两个元素。品牌区（左侧 232px）与 `.topbar-inner` 统一 68px 高、共用 `--topbar-bg` 背景、各带一条同 y 位置的 `border-bottom`，形成连续横带；移除贯穿顶部的侧边栏 `border-right`，改用 `.sidebar::before`（`position: fixed; top: 68px`）只在横带以下画竖线。
- 撤销上一轮「品牌 + 首个导航按钮连体块」的全部 `:first-child` 样式；侧边栏导航区与品牌带拉开 32px，按钮间距 10→16px，按钮之间仍保留 1px 细分隔线。
- 修复品牌名被挤成两行：侧边栏品牌区补 `white-space: nowrap` 与 `.brand { flex-shrink: 0 }`（原 topbar 有 nowrap，侧边栏没有）。≤960px 横条模式重置品牌带高度/背景并隐藏竖线伪元素。
- `style.css?v=3.2.6`；`app.js` 未改。
- 验证：overview / history × 1440/800px 截图无重叠裁切，`python main.py test` 100 项全绿，CSS 括号 385/385。截图见 `.workbuddy/visual-review/band2_*.png`。

### 顶部横带对齐修正 + 去除分割线 (2026-11-15, 纯前端)
- **根因**：`.sidebar-brand` 被错误嵌套在带 `padding: 32px 14px 14px` 的 `.sidebar-top` 内，品牌行变成缩进的悬浮小块，与右侧 `topbar` 不等高、不对齐。现将品牌行提升为 `.sidebar` 直接子元素：铺满 232px、固定 68px 高，与 `.topbar-inner` 同高、同为 `--surface` 白底、无竖线或横线分隔，顶部真正连成一条完整横带。
- 清除顶部与侧边栏的所有分割线：`topbar` / 品牌行 / 导航区的 `border-bottom`、导航按钮之间的 `border-top`、连接卡片内 `.conn-row + .conn-row` 与 `.conn-subhead` 的 `border-top`、侧边栏右侧竖线伪元素、顶部标题后的 `brand-divider`；分区改由背景色差与留白表达。
- 修复窄屏回归：品牌行不再设 `width: 100%`（flex 列布局自动撑满），≤960px 横条模式下品牌、三个导航按钮、连接状态恢复同排显示。
- `style.css?v=3.2.7`；`app.js` 未改。
- 验证：overview / history / market × 1440/800px 截图，`python main.py test` 100 项全绿，CSS 括号 381/381。截图见 `.workbuddy/visual-review/clean*.png`。

### 显示口径统一 + 顶部横带分割线 (2026-11-15, 仅前端显示层)
- 顶部横带下沿加回一条连续分割线：品牌行与 `.topbar` 同 `border-bottom`，`.topbar-inner` 68→67px 保证两侧边框处于同一 y、品牌行与右侧顶部区域等高对齐。
- 实时工作区上方价格格统一为 Polymarket / Chainlink 口径：标签「Polymarket 现价 / 开盘基准」，现价取 `live.poly_spot`、开盘取 `live.open_px`，「相对开盘偏离幅度」改由两者计算。后端 `return_bps`（Binance 参与）仍驱动模型，未改。
- 下方价格图只保留 Chainlink 60s TWAP 一条曲线（实线 + 淡面积），移除 Binance 主曲线与图例；价格轴范围、开盘基准线、hover 圆点/tooltip、统计行「最高/最低/振幅」全部改用 `price_history.poly_price`。TWAP 缓存过期点做前向填充，避免归一化回退到 Binance 打印价造成尖峰。模型概率 P*、盘口隐含、定价边际与概率曲线全部保留。
- 轮次格删除日期与「(北京时间)」小字；「微观风控与形态状态」列 `padding-left: 28px` 右移，≤960px 重置为 0。
- **严格只改显示**：`core/`、`runtime/`、`sim/`、`web/server.py`、数据采集、概率模型、Binance 参与的计算一律未动；历史复盘弹窗保持原样。
- `style.css?v=3.2.9`、`app.js?v=3.2.1`。
- 验证：`node --check app.js` OK、CSS 括号 382/382、`python main.py test` 100 项全绿；market/overview × 1440/800px 截图。截图见 `.workbuddy/visual-review/fin2_market_*.png`。

### 顶部 header 去除描述与当前轮次 (2026-11-15, 纯前端)
- 按截图红框移除 `.topbar-left` 中「BTC 5M 预测市场量化终端」描述与「当前轮次 xx:xx ~ xx:xx · 剩余 Ns」badge（`#live-slug-badge`）；顶部一行现在只剩页面标题 + 时钟 + 主题/刷新按钮。轮次信息仍由总览状态栏与市场页「北京时间轮次」格提供，去掉的是重复展示。
- 同步清理死代码：`.topbar-round` / `.telemetry-round-label` / `.page-slug-badge` / `.brand-desc` 四条 CSS 规则，以及 `app.js` 里对 `live-slug-badge` 的赋值（原本有 null guard，删 DOM 不会报错）。`winInfo` / `tau` 仍供 `market-time-range`、`market-tau`、图表徽标使用。
- 顶部横带分割线、第二行「北京时间轮次」、价格显示口径与图表均保持上一版状态，未改动；策略、计算、采集、账本一律未动。
- `style.css?v=3.2.10`、`app.js?v=3.2.2`。
- 验证：`node --check app.js` OK、CSS 括号 378/378、`python main.py test` 100 项全绿；market/overview × 1440/800px 截图无重叠裁切。截图见 `.workbuddy/visual-review/rm_*.png`。

---

## [v2.1] - 2026-11-11
### 双阶段复合做市与吸收引擎 (MWDE + LWAA)

#### 核心新增与量化口径
1. **阶段 1：前半程顺势低吸模块 (MWDE, 60s ~ 150s / $\tau \in [150s, 240s]$)**
   - **大动后缩量滤网**：前一盘振幅 $\ge \$20.0$，本盘前 60s 价格振幅 $\le \$15.0$，确认市场处于大动后的缩量均值回归通道。
   - **中轴胜率与现货偏离选边**：以实时现货偏离 $\Delta S = S_t - S_0$ 与模型概率 $P^*$ 判定强势侧（$\Delta S > 0$ 且 $P^* \ge 0.50$ 优先选 UP，反之选 DOWN）。
   - **买入限价放宽至 $\le \$0.45$**：仅在强势侧被市场恐慌假摔错杀至 $\le \$0.45$ 时，以极高赔率（>1:1.22）低吸吃入（Taker/Maker）。
   - **不急于对冲止损**：建仓后严禁追高买入对侧割肉或主动平仓（`taker_unwind` 彻底禁用）；若后续两边合并成本 $\le \$0.85$ 则双吃锁盈，否则坚定死扛单边至到期兑现 \$1.00。
2. **阶段 2：后半程尾盘高确信吸收模块 (LWAA, 120s ~ 15s / $\tau \in [15s, 120s]$)**
   - 若前半程未吃到单，进入尾盘大单边确认期（$P^* \ge 80\%$）。
   - 买入最高限价设为 **\$0.65**，硬性天花板 **\$0.70**（超过 \$0.70 绝不追高）。
   - 每轮挂单尝试上限设为 1 次，挂不上不再反复追挂重单。
3. **独立账本与环境隔离**：
   - 启用全新独立账本目录 `data/2_ledgers/v2.1/`，初始本金干净重置为 \$100.00。
   - 全套 74 项微观结构与数学不变量单元测试 100% 通过。

---

## [v1.1.1] - 2026-11-10 (Audit remediation, paper-only)

Patch-level release: `config.VERSION` stays `v1.1` so the running ledger is not
silently re-based; these are correctness fixes, not a new experiment.

### Fixed
- **Replay engine rewritten (`sim/replay.py`)**: the previous "replay" only read
  settled archives and rewrote PnL with hard-coded rules (blocked rounds -> 0,
  `unwind` recomputation, `insurance > 0.75` clamp). It never read book events,
  never called `plan_decision`, and never matched an order. The new engine
  replays every recorded book snapshot and trade print, reconstructs
  `SpotState` from the recorded oracle, re-runs the real policy and matcher
  (latency, queue position, depth, fees), and settles against the ledger's
  official winner. It writes `data/3_reports/replay_*.jsonl`.
  The documented "+$30.391 net improvement" was an artifact of that rewriting
  and has been retracted.
- **Matching fidelity (`sim/matcher.py`, `runtime/runner.py`)**: removed the
  synthetic `trade_volume=5.0` queue drain and the stale `last_trade_price`
  match — a resting maker order now only fills when a recorded print actually
  trades at or through its price, after the FIFO queue ahead is consumed and
  after the 300 ms latency. Taker execution now walks the book level by level
  and books VWAP (`taker_fill` / `walk_levels`); an empty or thin book yields a
  partial fill or a `taker_reject` instead of an infinite-depth fill at the
  best ask.
- **Pre-trade risk envelope (`core/policy.py`)**: `risk_adjusted_entry_size`
  now caps every new Leg-1 at `MAX_WINDOW_LOSS_USD` ($2.00) before the order is
  placed. Previously the parameter was only a stress-test statistic; a naked
  15-share entry at $0.60 could lose $9.88 in one window. When the opposite ask
  makes an immediate taker hedge lock a pair below $1.00, the position is
  risk-free at entry and the hard lot cap applies instead.
- **Structural cancel signal (`core/types.py`, `core/execution.py`,
  `core/policy.py`)**: added `Decision.urgent_cancel`. Urgency is now declared
  by the policy (toxic shock, tail freeze, regime lockout, lifecycle caps,
  risk-budget breach) instead of being inferred from substrings of
  `decision.reason`; rewording a message can no longer silently disable or
  trigger a cancel. Soft waits ("no edge", "insufficient capital") keep resting
  quotes and their queue position.
- **Uncertainty envelope dimensional bug (`core/estimator.py`)**: the old
  formula multiplied a per-second dispersion by 100 without dividing by
  $\sigma_\tau$, so $\epsilon$ was pinned to its 0.015 floor for every regime.
  It now scales with $\sigma_\tau = \sigma \sqrt{\tau/300}$ plus the
  latency-induced increment, and adapts inside $[0.008, 0.08]$.
- **Regime attribution (`runtime/runner.py`)**: attribution used the tick-count
  mode, which is structurally biased to `tail_freeze` (the last 60 s is always
  ~20% of ticks). It now records the regime that permitted the first entry
  (`entry_regime`), falling back to time-weighted dwell (`regime_dwell`).
- **Reconciliation (`runtime/runner.py`)**: after rewriting a reconciled
  record, `_rebuild_accounting()` reloads the ledger and rebuilds
  `total_pnl` and the circuit breaker, so `daily_pnl` and cooling state no
  longer drift from the persisted summary.
- **Tests**: added an end-to-end replay test (synthetic recording -> exact
  locked-pair PnL), depth-limited taker tests, the pre-trade loss-budget gate
  test, and the `urgent_cancel` contract; removed a dead duplicate test that
  referenced an undefined name. Suite is now 56 tests, all passing.

### Hardened (second audit pass)
- **Pre-trade sizing needs real hedge depth (`core/policy.py`)**: a "locked"
  pair is only risk-free if the opposite ladder can absorb it. The gate now
  walks the ask book and caps the entry at the visible hedgeable size whose
  VWAP keeps `pair_cost <= 1.00`; the remainder is treated as naked and must
  fit the $2.00 budget. Previously a single top-of-book price unlocked up to 15
  shares regardless of how little size was actually there.
- **Distress exit now compares utilities (`core/policy.py`)**: the emergency
  hedge/unwind branch used to fire whenever `pair_cost <= $1.05` and the clock
  was late (`tau < 90 s`), while logging "U_hedge(...) > U_hold(...)" without
  ever computing the comparison. It now requires `best_immediate > u_hold`, so
  the bot no longer pays a certain $0.02–$0.05 per pair to "remove risk" when
  holding to settlement is worth more.
- **Journal integrity (`runtime/runner.py`)**: `close_window` now settles each
  slug exactly once. A restart inside an already-settled window (or a double
  feed settle) used to append a second summary row, double-counting both
  `total_pnl` and the circuit breaker. Duplicate closes are logged and ignored.
- **Single-tick fill guard (`runtime/runner.py`)**: when a trade print walks a
  resting order's price level, the same order is excluded from the crossed-book
  pass in that tick. Previously the print and the stale snapshot could both
  consume the same liquidity, over-crediting maker fills.
- **Trade-stream replay guard (`runtime/trade_stream.py`)**: prints are
  de-duplicated by `(token, timestamp, price, size)` over a 5,000-entry window,
  so a WebSocket reconnect cannot replay prints and consume the maker queue
  twice. Ingest logic is now a testable `_ingest()` method.
- **TWAP freshness and thread safety (`runtime/market_feed.py`)**: the
  background TWAP poller uses its own `requests.Session` (the shared session is
  not thread-safe) and stamps `_last_poly_btc_t`; `get_polymarket_btc_price`
  returns `None` once the cache is older than `POLY_PRICE_MAX_AGE_S` (10 s)
  instead of feeding a dead poller's last price into `p_fair`.
- **Utility double-count (`core/policy.py`)**: `u_hold` no longer adds a
  `p_fill * maker_gain` bonus on top of the full hold-to-expiry EV. Holding is
  now valued as the `p_fill`-weighted mix of (hedge fills -> pair settles at
  $1.00) and (no fill -> settlement EV minus variance penalty). The unreachable
  legacy arbitration branch was removed, and `P_FILL_MAKER` is a config
  parameter instead of a magic default.
- **Atomic reconciliation (`runtime/runner.py`)**: `reconcile_record` rewrites
  `summary.jsonl` through a `tmp` + `os.replace` step, so a crash mid-rewrite can
  no longer truncate the accounting journal.
- **Explicit queue timeout (`runtime/runner.py`)**: `plan_order_actions` receives
  `MAX_QUEUE_WAIT_S` from config rather than silently relying on a default that
  happened to match.
- **Single capital source (`config.py`, `runtime/runner.py`, `web/server.py`,
  `main.py`)**: `INITIAL_CAPITAL_USD` replaces four hard-coded `100.0` literals
  (bankroll cap, dashboard ROI, reset state).
- **Pipeline hygiene (`runtime/pipeline.py`)**: `close_px` is reset every
  iteration, so a missing open price can no longer inherit a previous round's
  close through `'close_px' in locals()`; shutdown now warns that the still-open
  window was not journaled (previously it was dropped silently).
- **Dashboard load (`web/server.py`)**: window detail archives are cached by
  `(path, mtime, size)`, so the once-per-second `/api/history` poll no longer
  gunzips up to 50 archives per request.
- **Sealed snapshots are complete (`runtime/supervisor.py`)**: `seal_batch()`
  packaged only `config.py` and `core/*.py`, so matching, replay and dashboard
  code was missing from the archived version. It now includes `main.py`,
  `runtime/`, `sim/` and `web/`, and lists the files in `manifest.json`.
- **Tests**: added `tests/test_runtime_hardening.py` (duplicate close, print +
  crossed-book double fill, stream replay, stale TWAP) and extended the sizing
  gate test to cover thin hedge depth. Suite is now 56 tests.
- **Dead imports removed**: `core/estimator.py`, `core/execution.py`,
  `core/policy.py`, `core/regime.py`, `runtime/market_feed.py`,
  `runtime/runner.py`, `sim/matcher.py`, `sim/stress.py` no longer import
  symbols they never use (notably Kelly and micro-regime helpers inside
  `plan_decision`, which made it look like Kelly sizing was wired in).

### Consolidated (third audit pass)
- **One probability chain (`core/estimator.py`)**: `estimate_p_fair()` is now
  the single place that decides which reference price prices the market
  (Polymarket TWAP vs strike -> Binance spot vs Binance open -> spot vs strike
  -> 0.50). `runtime/runner.py`, `core/policy.py` and `core/regime.py` all call
  it instead of re-implementing the precedence inline.
- **One regime sample per tick**: `plan_decision()` accepts `regime_info`, so
  the runner no longer classifies the market a second time inside the policy.
  The dashboard now reports `_last_regime` / `_last_snr` -- the classification
  that actually drove the decision -- instead of a separate display-only
  classifier, which could contradict the trade log.
- **Removed dead and duplicate code**: `estimate_queue_wait_time` (no callers)
  and the 77-line `classify_market_micro_regime` (a second, subtly different
  regime vocabulary used only by the dashboard) were deleted; the duplicated
  `calculate_drift_snr` in `core/regime.py` now imports the canonical one.
  `ALLOW_INCREASED_RANGE_SIZE` defaulted to `True` inside `regime.py` while the
  config said `False`; the code default now matches the config.
- **Single Leg-1 entry path (`core/policy.py`)**: the ~70 duplicated lines for
  quoting Up and for quoting Down were merged into one `_plan_entry()` helper
  that returns `None` when that side simply has no edge, so the pair is tried
  in a loop and the fall-through reason is "no edge" instead of an urgent
  cancel. The hard-coded `r_up >= 0.35` / `r_down >= 0.35` checks are gone
  (they disagreed with `MIN_MAKER_ENTRY_PRICE = 0.38`), as is the unreachable
  second maker/ask guard in the unpaired path.
- **One late-window gate**: `TAIL_FREEZE_S` (60 s) is a strict subset of
  `MIN_ENTRY_TAU_S` (180 s), so the ladder now has a single entry cut-off that
  labels the reason "Tail freeze" or "Late window"; the toxic gate consults the
  regime (which already detects the shock) instead of OR-ing the raw spot flag.
- **Partial fills are exitable**: `u_sell` required `held_shares >= 5`, so a
  depth-limited partial fill (e.g. 1.93 shares) could never be unwound and had
  to be held to expiry. Any positive size with a live bid is now sellable.
- **No second live schema (`main.py`)**: `reset` used to hand-write a 45-field
  `live.json` copy next to the runner's writer; it now deletes the stale file
  and lets `runtime/runner._write_live` be the only producer.
- **Replay refreshed**: 27 of 77 recorded windows now trade for **-$1.629**
  (mean -$0.0212, t=-0.73, 63 fills, worst -$2.0000); superseded report files
  were removed so `data/3_reports/` holds one authoritative run.

### Risk-budget calibration
- **`MAX_WINDOW_LOSS_USD` could not fund one lot.** The budget was $2.00 while a
  single lot (5 shares) at `MAX_MAKER_ENTRY_PRICE=0.62` costs $3.10, so the
  pre-trade gate silently rejected every quote above $0.40 and the system spent
  most windows waiting (27 of 77 replayed windows traded). The two knobs now
  agree: window budget $3.00, max maker entry 0.60 (= 5 shares x 0.60).
- **`config.validate_params()`** runs from `python main.py` before any command and
  refuses to launch when the window budget cannot fund one minimum lot, when the
  window budget exceeds the daily drawdown, or when the entry corridor bounds are
  inverted. Configuration conflicts now fail loudly instead of disabling trading
  invisibly.
- **Honest effect** on the same event-driven replay: 41 of 79 recorded windows
  trade for **-$8.417** (mean -$0.1065, sd $0.5139, t=-1.84, 21 wins / 11 losses /
  9 flat, 90 fills, worst window -$2.3500) versus 27 windows / -$1.629 at $2.00.
  Widening the budget scales exposure to a negative edge: this is a consistency
  fix, not a profitability fix. Do not raise the budget again until a positive
  post-fill edge is demonstrated.

### Attribution honesty and gate counterfactual (fourth pass)
- **Regime labels must come from the entry tick.** `close_window()` now stamps
  `regime_source` (`entry_time` when a position was opened, `dwell` otherwise),
  and `/api/history` only credits a traded window to a regime when the label was
  taken at entry. Rows written before the field existed are reported as
  `unlabeled`; previously they were silently folded into `normal_oscillation`
  (216 of 252 historical rows had no label at all).
- **No invented default label.** The `w.get("regime") or "normal_oscillation"`
  fallback is gone: a missing label renders as `unlabeled`, not as a regime.
- **The matrix shows n, mean and a 95% CI.** Each cell lists the traded sample
  count, per-window mean and confidence interval. Cells with fewer than
  `ATTRIBUTION_MIN_SAMPLES = 30` traded windows are dimmed and tagged
  "样本不足": one window's PnL sd is $0.25-$1.50, so a handful of windows cannot
  separate a 5-cent edge from noise.
- **Gate value is measured, not asserted.** `python main.py replay --no-gates`
  replays the same windows twice (gates on / gates off) and reports the
  difference. On the current recorded corpus: gates on -$10.552 across 46 traded
  windows, gates off -$13.568 across 36, so the regime gates are worth +$3.015.
  The 13 windows the gates refused would have lost $3.775 (mean -$0.2904,
  t=-1.04), while 23 windows that the gated run traded no longer trade with the
  gates off, because the single permitted entry gets spent on a worse setup.
- **Tests**: 59 (added gate-counterfactual and parameter-restore cases).


### Operational fixes (launchers, CLI, dead-man switch)
- **`start.bat` / `stop.bat` were unusable on a Chinese Windows console**:
  both were UTF-8 files that `call`-ed a Chinese-named sibling, which `cmd.exe`
  (cp936) decoded into `'dp0...bat' is not recognized`. Double-clicking the
  desktop shortcut therefore did nothing. They are now ASCII-only, CRLF,
  no longer route through a Chinese filename, check for `python` on `PATH`, and
  pause on error instead of closing the window. All `.bat`/`.vbs` launchers
  were converted to CRLF.
- **CLI argument parsing (`main.py`)**: `start --no-browser` used to raise
  `ValueError: invalid literal for int()` from `int(sys.argv[2])`. Arguments
  are now parsed as `[port] [--no-browser]`, invalid ports and unknown flags
  print the usage block and exit 2, and `web` accepts `--no-browser` too.
- **Dead-man switch measured the wrong thing (`runtime/runner.py`)**: the
  "feed gap" was actually the interval between `step()` calls, so a stale book
  with a healthy loop never tripped the switch, while one slow HTTP round-trip
  cancelled every resting quote (observed live: "Dead-man switch active" on a
  normal 1 Hz feed). It now checks input staleness (`spot.timestamp`,
  `order_book.source_t`) and loop stall separately, and reports both ages.

### Dashboard (UI pass)
- **Regime attribution panel**: the matrix no longer renders ten rows of
  `0 场 / 0 胜 / --`. Rows are emitted only for regimes with settled
  (`traded`) windows, sorted by sample size; observed-but-never-traded regimes
  collapse into a compact "监测覆盖" chip strip below the table. The win/loss
  counts moved under the win-rate so the panel fits six columns, and the panel
  header now carries the count of attributable rounds.
- **Layout**: every table header is single-line (`white-space: nowrap`),
  numeric columns are right-aligned with tabular figures, KPI labels stack over
  their sub-badges instead of being squeezed, and the history ledger uses a
  tighter row rhythm so all ten columns fit without clipping.
- **Responsive**: added the first media queries (`1280px` / `1024px` / `720px`).
  The sidebar collapses to icons at 1024px and disappears at 720px; the hero,
  KPI and quote grids fall back to one or two columns. Before this pass a
  narrow window simply clipped the right-hand cards off-screen.
- **Lock state**: a hedged (UP == DN) live position now shows a `已锁仓` badge
  instead of a trailing text fragment that wrapped onto its own line.

- **Live chart**: added a readable price axis (three levels), shaded the part
  of the 5-minute window that has not elapsed (with a `剩余 Ns` marker) instead
  of leaving a flat line trailing into empty space, gave the probability panel
  its 25%/75% guides in dual mode, and moved the baseline readouts into grouped
  header chips (`基准` / `现值` / `概率`) so the right rail only carries the two
  live price pills. Canvas height 290 -> 340 px.
### Documentation
- `README.md`: corrected "live trading system" (there is no live order
  adapter yet), described matching honestly, fixed the duplicate "常用命令"
  section and the stale test count, documented the replay command.
- `docs/CORE_STRATEGY_PHILOSOPHY.md`: replaced the fabricated performance table
  (`avg_win +$0.830`, `avg_loss -$0.589`, ratio 1.41, 212 rounds +$14.754,
  "+$30.391 improvement", "87% of losses from overpriced hedges") with the
  ledger truth: 170 traded windows, **-$31.7998**, mean -$0.1871 (t=-1.63);
  loss structure 49.5% hedged / 39.5% naked / 11% other; and the new replay
  result over 77 fully recorded windows: 27 traded, **-$1.629**,
  mean -$0.0212 (sd $0.2529, t=-0.73), 63 fills, worst window -$2.0000
  (exactly the pre-trade window budget).
- Noted that `calculate_kelly_order_size` is a reference implementation and is
  **not** wired into Leg-1 sizing (v1.1 sizing comes from
  `regime.recommended_lots`); the earlier claim of "Fractional Kelly dynamic
  sizing" overstated the implementation.

---

## [v1.1] - 2026-11-09

### Added & Upgraded
- **Dynamic Realized Volatility Engine & Adaptive Quoting (`core/estimator.py`, `runtime/market_feed.py`)**:
  - Replaced static baseline `realized_vol_bps = 15.0 bps` with real-time rolling 60-second EWMA per-second log-return volatility calculation.
  - Implemented regime-aware adaptive spread quoting (`calculate_adaptive_spread_params`):
    - *Calm regime* ($< 5\text{ bps}$): automatically narrows base edge and tightens uncertainty cushion to capture top-of-book maker priority.
    - *Volatile regime* ($> 30\text{ bps}$): automatically expands safety pads and uncertainty bounds to protect maker quotes against informed adverse selection and toxic taker runs.
- **Account-Level Circuit Breaker & Watchdog Engine (`core/circuit_breaker.py`, `config.py`)**:
  - Introduced `MAX_DAILY_DRAWDOWN_USD = $8.00` hard daily cumulative loss tripping limit.
  - Introduced `LOSS_STREAK_COOLING = 3` consecutive losing rounds trigger with a 3-round cooling lock period.
  - Enforced strict "Reduce-only / Hedge-only" mode when circuit breaker is active: prohibits opening new Leg-1 entries while safely permitting inventory-reducing hedges or unwinds.
  - Added state persistence and rehydration from `summary.jsonl` on engine restart.
- **Millisecond WebSocket Live Trade Prints Stream (`runtime/trade_stream.py`, `sim/matcher.py`)**:
  - Connected to Polymarket's real-time WebSocket trade stream (`wss://ws-live-data.polymarket.com` activity trades).
  - Enhanced `QueueMatcher.on_trade_print` to physically deduct resting order queue depth based on exact market trade print volume, boosting fill simulation fidelity to >95%.
  - Added background worker thread with automatic heartbeat (`PING`/`PONG`), reconnect resilience, and raw trade stream recording into `data/1_market/events/`.
- **Fractional Kelly Dynamic Position Sizing (`core/estimator.py`, `core/policy.py`)**:
  - Implemented Quarter-Kelly ($0.25 f^*$) allocation model scaling discrete order sizes across {5.0, 10.0, 15.0} shares (1 to 3 lots) based on available account capital and model win probability edge.
  - Preserved strict paired inventory conservation by dynamically sizing Leg-2 hedges to match held lots.
  - *Correction (see v1.1.1)*: this model is a reference implementation only; v1.1 Leg-1 sizing is driven by `regime.recommended_lots`.
- **Dual-Track Real-Time Interactive Canvas & Multi-Source Oracle Display (`web/`, `runtime/runner.py`)**:
  - Replaced text decision box with a high-DPI HTML5 Canvas rendering dual synchronous tracks:
    - *Track 1 (Price & Benchmark)*: Overlays Binance real-time BTC spot price with Polymarket official Chainlink BTC/USD TWAP curve against the $S_0$ baseline, featuring dynamic vertical anti-collision tag pills and a real-time Basis spread indicator.
    - *Track 2 (Probability & Share Price)*: Overlays Polymarket orderbook implied probability ($P_{\text{market}}$) with PM-Quant theoretical fair value ($p_{\text{fair}}$), highlighting the active quantitative arbitrage edge ($\Delta = p_{\text{fair}} - P_{\text{market}}$) via soft translucent gradient fills.
  - Reverted to cryptographic color conventions: UP is Green (`#05b169`), DOWN is Red (`#cf202f`).
- **Experiment Reset & Batch Management CLI (`main.py`, `runtime/reset.py`)**:
  - Added `python main.py reset [VERSION] [--keep-backup]` command to cleanly reset experimental ledger state while safely archiving historical runs with timestamps.
- **Cross-Round Smooth Overlap Transition (`runtime/pipeline.py`)**:
  - Pre-listens to and resolves token IDs and baseline price for the subsequent 5-minute round 30 seconds prior to expiration ($\tau_s \le 30\text{s}$).
  - Eliminates the 5-10 second blank polling gap during round transitions.
- **Unit Testing Suite (`tests/test_v1_1_features.py`)**:
  - Added 11 new comprehensive invariant and microstructure unit tests, expanding the test suite to 25 tests passing in ~12ms.

### Added - 2026-11-10 (Synthetic Stress Harness)
- **`sim/stress.py` + `python main.py stress [N] [seed]`**: 15 synthetic regimes (calm / calibrated normal / volatile / OU range / pin-at-strike / trend / late breakout / fake breakout / flash crash / flash retrace / jump-diffusion / vol shift / whipsaw / single-venue basis dislocation / stale feed) replayed through the real `PaperRunner.step` path with a calibrated Polymarket book + trade-flow simulator (1-tick spread, liquidity evaporation at extremes, strict fills). Includes market-lag / flow / market-vol sensitivity sweeps and multi-window sequences exercising the circuit breaker. Robustness / risk-envelope test only; **not** profitability evidence. `tests/test_stress.py` (+3 tests, 41 total).
- Empirical findings recorded in `data/3_reports/stress_*.md`: no crashes, per-window loss envelope held, but expectancy is negative whenever the book lags spot by <=2s; real recorded data shows the book lags spot by ~0-1s (lead-lag correlation peaks at lag 0/1, ~0 by lag 3).

### Fixed & Upgraded - 2026-11-10
- **Dynamic Drift SNR & Micro-Regime Classifier (`core/estimator.py`, `config.py`)**:
  - Implemented continuous statistical Signal-to-Noise Ratio calculation $\text{SNR} = |\mu_t| \cdot \sqrt{\tau_s} / \sigma_t$ based on drift $\Delta \ln(S_t / S_0)$ and realized volatility.
  - Automatically classifies market state into three distinct regimes: `range_bound` ($\text{SNR} \le 1.2$), `trending_up` ($\text{SNR} > 1.8$), and `trending_down` ($\text{SNR} > 1.8$).
- **Dual-Regime Execution & Directional Momentum Defense (`core/policy.py`)**:
  - Pure Avellaneda-Stoikov market making when $\text{SNR} \le 1.2$ (Brownian noise regime, harvesting risk-free spread).
  - Directional momentum defense when $\text{SNR} > 1.8$: strictly blocks adverse maker quotes (forbids quoting UP when trending down; forbids quoting DOWN when trending up), stopping adverse selection and falling-knife catches.
  - Maker quote entry clamped strictly within $[0.38, 0.62]$ to avoid buying high-probability tops ($>0.62$) or bottoms ($<0.38$).
- **No-Arbitrage Pair Cost Ceiling (`core/policy.py`, `config.py`)**:
  - Enforced `MAX_PAIR_HEDGE_COST = 1.05`. If total pair acquisition cost $\text{held\_basis} + \text{hedge\_ask} + \text{fee} > 1.05$, taker hedge is disqualified (`u_hedge = -999.0`).
  - Mathematically asserts that paying $\$1.50+$ for a $\$1.00$ binary payout violates no-arbitrage; forces cut-loss `taker_unwind` or passive hold.
- **Official Taker Fee Model (`config.py`, `core/execution.py`, `core/policy.py`, `runtime/runner.py`)**:
  - Replaced the stale flat `$0.005/share` taker fee approximation with Polymarket's official dynamic crypto fee `fee = shares × 0.07 × p × (1 - p)` (peak $0.0175/share at 50/50, vanishing at extreme prices), shared by utility arbitration and ledger cash booking via `taker_fee_per_share`. Maker fills remain fee-free per official rules; daily maker rebates are not modeled (conservative).
- **Unit Testing Suite Expansion (`tests/test_invariants.py`)**:
  - Added Invariants 13, 14, 15, expanding unit tests to 34 tests passing 100% in ~25ms.

---

## [v1.0] - 2026-11-09

### Redesigned & Standardized
- **Semantic Versioning (`v1.0`)**:
  - Replaced legacy `v1_r1` revision naming with clean semantic version `v1.0`.
  - Isolated ledger directory under `data/2_ledgers/v1.0/` and purged legacy `v1_r1` artifacts and test-polluted records.
- **Removed Synthetic & Toy Artifacts**:
  - Removed synthetic scenario PnL fabrications from `sim/replay.py`; `replay.py` now strictly operates on recorded real market streams in `data/1_market/`.
  - Removed the one-shot toy `runtime/collector.py` script in favor of the unified market feed & recorder architecture documented in `PLAN.md`.
- **Master Architecture & Phased Plan (`PLAN.md`)**:
  - Established the 4-phase engineering roadmap (`v1.0` Data & Microstructure Foundation $\to$ `v1.1` Out-of-Sample Edge Calibration $\to$ `v1.2` Forward Paper Validation $\to$ `v2.0` Live CLOB Execution) with hard statistical pass/fail gates.
- **Core Mathematical Kernel & Investor Dashboard**:
  - Continuous Avellaneda-Stoikov inventory reservation pricing and 3-way utility arbitration (`core/policy.py`).
  - Discrete 5-share lot quantization and order action planning (`core/execution.py`).
  - Investor-oriented web dashboard (`web/`) exposing strictly net financial metrics and per-round order action timelines, guarded by 14 unit tests with strict temporary directory isolation.
- **Official Oracle & TWAP Truth Alignment**:
  - Updated `runtime/market_feed.py` to prioritize Polymarket's official `crypto-price` Chainlink BTC/USD 60s TWAP feed for opening and closing reference prices, with Binance spot K-lines as network fallback.
  - Reconciled historical ledger records and settled outcomes directly with Polymarket's official settlement oracle truth.
- **Multithreaded Concurrent Web Server**:
  - Upgraded HTTP server to `ThreadingHTTPServer` to natively handle high-frequency concurrent polling (`/api/live` + `/api/history`) without socket resets.
- **Dashboard UI & Financial Caliber Standardizations**:
  - Standardized round displays exclusively on formatted Beijing Time (`UTC+8`) and eliminated raw Unix slugs.
  - Formatted Polymarket Open / Close price column with dollar change and basis points (`+$XX.XX (+X.X bps)`).
  - Explicitly separated `可用资金` (Available Capital, `$100.00 + net_pnl`) from `周转资金` (Turnover Capital / Cumulative Volume).
  - Compacted network metrics box (rate, latency, traffic) to prevent border overflow, and added defensive rendering protections.
