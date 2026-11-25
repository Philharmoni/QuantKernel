"""Step 14 R1 closeout status and deterministic handoff reporting."""
from pathlib import Path


REQUIRED_STEPS = (3, 4, 5, 6, 7, 8, 9, 10, 12, 13)


def step_evidence_errors(step, active_run, manifest, verification):
    errors = []
    if manifest.get("step") != step or verification.get("step") != step:
        errors.append("step_identity")
    expected_name = Path(str(active_run)).name

    def matches(value):
        name = Path(str(value)).name
        return name == expected_name or name.startswith(expected_name + "_")

    if not matches(manifest.get("run_id")) or not matches(verification.get("run_id")):
        errors.append("run_identity")
    if verification.get("passed") is not True or verification.get("failures"):
        errors.append("verification_not_passed")
    registered = manifest.get("verification", {})
    if registered.get("passed") is not True:
        errors.append("manifest_verification_not_passed")
    for container in (manifest, verification):
        if container.get("oos_accessed") is True or container.get("confirmation_seen") is True:
            errors.append("forbidden_scope")
        if container.get("research_labels_read") is True:
            errors.append("research_labels_read")
    return sorted(set(errors))


def closeout_status(decision, r2_gate, evidence_passed):
    economic_promoted = (decision.get("decision") != "no_champion_no_promotion"
                         and decision.get("economic_promoted") is True)
    r2_permitted = (economic_promoted and r2_gate.get("passed") is True
                    and r2_gate.get("r2_backtest_permitted") is True)
    return {"r1_research_complete": bool(evidence_passed), "engineering_passed": bool(evidence_passed),
            "promotion_status": decision.get("decision"), "economic_promoted": economic_promoted,
            "r2_quality_gate_passed": r2_gate.get("passed") is True,
            "r2_backtest_permitted": r2_permitted,
            "can_enter_step11": r2_permitted and r2_gate.get("can_enter_step11") is True,
            "real_share_account_ready": False, "live_ready": False,
            "confirmation_seen": False, "oos_accessed": False}


def pct(value):
    return f"{100 * float(value):.4f}%"


def closeout_report(result, evidence, gate_matrix):
    lines = ["# Stage 3 R1 最终收口与交接报告", "",
        "**结论：R1 工程与证据收口完成，但无经济冠军、不晋级；R2、Step 11、确认段和正式 OOS 均未开放。**", "",
        "## 研究范围与最终状态", "",
        "- 已评价设计段 2019–2021 和验证段 2022–2023；未读取 2024 确认段或 2025+ 正式 OOS。",
        "- 账务仅为 `adjusted_unit_proxy`，成交仅为 `proxy_not_observed`；不是实际成交或真实股数账。",
        f"- 最终决策：`{result['promotion_status']}`；经济晋级：`{result['economic_promoted']}`。",
        f"- R2 质量门：`{result['r2_quality_gate_passed']}`；R2 回测获准：`{result['r2_backtest_permitted']}`。", "",
        "## 保留的经济结果", "", "| 区间 | C0 受限净账累计收益 | 年化收益 | 最大回撤 |", "|---|---:|---:|---:|"]
    for period, title in (("design", "设计段"), ("validation", "验证段")):
        row = result["key_results"][period]
        lines.append(f"| {title} | {pct(row['cumulative_return'])} | {pct(row['annualized_return'])} | {pct(row['max_drawdown'])} |")
    lines += ["", "验证段相对跑赢不能覆盖设计段失败，也不能抵消绝对收益、回撤和仓位质量失败；未改选候选或调整门槛。", "",
        "## 工程证据", "", "| Step | Run | 验收 |", "|---:|---|---|"]
    for step in REQUIRED_STEPS:
        entry = evidence["runs"][str(step)]
        lines.append(f"| {step} | `{entry['run_id']}` | passed |")
    lines += ["", f"- 受保护文件：{result['protected_files']['checked_files']} 个，差异 {len(result['protected_files']['differences'])}。",
        f"- 静态架构违规：{len(result['architecture_violations'])}。",
        f"- Step 4 固定降级：成功求解 {result['portfolio']['solved_days']} 天，不可行保持仓位 {result['portfolio']['infeasible_days']} 天。",
        f"- Step 13 真实截断/未来扰动前缀差异：{result['robustness']['truncation_prefix_mismatch']}/{result['robustness']['future_prefix_mismatch']}。", "",
        "## 十二组门禁矩阵", "", "本矩阵是对既有不可变验收证据的收口核对，不冒充重新运行全部历史回测。", "",
        "| 组 | 状态 | 主要证据 |", "|---|---|---|"]
    for row in gate_matrix:
        lines.append(f"| {row['gate_id']} | {'通过' if row['passed'] else '失败'} | {row['evidence']} |")
    lines += ["", "## 后续阻断与交接", "",
        "Step 4 大量调仓日不可行、行业 PIT 有限、成交量单位未验证、公司行动/整手/结算/公告/退市映射不完整。",
        "任何 R2 或正式 OOS 工作都必须另行授权并先满足对应质量门；不得用本 R1 收口结果替代。", ""]
    return "\n".join(lines)
