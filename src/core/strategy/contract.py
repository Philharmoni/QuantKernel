"""Stage 3 Step 0 contract and metadata-only admission checks."""
import hashlib
import json
from datetime import date, datetime
from fractions import Fraction
from pathlib import Path
import re

import yaml

from core.config.paths import PROJECT_ROOT

_DATE = re.compile(r"^[0-9]{8}$")
RAW_READ_PATTERN = re.compile(r"\." + "raw" + r"\s*\(")
LABEL_IMPORT_PATTERN = re.compile(
    r"^\s*(?:from|import)\s+core\.label\b|^\s*from\s+core\s+import\s+[^\n]*\blabel\b", re.M)
STRATEGY_DIRS = ("strategy", "portfolio", "execution", "accounting")


class ContractError(ValueError):
    pass


def canonical_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_stage3_contract(project_root: Path = PROJECT_ROOT) -> dict:
    config = yaml.safe_load((project_root / "configs/strategy_research.yaml").read_text(encoding="utf-8"))
    registry = yaml.safe_load((project_root / "configs/strategy_registry.yaml").read_text(encoding="utf-8"))
    errors = validate_contract(config, registry)
    if errors:
        raise ContractError("; ".join(errors))
    return {"config": config, "registry": registry, "config_sha256": canonical_hash(config),
            "registry_sha256": canonical_hash(registry)}


def parse_ymd(value) -> date:
    text = str(value)
    date_format = "%Y-%m-%d" if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text) else "%Y%m%d"
    if not _DATE.fullmatch(text) and date_format == "%Y%m%d":
        raise ContractError(f"Date must be YYYYMMDD or ISO YYYY-MM-DD: {value}")
    return datetime.strptime(text, date_format).date()


def validate_contract(config: dict, registry: dict) -> list[str]:
    errors = []
    if config.get("status") != "frozen_step_0":
        errors.append("Step 0 contract is not frozen")
    if not config["approval_scope"]["approved_steps"] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14]:
        errors.append("Approved scope must be Steps 0 through 10 and Steps 12-14; Step 11 remains blocked")
    run_group = config["delivery"].get("active_run_group", "")
    if not run_group or run_group in {".", ".."} or "/" in run_group or "\\" in run_group:
        errors.append("Active run group must be one safe strategy directory name")
    for key in ("oos_access", "network_download", "modify_stage1_stage2", "real_data_read"):
        if config["approval_scope"][key] is not False:
            errors.append(f"Forbidden approval: {key}")
    try:
        cutoff = parse_ymd(config["permissions"]["in_sample_end"])
        oos = parse_ymd(config["permissions"]["oos_start"])
        if (oos - cutoff).days != 1:
            errors.append("OOS must start on the calendar day after the in-sample cutoff")
        for name in ("design_period", "validation_period", "confirmation_period", "warm_up_period"):
            start, end = map(parse_ymd, config["research_protocol"][name])
            if not date(2018, 6, 1) <= start <= end <= cutoff:
                errors.append(f"Invalid sample period: {name}")
    except (ContractError, KeyError, TypeError, ValueError) as error:
        errors.append(f"Invalid date contract: {error}")
    components = config["signal"]["components"]
    if sum(abs(Fraction(value)) for value in components.values()) != 1:
        errors.append("Absolute component weights must sum to one")
    protocol = config["research_protocol"]
    if set(protocol["eligible_candidate_ids"]) != {"c0_three_module_fixed", "composite_equal"}:
        errors.append("Eligible candidate budget changed")
    if len(protocol["diagnostic_candidate_ids"]) > protocol["max_diagnostics"]:
        errors.append("Diagnostic budget exceeded")
    ids = [row["strategy_id"] for row in registry["strategies"]]
    if len(ids) != len(set(ids)) or set(ids) != set(protocol["eligible_candidate_ids"] + protocol["diagnostic_candidate_ids"] + ["eligible_passive_net", "common_support_passive_net"]):
        errors.append("Strategy registry and candidate budget disagree")
    if config["accounting"]["mode"] != "adjusted_unit_proxy" or config["delivery"]["real_share_account_ready"]:
        errors.append("R1 must not claim a real-share account")
    if config["delivery"]["live_ready"] or config["delivery"]["oos_accessed"]:
        errors.append("Step 0 cannot claim live readiness or OOS access")
    expected_components = {"bp": Fraction(1, 6), "sp_ttm": Fraction(1, 6),
                           "rev_20": Fraction(-1, 3), "vol_20": Fraction(-1, 3)}
    if {key: Fraction(value) for key, value in components.items()} != expected_components:
        errors.append("Primary signal components or directions changed")
    try:
        fractions = [config["objective"]["stock_target_weight"], config["objective"]["cash_target_weight"],
                     config["universe"]["min_common_factor_coverage"], config["universe"]["exclude_bottom_circ_mv_fraction"],
                     config["portfolio"]["max_stock_weight"], config["portfolio"]["industry_active_limit"],
                     config["portfolio"]["target_one_way_turnover_limit"], config["execution"]["daily_amount_participation"],
                     config["portfolio"]["position_adv_limit"]]
        if any(not 0 <= Fraction(value) <= 1 for value in fractions):
            errors.append("Weight/coverage/participation must be in [0,1]")
        if sum(Fraction(config["objective"][key]) for key in ("stock_target_weight", "cash_target_weight")) != 1:
            errors.append("Target stock and cash weights must sum to one")
        if config["rebalance"]["target_count"] > config["rebalance"]["retain_rank_through"]:
            errors.append("Retain rank must not be smaller than target count")
        if config["accounting"]["real_share_account_status"] != "blocked" or not config["accounting"]["real_share_account_requirements"]:
            errors.append("R2 prerequisites cannot be empty or implicitly accepted")
    except (ValueError, KeyError, ZeroDivisionError) as error:
        errors.append(f"Invalid numeric contract: {error}")
    return errors


def assert_accounting_allowed(config: dict, mode: str) -> None:
    if mode == "real_share_account_net":
        missing = config["accounting"]["real_share_account_requirements"]
        raise PermissionError(f"R2 blocked; accepted inputs required: {missing}")
    if mode != config["accounting"]["mode"]:
        raise PermissionError(f"Unsupported accounting mode: {mode}")


def assert_date_allowed(value, config: dict, *, role="trade_date", field_name=None) -> date:
    parsed = value if isinstance(value, date) else parse_ymd(value)
    if parsed > parse_ymd(config["permissions"]["in_sample_end"]):
        raise PermissionError(f"Locked OOS {role} denied: {parsed.isoformat()} field={field_name or role}")
    return parsed


def assert_label_scope(row: dict, config: dict) -> None:
    for field in ("trade_date", "entry_date", "exit_date", "available_date"):
        if row.get(field) is not None:
            assert_date_allowed(row[field], config, role=field, field_name=field)
    if row.get("exit_date") is not None and row.get("trade_date") is not None:
        if parse_ymd(row["exit_date"]) <= parse_ymd(row["trade_date"]):
            raise ContractError("Label exit must be after its signal date")


def architecture_violations(source_root: Path) -> list[str]:
    violations = []
    for folder_name in STRATEGY_DIRS:
        folder = source_root / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if RAW_READ_PATTERN.search(text):
                violations.append(f"raw_access:{path.relative_to(source_root)}")
            if LABEL_IMPORT_PATTERN.search(text):
                violations.append(f"label_import:{path.relative_to(source_root)}")
    return violations


def create_protection_manifest(project_root: Path = PROJECT_ROOT) -> dict:
    config = yaml.safe_load((project_root / "configs/strategy_research.yaml").read_text(encoding="utf-8"))
    protected = config["protected_scope"]
    excluded = [Path(item) for item in protected["excludes"]]
    entries = {}

    def is_excluded(relative):
        return (relative.suffix.lower() == ".md" or
                any(relative == item or item in relative.parents for item in excluded))

    for item in protected["paths"]:
        root = project_root / item
        paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        for path in paths:
            relative = path.relative_to(project_root)
            if is_excluded(relative) or any(part in {"__pycache__", ".pytest_cache"} for part in relative.parts):
                continue
            entries[relative.as_posix()] = file_sha256(path)
    return {"version": 1, "scope": protected.get("scope", "before_stage3_changes"), "files": entries,
            "manifest_sha256": canonical_hash(entries)}


def write_protection_manifest(project_root: Path = PROJECT_ROOT) -> dict:
    config = yaml.safe_load((project_root / "configs/strategy_research.yaml").read_text(encoding="utf-8"))
    target = project_root / config["protected_scope"]["hashes_file"]
    if target.exists():
        raise FileExistsError("Protected baseline is append/immutable; verify it instead of regenerating")
    result = create_protection_manifest(project_root)
    target.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def verify_protected_hashes(project_root: Path = PROJECT_ROOT, manifest_path: Path | None = None) -> dict:
    if manifest_path is None:
        config = yaml.safe_load((project_root / "configs/strategy_research.yaml").read_text(encoding="utf-8"))
        manifest_path = project_root / config["protected_scope"]["hashes_file"]
    target = manifest_path
    saved = json.loads(target.read_text(encoding="utf-8"))
    differences = []
    protected_files = {name: expected for name, expected in saved["files"].items()
                       if Path(name).suffix.lower() != ".md"}
    for name, expected in protected_files.items():
        path = project_root / name
        if not path.is_file():
            differences.append({"path": name, "reason": "missing"})
        elif file_sha256(path) != expected:
            differences.append({"path": name, "reason": "changed"})
    return {"passed": not differences, "checked_files": len(protected_files), "differences": differences}


def read_stage2_acceptance_metadata(project_root: Path = PROJECT_ROOT) -> dict:
    path = project_root / "factor_research/verification/stage2_quality_gate.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("passed") is not True or report.get("failures"):
        raise ContractError("Stage 2 acceptance metadata is not passed")
    return {"path": path.relative_to(project_root).as_posix(), "sha256": file_sha256(path),
            "passed": True, "checks": {key: value["passed"] for key, value in report["checks"].items()},
            "oos_formal_factors": report["checks"]["out_of_sample"]["formal_oos_factors"]}
