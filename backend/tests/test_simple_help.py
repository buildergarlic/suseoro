import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from suseoro.simple.app import create_app
from suseoro.simple.help_pages import HELP_FILES, default_help_dir


@pytest.fixture
def help_client(tmp_path):
    help_dir = tmp_path / 'help'
    help_dir.mkdir()
    source = '''<!doctype html><html lang="ko"><body>
    <h1>인터넷 없이 읽는 설명서</h1>
    <a href="user-guide.html#budget">상세 설명서</a>
    <a href="https://github.com/buildergarlic/suseoro" target="_self">GitHub</a>
    <button onclick="window.print()" type="button">인쇄 / PDF로 보관</button>
    <script>document.body.dataset.practice = 'ready';</script>
    </body></html>'''
    for filename in HELP_FILES:
        (help_dir / filename).write_text(source, encoding='utf-8')
    (help_dir / 'private.json').write_text('{"private": true}', encoding='utf-8')
    with TestClient(create_app(tmp_path / 'data', help_dir=help_dir), base_url='http://127.0.0.1:3847') as client:
        yield client, help_dir


@pytest.mark.parametrize('path', ['/help', '/help/', *('/help/' + name for name in sorted(HELP_FILES))])
def test_manual_pages_are_bundled_html_with_a_fresh_nonce(help_client, path):
    client, _ = help_client
    response = client.get(path)
    assert response.status_code == 200
    assert '인터넷 없이 읽는 설명서' in response.text
    assert response.headers['content-type'].startswith('text/html')
    assert response.headers['x-frame-options'] == 'SAMEORIGIN'
    policy = response.headers['content-security-policy']
    nonce = re.search(r"script-src 'nonce-([^']+)'", policy).group(1)
    assert f'<script nonce="{nonce}">' in response.text
    assert 'script-src \'self\'' not in policy
    assert "frame-ancestors 'self'" in policy
    assert "connect-src 'none'" in policy
    assert "form-action 'none'" in policy
    assert 'onclick=' not in response.text
    assert 'data-suseoro-print' in response.text
    assert "window.print()" in response.text
    assert "suseoro-help-close" in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert nonce not in client.get(path).headers['content-security-policy']


def test_external_links_open_outside_the_manual(help_client):
    client, _ = help_client
    body = client.get('/help/index.html').text
    assert '<a href="https://github.com/buildergarlic/suseoro" target="_blank" rel="noopener noreferrer">' in body
    assert '<a href="user-guide.html#budget">' in body


def test_short_help_url_redirects_so_relative_manual_links_work(help_client):
    client, _ = help_client
    response = client.get('/help', follow_redirects=False)
    assert response.status_code == 307
    assert response.headers['location'] == '/help/'


@pytest.mark.parametrize('path', [
    '/help/private.json', '/help/user-guide.md', '/help/unknown.html',
    '/help/%2e%2e/private.json', '/help/%2e%2e%2fprivate.json',
    '/help/..%5cprivate.json', '/help/index.html/extra',
])
def test_only_the_fixed_manual_files_can_be_read(help_client, path):
    client, _ = help_client
    response = client.get(path)
    assert response.status_code == 404
    assert '"private": true' not in response.text
    assert response.headers['x-frame-options'] == 'DENY'


def test_missing_manual_reports_a_readable_error_inside_the_help_frame(help_client):
    client, help_dir = help_client
    (help_dir / 'user-guide.html').unlink()
    response = client.get('/help/user-guide.html')
    assert response.status_code == 503
    assert '설명서 파일을 찾을 수 없습니다' in response.text
    assert response.headers['content-type'].startswith('text/html')
    assert response.headers['x-frame-options'] == 'SAMEORIGIN'
    assert 'target="_blank" rel="noopener noreferrer"' in response.text


def test_sandboxed_guide_cannot_read_or_change_live_library_data(help_client):
    client, _ = help_client
    response = client.get('/api/library/bootstrap')
    token = response.json()['csrf_token']
    assert response.headers['x-frame-options'] == 'DENY'
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert "frame-src 'self'" in response.headers['content-security-policy']
    assert 'nonce-' not in response.headers['content-security-policy']
    assert client.get('/api/library/bootstrap', headers={'Origin': 'null'}).status_code == 403
    assert client.get('/api/library/backup', headers={'Origin': 'null'}).status_code == 403
    assert client.post('/api/library/lists', json={'name': '설명서에서 변경'}, headers={
        'Origin': 'null', 'X-Suseoro-Token': token,
    }).status_code == 403
    assert client.post('/api/library/lists', json={'name': '토큰 없이 변경'}).status_code == 403


def test_development_and_frozen_help_locations(monkeypatch, tmp_path):
    import suseoro.simple.help_pages as help_pages
    monkeypatch.delattr(help_pages.sys, '_MEIPASS', raising=False)
    assert default_help_dir() == Path(help_pages.__file__).resolve().parents[4] / 'docs'
    monkeypatch.setattr(help_pages.sys, '_MEIPASS', str(tmp_path), raising=False)
    assert default_help_dir() == tmp_path / 'help'
