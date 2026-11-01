"""Step 1 validates fictional inputs and hand-derived expectations, not future engines."""
import csv
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from core.strategy.contract import (assert_accounting_allowed, assert_label_scope,
                                    load_stage3_contract)


ROOT = Path(__file__).parent / "fixtures/golden_stage3"
KEYS = {
    "daily_calendar.csv": ["trade_date"],
    "daily_adjusted_price.csv": ["trade_date", "ts_code"],
    "daily_trade_status.csv": ["trade_date", "ts_code"],
    "daily_stock_state.csv": ["trade_date", "ts_code"],
    "daily_research_universe.csv": ["trade_date", "ts_code", "universe_id"],
    "daily_industry.csv": ["trade_date", "ts_code"],
    "daily_index_member.csv": ["trade_date", "index_code", "con_code"],
    "factor_processed.csv": ["trade_date", "ts_code", "factor_id", "universe_id", "process_id"],
    "financial_available.csv": ["source_table", "record_id"],
    "forward_return_research_only.csv": ["trade_date", "ts_code", "label_id"],
}


def rows(name):
    with (ROOT / name).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.fixture
def expected():
    return json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))


def test_fictional_calendar_has_83_days_and_declared_holidays(expected):
    calendar = rows("daily_calendar.csv")
    assert len(calendar) == expected["calendar"]["trading_days"] == 83
    assert [int(row["trade_index"]) for row in calendar] == list(range(83))
    assert calendar[20]["trade_date"] == expected["calendar"]["T"] == "2020-03-30"
    assert calendar[21]["trade_date"] == expected["calendar"]["D"] == "2020-03-31"
    assert expected["calendar"]["holidays"] == ["2020-04-06", "2020-05-01", "2020-05-04", "2020-05-05"]


def test_all_inputs_have_declared_deep_primary_keys_and_hashes():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fictional"] and not manifest["real_data_accessed"]
    for name, key in KEYS.items():
        data = rows(name)
        identities = [tuple(row[column] for column in key) for row in data]
        assert len(identities) == len(set(identities)), name
        assert all(all(value != "" for value in identity) for identity in identities), name
        digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        assert manifest["files"][name] == digest


def test_independent_fixed_signal_and_missing_component_expectations(expected):
    data = [row for row in rows("factor_processed.csv")
            if row["trade_date"] == expected["calendar"]["T"] and row["process_id"] == "ws_z"]
    for code, expected_score in (("SA001.SZ", 19 / 30), ("SA002.SZ", 19 / 30)):
        values = {row["factor_id"]: float(row["value"]) for row in data if row["ts_code"] == code}
        modules = ((values["bp"] + values["sp_ttm"]) / 2, -values["rev_20"], -values["vol_20"])
        assert modules == (1, 0.6, 0.3)
        assert sum(modules) / 3 == pytest.approx(expected_score, abs=1e-15)
    assert [row["value"] for row in data if row["ts_code"] == "SA003.SZ" and row["factor_id"] == "sp_ttm"] == [""]
    assert expected["signal"]["SA001.SZ"]["score"] == pytest.approx(19 / 30, abs=1e-15)


def test_buffer_hand_example_retains_rank_four_and_replaces_rank_two(expected):
    case = expected["buffer"]
    held = case["held"]
    ranked = case["ranked"]
    target = [code for code in ranked if code in held and ranked.index(code) < 4]
    target += [code for code in ranked if code not in target][:3 - len(target)]
    target.sort(key=ranked.index)
    assert target == case["target"] == ["SA001.SZ", "SA004.SZ", "SA003.SZ"]
    assert "SA002.SZ" not in target


def test_financial_industry_and_member_visibility(expected):
    financial = {row["record_id"]: row for row in rows("financial_available.csv")}
    assert financial["original"]["available_date"] == expected["calendar"]["T"]
    assert financial["revision"]["available_date"] == expected["calendar"]["D"]
    assert financial["revision"]["effective_ann_date"] == expected["calendar"]["T"]
    industry = [row for row in rows("daily_industry.csv") if row["ts_code"] == "SA007.SZ"]
    assert next(row for row in industry if row["trade_date"] == "2020-04-13")["industry_pit_status"] == "snapshot_backfilled"
    assert next(row for row in industry if row["trade_date"] == expected["pit"]["SA007_from"])["industry_pit_status"] == "snapshot_after_in_date"
    members = rows("daily_index_member.csv")
    assert {row["con_code"] for row in members if row["trade_date"] == expected["calendar"]["T"]} == {"SA001.SZ", "SA002.SZ", "SA004.SZ"}
    assert "SA003.SZ" in {row["con_code"] for row in members if row["trade_date"] == expected["calendar"]["D"]}


def test_execution_facts_are_direction_specific_and_separate_from_daily_permission(expected):
    execution = expected["execution"]
    for case in ("SA004.SZ", "SA002.SZ", "SA003.SZ", "SA011.SZ"):
        assert execution[case]["date"] == expected["calendar"]["D"]
    assert execution["SA004.SZ"]["result"] == "reject_open_up_limit"
    assert execution["SA002.SZ"]["result"] == "reject_open_down_limit"
    daily = [row for row in rows("daily_trade_status.csv") if row["trade_date"] == expected["calendar"]["D"]]
    assert all(row["can_buy"] == "" and row["can_sell"] == "" for row in daily)


def test_cash_fee_and_sale_proceeds_hand_calculation(expected):
    case = expected["cash_fees"]
    assert case["initial_cash"] - case["reserved_fee"] == case["available_for_purchase"] == 9990
    assert case["sell_proceeds"] - case["sell_tax"] == case["next_day_available_sale_cash"] == 4995
    assert case["same_day_buying_power_from_sale"] == 0
    assert max(case["commission_small_order"], case["buy_notional"] * 0.0003) == 5


def test_adjusted_unit_accounting_has_no_extra_dividend_or_real_share_claim(expected):
    case = expected["accounting"]
    assert case["buy"]["cash_after"] == case["initial_cash"] - case["buy"]["units"] * case["buy"]["raw_price"] == 300
    after = case["valuation_after_split"]
    assert after["unit_value"] == after["raw_price"] * after["adj_factor"] == 28
    assert after["position_value"] == case["buy"]["units"] * after["unit_value"] == 1400
    assert after["nav"] == after["position_value"] + case["buy"]["cash_after"] == 1700
    assert after["cash_dividend_added"] == 0
    with pytest.raises(PermissionError, match="accepted_company_action_ledger"):
        assert_accounting_allowed(load_stage3_contract()["config"], "real_share_account_net")


def test_listing_state_delisting_and_stale_expectations(expected):
    states = rows("daily_stock_state.csv")
    sa010 = next(row for row in states if row["ts_code"] == "SA010.SZ" and row["trade_date"] == expected["calendar"]["T"])
    sa012 = next(row for row in states if row["ts_code"] == "SA012.SZ" and row["trade_date"] == expected["calendar"]["T"])
    assert sa010["listing_trade_days"] == "" and int(sa010["listing_trade_days_lower_bound"]) >= 120
    assert sa012["listing_trade_days"] == "" and int(sa012["listing_trade_days_lower_bound"]) < 120
    assert any(row["ts_code"] == "SA008.SZ" and row["is_st"] == "True" and row["trade_date"] == expected["lifecycle"]["ST_exit_signal"] for row in states)
    assert expected["lifecycle"]["delisted_position"] == "retain_non_tradable_asset_no_last_price_fill"


def test_fixture_overrides_do_not_change_production_thresholds(expected):
    overrides = yaml.safe_load((ROOT / "fixture_config.yaml").read_text(encoding="utf-8"))
    production = load_stage3_contract()["config"]
    assert overrides["rebalance"]["target_count"] == 3
    assert production["rebalance"]["target_count"] == 100
    assert production["rebalance"]["retain_rank_through"] == 120
    assert production["universe"]["min_cross_section"] == 300


def test_oos_label_fixture_is_rejected_on_entry_and_exit(expected):
    config = load_stage3_contract()["config"]
    label = rows("forward_return_research_only.csv")[-1]
    assert label["trade_date"] == "2024-12-31"
    for field in ("entry_date", "exit_date"):
        changed = dict(label)
        if field == "exit_date":
            changed["entry_date"] = "2024-12-31"
        with pytest.raises(PermissionError, match=field):
            assert_label_scope(changed, config)


def test_manifest_tamper_and_duplicate_key_detection_are_demonstrated(tmp_path):
    original = rows("daily_calendar.csv")
    duplicate = original + [original[0]]
    identities = [row["trade_date"] for row in duplicate]
    assert len(identities) != len(set(identities))
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps({"files": {"daily_calendar.csv": "wrong"}}))
    saved = json.loads(target.read_text())
    assert saved["files"]["daily_calendar.csv"] != hashlib.sha256((ROOT / "daily_calendar.csv").read_bytes()).hexdigest()


def test_partial_fill_capacity_and_zero_fill_expectations(expected):
    case = expected["execution"]["SA009.SZ"]
    assert min(case["adv20_t"] * 0.01, case["full_day_amount_d"] * 0.01) == case["fill_notional"] == 160000
    assert case["requested_notional"] - case["fill_notional"] == case["remaining_notional"] == 40000
    assert expected["execution"]["zero_fill_day"] == "cash_unchanged_no_nav_drop_from_unfilled_order"


def test_constraint_free_budget_and_fixed_degradation(expected):
    case = expected["constraints"]
    assert case["stock_budget"] - case["frozen_weight"] == case["free_weight"] == 0.75
    assert case["frozen_weight"] - 3 * 0.02 == case["stock_after_infeasible_freeze"] == pytest.approx(0.14)
    assert 1 - case["stock_after_infeasible_freeze"] == case["cash_after_infeasible_freeze"] == pytest.approx(0.86)
    assert case["expected"] == "keep_positions_no_new_risk_risk_exit_only"


def test_nav_fee_reconciliation_and_non_overlapping_return(expected):
    case = expected["nav_reconciliation"]
    assert case["initial_cash"] - case["buy_notional"] - case["commission"] - case["transfer_fee"] == pytest.approx(case["cash_after"])
    assert case["units"] * case["raw_price"] * case["adj_factor_after"] == case["position_value"]
    assert case["position_value"] + case["cash_after"] == pytest.approx(case["nav_after"])
    periods = expected["non_overlapping_periods"]
    assert (1 + periods["period_1_return"]) * (1 + periods["period_2_return"]) - 1 == pytest.approx(periods["compound_return"])


def test_scenario_registry_and_manifest_bind_all_expectations(expected):
    registry = json.loads((ROOT / "scenario_registry.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert {row["scenario_id"] for row in registry["scenarios"]} == set(expected) - {"fixture_version", "calendar"}
    for name in ("expected.json", "scenario_registry.json"):
        assert manifest["files"][name] == hashlib.sha256((ROOT / name).read_bytes()).hexdigest()


def test_fixture_regenerates_identically_in_two_independent_directories(tmp_path):
    import subprocess
    import sys
    first, second = tmp_path / "a", tmp_path / "b"
    for target in (first, second):
        target.mkdir()
        (target / "generate.py").write_bytes((ROOT / "generate.py").read_bytes())
        subprocess.run([sys.executable, str(target / "generate.py")], check=True,
                       capture_output=True, text=True)
    names = [path.name for path in first.iterdir() if path.name != "generate.py"]
    assert {path.name for path in second.iterdir() if path.name != "generate.py"} == set(names)
    assert {name: (first / name).read_bytes() for name in names} == {
        name: (second / name).read_bytes() for name in names}
