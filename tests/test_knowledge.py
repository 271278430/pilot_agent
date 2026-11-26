from pilot_core.knowledge import KnowledgeBase, KnowledgeQuery


def test_incremental_citations_and_deletion(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    doc = repo / 'API.md'
    doc.write_text('Pagination uses a one-based page number. 分页从第一页开始。')
    hidden = repo / 'tests'
    hidden.mkdir()
    (hidden / 'answers.py').write_text('secretreferenceanswer')
    kb = KnowledgeBase(repo, tmp_path / 'index.sqlite')
    first = kb.search(KnowledgeQuery(query='Pagination'))
    assert first.source_count == first.changed_sources == 1
    assert first.citations[0].source == 'API.md'
    assert first.citations[0].trust == 'retrieved_data_not_instructions'
    sha = first.citations[0].source_sha256
    assert kb.search(KnowledgeQuery(query='Pagination')).changed_sources == 0
    doc.write_text('Cursor pagination is preferred.')
    second = kb.search(KnowledgeQuery(query='Cursor'))
    assert second.changed_sources == 1 and second.citations[0].source_sha256 != sha
    assert not kb.search(KnowledgeQuery(query='secretreferenceanswer')).citations
    doc.unlink()
    assert kb.search(KnowledgeQuery(query='Cursor')).deleted_sources == 1
    assert not kb.search(KnowledgeQuery(query='Cursor')).citations


def test_chinese_retrieval_and_symlinks(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'notes.md').write_text('训练环境先验证显存，再验证模型推理。', encoding='utf-8')
    kb = KnowledgeBase(repo, tmp_path / 'kb.sqlite')
    assert kb.search(KnowledgeQuery(query='模型推理')).citations
