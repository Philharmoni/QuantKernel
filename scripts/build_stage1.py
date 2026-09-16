"""Sequential Stage 1 rebuild with source guards and a final quality gate."""
import argparse
import hashlib
import json
import logging
import re
import time

import _bootstrap  # noqa: F401
from core.calendar import build_calendar
from core.config import load_paths
from core.data.inventory import inventory
from core.data.profile import profile_all
from core.data.store import Store, json_text
from core.financial.available import build_financial_available
from core.financial.quarters import build_quarterly_and_ttm
from core.market.prices import build_adjusted_price
from core.market.state import build_stock_state
from core.market.trading import build_trade_status
from core.quality.checks import Quality, check_component, check_data_quality, file_sha256
from core.universe.research import build_research_universe


def metadata_signature(manifest):
    rows = sorted((r['path'], r['size'], r['mtime_ns']) for r in manifest['files'])
    digest = hashlib.sha256(json_text({'directories': manifest['directories'], 'files': rows}).encode('utf-8')).hexdigest()
    return {'signature': digest, 'file_count': len(rows), 'total_bytes': sum(r[1] for r in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--l0-scope', choices=['full', 'core'], default='full')
    parser.add_argument('--reuse-l0', action='store_true', help='Reuse profiles only when their source metadata and field contract still match')
    parser.add_argument('--record-run', default='latest', help='Save comparison evidence under verification/runs/NAME.json')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.record_run):
        parser.error('--record-run accepts only letters, digits, underscore and hyphen')
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    paths = load_paths(profile=args.profile)
    started = time.perf_counter()
    print('Checking read-only source metadata', flush=True)
    before = metadata_signature(inventory(paths.data_root))
    stages = []
    with Store(paths) as store:
        store.write_json(('verification', 'build_status.json'), {'status': 'running', 'run': args.record_run})
        store.write_json(('l2', 'data_quality_report', 'report.json'), {'passed': False, 'status': 'rebuilding', 'run': args.record_run})
        store.paths.output('l2', 'data_quality_report', 'REPORT.md').write_text('# 数据处理中\n\n正在按顺序重建并验证，先前报告不代表本次构建通过。\n', encoding='utf-8')
    stage_name = 'L0'
    try:
        with Store(paths) as store:
            signature_path = store.paths.output('l0', 'input_signature.json')
            if args.reuse_l0:
                if not signature_path.exists() or json.loads(signature_path.read_text(encoding='utf-8')) != before:
                    raise ValueError('L0 reuse denied: source metadata does not match the profiled input')
            else:
                names = None if args.l0_scope == 'full' else sorted(n for n, spec in store.tables.items() if spec.get('stage1'))
                profile_all(store, names)
                store.write_json(('l0', 'input_signature.json'), before)
            guard = Quality(store, args.start, args.end, args.l0_scope, True)
            guard.l0()
            if guard.report['failures']:
                raise ValueError(f"L0 gate failed: {guard.report['failures']}")
            stages.append({'stage': 'L0', 'passed': True, 'reused': args.reuse_l0})
        builders = [
            ('daily_calendar', lambda s: build_calendar(s)),
            ('daily_adjusted_price', lambda s: build_adjusted_price(s, args.start, args.end)),
            ('daily_stock_state', lambda s: build_stock_state(s, args.start, args.end)),
            ('daily_trade_status', lambda s: build_trade_status(s, args.start, args.end)),
            ('financial_available', lambda s: build_financial_available(s)),
            ('quarterly_and_ttm', lambda s: build_quarterly_and_ttm(s)),
            ('research_universe', lambda s: build_research_universe(s, args.start, args.end)),
        ]
        for stage_name, builder in builders:
            print(f'Building and validating {stage_name}', flush=True)
            step_started = time.perf_counter()
            with Store(paths) as store:
                result = builder(store)
                manifests = [result] if 'table' in result else list(result.values())
                # Every publisher rejects null/duplicate keys before replacing the artifact.
                for manifest in manifests:
                    if file_sha256(store.paths.output(manifest['layer'], manifest['table'], 'part-00000.parquet')) != manifest['sha256']:
                        raise ValueError(f"{manifest['table']}: publication checksum mismatch")
                check_component(store, stage_name, args.start, args.end, args.l0_scope)
                stages.append({'stage': stage_name, 'passed': True, 'tables': {m['table']: m['rows'] for m in manifests},
                               'elapsed_seconds': round(time.perf_counter()-step_started, 3)})
                store.write_json(('verification', 'build_status.json'), {'status': 'running', 'run': args.record_run, 'stages': stages})
            print(f'Passed {stage_name}: {stages[-1]["tables"]}', flush=True)
        stage_name = 'quality'
        print('Checking L0/L1/L2 quality, PIT, lineage and file integrity', flush=True)
        with Store(paths) as store:
            report = check_data_quality(store, args.start, args.end, args.l0_scope)
        after = metadata_signature(inventory(paths.data_root))
        raw_proof = {'passed': before == after, 'before': before, 'after': after,
                     'method': 'relative path, file size and mtime_ns; raw files only read'}
        step1 = paths.output('verification', 'local_inventory.json')
        if step1.exists():
            original = metadata_signature(json.loads(step1.read_text(encoding='utf-8')))
            raw_proof['unchanged_since_step1'] = original == after
            raw_proof['passed'] = raw_proof['passed'] and raw_proof['unchanged_since_step1']
        with Store(paths) as store:
            store.write_json(('verification', 'raw_unchanged.json'), raw_proof)
            refresh = Quality(store, args.start, args.end, args.l0_scope, True)
            refresh.report = report
            if not raw_proof['passed']:
                refresh.fail('raw_data', 'source_changed_during_build', [], [raw_proof])
            report = refresh.finish()
            run = {'passed': True, 'config_hash': store.config_hash, 'code_hash': store.code_hash,
                   'start': args.start, 'end': args.end, 'l0_scope': args.l0_scope,
                   'elapsed_seconds': round(time.perf_counter()-started, 3), 'stages': stages,
                   'tables': report['tables'], 'raw_signature': after,
                   'l0_profiles': {p.stem: file_sha256(p) for p in sorted(paths.output('l0', 'profiles').glob('*.json'))}}
            store.write_json(('verification', 'runs', args.record_run + '.json'), run)
            store.write_json(('verification', 'build_status.json'), {'status': 'passed_supported_scope', **run})
        print(f'Stage 1 supported-scope build passed in {run["elapsed_seconds"]}s; deferred capabilities remain explicit', flush=True)
    except Exception as error:
        with Store(paths) as store:
            store.write_json(('verification', 'build_status.json'), {'status': 'failed', 'stage': stage_name, 'error': str(error), 'stages': stages})
            if stage_name != 'quality':
                store.write_json(('l2', 'data_quality_report', 'report.json'), {'passed': False, 'full_phase_passed': False,
                    'failures': [{'table': stage_name, 'check': 'build_failed', 'fields': [], 'samples': [{'error': str(error)}]}]})
                store.paths.output('l2', 'data_quality_report', 'REPORT.md').write_text(f'# 构建失败\n\n{stage_name}：{error}\n', encoding='utf-8')
        raise


if __name__ == '__main__':
    main()
