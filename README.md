# QuantKernel

QuantKernel is a read-only TuShare data processing kernel for A-share quantitative research. It builds reproducible, point-in-time-aware intermediate data from a local or server-side raw data directory, while keeping the original files untouched.

Current scope: **Stage 1 data processing layer**. The project does not implement factors, stock pools, trading signals, portfolio construction, or backtesting yet.

## Current Status

The supported Stage 1 pipeline is implemented and tested:

- L0 source profiling and validation.
- L1 trading calendar, adjusted prices, stock identity state (with namechange-derived historical names and ST status), trade feasibility (with per-stock daily limit-price evidence), financial availability, quarterly financials, and TTM financials.
- L2 strategy-independent research universe (including the three `ex_st` samples, active since the 2026-09 improvement round) and data quality report.
- Golden fixture tests for calendar, prices, stock state, namechange-derived ST, limit prices, trade status, financial PIT, quality checks, and reproducibility helpers.

The trading calendar now covers 20180102–20260911 after the `trade_cal` extension; quotes and suspensions still start at 20180601 and those ranges are gated by actual source coverage. Historical ST/*ST is derived from `namechange` intervals (a name containing `ST` marks the ST state) and per-stock daily limit prices come from `stk_limit`; both sources were added in the 2026-09 improvement round described in [STAGE1_NEXT_IMPROVEMENTS.md](STAGE1_NEXT_IMPROVEMENTS.md).

Known incomplete or deferred capabilities are documented in [docs/DATA_SOURCE_NOTES.md](docs/DATA_SOURCE_NOTES.md) and [STAGE1_NEXT_IMPROVEMENTS.md](STAGE1_NEXT_IMPROVEMENTS.md). The important ones are historical delisting-period intervals, exact pre-2018 listing trading age (natural-day and estimated alternatives are provided), announcement timestamps, full supplier revision archives, and historical security-code mapping.

The latest local test run:

```text
168 passed
```

Generated `data_middle/` outputs were removed before GitHub publishing because they are large and fully rebuildable.

## Repository Layout

```text
QuantKernel/
  configs/                    # Path, source-table, and processing rules
  docs/                       # Source notes and Stage 1 execution record
  scripts/                    # Build, validation, comparison, and ops entrypoints
  src/core/                   # Core processing modules
  tests/                      # Unit tests and tiny golden fixtures
  DATA_PROCESSING_STAGE1_PLAN.md
  STAGE1_NEXT_IMPROVEMENTS.md
  README.md
```

Local-only directories are intentionally not committed:

```text
data/                         # Raw TuShare data, read-only
data_middle/                  # Rebuildable derived outputs and validation evidence
.venv/                        # Local virtual environment
```

## Install

Python 3.10+ is required.

```text
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe -m pip install -e . --no-deps
```

For a fresh dependency solve instead of the lock file:

```text
python -m pip install -e ".[test]"
```

## Paths

By default, QuantKernel reads raw data from `./data` and writes derived outputs to `./data_middle`.

Path profiles are configured in [configs/paths.yaml](configs/paths.yaml):

- `local`: `DATA_ROOT=./data`, `MIDDLE_ROOT=./data_middle`
- `server`: `DATA_ROOT=/data/tushare_data`, `MIDDLE_ROOT=./data_middle`

Environment variables override the selected profile:

```text
DATA_ROOT=D:\path\to\tushare_data
MIDDLE_ROOT=D:\path\to\data_middle
```

The raw data directory must be treated as read-only. Build outputs must not overlap with the raw data directory.

## Common Commands

Check path configuration:

```text
python scripts/check_paths.py
python scripts/check_paths.py --profile server
```

Profile and verify raw data:

```text
python scripts/verify_raw_data.py --checksums
python scripts/build_l0.py
```

Build Stage 1:

```text
python scripts/build_stage1.py --record-run first
```

Rebuild L1/L2 using an unchanged L0 profile:

```text
python scripts/build_stage1.py --reuse-l0 --record-run repeat
python scripts/compare_stage1_runs.py first repeat
```

Run quality checks and tests:

```text
python scripts/check_data_quality.py --l0-scope full
python -m pytest -q
```

Small interval builds should use a separate output root:

```powershell
$env:MIDDLE_ROOT = './data_middle/small_interval'
python scripts/build_stage1.py --start 20240102 --end 20240110 --l0-scope core
Remove-Item Env:MIDDLE_ROOT
```

## Data Layers

L0 is a source profile and audit layer. It does not rewrite raw data. It records schema, row counts, date coverage, missing rates, candidate-key conflicts, invalid date examples, and source signatures under `data_middle/l0/`.

L1 contains reusable, strategy-independent processed data:

- `daily_calendar`
- `daily_adjusted_price`
- `daily_stock_state`
- `daily_trade_status`
- `financial_available`
- `quarterly_financial`
- `ttm_financial`

L2 contains research preparation outputs:

- `research_universe_config`
- `daily_research_universe`
- `data_quality_report`

Each published table is written as `part-00000.parquet` with a `manifest.json` containing schema, primary key, row count, file hash, configuration hash, code hash, and upstream references.

## GitHub Publishing Notes

Do not commit raw data, derived data, caches, local environments, or large Parquet/DuckDB artifacts. The committed repository should contain code, configs, docs, tests, and tiny hand-written fixtures only.

Before pushing, check:

```text
git status --short --ignored
git check-ignore -v data data_middle
git check-ignore -v src/core/data/store.py
```

The first two should be ignored. `src/core/data/store.py` must **not** be ignored.
