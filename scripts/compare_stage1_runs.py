"""Compare two saved Stage 1 runs and publish repeatability evidence."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path

import _bootstrap  # noqa: F401
from core.config import load_paths


CORE_TABLES = (
    'daily_calendar', 'daily_adjusted_price', 'daily_stock_state', 'daily_trade_status',
    'financial_available', 'quarterly_financial', 'ttm_financial',
    'research_universe_config', 'daily_research_universe',
)
RUN_FIELDS = ('code_hash', 'config_hash', 'raw_signature', 'l0_scope', 'start', 'end', 'l0_profiles')
REQUIRED_STAGES = (
    'L0', 'daily_calendar', 'daily_adjusted_price', 'daily_stock_state',
    'daily_trade_status', 'financial_available', 'quarterly_and_ttm', 'research_universe',
)
TABLE_FIELDS = ('schema', 'rows', 'stored_rows', 'primary_key', 'fingerprint')


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _count(value):
    return type(value) is int and value >= 0


def compare_runs(first_path, second_path, output_path, *, same_platform=True, local_validation=False):
    """Read evidence only; never inspect or modify raw/derived table files."""
    paths = [Path(p).resolve() for p in (first_path, second_path)]
    report = {
        'passed': False,
        'method': 'Strict saved-run identity, input metadata and semantic output comparison',
        'same_platform': same_platform,
        'file_sha256_compared': same_platform,
        'evidence': [],
        'compared_run_fields': list(RUN_FIELDS),
        'compared_table_fields': list(TABLE_FIELDS) + (['sha256'] if same_platform else []),
        'tables': list(CORE_TABLES),
        'required_stages': list(REQUIRED_STAGES),
        'differences': [],
    }
    if local_validation:
        report.update({
            'scope': 'non_documentation_raw_files',
            'strict_full_directory_passed': False,
            'scope_note': '仅比较排除 data/ 根目录 Markdown 文件后的原始文件指纹与九张产物；'
                          '严格全目录核验的失败仍保留，本报告不表示完整流水线通过。',
        })

    def fail(field, first=None, second=None, reason='different'):
        report['differences'].append({'field': field, 'first': first, 'second': second, 'reason': reason})

    if paths[0] == paths[1]:
        fail('evidence', str(paths[0]), str(paths[1]), 'Two distinct run evidence files are required')
    runs = []
    for label, path in zip(('first', 'second'), paths):
        proof = {'run': label, 'path': str(path)}
        try:
            content = path.read_bytes()
            proof['sha256'] = hashlib.sha256(content).hexdigest()
            run = json.loads(content)
            if not isinstance(run, dict):
                raise ValueError('Run evidence must be a JSON object')
            proof['identity'] = {field: run.get(field) for field in RUN_FIELDS}
            proof['passed'] = run.get('passed')
            if local_validation:
                proof['scope'] = run.get('scope')
                proof['full_pipeline_passed'] = run.get('full_pipeline_passed')
            runs.append(run)
        except (OSError, ValueError) as error:
            fail(label, reason=f'Cannot read run evidence: {error}')
            runs.append(None)
        report['evidence'].append(proof)

    for label, run in zip(('first', 'second'), runs):
        if run is None:
            continue
        if run.get('passed') is not True:
            fail(f'{label}.passed', reason='The recorded build must have passed')
        if local_validation:
            if run.get('scope') != 'non_documentation_raw_files':
                fail(f'{label}.scope', reason='Local validation must be limited to non_documentation_raw_files')
            if run.get('full_pipeline_passed') is not False:
                fail(f'{label}.full_pipeline_passed', reason='Local validation must explicitly retain full_pipeline_passed=false')
        for field in RUN_FIELDS:
            if field not in run:
                fail(f'{label}.{field}', reason='Required run field is missing')
        for field in ('code_hash', 'config_hash'):
            if not _sha(run.get(field)):
                fail(f'{label}.{field}', reason='Expected a SHA-256 string')
        raw = run.get('raw_signature')
        if (not isinstance(raw, dict) or not _sha(raw.get('signature'))
                or not _count(raw.get('file_count')) or not _count(raw.get('total_bytes'))):
            fail(f'{label}.raw_signature', reason='Source signature, file count and total bytes are required')
        if run.get('l0_scope') not in ('core', 'full'):
            fail(f'{label}.l0_scope', reason='Expected core or full')
        profiles = run.get('l0_profiles')
        if (not isinstance(profiles, dict) or not profiles
                or any(not isinstance(name, str) or not name.strip() or not _sha(digest)
                       for name, digest in profiles.items())):
            fail(f'{label}.l0_profiles', reason='Expected a nonempty profile-name-to-SHA-256 mapping')
        expected_profiles = 9 if run.get('l0_scope') == 'core' else 69 if run.get('l0_scope') == 'full' else None
        if isinstance(profiles, dict) and expected_profiles is not None and len(profiles) != expected_profiles:
            fail(f'{label}.l0_profiles.count', len(profiles), expected_profiles,
                 'Profile count must match the recorded L0 scope')
        stages = run.get('stages')
        if not isinstance(stages, list) or not stages:
            fail(f'{label}.stages', reason='All required build-stage results are required')
        else:
            seen = set()
            for index, stage in enumerate(stages):
                if not isinstance(stage, dict) or not isinstance(stage.get('stage'), str) or not stage['stage']:
                    fail(f'{label}.stages[{index}]', reason='Expected a named stage result')
                    continue
                name = stage['stage']
                if name in seen:
                    fail(f'{label}.stages.{name}', reason='Duplicate stage result')
                seen.add(name)
                if stage.get('passed') is not True:
                    fail(f'{label}.stages.{name}.passed', reason='Every recorded stage must have passed')
            missing = sorted(set(REQUIRED_STAGES) - seen)
            if missing:
                fail(f'{label}.stages.required', missing, list(REQUIRED_STAGES), 'Required build stages are missing')
        tables = run.get('tables')
        if not isinstance(tables, dict):
            fail(f'{label}.tables', reason='Required table evidence is missing')
            continue
        if set(tables) != set(CORE_TABLES):
            fail(f'{label}.tables', sorted(tables), sorted(CORE_TABLES), 'Expected the nine core tables')
        for table in CORE_TABLES:
            entry = tables.get(table)
            prefix = f'{label}.tables.{table}'
            if not isinstance(entry, dict):
                fail(prefix, reason='Required table evidence is missing')
                continue
            for field in TABLE_FIELDS + ('sha256',):
                if field not in entry:
                    fail(f'{prefix}.{field}', reason='Required table field is missing')
            schema, key = entry.get('schema'), entry.get('primary_key')
            if (not isinstance(schema, dict) or not schema
                    or any(not isinstance(k, str) or not isinstance(v, str) for k, v in schema.items())):
                fail(f'{prefix}.schema', reason='Expected a nonempty field-to-type mapping')
            if (not isinstance(key, list) or not key or any(not isinstance(k, str) for k in key)
                    or len(set(key)) != len(key) or not isinstance(schema, dict) or not set(key) <= set(schema)):
                fail(f'{prefix}.primary_key', reason='Expected unique key fields present in the schema')
            for field in ('rows', 'stored_rows'):
                if not _count(entry.get(field)):
                    fail(f'{prefix}.{field}', reason='Expected a nonnegative integer')
            fingerprint = entry.get('fingerprint')
            if (not isinstance(fingerprint, dict) or not _count(fingerprint.get('row_count'))
                    or any(not isinstance(fingerprint.get(k), str) or not fingerprint[k].isdigit()
                           for k in ('hash_sum', 'hash_xor'))):
                fail(f'{prefix}.fingerprint', reason='Expected row_count, hash_sum and hash_xor')
            elif fingerprint['row_count'] != entry.get('rows'):
                fail(f'{prefix}.fingerprint.row_count', fingerprint['row_count'], entry.get('rows'), 'Fingerprint and scoped row count disagree')
            if not _sha(entry.get('sha256')):
                fail(f'{prefix}.sha256', reason='Expected a SHA-256 string')

    if all(run is not None for run in runs):
        first, second = runs
        for field in RUN_FIELDS:
            if field in first and field in second and first[field] != second[field]:
                fail(field, first[field], second[field])
        a_tables, b_tables = first.get('tables'), second.get('tables')
        if isinstance(a_tables, dict) and isinstance(b_tables, dict):
            for table in CORE_TABLES:
                a, b = a_tables.get(table), b_tables.get(table)
                if not isinstance(a, dict) or not isinstance(b, dict):
                    continue
                for field in report['compared_table_fields']:
                    if field in a and field in b and a[field] != b[field]:
                        fail(f'tables.{table}.{field}', a[field], b[field])

    report['passed'] = not report['differences']
    destination = Path(output_path).resolve()
    if destination in paths:
        raise ValueError('Comparison output must not overwrite a run evidence file')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, destination)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('first', help='First saved --record-run label')
    parser.add_argument('second', help='Second saved --record-run label')
    parser.add_argument('--profile')
    parser.add_argument('--local-validation', action='store_true',
                        help='Compare explicitly scoped local_runs evidence while retaining the strict directory failure')
    parser.add_argument('--cross-platform', action='store_true',
                        help='Compare semantic fingerprints; record that Parquet byte hashes were not compared')
    args = parser.parse_args()
    for label in (args.first, args.second):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', label):
            parser.error('Run labels accept only letters, digits, underscore and hyphen')
    paths = load_paths(profile=args.profile)
    destination = paths.output('verification', 'repeatability.json')
    run_directory = 'local_runs' if args.local_validation else 'runs'
    report = compare_runs(
        paths.output('verification', run_directory, args.first + '.json'),
        paths.output('verification', run_directory, args.second + '.json'), destination,
        same_platform=not args.cross_platform, local_validation=args.local_validation,
    )
    print(f"Repeatability {'passed' if report['passed'] else 'failed'}: {len(report['differences'])} differences; {destination}")
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
