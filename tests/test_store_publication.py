import json

from core.config import Paths
from core.data.store import Store


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
