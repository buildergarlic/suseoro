"""Small, edition-verified ISBN lookup for SuSeoRo.

Primary sources verified 2026-09-15:
https://www.nl.go.kr/NL/contents/N31101030500.do
https://openlibrary.org/dev/docs/api/books
https://openlibrary.org/developers/api
https://developers.google.com/books/docs/v1/using
https://developers.google.com/books/docs/v1/reference/volumes
https://openlibrary.org/developers/licensing
https://blog.aladin.co.kr/openapi

Open Library's /api/books endpoint is legacy; /isbn/{ISBN}.json resolves an
edition. The default API limit is one request/second, and interactive school
library lookup is explicitly within its intended use. Show provider provenance.
Google public-data calls need an API key (or OAuth), despite sometimes working
without one. Prices in saleInfo describe Google eBookstore purchaseability.
Aladin stopped issuing new keys on 2026-09-04 and ends all existing OpenAPI
access on 2026-10-30; this app therefore adds no Aladin dependency.

NL ISBN records contain PRE_PRICE (예정가격), not a current seller quote.
Google ebook prices are not used for print acquisition. Open Library has no
price. No provider response is accepted without its own matching ISBN.

Transport is deliberately a mockable module helper; no credentials are read
from the environment or logged. Socket operations time out after at most three
seconds, with a seven-second shared request budget and no retries. A bounded
daemon worker also keeps the public call within seven seconds if operating-system
DNS resolution stalls. At most four unfinished lookup workers may exist.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from suseoro.catalog.normalization import canonical_isbn13

REQUEST_TIMEOUT = 3.0
LOOKUP_BUDGET = 7.0
MAX_RESPONSE_BYTES = 1_000_000
_rate_lock = threading.Lock()
_next_request: dict[str, float] = {}
_PROVIDER_HOSTS = {"www.nl.go.kr", "www.googleapis.com", "openlibrary.org"}
_lookup_slots = threading.BoundedSemaphore(4)


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.suppressed += 1
        elif tag in {"br", "p", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.suppressed = max(0, self.suppressed - 1)
        elif tag in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clean for item in value if (clean := _text(item))]


def _blank(isbn: str = "") -> dict[str, Any]:
    return {"isbn": isbn, "title": "", "author": "", "publisher": "", "price": None,
            "published_date": "", "category": "", "link": "", "source": ""}


def _reserve_request(host: str, deadline: float) -> None:
    """Enforce Open Library's unidentified one-request-per-second limit."""
    if host != "openlibrary.org":
        return
    with _rate_lock:
        now = time.monotonic()
        scheduled = max(now, _next_request.get(host, 0.0))
        if scheduled >= deadline:
            raise TimeoutError()
        _next_request[host] = scheduled + 1.05
    wait = scheduled - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def _fetch_json(url: str, *, headers: dict[str, str] | None = None,
                timeout: float = REQUEST_TIMEOUT) -> Any:
    """Only fetch bounded JSON responses from our fixed provider hosts."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _PROVIDER_HOSTS:
        raise ValueError("Unsupported provider")
    deadline = time.monotonic() + timeout
    _reserve_request(parsed.hostname, deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    request = Request(url, headers={"Accept": "application/json",
                                   "User-Agent": "SuSeoRo/2.0 (school library ISBN lookup)",
                                   **(headers or {})})
    with urlopen(request, timeout=remaining) as response:
        chunks: list[bytes] = []
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("Response too large")
    return json.loads(b"".join(chunks).decode("utf-8-sig"))


def _request(url: str, deadline: float) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return _fetch_json(url, timeout=min(REQUEST_TIMEOUT, remaining))


def _price(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    # Only one unambiguous KRW amount; never extract digits from set/foreign prices.
    match = re.fullmatch(r"\s*(?:₩\s*)?(\d+|\d{1,3}(?:,\d{3})+)\s*(?:원|KRW)?\s*", value)
    return int(match[1].replace(",", "")) if match else None


def _date(value: Any) -> str:
    text = _text(value)
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _matching(value: Any, isbn: str) -> bool:
    return isinstance(value, str) and canonical_isbn13(value) == isbn


def _nl(isbn: str, key: str, deadline: float, warnings: list[str]) -> dict[str, Any] | None:
    payload = _request("https://www.nl.go.kr/seoji/SearchApi.do?" + urlencode({
        "cert_key": key, "result_style": "json", "page_no": 1, "page_size": 10,
        "isbn": isbn, "ebook_yn": "N",
    }), deadline)
    if not isinstance(payload, dict):
        raise ValueError("Invalid provider schema")
    error_code = str(payload.get("error_code", payload.get("ERROR_CODE", "")))
    if error_code in {"010", "011"}:
        warnings.append("국립중앙도서관 인증키를 확인해 주세요.")
        return None
    if error_code:
        warnings.append("국립중앙도서관에서 조회 오류를 반환했습니다.")
        return None
    docs = payload.get("docs", [])
    if not isinstance(docs, list):
        raise ValueError("Invalid provider schema")
    for item in docs:
        if (not isinstance(item, dict) or not _matching(item.get("EA_ISBN"), isbn)
                or item.get("EBOOK_YN") == "Y" or not _text(item.get("TITLE"))):
            continue
        book = _blank(isbn)
        book.update(title=_text(item.get("TITLE")), author=_text(item.get("AUTHOR")),
                    publisher=_text(item.get("PUBLISHER")), price=_price(item.get("PRE_PRICE")),
                    published_date=_date(item.get("PUBLISH_PREDATE")), category=_text(item.get("KDC")),
                    link="https://www.nl.go.kr/", source="국립중앙도서관")
        if book["price"] is not None:
            warnings.append("국립중앙도서관의 가격은 예정가격입니다. 현재 정가와 발행일을 확인해 주세요.")
        return book
    return None


def _google(isbn: str, key: str, deadline: float, warnings: list[str]) -> dict[str, Any] | None:
    payload = _request("https://www.googleapis.com/books/v1/volumes?" + urlencode({
        "q": f"isbn:{isbn}", "key": key, "maxResults": 10, "printType": "books",
    }), deadline)
    if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
        raise ValueError("Invalid provider schema")
    if payload.get("error"):
        warnings.append("Google Books에서 조회 오류를 반환했습니다. 인증키와 이용 한도를 확인해 주세요.")
        return None
    for item in payload.get("items", []):
        if not isinstance(item, dict) or not isinstance(item.get("volumeInfo"), dict):
            continue
        info = item["volumeInfo"]
        identifiers = info.get("industryIdentifiers", [])
        if not isinstance(identifiers, list) or not any(
            isinstance(identifier, dict) and identifier.get("type") in {"ISBN_10", "ISBN_13"}
            and _matching(identifier.get("identifier"), isbn) for identifier in identifiers
        ) or not _text(info.get("title")):
            continue
        link = _text(info.get("infoLink"))
        if urlparse(link).scheme not in {"https", "http"}:
            link = ""
        book = _blank(isbn)
        book.update(title=_text(info.get("title")), author=", ".join(_strings(info.get("authors"))),
                    publisher=_text(info.get("publisher")), published_date=_text(info.get("publishedDate")),
                    category=", ".join(_strings(info.get("categories"))), link=link, source="Google Books")
        return book
    return None


def _open_library(isbn: str, deadline: float, warnings: list[str]) -> dict[str, Any] | None:
    payload = _request(f"https://openlibrary.org/isbn/{isbn}.json", deadline)
    if not isinstance(payload, dict):
        raise ValueError("Invalid provider schema")
    identifiers = _strings(payload.get("isbn_13")) + _strings(payload.get("isbn_10"))
    if not any(_matching(identifier, isbn) for identifier in identifiers) or not _text(payload.get("title")):
        return None
    author = _text(payload.get("by_statement"))
    if not author:
        names: list[str] = []
        authors = payload.get("authors", [])
        if isinstance(authors, list):
            for item in authors[:3]:
                if not isinstance(item, dict):
                    continue
                name = _text(item.get("name"))
                author_key = item.get("key", "")
                if not name and isinstance(author_key, str) and re.fullmatch(r"/authors/OL\d+A", author_key):
                    try:
                        details = _request(f"https://openlibrary.org{author_key}.json", deadline)
                        name = _text(details.get("name")) if isinstance(details, dict) else ""
                    except (HTTPError, URLError, OSError, ValueError):
                        warnings.append("Open Library의 저자 정보 일부를 가져오지 못했습니다.")
                if name:
                    names.append(name)
            if len(authors) > 3:
                warnings.append("저자가 여러 명입니다. 전체 저자 표기를 확인해 주세요.")
        author = ", ".join(names)
    edition_key = payload.get("key", "")
    link = (f"https://openlibrary.org{edition_key}"
            if isinstance(edition_key, str) and re.fullmatch(r"/books/OL\d+M", edition_key)
            else f"https://openlibrary.org/isbn/{isbn}")
    book = _blank(isbn)
    book.update(title=_text(payload.get("title")), author=author,
                publisher=", ".join(_strings(payload.get("publishers"))),
                published_date=_text(payload.get("publish_date")), link=link, source="Open Library")
    return book


def _failure(provider: str, error: Exception) -> str:
    # Never include raw exception text: upstream URLs often contain personal keys.
    if isinstance(error, HTTPError):
        if error.code == 429:
            return f"{provider}의 조회 한도에 도달했습니다. 잠시 후 다시 조회해 주세요."
        if error.code in {401, 403}:
            return f"{provider}의 인증키 또는 접근 권한을 확인해 주세요."
        return f"{provider}에 연결하지 못했습니다. 잠시 후 다시 조회해 주세요."
    if isinstance(error, TimeoutError):
        return f"{provider}의 응답 대기 시간이 초과되었습니다."
    if isinstance(error, (URLError, OSError)):
        return f"{provider}에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요."
    return f"{provider}의 응답 형식이 올바르지 않습니다."


def _lookup_isbn(isbn: str, *, nl_api_key: str = "", google_api_key: str = "") -> dict[str, Any]:
    """Return a stable UI contract; missing data stays empty and prices nullable."""
    canonical = canonical_isbn13(isbn)
    if not canonical:
        return {"found": False, "book": _blank(), "warnings": ["유효한 ISBN-10 또는 ISBN-13을 입력해 주세요."]}
    deadline = time.monotonic() + LOOKUP_BUDGET
    warnings: list[str] = []
    providers = []
    if nl_api_key and nl_api_key.strip():
        providers.append(("국립중앙도서관", lambda: _nl(canonical, nl_api_key.strip(), deadline, warnings)))
    if google_api_key and google_api_key.strip():
        providers.append(("Google Books", lambda: _google(canonical, google_api_key.strip(), deadline, warnings)))
    providers.append(("Open Library", lambda: _open_library(canonical, deadline, warnings)))
    for provider, fetch in providers:
        if time.monotonic() >= deadline:
            warnings.append("조회 대기 시간이 초과되었습니다. 잠시 후 다시 조회해 주세요.")
            break
        try:
            book = fetch()
        except HTTPError as error:
            if error.code != 404:
                warnings.append(_failure(provider, error))
            continue
        except (URLError, OSError, ValueError) as error:
            warnings.append(_failure(provider, error))
            continue
        if book:
            if not book["author"]:
                warnings.append("저자 정보가 없습니다. 직접 입력해 주세요.")
            if not book["publisher"]:
                warnings.append("출판사 정보가 없습니다. 직접 입력해 주세요.")
            if book["price"] is None:
                warnings.append("확인된 종이책 가격이 없습니다. 정가를 직접 입력해 주세요.")
            return {"found": True, "book": book, "warnings": list(dict.fromkeys(warnings))}
    warnings.append("일치하는 ISBN의 도서 정보를 확인하지 못했습니다. 직접 입력해 주세요.")
    return {"found": False, "book": _blank(canonical), "warnings": list(dict.fromkeys(warnings))}


def lookup_isbn(isbn: str, *, nl_api_key: str = "", google_api_key: str = "") -> dict[str, Any]:
    """Bound public response time even when DNS or a provider does not respond."""
    canonical = canonical_isbn13(isbn)
    if not canonical:
        return {"found": False, "book": _blank(), "warnings": ["유효한 ISBN-10 또는 ISBN-13을 입력해 주세요."]}
    if not _lookup_slots.acquire(blocking=False):
        return {"found": False, "book": _blank(canonical),
                "warnings": ["도서 조회가 진행 중입니다. 잠시 후 다시 조회하거나 직접 입력해 주세요."]}
    results: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)

    def work() -> None:
        try:
            results.put(_lookup_isbn(canonical, nl_api_key=nl_api_key, google_api_key=google_api_key))
        except Exception:
            # A result must never expose a key through an unexpected exception.
            results.put({"found": False, "book": _blank(canonical),
                         "warnings": ["도서 조회를 완료하지 못했습니다. 잠시 후 다시 조회하거나 직접 입력해 주세요."]})
        finally:
            _lookup_slots.release()

    threading.Thread(target=work, daemon=True, name="suseoro-isbn-lookup").start()
    try:
        return results.get(timeout=LOOKUP_BUDGET)
    except queue.Empty:
        return {"found": False, "book": _blank(canonical),
                "warnings": ["조회 대기 시간이 초과되었습니다. 잠시 후 다시 조회하거나 직접 입력해 주세요."]}
