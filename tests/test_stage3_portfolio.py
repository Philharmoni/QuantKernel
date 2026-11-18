import copy
import json
from pathlib import Path

import pytest

from core.strategy.contract import load_stage3_contract
from core.strategy.portfolio import (make_orders, reference_profile, select_with_buffer,
                                     solve_targets)


def row(code, rank, industry="A", log_size=20.0, adv=1_000_000_000.0):
    return {"ts_code": code, "score_rank": rank, "score": 1.0 / rank,
            "industry_l1": industry, "log_circ_mv": log_size, "circ_mv": 100.0,
            "adv20_cny": adv, "return_volatility_20": 0.02}


def config():
    result = copy.deepcopy(load_stage3_contract()["config"])
    return result


def test_golden_buffer_retains_rank_four_and_replaces_rank_two():
    expected = json.loads(Path("tests/fixtures/golden_stage3/expected.json").read_text())["buffer"]
    ranked = [row(code, index + 1) for index, code in enumerate(expected["ranked"])]
    selected = select_with_buffer(ranked, {code: 0.1 for code in expected["held"]}, 3, 4)
    assert [row["ts_code"] for row in selected] == expected["target"]


def test_reference_profile_is_invariant_to_input_order():
    rows = [row(f"SA{i:03d}.SZ", i, industry="A" if i % 2 else "B", log_size=18 + i / 10)
            for i in range(1, 101)]
    assert reference_profile(rows) == reference_profile(list(reversed(rows)))


def test_one_hundred_name_projection_meets_budget_single_name_and_determinism():
    cfg = config()
    selected = [row(f"SA{i:03d}.SZ", i) for i in range(1, 101)]
    reference = reference_profile(selected)
    first = solve_targets(selected, {}, {}, reference, cfg, 10_000_000)
    second = solve_targets(selected, {}, {}, reference, cfg, 10_000_000)
    assert first["status"] == "solved", first
    assert first == second
    assert sum(first["targets"].values()) == pytest.approx(0.95, abs=1e-8)
    assert max(first["targets"].values()) <= 0.02 + 1e-10
    assert len(first["targets"]) == 100


def test_size_exposures_can_be_diagnostic_only():
    cfg = config()
    selected = [row(f"SA{i:03d}.SZ", i, log_size=21.0) for i in range(1, 101)]
    reference_rows = [row(f"REF{i:03d}.SZ", i, log_size=19.0 if i % 2 else 21.0)
                      for i in range(1, 101)]
    reference = reference_profile(reference_rows)
    strict = copy.deepcopy(cfg)
    strict["portfolio"]["size_z_enabled"] = True
    strict["portfolio"]["size_bucket_enabled"] = True
    blocked = solve_targets(selected, {}, {}, reference, strict, 10_000_000)
    assert blocked["status"] == "infeasible_keep_positions"
    result = solve_targets(selected, {}, {}, reference, cfg, 10_000_000)
    assert result["status"] == "solved", result
    assert result["constraint_diagnostics"] == {}
    assert result["exposure_diagnostics"]["size_z_active"] == pytest.approx(0.95, abs=1e-8)


def test_diagnostic_size_allows_missing_size_on_risk_exit():
    cfg = config()
    selected = [row(f"SA{i:03d}.SZ", i) for i in range(1, 101)]
    risk = row("RISK.SZ", 999)
    risk["log_circ_mv"] = None
    current = {item["ts_code"]: 0.0095 for item in selected}
    current["RISK.SZ"] = 0.01
    result = solve_targets(selected, current, {}, reference_profile(selected), cfg, 10_000_000,
                           risk_exits={"RISK.SZ"}, frozen_infos={"RISK.SZ": risk})
    assert result["status"] == "solved", result
    assert result["targets"]["RISK.SZ"] == 0
    assert result["exposure_diagnostics"]["size_missing_count"] == 1


def test_infeasible_single_cap_retains_positions_without_new_risk():
    cfg = config()
    selected = [row(f"SA{i:03d}.SZ", i) for i in range(1, 4)]
    current = {"FROZEN.SZ": 0.20}
    frozen_info = {"FROZEN.SZ": row("FROZEN.SZ", 999)}
    result = solve_targets(selected, current, current, reference_profile(selected), cfg, 10_000_000,
                           frozen_infos=frozen_info)
    assert result["status"] == "infeasible_keep_positions"
    assert result["targets"] == current


def test_order_capacity_partial_intent_and_costs():
    cfg = config()
    selected = [row("SA001.SZ", 1, adv=20_000_000)]
    reference = reference_profile(selected)
    cfg["objective"]["stock_target_weight"] = "0.95"
    result = solve_targets(selected, {}, {}, reference, cfg, 10_000_000)
    # One name cannot meet the full budget under the 2% cap; no artificial replacement.
    assert result["status"] == "infeasible_keep_positions"
    orders = make_orders({"SA001.SZ": 0.02}, {}, {"SA001.SZ": selected[0]}, cfg,
                         "2020-03-30", "2020-03-31", 10_000_000)
    assert orders[0]["reference_notional_cny"] == 200_000
    assert orders[0]["estimated_cost_cny"] > 65
    assert orders[0]["replacement_allowed"] is False


def test_risk_exit_is_allowed_during_solver_failure_without_turnover_relaxation():
    cfg = config()
    selected = [row(f"SA{i:03d}.SZ", i) for i in range(1, 4)]
    risk = row("RISK.SZ", 999)
    risk["risk_exit"] = True
    current = {"FROZEN.SZ": 0.20, "RISK.SZ": 0.10}
    frozen = {"FROZEN.SZ": 0.20}
    infos = {"FROZEN.SZ": row("FROZEN.SZ", 999)}
    result = solve_targets(selected, current, frozen, reference_profile(selected), cfg, 10_000_000,
                           risk_exits={"RISK.SZ"}, frozen_infos=infos)
    assert result["status"] == "infeasible_keep_positions"
    assert result["targets"]["RISK.SZ"] == 0
    assert result["targets"]["FROZEN.SZ"] == 0.20
    orders = make_orders(result["targets"], current, {"RISK.SZ": risk}, cfg,
                         "2020-03-30", "2020-03-31", 10_000_000)
    assert len(orders) == 1 and orders[0]["purpose"] == "risk_exit"
