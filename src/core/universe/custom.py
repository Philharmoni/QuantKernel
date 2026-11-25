"""Immutable custom daily research universes built from member CSV/Parquet files."""
import hashlib
import json
from pathlib import Path
import re

from core.data.store import date_sql, json_text, qs

CUSTOM_TABLES = ("custom_research_universe", "custom_research_universe_config")
_SLUG = re.compile(r"[a-z][a-z0-9_]*\Z")


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _members_digest(store) -> str:
    digest = hashlib.sha256()
    cursor = store.db.execute("""SELECT sha256(cast(trade_date AS VARCHAR)||'|'||ts_code)
        FROM custom_universe_members ORDER BY trade_date,ts_code""")
    while rows := cursor.fetchmany(10000):
        for (row_hash,) in rows:
            digest.update(bytes.fromhex(row_hash))
    return digest.hexdigest()


def _existing(store, name) -> str | None:
    root = store.output("l2", name)
    return store.output_relation("l2", name) if (root / "manifest.json").is_file() else None


def custom_universe_identity(store, universe_id: str) -> dict:
    if not _SLUG.fullmatch(universe_id):
        raise ValueError(f"Invalid custom universe_id: {universe_id!r}")
    configs = store.output_relation("l2", "custom_research_universe_config")
    members = store.output_relation("l2", "custom_research_universe")
    rows = store.rows(f"SELECT config_id,status FROM {configs} WHERE universe_id={qs(universe_id)}")
    if len(rows) != 1 or rows[0]["status"] != "active":
        raise ValueError(f"Custom universe is missing or not active: {universe_id}")
    count = store.rows(f"SELECT count(*) AS n FROM {members} WHERE universe_id={qs(universe_id)} "
                       "AND is_in_universe IS TRUE")[0]["n"]
    if not count:
        raise ValueError(f"Custom universe has no member rows: {universe_id}")
    config_manifest = json.loads(store.output("l2", "custom_research_universe_config", "manifest.json").read_text())
    member_manifest = json.loads(store.output("l2", "custom_research_universe", "manifest.json").read_text())
    return {"universe_id": universe_id, "config_id": rows[0]["config_id"],
            "config_manifest_sha256": config_manifest["sha256"],
            "members_manifest_sha256": member_manifest["sha256"]}


def build_custom_research_universe(store, input_path=None, universe_id=None, *, relation=None,
                                   start=None, end=None) -> dict:
    """Publish an immutable pool from a member file or a same-connection DuckDB relation.

    Provide exactly one of input_path and relation. Both need trade_date and ts_code;
    historical stock_name, validation, manifests, and final Parquet are handled here.
    """
    if not _SLUG.fullmatch(universe_id):
        raise ValueError("universe_id must match [a-z][a-z0-9_]*")
    reserved = {rule["universe_id"] for rule in store.rules["universe_rules"]}
    if universe_id in reserved or universe_id.startswith("index_member_"):
        raise ValueError(f"universe_id conflicts with a formal universe: {universe_id}")
    if (input_path is None) == (relation is None):
        raise ValueError("Provide exactly one of input_path or relation")
    source = None
    if relation is not None:
        reader = relation
        source_format = "relation"
    else:
        source = Path(input_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Custom universe input not found: {source}")
        suffix = source.suffix.lower()
        if suffix == ".csv":
            reader = f"read_csv({qs(source.as_posix())},header=true,all_varchar=true)"
            source_format = "csv"
        elif suffix == ".parquet":
            reader = f"read_parquet({qs(source.as_posix())})"
            source_format = "parquet"
        else:
            raise ValueError("Custom universe input must be CSV or Parquet")
    fields = store.columns(reader)
    missing = {"trade_date", "ts_code"} - set(fields)
    if missing:
        raise ValueError(f"Custom universe input missing fields: {sorted(missing)}")

    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW custom_universe_normalized AS
        SELECT {date_sql('trade_date')} AS trade_date,
               upper(trim(cast(ts_code AS VARCHAR))) AS ts_code
        FROM {reader}""")
    invalid = store.rows("""SELECT * FROM custom_universe_normalized
        WHERE trade_date IS NULL OR ts_code IS NULL
           OR NOT regexp_full_match(ts_code,'[0-9]{6}\\.(SH|SZ|BJ)') LIMIT 10""")
    if invalid:
        raise ValueError(f"Invalid custom universe dates or stock codes: {invalid}")
    predicates = []
    if start is not None:
        predicates.append(f"trade_date>={date_sql(qs(start))}")
    if end is not None:
        predicates.append(f"trade_date<={date_sql(qs(end))}")
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    store.db.execute("CREATE OR REPLACE TEMP VIEW custom_universe_members AS "
                     f"SELECT * FROM custom_universe_normalized{where}")
    if store.rows("SELECT count(*) AS n FROM custom_universe_members")[0]["n"] == 0:
        raise ValueError("Custom universe has no rows after date filtering")
    duplicates = store.rows("""SELECT trade_date,ts_code,count(*) AS n FROM custom_universe_members
        GROUP BY 1,2 HAVING n>1 LIMIT 10""")
    if duplicates:
        raise ValueError(f"Duplicate custom universe member keys: {duplicates}")

    calendar = store.output_relation("l1", "daily_calendar")
    states = store.output_relation("l1", "daily_stock_state")
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW custom_universe_enriched AS
        SELECT m.trade_date,m.ts_code,s.historical_name AS stock_name,
               {qs(universe_id)} AS universe_id,true AS is_in_universe
        FROM custom_universe_members m
        JOIN {calendar} c ON c.trade_date=m.trade_date
        LEFT JOIN {states} s ON s.trade_date=m.trade_date AND s.ts_code=m.ts_code""")
    unmatched = store.rows("""SELECT trade_date,ts_code FROM custom_universe_enriched
        WHERE stock_name IS NULL OR trim(stock_name)='' LIMIT 10""")
    if unmatched:
        raise ValueError(f"Custom universe members lack a daily stock state or historical name: {unmatched}")

    source_sha256 = _members_digest(store) if relation is not None else _sha256(source)
    engine = "stage1_custom_relation_v1" if relation is not None else "stage1_custom_file_v1"
    rule = {"engine": engine, "universe_id": universe_id,
            "membership": "one_row_is_member_for_that_day", "forward_fill": False,
            "source_sha256": source_sha256, "start": start, "end": end,
            "stock_name_policy": "l1_daily_stock_state_historical_name",
            "missing_historical_name_policy": "fail"}
    config_json = json_text(rule).strip()
    config_id = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
    old_configs = _existing(store, "custom_research_universe_config")
    if old_configs:
        conflicts = store.rows(f"SELECT config_id FROM {old_configs} WHERE universe_id={qs(universe_id)}")
        if conflicts and conflicts[0]["config_id"] != config_id:
            raise ValueError(f"Custom universe is immutable; use a new universe_id: {universe_id}")

    bounds = store.rows("SELECT count(*) AS n,min(trade_date) AS first,max(trade_date) AS last "
                        "FROM custom_universe_members")[0]
    store.db.execute(f"""CREATE OR REPLACE TEMP VIEW custom_universe_config_input AS
        SELECT {qs(universe_id)} AS universe_id,{qs(config_id)} AS config_id,
               {qs(config_json)} AS config_json,'active' AS status,
               {qs(source_sha256)} AS source_sha256,{qs(source_format)} AS source_format,
               {bounds['n']}::BIGINT AS input_rows,
               {date_sql(qs(str(bounds['first'])))} AS min_trade_date,
               {date_sql(qs(str(bounds['last'])))} AS max_trade_date""")
    config_query = "SELECT * FROM custom_universe_config_input"
    if old_configs:
        config_query = (f"SELECT * FROM {old_configs} WHERE universe_id<>{qs(universe_id)} "
                        f"UNION ALL {config_query}")
    config_manifest = store.publish("l2", "custom_research_universe_config", config_query, ["universe_id"])

    member_query = f"SELECT * FROM custom_universe_enriched"
    old_members = _existing(store, "custom_research_universe")
    if old_members:
        member_query = (f"SELECT * FROM {old_members} WHERE universe_id<>{qs(universe_id)} "
                        f"UNION ALL {member_query}")
    universe_manifest = store.publish("l2", "custom_research_universe", member_query,
                                      ["trade_date", "ts_code", "universe_id"])
    return {"config": config_manifest, "universe": universe_manifest}


def validate_custom_research_universe(store, universe_id=None) -> dict:
    if universe_id is not None:
        custom_universe_identity(store, universe_id)
    configs = store.output_relation("l2", "custom_research_universe_config")
    members = store.output_relation("l2", "custom_research_universe")
    failures = []
    orphan = store.rows(f"""SELECT m.universe_id FROM {members} m
        LEFT JOIN {configs} c ON c.universe_id=m.universe_id AND c.status='active'
        WHERE c.universe_id IS NULL GROUP BY 1 LIMIT 10""")
    if orphan:
        failures.append({"check": "active_config", "samples": orphan})
    invalid = store.rows(f"""SELECT * FROM {members} WHERE stock_name IS NULL OR trim(stock_name)=''
        OR is_in_universe IS NOT TRUE LIMIT 10""")
    if invalid:
        failures.append({"check": "member_contract", "samples": invalid})
    for row in store.rows(f"SELECT universe_id,config_id,config_json FROM {configs}"):
        if hashlib.sha256(row["config_json"].encode("utf-8")).hexdigest() != row["config_id"]:
            failures.append({"check": "config_hash", "samples": [{"universe_id": row["universe_id"]}]})
    report = {"passed": not failures, "universe_id": universe_id, "failures": failures}
    store.write_json(("verification", "stages", "custom_research_universe.json"), report)
    return report
