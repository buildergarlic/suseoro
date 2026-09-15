"""Serve only the trusted manuals packaged with the desktop application."""
from __future__ import annotations

import secrets
import sys
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse

HELP_FILES = frozenset({
    'index.html', 'visual-guide.html', 'user-guide.html',
    'quick-start.html', 'school-templates.html',
})

# The iframe has an opaque origin. It communicates only this close request;
# the parent validates the sending window before acting on it.
HELP_RUNTIME = """(() => {
  document.querySelectorAll('[data-suseoro-print]').forEach(button => {
    button.addEventListener('click', () => window.print());
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && window.parent !== window) {
      event.preventDefault();
      window.parent.postMessage({type: 'suseoro-help-close'}, '*');
    }
  });
})();"""


def default_help_dir() -> Path:
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / 'help'
    return Path(__file__).resolve().parents[4] / 'docs'


def help_policy(nonce: str) -> str:
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        "style-src 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'none'; object-src 'none'; frame-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    )


class _GuideHTML(HTMLParser):
    """Adapt our own HTML files without changing their SVG or script contents.

    This is not an upload sanitizer: callers must restrict inputs to the fixed
    bundled files. The sandbox and CSP keep practice scripts away from app data.
    """

    def __init__(self, nonce: str):
        super().__init__(convert_charrefs=False)
        self.nonce = nonce
        self.parts: list[str] = []
        self.runtime_added = False

    def _start(self, tag, attrs, *, closed=False):
        original = self.get_starttag_text()
        values = dict(attrs)
        changed = False
        print_action = (values.get('onclick') or '').strip().rstrip(';') == 'window.print()'
        clean = []
        for name, value in attrs:
            if name.startswith('on') or (tag == 'script' and name == 'nonce'):
                changed = True
                continue
            clean.append((name, value))
        if print_action:
            clean.append(('data-suseoro-print', ''))
            changed = True
        if tag == 'script':
            clean.append(('nonce', self.nonce))
            changed = True
        if tag == 'a':
            href = values.get('href') or ''
            destination = urlsplit(href)
            if destination.scheme or destination.netloc:
                clean = [(name, value) for name, value in clean if name not in ('target', 'rel')]
                clean.extend([('target', '_blank'), ('rel', 'noopener noreferrer')])
                changed = True
        if not changed:
            self.parts.append(original)
            return
        attributes = ''.join(
            ' ' + name if value is None else f' {name}="{escape(value, quote=True)}"'
            for name, value in clean
        )
        self.parts.append(f'<{tag}{attributes}' + ('/>' if closed else '>'))

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def add_runtime(self):
        if not self.runtime_added:
            self.parts.append(f'<script nonce="{self.nonce}">{HELP_RUNTIME}</script>')
            self.runtime_added = True

    def handle_endtag(self, tag):
        if tag == 'body':
            self.add_runtime()
        self.parts.append(f'</{tag}>')

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f'&{name};')

    def handle_charref(self, name):
        self.parts.append(f'&#{name};')

    def handle_comment(self, data):
        self.parts.append(f'<!--{data}-->')

    def handle_decl(self, decl):
        self.parts.append(f'<!{decl}>')


def manual_response(request: Request, directory: Path, filename: str) -> HTMLResponse:
    if filename not in HELP_FILES:
        return HTMLResponse('찾는 설명서가 없습니다.', status_code=404)
    # Mark only allowlisted guide responses as frameable, including readable
    # installation errors. Other app and API responses retain DENY.
    nonce = secrets.token_urlsafe(24)
    request.state.help_nonce = nonce
    try:
        root = directory.resolve()
        file = (root / filename).resolve()
        if file.parent != root:
            raise OSError('Manual is outside its bundle')
        source = file.read_text(encoding='utf-8-sig')
        status = 200
    except (OSError, UnicodeError):
        source = '''<!doctype html><html lang="ko"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>수서로 설명서</title><body style="font:18px/1.7 sans-serif;padding:32px;color:#234839">
        <h1>설명서 파일을 찾을 수 없습니다.</h1>
        <p>수서로를 최신 설치 파일로 다시 설치해 주세요.</p>
        <p><a href="https://github.com/buildergarlic/suseoro/releases/latest">최신 수서로 내려받기 ↗</a></p>
        </body></html>'''
        status = 503
    renderer = _GuideHTML(nonce)
    renderer.feed(source)
    renderer.close()
    renderer.add_runtime()
    return HTMLResponse(''.join(renderer.parts), status_code=status, headers={'Cache-Control': 'no-store'})
