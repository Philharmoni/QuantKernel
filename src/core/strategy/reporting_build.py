"""Step 9 deterministic promotion reports and evidence package."""
import json
from pathlib import Path

from core.strategy.benchmark_build import fetch_dicts
from core.strategy.contract import canonical_hash, file_sha256


def pct(value):
    return "null" if value is None else f"{100 * float(value):.4f}%"


def number(value):
    return "null" if value is None else f"{float(value):.4f}"


class Step9Builder:
    def __init__(self, inputs, config, project_root, run_roots):
        self.inputs = inputs
        self.db = inputs.db
        self.config = config
        self.root = Path(project_root)
        self.run_roots = {int(step): Path(path) for step, path in run_roots.items()}
        self.step7 = self.run_roots[7]
        self.step8 = self.run_roots[8]

    def rows(self, run_root, table, where=""):
        return fetch_dicts(self.db, f"SELECT * FROM read_parquet(?) {where}",
                           [str(Path(run_root) / f"l5/{table}/part-00000.parquet")])

    def build(self):
        d_perf = self.rows(self.step7, "performance", "WHERE period_id='design'")
        v_perf = self.rows(self.step8, "performance", "WHERE period_id='validation'")
        d_relative = self.rows(self.step7, "relative_performance")
        v_relative = self.rows(self.step8, "relative_performance")
        d_execution = self.rows(self.step7, "execution_summary")[0]
        v_execution = self.rows(self.step8, "execution_summary")[0]
        d_exposure = self.rows(self.step7, "exposure_daily")
        v_exposure = self.rows(self.step8, "exposure_daily")
        gate = self.rows(self.step8, "promotion_gate")[0]
        freeze = self.rows(self.step8, "candidate_freeze")[0]
        accounts = ["c0_three_module_fixed__ideal_target_gross", "c0_three_module_fixed__executed_proxy_gross",
                    "c0_three_module_fixed__executed_proxy_net", "eligible_passive__executed_proxy_net",
                    "common_support_passive__executed_proxy_net"]
        metrics = {"candidate_id": "c0_three_module_fixed", "design": {}, "validation": {},
                   "relative": {"design": d_relative, "validation": v_relative},
                   "execution": {"design": d_execution, "validation": v_execution},
                   "risk": {"design": self.risk_summary(d_exposure), "validation": self.risk_summary(v_exposure)},
                   "promotion_status": gate["promotion_status"], "research_verification_passed": gate["research_verification_passed"],
                   "economic_promoted": False, "real_share_account_ready": False, "live_ready": False,
                   "confirmation_seen": False, "oos_accessed": False}
        for period, rows in (("design", d_perf), ("validation", v_perf)):
            for account in accounts:
                row = next(item for item in rows if item["account_id"] == account)
                metrics[period][account] = {key: row[key] for key in
                    ("cumulative_return", "annualized_return", "annualized_volatility", "max_drawdown", "sortino", "calmar")}
        trial_ledger = []
        for step, run_root in ((7, self.step7), (8, self.step8)):
            for row in self.rows(run_root, "research_trial"):
                trial_ledger.append({**row, "source_step": step, "source_run_id": run_root.name,
                    "final_decision": "no_champion_no_promotion" if row["candidate_id"] == "c0_three_module_fixed" else "not_run"})
        reports = {
            "strategy_definition.md": self.definition_report(freeze),
            "performance_report.md": self.performance_report(metrics),
            "risk_report.md": self.risk_report(metrics),
            "execution_report.md": self.execution_report(metrics),
        }
        decision = {"candidate_id": "c0_three_module_fixed", "decision": "no_champion_no_promotion",
            "design_boundary_passed": gate["design_boundary_passed"],
            "validation_boundary_passed": gate["validation_boundary_passed"],
            "failure_reasons": json.loads(gate["failure_reasons_json"]),
            "economic_promoted": False, "r2_backtest_permitted": False,
            "real_share_account_ready": False, "live_ready": False,
            "confirmation_seen": False, "oos_accessed": False}
        return {"metrics": metrics, "reports": reports, "decision": decision, "trial_ledger": trial_ledger}

    @staticmethod
    def risk_summary(rows):
        return {"max_stale_weight": max(float(row["stale_weight"]) for row in rows),
                "max_non_tradable_weight": max(float(row["non_tradable_weight"]) for row in rows),
                "max_cash_weight": max(float(row["cash_weight"]) for row in rows),
                "max_top1_weight": max(float(row["top1_weight"]) for row in rows)}

    def definition_report(self, freeze):
        return f"""# C0 v2 冻结定义与账务边界

- 候选：`c0_three_module_fixed`，version 2；freeze id：`{freeze['freeze_id']}`。
- 信号：价值 `(bp + sp_ttm) / 2`、短期反转 `-rev_20`、低波动 `-return_volatility_20`，三模块等权，缺失不重新归一。
- 组合：前 100 名、95% 股票目标、单股不超过 2%、行业主动偏离不超过 10 个百分点；市值 z 和规模桶仅诊断。
- 执行：D 日未复权 open 代理、双 ADV/全日成交额容量、普通 DAY 订单、风险退出重检、T+1 和卖款延迟释放。
- 数量：按未复权 open 的股数等价量取整；普通股票最低 100 股/100 股递增，688 股票最低 200 股/1 股递增，仅全部可卖持仓允许余股退出。
- 账务：R1 `adjusted_unit_proxy`，允许小数复权单位，费用独立扣现金；数量约束仍属研究假设，不是 R2 真实股数账。
- 预注册 SHA-256：`{freeze['preregistration_sha256']}`；验证区间 2022-01-01 至 2023-12-31。
- 2024 确认段、正式 OOS、R2 和实盘均未开放。

## 工程边界

Step 4 全设计段只有首个调仓日形成新目标，其余日期按固定规则保持参考持仓。报告中的账户结果不是全期正常再平衡的证明。成交额单位和容量上限状态为 `not_validated`，不宣称容量已通过。
"""

    def performance_report(self, metrics):
        lines = ["# C0 v2 设计与验证绩效报告", "", "| 区间 | 账户 | 累计收益 | 年化收益 | 年化波动 | 最大回撤 |",
                 "|---|---|---:|---:|---:|---:|"]
        labels = {"c0_three_module_fixed__ideal_target_gross": "C0 理想毛账",
                  "c0_three_module_fixed__executed_proxy_gross": "C0 受限毛账",
                  "c0_three_module_fixed__executed_proxy_net": "C0 受限净账",
                  "eligible_passive__executed_proxy_net": "主要被动净账",
                  "common_support_passive__executed_proxy_net": "匹配被动净账"}
        for period, title in (("design", "设计段 2019–2021"), ("validation", "验证段 2022–2023")):
            for account, label in labels.items():
                row = metrics[period][account]
                lines.append(f"| {title} | {label} | {pct(row['cumulative_return'])} | {pct(row['annualized_return'])} | "
                             f"{pct(row['annualized_volatility'])} | {pct(row['max_drawdown'])} |")
        lines += ["", "## 相对表现", "", "| 区间 | 被动基准 | 相对净值增长 | 年化 IR |", "|---|---|---:|---:|"]
        for period, title in (("design", "设计段"), ("validation", "验证段")):
            for row in metrics["relative"][period]:
                lines.append(f"| {title} | {row['benchmark_id']} | {pct(row['relative_nav_growth'])} | {number(row['annualized_information_ratio'])} |")
        lines += ["", "## 决策", "", "验证段相对跑赢不能覆盖设计段失败，也不能抵消验证段绝对收益、回撤及仓位质量边界失败。最终为 **no_champion_no_promotion**；未切换候选或调参。", ""]
        return "\n".join(lines)

    def risk_report(self, metrics):
        lines = ["# 风险、失败场景与已知限制", "", "| 区间 | 最大陈旧权重 | 最大不可交易权重 | 最大现金权重 | 最大单股权重 |",
                 "|---|---:|---:|---:|---:|"]
        for period, title in (("design", "设计段"), ("validation", "验证段")):
            row = metrics["risk"][period]
            lines.append(f"| {title} | {pct(row['max_stale_weight'])} | {pct(row['max_non_tradable_weight'])} | "
                         f"{pct(row['max_cash_weight'])} | {pct(row['max_top1_weight'])} |")
        return "\n".join(lines) + """

- 陈旧和不可交易权重边界均为 1%；D/V 均存在超界，不伪装为可正常退出。
- 无报价时沿用最后有效复权标记，60/120 交易日压力折减与参考 NAV 并存；退市不制造成交。
- Step 4 大量调仓日不可行并保持原目标，路径存在长期静态持仓风险。
- 理想、受限毛账和净账是独立路径；顺序归因不等于唯一因果分解。
- R1 未覆盖真实公司行动、整手、完整成交仿真和成交量单位验证；R2 阻断。
"""

    def execution_report(self, metrics):
        lines = ["# 执行、成本与容量报告", "", "| 区间 | 订单 | 成交 | 部分成交 | 拒单 | 金额成交率 | 总成本（元） |",
                 "|---|---:|---:|---:|---:|---:|---:|"]
        for period, title in (("design", "设计段"), ("validation", "验证段")):
            row = metrics["execution"][period]
            lines.append(f"| {title} | {row['order_count']} | {row['fill_count']} | {row['partial_fill_count']} | "
                         f"{row['rejected_order_count']} | {pct(row['amount_fill_rate'])} | {float(row['total_cost_cny']):.2f} |")
        return "\n".join(lines) + """

- 所有成交均为 `proxy_not_observed`，不是实际市场成交回报。
- 买卖数量按未复权 open 的股数等价量执行板块最低数量和递增单位；全部可卖持仓余股退出单独标记，不用小数股数冒充实际申报。
- 容量采用剩余订单、1% ADV20_T 与 1% D 全日成交额的最小值；D 全日成交额仅作上界。
- 拒单、部分成交和剩余量保留；普通订单当日到期，风险退出按状态重发。
- 现金、批次、费用和 NAV 已对账；容量单位状态仍为 `not_validated`，不用于实盘或容量声明。
"""

    def evidence_index(self, report_hashes):
        evidence = {"plan_sha256": self.config["plan_contract_sha256"],
            "input_bundle": self.config["delivery"]["active_input_bundle"],
            "input_manifest_sha256": file_sha256(self.root / self.config["delivery"]["input_bundle_manifest"]),
            "registry_sha256": file_sha256(self.root / "configs/strategy_registry.yaml"),
            "preregistration_sha256": file_sha256(self.root / self.config["step8"]["preregistration"]),
            "reports": report_hashes, "runs": {}}
        for step in range(3, 9):
            run_root = self.run_roots[step]
            verification = list((run_root / "verification").glob(f"step{step}_checks.json"))[0]
            evidence["runs"][str(step)] = {"run_id": run_root.name,
                "manifest_sha256": file_sha256(run_root / "manifest.json"),
                "verification_sha256": file_sha256(verification)}
        evidence.update({"confirmation_seen": False, "oos_accessed": False,
                         "economic_promoted": False, "r2_backtest_permitted": False})
        return evidence
