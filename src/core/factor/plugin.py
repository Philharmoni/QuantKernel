"""Safe loading of one-version-per-directory factor research implementations."""
import hashlib
import importlib.util
import re
from pathlib import Path

from core.config.paths import PROJECT_ROOT

FACTOR_SOURCE_ROOT = PROJECT_ROOT / "research" / "factors"
_SLUG = re.compile(r"[a-z][a-z0-9_]*\Z")


def factor_source_path(factor_id: str, source_root: Path | None = None) -> Path:
    if not _SLUG.fullmatch(factor_id):
        raise ValueError(f"Invalid factor_id: {factor_id!r}")
    root = Path(source_root or FACTOR_SOURCE_ROOT).resolve()
    path = (root / factor_id / "compute.py").resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Factor source escapes research/factors: {factor_id}")
    if not path.is_file():
        raise FileNotFoundError(f"Missing factor implementation: {path}")
    return path


def load_factor_compute(factor_id: str, source_root: Path | None = None):
    path = factor_source_path(factor_id, source_root)
    module_name = "quantkernel_factor_" + hashlib.sha256(str(path).encode()).hexdigest()[:20]
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "compute", None)):
        raise TypeError(f"Factor source must define compute(): {path}")
    return module
