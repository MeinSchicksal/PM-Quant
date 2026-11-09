# Changelog

All notable changes to the PM-Quant quantitative trading system are documented in this file.

The format is based on Keep a Changelog, and revisions strictly map to `v<N>_r<M>`.

---

## [v1_r1] - 2026-11-09

### Added
- **First-Principles Engine**:
  - Continuous Avellaneda-Stoikov inventory reservation pricing (`core/policy.py`).
  - Real-time 3-way utility arbitration ($U_{\text{hedge}}, U_{\text{sell}}, U_{\text{hold}}$) eliminating legacy wait timers.
  - Continuous cumulative normal estimation via `math.erf` with dynamic uncertainty bandwidth (`core/estimator.py`).
- **Physical Invariants**:
  - Discrete 5-share lot quantization mapping strictly to discrete multiples (`core/execution.py`).
  - Network and matching queue delay simulation with 300ms latency (`sim/matcher.py`).
- **Data Architecture**:
  - 4-tier cold/hot separated filesystem: `1_market`, `2_ledgers`, `3_reports`, `4_runtime`.
  - Atomic writing for `live.json` and single-line append for `summary.jsonl`.
  - Gzip compression for granular window replay traces (`data/2_ledgers/<BATCH_ID>/windows/*.json.gz`).
- **Real-Time Data & Execution Pipeline**:
  - Resilient Polymarket 5-minute market discovery and order book streamer with automatic proxy fallback (`runtime/market_feed.py`).
  - Automated continuous live paper trading driver with 5-minute window rollover and settlement (`runtime/pipeline.py`).
  - Master unified command-line entry point (`main.py`).
- **Coinbase Institutional Web Dashboard**:
  - Zero-dependency Python HTTP server (`web/server.py`).
  - High-performance UI adhering to 4 font size tiers, zero drop shadow, 0.5px borders, and mono tabular figures.
- **Verification & Invariant Tests**:
  - Unit tests for lot quantization invariants, reservation price monotonicity, and adverse price jump taker hedging (`tests/test_invariants.py`).
  - Simulation suite passing across normal oscillation, adverse crash, and smooth trend regimes (`sim/replay.py`).
