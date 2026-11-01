"""Step 8 daily cross-sectional factor correlations and redundancy clusters."""
import hashlib
import logging
import math

import numpy as np

from core.data.store import json_text, qi, qs
from core.evaluation.composite import _date_key, composition_settings
from core.evaluation.ops import pearson
from core.market.prices import date_window


def complete_linkage_clusters(factor_ids, pair_stats, threshold, min_pair_days):
    """Deterministic complete-linkage clustering on distance=1-abs(mean corr)."""
    clusters = [[factor_id] for factor_id in sorted(factor_ids)]

    def distance(left, right):
        distances = []
        for first in left:
            for second in right:
                key = tuple(sorted((first, second)))
                stat = pair_stats.get(key)
                if stat is None or stat["n_days"] < min_pair_days or stat["mean_correlation"] is None:
                    return math.inf
                distances.append(1.0 - abs(stat["mean_correlation"]))
        return max(distances)

    while len(clusters) > 1:
        candidates = []
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                value = distance(clusters[i], clusters[j])
                if value <= 1.0 - threshold + 1e-15:
                    candidates.append((value, clusters[i], clusters[j], i, j))
        if not candidates:
            break
        _, left, right, i, j = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        merged = sorted(left + right)
        for index in sorted((i, j), reverse=True):
            clusters.pop(index)
        clusters.append(merged)
        clusters.sort(key=lambda cluster: cluster[0])
    return sorted((sorted(cluster) for cluster in clusters), key=lambda cluster: cluster[0])


def build_factor_correlation(store, start=None, end=None, config_root=None) -> dict:
    research, composition, _ = composition_settings(config_root)
    start = start or research["sample"]["evaluation_start"]
    end = end or research["sample"]["in_sample_end"]
    factors = sorted(composition["components"])
    universe_id = composition["universe_id"]
    process_id = composition["process_id"]
    processed = store.output_relation("l3", "factor_processed")
    daily = {(first, second): [] for i, first in enumerate(factors) for second in factors[i + 1:]}
    projections = ", ".join(
        f"max(CASE WHEN factor_id={qs(factor_id)} THEN value END) AS {qi(factor_id)}"
        for factor_id in factors)
    for year in range(int(_date_key(start)[:4]), int(_date_key(end)[:4]) + 1):
        year_start = max(_date_key(start), f"{year}0101")
        year_end = min(_date_key(end), f"{year}1231")
        logging.info("Building factor correlations for %s", year)
        query = f"""
            SELECT trade_date, ts_code, {projections} FROM {processed}
            WHERE universe_id={qs(universe_id)} AND process_id={qs(process_id)}
              AND {date_window(store, year_start, year_end, 'trade_date')}
            GROUP BY trade_date, ts_code ORDER BY trade_date, ts_code
        """
        table = store.db.execute(query).to_arrow_table()
        if not table.num_rows:
            continue
        dates = table.column("trade_date").to_numpy()
        values = {factor_id: table.column(factor_id).to_numpy(zero_copy_only=False).astype(np.float64)
                  for factor_id in factors}
        boundaries = np.flatnonzero(np.r_[True, dates[1:] != dates[:-1]])
        starts = np.r_[boundaries, len(dates)]
        for lo, hi in zip(starts[:-1], starts[1:]):
            for i, first in enumerate(factors):
                x_all = values[first][lo:hi]
                for second in factors[i + 1:]:
                    y_all = values[second][lo:hi]
                    valid = ~np.isnan(x_all) & ~np.isnan(y_all)
                    if np.count_nonzero(valid) >= 2:
                        correlation = pearson(x_all[valid], y_all[valid])
                        if correlation is not None:
                            daily[(first, second)].append(correlation)
        del table
    pair_stats = {}
    for (first, second), correlations in daily.items():
        values = np.asarray(correlations, dtype=np.float64)
        pair_stats[(first, second)] = {
            "n_days": len(values),
            "mean_correlation": float(np.mean(values)) if len(values) else None,
            "std_correlation": float(np.std(values)) if len(values) else None,
            "mean_abs_correlation": float(np.mean(np.abs(values))) if len(values) else None,
        }
    settings = composition["correlation"]
    clusters = complete_linkage_clusters(
        factors, pair_stats, settings["redundancy_threshold"], settings["min_pair_days"])
    cluster_ids = {factor_id: f"cluster_{index + 1:03d}"
                   for index, cluster in enumerate(clusters) for factor_id in cluster}
    rows = []
    for (first, second), stat in sorted(pair_stats.items()):
        mean = stat["mean_correlation"]
        rows.append({"universe_id": universe_id, "process_id": process_id,
                     "factor_a": first, "factor_b": second, **stat,
                     "redundant": bool(mean is not None and stat["n_days"] >= settings["min_pair_days"]
                                       and abs(mean) >= settings["redundancy_threshold"]),
                     "cluster_a": cluster_ids[first], "cluster_b": cluster_ids[second]})
    import pyarrow as pa
    frame = pa.Table.from_pylist(rows)
    store.db.register("factor_correlation_frame", frame)
    result = store.publish("l4", "factor_correlation", "SELECT * FROM factor_correlation_frame",
                           ["universe_id", "process_id", "factor_a", "factor_b"])
    store.db.unregister("factor_correlation_frame")
    digest = hashlib.sha256(json_text(composition).encode("utf-8")).hexdigest()
    report = {"composition_sha256": digest, "start": start, "end": end,
              "universe_id": universe_id, "process_id": process_id,
              "redundancy_threshold": settings["redundancy_threshold"],
              "linkage": "complete", "clusters": clusters, "pairs": rows,
              "manifest_sha256": result["sha256"]}
    store.write_json(("l4", "factor_correlation", "correlation_report.json"), report)
    lines = ["# 因子截面相关性与冗余聚类", "",
             f"区间：{start}–{end}；样本：`{universe_id}`；处理：`{process_id}`。",
             f"完整链接距离为 `1-abs(平均日截面相关)`，合并阈值 {settings['redundancy_threshold']}。", "",
             "## 聚类", ""]
    for index, cluster in enumerate(clusters, 1):
        lines.append(f"- cluster_{index:03d}: {', '.join(f'`{factor_id}`' for factor_id in cluster)}")
    redundant = sorted((row for row in rows if row["redundant"]),
                       key=lambda row: -abs(row["mean_correlation"]))
    lines += ["", "## 高相关因子对", "", "| 因子 A | 因子 B | 平均相关 | 平均绝对相关 | 天数 |",
              "|---|---|---:|---:|---:|"]
    for row in redundant:
        lines.append(f"| {row['factor_a']} | {row['factor_b']} | {row['mean_correlation']:.6f} "
                     f"| {row['mean_abs_correlation']:.6f} | {row['n_days']} |")
    if not redundant:
        lines.append("| 无超过阈值的因子对 | | | | |")
    store.paths.output("l4", "factor_correlation", "correlation_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return {"manifest": result, "clusters": clusters, "redundant_pairs": len(redundant),
            "composition_sha256": digest}
