"""Step 3 forward-return labels: every exception path in plan section 4.2 is
checked against the hand-written expectations in
tests/fixtures/golden_stage2/expected.json (forward_return_labels and
beyond_data_end), on top of the Stage 1 builders' real output for that fixture.
"""
from datetime import datetime
import json
import shutil

import pytest
import yaml

from core.calendar import build_calendar
from core.calendar.trading import TradingCalendar
from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store, qs
from core.factor.entry import check_factor_label_isolation, check_no_raw_access
from core.label.forward import build_forward_return
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status

HORIZONS = (1, 5, 10, 20)


def _parse(value):
    return datetime.strptime(value, "%Y%m%d").date()


@pytest.fixture
def expected(golden_stage2_root):
    return json.loads((golden_stage2_root / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture
def built_store(tmp_path, golden_stage2_root):
    # Same trade_status completeness override as the other golden_stage2 tests.
    destination = tmp_path / "configs"
    destination.mkdir()
    for filename in ("tushare_tables.yaml", "data_middle_layer.yaml"):
        shutil.copy2(PROJECT_ROOT / "configs" / filename, destination / filename)
    path = destination / "data_middle_layer.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules["trade_status"].update(
        limit_events_complete=True,
        suspension_events_complete=True,
        suspension_complete_code_suffixes=["SH", "SZ", "BJ"],
        coverage_start="20200302", coverage_end="20200605",
        limit_coverage_start="20200302", limit_coverage_end="20200605",
    )
    path.write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    with Store(Paths(golden_stage2_root, tmp_path / "middle"), config_root=destination) as store:
        build_calendar(store)
        build_adjusted_price(store)
        build_stock_state(store)
        build_trade_status(store)
        build_forward_return(store)
        yield store


def _label_row(store, ts_code, trade_date, family, horizon):
    relation = store.output_relation("l3", "forward_return")
    rows = store.rows(f"SELECT * FROM {relation} WHERE ts_code={qs(ts_code)} "
                      f"AND trade_date=DATE {qs(trade_date.isoformat())} "
                      f"AND label_id={qs(f'ret_{family}_{horizon}')}")
    assert len(rows) == 1, (ts_code, trade_date, family, horizon, rows)
    return rows[0]


def test_forward_return_labels_match_handwritten_expectations(built_store, expected):
    t_date = _parse(expected["scenario_indices"]["T_date"])
    calendar = TradingCalendar.from_store(built_store)
    labels = expected["forward_return_labels"]
    for ts_code, cases in labels.items():
        if ts_code == "description":
            continue
        for horizon in HORIZONS:
            case = cases[f"h{horizon}"]
            row = _label_row(built_store, ts_code, t_date, "o2o", horizon)
            assert row["exception_flag"] == case["flag"], (ts_code, horizon, row)
            if case["ret"] is None:
                assert row["ret"] is None, (ts_code, horizon, row)
            else:
                assert row["ret"] == pytest.approx(case["ret"], abs=1e-12), (ts_code, horizon, row)
            if "entry_price" in cases:
                assert row["entry_price"] == pytest.approx(cases["entry_price"]), (ts_code, horizon)
            if "exit_index" in case:
                assert row["exit_date"] == calendar.trade_dates[case["exit_index"]], (ts_code, horizon)
            if "actual_exit_index" in case:
                assert row["planned_exit_date"] == calendar.trade_dates[case["planned_exit_index"]]
                assert row["exit_date"] == calendar.trade_dates[case["actual_exit_index"]]
            if "exit_price" in case:
                assert row["exit_price"] == pytest.approx(case["exit_price"]), (ts_code, horizon)
            if "exit_delayed_days" in case:
                assert row["exit_delayed_days"] == case["exit_delayed_days"], (ts_code, horizon)
            if "last_quote_index" in case:
                # delisted_in_window: exit uses the last quote before delisting.
                assert row["exit_date"] == calendar.trade_dates[case["last_quote_index"]]
                assert row["exit_price"] == pytest.approx(case["last_quote_price"])
                assert row["delisted_in_window"] is True


def test_beyond_data_end_has_no_partial_window_return(built_store, expected):
    scenario = expected["beyond_data_end"]["SA001.SZ"]
    t_end = _parse(expected["scenario_indices"]["T_END_date"])
    calendar = TradingCalendar.from_store(built_store)
    inside = _label_row(built_store, "SA001.SZ", t_end, "o2o", 5)
    assert inside["exception_flag"] is None
    assert inside["exit_date"] == calendar.trade_dates[scenario["h5"]["exit_index"]]
    assert inside["ret"] == pytest.approx(scenario["h5"]["ret"])
    beyond = _label_row(built_store, "SA001.SZ", t_end, "o2o", 20)
    assert beyond["exception_flag"] == "beyond_data_end"
    assert beyond["beyond_data_end"] is True
    assert beyond["ret"] is None
    assert beyond["exit_price"] is None
    assert beyond["planned_exit_date"] is None  # index 66 > last valid index 65
    assert beyond["entry_date"] == calendar.trade_dates[scenario["entry_index"]]


def test_entry_blocked_label_is_null_and_exit_anomalies_keep_samples(built_store, expected):
    t_date = _parse(expected["scenario_indices"]["T_date"])
    for horizon in HORIZONS:
        blocked = _label_row(built_store, "SA003.SZ", t_date, "o2o", horizon)
        assert blocked["entry_blocked"] is True
        assert blocked["ret"] is None and blocked["entry_price"] is None
        # Delisted and delayed samples must survive with a computed return.
        delisted = _label_row(built_store, "SA006.SZ", t_date, "o2o", horizon)
        delayed = _label_row(built_store, "SA005.SZ", t_date, "o2o", horizon)
        if horizon > 1:
            assert delisted["ret"] is not None
            assert delisted["delisted_in_window"] is True
        assert delayed["ret"] is not None


def test_entry_open_at_up_limit_flag_but_label_still_computed(built_store, expected):
    t_date = _parse(expected["scenario_indices"]["T_date"])
    row = _label_row(built_store, "SA004.SZ", t_date, "o2o", 5)
    assert row["entry_open_at_up_limit"] is True
    assert row["exception_flag"] == "entry_open_at_up_limit"
    assert row["ret"] == pytest.approx(0.0)
    assert row["entry_price"] == pytest.approx(104.5)


def test_reference_c2c_label_uses_closes_and_same_exception_rules(built_store, expected):
    t_date = _parse(expected["scenario_indices"]["T_date"])
    calendar = TradingCalendar.from_store(built_store)
    flat = _label_row(built_store, "SA001.SZ", t_date, "c2c", 5)
    assert flat["entry_date"] == t_date
    assert flat["entry_price"] == pytest.approx(100)
    assert flat["exit_date"] == calendar.offset(t_date, 5)
    assert flat["ret"] == pytest.approx(0.0)
    assert flat["exception_flag"] is None
    # c2c entry is a close: no open-at-up-limit concept, even for SA004.
    limit_entry = _label_row(built_store, "SA004.SZ", t_date, "c2c", 5)
    assert limit_entry["entry_open_at_up_limit"] is False
    # SA005 resumes at index23 with close 115; T+10 = index26 also quotes 115.
    resumed = _label_row(built_store, "SA005.SZ", t_date, "c2c", 10)
    assert resumed["ret"] == pytest.approx(115 / 105 - 1, abs=1e-12)
    # Delisting inside the window is retained, not dropped, for c2c as well.
    delisted = _label_row(built_store, "SA006.SZ", t_date, "c2c", 10)
    assert delisted["delisted_in_window"] is True
    assert delisted["exit_price"] == pytest.approx(110)
    assert delisted["ret"] == pytest.approx(0.0)


def test_label_output_key_uniqueness_and_listed_at_t_scope(built_store, expected):
    relation = built_store.output_relation("l3", "forward_return")
    duplicates = built_store.rows(f"""SELECT trade_date, ts_code, label_id, count(*) AS n
        FROM {relation} GROUP BY 1,2,3 HAVING count(*)>1 LIMIT 5""")
    assert duplicates == []
    # SA006 delists at index20: rows exist while listed at T, none afterwards.
    delist_date = _parse(expected["scenario_indices"]["SA006_delist_date"])
    later = built_store.rows(f"SELECT count(*) AS n FROM {relation} "
                             f"WHERE ts_code='SA006.SZ' AND trade_date>=DATE {qs(delist_date.isoformat())}")
    assert later[0]["n"] == 0
    label_ids = {r["label_id"] for r in built_store.rows(f"SELECT DISTINCT label_id FROM {relation}")}
    assert label_ids == {f"ret_{family}_{h}" for family in ("o2o", "c2c") for h in HORIZONS}


def test_label_module_isolation_guards():
    assert check_no_raw_access() == []
    assert check_factor_label_isolation() == []
