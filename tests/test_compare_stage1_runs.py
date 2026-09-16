"""Small saved-evidence CLI check; no real input or derived data is touched."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys


def test_saved_run_comparison_checks_required_identity_and_outputs(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = root / 'scripts' / 'compare_stage1_runs.py'
    names = (
        'daily_calendar', 'daily_adjusted_price', 'daily_stock_state', 'daily_trade_status',
        'financial_available', 'quarterly_financial', 'ttm_financial',
        'research_universe_config', 'daily_research_universe',
    )
    table = {'schema': {'id': 'VARCHAR', 'value': 'DOUBLE'}, 'rows': 2, 'stored_rows': 2,
             'primary_key': ['id'], 'fingerprint': {'row_count': 2, 'hash_sum': '35', 'hash_xor': '17'},
             'sha256': 'a' * 64}
    run = {'passed': True, 'code_hash': 'b' * 64, 'config_hash': 'c' * 64,
           'raw_signature': {'signature': 'd' * 64, 'file_count': 9, 'total_bytes': 900},
           'start': None, 'end': None, 'l0_scope': 'full',
           'l0_profiles': {f'source_{i:02d}': '3' * 64 for i in range(69)},
           'stages': [{'stage': stage, 'passed': True} for stage in (
               'L0', 'daily_calendar', 'daily_adjusted_price', 'daily_stock_state',
               'daily_trade_status', 'financial_available', 'quarterly_and_ttm', 'research_universe')],
           'tables': {name: copy.deepcopy(table) for name in names}}
    raw, middle = tmp_path / 'raw', tmp_path / 'middle'
    raw.mkdir()
    runs = middle / 'verification' / 'runs'
    runs.mkdir(parents=True)
    first, second = runs / 'first.json', runs / 'second.json'
    first.write_text(json.dumps(run), encoding='utf-8')
    env = dict(os.environ, DATA_ROOT=str(raw), MIDDLE_ROOT=str(middle))

    def compare(candidate, *flags):
        second.write_text(json.dumps(candidate), encoding='utf-8')
        result = subprocess.run([sys.executable, str(script), 'first', 'second', *flags],
                                cwd=root, env=env, capture_output=True, text=True)
        proof = json.loads((middle / 'verification' / 'repeatability.json').read_text(encoding='utf-8'))
        return result, proof

    result, proof = compare(run)
    assert result.returncode == 0, result.stderr
    assert proof['passed'] is True and proof['differences'] == []
    assert proof['file_sha256_compared'] is True
    assert [item['path'] for item in proof['evidence']] == [str(first.resolve()), str(second.resolve())]
    assert all(len(item['sha256']) == 64 for item in proof['evidence'])

    changed = copy.deepcopy(run)
    changed['passed'] = False
    changed['code_hash'], changed['config_hash'] = 'e' * 64, 'f' * 64
    changed['raw_signature']['signature'] = '0' * 64
    changed['l0_scope'], changed['start'], changed['end'] = 'core', '20190101', '20190131'
    changed['tables']['daily_calendar']['schema']['value'] = 'VARCHAR'
    changed['tables']['daily_adjusted_price']['rows'] = 3
    changed['tables']['daily_stock_state']['primary_key'] = ['id', 'value']
    changed['tables']['daily_trade_status']['fingerprint']['hash_sum'] = '36'
    changed['tables']['financial_available']['sha256'] = '1' * 64
    changed['l0_profiles']['source_00'] = '4' * 64
    changed['stages'][2]['passed'] = False
    changed['stages'].pop()
    del changed['tables']['ttm_financial']
    result, proof = compare(changed)
    assert result.returncode == 1, result.stderr
    assert proof['passed'] is False
    fields = {item['field'] for item in proof['differences']}
    assert {'second.passed', 'code_hash', 'config_hash', 'raw_signature', 'l0_scope', 'start', 'end',
            'tables.daily_calendar.schema', 'tables.daily_adjusted_price.rows',
            'tables.daily_stock_state.primary_key', 'tables.daily_trade_status.fingerprint',
            'tables.financial_available.sha256', 'second.tables.ttm_financial', 'l0_profiles',
            'second.stages.daily_adjusted_price.passed', 'second.stages.required'} <= fields

    changed = copy.deepcopy(run)
    changed['tables']['daily_calendar']['sha256'] = '2' * 64
    result, proof = compare(changed, '--cross-platform')
    assert result.returncode == 0, result.stderr
    assert proof['passed'] is True
    assert proof['file_sha256_compared'] is False
    assert first.read_text(encoding='utf-8') == json.dumps(run)

    for field, invalid in (('l0_profiles', {}), ('l0_profiles', {'source_00': 'invalid'}),
                           ('l0_profiles', {f'source_{i:02d}': '3' * 64 for i in range(68)}),
                           ('stages', [])):
        changed = copy.deepcopy(run)
        changed[field] = invalid
        result, proof = compare(changed)
        assert result.returncode == 1, (field, result.stderr)
        assert any(item['field'].startswith(f'second.{field}') for item in proof['differences'])

    core_run = copy.deepcopy(run)
    core_run['l0_scope'] = 'core'
    core_run['l0_profiles'] = {f'source_{i:02d}': '3' * 64 for i in range(9)}
    first.write_text(json.dumps(core_run), encoding='utf-8')
    result, proof = compare(core_run)
    assert result.returncode == 0, result.stderr
    assert proof['passed'] is True

    local_runs = middle / 'verification' / 'local_runs'
    local_runs.mkdir()
    local = dict(copy.deepcopy(run), scope='non_documentation_raw_files', full_pipeline_passed=False)
    (local_runs / 'first.json').write_text(json.dumps(local), encoding='utf-8')

    def compare_local(candidate):
        (local_runs / 'second.json').write_text(json.dumps(candidate), encoding='utf-8')
        result = subprocess.run([sys.executable, str(script), 'first', 'second', '--local-validation'],
                                cwd=root, env=env, capture_output=True, text=True)
        proof = json.loads((middle / 'verification' / 'repeatability.json').read_text(encoding='utf-8'))
        return result, proof

    result, proof = compare_local(local)
    assert result.returncode == 0, result.stderr
    assert proof['passed'] is True and proof['scope'] == 'non_documentation_raw_files'
    assert proof['strict_full_directory_passed'] is False
    assert all('local_runs' in item['path'] for item in proof['evidence'])
    for field, invalid in (('scope', 'all_raw_files'), ('full_pipeline_passed', True)):
        result, proof = compare_local(dict(local, **{field: invalid}))
        assert result.returncode == 1, result.stderr
        assert any(item['field'] == f'second.{field}' for item in proof['differences'])
