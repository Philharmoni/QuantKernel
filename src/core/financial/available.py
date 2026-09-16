"""Preserve source versions and map announcements through the shared calendar."""
import logging

from core.calendar.trading import TradingCalendar
from core.data.store import date_sql, qi, qs


FINANCIAL_TABLES = ('income_vip', 'balancesheet_vip', 'cashflow_vip', 'fina_indicator')


def build_financial_available(store) -> dict:
    rules = store.rules['financial']
    supported = {
        'announcement_rule': 'max_ann_and_f_ann_then_next_trade_day',
        'version_identity': 'full_record_hash',
        'invalid_announcement_policy': 'preserve_raw_exclude_available',
    }
    for field, value in supported.items():
        if rules[field] != value:
            raise ValueError(f'Unsupported financial.{field}: {rules[field]}')
    calendar = TradingCalendar.from_store(store)
    cal = store.output_relation('l1', 'daily_calendar')
    relations = []
    for table in FINANCIAL_TABLES:
        logging.info('Normalizing financial source: %s', table)
        raw = store.raw(table)
        columns = [c for c in store.columns(raw) if not c.startswith('_source_')]
        packed = ','.join(f'{qi(c)} := {qi(c)}' for c in sorted(columns))
        additions = ',NULL::VARCHAR AS f_ann_date' if 'f_ann_date' not in columns else ''
        additions += ',NULL::VARCHAR AS report_type' if 'report_type' not in columns else ''
        name = qi('financial_' + table)
        # Sort only source identities, not hundreds of financial fields.
        store.db.execute(f'''CREATE OR REPLACE TEMP TABLE financial_identity AS
            SELECT _source_file,_source_row,
                sha256({qs(table + '|')} || to_json(struct_pack({packed}))) AS record_id FROM {raw}''')
        store.db.execute('''CREATE OR REPLACE TEMP TABLE financial_dedup AS
            SELECT record_id,count(*) AS source_count,
                first(_source_file ORDER BY _source_file,_source_row) AS _source_file,
                first(_source_row ORDER BY _source_file,_source_row) AS _source_row,
                list(struct_pack(file := _source_file, "row" := _source_row)
                     ORDER BY _source_file,_source_row) AS source_locations
            FROM financial_identity GROUP BY record_id''')
        store.db.execute(f'''CREATE OR REPLACE TEMP TABLE {name} AS
            WITH deduplicated AS (
                SELECT r.*,d.record_id,d.source_count,d.source_locations {additions}
                FROM {raw} r JOIN financial_dedup d USING(_source_file,_source_row)
            ), normalized AS (
                SELECT * EXCLUDE(ann_date,f_ann_date,end_date,_source_file,_source_row),
                    cast(ann_date AS VARCHAR) AS raw_ann_date,
                    cast(f_ann_date AS VARCHAR) AS raw_f_ann_date,
                    cast(end_date AS VARCHAR) AS raw_end_date,
                    {date_sql('ann_date')} AS ann_date,
                    {date_sql('f_ann_date')} AS f_ann_date,
                    {date_sql('end_date')} AS end_date,
                    _source_file AS source_file,_source_row AS source_row,{qs(table)} AS source_table
                FROM deduplicated
            ) SELECT *,greatest(ann_date,f_ann_date) AS effective_ann_date FROM normalized''')
        relations.append(f'SELECT * FROM {name}')
    store.db.execute('CREATE OR REPLACE TEMP VIEW financial_versions AS ' + ' UNION ALL BY NAME '.join(relations))
    store.db.execute('''CREATE OR REPLACE TEMP TABLE financial_version_numbers AS
        SELECT source_table,record_id,
            row_number() OVER(PARTITION BY source_table,ts_code,end_date,report_type
                ORDER BY effective_ann_date NULLS LAST,record_id) AS version FROM financial_versions''')
    store.db.execute(f'''CREATE OR REPLACE TEMP TABLE financial_date_map AS
        SELECT a.effective_ann_date,min(c.trade_date) AS available_date
        FROM (SELECT DISTINCT effective_ann_date FROM financial_versions) a
        LEFT JOIN {cal} c ON c.trade_date>a.effective_ann_date
        GROUP BY a.effective_ann_date''')
    query = f'''WITH classified AS (
        SELECT f.*,m.available_date AS mapped_date,
            CASE WHEN ts_code IS NULL OR NOT regexp_full_match(ts_code,'[^.[:space:]]+[.](SH|SZ|BJ)')
                    THEN 'invalid_code'
                 WHEN end_date IS NULL THEN 'invalid_report_date'
                 WHEN nullif(trim(raw_ann_date),'') IS NULL AND nullif(trim(raw_f_ann_date),'') IS NULL
                    THEN 'missing_announcement'
                 WHEN (nullif(trim(raw_ann_date),'') IS NOT NULL AND ann_date IS NULL) OR
                    (nullif(trim(raw_f_ann_date),'') IS NOT NULL AND f_ann_date IS NULL)
                    THEN 'invalid_announcement'
                 WHEN effective_ann_date<end_date THEN 'announcement_before_report'
                 WHEN effective_ann_date<DATE {qs(calendar.calendar_start)} THEN 'calendar_before_coverage'
                 WHEN effective_ann_date>DATE {qs(calendar.calendar_end)} OR m.available_date IS NULL
                    THEN 'calendar_after_coverage'
                 ELSE 'available' END AS availability_status
        FROM financial_versions f LEFT JOIN financial_date_map m USING(effective_ann_date)
    ) SELECT * EXCLUDE(mapped_date),
        CASE WHEN availability_status='available' THEN mapped_date ELSE NULL END AS available_date
        FROM classified JOIN financial_version_numbers USING(source_table,record_id)'''
    logging.info('Publishing financial_available')
    threads = store.db.execute("SELECT current_setting('threads')").fetchone()[0]
    store.db.execute('SET threads=1')
    try:
        return store.publish('l1', 'financial_available', query, ['source_table', 'record_id'], partition_by='source_table')
    finally:
        store.db.execute(f'SET threads={int(threads)}')


def financial_asof(store, trade_date) -> str:
    """All source versions known on a bounded research date; no arbitrary winner."""
    day = TradingCalendar.from_store(store)._bounded(trade_date)
    relation = store.output_relation('l1', 'financial_available')
    return (f"(SELECT * FROM {relation} WHERE availability_status='available' "
            f"AND available_date<=DATE {qs(day)})")
