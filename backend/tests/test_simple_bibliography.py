"""ISBN lookup contract and upstream failure tests; no live credentials or traffic."""

from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

ISBN = "9780140328721"
OTHER_ISBN = "9780980200447"


def module():
    from suseoro.simple import bibliography

    return bibliography


def install_transport(monkeypatch, responses):
    calls = []

    def transport(url, *, headers=None, timeout=0):
        calls.append((url, headers, timeout))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(module(), "_fetch_json", transport)
    return calls


def ol_book(**overrides):
    return {
        "key": "/books/OL7353617M",
        "isbn_13": [ISBN],
        "title": "Fantastic Mr. Fox",
        "by_statement": "Roald Dahl",
        "publishers": ["Puffin"],
        "publish_date": "October 1, 1988",
        **overrides,
    }


def nl_book(**overrides):
    return {"docs": [{
        "EA_ISBN": ISBN, "TITLE": "<b>책</b> &amp; 이야기",
        "AUTHOR": "저자: 홍길동", "PUBLISHER": "출판사", "PRE_PRICE": "15,000원",
        "PUBLISH_PREDATE": "20260101", "KDC": "813.8", **overrides,
    }]}


@pytest.mark.parametrize("isbn", ["", "12345", "9780140328722", "9790140328721", "ISBN13: nope"])
def test_invalid_isbn_never_queries_provider(monkeypatch, isbn):
    calls = install_transport(monkeypatch, [])
    result = module().lookup_isbn(isbn)
    assert result["found"] is False
    assert result["book"]["price"] is None
    assert "ISBN" in " ".join(result["warnings"])
    assert not calls


def test_isbn10_is_canonicalized_and_missing_price_is_not_zero(monkeypatch):
    calls = install_transport(monkeypatch, [ol_book()])
    result = module().lookup_isbn("0-14-032872-6")
    assert result["found"] is True
    assert result["book"] == {
        "isbn": ISBN, "title": "Fantastic Mr. Fox", "author": "Roald Dahl",
        "publisher": "Puffin", "price": None, "published_date": "October 1, 1988",
        "category": "", "link": "https://openlibrary.org/books/OL7353617M", "source": "Open Library",
    }
    assert len(calls) == 1
    assert urlparse(calls[0][0]).path == f"/isbn/{ISBN}.json"
    assert all(0 < call[2] <= 3 for call in calls)
    assert any("가격" in w for w in result["warnings"])


@pytest.mark.parametrize("ids", [[OTHER_ISBN], [], ["invalid"]])
def test_openlibrary_wrong_or_missing_edition_identifier_is_rejected(monkeypatch, ids):
    install_transport(monkeypatch, [ol_book(isbn_13=ids)])
    result = module().lookup_isbn(ISBN)
    assert result["found"] is False
    assert result["book"]["title"] == ""


def test_nl_exact_match_primary_and_estimated_price_is_labeled(monkeypatch):
    calls = install_transport(monkeypatch, [nl_book()])
    result = module().lookup_isbn(ISBN, nl_api_key="test-key")
    assert result["found"] is True
    assert result["book"]["title"] == "책 & 이야기"
    assert result["book"]["author"] == "저자: 홍길동"
    assert result["book"]["price"] == 15000
    assert result["book"]["published_date"] == "2026-01-01"
    assert result["book"]["source"] == "국립중앙도서관"
    assert any("예정가격" in warning for warning in result["warnings"])
    assert "test-key" not in str(result)
    assert len(calls) == 1
    assert parse_qs(urlparse(calls[0][0]).query)["isbn"] == [ISBN]


@pytest.mark.parametrize("price", [None, "", "가격불명", "전2권 30000원", "USD 15", "-1", "무료", True, 1.2])
def test_nl_ambiguous_price_is_not_inferred(monkeypatch, price):
    install_transport(monkeypatch, [nl_book(PRE_PRICE=price)])
    result = module().lookup_isbn(ISBN, nl_api_key="test-key")
    assert result["found"] is True
    assert result["book"]["price"] is None


def test_nl_set_isbn_is_not_accepted_as_individual_book(monkeypatch):
    install_transport(monkeypatch, [nl_book(EA_ISBN=OTHER_ISBN, SET_ISBN=ISBN), ol_book()])
    result = module().lookup_isbn(ISBN, nl_api_key="test-key")
    assert result["book"]["source"] == "Open Library"
    assert result["book"]["price"] is None


@pytest.mark.parametrize("failure", [TimeoutError("test-key"), URLError("test-key"),
    HTTPError("https://example.com/?key=test-key", 429, "test-key", {}, None),
    HTTPError("https://example.com/?key=test-key", 403, "test-key", {}, None),
    ValueError("malformed response test-key")])
def test_provider_failure_falls_back_without_key_in_warnings(monkeypatch, failure):
    calls = install_transport(monkeypatch, [failure, ol_book()])
    result = module().lookup_isbn(ISBN, nl_api_key="test-key")
    assert result["found"] is True
    assert result["book"]["source"] == "Open Library"
    assert result["warnings"]
    assert "test-key" not in str(result)
    assert len(calls) == 2


def test_nl_error_payload_falls_back(monkeypatch):
    install_transport(monkeypatch, [{"error_code": "011", "error_msg": "secret-key"}, ol_book()])
    result = module().lookup_isbn(ISBN, nl_api_key="secret-key")
    assert result["found"] is True
    assert any("인증" in w for w in result["warnings"])
    assert "secret-key" not in str(result)


def test_missing_author_and_publisher_remain_blank(monkeypatch):
    install_transport(monkeypatch, [ol_book(by_statement=None, authors=[], publishers=None)])
    result = module().lookup_isbn(ISBN)
    assert result["found"] is True
    assert result["book"]["author"] == ""
    assert result["book"]["publisher"] == ""
    assert any("저자" in w for w in result["warnings"])


def test_openlibrary_author_reference_is_resolved_with_fixed_provider_host(monkeypatch):
    calls = install_transport(monkeypatch, [
        ol_book(by_statement=None, authors=[{"key": "/authors/OL34184A"}]),
        {"name": "<b>Roald</b> Dahl"},
    ])
    result = module().lookup_isbn(ISBN)
    assert result["book"]["author"] == "Roald Dahl"
    assert calls[1][0] == "https://openlibrary.org/authors/OL34184A.json"


def test_malicious_author_reference_is_not_fetched(monkeypatch):
    calls = install_transport(monkeypatch, [ol_book(by_statement=None, authors=[{"key": "https://evil.example/secret"}])])
    result = module().lookup_isbn(ISBN)
    assert result["book"]["author"] == ""
    assert len(calls) == 1


def test_google_exact_isbn_only_and_ebook_price_not_used(monkeypatch):
    calls = install_transport(monkeypatch, [{"items": [
        {"volumeInfo": {"title": "Wrong edition", "industryIdentifiers": [{"type": "ISBN_13", "identifier": OTHER_ISBN}]}},
        {"volumeInfo": {"title": "<b>Matching title</b>", "authors": ["Author A", "Author B"],
                        "publisher": "Publisher", "industryIdentifiers": [{"type": "ISBN_13", "identifier": ISBN}],
                        "infoLink": "https://books.google.com/books?id=test", "categories": ["Fiction"]},
         "saleInfo": {"isEbook": True, "listPrice": {"amount": 9000, "currencyCode": "KRW"}}},
    ]}])
    result = module().lookup_isbn(ISBN, google_api_key="test-key")
    assert result["found"] is True
    assert result["book"]["title"] == "Matching title"
    assert result["book"]["author"] == "Author A, Author B"
    assert result["book"]["price"] is None
    assert result["book"]["source"] == "Google Books"
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [None, [], {"isbn_13": ISBN, "title": {}}, ol_book(title={}), ol_book(title="")])
def test_malformed_schema_is_not_accepted(monkeypatch, payload):
    install_transport(monkeypatch, [payload])
    assert module().lookup_isbn(ISBN)["found"] is False


def test_404_means_not_found_with_manual_entry_message(monkeypatch):
    install_transport(monkeypatch, [HTTPError("https://openlibrary.org/", 404, "Not found", {}, None)])
    result = module().lookup_isbn(ISBN)
    assert result["found"] is False
    assert any("직접 입력" in warning for warning in result["warnings"])


def test_total_deadline_prevents_next_provider_after_slow_first_request(monkeypatch):
    current = [0.0]
    calls = []
    monkeypatch.setattr(module().time, "monotonic", lambda: current[0])

    def transport(url, **kwargs):
        calls.append(url)
        current[0] = 20.0
        raise TimeoutError()

    monkeypatch.setattr(module(), "_fetch_json", transport)
    result = module().lookup_isbn(ISBN, nl_api_key="test-key")
    assert result["found"] is False
    assert len(calls) == 1


def test_html_script_contents_are_removed(monkeypatch):
    install_transport(monkeypatch, [ol_book(title='<script>alert(1)</script><b>Safe</b> &amp; sound')])
    assert module().lookup_isbn(ISBN)["book"]["title"] == "Safe & sound"


def test_local_rate_limiter_rejects_request_when_wait_exceeds_budget(monkeypatch):
    monkeypatch.setattr(module().time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(module(), "_next_request", {"openlibrary.org": 15.0})
    with pytest.raises(TimeoutError):
        module()._reserve_request("openlibrary.org", 12.0)


def test_public_lookup_has_hard_deadline_even_if_os_transport_hangs(monkeypatch):
    import threading
    import time

    released = threading.Event()
    finished = threading.Event()
    monkeypatch.setattr(module(), "LOOKUP_BUDGET", 0.02)

    def transport(url, **kwargs):
        released.wait(1)
        finished.set()
        return ol_book()

    monkeypatch.setattr(module(), "_fetch_json", transport)
    started = time.perf_counter()
    try:
        result = module().lookup_isbn(ISBN)
        assert time.perf_counter() - started < 0.3
        assert result["found"] is False
        assert any("대기 시간" in warning for warning in result["warnings"])
    finally:
        released.set()
        assert finished.wait(1)


def test_transport_does_not_access_an_unapproved_host():
    with pytest.raises(ValueError):
        module()._fetch_json("https://untrusted.example/book")


def test_transport_rejects_oversized_response(monkeypatch):
    from io import BytesIO

    monkeypatch.setattr(module(), "MAX_RESPONSE_BYTES", 20)
    monkeypatch.setattr(module(), "urlopen", lambda *args, **kwargs: BytesIO(b"x" * 30))
    with pytest.raises(ValueError):
        module()._fetch_json("https://www.nl.go.kr/seoji/SearchApi.do")


def test_transport_decodes_json_utf8_bom(monkeypatch):
    from io import BytesIO

    monkeypatch.setattr(module(), "urlopen", lambda *args, **kwargs: BytesIO('\ufeff{"title":"한글"}'.encode("utf-8")))
    assert module()._fetch_json("https://www.nl.go.kr/seoji/SearchApi.do") == {"title": "한글"}


def test_openlibrary_rate_error_is_reported_as_limit(monkeypatch):
    install_transport(monkeypatch, [HTTPError("https://openlibrary.org", 429, "Limit", {}, None)])
    result = module().lookup_isbn(ISBN)
    assert result["found"] is False
    assert any("한도" in warning for warning in result["warnings"])
