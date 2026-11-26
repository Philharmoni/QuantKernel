# Stage 2 Golden Fixture (`tests/fixtures/golden_stage2/`)

8 wholly fictional stocks, 66 trading days (2020-03-02 to 2020-06-05, with a
fictional holiday block so the calendar isn't a bare weekday loop). Entirely
hand-designed; **never** derived from `data/`. Regenerate with:

```
python tests/fixtures/golden_stage2/generate.py
```

This sits one layer below `tests/fixtures/golden/` (Stage 1's fixture): the
CSVs here use the same raw-provider-table field names, but are run through
the **existing Stage 1 builders** (`build_calendar`, `build_adjusted_price`,
`build_stock_state`, `build_trade_status`, `build_financial_available`,
`build_quarterly_and_ttm`, `build_research_universe`) to produce the L1/L2
inputs that Stage 2 modules (industry, label, benchmark, factor, preprocess,
evaluation) will consume. It is a separate, larger fixture than Stage 1's
(8 stocks / 66 trading days vs. 5 stocks / 34 trading days) because Stage 2's
boundary scenarios need more calendar room: exit delay needs room after the
suspension to resume, delisting-in-window needs the delist date to fall
strictly inside a 20-day holding window, and `beyond_data_end` needs a date
near the tail where `T+1+20` runs past the fixture's last trading day.

All prices use `open == high == low == close` (one-word-board convention,
reused from the Stage 1 fixture) and a constant `adj_factor = 1` throughout,
so every forward-return calculation is `close_exit / close_entry - 1` with no
adjustment-factor arithmetic — adjustment-factor mechanics were already fully
exercised by the Stage 1 fixture and are not re-tested here.

Trading-day indices referenced below (0-based, computed by `generate.py`, not
hand-guessed) resolve to these calendar dates for the current `START`/`STOP`/
`HOLIDAYS`:

| index | date | index | date | index | date |
|---|---|---|---|---|---|
| 0 | 20200302 | 16 | 20200324 (**ENTRY**, T+1) | 36 | 20200422 |
| 15 | 20200323 (**T**) | 20 | 20200330 (SA006 delist_date) | 45 | 20200508 (**T_END**) |
| 17 | 20200325 | 21 | 20200331 (**EXIT5**, T+1+5) | 46 | 20200511 (T_END+1) |
| 19 | 20200327 (SA006 last quote) | 23 | 20200402 (**EXIT5_RESUME**) | 51 | 20200518 (T_END+1+5) |
| 26 | 20200408 (T+1+10) | 30 | 20200414 (SA007 industry in_date) | 65 | 20200605 (last valid index) |
|  |  | 35 | 20200421 (**THIN_DAY**) | 66 | *(out of range — T_END+1+20)* |

If `START`/`STOP`/`HOLIDAYS` in `generate.py` ever change, re-run the script,
re-read its printed summary, and update both this table and `expected.json`
together — never hand-edit resolved dates independently of the generator.

## Stocks and scenarios

All 8 stocks are listed (`list_date=20190101`) well before the fixture
calendar starts, so "not yet listed" is not one of the covered scenarios
(there is no mid-fixture IPO in this fixture).

| Code | Scenario | Notes |
|---|---|---|
| `SA001.SZ` | Baseline / rank-tie partner A | Close constant at 100 throughout. |
| `SA002.SZ` | Rank-tie partner B | Close constant at 100 throughout — identical to `SA001.SZ` at every date, exercising tied ranks in RankIC / quantile edges. |
| `SA003.SZ` | `entry_blocked` + thin cross-section | Full-day suspension at index 16 (ENTRY) and again at index 35 (THIN_DAY). Close constant at 90 on all other days. |
| `SA004.SZ` | `entry_open_at_up_limit` | Close 95 for index < 16; one-word up-limit at 104.5 from index 16 onward (sustained, not reverted). `limit_list_d.csv`/`stk_limit.csv` carry the matching `up_limit=104.5` row at index 16. |
| `SA005.SZ` | `exit_delayed_days` | Close constant at 105, **except** full-day suspension at indices 21 and 22 (the planned EXIT5 day and the day after); resumes at index 23 with close 115 (sustained afterward). Delay = 2 trading days. |
| `SA006.SZ` | `delisted_in_window` | Close constant at 110 through index 19 (last quote); `list_status='D'`, `delist_date` = index 20's date; no quotations from index 20 onward. |
| `SA007.SZ` | Industry `in_date` **after** fixture start | Close constant at 120 throughout (no price-scenario role). SW industry membership `in_date` = index 30's date (mid-window). |
| `SA008.SZ` | Industry `in_date` **before** fixture start | Close constant at 400 throughout (deliberately large, to also serve as the winsorize outlier in the Step 1 formula example below). `in_date = 20190101`, i.e. before the fixture's first trading date. |

Per-stock `total_share` (万股, constant, in `stk_factor_pro.csv`): `SA001`
10000, `SA002` 8000, `SA003` 6000, `SA004` 7000, `SA005` 9000, `SA006` 5000,
`SA007` 4000, `SA008` 3000. Every row also carries `total_mv` and `circ_mv`
set to exactly `close * total_share`, i.e. `total_mv`/`circ_mv` are in 万元
(10k CNY) and `total_share` is in 万股 (10k shares) — the same unit
convention confirmed in the real `stk_factor_pro` table. This is the
market-cap unit-conversion check: any Stage 2 code that reads these columns
must reproduce `total_mv == close * total_share` exactly on every row.

## Financial announcements (`SA001.SZ` only)

Four consecutive FY2019 quarters (Q4 is the full fiscal year), cumulative
fields at a fixed 10% / 25% / 55% / 100% shape (of each field's full-year
total), so every quarter's value is the full-year total times a simple
fraction. Every `end_date` stays strictly before its own `ann_date` (an
announcement before its own report period ends is invalid and never becomes
available, per `core.financial.available`):

| end_date | ann_date (index) | available_date (index) | ratio |
|---|---|---|---|
| 20190331 (Q1) | index 1 (20200303) | index 2 (20200304) | 10% |
| 20190630 (Q2) | index 3 (20200305) | index 4 (20200306) | 25% |
| 20190930 (Q3) | index 6 (20200310) | index 7 (20200311) | 55% |
| 20191231 (Q4) | index 14 (20200320) | **index 15 = T (20200323)** | 100% |

Full-year totals: `revenue` 2,000,000,000; `n_income_attr_p` 500,000,000;
`n_cashflow_act` 480,000,000; `oper_cost` 1,400,000,000; `operate_profit`
600,000,000; `total_profit` 620,000,000 (all CNY yuan).

A same-day **revision** of Q4 `n_income_attr_p` (same `end_date=20191231`,
`update_flag='1'`, value 520,000,000) is announced at `ann_date` = index 15
(T itself), so its `available_date` = index 16 (T+1). This exercises the
announcement-visibility boundary exactly at T/T+1: at T, the TTM net income
available is still the original 500,000,000; from T+1 onward it is
520,000,000.

Point-in-time balance sheet fields (available at the same index 15 = T, same
`ann_date` as the original Q4 announcement, `end_date=20191231`):
`total_assets` 9,000,000,000; `total_hldr_eqy_exc_min_int` 5,000,000,000;
`total_liab` 4,000,000,000; `total_cur_assets` 3,000,000,000; `total_cur_liab`
1,500,000,000 (all CNY yuan; `total_assets == total_liab + total_hldr_eqy_exc_min_int`
by construction). `fina_indicator.csv` carries one `eps=2.50` row at the same
announcement, present for structural completeness only (not used by any
Step 1 check).

These seven new balance-sheet/income fields (`total_hldr_eqy_exc_min_int`,
`total_liab`, `total_cur_assets`, `total_cur_liab`, `oper_cost`,
`total_profit`, `operate_profit`) were added to Stage 1's
`cumulative_fields`/`point_in_time_fields` whitelist in
`configs/data_middle_layer.yaml` as part of Step 2 (financial whitelist
expansion), after the Stage 1 regression (tests, quality gate, two-build
comparison) was rerun and passed. `oper_cost`/`operate_profit`/`total_profit`
are `income_vip` cumulative fields and now get quarterly/TTM derivation
(verified against the same FY2019 four-quarter shape as `n_income_attr_p`);
the four balance-sheet fields are `point_in_time_fields` (documentation only
— `point_in_time_fields` is not consumed by `core.financial.quarters`, so
`balancesheet_vip` still has no quarterly/TTM rows, only raw
`financial_available` passthrough).

## Industry raw tables

`index_classify.csv` defines two wholly fictional sectors (`900001.SI`
FixtureSectorA, `900002.SI` FixtureSectorB — not real SW/CITIC codes).
`index_member_all.csv` assigns `SA001`–`SA006` to FixtureSectorA with
`in_date=20190101` (ordinary, not a focus scenario), and the two focus cases
described above (`SA007`/`SA008`). `l1_code`/`l2_code`/`l3_code` are all set
to the same value per row (classification-level granularity is not the
object of this fixture).

`index_weight.csv` carries four `000300.SH` constituents (`SA001`/`SA002`/
`SA003`/`SA004`, weight date `20200201`, weights 5.0/3.0/1.0/4.0). Step 2 uses
it for the as-of expansion mechanics; Step 4 uses it for the self-built
benchmark scenario below.

## Step 4 benchmark scenario

`daily_benchmark_return` for the `000300.SH` member subsample: members at T,
`ret_1d` at T+1. **index16 (20200324) is the only non-zero self-built return
in the fixture**, because `SA004` jumps 95 → 104.5 there with quotes on both
index15 and index16 (`ret_1d = 104.5/95-1 = 0.1` exactly):

- `member_count = 4`, `return_count = 3`: `SA003` is suspended at index16, so
  its `ret_1d` is NULL — excluded from the equal-weight mean and from the
  cap-weight denominator, never counted as 0.
- Equal weight: `(0+0+0.1)/3 = 1/30`.
- `circ_mv` weight (weights at member day index15: SA001 1,000,000 / SA002
  800,000 / SA004 665,000 万元): `0.1*665000/2465000 = 133/4930`.
- At index17 `SA003`'s `ret_1d` is still NULL (prev quote index15 ≠ prev
  trading day index16), so `return_count = 3` again and both returns are 0.

`index_daily.csv` (Step 4 addition) carries the official `000300.SH` daily for
exactly index15..18 with closes 1000/1100/1100/1100 and pre_closes
1000/1000/1100/1100, i.e. official returns `[0, 0.1, 0, 0]`. The self-built
equal-weight returns on those dates are `[0, 1/30, 0, 0]` = official/3
exactly, so the calibration Pearson is **exactly 1.0**, `mean_diff = -1/60`
and `tracking_error_daily = sqrt(1/1200)` (ddof=0). Exact values and formulas
live in `expected.json` under `benchmark`.

## What the Stage 1 fixture-contract test checks

`tests/test_golden_stage2_fixture.py` runs the existing Stage 1 builders over
this fixture and asserts the directly-observable facts above (suspension/
delisting/limit-up rows land where expected, `available_date` resolves per
the next-trading-day rule, the market-cap identity holds). This file remains
an upstream fixture-consistency contract against `expected.json`; later
Stage 2 modules have their own dedicated tests against this fixture.

## Step 1 formula-verification example (not derived from the entity panel)

Independent, fully hand-computed numeric examples live in
`expected.json` under `evaluation_formula_fixture`, pinned to the exact
formulas now frozen in `configs/factor_research.yaml`:

1. **IC / RankIC with ties**: the toy (non-registered) "mock factor" =
   each stock's close price at T, paired with its `ret_o2o_5` label at T
   (`SA003` excluded: `entry_blocked` → null label). Demonstrates
   average-rank tie handling (`SA001`/`SA002` tied at rank 2.5 of 7) and
   winsorize/standardize on the same 8-value cross-section (`SA008`'s 400 is
   the outlier, capped by `median +/- 5*MAD`). Neutralization against
   `log_circ_mv` is checked by **orthogonality property**, not by exact
   residual values (`sum(residual) ~= 0`, `sum(residual * log_circ_mv) ~= 0`,
   within `repeatability.float_tolerance`). The original formula fixture
   deliberately omits the industry-dummy term and checks only size
   residualization; full industry-neutral behavior is covered by the later
   Step 6 preprocessing tests.
   2. **Group performance (Sharpe / max drawdown / Calmar / win rate /
    profit-loss ratio / long-short)**: a standalone 4-period return series,
    unrelated to the 8-stock panel (that panel's returns are too degenerate —
    mostly flat — to illustrate drawdown/Sharpe mechanics). Explicitly labeled
    as a formula check; Step 7 tests additionally validate these formulas
    against this fixture's own thin multi-period panel.
3. **Newey-West t 值**：序列 `[1,2,3,4]`、`h=2`，Bartlett 滞后 1。
   手算 `gamma0=1.25`、`gamma1=0.3125`、长期方差
   `LRV=1.25+2*(1/2)*0.3125=1.5625`，因此
   `t=2.5/sqrt(1.5625/4)=4`。Step 7 另以双起点偏移、换手和盈亏平衡成本
   闭式样例验证非重叠分层汇总。

## Step 5 factor scenarios

`stk_factor_pro.csv` gains `turnover_rate=2.0` (constant, so `turn_20` is
exactly 2.0), `pe_ttm=25.0` and `pb=2.0` (vendor reciprocals 0.04 and 0.5 for
the vendor-comparison report).

SA001 gains four FY2018 quarters (revenue 1e9, net income 400e6, operating
cash flow 300e6, same 10/25/55/100% cumulative shape). **All four FY2018
announcements sit on the fixture's first trading day (20200302)**: with a
realistic 2018/2019 announcement rhythm they would fall before the calendar
coverage start and Stage 1 would mark them `calendar_before_coverage`
(never available), silently deleting the growth factors' base year. This makes
single-quarter YoY exactly hand-computable:

- `rev_yoy_q` = Q4-2019 revenue 900M / Q4-2018 450M - 1 = **1.0**;
- `np_yoy_q` = Q4-2019 net income 225M / 180M - 1 = **0.25**, becoming
  245M/180M-1 = **0.36111...** once the T+1 revision is visible (with
  `ep_ttm` 0.05→0.052 and `roe_ttm` 0.1→0.104).

Non-zero price-factor checkpoints:

- SA004 at index35 (20200421): `rev_20 = 104.5/95-1 = 0.1`; `vol_20` over
  index16..35 contains exactly one 0.1 and nineteen 0, i.e. ddof=0 std
  `sqrt(0.01/20 - 0.005^2) = sqrt(0.000475)`; `amihud_20` is contributed only
  by index16: `0.1/(10450*1000*20)`.
- SA001 at index20: `vol_20 = 0`, `turn_20 = 2.0`, `amihud_20 = 0`,
  `log_amount_20 = ln(1e7)`; `log_total_mv = log_circ_mv = ln(1e10)`
  (万元→元 conversion).
- SA003 at index35: the 20-day window contains both suspended days (index16
  and index35), leaving 18 quoted days (and a NULL `ret_1d` on resumption), so
  `vol_20`/`turn_20` must be **absent** — suspensions are never silently
  compressed out of a trading-day window.
- `mom_12_1`/`mom_6_1` need 252/126 trading days of lookback and therefore
  have no value on any of the 66 fixture days.

Exact numbers and formulas live in `expected.json` under `factors`.

## Step 8 composition formula scenarios

`expected.json` 的 `composition_formula_fixture` 固定三类手算预期：

1. 成分 A 的 RankIC `[1,3]` 得均值 2、ddof=0 标准差 1、ICIR 2；成分 B 的
   `[-1,1]` 得 ICIR 0，因此按 `ICIR/sum(abs(ICIR))` 归一化后权重为 1/0。
   第二个 IC 的实际出场日若恰为 T，则 T 权重不可使用该观测；样本内末日
   之后实现的标签即使对更晚 T 可见，也不进入滚动训练。
2. 等权合成中一个成分缺失时，在剩余可用权重上重新归一化；夹具仅剩
   `vol_20=8` 且预期方向为 -1，合成值手算为 -8。
3. 完整链接聚类使用 `1-abs(平均日截面相关)` 作为距离：AB=0.9、AC=0.1、
   BC=0.8 时只合并 A/B；AC 改为 -0.85 后三者满足完整链接阈值并合为一类。

另有独立 12 日小面板验证合成值、逐日权重、全日期截断不变性、双跑哈希及
重新经过 Step 6 预处理和 Step 7 评价的端到端路径。
