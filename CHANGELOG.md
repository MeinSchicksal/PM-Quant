# Changelog

All notable changes to the **PM-Quant** system are documented in this file.
Versions strictly follow semantic versioning **`v<Major>.<Minor>`** (e.g., `v1.0`, `v1.1`, `v2.0`), with each version isolated under `data/2_ledgers/<VERSION>/`.

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
  - Investor-oriented web dashboard (`web/`) exposing strictly net financial metrics and per-round order action timelines, guarded by 11 unit tests with strict temporary directory isolation.
