"""Portable root configuration and raw-data write protection."""
from dataclasses import dataclass
import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Paths:
    data_root: Path
    middle_root: Path

    def __post_init__(self):
        object.__setattr__(self, "data_root", Path(self.data_root).expanduser().resolve())
        object.__setattr__(self, "middle_root", Path(self.middle_root).expanduser().resolve())

    def validate(self) -> "Paths":
        if not self.data_root.is_dir():
            raise FileNotFoundError(
                f"DATA_ROOT does not exist or is not a directory: {self.data_root}. "
                "Set DATA_ROOT or configs/paths.yaml to an existing raw data directory."
            )
        # Disjoint trees also prevent rebuild/cleanup from containing raw data.
        if (self.middle_root == self.data_root
                or self.middle_root.is_relative_to(self.data_root)
                or self.data_root.is_relative_to(self.middle_root)):
            raise ValueError("DATA_ROOT and MIDDLE_ROOT must be disjoint; raw data is read-only")
        return self

    def output(self, *parts: str) -> Path:
        target = self.middle_root.joinpath(*parts).resolve()
        if not target.is_relative_to(self.middle_root):
            raise ValueError(f"Output escapes MIDDLE_ROOT: {target}")
        self.validate()
        return target


def load_paths(config: str | Path | None = None, profile: str | None = None,
               project_root: str | Path | None = None) -> Paths:
    root = Path(project_root or PROJECT_ROOT).resolve()
    config_path = Path(config) if config else root / "configs" / "paths.yaml"
    with config_path.open(encoding="utf-8") as stream:
        settings = yaml.safe_load(stream)
    selected = profile or os.environ.get("DATA_PROFILE") or settings["profile"]
    if selected not in settings["profiles"]:
        raise ValueError(f"Unknown path profile: {selected}")
    values = settings["profiles"][selected]

    def resolve(key: str) -> Path:
        value = os.environ.get(key, values[key])
        if not value or not str(value).strip():
            raise ValueError(f"{key} must not be empty")
        path = Path(value).expanduser()
        return (path if path.is_absolute() else root / path).resolve()

    return Paths(resolve("DATA_ROOT"), resolve("MIDDLE_ROOT")).validate()
