import json

from core.config import Paths
from core.data.store import Store, stored_artifact_sha256
from core.pipeline.stage2 import _prepare_repeat_paths


def test_republished_upstream_records_new_file_identity(tmp_path, golden_root):
    paths = Paths(golden_root, tmp_path / 'middle')
    with Store(paths) as store:
        old = store.publish('l1', 'quarter_source', 'SELECT 1 AS id, 10 AS value', ['id'])
    with Store(paths) as store:
        new = store.publish('l1', 'quarter_source', 'SELECT 1 AS id, 20 AS value', ['id'])
        derived = store.publish('l1', 'derived', 'SELECT 1 AS id, 80 AS value', ['id'])
    assert old['sha256'] != new['sha256']
    assert derived['upstream']['l1/quarter_source'] == new['sha256']
    assert 'l1/quarter_source' not in new['upstream']
    saved = json.loads(paths.output('l1', 'derived', 'manifest.json').read_text(encoding='utf-8'))
    assert saved['upstream'] == derived['upstream']


def test_stage2_store_separates_research_outputs_from_stage1(tmp_path, golden_root):
    paths = Paths(golden_root, tmp_path / 'middle')
    with Store(paths, stage='stage2') as store:
        store.publish('l1', 'daily_calendar', 'SELECT 1 AS id', ['id'])
        store.publish('l3', 'factor_value', 'SELECT 1 AS id', ['id'])
        evidence = store.write_json(('verification', 'stage2_entry.json'), {'passed': True})
        assert store.output('_tmp') == tmp_path / 'factor_research/_tmp'
        assert store.output('_provenance') == tmp_path / 'factor_research/_provenance'
    assert paths.middle_root.joinpath('l1/daily_calendar/part-00000.parquet').is_file()
    assert paths.factor_root.joinpath('l3/factor_value/part-00000.parquet').is_file()
    assert evidence == tmp_path / 'factor_research/verification/stage2_entry.json'
    assert not paths.middle_root.joinpath('l3').exists()


def test_stage2_factor_partitions_are_the_only_physical_copy(tmp_path, golden_root):
    paths = Paths(golden_root, tmp_path / 'middle')
    with Store(paths, stage='stage2') as store:
        published = store.publish(
            'l3', 'factor_value',
            "SELECT trade_date, ts_code, factor_id, value FROM (VALUES "
            "(DATE '2024-01-02', '000001.SZ', 'ep_ttm', 0.1), "
            "(DATE '2024-01-02', '000001.SZ', 'roe_ttm', 0.2), "
            "(DATE '2024-01-02', '000002.SZ', 'ep_ttm', 0.3)) "
            "AS source(trade_date, ts_code, factor_id, value)",
            ['trade_date', 'ts_code', 'factor_id'], partition_by='factor_id')
        relation = store.output_relation('l3', 'factor_value')
        assert store.rows(f'SELECT factor_id, count(*) AS n FROM {relation} GROUP BY factor_id ORDER BY factor_id') == [
            {'factor_id': 'ep_ttm', 'n': 2}, {'factor_id': 'roe_ttm', 'n': 1}]
        assert published['rows'] == 3
        assert set(published['partitions']) == {'ep_ttm', 'roe_ttm'}
        root = paths.output('l3', 'factor_value')
        assert not (root / 'part-00000.parquet').exists()
        assert stored_artifact_sha256(root) == published['sha256']
        repeat_paths = _prepare_repeat_paths(paths, '_repeat_test', [('l3', 'factor_value', ())])
        for entry in published['partitions'].values():
            assert repeat_paths.output('l3', 'factor_value', entry['path']).is_file()
        with Store(repeat_paths, stage='stage2') as repeat_store:
            repeat_relation = repeat_store.output_relation('l3', 'factor_value')
            assert repeat_store.db.execute(f'SELECT count(*) FROM {repeat_relation}').fetchone()[0] == 3
