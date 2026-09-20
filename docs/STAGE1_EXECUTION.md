# 第一阶段实施与验收记录

验收按 Step 0–11 严格顺序进行，未通过当前步骤不得执行后续步骤。2026-09 改进轮（namechange/stk_limit/日历扩展）的记录见文末"改进轮 V1.1"一节。

| 步骤 | 状态 | 证据 |
|---|---|---|
| 0 路径与工程约定 | 通过 | 路径检查成功；`tests/test_paths.py` 5 项通过；原始/派生数据未被 Git 跟踪 |
| 1 全量真实数据 | 通过 | 本地与服务器 109,320 文件逐路径、大小、SHA-256 全一致；9 张关键表可读 |
| 2 黄金测试数据 | 通过 | 5 只人工股票、34 个交易日；契约测试通过，重建一致 |
| 3 数据画像 | 通过 | 69 张真实业务表完整扫描；核心表字段确认；累计测试通过 |
| 4 交易日历 | 通过 | SSE 交易日主键唯一；日历测试通过（V1.1 起覆盖 20180102–20260911，2,111 交易日） |
| 5 复权价格与收益率 | 通过 | 9,415,148 行；15 项测试通过；全量复权与断档错误 0 |
| 6 每日证券状态 | 通过；V1.1 起 ST/名称由 namechange 派生 | 12+8 项测试；退市前 288,376 行正常上市事实保留；退市整理期/板块仍缺源挂起 |
| 7 每日买卖可行性 | 通过；V1.1 起限价证据优先 | 16+8 项测试；全量方向/停牌/BJ未知约束错误 0；限价占位行排除并披露 |
| 8 财务公告可见性 | 通过（供应商完整修订档案仍受源限制） | 643,462 行全保留；21 项专项测试；严格滞后、来源守恒核验通过 |
| 9 单季度与 TTM | 通过；缺季/冲突不补造 | 479,736 单季版本、498,990 TTM 版本；13 项测试；全量来源/可见日期错误 0 |
| 10 研究样本空间 | 通过；V1.1 起三个 ex_st 样本激活 | 10+3 项测试；all_a 9,414,104 成员证券日；退市前 288,376 行保留 |
| 11 统一质量与阶段验收 | 本地通过；服务器小区间验证按用户要求 deferred | 全量测试 168 项通过；证据见 `verification/` |

## Step 0

默认根为项目内 `data/`、`data_middle/`；可选服务器配置和环境覆盖。
输入输出目录须互不包含。缺失原始目录、输出越界均立即报错。
保留用户原有忽略规则，仅补充派生/大文件规则并修正过时注释。

## Step 1

`python scripts/verify_raw_data.py --checksums` 只读遍历原始文件，保存路径、
大小、修改时间与 SHA-256，并解码每张关键表的一份原始 Parquet。
清单保存在被 Git 忽略的 `data_middle/verification/`。

服务器实测（2026-09-12）：71 个一级目录、109,320 个文件、
7,998,006,450 字节，即 8.00 GB / 7.45 GiB。计划中的约 7.7GB 是近似值，
后续以逐文件同源比较为准，避免混淆十进制/二进制单位。
本地元数据比较已通过：目录、相对路径、逐文件大小全部一致；
`metadata_inventory_comparison.json` 仅表示元数据一致，不能替代内容哈希验收。
`scripts/ops/read_server_inventory.py` 可只读获取服务器清单与 SHA-256；
它属于运维辅助工具，不参与核心数据处理，也不修改服务器文件。

初步确认行情及复权因子同在 `stk_factor_pro`；`trade_cal` 按交易所分文件。
当前一级目录未见独立历史名称变更/ST 区间/退市整理期表；
该缺口需在后续真实字段画像中明确，不能以当前名称回溯代替历史状态。

最终 `inventory_comparison.json`：`passed=true`、`hashes_checked=109320`，
缺失文件、多余文件、大小不一致、哈希不一致均为 0。原始数据未被 Git 跟踪。

## 用户补充的缺源处理规则

用户已明确：先参考 `data/README.md`；确实缺少相应数据的部分暂不实现，
挂起并说明，继续完成可独立验证的部分。挂起项不视为验收通过，
不能用当前状态或猜测补成历史事实。配置和报告需列明挂起能力。

用户随后明确：暂不上传服务器，仅完成可在本地进行的验证后停止。
因此 Step 11 的服务器小区间构建保留 `deferred_by_user`，本轮没有上传或远程构建；
Step 1 已完成的只读服务器清单核验仍保留为既有证据。

初步只读核实：历史 ST/*ST/名称和退市整理期区间缺源；股票每日涨跌停价缺源，
`limit_list_d` 只有自 2020 年起的事件，`ft_limit` 属于期货。
日历仅覆盖 20180601–20260911，不能精确恢复此前老股上市交易日数。
VIP 财务还存在 `f_ann_date`，可晚于 `ann_date`，应保留并按较晚日期控制可见性。
三张 VIP 表从 20180630 开始，首期前置 Q1 缺失；20260630 分区各仅 1 行，
须披露源覆盖，不得补造财务数据。

## Step 2

`tests/fixtures/golden/` 包含 10 张人工 CSV、人工 `expected.json`、README 和重建脚本。
自然日覆盖 20181217–20190208，含 34 个交易日，5 只人工证券。
黄金数据未读取真实源文件。历史 ST 与整理期样例保留为未来能力的明确契约；
真实缺源功能按用户要求挂起，不能因为有人工样例而宣称真实功能完成。

## Step 3

`python scripts/build_l0.py` 全量扫描 69 表成功，
`python scripts/check_profiles.py` 验收 69 表 / 9 核心表通过。
可读概览：`data_middle/l0/DATA_PROFILE.md`；逐字段详情：`l0/profiles/*.json`；
原始异常定位样例：`l0/verification.json`。
画像校验通过表示扫描和异常报告完整，不表示原始数据不存在异常。
当前累计测试 19 项通过。主要异常与字段语义处理详见 `DATA_SOURCE_NOTES.md`。
核心行情 9,415,148 行、5,804 个代码，键唯一且关键价格/因子/量额无缺失。
全量画像使用的配置快照存于 `_provenance/configs/`；后续仅说明文案/缺源策略
更新不改变已画像字段统计，旧配置指纹仍可追溯。

## Step 4

`python scripts/build_l1.py --only calendar` 基于配置选择 SSE，先验证原始自然日日历的
日期合法性、`is_open` 仅为 0/1、日期唯一且没有自然日缺口，再生成
`data_middle/l1/daily_calendar/part-00000.parquet`。CFFEX 不混入 A 股日历。
输出包含 `trade_date`、`prev_trade_date`、`next_trade_date`、从 1 开始的
`trade_index`、原始自然日覆盖边界 `calendar_start/calendar_end` 和 `exchange`。

`TradingCalendar` 统一提供交易日判断、严格前一/后一交易日、正负交易日偏移和公告可用日。
公告可用日严格晚于公告日；交易日偏移要求起点本身是交易日。
输入越界，以及虽在自然日范围内但已无前后交易日的请求，均抛出明确的
`CalendarRangeError`，不会借用不完整的 `pretrade_date` 字段延伸日历。

`tests/test_calendar.py` 的 18 项测试已通过，覆盖黄金预期、周末、节假日、跨年、月底、
正负偏移、边界错误、交易所选择、重复/冲突日期和休市自然日缺口。
真实产物及 manifest 复核为 **2012 行、2012 个唯一交易日**，交易所仅 SSE，
交易日及自然日覆盖首末均为 **2018-06-01 至 2026-09-11**。
manifest 记录主键 `trade_date`、字段类型、配置指纹及 Parquet SHA-256。

## Step 5

`python scripts/build_l1.py --only prices` 从 `stk_factor_pro` 构建
`data_middle/l1/daily_adjusted_price/`，在派生前拒绝重复行情键、非法日期、非有限或
非正价格/因子、负成交量额、OHLC 关系错误及不在统一日历中的行情日期。
保留原始 OHLC、厂商 qfq/hfq、量额和其他源字段；新增
`adjusted_open/high/low/close = 原始价格 × 当日 adj_factor`。
源交易日期另存 `raw_trade_date`，记录源表、文件和行号，不修改原始数据。

`ret_1d` 仅在该股前一行情日等于统一日历的前一交易日时计算；缺行情后留空，
并由 `prev_quote_date/return_status` 说明首条报价或前一交易日缺失。
日期切片在历史收益率计算之后执行，切片首日保留真实前序行情，
不会将其错误视作新序列，也不使用未来末日因子归一历史价格。

`tests/test_prices.py` 的 15 项测试已通过，包括人工除权连续性、手算收益率、
断档、切片、修改未来价格/因子不改过去，以及重复键和异常数值拒绝。
真实 manifest 为 **9,415,148 行**，主键 `trade_date + ts_code` 唯一。
复核全量产物：四个复权 OHLC 与原价乘因子不一致为 **0**，
前一行情日不相邻却生成非空一日收益率的记录为 **0**。

## Step 6

`python scripts/build_l1.py --only state` 基于完整交易日历与 `stock_basic` 证券集合，
独立于是否有行情生成 `data_middle/l1/daily_stock_state/`。
输出 **5,901 只证券 × 2,012 个交易日 = 11,872,812 行**，
主键 `trade_date + ts_code` 唯一。上市身份按真实 `list_date` 开始，
退市日当天起不再正常上市；不用当前 `name/list_status` 过滤历史。
当前已退市证券在历史正常上市区间仍保留 **288,376 行**，无行情证券也保留身份。

可独立确认的字段包括 `is_listed/is_not_yet_listed/is_delisted`、
覆盖范围内可精确计算的 `listing_trade_days`，以及来源表、文件和行号。
日历起点之前上市的老股，其精确交易日年龄为 `NULL`，
另给 `listing_trade_days_lower_bound` 和 `listing_age_status=left_censored`；
不能把观察区间起点冒充上市日。更早的北交所报价保留在价格层，
不据此提前 A 股上市，也不回溯填入当前市场身份。

`tests/test_stock_state.py` 的 12 项测试已通过，覆盖后来退市、新股、
仅依赖基础身份源、老股年龄下界、历史 T 前缀代码、未来状态修改不改变过去、
切片一致性以及非法日期/重复基础代码拒绝。
历史 ST、*ST、退市整理期和板块生效区间仍缺真实源：
`is_st/is_star_st/is_delisting_period/board` 保持有类型的 `NULL`，
`historical_state_status=deferred_missing_history`；全量复核这些字段被猜填的行数为 **0**。
**本步仅可用身份部分通过，缺源历史状态及老股精确年龄仍挂起；
人工黄金历史状态不用于解除真实数据的挂起项。**

## Step 7

`python scripts/build_l1.py --only trading` 在确认上游覆盖充分后，联结身份、行情、
停复牌和涨跌停事件生成 `data_middle/l1/daily_trade_status/`。
除范围首末外，还检查身份表是否缺失中间某日或某只证券，避免把不完整上游
当成完整输入。真实输出 **11,872,812 行**，主键 `trade_date + ts_code` 唯一。

事实与规则分开保留：`is_suspended/is_full_day_suspended/is_intraday_suspended`、
`has_resume_event`、`is_limit_up/is_limit_down`、`quotation_exists`，以及观测事件类型、
源行情文件/行号、证券文件和停牌/涨跌停源文件；买卖方向分别给出
`can_buy/can_sell` 与 `cannot_buy_reason/cannot_sell_reason`。
已知停牌、未上市、退市、缺行情或零成交量会阻止买卖；日内停牌采用明确标注原因的
保守限制。涨停只阻止买入，跌停只阻止卖出；R 不当作停牌，Z 不当作收盘涨停。

`tests/test_trade_status.py` 的 16 项测试已通过，包括方向区别、日内停牌/复牌、
炸板、零成交、覆盖不全、BJ 未知状态、切片及上游中间缺口拒绝。
`data_middle/verification/step7.json` 记录 `passed=true`，
`blocked_errors/up_errors/down_errors/bj_unknown_errors` 全为 **0**。
但它同时记录 **9,203,925 行买入可行性未知、9,241,933 行卖出可行性未知**；
这些 `NULL` 是明确的源能力限制，不能视为已确认可交易。

真实 `limit_list_d` 不覆盖 ST 且仅从 2020 年起有事件，完整股票双向限价仍挂起；
缺事件不能推断未涨跌停。停牌的否定事实仅在已核验范围内对 SH/SZ 代码成立。
`source_diagnostics.json` 另保留 **41,318 条无法匹配当前身份代码的停复牌事件**
及定位样例；涨跌停事件未匹配数为 **0**。由于缺历史换码映射，
未直接观察到匹配事件的 BJ 停牌状态保持未知。
**本步通过的是已知事实的规则与未知状态保留，完整股票限价、ST 覆盖和跨代码关联
仍挂起，不将未知结论或缺源功能计为验收完成。**

## Step 8

21 项财务可见性测试通过；原始四表 643,462 行全部保留。
`python scripts/check_financial_available.py` 全量门禁通过：643,453 行有效；
9 行非法公告字符串 None 保留并隔离；候选键冲突组为利润表 6、资产负债表 31、现金流 2，全部版本保留。
来源计数、位置列表、严格下一交易日检查均无错误，证据为 `verification/step8.json`。
全量宽表排序曾触发 2GB 内存上限，改为窄记录标识去重、分来源排序并流式写入同一 Parquet 后成功。
发布前检查主键及来源，失败临时文件不成为正式产物。供应商完整历史修订快照缺口继续披露。

## Step 9

13 项专项测试通过，独立快照重算另验证 200 组事件流/56,585 次时点检查。
全量单季 479,736 版本，其中 433,634 有效；缺前期季度 45,891、缺金额 205、冲突相关 6。
全量 TTM 498,990 版本，其中 366,262 有效；缺季度 132,719、冲突 9。
所有有效 TTM 均有四个来源季度，季度版本及原记录可见日均不晚于派生版本日期，
来源金额加总和谱系检查错误为 0；证据为 `verification/step9.json`。
修订只从可用日起更新当季、同年下一季及受影响的四季窗口，不改历史已发布版本。
只转换配置的累计白名单；余额、EPS 和未配置口径不机械差分。缺失与冲突留空并带原因。

## Step 10

10 项专项测试通过；额外只读分类验证覆盖 39 项身份/年龄/交易状态组合。
`research_universe_config` 有 4 条配置，仅 all_a 为 active，三个 ex_st 规则为 deferred 且无成员行。
每日输出 11,872,812 行，其中 all_a 成员 9,414,104 行，非成员 2,458,708 行。
身份表达式和配置关联错误 0，后来退市证券在退市前保留 288,376 个成员证券日；证据 `verification/step10.json`。
缺行情、停牌和买卖方向未知不改变身份样本成员资格；IPO 门槛只按当日精确年龄或已证实下界判断。
历史 ST 功能和相应三个样本空间继续挂起，没有因人工样例或配置声明而解除。

## Step 11

统一检查已实现主键/字段类型/空值率/日期范围、价格与源行情对应、交易方向约束、
财务严格公告滞后、原记录指纹、最新可见季度版本、TTM 金额与来源、历史退市身份及配置追溯。
失败报告提供表、字段和证券/日期样例。修复发布追溯后，全量测试 **144 项通过（95.80 秒）**；
两次构建比较脚本随后补充本地范围限制并单独验证通过，涵盖不同结果、缺少画像/阶段和证据不足等拒绝路径。

### 原始目录完整性验收

收尾完整 SHA-256 复核比较 Step 1 的 109,320 个文件与当前 109,322 个文件：
109,319 个非根目录 Markdown 文件全部 SHA 一致，无文件丢失、目录差异或业务数据内容变化。
`data/README.md` 从 24,468 字节变为 24,735 字节；新增 `README_reordered.md`（24,739 字节）
和 `重要表记录.md`（610 字节）。后者在本次端到端构建期间从空文档变为 610 字节。
用户确认这些说明文档为手动写入，不是处理代码写入；本任务保留其内容，不回退或删除。

因此 `verification/raw_sha256_unchanged.json` 与 `verification/raw_unchanged.json`
已按用户确认更新为 **通过**：差异清单保留，验收说明为根目录 Markdown 变化由用户手动产生，
业务原始数据文件 SHA-256 未变，不视为数据处理流程污染 `data/`。
原始 Step 1 清单及其 SHA 保持不变；任何子目录数据、业务文件集合、大小或内容变化仍失败。
69 份已通过的 L0 画像按业务来源一致性复用，单独核对所有画像指纹和当前配置。

首次端到端统一检查还捕获 TTM 来源清单引用旧单季度文件指纹的问题：
发布器在读取新产物 schema 时读取了旧 manifest，污染后续产物的来源记录。
现已改为直接读取新文件 schema，并在发布成功后记录本次文件指纹；
新增重发布回归测试，与财务和完整黄金质量检查合计 15 项针对性验证通过。
原始失败报告保存在 `verification/strict_first/quality_report.json`，
原始失败报告保留在该目录用于追溯；当前验收证据按用户确认的文档变化更新。

服务器小日期区间验证按用户要求暂缓，`verification/server_validation.json`
标记 `deferred_by_user`。未向服务器上传、安装依赖或执行构建。
完整阶段及第二阶段准入继续为 **false**，不会把缺源、暂缓验证或未完成重复性验证冒充通过。

### 仍未完成或挂起的原计划部分

除 Step 11 的重复性验证外，原计划中仍未完成或只能以受限方式实现的部分如下：

- 历史 ST/*ST、完整历史更名、退市整理期：缺少带生效区间的专用历史状态源，相关字段保留 `NULL` 或挂起，三个 `ex_st` 样本空间配置保留但不生成成员。
- 老股票在 SSE 日历覆盖起点之前的精确上市交易日年龄：当前日历从 2018-06-01 开始，不能把覆盖起点倒推为上市日。
- 逐股逐日涨跌停价格和完整双向限价判断：原始表没有股票级 `up_limit/down_limit`；`limit_list_d` 不覆盖 ST 且只提供事件，缺事件时不能证明可交易方向。
- 财务缺失季度：三张 VIP 财务表缺 2018Q1，部分股票缺中间季度，2026Q2 源数据也不完整；单季和 TTM 只在真实齐备季度上计算，缺失部分留空并报告。
- 公告精确时刻：源表只有公告日期，没有公告时刻；V1 使用公告日后下一交易日可见，盘中可见性不实现。
- 财务历史修订全量归档：当前实现保留现有源记录版本和冲突，但不能证明数据供应商历史每次修订前的旧快照都存在。
- 历史证券代码映射和历史市场身份区间：财务源中存在当前 `stock_basic` 无法匹配的旧代码，北交所也有早于当前 `list_date` 的报价；V1 保留原始记录，不猜测回填换码或市场身份。
- 服务器小区间验证：按用户要求暂不上传、不远程执行，当前状态为 `deferred_by_user`。

## 改进轮 V1.1（2026-09）：namechange 派生 ST、stk_limit 限价、日历扩展

原始数据新增 `namechange/`（5,907 文件、14,205 行）与 `stk_limit/`（2,116 日分区、9,866,957 行），
`trade_cal/` 追加 20180101–20180531 分文件；两表登记进 `configs/tushare_tables.yaml` 并完成 L0 画像。

### 数据事实核验（全量只读分析）

- namechange：区间双端包含，`(ts_code,start_date)` 无重复；7 组重叠区间（4 只股票，ST 语义一致）；
  243 处区间内空洞、73 只股票首区间晚于 `list_date`、`T600018.SH` 无记录、7 个代码不在 stock_basic。
- stk_limit：`(trade_date,ts_code)` 无重复；SSE 日历 20180102–20260911 每个交易日均有分区；
  280 行 BJ 占位值（99999.99/0.0）无效；155 个代码不在 stock_basic（含 B 股 200xxx）；
  约 6.2 万个有报价但缺限价行的证券日集中在 2018–2022（2023 年后为 0）。
- 交叉验证：U 事件与 `close>=up_limit` 零矛盾（100,974 行一致）；`close>up_limit`/`close<down_limit` 各 1 行；
  价格型涨停日中约 4.6 万个无对应 U 事件（官方不统计 ST 股票）。
- trade_cal 扩展后日历为 20180102–20260911 共 2,111 交易日；`stk_factor_pro`/`suspend_d` 仍从 20180601 起。

### 实现变更

- `daily_stock_state`：新增 `historical_name`、`name_source_file/row`、`listing_natural_days`、
  `estimated_listing_trade_days`；`is_st/is_star_st` 由名称含 `ST`/`*ST` 派生（仅上市期间）；
  `historical_state_status` 给出 derived/uncovered/ambiguous/not_listed；退市整理期与板块保持 NULL。
  上市状态网格随日历扩展为 5,901 证券 × 2,111 交易日。
- `daily_trade_status`：新增 `up_limit/down_limit/limit_price_exists/source_limit_price_file/row`；
  有报价且有限价时按收盘价比对优先判定方向，事件降级为独立观测；
  限价源日分区完整性作为构建门禁；无效限价与证据冲突进入诊断。
- `daily_adjusted_price` 覆盖门禁改为按 `stk_factor_pro` 源实际日期范围（20180601–20260911），
  日历扩展导致的 2018H1 无行情区间不参与价格覆盖判定，该区间的证券日如实输出 `NO_QUOTE`。
- `daily_research_universe`：三个 `ex_st` 规则激活，ST 排除原因 `EXCLUDED_ST`，
  ST 未知（区间空洞/冲突）时成员资格为 NULL 并标注 `ST_STATUS_UNKNOWN`。
- 质量检查：`invented_history` 重写为对 namechange 重推导校验；新增限价来源一致性
  （`limit_price_sources`）与限价方向一致性（`limit_direction_from_price_evidence`）检查；
  universe 成员期望表达式纳入 ST 逻辑。
- 黄金夹具：`stock_state_history.csv` 删除，新增 `namechange.csv` 与 `stk_limit.csv`（人工名称刻意避开
  `ST` 子串）；`expected.json` 升级到 fixture_version 2。

### 验收

- 单元测试 168 项全部通过（V1 为 144 项，新增 ST 派生、区间冲突、限价证据、占位排除、ex_st 激活等用例）。
- 全量端到端构建与统一质量门禁：`python scripts/build_stage1.py --record-run improvements_v2`，
  证据存于 `data_middle/verification/runs/improvements_v2.json`。
- 财务链路（Step 8/9）按约定未改动；改进计划第 4 点（缺失季度）保留待单独任务。
- 仍未解除：退市整理期区间、20180102 前精确上市交易日数（已提供自然日/估算替代字段）、
  公告时刻、供应商完整修订档案、历史换码映射、跨市场身份、服务器小区间验证（deferred_by_user）。
