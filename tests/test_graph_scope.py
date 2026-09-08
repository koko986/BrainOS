from marlin.config import MarlinSettings
from marlin.runtime import MarlinRuntime


def test_graph_only_contains_selected_tree_and_preserves_other_data(tmp_path, monkeypatch):
    from marlin import indexer
    monkeypatch.setattr(indexer, "SKIP_NAMES", indexer.SKIP_NAMES - {"appdata"})
    root = tmp_path / "Projects"
    (root / "nested" / "empty").mkdir(parents=True)
    (root / "nested" / "app.py").write_text("print('hello')")
    sibling = tmp_path / "ProjectsElse"
    sibling.mkdir()
    (sibling / "other.py").write_text("pass")
    runtime = MarlinRuntime(MarlinSettings(
        database_path=tmp_path / "brain.db", graph_root=root,
        auto_index_c_drive=False, voice_output=False,
    ), start_background=False)
    try:
        runtime.indexer.index(sibling)
        runtime.indexer.index(root)
        runtime.knowledge.seed_demo()
        before = runtime.knowledge.count_entities()
        graph = runtime.graph()
        paths = {node['metadata']['path'] for node in graph['nodes']}
        assert paths == {str(root), str(root / 'nested'), str(root / 'nested' / 'empty'), str(root / 'nested' / 'app.py')}
        assert runtime.knowledge.count_entities() == before
        assert len([edge for edge in graph['links'] if edge['type'] == 'contains']) == 3
        ids = {node['id'] for node in graph['nodes']}
        assert all(edge['source'] in ids and edge['target'] in ids for edge in graph['links'])
        runtime.settings.graph_root = tmp_path / 'missing'
        assert runtime.graph()['nodes'] == []
    finally:
        runtime.shutdown()


def test_indexer_does_not_follow_links_outside_selected_root(tmp_path):
    from marlin.indexer import IncrementalIndexer
    from unittest.mock import Mock
    path = Mock()
    path.is_symlink.return_value = True
    assert IncrementalIndexer._skip(path)
