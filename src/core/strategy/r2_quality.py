"""Step 10 R2 readiness gate; never performs an R2 backtest."""


def evaluate_r2_gate(inventory):
    checks = {
        "r1_evidence_complete": inventory["r1_evidence_complete"],
        "champion_available": inventory["champion_available"],
        "company_action_ledger": inventory["company_action_ledger"] == "available",
        "volume_units_validated": inventory["volume_unit_validated"] is True,
        "board_lot_rules_complete": inventory["board_lot_rules_complete"] is True,
        "settlement_rules_complete": inventory["settlement_rules_complete"] is True,
        "announcement_calendar_available": inventory["announcement_calendar"] == "available",
        "delisting_mapping_complete": inventory["delisting_mapping_complete"] is True,
        "real_share_accounting_authorized": inventory["real_share_accounting_authorized"] is True,
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {"passed": not blockers, "checks": checks, "blockers": blockers,
        "r2_backtest_permitted": not blockers, "can_enter_step11": not blockers,
        "real_share_account_ready": False, "live_ready": False}


def quality_report(gate, inventory):
    lines = ["# R2 质量门报告", "", "**结论：R2 回测未获准，未执行。**" if not gate["passed"]
             else "**结论：R2 输入质量门通过。**", "", "| 检查 | 状态 |", "|---|---|"]
    for name, passed in gate["checks"].items():
        lines.append(f"| {name} | {'通过' if passed else '阻断'} |")
    lines += ["", "## 当前输入状态", "",
              f"- 公司行动账：`{inventory['company_action_ledger']}`",
              f"- 成交量单位已验证：`{inventory['volume_unit_validated']}`",
              f"- R1 证据包完整：`{inventory['r1_evidence_complete']}`",
              f"- 晋级冠军存在：`{inventory['champion_available']}`", "",
              "R1 代理成交和复权单位账不能替代真实公司行动、股数、整手与结算对账。",
              "必须保留无冠军结果；不得为了进入 R2 而改选未验证候选或放松经济边界。", ""]
    return "\n".join(lines)
