"""Configured identity membership with explicit deferred and unknown results."""
import hashlib

import pyarrow as pa

from core.data.store import json_text
from core.market.prices import date_window
from core.market.trading import require_date_coverage


def build_research_universe(store, start=None, end=None):
    rules = store.rules['universe_rules']
    if not isinstance(rules, list) or not rules:
        raise ValueError('universe_rules must contain at least one rule')
    configs, seen = [], set()
    historical_st = store.rules['capabilities']['historical_st']
    for rule in rules:
        expected = {'universe_id', 'exclude_st', 'exclude_bj', 'min_listing_trade_days'}
        if set(rule) != expected:
            raise ValueError(f'Unexpected/missing research-universe configuration fields: {rule}')
        name = rule['universe_id']
        minimum = rule['min_listing_trade_days']
        if not isinstance(name, str) or not name.strip() or name in seen:
            raise ValueError(f'Invalid/duplicate universe_id: {name}')
        seen.add(name)
        if any(not isinstance(rule[k], bool) for k in ('exclude_st', 'exclude_bj')):
            raise ValueError(f'{name}: exclusion flags must be boolean')
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
            raise ValueError(f'{name}: min_listing_trade_days must be a nonnegative integer')
        if rule['exclude_st'] and historical_st['status'] != 'deferred':
            raise ValueError('Historical ST filtering is not implemented: interval source remains deferred')
        payload = {'engine': 'stage1_v1', 'rule': rule,
                   'listing_age_policy': 'exact_or_confirmed_lower_bound_else_unknown',
                   'historical_st': historical_st if rule['exclude_st'] else 'not_required'}
        serialized = json_text(payload)
        configs.append({**rule, 'config_id': hashlib.sha256(serialized.encode('utf-8')).hexdigest(),
                        'config_json': serialized, 'status': 'deferred' if rule['exclude_st'] else 'active',
                        'deferred_reason': historical_st['reason'] if rule['exclude_st'] else None})
    require_date_coverage(store, 'daily_stock_state', start, end)
    require_date_coverage(store, 'daily_trade_status', start, end)
    states = store.output_relation('l1', 'daily_stock_state')
    trades = store.output_relation('l1', 'daily_trade_status')
    missing = store.rows(f'''SELECT s.trade_date,s.ts_code FROM {states} s ANTI JOIN {trades} t
        USING(trade_date,ts_code) WHERE {date_window(store,start,end,'s.trade_date')} LIMIT 10''')
    if missing:
        raise ValueError(f'daily_trade_status: incomplete upstream coverage: {missing}')
    config_schema = pa.schema([
        ('universe_id', pa.string()), ('exclude_st', pa.bool_()), ('exclude_bj', pa.bool_()),
        ('min_listing_trade_days', pa.int64()), ('config_id', pa.string()), ('config_json', pa.string()),
        ('status', pa.string()), ('deferred_reason', pa.string())])
    store.db.register('configured_universes', pa.Table.from_pylist(configs, schema=config_schema))
    config_manifest = store.publish('l2', 'research_universe_config', 'SELECT * FROM configured_universes', ['universe_id'])
    config_relation = store.output_relation('l2', 'research_universe_config')
    query = f'''WITH classified AS (
        SELECT s.trade_date,s.ts_code,r.universe_id,r.config_id,
            s.is_listed,s.is_delisted,s.is_a_share,s.exchange,
            s.listing_trade_days,s.listing_trade_days_lower_bound,
            t.can_buy,t.can_sell,t.cannot_buy_reason,t.cannot_sell_reason,
            s.source_file AS source_stock_file,
            CASE WHEN s.is_delisted THEN 'DELISTED' WHEN NOT s.is_listed THEN 'NOT_LISTED'
                 WHEN NOT s.is_a_share THEN 'NOT_A_SHARE'
                 WHEN s.is_listed IS NULL OR s.is_a_share IS NULL THEN 'IDENTITY_UNKNOWN'
                 WHEN r.exclude_bj AND ends_with(s.ts_code,'.BJ') THEN 'EXCLUDED_BJ'
                 WHEN r.min_listing_trade_days>0 AND s.listing_trade_days<r.min_listing_trade_days
                    THEN 'IPO_TOO_RECENT'
                 WHEN r.min_listing_trade_days>0 AND s.listing_trade_days IS NULL
                    AND (s.listing_trade_days_lower_bound IS NULL
                         OR s.listing_trade_days_lower_bound<r.min_listing_trade_days)
                    THEN 'LISTING_AGE_UNKNOWN'
                 ELSE NULL END AS exclusion_reason
        FROM {states} s JOIN {trades} t USING(trade_date,ts_code)
        CROSS JOIN {config_relation} r
        WHERE r.status='active' AND {date_window(store,start,end,'s.trade_date')}
    ) SELECT *,CASE WHEN exclusion_reason IS NULL THEN true
                    WHEN exclusion_reason IN ('IDENTITY_UNKNOWN','LISTING_AGE_UNKNOWN') THEN NULL
                    ELSE false END AS is_in_universe FROM classified'''
    universe_manifest = store.publish('l2', 'daily_research_universe', query, ['trade_date', 'ts_code', 'universe_id'])
    return {'config': config_manifest, 'universe': universe_manifest}
