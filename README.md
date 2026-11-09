# PM-Quant: Polymarket 5-Minute Quantitative Market Making System

An institutional, first-principles market making and hedging engine for Polymarket 5-minute binary outcome markets.

---

## 1. Architectural Philosophy

- **Zero Legacy Baggage**: Independent codebase located cleanly on `D:\pm-quant` with no symlinks or dependencies on old experimental codebases.
- **Continuous Economic Modeling**: Continuous inventory-adjusted reservation pricing (Avellaneda-Stoikov) and real-time 3-way utility arbitration ($U_{\text{hedge}}, U_{\text{sell}}, U_{\text{hold}}$).
- **Physical Invariants**:
  - Discrete 5-share lot quantization (orders and positions strictly multiples of 5 shares).
  - Explicit transit delay modeling (300ms wire + engine latency).
  - Cold/Hot storage separation (read-only historical tick data vs atomic runtime ledgers).
  - Single source of truth in `config.py` with strictly 10 core parameters.

---

## 2. Directory Structure

```text
D:\pm-quant/
├── config.py                 # Single source of truth (10 core parameters & paths)
├── requirements.txt          # Python dependencies
├── core/                     # Mathematical & economic kernel (pure functions)
│   ├── types.py              # Strongly-typed dataclasses (Position, OrderBook, etc.)
│   ├── estimator.py          # Continuous probability & uncertainty estimation
│   ├── policy.py             # Inventory reservation pricing & 3-way utility arbitration
│   └── execution.py          # Discrete lot quantization & order action planner
├── sim/                      # Offline backtesting & simulation
│   ├── matcher.py            # Latency-aware FIFO queue matcher
│   └── replay.py             # Scenario backtest harness (oscillate, crash, trend)
├── runtime/                  # Real-time execution & data pipeline
│   ├── runner.py             # Live session loop, live.json, and summary.jsonl writer
│   ├── collector.py          # Binance BTC spot & Polymarket order book recorder
│   └── supervisor.py         # Process tracking and revision sealing
├── web/                      # Zero-dependency monitoring dashboard
│   ├── server.py             # Lightweight HTTP server & JSON APIs
│   └── static/               # HTML5, CSS (Coinbase institutional design), JS
├── tests/                    # Invariant & mathematical property unit tests
│   └── test_invariants.py    # Quantization, reservation monotonicity, hedge triggers
└── data/                     # Structured cold & hot data
    ├── 1_market/             # Immutable raw market feeds (books, spot, settlements)
    ├── 2_ledgers/            # Per-batch execution journals (e.g., v1_r1/)
    ├── 3_reports/            # Aggregated research & batch comparisons
    └── 4_runtime/            # Live PID files, logs, and process descriptors
```

---

## 3. The 10 Core Parameters (`config.py`)

| Parameter | Default | Category | Economic & Physical Meaning |
|---|---|---|---|
| `LOT_SIZE` | `5.0` | Discrete Physics | Exchange minimum order lot size. |
| `MAX_LOTS` | `2` | Position Sizing | Max inventory allowed (2 lots = 10 shares, ~$4 to $7 exposure). |
| `LATENCY_S` | `0.30` | Network Physics | Simulated round-trip network & matching engine delay. |
| `BASE_EDGE` | `0.025` | Pricing Edge | Demanded profit margin (2.5 ticks) over theoretical probability. |
| `UNCERTAINTY_K`| `1.2` | Probability Band | Multiplier scaling uncertainty bandwidth $\epsilon_t$. |
| `RISK_AVERSION_GAMMA`| `0.15` | Inventory Utility | Avellaneda-Stoikov inventory penalty coefficient. |
| `MAX_QUEUE_WAIT_S` | `15.0` | Queue Dynamics | Threshold for expected queue wait before widening spread. |
| `TOXICITY_SHOCK_BPS` | `3.5` | Adverse Selection| Spot rapid shift threshold (3.5 bps / 3s) indicating informed order flow. |
| `TAIL_FREEZE_S` | `60.0` | Lifecycle Boundary | Seconds before settlement where initial maker entry is frozen. |
| `MAX_WINDOW_LOSS_USD`| `2.00` | Catastrophic Risk| Hard loss envelope per window triggering emergency exit. |

---

## 4. Operational Commands (Master CLI)

The system provides a unified CLI via `main.py`:

```bash
# 1. Start continuous live paper trading loop
python main.py run

# 2. Start Coinbase-style web monitoring dashboard
python main.py web 8900

# 3. Run mathematical invariant unit tests
python main.py test

# 4. Run offline scenario simulation benchmark
python main.py replay

# 5. Seal current batch with code snapshot & manifest
python main.py seal

# 6. Show current batch identity and parameter list
python main.py status
```

---

## 5. Versioning & Governance Convention

- Revisions follow `v<N>_r<M>` format (e.g. `v1_r1`, `v1_r2`).
- Parameter or rule changes require bumping `REVISION` in `config.py`.
- Each revision creates an isolated folder in `data/2_ledgers/<BATCH_ID>/`.
- Historical ledgers are never overwritten or mixed with new revisions.
