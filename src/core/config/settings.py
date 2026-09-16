from pathlib import Path
import yaml

from .paths import PROJECT_ROOT


def load_settings(name: str, config_root: Path | None = None) -> dict:
    path = (config_root or PROJECT_ROOT / "configs") / name
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Configuration must be a mapping: {path}")
    return value
