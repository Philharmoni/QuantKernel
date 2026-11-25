"""Shared independent-repeat policy for Stage 3 verification entrypoints."""
import json
from pathlib import Path

from core.quality.verification import verification_metadata, verification_mode


def resolve_repeat(repeat_root, publisher=None, verification="standard"):
    mode = verification_mode(verification)
    root = Path(repeat_root)
    if mode == "fast":
        return None, "skipped"
    manifest_path = root / "manifest.json"
    if manifest_path.is_file():
        return json.loads(manifest_path.read_text(encoding="utf-8")), "verified_existing"
    if mode == "standard":
        return None, "not_requested"
    if publisher is None:
        raise ValueError("Audit repeat requires a publisher")
    return publisher(), "executed"


def table_repeat_mismatches(manifest, repeat, tables):
    if repeat is None:
        return []
    return [table for table in tables
            if manifest["tables"][table]["sha256"] != repeat["tables"][table]["sha256"]]


def file_repeat_mismatches(manifest, repeat):
    if repeat is None:
        return []
    return [path for path, sha in manifest["files"].items() if repeat["files"].get(path) != sha]


def repeat_metadata(mode, status):
    return {"repeat_status": status, **verification_metadata(mode)}
