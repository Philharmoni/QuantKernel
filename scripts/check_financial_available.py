"""Step 8 full-source conservation and strictly delayed availability gate."""
import json

import _bootstrap  # noqa: F401
from core.data.store import Store, qi, qs
from core.financial.available import FINANCIAL_TABLES


def check(store):
    relation = store.output_relation('l1', 'financial_available')
    calendar = store.output_relation('l1', 'daily_calendar')
    violations = store.rows(f'''SELECT f.source_table,f.ts_code,f.end_date,f.ann_date,
        f.f_ann_date,f.effective_ann_date,f.available_date,f.availability_status
        FROM {relation} f LEFT JOIN {calendar} c ON f.available_date=c.trade_date
        WHERE (availability_status='available' AND
            (effective_ann_date IS NULL OR available_date IS NULL OR c.trade_date IS NULL
             OR available_date<=effective_ann_date OR c.prev_trade_date>effective_ann_date
             OR effective_ann_date<c.calendar_start))
           OR (availability_status<>'available' AND available_date IS NOT NULL) LIMIT 10''')
    if violations:
        raise ValueError(f'financial_available: invalid PIT visibility: {violations}')
    result = {'passed': True, 'config_hash': store.config_hash, 'sources': {}}
    for table in FINANCIAL_TABLES:
        profile = store.paths.output('l0', 'profiles', table + '.json')
        expected = (json.loads(profile.read_text(encoding='utf-8'))['rows'] if profile.exists()
                    else store.db.execute(f'SELECT count(*) FROM {store.raw(table)}').fetchone()[0])
        found = store.rows(f'''SELECT count(*) AS records,coalesce(sum(source_count),0) AS source_rows,
            count(*) FILTER(WHERE source_count<>len(source_locations)) AS lineage_errors
            FROM {relation} WHERE source_table={qs(table)}''')[0]
        if expected != found['source_rows'] or found['lineage_errors']:
            raise ValueError(f'{table}: source conservation/lineage failed; expected {expected}, found {found}')
        keys = ','.join(qi(k) for k in store.tables[table]['primary_key'])
        found['candidate_key_conflict_groups'] = store.db.execute(f'''SELECT count(*) FROM (
            SELECT {keys} FROM {relation} WHERE source_table={qs(table)} GROUP BY {keys} HAVING count(*)>1)''').fetchone()[0]
        result['sources'][table] = found
    result['availability_statuses'] = store.rows(f'''SELECT availability_status,count(*) AS row_count
        FROM {relation} GROUP BY availability_status ORDER BY availability_status''')
    store.write_json(('verification', 'step8.json'), result)
    return result


if __name__ == '__main__':
    with Store() as store:
        print(json.dumps(check(store), ensure_ascii=False, indent=2))
