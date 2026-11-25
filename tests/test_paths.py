from pathlib import Path
import subprocess

import pytest
import yaml

from core.config import Paths, load_paths
from core.config.paths import PROJECT_ROOT


def test_default_paths_ignore_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    monkeypatch.setattr("core.config.paths.PROJECT_ROOT", project)
    monkeypatch.chdir(tmp_path)
    paths = load_paths(PROJECT_ROOT / "configs" / "paths.yaml")
    assert paths.data_root == project / "data"
    assert paths.middle_root == project / "data_middle"
    assert paths.factor_root == project / "factor_research"
    assert paths.strategy_root == project / "strategy_research"
    assert paths.verification_root == project / "verification"


def test_profile_and_environment_override(tmp_path, monkeypatch):
    (tmp_path / "raw").mkdir()
    (tmp_path / "server_raw").mkdir()
    config = tmp_path / "paths.yaml"
    config.write_text(yaml.safe_dump({"profile": "local", "profiles": {
        "local": {"DATA_ROOT": "raw", "MIDDLE_ROOT": "middle"},
        "server": {"DATA_ROOT": "server_raw", "MIDDLE_ROOT": "server_middle"},
    }}), encoding="utf-8")
    assert load_paths(config, "server", tmp_path).data_root == tmp_path / "server_raw"
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "raw"))
    monkeypatch.setenv("MIDDLE_ROOT", str(tmp_path / "override"))
    assert load_paths(config, "server", tmp_path) == Paths(tmp_path / "raw", tmp_path / "override")


def test_missing_raw_directory_is_explicit(tmp_path):
    with pytest.raises(FileNotFoundError, match="DATA_ROOT does not exist"):
        Paths(tmp_path / "missing", tmp_path / "middle").validate()


def test_output_cannot_reach_raw_or_escape(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for middle in (raw, raw / "derived", tmp_path):
        with pytest.raises(ValueError, match="disjoint"):
            Paths(raw, middle).validate()
    paths = Paths(raw, tmp_path / "middle").validate()
    with pytest.raises(ValueError, match="escapes"):
        paths.output("..", "raw", "unsafe")
    with pytest.raises(ValueError, match="disjoint"):
        Paths(raw / ".." / "raw", raw / "would_write_raw").validate()


def test_data_is_ignored_and_untracked():
    outputs = ["data", "data_middle", "factor_research", "strategy_research", "verification"]
    tracked = subprocess.check_output(["git", "ls-files", "--", *outputs], cwd=PROJECT_ROOT, text=True)
    assert not tracked.strip()
    names = [f"{name}/probe" for name in outputs] + ["sample.parquet", "sample.duckdb", "run.log"]
    for name in names:
        result = subprocess.run(["git", "check-ignore", "-q", name], cwd=PROJECT_ROOT)
        assert result.returncode == 0, name


def test_stage_specific_output_routing(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    paths = Paths(raw, tmp_path / "middle").validate()
    assert paths.output("l1", "daily_calendar") == tmp_path / "middle/l1/daily_calendar"
    assert paths.output("l2", "daily_research_universe") == tmp_path / "middle/l2/daily_research_universe"
    assert paths.output("l1", "index_member_available") == tmp_path / "factor_research/l1/index_member_available"
    assert paths.output("l3", "factor_value") == tmp_path / "factor_research/l3/factor_value"
    assert paths.output("l4", "factor_summary") == tmp_path / "factor_research/l4/factor_summary"
    assert paths.output("verification", "repeatability.json") == tmp_path / "verification/stage1/repeatability.json"
    assert paths.output("_tmp") == tmp_path / "verification/stage1/tmp"
    assert paths.output("_provenance") == tmp_path / "verification/stage1/provenance"
