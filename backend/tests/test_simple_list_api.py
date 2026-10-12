from fastapi.testclient import TestClient

from suseoro.simple.app import create_app


def test_list_archive_api_requires_local_token_and_restores_saved_books(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:3847') as client:
        bootstrap = client.get('/api/library/bootstrap').json()
        headers = {'X-Suseoro-Token': bootstrap['csrf_token']}
        first = bootstrap['lists'][0]['id']
        second = client.post('/api/library/lists', json={'name': '다른 구입 목록'}, headers=headers).json()['id']
        base = f'/api/library/lists/{first}'
        book = client.post(base + '/books', json={'title': '보관할 책'}, headers=headers).json()
        assert client.delete(base).status_code == 403
        assert client.delete(base, headers=headers).json()['deleted_at']
        after = client.get('/api/library/bootstrap').json()
        assert [item['id'] for item in after['lists']] == [second]
        assert [item['id'] for item in after['deleted_lists']] == [first]
        assert client.get(base).status_code == 404
        assert client.patch(base, json={'name': '오래된 변경'}, headers=headers).status_code == 404
        assert client.patch(base + '/books/' + book['id'], json={'title': '오래된 변경'}, headers=headers).status_code == 404
        assert client.delete(f'/api/library/lists/{second}', headers=headers).status_code == 400
        assert client.post(base + '/restore').status_code == 403
        assert 'deleted_at' not in client.post(base + '/restore', headers=headers).json()
        assert client.get(base).json()['books'][0]['id'] == book['id']
        assert client.get('/api/library/bootstrap').json()['deleted_lists'] == []
