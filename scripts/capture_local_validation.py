"""Capture existing local validation evidence without rerunning quality queries."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.config.settings import load_settings
from core.data.inventory import inventory
from core.data.store import code_identity, json_text
from core.quality.checks import QUALITY_CODE_HASH, TABLES, file_sha256


COMPONENTS = (
    'daily_calendar', 'daily_adjusted_price', 'daily_stock_state', 'daily_trade_status',
    'financial_available', 'quarterly_and_ttm', 'research_universe',
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_evidence(path):
    content = path.read_bytes()
    value = json.loads(content)
    require(isinstance(value, dict), f'Expected a JSON object: {path}')
    return value, {'path': str(path.resolve()), 'sha256': hashlib.sha256(content).hexdigest()}


def validate_strict_failure(report):
    require(report.get('passed') is False, 'The archived strict evidence must record failure')
    if report.get('algorithm') != 'SHA-256':
        require(bool(report.get('failures')) and all(
            f.get('table') == 'raw_data' and f.get('check') == 'source_changed_during_build'
            for f in report['failures']), 'Archived quality failure must concern only the strict raw-directory guard')
        return 'raw_guard_quality_report'
    require(report.get('baseline_unchanged') is True and report.get('directories_match') is True
            and isinstance(report.get('baseline_directories'), list)
            and report['baseline_directories'] == report.get('current_directories'),
            'Strict SHA evidence must retain the original baseline and unchanged directories')
    require(report.get('missing_current') == [] and report.get('missing_checksums') == [],
            'Strict SHA evidence must have no missing files or missing checksums')
    baseline_count, current_count = report.get('baseline_files'), report.get('current_files')
    require(type(baseline_count) is int and baseline_count > 0 and type(current_count) is int
            and type(report.get('hashes_checked')) is int and report['hashes_checked'] == baseline_count,
            'Strict SHA evidence must cover every baseline file')
    differences = []
    for field in ('added_current', 'size_mismatches', 'hash_mismatches'):
        values = report.get(field)
        require(isinstance(values, list) and all(isinstance(name, str) and name
                and '/' not in name and '\\' not in name and ':' not in name
                and PurePosixPath(name).suffix.lower() == '.md' for name in values),
                f'Strict SHA evidence contains a non-root-Markdown difference: {field}')
        require(len(set(values)) == len(values), f'Duplicate difference paths: {field}')
        differences.extend(values)
    require(bool(differences) and current_count == baseline_count + len(report['added_current']),
            'Strict SHA evidence counts must agree with its root-Markdown-only differences')
    return 'raw_sha256_comparison'


def scoped_inventory(manifest):
    """Exclude only Markdown files directly inside DATA_ROOT."""
    files, documents, seen = [], [], set()
    for row in manifest['files']:
        path = PurePosixPath(row['path'])
        require(not path.is_absolute() and '..' not in path.parts and str(path) not in seen,
                f'Invalid or duplicate inventory path: {path}')
        seen.add(str(path))
        record = (str(path), row['size'], row['mtime_ns'])
        (documents if path.parent == PurePosixPath('.') and path.suffix.lower() == '.md' else files).append(record)
    return {'directories': sorted(manifest['directories']), 'files': sorted(files)}, sorted(documents)


def capture(paths, label, strict_report_path):
    require(re.fullmatch(r'[A-Za-z0-9_-]+', label) is not None, 'Invalid capture label')
    destination = paths.output('verification', 'local_runs', label + '.json')
    require(not destination.exists(), f'Capture already exists; choose a new label: {destination}')
    quality_archive = paths.output('verification', 'local_runs', label + '.quality_report.json')
    require(not quality_archive.exists(), f'Archived quality report already exists; choose a new label: {quality_archive}')
    quality_path = paths.output('l2', 'data_quality_report', 'report.json')
    strict_path = Path(strict_report_path).expanduser().resolve()
    require(strict_path != quality_path and strict_path.is_relative_to(paths.output('verification')),
            'Strict failure must be archived under verification/, separately from the current quality report')
    strict, strict_proof = read_evidence(strict_path)
    strict_proof['kind'] = validate_strict_failure(strict)
    if strict_proof['kind'] == 'raw_sha256_comparison':
        baseline_path = paths.output('verification', 'local_inventory.json')
        require(Path(strict.get('data_root', '')).resolve() == paths.data_root
                and Path(strict.get('baseline_path', '')).resolve() == baseline_path
                and strict.get('baseline_sha256') == file_sha256(baseline_path),
                'Strict SHA evidence refers to a different data root or changed Step 1 baseline')

    table_config = load_settings('tushare_tables.yaml')['tables']
    settings = {'tables': table_config, 'rules': load_settings('data_middle_layer.yaml')}
    config_hash = hashlib.sha256(json_text(settings).encode()).hexdigest()
    producer_hash = code_identity()
    report, quality_proof = read_evidence(quality_path)
    require(report.get('passed') is True and report.get('failures') == [], 'Latest quality report has not passed')
    require(report.get('l0_scope') == 'full' and report.get('requested_start') is None
            and report.get('requested_end') is None, 'An unbounded full-L0 quality report is required')
    require(report.get('code_hash') == producer_hash and report.get('config_hash') == config_hash
            and report.get('quality_code_hash') == QUALITY_CODE_HASH, 'Quality report has stale code or configuration')
    require(set(report.get('tables', {})) == set(TABLES), 'Quality report must contain all nine core outputs')
    archived_config, config_proof = read_evidence(paths.output('_provenance', 'configs', config_hash + '.json'))
    require(archived_config == settings, 'Archived configuration differs from the current configuration')

    summary, summary_proof = read_evidence(paths.output('l0', 'profile_summary.json'))
    require(len(table_config) == 69 and summary.get('passed') is True and summary.get('scope') == 'full'
            and summary.get('config_hash') == config_hash and not summary.get('errors')
            and set(summary.get('tables', {})) == set(table_config), 'All 69 L0 profiles must have passed under the current configuration')
    profile_paths = {p.stem: p for p in paths.output('l0', 'profiles').glob('*.json')}
    require(set(profile_paths) == set(table_config), 'The 69 L0 profile files must match the configured sources')
    profile_hashes = {}
    for name, spec in table_config.items():
        profile, proof = read_evidence(profile_paths[name])
        require(profile.get('table') == name and profile.get('config_hash') == config_hash
                and set(spec['required_fields']) <= set(profile.get('columns', {}))
                and profile.get('primary_key') == spec['primary_key']
                and set(profile.get('dates', {})) == set(spec['date_fields'])
                and profile.get('rows') == summary['tables'][name]['rows'], f'L0 profile/configuration mismatch: {name}')
        require(profile_paths[name].stat().st_mtime_ns <= quality_path.stat().st_mtime_ns,
                f'L0 profile changed after the quality report: {name}')
        profile_hashes[name] = proof['sha256']

    table_proofs = {}
    for name, (layer, key, required_schema) in TABLES.items():
        manifest, proof = read_evidence(paths.output(layer, name, 'manifest.json'))
        table = report['tables'][name]
        require(manifest.get('code_hash') == producer_hash and manifest.get('config_hash') == config_hash,
                f'Stale output manifest: {name}')
        require(manifest.get('schema') == table.get('schema')
                and all(table['schema'].get(field) == dtype for field, dtype in required_schema.items())
                and manifest.get('primary_key') == table.get('primary_key') == key
                and manifest.get('rows') == table.get('stored_rows') == table.get('rows')
                and table.get('fingerprint', {}).get('row_count') == table['rows'], f'Output/report contract mismatch: {name}')
        actual_sha = file_sha256(paths.output(layer, name, 'part-00000.parquet'))
        require(actual_sha == manifest.get('sha256') == table.get('sha256'), f'Output SHA differs from the validated report: {name}')
        table_proofs[name] = proof

    stages = [{'stage': 'L0', 'passed': True, 'reused': True, 'evidence': summary_proof}]
    for name in COMPONENTS:
        stage, proof = read_evidence(paths.output('verification', 'stages', name + '.json'))
        require(stage.get('component') == name and stage.get('passed') is True and stage.get('failures') == []
                and stage.get('code_hash') == producer_hash and stage.get('config_hash') == config_hash,
                f'Current component gate has not passed: {name}')
        stages.append({'stage': name, 'passed': True, 'evidence': proof})

    baseline, baseline_proof = read_evidence(paths.output('verification', 'local_inventory.json'))
    original, original_docs = scoped_inventory(baseline)
    current, current_docs = scoped_inventory(inventory(paths.data_root))
    require(original == current, 'Non-documentation raw file paths, sizes, timestamps or directories differ from Step 1')
    raw_signature = {'signature': hashlib.sha256(json_text(current).encode()).hexdigest(),
                     'file_count': len(current['files']), 'total_bytes': sum(row[1] for row in current['files'])}
    old_docs, new_docs = {row[0]: row[1:] for row in original_docs}, {row[0]: row[1:] for row in current_docs}
    document_changes = [{'path': name, 'before': old_docs.get(name), 'after': new_docs.get(name)}
                        for name in sorted(set(old_docs) | set(new_docs)) if old_docs.get(name) != new_docs.get(name)]
    quality_bytes = quality_path.read_bytes()
    require(hashlib.sha256(quality_bytes).hexdigest() == quality_proof['sha256'], 'Quality report changed during capture')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with quality_archive.open('xb') as stream:
        stream.write(quality_bytes)
    quality_proof = {'path': str(quality_archive.resolve()), 'sha256': quality_proof['sha256']}
    run = {
        'passed': True, 'scope': 'non_documentation_raw_files', 'full_pipeline_passed': False,
        'full_phase_passed': False, 'strict_failure': strict_proof,
        'scope_note': '根目录 Markdown 文档存在外部变更；严格全目录原始文件核验的失败记录保持不变。'
                      '本记录仅确认非文档原始文件元数据、已有质量验收及产物指纹，不能证明整个 data/ 未变。',
        'excluded_root_markdown_changes': document_changes,
        'raw_verification_method': 'Step 1 comparison of every non-root-Markdown file path, size and mtime_ns, plus directory names; no raw content hashing in this capture',
        'code_hash': producer_hash, 'config_hash': config_hash, 'quality_code_hash': QUALITY_CODE_HASH,
        'start': None, 'end': None, 'l0_scope': 'full', 'stages': stages,
        'tables': report['tables'], 'raw_signature': raw_signature, 'l0_profiles': profile_hashes,
        'evidence': {'quality_report': quality_proof, 'configuration': config_proof,
                     'step1_inventory': baseline_proof, 'output_manifests': table_proofs},
    }
    temporary = destination.with_suffix('.json.tmp')
    temporary.write_text(json_text(run), encoding='utf-8')
    os.replace(temporary, destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('label')
    parser.add_argument('--profile')
    parser.add_argument('--strict-report', required=True,
                        help='Archived root-Markdown-only raw SHA failure or raw-guard quality failure beneath verification/')
    args = parser.parse_args()
    try:
        target = capture(load_paths(profile=args.profile), args.label, args.strict_report)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f'Local validation capture failed: {error}')
        return 1
    print(f'Captured local business-data validation: {target}; strict full pipeline remains failed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
