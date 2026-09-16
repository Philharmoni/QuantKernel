# 第一阶段：A股量化数据处理层 V1 方案

## 1. 阶段目标

第一阶段只建设数据处理层，为后续因子研究、股票池构建、策略回测提供可信数据基础。本阶段不实现因子、不实现股票池、不实现回测。

核心目标：

- 只读使用 TuShare 原始数据。
- 本地保留一份全量真实数据用于开发和验证。
- 保留历史退市股、历史 ST 股、停牌股等真实历史状态。
- 财务数据按公告日滞后方式做 PIT 对齐。
- 保留原始价格、复权因子、复权价格和收益率。
- 生成可复用的证券状态、可交易性、研究样本空间。
- 所有派生产物可重建、可验证、可追溯。

## 2. 基本原则

1. `data/` 保存 TuShare 原始数据，本项目只读，不修改、不覆盖、不删除。
2. 服务器原始数据约为 7.7GB，规模可接受，因此第一阶段优先将全量数据同步到本地验证。
3. 原始表保留 TuShare 目录名和字段名，例如 `stock_basic`、`trade_cal`、`suspend_d`、`limit_list_d`、`stk_factor_pro`、`income_vip`、`balancesheet_vip`。
4. 派生表使用直白业务名，例如 `daily_stock_state`、`daily_trade_status`、`daily_adjusted_price`。
5. 不用当前状态回溯过滤历史股票；股票是否 ST、退市、停牌、涨跌停，必须按历史日期判断。
6. 财务 PIT 第一版采用公告日滞后：若只有公告日期、没有公告时刻，则从公告日后的下一个交易日开始可用。
7. 状态事实字段与衍生规则字段分离；先保存事实，再派生 `can_buy`、`can_sell`、`is_in_universe`。
8. 第一阶段输出以 Parquet 为主；DuckDB 可作为查询工具，不作为唯一数据存储。
9. 全量真实数据用于发现真实字段、真实规模和真实异常；小型黄金测试数据用于验证规则逻辑。

## 3. 推荐目录

```text
QuantKernel/
  data/                    # 本地全量 TuShare 数据，禁止提交
  data_middle/             # 数据处理中间层输出，禁止提交
  configs/
    paths.yaml
    tushare_tables.yaml
    data_middle_layer.yaml
  scripts/
    profile_data.py
    build_l0.py
    build_l1.py
    build_l2.py
    check_data_quality.py
  src/
    core/
      config/
      data/
      calendar/
      market/
      universe/
      financial/
      quality/
  tests/
    fixtures/              # 小型黄金测试数据，可提交
```

说明：

- `data/` 和 `data_middle/` 不提交 Git。
- `tests/fixtures/` 只放极小人工样例，用来验证 PIT、退市、ST、停牌、涨跌停、复权、财务公告滞后等边界逻辑。
- 代码中不得硬编码本地或服务器绝对路径。

## 4. 数据层划分

### L0：原始数据画像与轻标准化层

L0 不改变数据含义，只确认 TuShare 原始表的位置、字段、主键、日期字段、数据范围和质量问题。

重点表：

- `trade_cal`
- `stock_basic`
- `suspend_d`
- `limit_list_d`
- `stk_factor_pro`
- `income_vip`
- `balancesheet_vip`
- `cashflow_vip`
- `fina_indicator`
- 其他后续确认需要的状态、行业、指数、估值类表

### L1：PIT 对齐与状态派生层

L1 生成策略无关、可复用的标准派生数据：

- `daily_calendar`：标准交易日历
- `daily_adjusted_price`：复权价格与收益率
- `daily_stock_state`：每日证券身份状态
- `daily_trade_status`：每日买卖可行性
- `financial_available`：按公告日滞后的财务可见记录
- `quarterly_financial`：单季度财务数据
- `ttm_financial`：TTM 财务数据

### L2：研究准备层

L2 不做因子、不做策略，只生成通用研究样本空间：

- `daily_research_universe`
- `research_universe_config`
- `data_quality_report`

示例样本空间：

- `all_a`
- `all_a_ex_st`
- `all_a_ex_st_ipo120`
- `all_a_ex_st_ipo120_ex_bj`

## 5. 分步骤实施与验收

### Step 0：确认路径与工程约定

实施内容：

- 建立路径配置，明确 `DATA_ROOT`、`MIDDLE_ROOT`。
- 本地 `DATA_ROOT` 默认指向 `./data`。
- 服务器 `DATA_ROOT` 可指向 `/data/tushare_data` 或服务器项目内 `./data`。
- 配置 `.gitignore`，排除 `data/`、`data_middle/`、大体量 Parquet、DuckDB 文件和日志。

验证标准：

- 本地和服务器可以通过配置切换数据根目录。
- 项目代码不依赖某台机器的绝对路径。
- `data/` 和 `data_middle/` 不被 Git 跟踪。
- 缺少数据目录时，程序能给出清晰错误，而不是静默失败。

进入下一步条件：

- 路径配置、目录策略、Git 忽略规则确认无歧义。

### Step 1：确认本地全量 TuShare 数据

实施内容：

- 确认本地 `./data` 或配置指定目录已经包含服务器 `/data/tushare_data` 的全量数据。
- 校验本地数据保持原始目录结构和文件名。
- 校验本地数据没有改变文件格式、字段名、编码或分区结构。
- 同步命令不纳入核心数据处理逻辑；若未来需要重新同步，可作为运维说明或辅助脚本保存。

验证标准：

- 本地数据总量与服务器 `7.7GB` 规模基本一致，差异可解释。
- 本地一级目录与服务器一级目录一致或差异可解释。
- 关键目录存在：`stock_basic`、`trade_cal`、`suspend_d`、`limit_list_d`、`stk_factor_pro`、财务表目录。
- 关键文件可被本地读取。
- 同步后 Git 不跟踪任何原始数据文件。

进入下一步条件：

- 本地能看到全量真实字段、真实文件格式和真实目录结构。

### Step 2：构造黄金测试数据

实施内容：

- 在 `tests/fixtures/` 中建立极小人工数据。
- 覆盖 3 到 5 只股票、20 到 40 个交易日。
- 至少包含普通股票、ST 区间、新股、后来退市股票、停牌、涨停、跌停、复权因子变化、财务公告滞后、缺失季度。
- 人工写明每个关键规则的预期结果。

验证标准：

- 单元测试不依赖全量 `data/` 也能运行。
- 后来退市股票在退市前仍存在于预期样本中。
- ST 只在 ST 生效区间被排除。
- 财务数据只在 `available_date` 之后可见。
- 涨停不可买、跌停不可卖、停牌不可买卖等规则有明确样例。

进入下一步条件：

- 黄金数据足以验证核心 PIT 与交易状态规则。

### Step 3：数据画像与表说明配置

实施内容：

- 扫描全量 `data/`，生成字段列表、类型、行数、日期范围、股票数量、主键重复情况、缺失率。
- 编写 `configs/tushare_tables.yaml`，记录每张 TuShare 表的用途、路径、主键、日期字段、股票代码字段。
- 不强制改名，不把 TuShare 表翻译成抽象表名。

验证标准：

- 每张关键表都有字段画像。
- 配置中的路径、字段、主键都能在真实数据中找到。
- 主键重复、缺失字段、日期异常能被报告出来。
- 人可以通过配置文件看懂每张表的用途和使用边界。

进入下一步条件：

- 关键原始表和字段已确认，后续代码不靠猜字段。

### Step 4：交易日历 `daily_calendar`

实施内容：

- 基于 `trade_cal` 生成标准交易日历。
- 支持判断是否交易日、上一个交易日、下一个交易日、偏移 N 个交易日。
- 支持公告日映射到财务可用日。

验证标准：

- 周末、节假日、月末、年末处理正确。
- 任意公告日能映射到下一个可用交易日。
- 越界日期给出清晰错误。
- 输出主键 `trade_date` 唯一。

进入下一步条件：

- 所有后续日期逻辑统一依赖该交易日历。

### Step 5：复权价格与收益率 `daily_adjusted_price`

实施内容：

- 从日频行情及复权因子生成复权价格。
- 保留原始价格，不覆盖原始成交价格。
- 输出常用收益率，例如 `ret_1d`。

验证标准：

- 原始 OHLC 字段与复权 OHLC 字段同时可追溯。
- 复权因子变化日前后的价格和收益率可手工核对。
- 收益率不跨越不存在行情的日期错误计算。
- 输出主键 `trade_date + ts_code` 唯一。

进入下一步条件：

- 研究用价格和交易用原始价格职责分离。

### Step 6：每日证券状态 `daily_stock_state`

实施内容：

- 基于 `stock_basic` 及可用状态表生成每日证券身份状态。
- 字段包括上市状态、退市状态、ST、*ST、退市整理期、上市交易日数、交易所、板块等。
- 历史退市股在退市前仍必须保留。

验证标准：

- 后来退市股票在退市前正常出现在状态表中。
- ST 只在历史 ST 区间标记，不用当前 ST 状态回溯。
- 新股上市天数按历史交易日累计。
- 退市后不再标记为正常上市。
- 增加未来状态记录，不改变过去日期的状态结果。

进入下一步条件：

- 证券身份状态满足 PIT 要求，无幸存者偏差。

### Step 7：每日买卖可行性 `daily_trade_status`

实施内容：

- 基于行情、停牌、涨跌停、上市状态生成买卖方向状态。
- 字段包括 `is_suspended`、`is_limit_up`、`is_limit_down`、`can_buy`、`can_sell`、不可买原因、不可卖原因。
- `can_buy` 和 `can_sell` 分开判断。

验证标准：

- 停牌：不可买、不可卖。
- 涨停：默认不可买，但不必默认不可卖。
- 跌停：默认不可卖，但不必默认不可买。
- 未上市、已退市：不可买、不可卖。
- 状态事实与交易结论可追溯到上游表。

进入下一步条件：

- 后续回测可以直接查询某日某股票是否可买、可卖。

### Step 8：财务公告可见性 `financial_available`

实施内容：

- 处理 `income_vip`、`balancesheet_vip`、`cashflow_vip`、`fina_indicator` 等财务表。
- 保留报告期、公告日、可用日。
- 可用日默认为公告日后的下一个交易日。
- 同一股票同一报告期多条记录时，保留可追溯版本字段。

验证标准：

- 财务数据不会按报告期提前可见。
- 周末或非交易日公告能映射到正确交易日。
- 任意研究日只能看到 `available_date <= trade_date` 的财务记录。
- 输出保留 `end_date`、`ann_date`、`available_date`。

进入下一步条件：

- 财务 PIT 的基本未来函数风险被控制。

### Step 9：单季度与 TTM 财务

实施内容：

- 将累计口径财务字段转换为单季度口径。
- 基于最近四个季度计算 TTM。
- 对缺失季度、重复季度、异常报告期给出明确处理规则。

验证标准：

- Q1 不做差分，Q2/Q3/Q4 正确减去前期累计值。
- 跨年季度计算正确。
- TTM 必须能追溯到四个来源季度。
- 缺失季度不静默生成错误 TTM。

进入下一步条件：

- 基本面因子可使用统一财务口径。

### Step 10：研究样本空间 `daily_research_universe`

实施内容：

- 基于 `daily_stock_state`、`daily_trade_status`、必要的流动性字段生成通用研究样本。
- 样本空间规则配置化，例如是否排除 ST、是否排除北交所、最小上市天数。
- 不在此步骤计算因子、排名、股票池或回测信号。

验证标准：

- `all_a` 保留历史可见股票，不剔除后来退市股票。
- `all_a_ex_st_ipo120` 只排除当日 ST 和当日上市未满 120 个交易日的股票。
- 修改未来日期状态，不改变过去样本。
- 每个 `universe_id` 都能追溯到对应配置。
- 输出主键 `trade_date + ts_code + universe_id` 唯一。

进入下一步条件：

- 后续因子研究可以直接按日期读取可信研究样本。

### Step 11：数据质量检查与阶段验收

实施内容：

- 对 L0/L1/L2 关键输出做统一质量检查。
- 检查主键重复、缺失率、日期范围、字段类型、未来数据、幸存者偏差样例。
- 生成可读报告。

验证标准：

- 每张核心输出表主键唯一。
- 日期覆盖范围符合输入数据范围。
- 财务记录满足 `available_date >= ann_date`，且研究查询不使用未来数据。
- 历史退市股票在退市前仍存在于状态表和可选样本中。
- 同一输入、同一配置重复运行结果一致。
- 检查失败时能指出表名、字段、日期和股票代码。

进入下一阶段条件：

- Step 0 到 Step 11 全部通过。
- 数据处理层可在本地全量数据上完整跑通。
- 服务器可用真实 `/data/tushare_data` 至少跑通一个小日期区间。
- 输出数据足以支持第二阶段因子研究。

## 6. 第一阶段最终交付物

代码与配置：

- `configs/paths.yaml`
- `configs/tushare_tables.yaml`
- `configs/data_middle_layer.yaml`
- `scripts/profile_data.py`
- `scripts/build_l0.py`
- `scripts/build_l1.py`
- `scripts/build_l2.py`
- `scripts/check_data_quality.py`
- `src/core/` 下的数据、日历、行情、证券状态、财务、质量检查模块

测试与验证：

- `tests/fixtures/` 黄金测试数据
- PIT 财务测试
- 退市与 ST 历史状态测试
- 停牌、涨停、跌停买卖可行性测试
- 复权与收益率测试
- 研究样本空间测试

数据输出：

- `data_middle/l0/` 数据画像和必要轻标准化结果
- `data_middle/l1/daily_calendar/`
- `data_middle/l1/daily_adjusted_price/`
- `data_middle/l1/daily_stock_state/`
- `data_middle/l1/daily_trade_status/`
- `data_middle/l1/financial_available/`
- `data_middle/l1/quarterly_financial/`
- `data_middle/l1/ttm_financial/`
- `data_middle/l2/daily_research_universe/`
- `data_middle/l2/research_universe_config/`
- `data_middle/l2/data_quality_report/`

## 7. 暂不实现内容

第一阶段暂不实现：

- 因子计算与因子评价。
- 股票池构建。
- 策略信号。
- 组合管理。
- 交易撮合。
- 回测绩效。
- 实盘接口。
- 数据快照版本系统。
- Web 管理界面。

这些内容进入第二、第三阶段后再按独立方案实施。

## 8. 判断是否可以进入第二阶段

只有同时满足以下条件，才进入因子研究阶段：

1. 全量真实数据已能在本地读取和画像。
2. 黄金测试数据覆盖主要边界场景。
3. 交易日历、复权价格、证券状态、买卖可行性、财务 PIT、研究样本空间全部通过测试。
4. 退市股票不会在历史正常上市期间被错误删除。
5. ST、新股、停牌、涨跌停都按历史日期标注。
6. 财务数据不会按报告期提前使用。
7. 所有核心派生表主键唯一、配置可追溯、重复运行结果一致。
8. 服务器真实数据至少完成一次小区间构建验证。

优先保证正确性、可解释性、无未来数据和无幸存者偏差，再考虑性能优化。
