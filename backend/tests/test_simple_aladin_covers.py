from io import BytesIO

from PIL import Image
import pytest

from suseoro.simple import covers

ISBN = '9791160517408'
URL = 'https://image.aladin.co.kr/product/38195/58/cover500/k612034712_1.jpg'


def test_aladin_exact_edition_is_used_before_foreign_providers(monkeypatch):
    picture = Image.new('RGB', (100, 150), 'navy')
    picture.putpixel((0, 0), (255, 255, 255))
    output = BytesIO()
    picture.save(output, 'PNG')
    page = f'<meta content="{ISBN}" property="books:isbn"><meta property="og:image" content="{URL}">'
    def fetch(url, **kwargs):
        if url == f'https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={ISBN}':
            return page.encode(), 'text/html'
        if url == URL:
            return output.getvalue(), 'image/png'
        raise OSError('Other providers have no Korean cover')
    monkeypatch.setattr(covers, '_fetch_bytes', fetch)
    result = covers.CoverService().get(ISBN)
    assert result is not None and result.source == 'Aladin'
    with Image.open(BytesIO(result.data)) as image:
        assert image.size == (100, 150)


@pytest.mark.parametrize('isbn,url', [
    ('9780140328721', URL), ('', URL),
    (ISBN, 'https://127.0.0.1/private'),
    (ISBN, 'https://image.aladin.co.kr.evil.example/product/1.jpg'),
    (ISBN, 'https://image.aladin.co.kr/img/no-cover.gif'),
])
def test_aladin_rejects_wrong_edition_and_non_cover_urls(monkeypatch, isbn, url):
    contacted = []
    def fetch(target, **kwargs):
        contacted.append(target)
        if target.startswith('https://www.aladin.co.kr/'):
            return f'<meta property="books:isbn" content="{isbn}"><meta property="og:image" content="{url}">'.encode(), 'text/html'
        raise OSError('No cover')
    monkeypatch.setattr(covers, '_fetch_bytes', fetch)
    assert covers.CoverService().get(ISBN) is None
    assert url not in contacted


def test_temporary_failure_does_not_hide_a_later_available_cover(monkeypatch):
    service = covers.CoverService()
    monkeypatch.setattr(service, '_lookup', lambda *args: None)
    assert service.get(ISBN) is None
    expected = covers.CoverImage(b'validated-image', 'Aladin')
    monkeypatch.setattr(service, '_lookup', lambda *args: expected)
    assert service.get(ISBN) == expected
