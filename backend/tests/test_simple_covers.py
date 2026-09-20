"""Local cover endpoint tests; providers are replaced at the transport boundary."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from suseoro.simple import covers
from suseoro.simple.app import create_app

@pytest.fixture(autouse=True)
def isolate_legacy_providers(monkeypatch):
    monkeypatch.setattr(covers.CoverService, '_aladin', lambda *args: None)


ISBN = '9788936434267'
OTHER = '9780140328721'


def image_bytes(size=(100, 150), format='PNG'):
    image = Image.new('RGB', size, 'navy')
    image.putpixel((0, 0), (255, 255, 255))
    output = BytesIO()
    image.save(output, format)
    return output.getvalue()


def transport(monkeypatch, responses):
    calls = []
    def fetch(url, *, deadline, max_bytes):
        calls.append(url)
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(covers, '_fetch_bytes', fetch)
    return calls


def not_found():
    return HTTPError('https://covers.openlibrary.org/', 404, 'not found', {}, None)


def google_body(isbn=ISBN, url='https://books.google.com/books/content?id=abc&img=1'):
    return ('suseoroCover(' + json.dumps({f'ISBN:{isbn}': {'bib_key': f'ISBN:{isbn}', 'thumbnail_url': url}}) + ');').encode()


def test_endpoint_returns_validated_jpeg_and_reuses_cover(tmp_path, monkeypatch):
    calls = transport(monkeypatch, [(image_bytes(), 'image/png')])
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:3847') as client:
        response = client.get('/api/library/covers/' + ISBN)
        assert response.status_code == 200
        assert response.headers['content-type'] == 'image/jpeg'
        assert response.headers['x-cover-source'] == 'Open Library'
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert 'max-age=' in response.headers['cache-control']
        with Image.open(BytesIO(response.content)) as image:
            assert image.format == 'JPEG' and image.size == (100, 150)
        assert client.get('/api/library/covers/' + ISBN).content == response.content
        assert client.get('/api/library/covers/9788936434268').status_code == 400
    assert len(calls) == 1 and '?default=false' in calls[0]


def test_missing_cover_returns_uncached_404_for_retry(tmp_path, monkeypatch):
    calls = transport(monkeypatch, [not_found(), (b'suseoroCover({});', 'application/javascript')] * 2)
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:3847') as client:
        for _ in range(2):
            response = client.get('/api/library/covers/' + ISBN)
            assert response.status_code == 404 and response.content == b''
            assert response.headers['cache-control'] == 'no-store'
    assert len(calls) == 4


def test_exact_google_isbn_fallback_fetches_only_allowlisted_thumbnail(monkeypatch):
    calls = transport(monkeypatch, [not_found(), (google_body(), 'application/javascript'), (image_bytes(), 'image/png')])
    result = covers.CoverService().get(ISBN)
    assert result is not None and result.source == 'Google Books'
    assert calls[-1].startswith('https://books.google.com/books/content?')


@pytest.mark.parametrize('body', [
    google_body(OTHER),
    google_body(url='https://127.0.0.1/private'),
    google_body(url='https://books.google.com.evil.example/books/content'),
    b'suseoroCover({});alert("execute")',
    b'<html>captcha</html>',
])
def test_google_wrong_isbn_or_untrusted_url_or_script_is_not_fetched(monkeypatch, body):
    calls = transport(monkeypatch, [not_found(), (body, 'application/javascript')])
    assert covers.CoverService().get(ISBN) is None
    assert len(calls) == 2


@pytest.mark.parametrize('data,mime', [
    (b'<html>not a cover</html>', 'image/jpeg'),
    (b'<svg xmlns="http://www.w3.org/2000/svg"/>', 'image/svg+xml'),
    (image_bytes((1, 1)), 'image/png'),
    (image_bytes(), 'text/html'),
])
def test_nonimages_and_blank_placeholder_are_rejected(monkeypatch, data, mime):
    transport(monkeypatch, [(data, mime), (b'suseoroCover({});', 'application/javascript')])
    assert covers.CoverService().get(ISBN) is None


def test_oversized_decoded_image_is_rejected_without_full_decode(monkeypatch):
    monkeypatch.setattr(covers, 'MAX_IMAGE_PIXELS', 1000)
    transport(monkeypatch, [(image_bytes(), 'image/png'), (b'suseoroCover({});', 'application/javascript')])
    assert covers.CoverService().get(ISBN) is None


def test_uniform_blank_image_is_not_reported_as_a_cover(monkeypatch):
    output = BytesIO()
    Image.new('RGB', (100, 150), 'white').save(output, 'PNG')
    transport(monkeypatch, [(output.getvalue(), 'image/png'), (b'suseoroCover({});', 'application/javascript')])
    assert covers.CoverService().get(ISBN) is None


@pytest.mark.parametrize('url', ['http://covers.openlibrary.org/b/isbn/x', 'https://user:password@books.google.com/books', 'https://books.google.com:444/books', 'https://127.0.0.1/', 'file:///secret', 'https://ia600502.us.archive.org.evil.example/a'])
def test_transport_rejects_untrusted_hosts_and_schemes_before_network(url):
    with pytest.raises(ValueError):
        covers._fetch_bytes(url, deadline=0, max_bytes=100)


def test_redirects_are_checked_before_the_next_network_request():
    handler = covers.CoverRedirectHandler()
    request = Request('https://covers.openlibrary.org/b/isbn/9788936434267-M.jpg?default=false')
    with pytest.raises(ValueError):
        handler.redirect_request(request, None, 302, 'redirect', {}, 'https://127.0.0.1/private')
    redirected = handler.redirect_request(request, None, 302, 'redirect', {}, 'https://ia600502.us.archive.org/view_archive.php?file=cover.jpg')
    assert redirected.full_url.startswith('https://ia600502.us.archive.org/')


def test_open_library_archive_download_redirect_is_allowed_but_other_archive_paths_are_not():
    handler = covers.CoverRedirectHandler()
    request = Request('https://covers.openlibrary.org/b/isbn/' + ISBN + '-M.jpg?default=false')
    url = 'https://archive.org/download/m_covers_0013/m_covers_0013_14.zip/0013141400-M.jpg'
    assert handler.redirect_request(request, None, 302, 'redirect', {}, url).full_url == url
    with pytest.raises(ValueError):
        handler.redirect_request(request, None, 302, 'redirect', {}, 'https://archive.org/services/anything')


@pytest.mark.parametrize('declared_length', ['', '100'])
def test_transport_bounds_size_with_or_without_content_length(monkeypatch, declared_length):
    class Response(BytesIO):
        headers = {'Content-Type': 'image/png', 'Content-Length': declared_length}
        def geturl(self):
            return 'https://covers.openlibrary.org/b/isbn/' + ISBN + '-M.jpg'
    class Opener:
        def open(self, request, timeout):
            return Response(b'x' * 100)
    monkeypatch.setattr(covers, 'build_opener', lambda *args: Opener())
    with pytest.raises(ValueError, match='too large'):
        covers._fetch_bytes('https://covers.openlibrary.org/b/isbn/' + ISBN + '-M.jpg', deadline=covers.time.monotonic() + 1, max_bytes=10)


def test_repeated_concurrent_isbn_requests_share_one_fetch(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    def fetch(url, *, deadline, max_bytes):
        calls.append(url)
        started.set()
        assert release.wait(2)
        return image_bytes(), 'image/png'
    monkeypatch.setattr(covers, '_fetch_bytes', fetch)
    service = covers.CoverService()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.get, ISBN)
        assert started.wait(1)
        second = pool.submit(service.get, ISBN)
        release.set()
        assert first.result(2).data == second.result(2).data
    assert len(calls) == 1


def test_dns_stall_does_not_hold_response_or_spawn_unbounded_workers(monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(covers, 'LOOKUP_BUDGET', 0.05)
    monkeypatch.setattr(covers, 'MAX_WORKERS', 1)
    calls = []
    def fetch(url, *, deadline, max_bytes):
        calls.append(url)
        started.set()
        release.wait(2)
        return image_bytes(), 'image/png'
    monkeypatch.setattr(covers, '_fetch_bytes', fetch)
    service = covers.CoverService()
    try:
        assert service.get(ISBN) is None
        assert started.is_set()
        assert service.get(OTHER) is None
        assert len(calls) == 1
    finally:
        release.set()


def test_provider_limits_stop_additional_requests(monkeypatch):
    monkeypatch.setattr(covers, 'OPEN_LIBRARY_LIMIT', 1)
    monkeypatch.setattr(covers, 'GOOGLE_LIMIT', 0)
    calls = transport(monkeypatch, [(image_bytes(), 'image/png')])
    service = covers.CoverService()
    assert service.get(ISBN) is not None
    assert service.get(OTHER) is None
    assert len(calls) == 1


def test_cache_entry_limit_evicts_old_cover(monkeypatch):
    monkeypatch.setattr(covers, 'MAX_CACHE_ENTRIES', 1)
    calls = transport(monkeypatch, [(image_bytes(), 'image/png')] * 3)
    service = covers.CoverService()
    for isbn in [ISBN, OTHER, ISBN]:
        assert service.get(isbn) is not None
    assert len(calls) == 3


def test_cache_expiry_allows_another_provider_attempt(monkeypatch):
    monkeypatch.setattr(covers, 'POSITIVE_TTL', 0)
    calls = transport(monkeypatch, [(image_bytes(), 'image/png')] * 2)
    service = covers.CoverService()
    assert service.get(ISBN) is not None
    assert service.get(ISBN) is not None
    assert len(calls) == 2


def test_cache_byte_limit_does_not_keep_an_oversized_entry(monkeypatch):
    monkeypatch.setattr(covers, 'MAX_CACHE_BYTES', 1)
    calls = transport(monkeypatch, [(image_bytes(), 'image/png')] * 2)
    service = covers.CoverService()
    assert service.get(ISBN) is not None
    assert service.get(ISBN) is not None
    assert len(calls) == 2
