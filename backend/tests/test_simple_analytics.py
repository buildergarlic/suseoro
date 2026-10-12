from fastapi.testclient import TestClient

from suseoro.simple import analytics
from suseoro.simple.analytics import HoldingsAnalytics, category_group
from suseoro.simple.app import create_app
from suseoro.simple.store import LibraryStore


def test_statistics_counts_records_copies_coverage_and_estimated_topics(tmp_path):
    store = LibraryStore(tmp_path)
    store.add_holdings([
        {'title': '숲속 친구의 모험', 'isbn': '9780140328721', 'category': '813.8', 'quantity': 2, 'published_date': '2020-01-01', 'publisher': '햇살'},
        {'title': '지구와 환경 과학', 'isbn': '9780140328721', 'category': '400', 'published_date': '2019', 'publisher': '햇살'},
        {'title': '제목만 아는 책', 'isbn': 'invalid'},
    ])
    result = HoldingsAnalytics(store).statistics()
    assert (result['total_records'], result['total_copies'], result['unique_isbns']) == (3, 4, 1)
    assert result['coverage'] == {'missing_isbn': 1, 'missing_category': 1, 'missing_year': 1}
    assert sum(item['count'] for item in result['category_counts']) == 3
    assert {item['key']: item['count'] for item in result['category_counts']}['8'] == 1
    assert {'label': '우정·학교', 'count': 1} in result['topics']
    assert result['top_publishers'] == [{'label': '햇살', 'count': 2}]
    assert '추정' in result['topic_method']
    assert category_group('Science fiction') == '8'
    assert category_group('KDC: 300') == '3'
    assert category_group('아813.8') == '8'
    assert analytics.publication_year('20200303') == '2020'


def test_isbn_cache_enriches_statistics_without_overwriting_holdings(tmp_path, monkeypatch):
    store = LibraryStore(tmp_path)
    store.add_holdings([{'title': '원본 제목', 'isbn': '9780140328721', 'category': '600'},
                        {'title': '빈 분류', 'isbn': '9780140328721'}])
    service = HoldingsAnalytics(store)
    monkeypatch.setattr(analytics, 'lookup_isbn', lambda isbn, **kw: {'found': True, 'book': {'isbn': isbn, 'title': '서지 제목', 'category': '800', 'subjects': ['friendship'], 'publisher': '별빛', 'published_date': '2021'}})
    monkeypatch.setattr(service._cancel, 'wait', lambda timeout: False)
    service._status.update(phase='running', total=1)
    service._run(['9780140328721'])
    result = service.statistics()
    assert result['metadata_records'] == 2
    counts = {item['key']: item['count'] for item in result['category_counts']}
    assert counts['6'] == counts['8'] == 1
    assert store.list_holdings()['items'][0]['title'] == '원본 제목'
    assert service.start()['total'] == 0


def test_network_failures_are_not_negative_cached_and_resume(tmp_path, monkeypatch):
    store = LibraryStore(tmp_path)
    store.add_holdings([{'title': '책', 'isbn': '9780140328721'}])
    service = HoldingsAnalytics(store)
    monkeypatch.setattr(analytics, 'lookup_isbn', lambda *a, **kw: {'found': False, 'book': {}, 'warnings': ['Open Library에 연결하지 못했습니다.']})
    service._status.update(phase='running', total=1)
    service._run(['9780140328721'])
    assert service.status()['phase'] == 'error'
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM holdings_metadata').fetchone()[0] == 0
    monkeypatch.setattr(analytics, 'lookup_isbn', lambda *a, **kw: {'found': False, 'book': {}, 'warnings': ['일치하는 ISBN의 도서 정보를 확인하지 못했습니다.']})
    monkeypatch.setattr(service._cancel, 'wait', lambda timeout: False)
    service._run(['9780140328721'])
    assert service.status()['phase'] == 'completed'
    assert service.start()['total'] == 0


def test_dashboard_and_reporting_api_are_local_and_mutations_need_csrf(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app, base_url='http://127.0.0.1')
    app.state.store.add_holdings([{'title': '어린이 과학', 'category': '400'}])
    assert client.get('/api/library/holdings/statistics').json()['classified_records'] == 1
    assert client.post('/api/library/holdings/enrichment').status_code == 403
    token = client.get('/api/library/bootstrap').json()['csrf_token']
    headers = {'X-Suseoro-Token': token}
    assert client.post('/api/library/holdings/enrichment', headers=headers).json()['phase'] == 'completed'
    assert client.patch('/api/library/error-reports', json={'enabled': False}, headers=headers).json()['enabled'] is False
    assert client.patch('/api/library/error-reports', json={'enabled': 'false'}, headers=headers).status_code == 400
    assert client.get('/api/library/holdings/statistics', headers={'Host': 'evil.test'}).status_code == 403


def test_stop_during_lookup_does_not_write_a_cache_result(tmp_path, monkeypatch):
    store = LibraryStore(tmp_path)
    service = HoldingsAnalytics(store)
    def lookup(isbn, **kwargs):
        service.stop()
        return {'found': True, 'book': {'isbn': isbn, 'category': '800'}}
    monkeypatch.setattr(analytics, 'lookup_isbn', lookup)
    service._status.update(phase='running', total=1)
    service._run(['9780140328721'])
    assert service.status()['phase'] == 'idle'
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM holdings_metadata').fetchone()[0] == 0
