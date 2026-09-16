"""Strategy-free historical identity samples and explicit deferred capabilities."""
import csv
from datetime import date
import json
import shutil

import pytest
import yaml

from core.calendar import build_calendar
from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status
from core.universe.research import build_research_universe


def _config(tmp_path, change=None):
    folder = tmp_path / "configs"
    folder.mkdir(parents=True)
    for name in ("tushare_tables.yaml", "data_middle_layer.yaml"):
        shutil.copy2(PROJECT_ROOT / "configs" / name, folder / name)
    path = folder / "data_middle_layer.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    if change:
        change(rules)
    path.write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    return folder


def _copy_sources(golden_root, destination):
    destination.mkdir()
    for table in ("trade_cal", "stock_basic", "stk_factor_pro", "suspend_d", "limit_list_d"):
        shutil.copy2(golden_root / f"{table}.csv", destination / f"{table}.csv")
    return destination


def _change_basic(raw, change):
    path = raw / "stock_basic.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    change(rows)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _dependencies(store):
    build_calendar(store)
    build_adjusted_price(store)
    build_stock_state(store)
    build_trade_status(store)


def _rows(store):
    relation = store.output_relation("l2", "daily_research_universe")
    return store.rows(f"SELECT * FROM {relation} ORDER BY trade_date,ts_code,universe_id")


def _configs(store):
    relation = store.output_relation("l2", "research_universe_config")
    return {row["universe_id"]: row for row in store.rows(f"SELECT * FROM {relation}")}


def _build(raw, middle, config=None, **kwargs):
    with Store(Paths(raw, middle), config_root=config) as store:
        _dependencies(store)
        build_research_universe(store, **kwargs)
        return _rows(store), _configs(store)


def _index(rows):
    return {(row["universe_id"], row["ts_code"], row["trade_date"]): row for row in rows}


def test_default_identity_sample_keeps_suspensions_and_later_delisted_securities(tmp_path, golden_root):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        _dependencies(store)
        manifests = build_research_universe(store)
        assert set(manifests) == {"config", "universe"}
        rows = _rows(store)
        indexed = _index(rows)
        assert len(rows) == len(indexed) == 34 * 5
        configs = _configs(store)
        assert len(configs) == 4
        assert configs["all_a"]["status"] == "active"
        assert {row["universe_id"] for row in rows} == {"all_a"}
        for name, config in configs.items():
            assert isinstance(json.loads(config["config_json"]), dict)
            if name != "all_a":
                assert config["status"] == "deferred"
                assert config["deferred_reason"]
        for row in rows:
            assert row["config_id"] == configs[row["universe_id"]]["config_id"]
        assert indexed[("all_a", "600001.SH", date(2019, 1, 22))]["is_in_universe"] is True
        assert indexed[("all_a", "600001.SH", date(2019, 1, 23))]["is_in_universe"] is False
        assert indexed[("all_a", "000003.SZ", date(2019, 1, 8))]["is_in_universe"] is False
        assert indexed[("all_a", "000003.SZ", date(2019, 1, 9))]["is_in_universe"] is True
        for day in (3, 9):
            row = indexed[("all_a", "000001.SZ", date(2019, 1, day))]
            assert row["is_in_universe"] is True
            assert row["can_buy"] is False
            assert row["can_sell"] is False
        trade = store.output_relation("l1", "daily_trade_status")
        expected_trade = {(row["ts_code"], row["trade_date"]): row for row in store.rows(f"SELECT * FROM {trade}")}
        for row in rows:
            expected = expected_trade[(row["ts_code"], row["trade_date"])]
            assert row["can_buy"] is expected["can_buy"]
            assert row["can_sell"] is expected["can_sell"]


def test_non_st_rules_support_beijing_exclusion_and_exact_listing_age(tmp_path, golden_root):
    def add_rules(rules):
        rules["universe_rules"] += [
            dict(universe_id="fixture_ex_bj", exclude_st=False, exclude_bj=True, min_listing_trade_days=0),
            dict(universe_id="fixture_ipo2", exclude_st=False, exclude_bj=False, min_listing_trade_days=2),
        ]

    config = _config(tmp_path, add_rules)
    rows, configs = _build(golden_root, tmp_path / "middle", config)
    indexed = _index(rows)
    assert configs["fixture_ex_bj"]["status"] == "active"
    assert configs["fixture_ipo2"]["status"] == "active"
    excluded = indexed[("fixture_ex_bj", "830001.BJ", date(2019, 1, 16))]
    assert excluded["is_in_universe"] is False
    assert excluded["exclusion_reason"]
    assert indexed[("fixture_ipo2", "000003.SZ", date(2019, 1, 9))]["is_in_universe"] is False
    assert indexed[("fixture_ipo2", "000003.SZ", date(2019, 1, 10))]["is_in_universe"] is True


def test_unknown_old_listing_age_can_only_pass_when_its_lower_bound_proves_threshold(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")
    _change_basic(raw, lambda rows: rows[0].__setitem__("list_date", "20170101"))

    def add_age_rule(rules):
        rules["universe_rules"].append(
            dict(universe_id="fixture_ipo2", exclude_st=False, exclude_bj=False, min_listing_trade_days=2)
        )

    config = _config(tmp_path, add_age_rule)
    indexed = _index(_build(raw, tmp_path / "middle", config)[0])
    unknown = indexed[("fixture_ipo2", "000001.SZ", date(2018, 12, 17))]
    assert unknown["is_in_universe"] is None
    assert "LISTING_AGE_UNKNOWN" in unknown["exclusion_reason"]
    assert indexed[("fixture_ipo2", "000001.SZ", date(2018, 12, 18))]["is_in_universe"] is True
    assert indexed[("fixture_ipo2", "000003.SZ", date(2019, 1, 8))]["is_in_universe"] is False
    assert indexed[("fixture_ipo2", "600001.SH", date(2019, 1, 23))]["is_in_universe"] is False


def test_non_a_share_identity_is_not_in_all_a_even_when_listed(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "raw")

    def add_non_a_share(rows):
        rows.append(dict(rows[0], ts_code="200099.SZ", symbol="200099", curr_type="HKD", name="Artificial B share"))

    _change_basic(raw, add_non_a_share)
    rows, _ = _build(raw, tmp_path / "middle")
    other = [row for row in rows if row["ts_code"] == "200099.SZ"]
    assert len(other) == 34
    assert all(row["is_in_universe"] is False for row in other)


def test_config_ids_are_stable_and_change_with_the_rule(tmp_path, golden_root):
    baseline = _config(tmp_path / "baseline")
    changed = _config(tmp_path / "changed", lambda rules: rules["universe_rules"][0].__setitem__("exclude_bj", True))
    _, original = _build(golden_root, tmp_path / "first", baseline)
    _, repeat = _build(golden_root, tmp_path / "second", baseline)
    changed_rows, revised = _build(golden_root, tmp_path / "third", changed)
    assert original == repeat
    assert original["all_a"]["config_id"] != revised["all_a"]["config_id"]
    assert original["all_a"]["config_json"] != revised["all_a"]["config_json"]
    assert all(row["config_id"] == revised["all_a"]["config_id"] for row in changed_rows)


def test_future_delisting_state_does_not_change_earlier_research_rows(tmp_path, golden_root):
    raw = _copy_sources(golden_root, tmp_path / "future_raw")

    def move_future_delisting(rows):
        next(row for row in rows if row["ts_code"] == "600001.SH")["delist_date"] = "20190125"

    _change_basic(raw, move_future_delisting)
    before, _ = _build(golden_root, tmp_path / "before")
    after, _ = _build(raw, tmp_path / "after")
    assert [row for row in before if row["trade_date"] < date(2019, 1, 23)] == [
        row for row in after if row["trade_date"] < date(2019, 1, 23)
    ]
    assert _index(before)[("all_a", "600001.SH", date(2019, 1, 23))]["is_in_universe"] is False
    assert _index(after)[("all_a", "600001.SH", date(2019, 1, 23))]["is_in_universe"] is True


def test_research_slice_matches_full_output(tmp_path, golden_root):
    full, _ = _build(golden_root, tmp_path / "full")
    sliced, _ = _build(golden_root, tmp_path / "slice", start="20190109", end="20190123")
    assert sliced == [row for row in full if date(2019, 1, 9) <= row["trade_date"] <= date(2019, 1, 23)]


@pytest.mark.parametrize("upstream", ["daily_stock_state", "daily_trade_status"])
def test_missing_upstream_security_date_is_not_silently_dropped(tmp_path, golden_root, upstream):
    with Store(Paths(golden_root, tmp_path / "middle")) as store:
        _dependencies(store)
        relation = store.output_relation("l1", upstream)
        store.publish(
            "l1", upstream,
            f"SELECT * FROM {relation} WHERE trade_date <> DATE '2019-01-09' OR ts_code <> '000001.SZ'",
            ["trade_date", "ts_code"],
        )
        with pytest.raises(ValueError, match="coverage|grid|missing|incomplete"):
            build_research_universe(store)


def test_changing_capability_flag_cannot_enable_unimplemented_st_history(tmp_path, golden_root):
    config = _config(tmp_path, lambda rules: rules["capabilities"]["historical_st"].__setitem__("status", "available"))
    with Store(Paths(golden_root, tmp_path / "middle"), config_root=config) as store:
        _dependencies(store)
        with pytest.raises(ValueError, match="ST|st|historical"):
            build_research_universe(store)
