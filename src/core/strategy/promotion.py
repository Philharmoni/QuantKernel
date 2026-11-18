"""Step 8 pre-registered economic and verification gates."""
import json


def boundary_check(performance, relative, exposures, period_id, thresholds):
    net = next(row for row in performance if row["period_id"] == period_id
               and row["account_id"] == "c0_three_module_fixed__executed_proxy_net")
    primary = next(row for row in relative if row["period_id"] == period_id
                   and row["benchmark_id"] == "eligible_passive_net")
    ir = primary["annualized_information_ratio"]
    checks = {
        "annualized_return": {"actual": net["annualized_return"], "operator": ">=",
                              "threshold": float(thresholds["minimum_annualized_return"])},
        "max_drawdown_abs": {"actual": abs(net["max_drawdown"]), "operator": "<=",
                             "threshold": float(thresholds["maximum_drawdown_abs"])},
        "information_ratio": {"actual": ir, "operator": ">=",
                              "threshold": float(thresholds["minimum_information_ratio"])},
        "stale_weight_max": {"actual": max(float(row["stale_weight"]) for row in exposures), "operator": "<=",
                             "threshold": float(thresholds["maximum_stale_weight"])},
        "non_tradable_weight_max": {"actual": max(float(row["non_tradable_weight"]) for row in exposures),
                                    "operator": "<=", "threshold": float(thresholds["maximum_non_tradable_weight"])},
    }
    failures = []
    for name, check in checks.items():
        actual = check["actual"]
        passed = actual is not None and (actual >= check["threshold"] if check["operator"] == ">="
                                        else actual <= check["threshold"])
        check["passed"] = passed
        if not passed:
            failures.append(name)
    return {"passed": not failures, "checks": checks, "failures": failures}


def promotion_gate(design, validation):
    failures = ["design:" + name for name in design["failures"]]
    failures += ["validation:" + name for name in validation["failures"]]
    passed = design["passed"] and validation["passed"]
    return {"period_id": "validation", "strategy_id": "c0_three_module_fixed", "scenario_id": "main",
        "design_boundary_passed": design["passed"], "validation_boundary_passed": validation["passed"],
        "research_verification_passed": passed, "promotion_status": "r2_review_eligible" if passed else "no_champion_no_promotion",
        "failure_reasons_json": json.dumps(failures, sort_keys=True),
        "design_checks_json": json.dumps(design["checks"], sort_keys=True),
        "validation_checks_json": json.dumps(validation["checks"], sort_keys=True),
        "economic_promoted": False, "r2_review_eligible": passed,
        "real_share_account_ready": False, "live_ready": False}
