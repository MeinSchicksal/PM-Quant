# Changelog

All notable changes to the **PM-Quant** system are documented in this file.
Versions strictly follow semantic versioning **`v<Major>.<Minor>`** (e.g., `v1.0`, `v1.1`, `v2.0`), with each version isolated under `data/2_ledgers/<VERSION>/`.

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

### Documentation
- `README.md`: corrected "live trading system" (there is no live order
  adapter yet), described matching honestly, fixed the duplicate "常用命令"
  section and the stale test count, documented the replay command.
- `docs/CORE_STRATEGY_PHILOSOPHY.md`: replaced the fabricated performance table
  (`avg_win +$0.830`, `avg_loss -$0.589`, ratio 1.41, 212 rounds +$14.754,
  "+$30.391 improvement", "87% of losses from overpriced hedges") with the
  ledger truth: 170 traded windows, **-$31.7998**, mean -$0.1871 (t=-1.63);
  loss structure 49.5% hedged / 39.5% naked / 11% other; and the new replay
  result over 77 fully recorded windows: 24 traded, **-$1.659**,
  mean -$0.0215 (sd $0.2529, t=-0.75), 54 fills, worst window -$2.0000
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
