"""Report structural, semantic, PIT, lineage and source-coverage violations."""
import hashlib
import json
import logging
from pathlib import Path
import re

from core.calendar.trading import TradingCalendar
from core.data.store import date_sql, json_text, qi, qs
from core.market.prices import date_window


TABLES = {
    'daily_calendar': ('l1', ['trade_date'], {'trade_date': 'DATE', 'prev_trade_date': 'DATE', 'next_trade_date': 'DATE', 'trade_index': 'BIGINT'}),
    'daily_adjusted_price': ('l1', ['trade_date', 'ts_code'], {'trade_date': 'DATE', 'ts_code': 'VARCHAR', 'close': 'DOUBLE', 'adj_factor': 'DOUBLE', 'adjusted_close': 'DOUBLE', 'ret_1d': 'DOUBLE', 'prev_quote_date': 'DATE'}),
    'daily_stock_state': ('l1', ['trade_date', 'ts_code'], {'trade_date': 'DATE', 'ts_code': 'VARCHAR', 'is_listed': 'BOOLEAN', 'is_delisted': 'BOOLEAN', 'is_a_share': 'BOOLEAN', 'is_st': 'BOOLEAN', 'is_star_st': 'BOOLEAN', 'listing_trade_days': 'BIGINT', 'listing_trade_days_lower_bound': 'BIGINT', 'listing_natural_days': 'BIGINT', 'estimated_listing_trade_days': 'BIGINT', 'historical_name': 'VARCHAR', 'historical_state_status': 'VARCHAR'}),
    'daily_trade_status': ('l1', ['trade_date', 'ts_code'], {'trade_date': 'DATE', 'ts_code': 'VARCHAR', 'can_buy': 'BOOLEAN', 'can_sell': 'BOOLEAN', 'is_suspended': 'BOOLEAN', 'is_limit_up': 'BOOLEAN', 'is_limit_down': 'BOOLEAN', 'up_limit': 'DOUBLE', 'down_limit': 'DOUBLE', 'limit_price_exists': 'BOOLEAN'}),
    'financial_available': ('l1', ['source_table', 'record_id'], {'source_table': 'VARCHAR', 'record_id': 'VARCHAR', 'ts_code': 'VARCHAR', 'end_date': 'DATE', 'ann_date': 'DATE', 'f_ann_date': 'DATE', 'effective_ann_date': 'DATE', 'available_date': 'DATE', 'availability_status': 'VARCHAR', 'version': 'BIGINT'}),
    'quarterly_financial': ('l1', ['source_table', 'ts_code', 'metric', 'end_date', 'available_date'], {'source_table': 'VARCHAR', 'ts_code': 'VARCHAR', 'end_date': 'DATE', 'available_date': 'DATE', 'value': 'DOUBLE', 'status': 'VARCHAR', 'version_id': 'VARCHAR', 'source_record_ids': 'VARCHAR[]'}),
    'ttm_financial': ('l1', ['source_table', 'ts_code', 'metric', 'end_date', 'available_date'], {'source_table': 'VARCHAR', 'ts_code': 'VARCHAR', 'end_date': 'DATE', 'available_date': 'DATE', 'value': 'DOUBLE', 'status': 'VARCHAR', 'version_id': 'VARCHAR', 'source_quarter_ids': 'VARCHAR[]'}),
    'research_universe_config': ('l2', ['universe_id'], {'universe_id': 'VARCHAR', 'config_id': 'VARCHAR', 'config_json': 'VARCHAR', 'status': 'VARCHAR'}),
    'daily_research_universe': ('l2', ['trade_date', 'ts_code', 'universe_id'], {'trade_date': 'DATE', 'ts_code': 'VARCHAR', 'universe_id': 'VARCHAR', 'config_id': 'VARCHAR', 'is_in_universe': 'BOOLEAN'}),
}


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


QUALITY_CODE_HASH = file_sha256(Path(__file__))


def semantic_fingerprint(store, relation):
    """Order-independent checksums, comparable with the same DuckDB version."""
    fields = ','.join(qi(c) for c in store.columns(relation))
    return store.rows(f'''SELECT count(*) AS row_count,
        coalesce(sum(hash(ROW({fields}))),0)::VARCHAR AS hash_sum,
        coalesce(bit_xor(hash(ROW({fields}))),0)::VARCHAR AS hash_xor FROM {relation}''')[0]


class Quality:
    def __init__(self, store, start, end, scope, verify_files):
        self.store, self.start, self.end = store, start, end
        self.scope, self.verify_files = scope, verify_files
        self.relations, self.profiles = {}, {}
        self.report = {'passed': False, 'full_phase_passed': False, 'scope': 'supported_data_only',
                       'config_hash': store.config_hash, 'code_hash': store.code_hash,
                       'quality_code_hash': QUALITY_CODE_HASH,
                       'requested_start': start, 'requested_end': end, 'l0_scope': scope,
                       'failures': [], 'tables': {}, 'capabilities': store.rules['capabilities'],
                       'observations': {}, 'acceptance_evidence': {}}

    def fail(self, table, check, fields, samples):
        self.report['failures'].append({'table': table, 'check': check, 'fields': fields, 'samples': samples})

    def check(self, table, check, fields, query):
        logging.info('Checking %s: %s', table, check)
        try:
            samples = self.store.rows(f'SELECT * FROM ({query}) LIMIT 5')
            if samples:
                self.fail(table, check, fields, samples)
        except Exception as error:
            self.fail(table, check, fields, [{'error': str(error)}])

    def l0(self):
        if self.scope not in ('core', 'full'):
            self.fail('l0', 'invalid_scope', [], [{'scope': self.scope}])
            return
        names = [n for n, spec in self.store.tables.items() if self.scope == 'full' or spec.get('stage1')]
        try:
            summary = json.loads(self.store.paths.output('l0', 'profile_summary.json').read_text(encoding='utf-8'))
            if not summary['passed'] or not set(names) <= set(summary['tables']) or (self.scope == 'full' and summary['scope'] != 'full'):
                raise ValueError('Required L0 full/core source scan has not passed')
        except Exception as error:
            self.fail('l0', 'profile_summary', [], [{'error': str(error)}])
        for name in names:
            try:
                spec = self.store.tables[name]
                profile = json.loads(self.store.paths.output('l0', 'profiles', name + '.json').read_text(encoding='utf-8'))
                missing = set(spec['required_fields']) - set(profile['columns'])
                if missing or profile['primary_key'] != spec['primary_key'] or set(profile['dates']) != set(spec['date_fields']):
                    raise ValueError(f'Profile/configuration mismatch; missing fields: {sorted(missing)}')
                if not self.store.paths.output('_provenance', 'configs', profile['config_hash'] + '.json').is_file():
                    raise ValueError('Archived source-profile configuration is missing')
                self.profiles[name] = profile
            except Exception as error:
                self.fail(name, 'l0_profile', self.store.tables[name]['required_fields'], [{'error': str(error)}])
        self.report['observations']['source_issues'] = [
            {'table': name, 'issues': profile['issues']} for name, profile in self.profiles.items() if profile['issues']]

    def tables(self):
        for name, (layer, key, required) in TABLES.items():
            logging.info('Profiling output and verifying integrity: %s', name)
            try:
                relation = self.store.output_relation(layer, name)
                schema = self.store.columns(relation)
                wrong = [{'field': f, 'expected': t, 'actual': schema.get(f)} for f, t in required.items() if schema.get(f) != t]
                if wrong:
                    self.fail(name, 'schema', [r['field'] for r in wrong], wrong)
                manifest = json.loads(self.store.paths.output(layer, name, 'manifest.json').read_text(encoding='utf-8'))
                if manifest['primary_key'] != key or manifest['schema'] != schema:
                    self.fail(name, 'manifest_schema_or_key', key, [{'manifest_key': manifest['primary_key'], 'actual_schema': schema}])
                if manifest.get('config_hash') != self.store.config_hash or manifest.get('code_hash') != self.store.code_hash:
                    self.fail(name, 'stale_configuration_or_code', ['config_hash', 'code_hash'],
                              [{'config_hash': manifest.get('config_hash'), 'code_hash': manifest.get('code_hash')}])
                for upstream, expected_hash in manifest.get('upstream', {}).items():
                    try:
                        upstream_layer, upstream_name = upstream.split('/')
                        upstream_manifest = json.loads(self.store.paths.output(upstream_layer, upstream_name, 'manifest.json').read_text(encoding='utf-8'))
                        if upstream_manifest['sha256'] != expected_hash:
                            self.fail(name, 'stale_upstream', ['upstream'], [{'upstream': upstream, 'expected_sha256': expected_hash, 'actual_sha256': upstream_manifest['sha256']}])
                    except Exception as error:
                        self.fail(name, 'missing_upstream', ['upstream'], [{'upstream': upstream, 'error': str(error)}])
                if self.verify_files:
                    actual_hash = file_sha256(self.store.paths.output(layer, name, 'part-00000.parquet'))
                    if actual_hash != manifest['sha256']:
                        self.fail(name, 'file_sha256', [], [{'expected': manifest['sha256'], 'actual': actual_hash}])
                fields = ','.join(qi(c) for c in key)
                if set(key) <= set(schema):
                    self.check(name, 'duplicate_primary_key', key, f'SELECT {fields},count(*) AS occurrences FROM {relation} GROUP BY {fields} HAVING count(*)>1')
                    self.check(name, 'null_primary_key', key, f"SELECT {fields} FROM {relation} WHERE " + ' OR '.join(f'{qi(c)} IS NULL' for c in key))
                total = self.store.db.execute(f'SELECT count(*) FROM {relation}').fetchone()[0]
                if total != manifest['rows']:
                    self.fail(name, 'manifest_row_count', key, [{'expected': manifest['rows'], 'actual': total}])
                scoped = relation
                if name != 'daily_calendar' and 'trade_date' in schema and not wrong:
                    scoped = f'(SELECT * FROM {relation} WHERE {date_window(self.store,self.start,self.end)})'
                expressions = ['count(*) AS row_count']
                expressions += [f'count(*) FILTER(WHERE {qi(c)} IS NULL) AS n_{i}' for i, c in enumerate(schema)]
                dates = [c for c, dtype in schema.items() if dtype == 'DATE']
                for i, c in enumerate(dates):
                    expressions += [f'min({qi(c)}) AS lo_{i}', f'max({qi(c)}) AS hi_{i}']
                stats = self.store.rows('SELECT ' + ','.join(expressions) + f' FROM {scoped}')[0]
                info = {'schema': schema, 'rows': stats['row_count'], 'stored_rows': total, 'primary_key': key,
                        'null_counts': {c: stats[f'n_{i}'] for i, c in enumerate(schema)},
                        'null_rates': {c: stats[f'n_{i}']/stats['row_count'] if stats['row_count'] else None for i, c in enumerate(schema)},
                        'date_ranges': {c: {'min': stats[f'lo_{i}'], 'max': stats[f'hi_{i}']} for i, c in enumerate(dates)},
                        'sha256': manifest['sha256'], 'fingerprint': semantic_fingerprint(self.store, scoped)}
                self.report['tables'][name] = info
                if not wrong:
                    self.relations[name] = scoped
            except Exception as error:
                self.fail(name, 'unreadable_artifact', list(required), [{'error': str(error)}])

    def market(self):
        s, r = self.store, self.relations
        if 'daily_calendar' not in r:
            return
        c = r['daily_calendar']
        try:
            TradingCalendar.from_store(s)
            source_calendar = TradingCalendar.from_rows(s.rows(f'SELECT exchange,cal_date,is_open FROM {s.raw("trade_cal")}'), s.rules['calendar_exchange'])
            actual_calendar = TradingCalendar.from_store(s)
            if source_calendar != actual_calendar:
                self.fail('daily_calendar', 'source_calendar_coverage', ['trade_date'], [{'error': 'Output calendar differs from raw open dates/bounds'}])
        except Exception as error:
            self.fail('daily_calendar', 'calendar_consistency', ['trade_date'], [{'error': str(error)}])
        for name in ('daily_adjusted_price', 'daily_stock_state', 'daily_trade_status', 'daily_research_universe'):
            if name in r:
                self.check(name, 'non_trading_date', ['trade_date', 'ts_code'], f'SELECT p.trade_date,p.ts_code FROM {r[name]} p ANTI JOIN {c} c USING(trade_date)')
        basic = s.raw('stock_basic')
        for name in ('daily_stock_state', 'daily_trade_status'):
            if name in r:
                self.check(name, 'missing_security_date', ['trade_date', 'ts_code'], f'''SELECT c.trade_date,b.ts_code
                    FROM {c} c CROSS JOIN {basic} b ANTI JOIN {r[name]} x ON x.trade_date=c.trade_date AND x.ts_code=b.ts_code
                    WHERE {date_window(s,self.start,self.end,'c.trade_date')}''')
        if 'daily_adjusted_price' in r:
            p = r['daily_adjusted_price']
            bad_adjusted = ' OR '.join(f'adjusted_{f} IS DISTINCT FROM ({f}*adj_factor)' for f in ('open', 'high', 'low', 'close'))
            self.check('daily_adjusted_price', 'adjustment', ['open', 'high', 'low', 'close', 'adj_factor'],
                       f'SELECT trade_date,ts_code,close,adj_factor,adjusted_close FROM {p} WHERE {bad_adjusted}')
            self.check('daily_adjusted_price', 'return_across_gap', ['ret_1d', 'prev_quote_date'], f'''SELECT p.trade_date,p.ts_code,p.ret_1d,p.prev_quote_date,c.prev_trade_date
                FROM {p} p JOIN {c} c USING(trade_date) WHERE (p.prev_quote_date IS DISTINCT FROM c.prev_trade_date AND ret_1d IS NOT NULL)
                OR (p.prev_quote_date=c.prev_trade_date AND (ret_1d IS NULL OR NOT isfinite(ret_1d)))''')
            raw = s.raw('stk_factor_pro')
            self.check('daily_adjusted_price', 'raw_price_coverage', ['trade_date', 'ts_code'], f'''SELECT {date_sql('x.trade_date')} AS trade_date,x.ts_code
                FROM {raw} x ANTI JOIN {p} p ON p.trade_date={date_sql('x.trade_date')} AND p.ts_code=x.ts_code
                WHERE {date_window(s,self.start,self.end,date_sql('x.trade_date'))}''')
            self.check('daily_adjusted_price', 'invented_quote', ['trade_date', 'ts_code'], f'''SELECT p.trade_date,p.ts_code FROM {p} p
                ANTI JOIN {raw} x ON p.trade_date={date_sql('x.trade_date')} AND p.ts_code=x.ts_code''')
            fields = ['open', 'high', 'low', 'close', 'adj_factor', 'vol', 'amount']
            mismatch = ' OR '.join(f'p.{qi(field)} IS DISTINCT FROM try_cast(x.{qi(field)} AS DOUBLE)' for field in fields)
            self.check('daily_adjusted_price', 'original_source_values', fields, f'''SELECT p.trade_date,p.ts_code,p.close,x.close AS source_close,p.adj_factor,x.adj_factor AS source_factor
                FROM {p} p JOIN {raw} x ON p.trade_date={date_sql('x.trade_date')} AND p.ts_code=x.ts_code WHERE {mismatch}''')
            self.check('daily_adjusted_price', 'return_value', ['ret_1d'], f'''WITH ordered_source AS (
                SELECT ts_code,{date_sql('trade_date')} AS trade_date,
                    try_cast(close AS DOUBLE)*try_cast(adj_factor AS DOUBLE) AS price,
                    lag({date_sql('trade_date')}) OVER w AS prev_date,
                    lag(try_cast(close AS DOUBLE)*try_cast(adj_factor AS DOUBLE)) OVER w AS prev_price
                FROM {raw} WINDOW w AS (PARTITION BY ts_code ORDER BY {date_sql('trade_date')})
            ) SELECT p.trade_date,p.ts_code,p.ret_1d,x.price/x.prev_price-1 AS expected_ret
                FROM {p} p JOIN ordered_source x USING(trade_date,ts_code) JOIN {c} c USING(trade_date)
                WHERE x.prev_date=c.prev_trade_date AND (p.ret_1d IS NULL OR abs(p.ret_1d-(x.price/x.prev_price-1))>1e-12)''')
        if 'daily_stock_state' in r:
            st = r['daily_stock_state']
            listed = f"a.trade_date>={date_sql('b.list_date')} AND ({date_sql('b.delist_date')} IS NULL OR a.trade_date<{date_sql('b.delist_date')})"
            self.check('daily_stock_state', 'historical_listing', ['is_listed', 'is_delisted'], f'''SELECT a.trade_date,a.ts_code,a.is_listed,a.is_delisted,b.list_date,b.delist_date
                FROM {st} a JOIN {basic} b USING(ts_code) WHERE a.is_listed IS DISTINCT FROM ({listed})
                OR a.is_delisted IS DISTINCT FROM ({date_sql('b.delist_date')} IS NOT NULL AND a.trade_date>={date_sql('b.delist_date')})''')
            namechange = s.raw('namechange')
            self.check('daily_stock_state', 'invented_history', ['is_st', 'is_star_st', 'is_delisting_period', 'board', 'historical_state_status'], f'''
                WITH nc AS (
                    SELECT ts_code,
                           upper(nullif(trim(cast({qi('name')} AS VARCHAR)),'')) AS upper_name,
                           {date_sql(qi('start_date'))} AS start_date,
                           {date_sql(qi('end_date'))} AS end_date
                    FROM {namechange}
                ), covering AS (
                    SELECT c.trade_date, n.ts_code,
                           bool_or(contains(n.upper_name,'ST')) AS any_st,
                           bool_and(contains(n.upper_name,'ST')) AS all_st,
                           bool_or(contains(n.upper_name,'*ST')) AS any_star_st,
                           bool_and(contains(n.upper_name,'*ST')) AS all_star_st,
                           count(*) AS covering_intervals
                    FROM {c} c JOIN nc n
                      ON c.trade_date>=n.start_date
                     AND c.trade_date<=coalesce(n.end_date, DATE '9999-12-31')
                    GROUP BY c.trade_date, n.ts_code
                ), expected AS (
                    SELECT a.trade_date, a.ts_code,
                           CASE WHEN NOT a.is_listed THEN NULL
                                WHEN a.is_listed IS NULL THEN NULL
                                WHEN coalesce(v.covering_intervals,0)=0 THEN NULL
                                WHEN v.any_st<>v.all_st THEN NULL ELSE v.any_st END AS exp_is_st,
                           CASE WHEN NOT a.is_listed THEN NULL
                                WHEN a.is_listed IS NULL THEN NULL
                                WHEN coalesce(v.covering_intervals,0)=0 THEN NULL
                                WHEN v.any_star_st<>v.all_star_st THEN NULL ELSE v.any_star_st END AS exp_is_star_st
                    FROM {st} a LEFT JOIN covering v USING(trade_date,ts_code)
                )
                SELECT a.trade_date,a.ts_code,a.is_st,a.is_star_st,a.is_delisting_period,a.board,a.historical_state_status,
                       e.exp_is_st,e.exp_is_star_st
                FROM {st} a JOIN expected e USING(trade_date,ts_code)
                WHERE a.is_st IS DISTINCT FROM e.exp_is_st
                   OR a.is_star_st IS DISTINCT FROM e.exp_is_star_st
                   OR a.is_delisting_period IS NOT NULL OR a.board IS NOT NULL
                   OR (a.is_listed AND a.historical_state_status='namechange_derived' AND a.is_st IS NULL)
                   OR (a.is_listed AND a.is_st IS NOT NULL AND a.historical_state_status<>'namechange_derived')''')
            self.report['observations']['retained_before_delisting'] = s.db.execute(f'''SELECT count(*) FROM {st} a JOIN {basic} b USING(ts_code)
                WHERE a.is_listed AND {date_sql('b.delist_date')} IS NOT NULL AND a.trade_date<{date_sql('b.delist_date')}''').fetchone()[0]
            self.report['observations']['historical_state_status'] = s.rows(
                f'SELECT historical_state_status,count(*) AS row_count FROM {st} GROUP BY 1 ORDER BY 1')
        if 'daily_trade_status' in r:
            t = r['daily_trade_status']
            self.check('daily_trade_status', 'directional_constraints', ['can_buy', 'can_sell'], f'''SELECT trade_date,ts_code,is_listed,is_suspended,is_limit_up,is_limit_down,can_buy,can_sell
                FROM {t} WHERE ((NOT is_listed OR is_suspended OR NOT quotation_exists OR vol<=0) AND
                    (can_buy IS DISTINCT FROM false OR can_sell IS DISTINCT FROM false))
                OR (is_limit_up AND can_buy IS DISTINCT FROM false) OR (is_limit_down AND can_sell IS DISTINCT FROM false)''')
            self.check('daily_trade_status', 'unknown_and_reasons', ['can_buy', 'can_sell', 'cannot_buy_reason', 'cannot_sell_reason'], f'''SELECT trade_date,ts_code,can_buy,can_sell,cannot_buy_reason,cannot_sell_reason
                FROM {t} WHERE (can_buy IS NULL AND coalesce(NOT ends_with(cannot_buy_reason,'_UNKNOWN'),true))
                OR (can_sell IS NULL AND coalesce(NOT ends_with(cannot_sell_reason,'_UNKNOWN'),true))
                OR (can_buy AND cannot_buy_reason IS NOT NULL) OR (can_sell AND cannot_sell_reason IS NOT NULL)
                OR (can_buy AND (is_suspended IS NULL OR is_limit_up IS NULL))
                OR (can_sell AND (is_suspended IS NULL OR is_limit_down IS NULL))''')
            limit_raw = s.raw('stk_limit')
            self.check('daily_trade_status', 'limit_price_sources', ['up_limit', 'down_limit', 'limit_price_exists'], f'''
                WITH valid_raw AS (
                    SELECT {date_sql('x.trade_date')} AS trade_date, x.ts_code,
                           try_cast(x.up_limit AS DOUBLE) AS up_limit,
                           try_cast(x.down_limit AS DOUBLE) AS down_limit
                    FROM {limit_raw} x
                    WHERE try_cast(x.up_limit AS DOUBLE) IS NOT NULL AND try_cast(x.down_limit AS DOUBLE) IS NOT NULL
                      AND isfinite(try_cast(x.up_limit AS DOUBLE)) AND isfinite(try_cast(x.down_limit AS DOUBLE))
                      AND try_cast(x.up_limit AS DOUBLE)>try_cast(x.down_limit AS DOUBLE)
                      AND try_cast(x.down_limit AS DOUBLE)>0
                )
                SELECT t.trade_date,t.ts_code,t.up_limit,t.down_limit,t.limit_price_exists
                FROM {t} t LEFT JOIN valid_raw v ON t.trade_date=v.trade_date AND t.ts_code=v.ts_code
                WHERE (t.limit_price_exists AND (v.ts_code IS NULL OR t.up_limit IS DISTINCT FROM v.up_limit
                          OR t.down_limit IS DISTINCT FROM v.down_limit))
                   OR (NOT t.limit_price_exists AND v.ts_code IS NOT NULL)''')
            self.check('daily_trade_status', 'limit_direction_from_price_evidence', ['is_limit_up', 'is_limit_down', 'up_limit', 'down_limit'], f'''
                SELECT trade_date,ts_code,raw_close,up_limit,down_limit,is_limit_up,is_limit_down
                FROM {t} WHERE limit_price_exists AND quotation_exists
                  AND (is_limit_up IS DISTINCT FROM (raw_close>=up_limit OR list_contains(observed_limit_events,'U'))
                    OR is_limit_down IS DISTINCT FROM (raw_close<=down_limit OR list_contains(observed_limit_events,'D')))''')
            self.report['observations']['trade_unknowns'] = s.rows(f'''SELECT count(*) FILTER(WHERE can_buy IS NULL) AS buy_unknown,
                count(*) FILTER(WHERE can_sell IS NULL) AS sell_unknown FROM {t}''')[0]
            self.report['observations']['limit_price_coverage'] = s.rows(f'''SELECT count(*) FILTER(WHERE limit_price_exists) AS with_limit_price,
                count(*) FILTER(WHERE limit_price_exists IS NOT TRUE AND quotation_exists) AS quoted_without_limit_price FROM {t}''')[0]

    def financial(self):
        s, r = self.store, self.relations
        if 'financial_available' not in r or 'daily_calendar' not in r:
            return
        f, c = r['financial_available'], r['daily_calendar']
        self.check('financial_available', 'original_date_consistency', ['raw_ann_date', 'raw_f_ann_date', 'raw_end_date', 'ann_date', 'f_ann_date', 'end_date'], f'''SELECT source_table,ts_code,end_date,ann_date,f_ann_date,available_date,raw_ann_date,raw_f_ann_date,raw_end_date
            FROM {f} WHERE ann_date IS DISTINCT FROM {date_sql('raw_ann_date')}
                OR f_ann_date IS DISTINCT FROM {date_sql('raw_f_ann_date')}
                OR end_date IS DISTINCT FROM {date_sql('raw_end_date')}
                OR effective_ann_date IS DISTINCT FROM greatest(ann_date,f_ann_date)''')
        self.check('financial_available', 'invalid_record_must_not_be_available', ['availability_status', 'raw_ann_date', 'raw_f_ann_date', 'end_date', 'ts_code'], f'''SELECT source_table,ts_code,end_date,available_date,availability_status,raw_ann_date,raw_f_ann_date
            FROM {f} WHERE availability_status='available' AND (
                ts_code IS NULL OR NOT regexp_full_match(ts_code,'[^.[:space:]]+[.](SH|SZ|BJ)')
                OR end_date IS NULL OR effective_ann_date IS NULL OR effective_ann_date<end_date
                OR (nullif(trim(raw_ann_date),'') IS NOT NULL AND ann_date IS NULL)
                OR (nullif(trim(raw_f_ann_date),'') IS NOT NULL AND f_ann_date IS NULL))''')
        self.check('financial_available', 'strict_announcement_delay', ['ann_date', 'f_ann_date', 'available_date'], f'''SELECT f.source_table,f.ts_code,f.end_date,f.ann_date,f.f_ann_date,f.effective_ann_date,f.available_date,f.availability_status
            FROM {f} f LEFT JOIN {c} c ON c.trade_date=f.available_date
            WHERE (availability_status='available' AND (f.available_date IS NULL OR c.trade_date IS NULL OR effective_ann_date IS NULL
                OR f.available_date<=effective_ann_date OR c.prev_trade_date>effective_ann_date OR effective_ann_date<c.calendar_start
                OR effective_ann_date IS DISTINCT FROM greatest(ann_date,f_ann_date)))
            OR (availability_status<>'available' AND f.available_date IS NOT NULL)''')
        for source in ('income_vip', 'cashflow_vip', 'balancesheet_vip', 'fina_indicator'):
            if source in self.profiles:
                expected = self.profiles[source]['rows']
                original = []
                for field, spec in sorted(self.profiles[source]['columns'].items()):
                    dtype = spec['type']
                    if not re.fullmatch(r'(?:U?(?:BIGINT|SMALLINT|TINYINT|INTEGER)|VARCHAR|DOUBLE|FLOAT|BOOLEAN|DATE|TIMESTAMP(?:_MS|_NS|_S)?|DECIMAL\([0-9]+,\s*[0-9]+\))', dtype):
                        raise ValueError(f'{source}.{field}: unsupported original scalar type for record verification: {dtype}')
                    preserved = 'raw_' + field if field in ('ann_date', 'f_ann_date', 'end_date') else field
                    original.append(f'{qi(field)} := cast({qi(preserved)} AS {dtype})')
                expected_id = f"sha256({qs(source + '|')} || to_json(struct_pack({','.join(original)})))"
                self.check('financial_available', 'original_record_hash', ['record_id'], f'''SELECT source_table,ts_code,end_date,ann_date,record_id
                    FROM {f} WHERE source_table={qs(source)} AND record_id IS DISTINCT FROM {expected_id}''')
                self.check('financial_available', 'source_conservation', ['source_table', 'source_count'], f'''SELECT source_table,sum(source_count) AS source_rows,{expected} AS expected
                    FROM {f} WHERE source_table={qs(source)} GROUP BY source_table HAVING sum(source_count)<>{expected}''')
                count = s.db.execute(f'SELECT coalesce(sum(source_count),0) FROM {f} WHERE source_table={qs(source)}').fetchone()[0]
                if count != expected:
                    self.fail('financial_available', 'source_conservation', ['source_table', 'source_count'], [{'source_table': source, 'expected': expected, 'actual': count}])
        self.report['observations']['financial_availability'] = s.rows(f'SELECT availability_status,count(*) AS row_count FROM {f} GROUP BY availability_status ORDER BY availability_status')
        for name in ('quarterly_financial', 'ttm_financial'):
            if name in r:
                self.check(name, 'value_status', ['value', 'status'], f'''SELECT ts_code,end_date,available_date,value,status FROM {r[name]}
                    WHERE (status='available' AND (value IS NULL OR NOT isfinite(value))) OR (status<>'available' AND value IS NOT NULL)''')
                self.report['observations'][name + '_statuses'] = s.rows(f'SELECT status,count(*) AS row_count FROM {r[name]} GROUP BY status ORDER BY status')
        if 'quarterly_financial' not in r:
            return
        q = r['quarterly_financial']
        allowed, metric_values = [], []
        for source, metrics in s.rules['financial']['cumulative_fields'].items():
            for metric in metrics:
                allowed.append(f'(source_table={qs(source)} AND metric={qs(metric)})')
                metric_values.append(f'WHEN q.source_table={qs(source)} AND q.metric={qs(metric)} THEN try_cast(f.{qi(metric)} AS DOUBLE)')
        self.check('quarterly_financial', 'cumulative_metric_whitelist', ['source_table', 'metric'],
                   f"SELECT ts_code,end_date,available_date,source_table,metric FROM {q} WHERE NOT ({' OR '.join(allowed) or 'false'})")
        self.check('quarterly_financial', 'source_periods', ['source_end_dates', 'end_date'], f'''SELECT ts_code,end_date,available_date,source_end_dates
            FROM {q} WHERE status='available' AND (end_date<>last_day(end_date) OR month(end_date) NOT IN (3,6,9,12)
                OR source_end_dates IS DISTINCT FROM CASE WHEN month(end_date)=3 THEN [end_date]
                   ELSE [last_day(end_date-INTERVAL '3 months'),end_date] END)''')
        self.check('quarterly_financial', 'source_record_lineage', ['source_record_ids', 'available_date'], f'''SELECT q.ts_code,q.metric,q.end_date,q.available_date,
            count(f.record_id) AS matched_records,len(q.source_record_ids) AS expected_records,max(f.available_date) AS max_source_date
            FROM {q} q LEFT JOIN UNNEST(q.source_record_ids) u(record_id) ON true
            LEFT JOIN {f} f ON f.record_id=u.record_id AND f.source_table=q.source_table AND f.ts_code=q.ts_code
            GROUP BY q.version_id,q.ts_code,q.metric,q.end_date,q.available_date,q.source_record_ids
            HAVING count(f.record_id)<>len(q.source_record_ids) OR len(q.source_record_ids)=0 OR max(f.available_date) IS DISTINCT FROM q.available_date''')
        self.check('quarterly_financial', 'latest_visible_source_versions', ['source_record_ids', 'source_end_dates', 'available_date'], f'''WITH visible AS (
            SELECT q.version_id,q.ts_code,q.metric,q.end_date,q.available_date,q.source_record_ids,d.period,f.record_id,
                dense_rank() OVER(PARTITION BY q.version_id,d.period ORDER BY f.effective_ann_date DESC NULLS LAST) AS recency
            FROM {q} q CROSS JOIN UNNEST(q.source_end_dates) d(period)
            LEFT JOIN {f} f ON f.source_table=q.source_table AND f.ts_code=q.ts_code AND f.end_date=d.period
                AND f.available_date<=q.available_date AND trim(cast(f.report_type AS VARCHAR))='1'
        ) SELECT ts_code,metric,end_date,available_date,source_record_ids,
            coalesce(list(record_id ORDER BY record_id) FILTER(WHERE record_id IS NOT NULL),[]::VARCHAR[]) AS expected_record_ids
            FROM visible WHERE recency=1 GROUP BY version_id,ts_code,metric,end_date,available_date,source_record_ids
            HAVING source_record_ids IS DISTINCT FROM coalesce(list(record_id ORDER BY record_id) FILTER(WHERE record_id IS NOT NULL),[]::VARCHAR[])''')
        value_case = 'CASE ' + ' '.join(metric_values) + ' ELSE NULL END'
        self.check('quarterly_financial', 'cumulative_difference_value', ['value', 'source_record_ids'], f'''WITH metrics AS (
            SELECT q.version_id,q.ts_code,q.metric,q.end_date,q.available_date,q.value,q.source_end_dates,f.end_date AS source_end_date,
                {value_case} AS cumulative_value
            FROM {q} q CROSS JOIN UNNEST(q.source_record_ids) u(record_id)
            LEFT JOIN {f} f ON f.record_id=u.record_id AND f.source_table=q.source_table AND f.ts_code=q.ts_code
            WHERE q.status='available'
        ), periods AS (
            SELECT version_id,ts_code,metric,end_date,available_date,value,source_end_dates,source_end_date,
                max(cumulative_value) AS cumulative_value,
                count(DISTINCT cumulative_value)<>1 OR count(cumulative_value)<>count(*)
                    OR count(*) FILTER(WHERE NOT isfinite(cumulative_value))>0 AS conflict
            FROM metrics GROUP BY ALL
        ) SELECT ts_code,metric,end_date,available_date,value,
            sum(CASE WHEN source_end_date=end_date THEN cumulative_value ELSE -cumulative_value END) AS expected_value
            FROM periods GROUP BY version_id,ts_code,metric,end_date,available_date,value,source_end_dates
            HAVING bool_or(conflict) OR list(source_end_date ORDER BY source_end_date)<>source_end_dates
                OR abs(value-sum(CASE WHEN source_end_date=end_date THEN cumulative_value ELSE -cumulative_value END))
                    >1e-12*greatest(1,sum(abs(cumulative_value)))''')
        if 'ttm_financial' not in r:
            return
        t = r['ttm_financial']
        self.check('ttm_financial', 'four_source_quarters', ['source_quarter_ids', 'source_end_dates'], f'''SELECT ts_code,metric,end_date,available_date,source_quarter_ids,source_end_dates
            FROM {t} WHERE status='available' AND (len(source_quarter_ids)<>4 OR len(list_distinct(source_quarter_ids))<>4 OR len(source_end_dates)<>4
            OR date_diff('month',source_end_dates[1],source_end_dates[2])<>3 OR date_diff('month',source_end_dates[2],source_end_dates[3])<>3
            OR date_diff('month',source_end_dates[3],source_end_dates[4])<>3 OR source_end_dates[4]<>end_date)''')
        self.check('ttm_financial', 'quarter_lineage_and_sum', ['source_quarter_ids', 'available_date', 'value'], f'''SELECT t.ts_code,t.metric,t.end_date,t.available_date,t.value,t.status,
            count(q.version_id) AS matched_quarters,len(t.source_quarter_ids) AS expected_quarters,max(q.available_date) AS max_source_date,
            list(q.end_date ORDER BY u.position) AS actual_end_dates,t.source_end_dates,sum(q.value) AS expected_value
            FROM {t} t CROSS JOIN UNNEST(t.source_quarter_ids) WITH ORDINALITY u(version_id,position)
            LEFT JOIN {q} q ON q.version_id=u.version_id AND q.source_table=t.source_table AND q.ts_code=t.ts_code AND q.metric=t.metric
            GROUP BY t.version_id,t.ts_code,t.metric,t.end_date,t.available_date,t.value,t.status,t.source_quarter_ids,t.source_end_dates
            HAVING count(q.version_id)<>len(t.source_quarter_ids) OR max(q.available_date) IS DISTINCT FROM t.available_date
            OR (t.status='available' AND (count(*) FILTER(WHERE q.status<>'available')>0 OR list(q.end_date ORDER BY u.position)<>t.source_end_dates
                OR abs(t.value-sum(q.value))>1e-12*greatest(1,sum(abs(q.value)))))''')
        self.check('ttm_financial', 'latest_visible_quarter_versions', ['source_quarter_ids', 'available_date'], f'''SELECT t.ts_code,t.metric,t.end_date,t.available_date,
            old.end_date AS source_end_date,old.version_id AS used_version,newer.version_id AS newer_visible_version
            FROM {t} t CROSS JOIN UNNEST(t.source_quarter_ids) u(version_id)
            JOIN {q} old ON old.version_id=u.version_id AND old.source_table=t.source_table AND old.ts_code=t.ts_code AND old.metric=t.metric
            JOIN {q} newer ON newer.source_table=old.source_table AND newer.ts_code=old.ts_code AND newer.metric=old.metric AND newer.end_date=old.end_date
                AND newer.available_date<=t.available_date AND newer.available_date>old.available_date''')
        self.check('ttm_financial', 'original_record_lineage', ['source_record_ids', 'source_quarter_ids'], f'''SELECT t.ts_code,t.metric,t.end_date,t.available_date,t.source_record_ids,
            list(DISTINCT u.record_id ORDER BY u.record_id) AS expected_source_records
            FROM {t} t CROSS JOIN UNNEST(t.source_quarter_ids) ids(version_id)
            JOIN {q} q ON q.version_id=ids.version_id AND q.source_table=t.source_table AND q.ts_code=t.ts_code AND q.metric=t.metric
            CROSS JOIN UNNEST(q.source_record_ids) u(record_id)
            GROUP BY t.version_id,t.ts_code,t.metric,t.end_date,t.available_date,t.source_record_ids
            HAVING t.source_record_ids IS DISTINCT FROM list(DISTINCT u.record_id ORDER BY u.record_id)''')

    def universe(self):
        s, r = self.store, self.relations
        if not {'daily_research_universe', 'research_universe_config', 'daily_stock_state', 'daily_trade_status'} <= set(r):
            return
        u, cfg, st, t = (r[n] for n in ('daily_research_universe', 'research_universe_config', 'daily_stock_state', 'daily_trade_status'))
        self.check('daily_research_universe', 'config_trace', ['universe_id', 'config_id'], f'''SELECT u.trade_date,u.ts_code,u.universe_id,u.config_id,c.status
            FROM {u} u LEFT JOIN {cfg} c USING(universe_id,config_id) WHERE c.status IS DISTINCT FROM 'active' ''')
        for row in s.rows(f'SELECT * FROM {cfg}'):
            expected = hashlib.sha256(row['config_json'].encode('utf-8')).hexdigest()
            if row['config_id'] != expected or (row['exclude_st'] and row['status'] != 'active'):
                self.fail('research_universe_config', 'rule_identity_or_deferral', ['config_id', 'status'], [row])
        self.check('daily_research_universe', 'missing_security_date', ['trade_date', 'ts_code', 'universe_id'], f'''SELECT s.trade_date,s.ts_code,c.universe_id
            FROM {st} s CROSS JOIN {cfg} c ANTI JOIN {u} u ON u.trade_date=s.trade_date AND u.ts_code=s.ts_code AND u.universe_id=c.universe_id
            WHERE c.status='active' ''')
        expected = '''CASE WHEN NOT s.is_listed OR s.is_delisted OR NOT s.is_a_share THEN false
            WHEN s.is_listed IS NULL OR s.is_a_share IS NULL THEN NULL
            WHEN c.exclude_bj AND ends_with(s.ts_code,'.BJ') THEN false
            WHEN c.exclude_st AND s.is_st THEN false
            WHEN c.exclude_st AND s.is_st IS NULL THEN NULL
            WHEN c.min_listing_trade_days=0 THEN true
            WHEN s.listing_trade_days IS NOT NULL THEN s.listing_trade_days>=c.min_listing_trade_days
            WHEN s.listing_trade_days_lower_bound>=c.min_listing_trade_days THEN true ELSE NULL END'''
        self.check('daily_research_universe', 'historical_membership', ['is_in_universe'], f'''SELECT u.trade_date,u.ts_code,u.universe_id,u.is_in_universe,
            s.is_listed,s.is_delisted,s.is_a_share FROM {u} u JOIN {st} s USING(trade_date,ts_code) JOIN {cfg} c USING(universe_id)
            WHERE u.is_in_universe IS DISTINCT FROM ({expected})''')
        self.check('daily_research_universe', 'trade_facts_preserved', ['can_buy', 'can_sell'], f'''SELECT u.trade_date,u.ts_code,u.universe_id,u.can_buy,u.can_sell
            FROM {u} u JOIN {t} t USING(trade_date,ts_code) WHERE u.can_buy IS DISTINCT FROM t.can_buy OR u.can_sell IS DISTINCT FROM t.can_sell''')
        self.report['observations']['universe_statuses'] = s.rows(f'SELECT universe_id,status,deferred_reason FROM {cfg} ORDER BY universe_id')

    def finish(self):
        report = self.report
        report['passed'] = not report['failures']
        for label, filename in (('repeatability', 'repeatability.json'), ('server', 'server_validation.json'), ('raw_unchanged', 'raw_unchanged.json')):
            path = self.store.paths.output('verification', filename)
            report['acceptance_evidence'][label] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'status': 'not_run'}
        report['acceptance_evidence_complete'] = all(v.get('passed') is True for v in report['acceptance_evidence'].values())
        report['full_phase_passed'] = (report['passed'] and self.scope == 'full' and report['acceptance_evidence_complete']
            and not any(v['status'] in ('deferred', 'limited') for v in report['capabilities'].values()))
        self.store.write_json(('l2', 'data_quality_report', 'report.json'), report)
        lines = ['# 第一阶段数据质量报告', '', f"可用范围检查：{'通过' if report['passed'] else '失败'}。完整阶段准入：{'通过' if report['full_phase_passed'] else '未通过（仍有挂起/受限能力）'}。", '',
                 f"配置指纹：`{report['config_hash']}`；代码指纹：`{report['code_hash']}`。", '',
                 '| 核心产物 | 范围内行数 | 主键 |', '|---|---:|---|']
        for name, item in report['tables'].items():
            lines.append(f"| {name} | {item['rows']:,} | {' + '.join(item['primary_key'])} |")
        lines += ['', '## 挂起与受限能力', '']
        for name, capability in report['capabilities'].items():
            if capability['status'] in ('deferred', 'limited'):
                lines.append(f"- **{name}**（{capability['status']}）：{capability['reason']}")
        lines += ['', '## 检查失败', '']
        if not report['failures']:
            lines.append('无。缺失源、金额或方向未知仍在上述能力和 JSON 字段统计中披露，不能当成完整覆盖。')
        for failure in report['failures']:
            lines += [f"- **{failure['table']} / {failure['check']}**；字段：{', '.join(failure['fields'])}",
                      f"  样例：`{json.dumps(failure['samples'], ensure_ascii=False, default=str)}`"]
        lines += ['', '## 验收证据', '', '```json', json_text(report['acceptance_evidence']).rstrip(), '```', '',
                  '逐字段类型、空值数量/比例、日期范围、内容校验值和原始异常样例详见同目录 report.json。',
                  '未实现因子、股票池、策略信号或回测；历史 ST 等缺源项挂起，不宣称已达到完整第二阶段准入条件。', '']
        self.store.paths.output('l2', 'data_quality_report', 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')
        if not report['passed']:
            raise ValueError(f"Data quality failed: {len(report['failures'])} checks; see l2/data_quality_report/report.json")
        return report


def check_data_quality(store, start=None, end=None, l0_scope='core', verify_files=True):
    quality = Quality(store, start, end, l0_scope, verify_files)
    quality.l0()
    quality.tables()
    for name in ('market', 'financial', 'universe'):
        try:
            getattr(quality, name)()
        except Exception as error:
            quality.fail(name, 'validation_exception', [], [{'error': str(error)}])
    return quality.finish()


def check_component(store, component, start=None, end=None, l0_scope='core'):
    """Run the current step's semantic gate before its successor can execute."""
    dependencies = {
        'daily_calendar': ['daily_calendar'],
        'daily_adjusted_price': ['daily_calendar', 'daily_adjusted_price'],
        'daily_stock_state': ['daily_calendar', 'daily_stock_state'],
        'daily_trade_status': ['daily_calendar', 'daily_stock_state', 'daily_trade_status'],
        'financial_available': ['daily_calendar', 'financial_available'],
        'quarterly_and_ttm': ['daily_calendar', 'financial_available', 'quarterly_financial', 'ttm_financial'],
        'research_universe': ['daily_stock_state', 'daily_trade_status', 'research_universe_config', 'daily_research_universe'],
    }
    if component not in dependencies:
        raise ValueError(f'Unknown component gate: {component}')
    quality = Quality(store, start, end, l0_scope, True)
    quality.l0()
    for name in dependencies[component]:
        relation = store.output_relation(TABLES[name][0], name)
        if name != 'daily_calendar' and 'trade_date' in store.columns(relation):
            relation = f'(SELECT * FROM {relation} WHERE {date_window(store,start,end)})'
        quality.relations[name] = relation
    method = 'universe' if component == 'research_universe' else 'financial' if component in ('financial_available', 'quarterly_and_ttm') else 'market'
    try:
        getattr(quality, method)()
    except Exception as error:
        quality.fail(component, 'validation_exception', [], [{'error': str(error)}])
    result = {'component': component, 'passed': not quality.report['failures'],
              'config_hash': store.config_hash, 'code_hash': store.code_hash,
              'failures': quality.report['failures'], 'observations': quality.report['observations']}
    store.write_json(('verification', 'stages', component + '.json'), result)
    if not result['passed']:
        raise ValueError(f'{component} semantic gate failed: {json_text(result["failures"])}')
    return result
