# 第一阶段数据处理层后续改进说明

本文记录 V1 之后建议优先处理的 4 个改进目标及当前执行状态。改进 1–4 均已于 2026-09 完成（依据本地补充的 `namechange`、`stk_limit`、VIP 财务缺失季度原始数据与扩展后的 `trade_cal`）；退市整理期按约定继续挂起。

## 1. 补充历史 ST、名称变更和退市整理期数据 —— ST 与名称变更已完成

已下载 `namechange`（5,907 个按代码文件、14,205 行，区间为双端包含 `[start_date, end_date]`，`end_date` 为空表示至今），并据此派生：

- `daily_stock_state.historical_name`：按交易日适用的历史名称；`historical_name_changes` 能力解除挂起。
- `daily_stock_state.is_st / is_star_st`：名称含 `ST` 字样即视为 ST（`*ST`、`S*ST`、`SST`、`GST` 均满足），`*ST` 同时视为 `is_star_st=true`；仅上市期间有值。
- `historical_state_status`：`namechange_derived`（区间覆盖且语义一致）/ `namechange_uncovered`（无覆盖区间）/ `namechange_ambiguous`（重叠区间 ST 语义冲突）/ `not_listed`。
- 依赖 ST 的三个 `ex_st` 样本空间已激活并生成成员。

已知边界（保留原始记录、诊断披露、不猜测回填）：

- 7 组重叠区间（4 只股票，ST 语义一致，按一致值派生；若冲突则置空并标 ambiguous）。
- 243 处区间内空洞、73 只股票首个区间晚于 `list_date`、`T600018.SH` 退市历史代码无记录：这些证券日 ST 为未知。
- 7 个 namechange 代码不在当前 `stock_basic`（旧退市/北交所代码）。
- **退市整理期仍缺源**：namechange 不含整理期起止区间，`is_delisting_period` 保持有类型 NULL，能力继续挂起。

## 2. 增加上市年龄替代字段 —— 已完成

`trade_cal` 已补充 20180101–20180531，日历现覆盖 20180101–20260911（2,111 个交易日）；20180102 及以后上市的股票可精确计算上市交易日数。对更早上市的老股，`daily_stock_state` 新增两个明确口径的替代字段：

- `listing_natural_days`（BIGINT）：`list_date` 起算的自然日差；未上市为 0，退市后冻结在退市日前一日。
- `estimated_listing_trade_days`（BIGINT）：`listing_natural_days / 365.25 * 252` 四舍五入取整，明确标注估算口径，不冒充精确值。

精确上市交易日数在日历覆盖起点（20180102）之前仍为 NULL（`left_censored`），不用估算值冒充。

## 3. 补充逐股逐日涨跌停价格 —— 已完成

已下载 `stk_limit`（2,116 个日分区 20180102–20260918、9,866,957 行，`(trade_date, ts_code)` 无重复，覆盖 SSE 日历全部交易日）。`daily_trade_status` 新增：

- 事实字段：`up_limit`、`down_limit`、`limit_price_exists`、`source_limit_price_file/row`。
- 方向判断：有报价且有限价时，`is_limit_up = close >= up_limit`、`is_limit_down = close <= down_limit`，价格证据优先于事件推断；`limit_list_d` 的 U/D/Z 事件仍保留为独立观测事实。
- 原 `LIMIT_STATUS_UNKNOWN` 大幅收窄：仅限价缺失（约 6.2 万个有报价但缺限价行的证券日，集中在 2018–2022）或限价无效时保持未知。

已知边界：

- 280 行 BJ 占位值（`up_limit=99999.99, down_limit=0.0`）按无效处理：保留原始行、不参与方向判断、诊断披露。
- 155 个限价代码不在当前 `stock_basic`（B 股 200xxx 与退市旧码），保留并披露。
- 与 `limit_list_d` 交叉验证：U 事件与 `close>=up_limit` 零矛盾；价格证据额外覆盖官方不统计的 ST 股票（约 4.6 万个无 U 事件的价格型涨停日）。

## 4. 跟踪财务缺失季度 —— 已完成主要补齐（limited）

三张 VIP 财务（`income_vip`、`balancesheet_vip`、`cashflow_vip`）经非 VIP 逐股接口补齐（当前账号无 VIP 接口权限，非 VIP 字段为 VIP 子集）：

- 新增报告期分区 `20170930`、`20171231`、`20180331`（此前首期为 20180630，缺 2018Q1 及 TTM 前史）。
- `20260331`、`20260630` 补充文件：只含原分区已有股票之外的部分（原 `20260331` 每表约 1100 行、`20260630` 每表仅 1 行，补后各期代码数约 5700 / 6100）。
- 只取 `report_type='1'`，`update_flag` 0/1 两版都保留（与本地 VIP 数据一致）；同键版本对的累计指标值预检一致（仅 20260630 各 1 组真实冲突，按 ambiguous 处理）。

日历边界（保留记录、标注状态、不猜测回填）：

- `20170930` 原始公告（2017-10）早于日历起点 20180101，处理层标 `calendar_before_coverage`（保留但不可见）；依赖它的单季 2017Q4 与 TTM 2018Q1–Q3 保持缺失。
- `20170630` 及更早报告期未纳入补数范围（公告窗口自 20170901 起）。
- 北交所/新三板旧代码多披露年报而少季报，故 `20171231` 代码数（约 5100）多于 `20170930`/`20180331`（约 3900）。
- `fina_indicator` 首期仍为 20180630（不进入单季/TTM 差分，未补）。

当前报告位置：

- `data_middle/l2/data_quality_report/REPORT.md`
- `data_middle/l2/data_quality_report/report.json`
- `data_middle/l1/quarterly_financial/`
- `data_middle/l1/ttm_financial/`

上一轮量级（V1 记录）：`quarterly_financial` 约 47.97 万行，其中约 4.6 万行值为空；`ttm_financial` 约 49.9 万行，其中约 13.27 万行为空。本轮补数后的量级以重建后的质量报告为准。
