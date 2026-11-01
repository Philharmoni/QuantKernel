"""Cross-sectional preprocessing math (plan section 4.5 / Step 6).

Pure numpy functions, used identically by the golden tests and by the
production panel builder, so there is exactly one implementation of the frozen
formulas:

  winsorize   bound = median +/- k * MAD, MAD = median(|x - median|), no 1.4826
              scaling; MAD == 0 skips clipping entirely (config
              winsorize_zero_mad=skip) instead of collapsing the cross-section.
  standardize (x - mean) / population std (ddof=0).
  neutralize  OLS of z on industry dummies + log_circ_mv, evaluated with the
              within (fixed-effects) form: demean z and x inside each industry
              (unknown industry is its own group), fit ONE common slope
              beta = sum(dx*dz)/sum(dx^2), residual = dz - beta*dx. This is
              algebraically identical to the dummy-variable OLS and makes the
              orthogonality properties (sum(residual)=0 per industry,
              sum(residual*x)=0 overall) exact up to float rounding.

Missing values are never filled: NaN factor values are excluded before any
statistic, and a day below min_cross_section yields no output at all.
"""
from dataclasses import dataclass, field

import numpy as np


@dataclass
class CrossSectionResult:
    values: dict = field(default_factory=dict)      # process_id -> np.ndarray (aligned to input order)
    diagnostics: dict = field(default_factory=dict)
    quantiles: dict = field(default_factory=dict)   # before/after winsorize, for the report


def winsorize_mad(values: np.ndarray, k: float) -> tuple:
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    if mad == 0.0:
        return values.copy(), {"median": median, "mad": mad, "lower": median,
                               "upper": median, "clipped": 0, "zero_mad": True}
    lower, upper = median - k * mad, median + k * mad
    clipped = np.clip(values, lower, upper)
    count = int(np.count_nonzero(clipped != values))
    return clipped, {"median": median, "mad": mad, "lower": lower, "upper": upper,
                     "clipped": count, "zero_mad": False}


def zscore(values: np.ndarray) -> tuple:
    mean = float(np.mean(values))
    std = float(np.std(values))  # ddof=0
    if std == 0.0:
        return None, {"mean": mean, "std": std}
    return (values - mean) / std, {"mean": mean, "std": std}


def neutralize_within(z: np.ndarray, x: np.ndarray, groups: np.ndarray) -> tuple:
    """Residual of z on industry dummies (groups) + a single common x slope."""
    unique_groups, inverse = np.unique(groups, return_inverse=True)
    n_groups = len(unique_groups)
    dz = z.copy()
    dx = x.copy()
    for index in range(n_groups):
        mask = inverse == index
        dz[mask] -= float(np.mean(z[mask]))
        dx[mask] -= float(np.mean(x[mask]))
    denominator = float(np.sum(dx * dx))
    info = {"groups": n_groups, "n": len(z), "df": len(z) - (n_groups + 1)}
    if denominator == 0.0:
        return None, {**info, "reason": "zero_regressor_variance"}
    beta = float(np.sum(dx * dz) / denominator)
    residual = dz - beta * dx
    info.update(beta=beta,
                max_abs_group_sum_residual=float(max(
                    abs(float(np.sum(residual[inverse == index]))) for index in range(n_groups))),
                abs_sum_residual_x=float(abs(np.sum(residual * x))))
    return residual, info


def process_cross_section(values, industries, log_circ_mv, *, winsorize_k,
                          unknown_label, min_cross_section, min_neutralization_df,
                          neutral_process_id=None, min_residual_std=1.0e-10) -> CrossSectionResult:
    """Run the frozen pipeline on one (trade_date, factor_id, universe) slice.

    values/industries/log_circ_mv are aligned arrays in a fixed order (the
    production builder sorts by ts_code, which also fixes numpy's summation
    order for bitwise reproducibility). Returns the main ws_z values always
    (when computable) and the neutral version only when neutral_process_id is
    given and its degrees-of-freedom/variance requirements hold.
    """
    values = np.asarray(values, dtype=np.float64)
    log_circ_mv = np.asarray(log_circ_mv, dtype=np.float64)
    industries = np.asarray(industries, dtype=object)
    result = CrossSectionResult()
    diag = result.diagnostics
    valid = ~np.isnan(values)
    n_valid = int(np.count_nonzero(valid))
    diag["n_input"] = len(values)
    diag["n_valid"] = n_valid
    if n_valid < min_cross_section:
        diag["skip_reason"] = "insufficient_cross_section"
        return result
    x = values[valid]
    industry = industries[valid]
    mv = log_circ_mv[valid]
    unknown = np.array([g is None or g == "" for g in industry])
    industry = np.where(unknown, unknown_label, industry)
    diag["unknown_industry_count"] = int(np.count_nonzero(unknown))
    diag["unknown_industry_share"] = diag["unknown_industry_count"] / n_valid

    clipped, winfo = winsorize_mad(x, winsorize_k)
    diag.update({f"winsorize_{key}": value for key, value in winfo.items()})
    z, zinfo = zscore(clipped)
    diag.update({f"standardize_{key}": value for key, value in zinfo.items()})
    result.quantiles = {
        "before": [float(q) for q in np.quantile(x, [0.01, 0.5, 0.99])],
        "after": [float(q) for q in np.quantile(clipped, [0.01, 0.5, 0.99])],
    }
    if z is None:
        diag["skip_reason"] = "zero_variance"
        return result
    result.values["ws_z"] = (np.flatnonzero(valid), z)

    if neutral_process_id is None:
        return result
    usable = ~np.isnan(mv)
    diag["neutralize_missing_regressor"] = int(np.count_nonzero(~usable))
    if int(np.count_nonzero(usable)) < min_cross_section:
        diag["neutralize_skip_reason"] = "insufficient_neutralization_cross_section"
        return result
    residual, ninfo = neutralize_within(z[usable], mv[usable], industry[usable])
    diag.update({f"neutralize_{key}": value for key, value in ninfo.items()})
    if residual is None:
        diag["neutralize_skip_reason"] = ninfo["reason"]
        return result
    if ninfo["df"] < min_neutralization_df:
        diag["neutralize_skip_reason"] = "insufficient_neutralization_df"
        return result
    rez, rezinfo = zscore(residual)
    diag.update({f"restandardize_{key}": value for key, value in rezinfo.items()})
    if rez is None:
        diag["neutralize_skip_reason"] = "zero_residual_variance"
        return result
    # Near-collinear factor/regressor case (e.g. log_total_mv neutralized on
    # log_circ_mv when the two are identical): the residual is numerically ~0,
    # and re-standardizing would only amplify float rounding into noise.
    if rezinfo["std"] < min_residual_std:
        diag["neutralize_skip_reason"] = "degenerate_residual_variance"
        return result
    indices = np.flatnonzero(valid)[usable]
    result.values[neutral_process_id] = (indices, rez)
    return result
