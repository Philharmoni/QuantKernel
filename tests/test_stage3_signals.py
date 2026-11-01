import json
from pathlib import Path

import pytest
import yaml

from core.strategy.contract import load_stage3_contract
from core.strategy.inputs import SnapshotInputs
from core.strategy.signals import TABLE_KEYS, build_signal_views, publish_signals


ROOT = Path(__file__).parent / "fixtures/golden_stage3"
T = "2020-03-30"


def fixture_config():
    return yaml.safe_load((ROOT / "fixture_config.yaml").read_text(encoding="utf-8"))


def test_fixed_modules_missing_factor_and_no_label_access():
    with SnapshotInputs.from_fixture(ROOT) as inputs:
        build_signal_views(inputs, load_stage3_contract()["config"], fixture_config())
        row = inputs.db.execute("""SELECT value_module,reversal_module,low_vol_module,score
            FROM strategy_signal WHERE trade_date=? AND ts_code='SA001.SZ'""", [T]).fetchone()
        assert row == pytest.approx((1.0, 0.6, 0.3, 19 / 30), abs=1e-15)
        missing = inputs.db.execute("""SELECT factors_complete,is_signal_eligible FROM strategy_universe
            WHERE trade_date=? AND ts_code='SA003.SZ'""", [T]).fetchone()
        assert missing == (False, False)
        assert "l3/forward_return" not in inputs.read_log


def test_eligibility_liquidity_listing_and_daily_gate():
    with SnapshotInputs.from_fixture(ROOT) as inputs:
        build_signal_views(inputs, load_stage3_contract()["config"], fixture_config())
        adv = inputs.db.execute("""SELECT adv20_observations,adv20_cny FROM strategy_feature
            WHERE trade_date=? AND ts_code='SA001.SZ'""", [T]).fetchone()
        assert adv == (20, 20000000.0)
        listing = inputs.db.execute("""SELECT state_valid,listing_trade_days,listing_trade_days_lower_bound FROM strategy_feature f
            JOIN strategy_universe u USING(trade_date,ts_code,universe_id)
            WHERE f.trade_date=? AND f.ts_code='SA010.SZ'""", [T]).fetchone()
        assert listing == (True, None, 220)
        assert inputs.db.execute("SELECT count(*) FROM strategy_universe WHERE trade_date=? AND ts_code='SA012.SZ'", [T]).fetchone()[0] == 0
        daily = inputs.db.execute("SELECT other_eligible_count,factor_complete_count,day_gate_pass FROM strategy_signal_daily WHERE trade_date=?", [T]).fetchone()
        assert daily[0] >= 4 and daily[1] >= 4 and daily[2] is True
        eligible = inputs.db.execute("""SELECT score_rank,score,ts_code FROM strategy_signal
            WHERE trade_date=? AND is_signal_eligible ORDER BY score_rank""", [T]).fetchall()
        assert [row[0] for row in eligible] == list(range(1, len(eligible) + 1))
        assert eligible == sorted(eligible, key=lambda row: (-row[1], row[2]))


def test_industry_pit_and_degenerate_cross_section():
    with SnapshotInputs.from_fixture(ROOT) as inputs:
        build_signal_views(inputs, load_stage3_contract()["config"], fixture_config())
        industries = inputs.db.execute("""SELECT trade_date,industry_l1 FROM strategy_feature
            WHERE ts_code='SA007.SZ' AND trade_date IN ('2020-04-13','2020-04-14') ORDER BY trade_date""").fetchall()
        assert [(str(day), industry) for day, industry in industries] == [("2020-04-13", "__UNKNOWN__"), ("2020-04-14", "FictionalA")]
        degenerate_date = json.loads((ROOT / "expected.json").read_text())["signal"]["all_equal_at"]
        degenerate = inputs.db.execute("""SELECT min_score,max_score,nondegenerate_score,day_gate_pass
            FROM strategy_signal_daily WHERE trade_date=?""", [degenerate_date]).fetchone()
        assert degenerate == (-1 / 3, -1 / 3, False, False)
        assert inputs.db.execute("SELECT count(*) FROM strategy_signal WHERE trade_date=? AND is_signal_eligible", [degenerate_date]).fetchone()[0] == 0


def test_truncation_invariance_for_all_step3_tables():
    with SnapshotInputs.from_fixture(ROOT) as inputs:
        config = load_stage3_contract()["config"]
        build_signal_views(inputs, config, fixture_config())
        full = {table: inputs.db.execute(f"SELECT * FROM {table} WHERE trade_date<=? ORDER BY 1,2,3", [T]).fetchall() for table in TABLE_KEYS}
        build_signal_views(inputs, config, fixture_config(), as_of="20200330")
        for table, expected in full.items():
            actual = inputs.db.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall()
            assert actual == expected, table


def test_two_independent_fixture_publications_are_byte_identical(tmp_path):
    config = load_stage3_contract()["config"]
    for name in ("a", "b"):
        with SnapshotInputs.from_fixture(ROOT) as inputs:
            publish_signals(inputs, tmp_path / name, config, fixture_config())
    for table in TABLE_KEYS:
        relative = Path("l3") / table / "part-00000.parquet"
        assert (tmp_path / "a" / relative).read_bytes() == (tmp_path / "b" / relative).read_bytes()
        manifest = json.loads((tmp_path / "a" / relative.parent / "manifest.json").read_text())
        assert manifest["rows"] > 0 and manifest["primary_key"] == TABLE_KEYS[table]
