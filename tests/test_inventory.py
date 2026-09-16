import pytest

from core.data.inventory import compare_inventories, inventory, check_key_files


def test_manifest_does_not_modify_input_and_detects_same_size_changes(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    table = raw / "stock_basic"
    table.mkdir()
    source = table / "sample.txt"
    source.write_bytes(b"abc")
    before = source.stat().st_mtime_ns
    first = inventory(raw, checksums=True)
    assert source.stat().st_mtime_ns == before
    assert source.read_bytes() == b"abc"
    assert compare_inventories(first, first)["passed"]
    source.write_bytes(b"abd")
    comparison = compare_inventories(first, inventory(raw, checksums=True))
    assert not comparison["passed"]
    assert comparison["hash_mismatches"] == ["stock_basic/sample.txt"]


def test_missing_key_table_fails_explicitly(tmp_path):
    with pytest.raises(FileNotFoundError, match="stock_basic"):
        check_key_files(tmp_path)


def test_inventory_reports_missing_and_extra_files(tmp_path):
    (tmp_path / "one").write_bytes(b"1")
    before = inventory(tmp_path)
    (tmp_path / "one").rename(tmp_path / "two")
    result = compare_inventories(inventory(tmp_path), before)
    assert result["missing_local"] == ["one"]
    assert result["extra_local"] == ["two"]


def test_strict_comparison_requires_checksums(tmp_path):
    (tmp_path / "one").write_bytes(b"1")
    manifest = inventory(tmp_path)
    result = compare_inventories(manifest, manifest)
    assert not result["passed"]
    assert result["missing_checksums"] == ["one"]


def test_invalid_manifests_are_rejected(tmp_path):
    from copy import deepcopy

    (tmp_path / "one").write_bytes(b"1")
    manifest = inventory(tmp_path, checksums=True)
    duplicate = deepcopy(manifest)
    duplicate["files"].append(duplicate["files"][0])
    with pytest.raises(ValueError, match="duplicate"):
        compare_inventories(duplicate, manifest)
    manifest["total_bytes"] += 1
    with pytest.raises(ValueError, match="total_bytes"):
        compare_inventories(manifest, manifest)


def test_scan_error_is_not_swallowed(tmp_path, monkeypatch):
    def failing_walk(root, onerror, followlinks):
        onerror(PermissionError("cannot scan table directory"))
        return iter(())

    monkeypatch.setattr("core.data.inventory.os.walk", failing_walk)
    with pytest.raises(PermissionError, match="cannot scan"):
        inventory(tmp_path)
