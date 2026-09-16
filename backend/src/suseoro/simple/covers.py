"""Bounded ISBN-only thumbnails for the local desktop, without provider keys.

Official sources checked 2026-09-16:
https://openlibrary.org/dev/docs/api/covers
https://developers.google.com/books/docs/dynamic-links

Open Library ISBN cover requests are limited to 100/IP per five minutes.
Google's documented, keyless Dynamic Links returns thumbnail_url for an exact
bib_key. This desktop queries from the end user's own PC/IP, on demand only;
it does not prefetch a catalogue or query preview availability offline. JSONP
is parsed as JSON under a fixed wrapper and is never executed. The v1 Books
API is not used without its officially required API key.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from io import BytesIO
import json
import re
import threading
import time
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image

from suseoro.catalog.normalization import canonical_isbn13

REQUEST_TIMEOUT = 2.5
LOOKUP_BUDGET = 7.0
MAX_WORKERS = 4
MAX_PENDING = 32
MAX_IMAGE_BYTES = 1_000_000
MAX_JSON_BYTES = 100_000
MAX_IMAGE_PIXELS = 2_000_000
MAX_CACHE_ENTRIES = 256
MAX_CACHE_BYTES = 16 * 1024 * 1024
POSITIVE_TTL = 24 * 60 * 60
NEGATIVE_TTL = 120
OPEN_LIBRARY_LIMIT = 90
GOOGLE_LIMIT = 60
RATE_WINDOW = 300
_GOOGLE_HOSTS = {'books.google.com', 'books.googleusercontent.com'}


def _allowed_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError('Invalid cover URL')
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    allowed = (host in {'covers.openlibrary.org', *_GOOGLE_HOSTS}
               or re.fullmatch(r'ia\d{6}\.(?:us|eu)\.archive\.org', host)
               or (host == 'archive.org' and parsed.path.startswith('/download/')))
    if (parsed.scheme != 'https' or not allowed or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError('Unsupported cover provider')
    return url


class CoverRedirectHandler(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _allowed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_bytes(url: str, *, deadline: float, max_bytes: int) -> tuple[bytes, str]:
    """Check every redirect before contact, and bound response size and time."""
    _allowed_url(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    request = Request(url, headers={'User-Agent': 'SuSeoRo/2.1 (local school library cover display)',
                                    'Accept-Encoding': 'identity'})
    opener = build_opener(CoverRedirectHandler())
    with opener.open(request, timeout=min(REQUEST_TIMEOUT, remaining)) as response:
        _allowed_url(response.geturl())
        length = response.headers.get('Content-Length', '')
        if length.isdigit() and int(length) > max_bytes:
            raise ValueError('Cover response too large')
        if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
            raise ValueError('Unsupported cover encoding')
        chunks, size = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            chunk = response.read1(min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError('Cover response too large')
            chunks.append(chunk)
        return b''.join(chunks), response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()


@dataclass(frozen=True)
class CoverImage:
    data: bytes
    source: str


def _thumbnail(data: bytes, mime: str, source: str) -> CoverImage:
    if len(data) > MAX_IMAGE_BYTES or mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}:
        raise ValueError('Not a supported cover image')
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        if image.format not in {'JPEG', 'PNG', 'WEBP', 'GIF'} or min(width, height) < 16 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError('Invalid cover dimensions')
        image.verify()
    with Image.open(BytesIO(data)) as image:
        image.seek(0)
        image = image.convert('RGB')
        if all(low == high for low, high in image.getextrema()):
            raise ValueError('Blank cover image')
        image.thumbnail((256, 384))
        output = BytesIO()
        # Re-encoding strips executable/container metadata and bounds output.
        image.save(output, 'JPEG', quality=85)
        return CoverImage(output.getvalue(), source)


class CoverService:
    """Per-app bounded LRU, duplicate-request sharing and bounded DNS workers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_WORKERS)
        self._cache: OrderedDict[str, tuple[float, CoverImage | None]] = OrderedDict()
        self._cache_bytes = 0
        self._inflight: dict[str, Future] = {}
        self._requests = {'openlibrary': deque(), 'google': deque()}

    def _permit(self, provider: str) -> bool:
        with self._lock:
            now = time.monotonic()
            history = self._requests[provider]
            while history and history[0] <= now - RATE_WINDOW:
                history.popleft()
            limit = OPEN_LIBRARY_LIMIT if provider == 'openlibrary' else GOOGLE_LIMIT
            if len(history) >= limit:
                return False
            history.append(now)
            return True

    def _lookup(self, isbn: str, deadline: float) -> CoverImage | None:
        if self._permit('openlibrary'):
            try:
                data, mime = _fetch_bytes(f'https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg?default=false', deadline=deadline, max_bytes=MAX_IMAGE_BYTES)
                return _thumbnail(data, mime, 'Open Library')
            except Exception:
                # No provider URL, exception body or credential is returned.
                pass
        if time.monotonic() >= deadline or not self._permit('google'):
            return None
        try:
            key = 'ISBN:' + isbn
            query = urlencode({'jscmd': 'viewapi', 'bibkeys': key, 'callback': 'suseoroCover'})
            data, _ = _fetch_bytes('https://books.google.com/books?' + query, deadline=deadline, max_bytes=MAX_JSON_BYTES)
            text = data.decode('utf-8-sig').strip()
            match = re.fullmatch(r'suseoroCover\((.*)\);?', text, re.DOTALL)
            if not match:
                return None
            payload = json.loads(match.group(1))
            info = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(info, dict) or info.get('bib_key') != key:
                return None
            url = info.get('thumbnail_url')
            if not isinstance(url, str):
                return None
            parsed = urlsplit(url)
            if parsed.hostname not in _GOOGLE_HOSTS or not parsed.path.startswith('/books'):
                return None
            # Older official metadata may still spell a Google image URL http.
            if parsed.scheme == 'http':
                url = urlunsplit(parsed._replace(scheme='https'))
            _allowed_url(url)
            data, mime = _fetch_bytes(url, deadline=deadline, max_bytes=MAX_IMAGE_BYTES)
            return _thumbnail(data, mime, 'Google Books')
        except Exception:
            return None

    def _remember(self, isbn: str, image: CoverImage | None) -> None:
        previous = self._cache.pop(isbn, None)
        if previous and previous[1]:
            self._cache_bytes -= len(previous[1].data)
        self._cache[isbn] = (time.monotonic() + (POSITIVE_TTL if image else NEGATIVE_TTL), image)
        self._cache_bytes += len(image.data) if image else 0
        while len(self._cache) > MAX_CACHE_ENTRIES or self._cache_bytes > MAX_CACHE_BYTES:
            _, (_, removed) = self._cache.popitem(last=False)
            self._cache_bytes -= len(removed.data) if removed else 0

    def get(self, isbn: str) -> CoverImage | None:
        canonical = canonical_isbn13(isbn)
        if not canonical:
            raise ValueError('유효한 ISBN-10 또는 ISBN-13을 입력해 주세요.')
        deadline = time.monotonic() + LOOKUP_BUDGET
        with self._lock:
            cached = self._cache.get(canonical)
            if cached and cached[0] > time.monotonic():
                self._cache.move_to_end(canonical)
                return cached[1]
            future = self._inflight.get(canonical)
            owner = future is None
            if owner:
                if len(self._inflight) >= MAX_PENDING:
                    return None
                future = Future()
                self._inflight[canonical] = future
        if owner:
            remaining = max(0.0, deadline - time.monotonic())
            if not self._slots.acquire(timeout=remaining):
                with self._lock:
                    self._inflight.pop(canonical, None)
                future.set_result(None)
                return None

            def work():
                image = None
                try:
                    image = self._lookup(canonical, deadline)
                except Exception:
                    pass
                finally:
                    with self._lock:
                        self._remember(canonical, image)
                        self._inflight.pop(canonical, None)
                    future.set_result(image)
                    self._slots.release()

            threading.Thread(target=work, daemon=True, name='suseoro-cover').start()
        try:
            return future.result(timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeout:
            return None
