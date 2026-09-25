"""Read-only browsing of an imported school holdings snapshot."""

import pytest
from fastapi.testclient import TestClient

from suseoro.simple.app import create_app


@pytest.fixture
def library(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:3847') as client:
        yield client, app.state.store


def test_holdings_are_returned_in_stable_bounded_pages(library):
    client, store = library
    store.replace_holdings([
        {'title': f'소장 도서 {number}', 'author': '저자'}
        for number in range(1, 6)
    ])

    response = client.get('/api/library/holdings', params={'page': 2, 'page_size': 2})

    assert response.status_code == 200
    data = response.json()
    assert (data['total'], data['page'], data['page_size']) == (5, 2, 2)
    assert [item['title'] for item in data['items']] == ['소장 도서 3', '소장 도서 4']
    assert [item['author'] for item in data['items']] == ['저자', '저자']
    assert client.get('/api/library/holdings', params={'page': 4, 'page_size': 2}).json() == {
        'items': [], 'total': 5, 'page': 4, 'page_size': 2,
    }


@pytest.mark.parametrize('query,title', [
    ('한강', '채식주의자'),
    ('9788936434267', '채식주의자'),
    ('창비', '채식주의자'),
    ('추천.xlsx', '채식주의자'),
    ('PENGUIN', '영어책'),
    ('100%', '백 퍼센트'),
])
def test_holdings_searches_book_fields_and_treats_wildcards_literally(library, query, title):
    client, store = library
    store.replace_holdings([
        {'title': '채식주의자', 'author': '한강', 'isbn': '9788936434267',
         'publisher': '창비', 'source': '추천.xlsx'},
        {'title': '영어책', 'author': '다른 작가', 'publisher': 'Penguin'},
        {'title': '백 퍼센트', 'author': '수학자', 'source': '100% 기록.csv'},
    ])

    response = client.get('/api/library/holdings', params={'query': query})

    assert response.status_code == 200
    assert response.json()['total'] == 1
    assert [item['title'] for item in response.json()['items']] == [title]


def test_holdings_browse_reflects_replacement_without_changing_purchase_matching(library):
    client, store = library
    list_id = store.lists()[0]['id']
    store.add_book(list_id, {'title': '기존 책', 'author': '저자'})
    store.replace_holdings([{'title': '기존 책', 'author': '저자'}])
    assert store.list_state(list_id)['books'][0]['holdings_status'] == 'held'

    first = client.get('/api/library/holdings').json()
    assert first['total'] == 1
    store.replace_holdings([{'title': '새 책', 'author': '다른 저자'}])

    second = client.get('/api/library/holdings').json()
    assert [item['title'] for item in second['items']] == ['새 책']
    assert second['total'] == 1
    assert store.list_state(list_id)['books'][0]['holdings_status'] == 'not_held'


@pytest.mark.parametrize('params', [
    {'page': 0}, {'page_size': 0}, {'page_size': 101}, {'query': 'x' * 201},
])
def test_holdings_rejects_unbounded_or_invalid_browse_parameters(library, params):
    client, _store = library
    assert client.get('/api/library/holdings', params=params).status_code == 422
