# 第一阶段数据处理层后续改进说明

本文记录本次 V1 之后建议优先处理的 4 个改进目标。当前 V1 已完成可用范围内的数据处理层，不实现因子、股票池和回测；以下内容作为下一轮补充数据与增强字段的依据。

## 1. 补充历史 ST、名称变更和退市整理期数据

如果后续需要严格支持剔除 ST、历史样本空间、退市整理期识别和避免幸存者偏差，应从原始 Tushare 数据源补充历史状态类数据。

关键要求：

- 数据必须带有可核验的生效日期和结束日期。
- 不能只用当前 `stock_basic.name` 或零散事件名称倒推历史 ST 状态。
- 补齐后再解除 `historical_st`、`historical_name_changes`、`historical_delisting_period` 和依赖 ST 的 `ex_st` 样本空间挂起状态。

## 2. 增加上市年龄替代字段

当前 SSE 交易日历从 2018-06-01 开始，无法精确还原更早上市股票在覆盖起点前的交易日年龄。下一轮建议增加两个明确口径的字段：

- `listing_natural_days`：按 IPO/list_date 到当前交易日的自然日差计算。
- `estimated_listing_trade_days`：按自然日估算交易日年龄，例如 `listing_natural_days / 365.25 * 252`。

这两个字段应明确标注自然日口径和估算口径，不冒充精确历史交易日数量。

## 3. 补充逐股逐日涨跌停价格

如果后续需要准确判断涨停不可买、跌停不可卖，应从原始 Tushare 数据源补充完整股票级逐日 `up_limit` / `down_limit` 数据。

当前 `limit_list_d` 更适合作为涨跌停事件源，不能证明没有事件的股票当天一定没有涨跌停。补齐股票级限价数据后，可将 `daily_trade_status` 中部分 `unknown` 方向判断升级为明确的 `can_buy` / `can_sell`。

## 4. 跟踪财务缺失季度

财务缺失季度当前不补造，已在质量报告和产物状态字段中披露。下一轮若需要更完整的单季和 TTM 数据，应先评估是否能从 Tushare 补齐缺失季度。

当前报告位置：

- `data_middle/l2/data_quality_report/REPORT.md`
- `data_middle/l2/data_quality_report/report.json`
- `data_middle/l1/quarterly_financial/`
- `data_middle/l1/ttm_financial/`

当前量级：

- `quarterly_financial` 约 47.97 万行，其中约 4.6 万行因缺上一季度或指标缺失导致值为空。
- `ttm_financial` 约 49.9 万行，其中约 13.27 万行因四个季度不齐导致 TTM 为空。

下一轮处理原则仍应是保留记录、标注状态、避免用猜测值补齐。
