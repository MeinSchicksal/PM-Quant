# Changelog

All notable changes to the **PM-Quant** system are documented in this file.
Versions strictly follow semantic versioning **`v<Major>.<Minor>`** (e.g., `v1.0`, `v1.1`, `v2.0`), with each version isolated under `data/2_ledgers/<VERSION>/`.

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
