# QuantKernel

面向 A 股量化研究的可复现数据、因子研究与策略研究框架。

QuantKernel 将 TuShare 原始数据转换为带历史时点（PIT）约束的研究数据，并在此基础上提供因子实验、隔离评价、审核入库、策略组合、代理执行、R1 账户和完整验证证据。原始数据始终只读，研究结果与权限边界均可追溯。

> 当前交付范围为 Stage 1 数据处理、Stage 2 因子研究和 Stage 3 R1 策略研究。R2 真实现金—股数回测、正式 OOS 和实盘交易未开放。

## 项目概览

<!-- 项目概览图预留：后续将 AI 绘制的图片保存为 assets/quantkernel-overview.png，并在此处插入图片。 -->

```text
TuShare 原始数据（只读）
        │
        ▼
Stage 1：数据处理
L0 数据画像 → L1 日历/行情/证券状态/交易状态/财务 → L2 每日研究股票池
        │
        ▼
Stage 2：因子研究
因子计算 → 预处理 → 前向标签 → IC/RankIC/分组评价 → 相关性与合成 → 审核入库
        │
        ▼
Stage 3：策略研究
样本内快照 → 资格与信号 → 目标组合与订单 → 代理执行与 R1 账户 → D/V 评价与证据收口
```

每个正式产物都配套 manifest，记录 Schema、主键、行数、内容哈希、代码配置及上游血缘。实验和回测不会只保留一份人工结论，而是保留可独立复核的运行证据。

## 核心特性

- **原始数据只读**：原始目录与各阶段输出目录相互隔离，不覆盖供应商数据。
- **历史时点约束**：财务公告、行业、指数成分和标签均有可见性规则，未知不猜填。
- **不可变研究身份**：输入、代码、配置、股票池和结果通过哈希及 manifest 绑定。
- **独立因子实验**：新因子无需先进入正式库，实验评价与正式 L3/L4 产物隔离。
- **自定义股票池**：支持 CSV/Parquet 或同一 DuckDB 连接中的内存 relation，无需中间文件。
- **策略权限分层**：R1、R2、确认段、正式 OOS 和实盘分别授权，工程通过不自动放开权限。
- **失败也可追溯**：拒单、不可行组合、空结果、未晋级和越权阻断均有明确记录。

## 项目结构

```text
QuantKernel/
├── assets/                         # 项目图片等静态资源
├── configs/                        # 路径、数据表、因子、策略及权限配置
├── docs/
│   ├── specs/                      # 当前长期技术规范
│   ├── DATA_SOURCE_NOTES.md        # 原始数据覆盖、异常与缺源边界
│   └── STAGE*_ACCEPTANCE.md        # 各阶段历史验收摘要
├── research/factors/               # 每个因子的独立 SPEC 与 compute.py
├── scripts/                        # 数据构建、实验、回测和检查入口
├── src/core/                       # 数据、因子、评价、组合、执行与账户实现
├── tests/                          # 单元测试与小型人工黄金夹具
│
├── data/                           # 本地 TuShare 原始数据，只读，不提交
├── data_middle/                    # Stage 1 L0/L1/L2 产物，不提交
├── factor_research/                # 因子实验、正式 L1/L3/L4 与证据，不提交
├── strategy_research/              # 策略快照、预注册、运行与证据，不提交
├── verification/                   # Stage 1 与框架验证证据，不提交
│
├── pyproject.toml                  # 项目与测试配置
├── requirements-lock.txt           # 已验证依赖版本
└── TODO.md                         # 当前真实待办
```

策略运行按 `strategy_research/runs/<strategy_id>/stepN/` 分组；因子实验按 `factor_research/experiments/<factor_id>/<experiment_id>/` 隔离。生成的数据目录均由 `.gitignore` 排除。

## 快速开始

### 1. 安装

需要 Python 3.10+，推荐使用 Conda 管理环境。当前锁文件验证环境为 Python 3.13。

```bash
conda create -n quantkernel python=3.13 -y
conda activate quantkernel
python -m pip install -r requirements-lock.txt
python -m pip install -e . --no-deps
```

如需在其他 Python 版本重新解析依赖，可在创建环境时调整版本，然后使用：

```bash
python -m pip install -e ".[test]"
```

### 2. 配置路径

默认读取 `./data`，各阶段分别写入 `./data_middle`、`./factor_research`、`./strategy_research` 和 `./verification`。路径 profile 见 [configs/paths.yaml](configs/paths.yaml)。

可用同名环境变量覆盖：

```bash
export DATA_ROOT=/path/to/tushare_data
export MIDDLE_ROOT=/path/to/data_middle
export FACTOR_RESEARCH_ROOT=/path/to/factor_research
export STRATEGY_RESEARCH_ROOT=/path/to/strategy_research
export VERIFICATION_ROOT=/path/to/verification

python scripts/check_paths.py
```

五个根目录必须互不包含，防止一个阶段覆盖另一个阶段的输入或产物。

## Stage 1：构建研究数据

```bash
# 只读核验原始文件及内容哈希
python scripts/verify_raw_data.py --checksums

# 完整构建并记录首次运行
python scripts/build_stage1.py --record-run first

# 复用未变化的 L0 画像，独立重建并比较
python scripts/build_stage1.py --reuse-l0 --record-run repeat
python scripts/compare_stage1_runs.py first repeat

# 统一质量检查
python scripts/check_data_quality.py --l0-scope full
```

主要产物：

| 层级 | 内容 |
|---|---|
| L0 | 来源画像、Schema、行数、覆盖、异常与源文件签名 |
| L1 | 交易日历、复权行情、证券状态、交易状态、财务可见记录、单季和 TTM |
| L2 | 默认及自定义每日研究股票池、数据质量报告 |

真实数据覆盖和缺源边界见 [数据源说明](docs/DATA_SOURCE_NOTES.md)。

## Stage 2：进行因子研究

### 标准实验流程

```text
编写 compute.py → 计算并冻结实验 → 隔离评价 → 审核入库 → 正式重建
```

1. 在 `research/factors/my_factor/compute.py` 中定义 `SPEC` 和 `compute()`。
2. 运行实验，自动保存代码、配置、因子值和 manifest。
3. 在实验目录内评价，不修改正式产物。
4. 技术证据和准入策略通过后，显式批准入库。
5. 使用同一逐因子源码重建正式 L3/L4，而不是复制实验结果。

```bash
# 1. 计算因子值；end 会触发截断一致性检查
python scripts/run_factor_experiment.py my_factor --start 20200101 --end 20241231

# 2. 隔离评价
python scripts/evaluate_factor.py \
  factor_research/experiments/my_factor/<experiment_id>/factor_values.parquet

# 3. 审核后入库；当前只允许新增 factor_id
python scripts/promote_factor.py \
  factor_research/experiments/my_factor/<experiment_id> --approve

# 4. 正式重建与质量门
python scripts/build_stage2.py --verification standard
python scripts/check_stage2_quality.py --verification standard
```

实验目录包含 `experiment.yaml`、代码与配置快照、`factor_values.parquet`、`summary.json`、`report.md` 和 manifest。评价覆盖预处理、IC/RankIC、ICIR、Newey-West 显著性、非重叠分组、换手、相关性和绩效诊断。

### 自定义股票池

成员文件至少包含 `trade_date` 和 `ts_code`：

```bash
python scripts/build_custom_universe.py members.csv --universe-id my_pool_v1

python scripts/evaluate_factor.py \
  factor_research/experiments/my_factor/<experiment_id>/factor_values.parquet \
  --universe my_pool_v1
```

也可以在同一 `Store`/DuckDB 连接中调用 `build_custom_research_universe(store, relation="my_members", universe_id="my_pool_v1")`。文件与 relation 共用校验和发布路径；同一 ID 内容发生变化会被拒绝。

自定义评价写入 `evaluations/<universe_id>/`，不会覆盖默认评价或正式产物。

## Stage 3：进行策略研究

> 当前仅支持 R1 研究近似回测：`adjusted_unit_proxy` 复权研究单位 + `proxy_not_observed` 代理成交。R1 结果不等于真实股数账户、实际成交收益或实盘能力。

### 研究与验收顺序

```text
合约检查
  → 样本内快照与准入
  → 资格和信号
  → 目标组合与订单
  → 执行与 R1 账户
  → 被动基准与真实小窗
  → 设计段 D
  → 预注册
  → 验证段 V
  → 报告与晋级判断
  → R2 负质量门
  → 敏感性、稳健性与收口
```

主要入口如下；各构建环节配有相应的 `check_*.py` 验证脚本，预注册冻结和合约检查等独立入口除外：

| 环节 | 构建或检查入口 | 关键产物/约束 |
|---|---|---|
| 合约与权限 | `scripts/check_stage3_contract.py` | YAML、注册表、权限和保护文件 |
| 输入快照 | `scripts/prepare_stage3_inputs.py` | 不可变样本内 bundle 与 admission |
| 资格与信号 | `scripts/build_strategy_signals.py` | 每日资格、风险特征和固定分数 |
| 组合与订单 | `scripts/build_strategy_portfolio.py` | 约束投影、不可行降级和订单意图 |
| 执行与账户 | `scripts/build_strategy_execution.py` | 代理成交、批次、现金和 R1 NAV |
| 基准与小窗 | `scripts/build_strategy_benchmark.py` | 理想/执行、毛/净账户路径 |
| 设计段 D | `scripts/build_strategy_research.py` | 候选、绩效、相对表现和 trial |
| 验证前冻结 | `scripts/freeze_strategy_candidate.py` | 不可变预注册，必须先于 V 评价 |
| 验证段 V | `scripts/build_strategy_validation.py` | 晋级门禁，不读取 C/OOS |
| 报告与证据 | `scripts/build_strategy_reports.py` | 报告、晋级决策和证据索引 |
| R2 质量门 | `scripts/build_r2_quality_gate.py` | 负质量门，不是 R2 回测 |
| 敏感性 | `scripts/build_strategy_sensitivity.py` | 成本、波动率和容量诊断 |
| 稳健性 | `scripts/build_strategy_robustness.py` | 截断、未来扰动、PIT 与压力 |
| R1 收口 | `scripts/build_strategy_closeout.py` | 活动证据对账和最终状态 |

这些入口由 [configs/strategy_research.yaml](configs/strategy_research.yaml) 和 [configs/strategy_registry.yaml](configs/strategy_registry.yaml) 驱动，**不是可以无条件连续执行的一键命令**。新候选、输入快照、预注册、不可变目录和授权范围必须一致；不得通过覆盖历史 run、反复查看 V/C/OOS 或修改门槛来制造晋级。

当前 R1 已完成收口，但最终为 `no_champion_no_promotion`。R2、Step 11、2024 确认段、正式 OOS 和实盘仍关闭。

## 验证与测试

```bash
# 完整测试
python -m pytest -q

# Stage 2 标准质量门
python scripts/check_stage2_quality.py --verification standard

# Stage 3 合约、权限和保护文件
python scripts/check_stage3_contract.py
```

| 验证级别 | 用途 |
|---|---|
| `fast` | 本地开发迭代，保留业务语义检查，减少文件和重复构建开销 |
| `standard` | 默认研究模式，复用既有独立重复性证据，不伪造缺失证据 |
| `audit` | 发布或冻结验收，执行完整哈希、深度主键和独立重复性检查 |

Stage 1/2 CLI 使用 `--verification`；Stage 3 检查入口读取 `QUANTKERNEL_VERIFICATION`。`repeat_status=not_requested` 只表示本轮未请求独立重复，不表示重复性已经通过。

## 文档导航

| 文档 | 用途 |
|---|---|
| [Stage 1 技术规范](docs/specs/STAGE1_DATA_PROCESSING.md) | 数据分层、PIT、交易状态、股票池和质量规则 |
| [Stage 2 技术规范](docs/specs/STAGE2_FACTOR_RESEARCH.md) | 因子实验、评价、入库与 OOS 纪律 |
| [Stage 3 技术规范](docs/specs/STAGE3_STRATEGY_RESEARCH.md) | 信号、组合、执行、R1/R2 和研究权限 |
| [数据源说明](docs/DATA_SOURCE_NOTES.md) | 原始数据真实覆盖、异常与缺源边界 |
| [Stage 1 验收摘要](docs/STAGE1_ACCEPTANCE.md) | 数据框架历史验收 |
| [Stage 2 验收摘要](docs/STAGE2_ACCEPTANCE.md) | 因子框架历史验收 |
| [Stage 3 验收摘要](docs/STAGE3_ACCEPTANCE.md) | 策略研究、无冠军结论与权限历史 |
| [TODO](TODO.md) | 当前真实待办；权限和能力边界不冒充普通任务 |

## GitHub 发布说明

仓库只应提交代码、配置、文档、测试和小型人工夹具。原始数据、Parquet/DuckDB、实验产物、回测结果、缓存和本地环境均不提交。

```bash
git status --short --ignored
git check-ignore -v data data_middle factor_research strategy_research verification
```

上述五个数据根目录应被 Git 忽略。研究结论的可追溯性由本地 manifest 和运行证据保证，主页及验收文档只保留必要摘要。
