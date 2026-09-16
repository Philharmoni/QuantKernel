"""Event-time quarterly and TTM versions with complete dependency lineage."""
import calendar as month_calendar
from datetime import date
import hashlib
from itertools import groupby
import math

import pyarrow as pa
import pyarrow.parquet as pq

from core.calendar.trading import TradingCalendar
from core.data.store import json_text, qi, qs


def _quarter(day):
    if day.month not in (3, 6, 9, 12) or day.day != month_calendar.monthrange(day.year, day.month)[1]:
        return None
    return day.year * 4 + day.month // 3 - 1


def _end(index):
    year, quarter = divmod(index, 4)
    month = (quarter + 1) * 3
    return date(year, month, month_calendar.monthrange(year, month)[1])


def _identity(kind, row):
    return hashlib.sha256((kind + json_text(row)).encode('utf-8')).hexdigest()


def _consensus(records):
    possibilities = {(r['value_state'], r['metric_value']) for r in records}
    ids = sorted({r['record_id'] for r in records})
    if len(possibilities) != 1:
        return None, 'ambiguous_metric', ids
    status, value = possibilities.pop()
    return value, status, ids


def _quarter_value(period, cumulative):
    current = cumulative[period]
    value, status, ids = current['consensus']
    index = _quarter(period)
    if index is None:
        return None, 'unsupported_report_period', ids, [period]
    if index % 4 == 0:
        return value, status, ids, [period]
    previous = _end(index - 1)
    dates = [previous, period]
    if previous not in cumulative:
        reason = 'missing_previous_quarter' if status == 'available' else status + '+missing_previous_quarter'
        return None, reason, ids, dates
    old_value, old_status, old_ids = cumulative[previous]['consensus']
    ids = sorted(set(ids + old_ids))
    if status != 'available' or old_status != 'available':
        problems = sorted({s for s in (status, old_status) if s != 'available'})
        return None, '+'.join(problems), ids, dates
    result = value - old_value
    if not math.isfinite(result):
        return None, 'invalid_computation', ids, dates
    return result, 'available', ids, dates


def _derive_group(group, rows):
    source, code, metric = group
    cumulative, quarters, ttm_signatures = {}, {}, {}
    for available, event_rows in groupby(rows, key=lambda r: r['available_date']):
        updates = {}
        for record in event_rows:
            updates.setdefault(record['end_date'], []).append(record)
        affected = set()
        for period, records in updates.items():
            latest = max(r['effective_ann_date'] for r in records)
            old = cumulative.get(period)
            if old is not None and latest < old['effective_ann_date']:
                continue
            chosen = [r for r in records if r['effective_ann_date'] == latest]
            if old is not None and latest == old['effective_ann_date']:
                chosen += old['records']
            cumulative[period] = {'effective_ann_date': latest, 'records': chosen, 'consensus': _consensus(chosen)}
            affected.add(period)
            index = _quarter(period)
            if index is not None and index % 4 != 3 and _end(index + 1) in cumulative:
                affected.add(_end(index + 1))
        changed = set()
        for period in sorted(affected):
            value, status, ids, dates = _quarter_value(period, cumulative)
            signature = (value, status, tuple(ids), tuple(dates))
            if period in quarters and quarters[period]['signature'] == signature:
                continue
            row = dict(source_table=source, ts_code=code, metric=metric, end_date=period,
                       available_date=available, value=value, status=status,
                       source_record_ids=ids, source_end_dates=dates)
            row['version_id'] = _identity('quarter_v1', row)
            quarters[period] = {'row': row, 'signature': signature}
            changed.add(period)
            yield 'quarterly', row
        affected_ttm = set()
        for period in changed:
            index = _quarter(period)
            if index is None:
                affected_ttm.add(period)
            else:
                affected_ttm.update(_end(i) for i in range(index, index + 4) if _end(i) in quarters)
        for period in sorted(affected_ttm):
            index = _quarter(period)
            dates = [] if index is None else [_end(i) for i in range(index - 3, index + 1)]
            present = [quarters[d]['row'] for d in dates if d in quarters]
            missing = [d for d in dates if d not in quarters]
            unavailable = [r['end_date'] for r in present if r['status'] != 'available']
            quarter_ids = [r['version_id'] for r in present]
            record_ids = sorted({rid for r in present for rid in r['source_record_ids']})
            value = None
            if index is None:
                status = 'unsupported_report_period'
                record_ids = quarters[period]['row']['source_record_ids']
            elif any('ambiguous' in r['status'] for r in present):
                status = 'ambiguous_quarter'
            elif any('invalid' in r['status'] for r in present):
                status = 'invalid_quarter'
            elif missing or unavailable:
                status = 'missing_quarter'
            else:
                try:
                    value = math.fsum(r['value'] for r in present)
                except OverflowError:
                    value = None
                status = 'available' if value is not None and math.isfinite(value) else 'invalid_computation'
                if status != 'available':
                    value = None
            signature = (value, status, tuple(quarter_ids), tuple(record_ids))
            if ttm_signatures.get(period) == signature:
                continue
            ttm_signatures[period] = signature
            row = dict(source_table=source, ts_code=code, metric=metric, end_date=period,
                       available_date=available, value=value, status=status,
                       source_record_ids=record_ids, source_end_dates=dates,
                       source_quarter_ids=quarter_ids, missing_source_end_dates=missing,
                       unavailable_source_end_dates=unavailable)
            row['version_id'] = _identity('ttm_v1', row)
            yield 'ttm', row


def build_quarterly_and_ttm(store):
    rules = store.rules['financial']
    expected = {'missing_quarter_policy': 'null_with_reason',
                'same_effective_date_metric_conflict_policy': 'null_with_ambiguous',
                'update_flag_is_tiebreaker': False, 'report_types': ['1']}
    for key, value in expected.items():
        if rules[key] != value:
            raise ValueError(f'Unsupported financial.{key}: {rules[key]}')
    TradingCalendar.from_store(store)
    relation = store.output_relation('l1', 'financial_available')
    available_columns = store.columns(relation)
    parts = []
    for source, fields in rules['cumulative_fields'].items():
        if (source not in ('income_vip', 'cashflow_vip') or not isinstance(fields, list)
                or not fields or any(not isinstance(f, str) for f in fields) or len(fields) != len(set(fields))):
            raise ValueError(f'Invalid cumulative financial configuration: {source}/{fields}')
        source_columns = store.columns(store.raw(source))
        for metric in fields:
            if metric not in available_columns or metric not in source_columns:
                raise ValueError(f'{source}.{metric}: missing field; update configuration and source notes first')
            field = qi(metric)
            numeric = f'try_cast({field} AS DOUBLE)'
            parts.append(f'''SELECT source_table,ts_code,{qs(metric)} AS metric,end_date,
                effective_ann_date,available_date,record_id,
                CASE WHEN isfinite({numeric}) THEN {numeric} ELSE NULL END AS metric_value,
                CASE WHEN {field} IS NULL OR trim(cast({field} AS VARCHAR))='' THEN 'missing_metric'
                     WHEN {numeric} IS NULL OR NOT isfinite({numeric}) THEN 'invalid_metric'
                     ELSE 'available' END AS value_state
                FROM {relation} WHERE source_table={qs(source)} AND trim(cast(report_type AS VARCHAR))='1'
                AND availability_status='available' ''')
    if not parts:
        raise ValueError('No cumulative financial fields are configured')
    query = ' UNION ALL '.join(parts) + ' ORDER BY source_table,ts_code,metric,available_date,effective_ann_date,record_id'
    base = [('source_table', pa.string()), ('ts_code', pa.string()), ('metric', pa.string()),
            ('end_date', pa.date32()), ('available_date', pa.date32()), ('value', pa.float64()),
            ('status', pa.string()), ('source_record_ids', pa.list_(pa.string())),
            ('source_end_dates', pa.list_(pa.date32())), ('version_id', pa.string())]
    schemas = {'quarterly': pa.schema(base), 'ttm': pa.schema(base + [
        ('source_quarter_ids', pa.list_(pa.string())), ('missing_source_end_dates', pa.list_(pa.date32())),
        ('unavailable_source_end_dates', pa.list_(pa.date32()))])}
    paths = {name: store.paths.output('_tmp', name + '_financial.input.parquet') for name in schemas}
    buffers = {name: [] for name in schemas}
    writers = {name: pq.ParquetWriter(paths[name], schema, compression='zstd') for name, schema in schemas.items()}
    try:
        reader = store.db.execute(query).to_arrow_reader(8192)
        records = (row for batch in reader for row in batch.to_pylist())
        for group, grouped in groupby(records, key=lambda r: (r['source_table'], r['ts_code'], r['metric'])):
            for name, row in _derive_group(group, grouped):
                buffers[name].append(row)
                if len(buffers[name]) >= 4096:
                    writers[name].write_table(pa.Table.from_pylist(buffers[name], schema=schemas[name]))
                    buffers[name].clear()
        for name in schemas:
            if buffers[name]:
                writers[name].write_table(pa.Table.from_pylist(buffers[name], schema=schemas[name]))
    finally:
        for writer in writers.values():
            writer.close()
    key = ['source_table', 'ts_code', 'metric', 'end_date', 'available_date']
    results = {}
    for name, path in paths.items():
        table = 'quarterly_financial' if name == 'quarterly' else 'ttm_financial'
        results[name] = store.publish('l1', table, f'SELECT * FROM read_parquet({qs(path.as_posix())})', key,
                                      partition_by='source_table')
    return results


def financial_derived_asof(store, table, trade_date):
    if table not in ('quarterly_financial', 'ttm_financial'):
        raise ValueError(f'Unsupported financial derived table: {table}')
    day = TradingCalendar.from_store(store)._bounded(trade_date)
    relation = store.output_relation('l1', table)
    return f'''(SELECT * FROM {relation} WHERE available_date<=DATE {qs(day)}
        QUALIFY row_number() OVER(PARTITION BY source_table,ts_code,metric,end_date
            ORDER BY available_date DESC,version_id DESC)=1)'''
