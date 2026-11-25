# QuantKernel

QuantKernel is a read-only TuShare data processing kernel for A-share quantitative research. It builds reproducible, point-in-time-aware intermediate data from a local or server-side raw data directory, while keeping the original files untouched.

Current scope: **Stage 1 data processing, Stage 2 factor research, and Stage 3 strategy research**, with stage-specific outputs and validation evidence.

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
342 passed
```

Generated `data_middle/` outputs were removed before GitHub publishing because they are large and fully rebuildable.

## Repository Layout

```text
QuantKernel/
  configs/                    # Path, source-table, factor, and strategy rules
  docs/                       # Source notes and Stage 1-3 execution records
  scripts/                    # Build, validation, comparison, and ops entrypoints
  src/core/                   # Core processing and research modules
  tests/                      # Unit tests and tiny golden fixtures
  DATA_PROCESSING_STAGE1_PLAN.md
  FACTOR_RESEARCH_STAGE2_PLAN.md
  STRATEGY_RESEARCH_STAGE3_PLAN.md
  STAGE1_NEXT_IMPROVEMENTS.md
  README.md
```

Local-only directories are intentionally not committed:

```text
data/                         # Raw TuShare data, read-only
data_middle/                  # Stage 1 L0/L1/L2 processed data only
factor_research/              # Stage 2 reference/factor outputs and their evidence
strategy_research/            # Stage 3 snapshots, runs, reports, and their evidence
verification/                 # Stage 1 and framework-level validation evidence
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

By default, QuantKernel reads raw data from `./data` and separates outputs by stage: Stage 1 uses `./data_middle`, Stage 2 uses `./factor_research`, Stage 3 uses `./strategy_research`, and Stage 1/framework validation uses `./verification`.

Path profiles are configured in [configs/paths.yaml](configs/paths.yaml). Both `local` and `server` use the same four project-relative output roots; only `DATA_ROOT` differs (`./data` locally and `/data/tushare_data` on the server).

Environment variables override the selected profile:

```text
DATA_ROOT=D:\path\to\tushare_data
MIDDLE_ROOT=D:\path\to\data_middle
FACTOR_RESEARCH_ROOT=D:\path\to\factor_research
STRATEGY_RESEARCH_ROOT=D:\path\to\strategy_research
VERIFICATION_ROOT=D:\path\to\verification
```

The raw data directory is read-only. All five roots must be mutually disjoint so one stage cannot overwrite another stage's inputs or outputs.

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

## Factor Research Workflow

Factor exploration has three actions. Each factor implementation lives in `research/factors/<factor_id>/compute.py`; new experiments may define a complete `SPEC` in that file without first entering the accepted registry.

```bash
# 1. Compute values and automatically freeze experiment metadata/code/config snapshots
python scripts/run_factor_experiment.py my_factor --start 20200101 --end 20241231

# 2. Evaluate the generated Parquet; all outputs stay inside this experiment
python scripts/evaluate_factor.py \
  factor_research/experiments/my_factor/<experiment_id>/factor_values.parquet

# 3. After technical/policy checks and explicit research approval, append to the library
python scripts/promote_factor.py \
  factor_research/experiments/my_factor/<experiment_id> --approve
```

`experiment.yaml`, `manifest.json`, code snapshots, evaluation tables, `summary.json`, and `report.md` are generated automatically. Factor values contain `trade_date`, `ts_code`, `stock_name`, `factor_id`, and `value`. Evaluation is in-sample by default and reuses the common preprocessing, forward-label, IC/RankIC/RankICIR, quantile, and peer-correlation engines.

Admission policy is in `configs/factor_admission.yaml`. The initial policy requires at least 60 primary-horizon RankIC observations and explicit `--approve`; optional IC/ICIR/correlation thresholds can be configured. The first promotion implementation accepts new factor IDs only and never overwrites an existing factor version.

`configs/factor_registry.yaml` is the accepted factor library, using each `factor_id` as a top-level key with no enclosing `factors` list; entries are separated by blank lines. The loader still accepts legacy experiment snapshots. `scripts/build_stage2.py` remains the batch rebuild/regression entrypoint for accepted factors and dispatches to the same per-factor source files; experiment L3/L4 files are never copied into formal outputs.

### Custom Research Universes

A member CSV/Parquet needs only `trade_date` and `ts_code`; each row is a member on that day. Build an immutable Stage 1 custom pool, then evaluate the same computed factor values independently within it:

```bash
python scripts/build_custom_universe.py members.csv --universe-id my_pool_v1
python scripts/evaluate_factor.py \
  factor_research/experiments/my_factor/<experiment_id>/factor_values.parquet \
  --universe my_pool_v1
```

The Stage 1 outputs are `l2/custom_research_universe` and `l2/custom_research_universe_config`, with historical `stock_name` added from `daily_stock_state`. Custom evaluation is written to `evaluations/<universe_id>/`, leaving default evaluation, formal L3/L4, and promotion evidence unchanged. Reusing an ID with different input is rejected; use a new universe ID.

Custom screening code may skip the member file entirely: create a temporary view containing `trade_date,ts_code` in the same `Store` connection and call `build_custom_research_universe(store, relation="my_members", universe_id="my_pool_v1")`. File and relation inputs share the same validation and final publication path.

## Verification Levels

All stages retain schema, primary-key, PIT/OOS permission, and business-semantic checks. File hashing and independent recomputation are controlled separately:

| Mode | Parquet content hashes | Independent repeat | Intended use |
|---|---|---|---|
| `fast` | Skipped after publication; manifests still record publisher hashes | Skipped | Local development and iteration |
| `standard` (default) | Stage 1 admission once per Stage 2 run; immutable Stage 3 bundles once per process | Reads existing evidence only; never creates a missing repeat | Normal research runs |
| `audit` | Full independent file verification and deep primary-key scans | Executes or independently verifies all required repeats | Release, dependency upgrade, and frozen acceptance |

Stage 1/2 CLIs accept `--verification`:

```text
python scripts/build_stage1.py --verification standard
python scripts/build_stage2.py --verification standard
python scripts/build_stage2.py --verification audit
python scripts/check_stage2_quality.py --verification fast
```

Stage 3 check entrypoints read `QUANTKERNEL_VERIFICATION` (`fast`, `standard`, or `audit`). Standard mode preserves every accounting, PIT, authorization, truncation, and robustness semantic check; `repeat_status=not_requested` means no independent repeat was run, not that one passed. Step 14 reports incomplete independent repeats as G10 `not_run`, with `audit_complete=false`.

Stage 3 snapshot content verification uses a bounded, thread-safe process cache keyed by bundle root, manifest hash, and admission hash. Audit mode bypasses successful cached results and refreshes file verification.

Small interval builds should use an isolated set of output roots:

```powershell
$env:MIDDLE_ROOT = './verification/framework/small_interval/data_middle'
$env:FACTOR_RESEARCH_ROOT = './verification/framework/small_interval/factor_research'
$env:STRATEGY_RESEARCH_ROOT = './verification/framework/small_interval/strategy_research'
$env:VERIFICATION_ROOT = './verification/framework/small_interval/verification'
python scripts/build_stage1.py --start 20240102 --end 20240110 --l0-scope core
Remove-Item Env:MIDDLE_ROOT, Env:FACTOR_RESEARCH_ROOT, Env:STRATEGY_RESEARCH_ROOT, Env:VERIFICATION_ROOT
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

Stage 1 acceptance evidence is stored separately under `verification/stage1/`, not inside the data middle layer.

Stage 2 keeps its three additional L1 reference tables, L3 factor inputs/values, L4 evaluations/reports, repeatability workspaces, and validation evidence under `factor_research/`. Stage 3 keeps immutable input snapshots, preregistration, runs, reports, and verification under `strategy_research/`.

Each published table is written as `part-00000.parquet` with a `manifest.json` containing schema, primary key, row count, file hash, configuration hash, code hash, and upstream references.

## GitHub Publishing Notes

Do not commit raw data, derived data, caches, local environments, or large Parquet/DuckDB artifacts. The committed repository should contain code, configs, docs, tests, and tiny hand-written fixtures only.

Before pushing, check:

```text
git status --short --ignored
git check-ignore -v data data_middle factor_research strategy_research verification
git check-ignore -v src/core/data/store.py
```

The first five should be ignored. `src/core/data/store.py` must **not** be ignored.
