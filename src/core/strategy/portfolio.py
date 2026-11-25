"""Step 4 deterministic target portfolios and reference order intents."""
import hashlib
import json
from pathlib import Path

import numpy as np

from core.config.paths import PROJECT_ROOT
from core.strategy.contract import canonical_hash, file_sha256, load_stage3_contract, parse_ymd
from core.strategy.inputs import SnapshotInputs, quote_string

TABLE_KEYS = {
    "target_portfolio": ["signal_date", "ts_code", "strategy_id"],
    "order_intent": ["order_id"],
    "portfolio_daily": ["signal_date", "strategy_id"],
}


def reference_profile(rows):
    if not rows:
        raise ValueError("Passive reference is empty")
    rows = sorted(rows, key=lambda row: row["ts_code"])
    total = sum(float(row["circ_mv"]) for row in rows)
    if not np.isfinite(total) or total <= 0:
        raise ValueError("Passive reference has invalid circ_mv")
    logs = np.array([float(row["log_circ_mv"]) for row in rows], dtype=float)
    if not np.isfinite(logs).all():
        raise ValueError("Passive reference has missing log_circ_mv")
    mean, std = float(logs.mean()), float(logs.std())
    q1, q2 = map(float, np.quantile(logs, [1 / 3, 2 / 3]))
    industry = {}
    buckets = {"small": 0.0, "mid": 0.0, "large": 0.0}
    for row in rows:
        weight = float(row["circ_mv"]) / total
        industry[row["industry_l1"]] = industry.get(row["industry_l1"], 0.0) + weight
        bucket = "small" if float(row["log_circ_mv"]) <= q1 else "mid" if float(row["log_circ_mv"]) <= q2 else "large"
        buckets[bucket] += weight
    return {"industry": industry, "size_mean": mean, "size_std": std,
            "size_q1": q1, "size_q2": q2, "size_buckets": buckets}


def _linear(rows, coefficients, lower=-np.inf, upper=np.inf):
    rows.append((np.asarray(coefficients, dtype=float), float(lower), float(upper)))


def _project_linear(value, coefficients, lower, upper):
    dot = float(coefficients @ value)
    norm = float(coefficients @ coefficients)
    if norm == 0:
        return value
    if dot > upper:
        return value - ((dot - upper) / norm) * coefficients
    if dot < lower:
        return value + ((lower - dot) / norm) * coefficients
    return value


def dykstra_projection(initial, lower, upper, rows, max_cycles=2000, tolerance=1e-10):
    """Euclidean projection onto box/halfspaces, deterministic for a frozen row order."""
    x = np.clip(np.asarray(initial, dtype=float), lower, upper)
    residuals = [np.zeros_like(x) for _ in rows] + [np.zeros_like(x)]
    constraints = list(rows) + [(None, None, None)]
    for cycle in range(1, max_cycles + 1):
        previous = x.copy()
        for index, (coefficients, low, high) in enumerate(constraints):
            value = x + residuals[index]
            if coefficients is None:
                projected = np.clip(value, lower, upper)
            else:
                projected = _project_linear(value, coefficients, low, high)
            residuals[index] = value - projected
            x = projected
        violation = max([0.0] + [max(0.0, float(a @ x) - high, low - float(a @ x))
                                 for a, low, high in rows])
        change = float(np.max(np.abs(x - previous)))
        if violation <= tolerance and change <= 1e-12:
            return x, True, cycle, violation
    violation = max([0.0] + [max(0.0, float(a @ x) - high, low - float(a @ x))
                             for a, low, high in rows])
    return x, False, max_cycles, violation


def select_with_buffer(ranked, current_weights, target_count, retain_rank):
    retained = sorted([row for row in ranked if current_weights.get(row["ts_code"], 0) > 0
                       and row["score_rank"] <= retain_rank], key=lambda row: (row["score_rank"], row["ts_code"]))
    retained = retained[:target_count]
    selected = list(retained)
    for row in sorted(ranked, key=lambda row: (row["score_rank"], row["ts_code"])):
        if len(selected) >= target_count:
            break
        if row["ts_code"] not in {item["ts_code"] for item in selected}:
            selected.append(row)
    return sorted(selected, key=lambda row: (row["score_rank"], row["ts_code"]))


def solve_targets(selected, current_weights, frozen_weights, reference, config, nav,
                  ordinary_exits=(), risk_exits=(), frozen_infos=None):
    """Project equal ideals; on failure retain reference positions and permit risk exits only."""
    rules = config["portfolio"]
    stock_budget = float(config["objective"]["stock_target_weight"])
    frozen_weights = {code: float(weight) for code, weight in frozen_weights.items()}
    selected = sorted([row for row in selected if row["ts_code"] not in frozen_weights], key=lambda row: row["ts_code"])
    variable_codes = [row["ts_code"] for row in selected]
    for code in sorted(set(ordinary_exits) | set(risk_exits)):
        if code not in variable_codes and code not in frozen_weights:
            variable_codes.append(code)
    info = {row["ts_code"]: row for row in selected}
    info.update(frozen_infos or {})
    risk_exits = set(risk_exits)
    ordinary_exits = set(ordinary_exits)
    n = len(variable_codes)
    if n == 0:
        targets = dict(current_weights)
        for code in risk_exits:
            targets[code] = 0.0
        return {"status": "risk_exit_only" if risk_exits else "no_action", "targets": targets,
                "solver_cycles": 0, "max_violation": 0.0, "variable_count": 0}
    free_budget = stock_budget - sum(frozen_weights.values())
    ideals = {row["ts_code"]: free_budget / len(selected) for row in selected} if selected and free_budget > 0 else {}
    initial_w = np.array([ideals.get(code, 0.0) for code in variable_codes], dtype=float)
    current = np.array([float(current_weights.get(code, 0.0)) for code in variable_codes], dtype=float)
    initial_t = np.array([0.0 if code in risk_exits else abs(initial_w[i] - current[i])
                          for i, code in enumerate(variable_codes)], dtype=float)
    lower_w, upper_w = np.zeros(n), np.zeros(n)
    for i, code in enumerate(variable_codes):
        if code in ordinary_exits or code in risk_exits:
            upper_w[i] = 0.0
        else:
            adv = float(info[code]["adv20_cny"])
            upper_w[i] = min(float(rules["max_stock_weight"]), adv * float(rules["position_adv_limit"]) / nav)
    lower = np.concatenate([lower_w, np.zeros(n)])
    upper = np.concatenate([upper_w, np.full(n, np.inf)])
    rows = []
    _linear(rows, np.concatenate([np.ones(n), np.zeros(n)]), free_budget, free_budget)
    for i, code in enumerate(variable_codes):
        if code in risk_exits:
            continue
        adv = float(info[code]["adv20_cny"]) if code in info else 0.0
        order_cap = adv * float(rules["order_adv_participation"]) / nav
        buy = np.zeros(2 * n)
        buy[i] = 1
        _linear(rows, buy, upper=current[i] + order_cap)
        sell = np.zeros(2 * n)
        sell[i] = -1
        _linear(rows, sell, upper=-current[i] + order_cap)
        plus = np.zeros(2 * n)
        plus[i], plus[n + i] = 1, -1
        _linear(rows, plus, upper=current[i])
        minus = np.zeros(2 * n)
        minus[i], minus[n + i] = -1, -1
        _linear(rows, minus, upper=-current[i])
    if any(weight > 0 for weight in current_weights.values()):
        _linear(rows, np.concatenate([np.zeros(n), np.ones(n)]), upper=2 * float(rules["target_one_way_turnover_limit"]))
    e = stock_budget
    groups = sorted(set(reference["industry"]) | {row["industry_l1"] for row in selected})
    frozen_industry = {}
    for code, weight in frozen_weights.items():
        industry = info.get(code, {}).get("industry_l1", "__UNKNOWN__")
        frozen_industry[industry] = frozen_industry.get(industry, 0.0) + weight
    for group in groups:
        coefficients = np.zeros(2 * n)
        for i, code in enumerate(variable_codes):
            if code in info and info[code]["industry_l1"] == group:
                coefficients[i] = 1
        center = e * reference["industry"].get(group, 0.0) - frozen_industry.get(group, 0.0)
        limit = float(rules["industry_active_limit"])
        _linear(rows, coefficients, center - limit, center + limit)
    std = reference["size_std"]
    size_z_enabled = bool(rules.get("size_z_enabled", True))
    size_bucket_enabled = bool(rules.get("size_bucket_enabled", True))
    z = {}
    for code, row in info.items():
        if row.get("log_circ_mv") is not None:
            z[code] = 0.0 if std == 0 else (float(row["log_circ_mv"]) - reference["size_mean"]) / std
    size_codes = set(variable_codes) | set(frozen_weights)
    missing_size_codes = size_codes - set(z)
    required_size_codes = {code for i, code in enumerate(variable_codes) if upper_w[i] > 0}
    required_size_codes |= {code for code, weight in frozen_weights.items() if weight > 0}
    if (size_z_enabled or size_bucket_enabled) and missing_size_codes & required_size_codes:
        raise ValueError("Size constraints require log_circ_mv for active positions")
    def assigned_size_bucket(code):
        raw = info.get(code, {}).get("log_circ_mv")
        if raw is None:
            return None
        value = float(raw)
        return "small" if value <= reference["size_q1"] else "mid" if value <= reference["size_q2"] else "large"
    coefficients = np.zeros(2 * n)
    for i, code in enumerate(variable_codes):
        coefficients[i] = z.get(code, 0.0)
    frozen_z = sum(weight * z.get(code, 0.0) for code, weight in frozen_weights.items())
    if size_z_enabled:
        _linear(rows, coefficients, -float(rules["size_z_active_limit"]) - frozen_z,
                float(rules["size_z_active_limit"]) - frozen_z)
    if size_bucket_enabled:
        for bucket in ("small", "mid", "large"):
            coefficients = np.zeros(2 * n)
            for i, code in enumerate(variable_codes):
                coefficients[i] = 1 if assigned_size_bucket(code) == bucket else 0
            frozen_bucket = 0.0
            for code, weight in frozen_weights.items():
                if assigned_size_bucket(code) == bucket:
                    frozen_bucket += weight
            center = e * reference["size_buckets"][bucket] - frozen_bucket
            limit = float(rules["size_bucket_active_limit"])
            _linear(rows, coefficients, center - limit, center + limit)
    initial = np.concatenate([initial_w, initial_t])
    solution, converged, cycles, violation = dykstra_projection(
        initial, lower, upper, rows, max_cycles=int(rules.get("solver_max_cycles", 2000)),
        tolerance=float(rules.get("solver_tolerance", 1e-10)))
    weights, absolute = solution[:n], solution[n:]
    diagnostics = {"budget_abs": abs(float(weights.sum()) - free_budget),
                   "single_cap_excess": float(np.max(weights - upper_w)),
                   "turnover_excess": (float(absolute.sum()) - 2 * float(rules["target_one_way_turnover_limit"]))
                                      if any(weight > 0 for weight in current_weights.values()) else 0.0}
    industry_excess = 0.0
    for group in groups:
        active = frozen_industry.get(group, 0.0) - e * reference["industry"].get(group, 0.0)
        active += sum(weights[i] for i, code in enumerate(variable_codes) if info.get(code, {}).get("industry_l1") == group)
        industry_excess = max(industry_excess, abs(active) - float(rules["industry_active_limit"]))
    diagnostics["industry_max_excess"] = industry_excess
    size_active = frozen_z + sum(weights[i] * z.get(code, 0.0) for i, code in enumerate(variable_codes))
    if size_z_enabled:
        diagnostics["size_z_excess"] = abs(size_active) - float(rules["size_z_active_limit"])
    bucket_excess = 0.0
    bucket_active = {}
    for bucket in ("small", "mid", "large"):
        active = -e * reference["size_buckets"][bucket]
        for code, weight in frozen_weights.items():
            if assigned_size_bucket(code) == bucket:
                active += weight
        for i, code in enumerate(variable_codes):
            if assigned_size_bucket(code) == bucket:
                active += weights[i]
        bucket_active[bucket] = active
        bucket_excess = max(bucket_excess, abs(active) - float(rules["size_bucket_active_limit"]))
    if size_bucket_enabled:
        diagnostics["size_bucket_max_excess"] = bucket_excess
    exposures = {"size_z_active": size_active, "size_bucket_active": bucket_active,
                 "size_missing_count": len(missing_size_codes)}
    conflicts = {name: value for name, value in diagnostics.items() if value > 1e-8}
    feasible = converged and violation <= 1e-8 and np.all(weights >= -1e-8)
    if not feasible:
        targets = dict(current_weights)
        for code in risk_exits:
            targets[code] = 0.0
        return {"status": "infeasible_keep_positions", "targets": targets,
                "solver_cycles": cycles, "max_violation": violation, "variable_count": n,
                "constraint_diagnostics": conflicts, "exposure_diagnostics": exposures}
    targets = dict(frozen_weights)
    for i, code in enumerate(variable_codes):
        targets[code] = 0.0 if abs(solution[i]) < 1e-12 else float(solution[i])
    return {"status": "solved", "targets": targets, "solver_cycles": cycles,
            "max_violation": violation, "variable_count": n, "constraint_diagnostics": conflicts,
            "exposure_diagnostics": exposures}


def estimate_cost(notional, side, volatility, adv20, config):
    if notional <= 0:
        return {"commission": 0.0, "transfer": 0.0, "tax": 0.0, "impact": 0.0, "total": 0.0}
    costs = config["costs"]["main"]
    commission = max(float(costs["commission_minimum_cny_per_stock_direction_day"]),
                     notional * float(costs["commission_rate"]))
    transfer = notional * float(costs["transfer_fee_rate"])
    tax = notional * float(costs["sell_tax_rate"]) if side == "SELL" else 0.0
    participation = notional / adv20 if adv20 > 0 else np.inf
    impact = notional * (float(costs["impact_fixed_rate"]) + float(costs["impact_volatility_coefficient"]) * volatility * np.sqrt(participation))
    return {"commission": commission, "transfer": transfer, "tax": tax, "impact": impact,
            "total": commission + transfer + tax + impact}


def make_orders(targets, current_weights, infos, config, signal_date, execution_date, nav):
    orders = []
    if execution_date is None:
        return orders
    for code in sorted(set(targets) | set(current_weights)):
        delta = float(targets.get(code, 0.0)) - float(current_weights.get(code, 0.0))
        if abs(delta) <= 1e-12:
            continue
        info = infos.get(code, {})
        side = "BUY" if delta > 0 else "SELL"
        adv = info.get("adv20_cny")
        volatility = info.get("return_volatility_20")
        cap = float(adv) * float(config["portfolio"]["order_adv_participation"]) / nav if adv is not None else 0.0
        order_weight = min(abs(delta), cap)
        notional = order_weight * nav
        purpose = "risk_exit" if info.get("risk_exit") else "rebalance"
        reason = "" if order_weight > 0 and volatility is not None else "missing_adv_or_volatility"
        if notional <= 0 or volatility is None:
            notional, order_weight = 0.0, 0.0
        cost = estimate_cost(notional, side, float(volatility or 0), float(adv or 0), config)
        order_id = hashlib.sha256(f"{config['signal']['primary_id']}|{signal_date}|{code}|{side}|{purpose}".encode()).hexdigest()[:24]
        orders.append({"order_id": order_id, "signal_date": signal_date, "planned_execution_date": execution_date,
            "ts_code": code, "strategy_id": config["signal"]["primary_id"], "side": side, "purpose": purpose,
            "target_weight": targets.get(code, 0.0), "current_reference_weight": current_weights.get(code, 0.0),
            "order_weight": order_weight, "reference_nav_cny": nav, "reference_notional_cny": notional,
            "adv20_cny": adv, "return_volatility_20": volatility, "estimated_cost_cny": cost["total"],
            "time_in_force": "DAY", "replacement_allowed": False, "order_status": "intent" if notional else "blocked",
            "block_reason": reason})
    return orders


def default_step4_run_id(project_root=PROJECT_ROOT, use_active=True):
    contract = load_stage3_contract(project_root)
    active = contract["config"]["delivery"].get("active_step4_run")
    if use_active and active:
        return active
    config = contract["config"]
    identity = {"step": 4, "step3_manifest": file_sha256(project_root / config["delivery"]["step3_manifest"]),
                "portfolio": config["portfolio"], "objective": config["objective"], "costs": config["costs"],
                "code": file_sha256(Path(__file__)),
                "builder": file_sha256(Path(__file__).with_name("portfolio_build.py"))}
    return config["delivery"]["active_run_group"] + "/step4"
