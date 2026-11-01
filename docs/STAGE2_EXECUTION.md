# 第二阶段（因子研究层）执行记录

方案：`FACTOR_RESEARCH_STAGE2_PLAN.md`。各步骤严格按顺序验收。
一阶段规划准入记录见 `docs/STAGE1_EXECUTION.md`（含"准入轮"与二阶段 Step 2
中属于一阶段受控变更的财务白名单扩展部分）。

## Step 0：二阶段工程约定与一阶段契约（2026-10）

- `configs/factor_research.yaml` 落定方案第 4 节约定。
- `src/core/factor/entry.py` + `scripts/check_stage2_entry.py`：校验一阶段九张产物
  manifest 存在、主键/行数与质量报告一致、`raw_unchanged/repeatability` 为 passed，
  输出本次绑定的上游指纹集合；`check_no_raw_access()` 静态扫描 Stage 2 源码目录，
  禁止直接读取原始表。
- 全量测试通过；`check_stage2_entry.py` 在现有真实数据产物上通过。

## Step 1：二阶段黄金测试数据（2026-10）

- `tests/fixtures/golden_stage2/`：8 只人工股票（SA001–SA008），66 个交易日
  （20200302–20200605）的 L1/L2 形态输入，不读真实数据。覆盖停牌入场、出场顺延、
  持有期退市、开盘一字涨停、财务公告前后、行业 `in_date` 前后、市值单位换算等边界。
- 新建 `tests/test_golden_stage2_fixture.py`（10 项），验证已规划场景中与当前一阶段
  构建器直接相关的部分。
- 过程中修复夹具问题：`generate.py` 原先把 SA001 的 Q4 `end_date` 设为 `20200930`
  （在 `ann_date=20200320` 之后），会被 `build_financial_available` 判定为
  `announcement_before_report` 导致 `available_date` 恒为空；已改为 FY2019 的四个季度，
  同步更新 `generate.py`、`README.md`、`expected.json` 并重新生成 CSV。
- 全量测试通过。

## Step 2：行业 PIT 口径与指数成分每日化（2026-10）

按 `FACTOR_RESEARCH_STAGE2_PLAN.md` Step 2 剩余两项（均不依赖联网下载）：行业
`strict`/`backfilled` 双口径、`index_weight` 月度权重转每日持仓。

### 新增模块与架构约束

- `src/core/reference/membership.py`（新目录，不在 `core.factor.entry` 的 Stage 2
  禁止直连原始表清单内）：唯一读取 `index_member_all`/`index_weight` 原始表的位置，
  校验后发布 L1 `index_member_available`（主键 `[ts_code, l1_code, in_date]`）与
  `index_weight_available`（主键 `[index_code, con_code, trade_date]`），镶镌一阶段
  `financial/available.py` 的"原始校验透传"范式。
- `src/core/industry/pit.py`：发布 L3 `daily_industry`，每行一个
  `industry_pit_status`（`pit_history`/`snapshot_after_in_date`/
  `snapshot_backfilled`/`unknown`，定义见 `configs/factor_research.yaml`
  `industry` 段）。真实数据 `index_member_all` 目前仅有 `is_new='Y'`
  单快照（`out_date` 全空，经核实每个 `ts_code` 恰一行），故 `pit_history`/
  `unknown` 当前永不出现，只产出 `snapshot_after_in_date`（`trade_date>=in_date`）
  与 `snapshot_backfilled`（更早日期，回填当前分类并显式标注前视风险）。
- `src/core/benchmark/membership.py`：发布 L3 `daily_index_member`，将
  `index_weight` 的月度/定期快照按发布滞后（`factor_research.yaml`
  `timing.index_weight_publication_lag_trading_days=5`，待定）展开为每日持仓：
  每次调仓只在其"可得日"（锚定交易日 + 滞后交易日数）后才可见，并持续到下一次
  调仓的可得日为止；滞后后超出日历覆盖范围的调仓记录会被显式丢弃并记入
  `source_diagnostics.json`，不做静默外推。
- `core.industry`/`core.benchmark` 均不直接读取原始表，只消费
  `core.reference.membership` 发布的 L1 产物，由 `check_no_raw_access` 守卫。

### 配置变更

- `configs/tushare_tables.yaml`：确认并写死 `index_member_all` 主键
  `[ts_code, l1_code, in_date]`、`index_weight` 主键
  `[index_code, con_code, trade_date]`（此前标注"未确认"），补充
  `required_fields`。
- `configs/data_middle_layer.yaml`：新增 `reference_membership` 段（两张原始表主键
  配置）；`capabilities` 新增 `industry_pit_history: limited`，记录"仅有当前快照、
  无历史区间"的已知边界。
- `configs/factor_research.yaml`：新增 `industry` 段
  （`pit_statuses`/`strict_usable_statuses`/`backfilled_usable_statuses`）。

### 回归结果

- 新增 `tests/test_industry_and_index_member.py`（7 项，基于 `golden_stage2`
  夹具）：SA007 跨越 `in_date` 前后的双状态验证、SA008 全窗口内均为观测成员、
  `daily_industry` 每日每股恰一行、`daily_index_member` 按发布滞后的 as-of 展开
  （000300.SH 对 SA001/SA002 的权重与可得日）、无调仓被丢弃。全量单元测试
  **192 项通过**。
- 真实全量数据运行（复用一阶段 `data_middle`）：`index_member_available`
  5,899 行、`index_weight_available` 228,570 行；`daily_industry`
  12,452,789 行，状态分布仅 `snapshot_after_in_date`/`snapshot_backfilled`
  （`pit_history`/`unknown` 均为 0，符合"仅当前快照"的预期）；
  `daily_index_member` 4,102,514 行，`rebalances_dropped_beyond_calendar_coverage`
  为空（6 个指数的调仓记录滞后后均落在日历覆盖范围内）。
- 独立双目录重跑比较四个产物的 `sha256`：**完全一致**，确认可复现。
- `scripts/check_stage2_entry.py`：Step 0 准入门禁继续通过（新模块不影响
  `core.factor.entry` 的 9 张上游表绑定与禁止直连原始表检查）。
- 行业 PIT 的 `pit_history`/`unknown` 两个状态、`daily_index_member` 的发布滞后
  天数（5，待定）均为已知边界，带着挂起状态进入后续步骤，不阻塞 Step 2 验收。

## Step 3：前向收益标签 `forward_return`（2026-10）

### 实现

- `src/core/label/forward.py`：发布 L3 `forward_return`，主键
  `[trade_date, ts_code, label_id]`，长表同时容纳两类标签（`label_id` 区分）：
  - `ret_o2o_h`（主标签）：`T+1` 开盘入场、`T+1+h` 开盘出场；
  - `ret_c2c_h`（对照标签）：`T` 收盘到 `T+h` 收盘，仅作理论上界，无
    `entry_open_at_up_limit` 概念（入场是收盘价）。
  - `h ∈ {1,5,10,20}`，价格一律用一阶段 `adjusted_open/adjusted_close`
    （原价 × 当日 `adj_factor`），不用厂商 qfq/hfq。
- 异常路径（方案 4.2）全部实现，单一 `exception_flag` 按优先级汇总
  （`beyond_data_end` > `entry_blocked` > `delisted_in_window` >
  `exit_delayed_days` > `entry_open_at_up_limit`），同时保留独立布尔列
  （`entry_blocked`/`entry_open_at_up_limit`/`delisted_in_window`/
  `beyond_data_end`）供下游"可交易口径"过滤，避免出场异常掩盖入场条件：
  - `beyond_data_end`：计划出场交易日超出日历覆盖（含 `T+1` 本身不存在），
    标签为空，绝不用部分区间收益替代；仅 `T` 日仍上市的股票生成行。
  - `entry_blocked`：入场日无行情或未上市，标签为空。
  - `delisted_in_window`：计划出场日已退市，用退市前最后行情的
    `adjusted_close` 出场，样本保留不删除。
  - `exit_delayed_days`：出场日无行情但未退市，顺延至首个有行情日开盘
    （c2c 为收盘），上限 `labels.max_exit_delay_trading_days=60`；超限用最后
    行情价出场，`exit_delayed_days` 记录顺延交易日数（超限时记为上限值）。
  - `entry_open_at_up_limit`：入场日未复权 `open >= up_limit`（精确 DOUBLE
    比较，与 `core.market.trading` 的 `price_limit_up` 口径一致），标签照算。
- 实现方式：`label_panel` 临时表（全网格 × 行情 × 涨停价，含每股"下一/上一
  有行情交易日"窗口指针）一次物化，8 个标签视图复用；避免按行 Python 循环。
- 隔离约束：`core.factor.entry.check_factor_label_isolation()` 新增静态检查，
  禁止 `core/factor/**` 导入 `core.label`；`check_no_raw_access` 对 `label/`
  目录的禁读原始表守卫自动生效。

### 验证结果

- 新增 `tests/test_forward_return.py`（7 项）：`expected.json` 中
  `forward_return_labels` 的 8 股票 × 4 前瞻期全部逐值核对（收益、异常标记、
  入场/出场价格与日期、顺延天数、退市最后行情）、`beyond_data_end` 无部分区间
  收益、`entry_blocked` 标签为空、退市/顺延样本未被删除、开盘涨停标签照算、
  c2c 对照口径、主键唯一性与"T 日上市"行范围、两条隔离守卫。全量单元测试
  **199 项通过**。
- 真实全量数据构建：`forward_return` **78,083,360 行**（约 40 秒）；
  异常计数符合规则：`beyond_data_end`/`entry_blocked` 的 `with_return` 均为 0，
  `delisted_in_window`（o2o h=1/5/10/20 分别 89/532/1277/2498 条）与
  `exit_delayed_days` 样本全部带收益保留。
- 复权因子变化日手工核对：`000333.SZ` 在 2019-12-30/31 的 `ret_o2o_1`
  （跨越 2020-01-02 `adj_factor` 4.513→4.5132）与手算
  `adjusted_open` 比值**逐位一致**。
- 独立目录（相同上游产物副本）重建：`sha256` **完全一致**，行数一致，可复现。

## Step 4：基准收益 `daily_benchmark_return`（2026-10）

### 实现

- `src/core/reference/market.py`：唯一读取 `index_daily` 原始表的位置，校验后发布
  L1 `index_daily_available`（主键 `[ts_code, trade_date]`），供官方口径标定消费；
  `core.benchmark` 仍不直接读原始表。
- `src/core/benchmark/returns.py`：发布 L3 `daily_benchmark_return`，主键
  `[trade_date, benchmark_id, method]`。**成员取 T 日、收益取 T+1**：
  研究样本（`all_a_ex_st_ipo120_ex_bj`、`all_a_ex_st`）取
  `daily_research_universe` 的 `is_in_universe IS TRUE` 行；指数子样本
  （`index_member_hs300/zz500/zz1000`）取 Step 2 的 `daily_index_member`
  （已含发布滞后）。收益为 T+1 行的 `ret_1d`（复权收盘对收盘），其值为 NULL
  的成员（T 或 T+1 缺行情）**同时**从等权均值与市值加权分母中剔除，不按 0 计、
  不前值填充，`member_count` 与 `return_count` 之差保留可见。权重用 T 日
  `circ_mv`（万元，归一化后单位约掉）。有成员但无可用收益的日期保留
  `ret=NULL` 行，无成员的日期不出行。
- `src/core/benchmark/calibration.py`：`calibration_metrics()` 纯函数（ddof=0
  总体方差，与预处理 z-score 口径一致；Pearson 在 n<2 或任一序列方差为 0 时为
  None）+ `build_benchmark_calibration()`，输出 `calibration.json` 与
  `calibration.md`，显式标注 `self_built_approximation_not_official_index`。
- **位级可复现性修复**：首次双目录构建发现 sha256 不一致，逐值比对确认为并行
  浮点 `avg/sum` 的聚合顺序造成末位 ulp 差异（20,124 行中 14,878 行差约 1e-15）。
  按 `repeatability.preferred_method=fixed_sort_order` 的冻结约定，改为
  **DECIMAL 精确求和、仅最终商转回 DOUBLE**（求和顺序无关），逐元素乘积仍为
  确定性的 DOUBLE。修复后双目录 sha256 **完全一致**。
- 配置：`configs/tushare_tables.yaml` 确认 `index_daily` 主键并补充
  `required_fields`；`configs/data_middle_layer.yaml` 新增 `reference_market`；
  `configs/factor_research.yaml` 新增 `benchmark` 段（方法、两类样本来源、标定
  参数与口径声明）。
- 夹具：`golden_stage2` 的 `index_weight.csv` 由 2 个成员扩为 4 个（新增
  SA003/SA004），新增 `index_daily.csv`（000300.SH，index15–18 四天）；
  `expected.json`/`README.md` 增加 `benchmark` 人工预期。

### 验证结果

- 新增 `tests/test_benchmark_return.py`（8 项），全量单元测试 **207 项通过**：
  - index16（20200324）等权 = `1/30`、流通市值加权 = `66500/2465000`
    （SA004 当日 `ret_1d` 恰为 0.1；SA003 停牌 `ret_1d` 为 NULL，
    `member_count=4`/`return_count=3`）与手算逐位一致；
  - index17 SA003 仍 NULL、两口径收益均为 0；
  - 标定闭式解：官方收益 `[0,0.1,0,0]` 恰为自建等权的 3 倍，Pearson 精确为 1、
    `mean_diff=-1/60`、`tracking_error_daily=sqrt(1/1200)`；另有独立手算样例与
    空输入/常数序列/单点退化用例。
- 真实全量数据：`index_daily_available` 1,800 行；`daily_benchmark_return`
  **20,124 行**（4 个样本 × 2 口径），指数子样本成员数恒为 300/500/1000。
- **000852.SH 标定**（重叠 2019-01-02 ~ 2026-06-08，1,800 天，重叠充足）：
  - 等权：Pearson **0.98737**，日跟踪误差 0.2465%，年化 3.91%；
  - 流通市值加权：Pearson **0.99870**，日跟踪误差 0.0905%，年化 1.44%。
  - 结论：加权口径显著优于等权（与官方市值加权方式一致所致），偏离可由成分
    发布滞后、`circ_mv` 非官方自由流通靠档加权、官方为价格指数（不含分红再投资）
    解释；自建 300/500 基准的可信度据此标定，报告中明确为自建近似口径。
- 000300.SH/000905.SH 官方日线下载继续按用户指令挂起，官方到位前 Step 4 以自建
  基准为主，报告不冒充官方指数。

## Step 5：因子框架与基线因子库 `factor_value`（2026-10）

### 实现

- `configs/factor_registry.yaml`：17 个基线因子全部登记（id/version/类别/输入/
  公式/回看/min_periods/方向/单位/valid_from/PIT 说明），并冻结共享口径：
  万元→元、成交额千元→元（显式假设）、as-of 规则、交易日序号滚动窗口、
  DECIMAL 确定性聚合。
- `src/core/factor/registry.py`：注册表加载与校验。
- `src/core/factor/asof.py`：财务 PIT as-of 展开。按 `(ts_code, end_date)` 的
  版本区间 `[available_date, 下一版本 available_date)` 展开到每日；
  `current_metric_daily` 取**最新可见 end_date**，其可见版本非 `available`
  时保持空，**不回退到更早报告期**。资产负债表经 `financial_available`
  同样按版本区间展开。
- `src/core/factor/baseline.py`：17 因子执行引擎。滚动窗口用 `trade_index`
  RANGE 帧（停牌日仍占窗口位置，不偷连），`min_periods` 统计帧内非空输入；
  动量要求精确端点有行情。`start/end` 只限输出、`input_end` 限全部输入
  （截断门禁依赖这一分离）。只发布非空值，按 `valid_from` 过滤。
- `src/core/factor/truncate.py`：截断不变性门禁（精确相等比较）。
- `src/core/factor/report.py`：按日覆盖率（含骤降日定位）与厂商对照
  （`ep_ttm` vs `1/pe_ttm`、`bp` vs `1/pb`，含大偏差样本的公告窗口占比）。
- `Store.publish` 分区限制放宽为"主键任一字段"，`factor_value` 按
  `factor_id` 分区（一阶段 `source_table` 分区行为不变）。
- 引擎内存：`factor_research.yaml engine.memory_limit` 由 2GB 提至 8GB
  （1.56 亿行暂存表 + as-of 展开在 2GB 下 OOM；仅作用于二阶段因子构建）。
- **确定性聚合修复**：首次真实数据截断门禁未通过，差异全部是
  `stddev_pop/avg` 窗口函数在不同输入规模下的末位 ulp（非未来函数）。按
  Step 4 同一范式改为**精确 DECIMAL 求和、最终商/开方回 DOUBLE**，修复后
  门禁 0 差异。

### 验证结果

- 新增 `tests/test_factor_baseline.py`（8 项），全量单元测试 **215 项通过**。
- 黄金值/缺席逐值核对：`log_total_mv=ln(1e10)`、`ep_ttm=0.05`（T+1 修订后
  0.052）、`bp=0.5`、`roe_ttm=0.1`（T+1 0.104）、`rev_yoy_q=1.0`、
  `np_yoy_q=0.25`（T+1 0.36111）等；SA004 在 index35 的 `rev_20=0.1`、
  `vol_20=sqrt(0.000475)`、`amihud_20=0.1/(10450*1000*20)`；SA003 停牌窗口
  不足 `min_periods` 不得出值；`mom_12_1/mom_6_1` 在 66 天夹具内无任何值。
- **截断不变性**：夹具 66 个交易日 × 17 因子重算 0 差异；真实数据抽样
  3 个截断日（2020-04-30、2022-06-30、2024-12-31）× 17 因子，比较
  **245,002 个值，0 差异通过**（`truncation_invariance.json`）。
- 真实全量构建：`factor_value` **156,021,260 行**（约 26 分钟，1.37GB），
  主键唯一，`valid_from` 之前无值；各因子覆盖起点合理。
- 有界窗口（2024Q1）独立双目录重建：**sha256 完全一致**，可复现。
- **覆盖率**：17 因子按日覆盖率入 `coverage_report.json/md` 与
  `coverage_daily.parquet`；覆盖率骤降（低于前一日一半）日期 **0 天**。
- **厂商对照**（`vendor_comparison.json/md`）：
  - `ep_ttm` vs `1/pe_ttm`：633 万对，绝对相对偏差中位数 **6.5e-07**、
    P90 3.3e-06（与厂商基本一致）；大偏差样本 27.2 万，其中 **31.9%**
    落在公告窗口（PIT 可见性差异的预期集中形态）。
  - `bp` vs `1/pb`：928 万对，中位数 **1.77e-05**、P90 6.41%；大偏差样本
    115.8 万，其中 **14.9%** 落在公告窗口。`bp` 偏离明显大于 `ep_ttm`，
    与厂商 `pb` 的权益口径/报告期选择不同一致，已作为偏离分布记录而非
    强行调齐。

## Step 6：因子预处理 `factor_processed`（2026-10）

### 实现

- `src/core/preprocess/ops.py`：纯 numpy 截面算子，测试与生产共用同一份实现。
  Winsorize 用 `median ± 5×MAD`（无 1.4826 缩放）；**MAD=0 时跳过截断**
  （避免整个截面被压到中位数）；z-score 用 ddof=0；中性化用 within（固定效应）
  形式——行业组内去均值后拟合**单一公共斜率**，与"行业哑变量 + log_circ_mv
  截面 OLS 残差"代数等价，未知行业单列一组。
- `src/core/preprocess/panel.py`：按 `(universe, factor, trade_date)` 逐截面处理
  （截面内按 ts_code 固定排序，保证 numpy 求和顺序可复现），输出主键
  `[trade_date, ts_code, factor_id, universe_id, process_id]`，按 `factor_id` 分区。
  两个处理版本：`ws_z`（主口径）与 `ws_z_industry_mv_neutral_z`（对照）。
  缺失不插补；`min_cross_section`（生产 30）不足的日期**不出行并记录原因**；
  中性化另需残差自由度 ≥ `min_neutralization_df`。
- **新增退化边界处理**：夹具中 `log_total_mv` 与 `log_circ_mv` 完全相等，
  中性化拟合完美、残差标准差仅 ~1e-15，再标准化会把舍入误差放大成伪截面。
  增加 `min_residual_std=1e-10`，低于即跳过并记录
  `degenerate_residual_variance`。
- 行业口径用 strict 政策：`snapshot_backfilled`/缺失行业一律进入"未知行业"
  哑变量，占比按日写入诊断。诊断产物：`processing_diagnostics.parquet`
  （逐日逐因子）、`winsorize_quantiles.parquet`（去极值前后分位数抽样对比）、
  `processing_report.json/md`。

### 验证结果

- 新增 `tests/test_preprocess.py`（10 项），全量单元测试 **225 项通过**：
  - 夹具既有手写预期逐值吻合：median 102.5、MAD 7.5、边界 [65,140]、
    SA008 截断至 140、均值 107.5、std 15、8 个 z 值全部一致；
  - 中性化手算例（两组，beta=1.2，残差 [0.2,-0.2,-0.4,0.4]）精确一致；
  - 端到端 `ws_z` 与 Python stdlib 独立重算一致（均值 0、标准差 1）；
  - 中性化输出满足正交性：Σresid=0、Σresid·log_circ_mv=0、分行业残差和=0；
  - T 日 SA007 因 `in_date` 未到而进入未知行业哑变量（占比 1/8），
    20200414 起归零；`log_total_mv` 共线退化案例不出行且原因记录；
    `min_cross_section=9` 时全表无行且原因均为 `insufficient_cross_section`。
- 真实全量构建（默认样本 `all_a_ex_st_ipo120_ex_bj`）：
  `factor_processed` **287,215,854 行**（约 5 分钟）。
- 真实数据正交性：各因子逐日最大行业残差和 ≤ **4.6e-12**、
  最大 |Σresid·x| ≤ **1.6e-10**，数值上为 0，满足验收。
- 未知行业占比：均值约 **10–12%**、单日最高约 **26%**（`in_date` 前的
  backfilled 与未覆盖股票），已按日入报告，供后续敏感性分析。
- 跳过记录：仅财务类因子在早期有 5–32 天 `insufficient_cross_section`，
  全部带原因、无前值填充；`zero_mad` 与中性化跳过的日期亦入诊断。
- 有界窗口（2024Q1）独立双目录重建：**sha256 完全一致**。

## Step 7：单因子评价（2026-10-31）

### 实现

- `src/core/evaluation/ops.py`：评价公式与生产构建共用同一 numpy 实现：
  Pearson IC、平均秩 RankIC、IC/RankIC 均值/总体标准差/胜率、RankICIR 及
  年化值、Bartlett Newey-West t 值（滞后 `h-1`，`h=1` 用普通 t 值）、
  方向调整分层、组换手和财富路径绩效（夏普、最大回撤及日期、Calmar、
  胜率、盈亏比）。
- `src/core/evaluation/builder.py`：唯一连接预处理因子与前向标签的评价入口，
  发布：
  - `factor_ic_daily`：理论/可交易口径、全样本与市值三分位/指数成分分段；
    全样本覆盖 `h=1/5/10/20` 形成 IC 衰减，分段仅主评价 `h=5/20`。
  - `factor_quantile_return`：按 `trade_index % h` 的全部非重叠调仓偏移，
    记录组收益、样本数及相对上一同偏移调仓期的单边换手。
  - `factor_summary`：主键含 `period_id`，覆盖全区间、年度、样本内/外及
    市值/指数分段；包含 IC/ICIR/NW-t、因子秩自相关、单调性、组换手、
    多头与多空绩效和盈亏平衡成本。绩效数值在各偏移路径分别计算后取均值，
    回撤日期取自回撤最深的偏移；成本为平均毛收益/平均单边换手，多空换手
    为两腿之和，均为无费用统计诊断而非交易回测。
- `src/core/evaluation/trial.py`：JSONL 只增研究日志及 Parquet 快照；同一
  `factor_id+version` 正式样本外访问仅允许一次，门禁在读取因子/标签之前执行，
  只有真实提升注册表版本才形成新的研究对象。`scripts/evaluate_factors.py`
  提供区间、因子、处理版本、前瞻期和显式样本外开关。
- `configs/factor_research.yaml` 补齐分层最小组内样本数、分段前瞻期、偏移
  绩效聚合与盈亏平衡成本口径。默认评价结束日截断至 2024-12-31，不会因
  未传 `end` 而触碰样本外。
- 过程中修复：分段集合曾按主前瞻期重复追加（会造成重复主键）；汇总主键
  补上 `period_id`；统一 `YYYYMMDD`/ISO 日期比较与解析；真实全量汇总首次
  因单调性查询物化长数组超过 8GB，改为 **DECIMAL 精确均值**后从已发布
  IC/分层表恢复，求和顺序无关且无需提高内存上限。

### 验证结果

- 新增 `tests/test_evaluation.py`（9 项），全量单元测试 **234 项通过**：
  黄金 IC `-0.16622158559867703`、RankIC 0、并列平均秩、4 期多头/多空
  绩效全部与手写值一致；NW 序列 `[1,2,3,4]`、`h=2` 的 t 值精确为 4；
  换手、方向分层、单调性和盈亏平衡成本另有闭式检查。
- 小型端到端面板验证非重叠偏移、理论/可交易口径、分段与期间主键、日表到
  汇总重算一致、双跑 sha256 一致；样本外第二次访问在构建前抛出
  `PermissionError`，ISO 日期路径同样覆盖。
- 真实样本内全量评价（注册表实际 **18 个因子** × 2 处理版本 × 4 前瞻期）：
  - `factor_ic_daily` **1,541,376 行**，区间 2019-07-01～2024-12-31；
  - `factor_quantile_return` **15,413,760 行**，全部分层均为 10 组；
  - `factor_summary` **3,168 行**，全部含 NW t 值与因子秩自相关，
    `out_of_sample` 行数为 0；
  - `factor_trial_log` **18 行**，每因子一条本次记录，`oos_accessed` 全为
    false，结果指纹已绑定。
- 真实 2024Q1 独立双目录重建：`factor_ic_daily` 66,816 行、
  `factor_quantile_return` 668,160 行、`factor_summary` 1,728 行，三表行数及
  sha256 均**完全一致**；研究日志的 UTC `trial_id` 按设计不参与重复性比较。
- 本轮未正式访问 2025 年以后锁定样本外，样本外额度保持未消耗；每因子
  Markdown 报告属于 Step 9，不在本步提前生成。

## Step 8：因子相关性与简单合成（2026-10-31）

### 实现

- `src/core/evaluation/correlation.py`：在默认样本的 `ws_z` 面板上逐日计算
  两两 Pearson 截面相关，汇总均值、总体标准差和平均绝对相关；以
  `1-abs(平均相关)` 为距离做确定性**完整链接**聚类，阈值 0.7，输出
  `factor_correlation` 及 JSON/Markdown 冗余提示。
- `src/core/evaluation/composite.py`：仅允许两种透明合成，成分均为预期方向
  调整后的 `ws_z` 分数，不使用原始量纲直接平均：
  - `composite_equal`：可用成分等权；个股缺失成分时在剩余权重上重新归一化，
    不插补；每日至少 8 个有效成分。
  - `composite_icir_60`：最近 60 个已实现 RankIC 观测、至少 20 期，ddof=0；
    使用**有符号** `ICIR/sum(abs(ICIR))` 权重，零方差/历史不足权重为 0，
    个股缺失时在剩余绝对权重上重新归一化。
- 滚动权重时点门禁：IC 信号日整截面必须满足 `max(exit_date) < T`，不是
  仅过滤部分股票，也不能用 `planned_exit_date` 替代；训练信号与标签实际出场
  日均不得晚于 2024-12-31。样本外日期的合成值只使用样本内已冻结训练历史。
- 可追溯产物：`factor_ic_realized` 保存每日 RankIC 与 `max_exit_date`；
  `factor_composite_weight` 保存每日成分 IC 均值/标准差/ICIR、权重、最后
  信号日及最后出场日；`factor_composite_value` 保存两种合成值，随后按因子
  ID 幂等合并进标准 `factor_value`。合成值使用 DECIMAL 精确求和后转 DOUBLE。
- 两个合成因子已登记进 `factor_registry.yaml`（version 1），并复用 Step 6
  预处理、Step 7 评价与截断门禁。`build_factor_processed` 和
  `evaluate_factors` 新增按因子 ID 合并选项，避免增量构建覆盖 18 个基线因子；
  基线 `build_factor_values` 默认明确排除合成类别，合成截断会同时重算 IC、
  权重和值，而不是只检查最后平均公式。
- `scripts/build_factor_composites.py` 提供相关性→合成→预处理→可选截断的
  顺序入口；`composition_report.json` 记录合成配置 SHA-256。

### 验证结果

- 新增 `tests/test_composite.py`（8 项），全量单元测试 **242 项通过**：
  - A=[1,3]、B=[-1,1] 时 ICIR=2/0、归一化权重=1/0；出场日等于 T 不可见，
    样本内末日之后实现的标签不可见；
  - 缺失成分等权重归一化手算值 -8、完整链接聚类、逐日相关产物、权重绝对和、
    双跑 sha256 均通过；
  - 12 日夹具全日期合成截断 0 差异，并成功回接预处理、评价及评价增量合并。
- 真实全量构建：
  - `factor_correlation` **153 对**，形成 **14 个聚类、6 对高相关提示**；
    典型为 `log_circ_mv/log_total_mv` 0.918715、`roa_ttm/roe_ttm` 0.902047、
    `amihud_20/log_amount_20` -0.819319，完整结果见
    `data_middle/l4/factor_correlation/correlation_report.md`；
  - `factor_ic_realized` **23,796 行**，`factor_composite_weight` **63,000 行**；
  - `composite_equal` **7,689,596 行**（2019-07-01 起），
    `composite_icir_60` **7,580,111 行**（2019-08-14 起）；
  - 合并后 `factor_value` **171,290,967 行**、`factor_processed`
    **317,755,248 行**，原基线行均保留。
- 真实 2024-12-31 截断门禁：两种合成因子共比较 **9,884 个值，0 差异**；
  权重水位核验 `last_exit_date>=T` 与 `last_exit_date>2024-12-31` 均为 **0 行**。
- 两种合成因子完成 Step 7 样本内评价并增量合并：当前
  `factor_ic_daily` **1,710,592 行**、`factor_quantile_return`
  **17,105,920 行**、`factor_summary` **3,520 行（20 因子）**、
  `factor_trial_log` **20 行**；样本外评价行与 OOS 访问记录仍均为 0。
- 真实 2024Q1 独立双目录重建：相关性 153 行、已实现 IC 23,796 行、权重
  2,088 行、合成值 570,330 行，以及合并因子值/预处理面板的行数和 sha256
  **全部一致**。

## Step 9：标准化因子报告（2026-10-31）

### 实现

- `src/core/evaluation/report.py`：仅从注册表、覆盖率/预处理报告、L4 评价表、
  相关性表和 `factor_trial_log` 生成报告，不重新计算或手工填写绩效数字。
- 每因子输出 `{factor_id}.md` 与 `{factor_id}.json`，内容包括：定义/版本/
  输入/方向/PIT 约束、覆盖率、预处理诊断、h=1/5/10/20 IC 衰减、两种处理
  版本对比、分层换手与多头/多空绩效、年度评价、市值与指数成分分段、
  相关冗余提示、样本外状态和来源哈希。
- 主口径固定为 `ws_z / theoretical / full / in_sample`；所有前瞻期按数值
  1/5/10/20 排序。没有样本外评价时明确写“尚未正式访问锁定样本外”，不生成
  推测性样本外结论。
- 全因子汇总输出 `summary.md`、`summary.json`、`summary.csv`，并列 h5/h20
  RankIC、年化 RankICIR、NW-t、多空绩效、覆盖率和样本内外状态。
- 每份报告记录 trial_id、评价区间及 L3/L4 来源行数与 SHA-256。增量合并后
  当前 L4 文件哈希可能与历史单次 trial 哈希不同，两者同时保留，报告数字
  始终对应当前列明的 L4 文件。
- `manifest.json` 固定代码哈希、研究/注册表配置哈希、全部来源哈希和每个报告
  文件哈希；不写生成时间，避免无意义的双跑差异。
- `scripts/build_factor_reports.py` 默认先刷新 20 因子覆盖率，再生成全部报告；
  `--skip-coverage` 支持使用已验收覆盖率的纯报告重复构建。

### 验证结果

- 新增 `tests/test_report.py`（2 项），全量单元测试 **244 项通过**：报告与
  小型 L4 面板数值一致，IC 衰减/年度/分段/相关性内容齐全，样本外状态与
  trial_id 可追溯，重复生成的全部文件哈希一致。
- 真实报告：20 个因子共 **40 个单因子文件 + 3 个汇总文件 = 43 个内容文件**；
  覆盖率报告已刷新为 20 因子。所有汇总行均为 `in_sample_only`，没有样本外
  绩效结论。
- 对 20 因子 h5/h20 的 RankIC、年化 RankICIR、NW-t、收益、夏普、回撤、换手、
  单调性和秩自相关执行自动对账，共 **440 项数值与 `factor_summary` 完全一致**。
- 真实独立双目录重建：报告 manifest 完全相同，**43/43 个内容文件 SHA-256
  全部一致**。
- 报告明确披露：行业 PIT 仅 `limited`、自建指数基准口径、无费用/无成交队列
  假设、相关聚类仅为冗余提示，不将统计评价解释为可交易承诺。

## Step 10：统一质量门禁（2026-10-31）

### 实现

- `src/core/quality/stage2.py` 与 `scripts/check_stage2_quality.py` 统一执行：
  Stage 1 准入与原始数据不变证据、Stage 2 源码原始表扫描、因子/标签隔离、
  L1–L4 当前文件 SHA-256/schema/行数/主键及上游血缘、财务与标签/行业/合成
  权重 PIT 水位、全因子截断重算、Stage 1 与 Step 7–9 双目录重复性证据、
  trial/报告样本外状态及 Step 0–10 文档同步。
- 普通产物要求上游哈希与当前 manifest 一致；增量合并表标记为
  `incremental_mixed`，允许保留历史上游引用，但仍强制当前文件完整性、
  schema、主键和行数；L3 可声明原始来源血缘，L4 仍要求 `raw_tables=[]`。
  可选 `--deep-primary-keys` 会重新扫描
  全表重复/空主键；默认依据发布门禁及当前文件哈希验证未发生变化。
- 输出 `verification/stage2_quality_gate.json`、`.md` 和
  `step10_truncation.json`。行业 PIT `limited`、官方指数核验 `suspended`、
  涨跌停覆盖边界单列为 acknowledged boundaries，不与失败项混淆。
- 过程中修正门禁自身假设：统一接受 `YYYYMMDD`/ISO 截断日期；按 c2c 的 T
  收盘入场与 o2o 的下一交易日入场分别检查标签；区分 L3 原始血缘声明与
  L4 禁止原始表；采用实际基准主键 `benchmark_id/method`；按能力配置对象
  读取边界状态，并显式登记官方指数核验 `suspended`。

### 验证结果

- 新增 `tests/test_stage2_quality.py`（5 项）及紧凑日期截断回归，全量单元
  测试 **249 项通过**；测试覆盖文件/schema/主键、重复性证据、文档同步、
  样本外状态和 c2c/o2o 入场日差异。
- 真实统一门禁 **通过，8/8 检查组通过、失败数 0**：architecture、
  stage1_entry、layering_lineage、pit、truncation、repeatability、
  out_of_sample、documentation。
- 共核验 **28 类 L1–L4 产物**的当前 parquet SHA-256、manifest schema/行数/
  主键与血缘；普通产物的上游哈希严格一致，增量合并表的历史引用被明确
  标记而非误报。
- 20 个因子在 2024-12-31 全部重新截断计算，共比较 **104,270 个值，
  0 差异**。
- PIT 水位中财务可见日倒置、非法标签区间、非有限收益、空收益缺原因、
  越数据末端收益、非法行业状态、权重未实现/越样本内训练及 IC 历史越界
  均为 **0 行**；3,400,222 行 `snapshot_backfilled` 仅作为已知前视风险
  边界披露。
- Stage 1、Step 7 三表、Step 8 六类产物及 Step 9 报告共 **11 组重复性
  证据全部一致**；20 条 trial、样本外汇总行和 43 个报告文件状态核验通过，
  未记录样本外访问的因子没有样本外评价行。
- 正式结果见 `data_middle/verification/stage2_quality_gate.json` 与 `.md`；
  三项已披露边界仍为行业 PIT `limited`、官方指数核验 `suspended`、
  涨跌停事件完整性 `false`，没有被总体通过状态隐藏。

## 阶段 2 完成与阶段 3 交接（2026-10-31）

- Step 0–10 共 **11 个步骤全部完成**，阶段 2 状态标记为 **完成**；最终全量
  单元测试 **249 项通过**，统一质量门禁 **8/8 通过、失败数 0**。
- 可供阶段 3 使用的核心输入：L1/L2 股票池与行情/财务事实、L3 行业/指数/
  标签/20 因子原值与预处理面板、L4 单因子评价/相关聚类/合成权重/trial 记录，
  以及 `data_middle/l4/factor_report/` 的 20 因子报告和全因子汇总。
- 阶段 3 的首要任务为**策略方案设计**：明确投资目标、信号映射、组合构建、
  约束、调仓、成交与成本、回测口径、风险指标、样本外纪律和逐步验收门禁；
  方案确认前不直接进入策略实现。
- 新阶段必须继续执行：原始数据只读与分层访问、全链路 PIT、截断不变性、
  独立双目录确定性、append-only 研究记录和样本外单次正式访问纪律。不得把
  因子层理论多空收益直接当作策略可交易收益。
- 默认继续使用样本内数据；任何 2025 年以后的正式样本外评价都必须先经过
  版本与访问门禁。行业 PIT `limited`、官方指数核验 `suspended` 和涨跌停
  覆盖边界必须进入策略假设、回测限制和最终报告。
- 本交接记录不代表已经完成策略样本外验证或具备实盘条件；阶段 3 应另行
  建立策略级方案、实现、回测、执行与质量验收计划。

## 一键重建与分阶段部署（2026-11-01）

- 新增 `scripts/build_stage2.py` 与 `core.pipeline.stage2`，提供从 Stage 1
  已验收产物到 Step 10 的完整、失败即停编排。它包含 Stage 2 额外的
  `index_member_available`、`index_weight_available`、`index_daily_available`
  L1 参考表构建，因此新设备只需先完成 Stage 1，不需要手工补跑隐藏步骤。
- 前置条件：Python 依赖已安装、原始 `DATA_ROOT` 已挂载、`MIDDLE_ROOT` 可写、
  Stage 1 一键构建及质量门禁已通过。原始数据与中间数据目录必须分离。
- 仅查看执行计划（不读写数据）：
  ```bash
  conda run -n quantkernel python scripts/build_stage2.py --dry-run
  ```
- 完整构建：
  ```bash
  conda run -n quantkernel python scripts/build_stage2.py
  ```
- 顺序执行：Stage 1 准入→全量黄金/单元测试→行业与指数参考→标签→基准→
  18 个基线因子及截断→预处理→Step 7 评价及双目录重复性→2 个合成因子、
  截断与增量评价→Step 8 双目录重复性→20 因子报告→Step 9 双目录重复性→
  原始数据未变更检查→20 因子统一截断与 Step 10 门禁。
- 不执行联网下载，不访问锁定样本外。合成预处理合并后会重新发布相关性与
  合成产物以绑定当前上游哈希，避免新设备因历史增量血缘而无法通过最终门禁。
- 运行状态写入 `verification/stage2_build_status.json`，失败时记录具体阶段；
  成功记录写入 `verification/runs/stage2_latest.json`，原始数据独立性证明写入
  `verification/stage2_raw_unchanged.json`。旧质量通过状态在重建开始时即改为
  `rebuilding`，不会与本轮结果混淆。
- 默认重跑发布门禁与当前文件完整性；如需额外全表重复/空主键扫描，使用
  `--deep-primary-keys`。`--record-run NAME` 可保存独立运行记录。
- 新增 `tests/test_stage2_pipeline.py`（3 项），覆盖 dry-run、CLI、全流程步骤
  与重复性比较。当前全量回归 **287 项通过**；本次仅验证编排和 dry-run，
  未重新执行整套耗时的真实 Stage 2 重建。
- 分阶段部署时，Stage 1 使用既有 `scripts/build_stage1.py`，Stage 2 使用上述
  `scripts/build_stage2.py`；Stage 3 保持独立入口和独立验收，不由本脚本隐式执行。
