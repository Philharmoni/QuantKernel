"""Stage 2 admission gate.

Before any Stage 2 (factor research) builder runs, this module verifies that
the nine Stage 1 L1/L2 products still match their own manifests and that the
Stage 1 quality report passed with the required acceptance evidence. It never
reads data/ or recomputes anything under data_middle/{l0,l1,l2}; it only
reads existing manifests, the quality report, and recomputes parquet file
hashes to detect tampering. The resulting per-table fingerprints are the
"upstream bundle" that Stage 2 publications are bound to.
"""
import hashlib
import json
from pathlib import Path
import re

from core.config.settings import load_settings
from core.data.store import json_text
from core.quality.checks import TABLES, file_sha256

# Source subpackages that belong to Stage 2 and must never call the raw-table
# reader on Store (method name intentionally not spelled out literally here
# so this guidance text cannot match its own regular-expression check below);
# they may only consume L1/L2/L3 outputs via Store.output_relation().
# Directories that do not exist yet are skipped, so this guard is active from
# Step 0 and stays active as later steps add code.
STAGE2_SOURCE_DIRS = ('industry', 'benchmark', 'label', 'factor', 'preprocess', 'evaluation')
_FORBIDDEN_CALL_PATTERN = re.compile(r'\.' + 'raw' + r'\s*\(')

# Plan Step 3: factor code must never import the label module -- labels and
# factors may only meet inside core.evaluation. Scanned as import statements
# (not arbitrary text) so prose about the rule cannot trip the guard.
_LABEL_IMPORT_PATTERN = re.compile(
    r'^\s*(?:from|import)\s+core\.label\b|^\s*from\s+core\s+import\s+[^\n]*\blabel\b', re.M)


def check_factor_label_isolation(src_root: Path | None = None) -> list[str]:
    root = Path(src_root) if src_root else Path(__file__).resolve().parents[1]
    folder = root / 'factor'
    violations = []
    if folder.is_dir():
        for path in sorted(folder.rglob('*.py')):
            if _LABEL_IMPORT_PATTERN.search(path.read_text(encoding='utf-8')):
                violations.append(str(path))
    return violations


def check_no_raw_access(src_root: Path | None = None) -> list[str]:
    root = Path(src_root) if src_root else Path(__file__).resolve().parents[1]
    violations = []
    for sub in STAGE2_SOURCE_DIRS:
        folder = root / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob('*.py')):
            if _FORBIDDEN_CALL_PATTERN.search(path.read_text(encoding='utf-8')):
                violations.append(str(path))
    return violations


def check_stage2_entry(store, config_root: Path | None = None) -> dict:
    research = load_settings('factor_research.yaml', config_root)
    required_evidence = research['stage2_entry']['required_acceptance_evidence']
    failures = []

    def fail(table, check, fields, samples):
        failures.append({'table': table, 'check': check, 'fields': fields, 'samples': samples})

    report_path = store.paths.output('l2', 'data_quality_report', 'report.json')
    report = None
    if not report_path.is_file():
        fail('l2/data_quality_report', 'missing_report', [], [{'path': str(report_path)}])
    else:
        report = json.loads(report_path.read_text(encoding='utf-8'))
        if report.get('passed') is not True:
            fail('l2/data_quality_report', 'stage1_not_passed', ['passed'], [{'passed': report.get('passed')}])
        evidence = report.get('acceptance_evidence', {})
        for name in required_evidence:
            if evidence.get(name, {}).get('passed') is not True:
                fail('l2/data_quality_report', f'acceptance_evidence_{name}_not_passed',
                     [name], [{'evidence': evidence.get(name)}])

    bundle = {}
    for name, (layer, key, required) in TABLES.items():
        manifest_path = store.paths.output(layer, name, 'manifest.json')
        part_path = store.paths.output(layer, name, 'part-00000.parquet')
        if not manifest_path.is_file() or not part_path.is_file():
            fail(name, 'missing_artifact', ['manifest', 'part-00000.parquet'],
                 [{'manifest_exists': manifest_path.is_file(), 'part_exists': part_path.is_file()}])
            continue
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('primary_key') != key:
            fail(name, 'primary_key_mismatch', ['primary_key'], [{'expected': key, 'actual': manifest.get('primary_key')}])
        missing_fields = sorted(set(required) - set(manifest.get('schema', {})))
        if missing_fields:
            fail(name, 'schema_missing_fields', missing_fields, [{'manifest_schema': manifest.get('schema')}])
        actual_sha = file_sha256(part_path)
        if actual_sha != manifest.get('sha256'):
            fail(name, 'file_sha256_mismatch', ['sha256'], [{'manifest': manifest.get('sha256'), 'actual': actual_sha}])
            continue
        if report is not None:
            reported = report.get('tables', {}).get(name, {})
            if reported.get('sha256') != manifest.get('sha256'):
                fail(name, 'report_manifest_sha256_mismatch', ['sha256'],
                     [{'report': reported.get('sha256'), 'manifest': manifest.get('sha256')}])
        bundle[name] = {'layer': layer, 'primary_key': key, 'sha256': manifest.get('sha256'),
                         'rows': manifest.get('rows'), 'config_hash': manifest.get('config_hash'),
                         'code_hash': manifest.get('code_hash')}

    raw_access_violations = check_no_raw_access()
    if raw_access_violations:
        fail('stage2_source', 'raw_table_access_forbidden', [], [{'files': raw_access_violations}])

    passed = not failures
    result = {
        'passed': passed,
        'failures': failures,
        'required_acceptance_evidence': required_evidence,
        'upstream_tables': bundle,
        'bundle_sha256': hashlib.sha256(json_text(bundle).encode('utf-8')).hexdigest() if passed else None,
        'stage1_report_passed': report.get('passed') if report else None,
        'stage1_config_hash': report.get('config_hash') if report else None,
        'stage1_code_hash': report.get('code_hash') if report else None,
    }
    store.write_json(('verification', 'stage2_entry.json'), result)
    if not passed:
        raise ValueError(f'Stage 2 entry gate failed: {json_text(failures)}')
    return result
