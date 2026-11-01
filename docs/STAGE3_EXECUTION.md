# 第三阶段（策略研究）执行记录

方案：`STRATEGY_RESEARCH_STAGE3_PLAN.md`。按顺序验收，不修改阶段 1/2 产物。

## Step 0：授权、冻结契约与保护边界（2026-10-31）

- 用户授权 Step 0/1；采用计划第16节推荐默认值。真实数据读取、OOS、联网、R2 和实盘均未授权。
- `configs/strategy_research.yaml` 冻结信号、调仓、约束、执行、R1 账务、成本、区间与晋级阈值，并绑定计划哈希。
- `configs/strategy_registry.yaml` 登记 2 个可晋级候选、5 个诊断和 2 个被动对照。
- `configs/stage3_dependencies.yaml` 复用既有依赖；Step 4 求解器未选定，不新增包。
- `src/core/strategy/contract.py` 实现契约校验、实际标签终点权限、R2 阻断、源码静态隔离和哈希保护。
- `configs/stage3_protected_hashes.json` 保护原始数据、阶段 1/2 产物与源码配置。初次检查发现新增测试文件被误纳入，仅移除该新文件；其余 **117,724** 个原有哈希保持不变，最终 0 差异。生成器拒绝覆盖既有保护基线。
- `scripts/check_stage3_contract.py` 仅检查配置、验收元数据、静态隔离和保护哈希，不写 `data_middle/`。

### 验收

- 候选、方向、成本、约束和 R1/R2 边界冻结；R2 缺公司行动等输入时阻断。
- 未查询真实行情、因子或标签 parquet，未运行阶段 1/2 构建或质量报告刷新。
- 真实输入状态仍为 `blocked_input_scope`；Step 2 需另行获准的样本内快照或有界读取证明。
- `engineering_passed=false`、`live_ready=false`；Step 0 通过不代表策略或回测已通过。

## Step 1：独立人工黄金夹具（2026-10-31）

### 实施

- `tests/fixtures/golden_stage3/` 新建 12 只虚构 SA 股票、83 个交易日的 L1/L2/L3 形态 CSV，不依赖真实数据。
- `generate.py` 只生成虚构输入、独立闭式/手算预期及摘要，不实现信号、组合、执行、账务或评价生产算法。
- `expected.json` 与 `scenario_registry.json` 登记固定信号、缓冲、财务/行业/成分 PIT、异常成交、费用、现金、T+1、复权单位、约束降级、净值归因、权限和研究预算案例。
- `fixture_config.yaml` 显式缩小夹具门槛，不改变生产 300 截面/100 持仓等默认值。
- `forward_return_research_only.csv` 含一条虚构越界标签，专门验证 entry/exit 越界拒绝，不属于信号白名单。
- `tests/test_golden_stage3_fixture.py` 检查深度主键、哈希、闭式结果、输入事实、权限拒绝和两个独立目录逐文件一致。
- 调试中修正夹具自身的缓冲排序、行业日期、上市年龄、节假日与冻结仓位手算错误；没有改动冻结投资参数。

### 验收

- 全部输入为虚构数据，深度主键无重复/空值，manifest 覆盖 CSV、预期和场景登记。
- 后续 Step 3–6 的异常场景有独立预期；这些是待实现验收，不宣称策略算法已完成。
- 夹具双目录生成业务文件逐字节一致；不等同于未来完整回测双目录验收。

## Step 0/1 回归与边界

- 全量单元测试 **276 项通过**（原 249 项 + 新增 27 项）；测试写入 pytest 临时目录。
- Step 0 契约/保护检查通过；阶段 1/2 保护文件 0 差异。
- Step 0/1 未访问锁定样本外真实数据，未联网，未修改旧产物，未产生策略收益或样本外结论。

## Step 2：物理样本内快照与输入准入（2026-11-01）

### 实施

- 用户授权按物理样本内快照方案实施 Step 2。独立准备流程读取必要的现有 L1–L4 上游文件；为物理切片可以扫描含全期日期的上游文件，但不生成、不评价样本外绩效，不访问原始表，不联网，不改上游。
- `configs/strategy_inputs.yaml` 冻结 15 张输入表、深度主键、日期字段、研究域和单位假设。金额沿用千元→元研究假设，`vol` 单位仍未验证，市值单位为万元，原价/复权价分开。
- `src/core/strategy/inputs.py` 实现快照准备、不可变 bundle、哈希校验、受限日历和信号/研究域访问控制。日历保留原 `trade_index`，2024-12-31 的下一交易日链接为空。
- `src/core/strategy/admission.py` 独立复验 schema 所需字段、深度主键、日期上界、财务可见性、实际标签终点、合成权重实现水位和因子注册覆盖。
- `scripts/prepare_stage3_inputs.py`、`scripts/check_stage3_inputs.py` 提供准备与只读准入入口；准备失败清理暂存，不完整 bundle 不可读取。

### 快照与异常处理

- 已发布 `data_middle/strategy_research/inputs/in_sample_65efe5cadd765004/`，状态 `complete`，约 4.4 GB，其中 `signal_inputs` 约 3.7 GB、`research_only` 约 720 MB。
- 15 张表、1699 个获准交易日；`factor_value` 和 `factor_processed` 均覆盖注册表 20 个因子。
- 初次准备发现样本内信号的实际标签入场/退出可能落在 2025 年，流程阻断且未发布。随后改为只隔离研究域中这些标签：收益、实际入场/退出日期及价格置空，标记 `input_scope_status=quarantined`。
- 最终隔离 **408,427** 条跨界标签；隔离行保留收益/日期/价格为 0 条。未把样本外信号行复制进快照，未使用跨界标签训练或评价。
- `manifest.json` 保存精确上游文件哈希、逐表内容哈希、行数、主键、schema、日期范围和 mixed 血缘接纳；`admission.json` 保存独立复验结果并反向绑定 manifest。

### 验收

- 真实快照独立准入通过：越界日期 0、财务生效日晚于可见日 0、非空标签缺实际日期 0、未实现合成权重 0、主键异常 0。
- 人工完整快照与损坏哈希反例通过；CSV 空字符串和 Parquet 日期统一按空值/DATE 处理。
- `configs/strategy_research.yaml` 仅追加 Step 2 准备授权和 admitted bundle 身份；投资、信号、费用、候选和 OOS 冻结规则不变。
- 全量单元测试 **284 项通过**（Step 0/1 的 276 项 + Step 2 新增 8 项）；新增代码诊断 0。
- 阶段 1/2 保护文件 **117,724** 个、差异 0；未重跑或改写阶段 2 质量门禁。

### 边界与下一步

- Step 2 通过表示后续可以使用该固定快照开展 Step 3，不表示策略、回测或阶段 3 整体门禁通过。
- 行业 PIT 仍 limited、官方指数核验仍 suspended、涨跌停覆盖仍有限；金额单位是研究假设、成交量单位未验证、公司行动账未具备。
- R2 和实盘继续阻断，`live_ready=false`。下一步为 Step 3 股票资格和策略信号，尚未实施。

## Step 3：股票资格、风险特征与固定信号（2026-11-01）

### 实施

- 仅消费 `in_sample_65efe5cadd765004` 的 `signal_inputs`，未读取研究标签。新增 `src/core/strategy/signals.py` 和 `scripts/build_strategy_signals.py`。
- 资格顺序：基础池→当日行情/市值→20 个统一交易日成交额窗口→规模底部 10% 过滤→当日交易/风险/上市年龄状态→四因子共同覆盖→截面及退化门禁。
- 主分数严格为价值 `(bp+sp_ttm)/2`、反转 `-rev_20`、低波 `-vol_20` 的等权平均；缺失不重分配，按分数降序、代码升序排名。
- L3 产物：`strategy_universe`、`strategy_feature`、`strategy_signal`、`strategy_signal_daily`，均有独立深度主键、schema、行数、内容哈希和 run manifest。
- 风险特征包含 ADV20、规模、行业 limited 标记和 20 日收益波动率。波动率使用 1e-12 定点收益及整数窗口矩，避免全量/截断浮点累计尾差；不是新增投资参数。

### 产物

- 最终 run：`data_middle/strategy_research/runs/step3_cd3844b567612ae5/`，约 785 MB。
- 三张股票日级表各 **6,493,254** 行；日级统计 **1,580** 天，其中 **1,380** 天通过截面门禁。
- 其他资格股票数日度范围 1,906–4,418；平均四因子共同覆盖率约 **85.46%**。这些是样本与信号统计，不是收益或回测结果。
- 独立复验：分数公式、缺因子晋级、排名连续性、日级门禁、资格一致性和 OOS 行数异常均为 **0**。
- 2020-04-30 真实截断重建：四张表逐值差异均为 **0**。
- 独立目录 `verification/step3_cd3844b567612ae5_repeat/` 完整重建：四张表 SHA-256 全部一致。
- 验证证据：`runs/step3_cd3844b567612ae5/verification/step3_checks.json`，状态通过。

### 失败记录与回归

- `step3_ad7e4302296be485` 初次真实截断发现 20 日波动率约 1e-17 的浮点尾差；资格和信号无差异。失败记录保留。
- `step3_78dd1f18e92fa5fa` 固定小数后仍有一条舍入边界差异，继续保留失败记录；最终改为整数窗口矩后通过，未放宽验收容差。
- 新增 Step 3 测试 5 项，覆盖固定分数、资格/行业 PIT、退化截面、逐表截断和夹具双目录字节一致。
- 全量回归 **292 项通过**，新增代码诊断 0；该数字同时包含工作区另一项阶段 2 编排工作的 3 项测试。

### 外部变更与边界

- 最终仓库级保护检查发现 `configs/factor_research.yaml`、`docs/STAGE2_EXECUTION.md`、`src/core/quality/stage2.py` 的哈希不同于 Step 0 基线。当前内容显示为另一项阶段 2 一键重建/分阶段部署工作，并新增 `scripts/build_stage2.py`、`core.pipeline.stage2` 和测试；Step 3 未写入这些文件。
- 未覆盖、未回滚这些外部变更，也未刷新保护基线。**Step 3 自身规则/截断/确定性验收已通过，但仓库级保护检查仍需单独确认上述外部变更后才能恢复通过。**
- 最终信号 run 使用固定快照，不受后续上游文件变化影响。行业 PIT、成交量单位和公司行动边界不变，R2、正式 OOS 和实盘继续阻断。
- 下一步为 Step 4 目标组合与订单，尚未实施。
