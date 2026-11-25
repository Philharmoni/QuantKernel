"""Portable root configuration and raw-data write protection."""
from dataclasses import dataclass
import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FACTOR_REFERENCE_TABLES = {"index_daily_available", "index_member_available", "index_weight_available"}


@dataclass(frozen=True)
class Paths:
    data_root: Path
    middle_root: Path
    factor_root: Path | None = None
    strategy_root: Path | None = None
    verification_root: Path | None = None

    def __post_init__(self):
        object.__setattr__(self, "data_root", Path(self.data_root).expanduser().resolve())
        object.__setattr__(self, "middle_root", Path(self.middle_root).expanduser().resolve())
        parent = self.middle_root.parent
        for field, default in (("factor_root", "factor_research"),
                               ("strategy_root", "strategy_research"),
                               ("verification_root", "verification")):
            value = getattr(self, field) or parent / default
            object.__setattr__(self, field, Path(value).expanduser().resolve())

    @property
    def output_roots(self) -> tuple[tuple[str, Path], ...]:
        return (("MIDDLE_ROOT", self.middle_root), ("FACTOR_RESEARCH_ROOT", self.factor_root),
                ("STRATEGY_RESEARCH_ROOT", self.strategy_root), ("VERIFICATION_ROOT", self.verification_root))

    def validate(self) -> "Paths":
        if not self.data_root.is_dir():
            raise FileNotFoundError(
                f"DATA_ROOT does not exist or is not a directory: {self.data_root}. "
                "Set DATA_ROOT or configs/paths.yaml to an existing raw data directory."
            )
        # Every output tree must be disjoint from the read-only raw tree and
        # from one another, so cleanup of one stage cannot damage another.
        roots = (("DATA_ROOT", self.data_root), *self.output_roots)
        for index, (left_name, left) in enumerate(roots):
            for right_name, right in roots[index + 1:]:
                if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                    raise ValueError(f"{left_name} and {right_name} must be disjoint; raw data is read-only")
        return self

    @staticmethod
    def _below(root: Path, parts: tuple[str, ...], label: str) -> Path:
        target = root.joinpath(*parts).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"Output escapes {label}: {target}")
        return target

    def factor_output(self, *parts: str) -> Path:
        self.validate()
        return self._below(self.factor_root, parts, "FACTOR_RESEARCH_ROOT")

    def strategy_output(self, *parts: str) -> Path:
        self.validate()
        return self._below(self.strategy_root, parts, "STRATEGY_RESEARCH_ROOT")

    def verification_output(self, *parts: str) -> Path:
        self.validate()
        return self._below(self.verification_root, parts, "VERIFICATION_ROOT")

    def output(self, *parts: str) -> Path:
        if parts and (parts[0] in {"l3", "l4"} or
                      (parts[0] == "l1" and len(parts) > 1 and parts[1] in FACTOR_REFERENCE_TABLES)):
            return self.factor_output(*parts)
        if parts and parts[0] == "verification":
            return self.verification_output("stage1", *parts[1:])
        if parts and parts[0] == "_provenance":
            return self.verification_output("stage1", "provenance", *parts[1:])
        if parts and parts[0] == "_tmp":
            return self.verification_output("stage1", "tmp", *parts[1:])
        self.validate()
        return self._below(self.middle_root, parts, "MIDDLE_ROOT")


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

    def resolve(key: str, default: str | None = None) -> Path:
        value = os.environ.get(key, values.get(key, default))
        if not value or not str(value).strip():
            raise ValueError(f"{key} must not be empty")
        path = Path(value).expanduser()
        return (path if path.is_absolute() else root / path).resolve()

    return Paths(resolve("DATA_ROOT"), resolve("MIDDLE_ROOT"),
                 resolve("FACTOR_RESEARCH_ROOT", "./factor_research"),
                 resolve("STRATEGY_RESEARCH_ROOT", "./strategy_research"),
                 resolve("VERIFICATION_ROOT", "./verification")).validate()
