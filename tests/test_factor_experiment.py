"""Tests for the three-action per-factor experiment framework."""
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from core.config import Paths
from core.config.paths import PROJECT_ROOT
from core.data.store import Store, json_text
from core.factor import experiment as experiment_module
from core.factor.experiment import load_experiment, run_experiment
from core.factor.plugin import factor_source_path, load_factor_compute
from core.factor.promotion import promote_experiment, promotion_checks
from core.calendar import build_calendar
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _spec(factor_id="toy_factor"):
    return {"factor_id": factor_id, "version": 1, "category": "size",
            "inputs": {"table": "daily_adjusted_price", "fields": ["total_mv"]},
            "formula": "ln(total_mv * 10000)", "lookback": None, "min_periods": None,
            "expected_direction": -1, "unit": "log(CNY)", "valid_from": "20180601",
            "pit": "quote visible at T close"}


def test_every_accepted_base_factor_has_one_source():
    from core.factor.registry import factor_map
    accepted = [factor_id for factor_id, spec in factor_map().items() if spec["category"] != "composite"]
    for factor_id in accepted:
        assert factor_source_path(factor_id).is_file()
        assert callable(load_factor_compute(factor_id).compute)
    with pytest.raises(ValueError):
        factor_source_path("../unsafe")


def test_compute_auto_generates_immutable_experiment(tmp_path, monkeypatch):
    raw = PROJECT_ROOT / "tests" / "fixtures" / "golden_stage2"
    official = Paths(raw, tmp_path / "official_middle", tmp_path / "official_factor",
                     tmp_path / "official_strategy", tmp_path / "official_verification")
    with Store(official) as store:
        build_calendar(store)
        build_adjusted_price(store)
        build_stock_state(store)
    monkeypatch.setattr(experiment_module, "load_paths", lambda profile=None: official)
    source_root = tmp_path / "research" / "factors"
    source = source_root / "toy_factor" / "compute.py"
    source.parent.mkdir(parents=True)
    source.write_text("""from core.factor.compute import simple_price_factor
SPEC = %r
def compute(store,start=None,end=None,input_end=None,spec=None,params=None):
    return simple_price_factor(store,'toy_factor','ln(total_mv*10000)',
        'total_mv>0 AND isfinite(total_mv)',start,end,input_end)
""" % _spec(), encoding="utf-8")
    result = run_experiment("toy_factor", start="20200323", end="20200331",
                            experiment_root=tmp_path / "experiments", source_root=source_root)
    root = result["root"]
    assert (root / "experiment.yaml").is_file()
    assert (root / "code/compute.py").is_file()
    assert (root / "factor_values.parquet").is_file()
    snapshot_path = root / "snapshot/configs/factor_registry.yaml"
    assert snapshot_path.is_file()
    snapshot = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
    assert "factors" not in snapshot and "registry_version" not in snapshot
    assert "factor_id" not in snapshot["toy_factor"]
    assert "\n\ntoy_factor:" in snapshot_path.read_text(encoding="utf-8")
    assert result["manifest"]["truncation"]["passed"] is None
    assert result["manifest"]["truncation"]["status"] == "not_requested"
    audited = run_experiment("toy_factor", start="20200323", end="20200331",
                             experiment_root=tmp_path / "experiments", source_root=source_root,
                             verification="audit")
    assert audited["manifest"]["truncation"]["passed"] is True
    loaded = load_experiment(root / "factor_values.parquet")
    assert loaded[1]["factor_id"] == "toy_factor"
    with pytest.raises(FileExistsError):
        run_experiment("toy_factor", start="20200323", end="20200331",
                       experiment_root=tmp_path / "experiments", source_root=source_root)
    (root / "factor_values.parquet").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_experiment(root)


def test_legacy_factor_registry_snapshot_remains_readable():
    from core.factor.registry import normalize_factor_registry
    legacy = {"registry_version": 1, "factors": [_spec()]}
    body = {key: value for key, value in _spec().items() if key != "factor_id"}
    current = {"toy_factor": body}
    assert normalize_factor_registry(legacy) == normalize_factor_registry(current)


def _promotion_fixture(tmp_path):
    source_root = tmp_path / "research" / "factors"
    source = source_root / "toy_factor" / "compute.py"
    source.parent.mkdir(parents=True)
    source.write_text("def compute():\n    return None\n", encoding="utf-8")
    root = tmp_path / "experiment"
    (root / "code").mkdir(parents=True)
    (root / "evaluation").mkdir()
    (root / "code/compute.py").write_bytes(source.read_bytes())
    (root / "factor_values.parquet").write_bytes(b"test values")
    metrics = []
    for horizon in (5, 20):
        metrics.append({"label_id": f"ret_o2o_{horizon}", "period_id": "in_sample",
                        "rank_ic_n_days": 100, "rank_ic_mean": 0.03, "rank_icir": 0.3})
    summary = {"technical_passed": True, "metrics": metrics, "peer_correlations": []}
    (root / "evaluation/summary.json").write_text(json_text(summary), encoding="utf-8")
    (root / "evaluation/report.md").write_text("ok\n", encoding="utf-8")
    evaluation = {"technical_passed": True, "summary_sha256": _sha(root / "evaluation/summary.json"),
                  "report_sha256": _sha(root / "evaluation/report.md")}
    (root / "evaluation/manifest.json").write_text(json_text(evaluation), encoding="utf-8")
    manifest = {"factor_values": {"sha256": _sha(root / "factor_values.parquet")},
                "code_sha256": _sha(source)}
    (root / "manifest.json").write_text(json_text(manifest), encoding="utf-8")
    experiment = {"experiment_id": "exp_test", "factor_id": "toy_factor", "status": "computed",
                  "factor_spec": _spec(), "code_sha256": _sha(source)}
    (root / "experiment.yaml").write_text(yaml.safe_dump(experiment), encoding="utf-8")
    registry = tmp_path / "factor_registry.yaml"
    registry.write_text("existing: {}\n", encoding="utf-8")
    policy = yaml.safe_load((PROJECT_ROOT / "configs/factor_admission.yaml").read_text())
    (tmp_path / "factor_admission.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    return root, source_root, registry


def test_promotion_requires_approval_and_appends_atomically(tmp_path):
    root, source_root, registry = _promotion_fixture(tmp_path)
    original = registry.read_text()
    with pytest.raises(ValueError, match="manual_approval_required"):
        promote_experiment(root, registry_path=registry, source_root=source_root)
    assert registry.read_text() == original
    result = promote_experiment(root, approve=True, registry_path=registry, source_root=source_root)
    assert result["promoted"] and (root / "promotion.json").is_file()
    saved = yaml.safe_load(registry.read_text())
    assert list(saved) == ["existing", "toy_factor"]
    assert "factor_id" not in saved["toy_factor"]
    assert "\n\ntoy_factor:" in registry.read_text()
    with pytest.raises(ValueError, match="already exists"):
        promote_experiment(root, approve=True, registry_path=registry, source_root=source_root)


def test_promotion_rejects_source_changed_after_evaluation(tmp_path):
    root, source_root, registry = _promotion_fixture(tmp_path)
    source = factor_source_path("toy_factor", source_root)
    source.write_text("def compute():\n    return 'changed'\n", encoding="utf-8")
    experiment = yaml.safe_load((root / "experiment.yaml").read_text())
    checks = promotion_checks(root, experiment, approve=True,
                              policy_root=registry.parent, source_root=source_root)
    assert not checks["passed"] and "source_changed_after_evaluation" in checks["failures"]
