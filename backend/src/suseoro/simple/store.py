"""Transactional personal purchase lists, with explicit unknown prices and backups."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from suseoro.catalog.normalization import canonical_isbn13, normalize_key, normalize_author

TEXT_FIELDS = ('title', 'author', 'publisher', 'isbn', 'category', 'requester', 'audience',
               'source', 'note', 'published_date', 'link')
BOOK_DEFAULTS = {**{key: '' for key in TEXT_FIELDS}, 'price': None, 'quantity': 1,
                 'selected': True, 'priority': 'normal', 'needs_review': False, 'warnings': []}

BACKUP_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
BACKUP_MAX_ATTACHMENTS_BYTES = 200 * 1024 * 1024
BACKUP_MAX_JSON_BYTES = 300 * 1024 * 1024
BACKUP_MAX_ATTACHMENTS = 2000
_ATTACHMENT_FOLDERS = {'imports': 'sources', 'templates': 'templates'}
_IMPORT_SUFFIXES = {'.xlsx', '.xls', '.xlsb', '.ods', '.csv', '.tsv', '.txt', '.pdf', '.hwp', '.hwpx', '.docx'}


def _backup_size(payload):
    """Bound the actual UTF-8 JSON size, including base64 and book provenance."""
    total = 0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False).iterencode(payload):
            total += len(chunk.encode('utf-8'))
            if total > BACKUP_MAX_JSON_BYTES:
                raise ValueError('백업 전체 크기가 300 MB를 초과합니다. 자료를 나누어 보관해 주세요.')
    except (TypeError, UnicodeError) as exc:
        raise ValueError('백업 자료 형식이 올바르지 않습니다.') from exc


def _attachment_identity(item):
    if not isinstance(item, dict):
        raise ValueError('백업 첨부 정보가 올바르지 않습니다.')
    kind, item_id, filename = item.get('kind'), item.get('id'), item.get('filename')
    if not isinstance(kind, str) or kind not in _ATTACHMENT_FOLDERS:
        raise ValueError('백업 첨부 종류가 올바르지 않습니다.')
    if not isinstance(item_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', item_id) is None:
        raise ValueError('백업 첨부 식별자가 올바르지 않습니다.')
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 180
            or re.search(r'[\x00-\x1f\x7f<>:"/\\|?*]', filename)
            or filename.rstrip(' .') != filename):
        raise ValueError('백업 첨부 파일명이 올바르지 않습니다.')
    suffix = Path(filename).suffix.lower()
    if suffix not in ({'.xlsx'} if kind == 'templates' else _IMPORT_SUFFIXES):
        raise ValueError('백업 첨부 파일 형식이 올바르지 않습니다.')
    return kind, item_id, filename, suffix


def _validated_attachments(backup):
    """Validate the entire manifest before any files or stored data change.

    Missing attachments means an older version-1 backup; preserve that version's
    behavior of restoring records while retaining existing local file entries.
    """
    if 'attachments' not in backup:
        return None
    attachments = backup['attachments']
    if not isinstance(attachments, list) or len(attachments) > BACKUP_MAX_ATTACHMENTS:
        raise ValueError('백업 첨부 파일 수가 올바르지 않습니다.')
    identities, normalized, total = set(), [], 0
    for item in attachments:
        kind, item_id, filename, suffix = _attachment_identity(item)
        if item_id in identities:
            raise ValueError('백업 첨부 식별자가 중복되었습니다.')
        identities.add(item_id)
        size, digest, encoded = item.get('size_bytes'), item.get('sha256'), item.get('content_base64')
        if type(size) is not int or not 0 < size <= BACKUP_MAX_ATTACHMENT_BYTES:
            raise ValueError('백업 첨부 파일의 개별 크기 제한을 초과했거나 크기가 올바르지 않습니다.')
        total += size
        if total > BACKUP_MAX_ATTACHMENTS_BYTES:
            raise ValueError('백업 첨부 파일의 전체 크기 제한을 초과했습니다.')
        if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
            raise ValueError('백업 첨부 파일의 검증값이 올바르지 않습니다.')
        if not isinstance(encoded, str) or len(encoded) != 4 * ((size + 2) // 3):
            raise ValueError('백업 첨부 파일의 크기 또는 인코딩이 올바르지 않습니다.')
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('백업 첨부 파일의 인코딩이 올바르지 않습니다.') from exc
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError('백업 첨부 파일이 손상되었습니다. 크기와 검증값을 확인해 주세요.')
        normalized.append({'kind': kind, 'id': item_id, 'filename': filename,
                           'suffix': suffix, 'content': content})
    return normalized


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier() -> str:
    return uuid.uuid4().hex


def integer(value, label, minimum=0, maximum=1_000_000_000):
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError(f'{label}: {minimum:,}~{maximum:,} 범위의 정수를 입력해 주세요.')
    return value


def validate_book(values: dict, previous: dict | None = None) -> dict:
    if not isinstance(values, dict):
        raise ValueError('도서 정보 형식을 확인해 주세요.')
    result = {**BOOK_DEFAULTS, **(previous or {})}
    for key in TEXT_FIELDS:
        if key in values:
            if not isinstance(values[key], str) or len(values[key]) > (12000 if key == 'note' else 2000):
                raise ValueError(f'{key} 항목이 너무 길거나 올바른 글자가 아닙니다.')
            result[key] = values[key].strip()
    for key in ('selected', 'needs_review'):
        if key in values:
            if type(values[key]) is not bool:
                raise ValueError('구매 여부와 확인 여부를 다시 선택해 주세요.')
            result[key] = values[key]
    if 'price' in values:
        result['price'] = None if values['price'] is None else integer(values['price'], '정가')
    if 'quantity' in values:
        result['quantity'] = integer(values['quantity'], '수량', 1, 10000)
    if 'priority' in values:
        if values['priority'] not in ('high', 'normal', 'low'):
            raise ValueError('우선순위를 다시 선택해 주세요.')
        result['priority'] = values['priority']
    if 'warnings' in values:
        warnings = values['warnings']
        if not isinstance(warnings, list) or len(warnings) > 100 or any(not isinstance(w, str) or len(w) > 4000 for w in warnings):
            raise ValueError('확인 메시지 형식이 올바르지 않습니다.')
        result['warnings'] = warnings
    # Original values are retained separately from the editable bibliographic fields.
    for key in ('raw_values', 'raw_text', 'provenance'):
        if key in values and (not previous or key not in previous):
            try:
                encoded = json.dumps(values[key], ensure_ascii=False)
            except (ValueError, TypeError) as exc:
                raise ValueError('원본 정보 형식이 올바르지 않습니다.') from exc
            if len(encoded) > 50000:
                raise ValueError('한 도서의 원본 정보가 너무 깁니다.')
            result[key] = json.loads(encoded)
    if previous and previous.get('needs_review') and values.get('needs_review') is False:
        result['reviewed_warnings'] = list(previous.get('warnings', []))
        result['warnings'] = []
    isbn = canonical_isbn13(result['isbn'])
    if isbn:
        result['isbn'] = isbn
    if result['link'] and not result['link'].startswith('https://'):
        result['link'] = ''
    if not result['title'] and not result['isbn'] and not result['needs_review']:
        raise ValueError('도서명이나 ISBN을 입력해 주세요.')
    return result


def validate_list(values: dict, previous=None):
    result = {**{'name': f'{datetime.now().year} 도서 구입', 'year': datetime.now().year,
                 'budget': 15_000_000, 'discount_percent': 0}, **(previous or {})}
    if 'name' in values:
        if not isinstance(values['name'], str) or not values['name'].strip() or len(values['name']) > 120:
            raise ValueError('목록 이름을 1~120자로 입력해 주세요.')
        result['name'] = values['name'].strip()
    for key, label, low, high in (('year', '연도', 2000, 2200), ('budget', '예산', 0, 1_000_000_000)):
        if key in values:
            result[key] = integer(values[key], label, low, high)
    if 'discount_percent' in values:
        value = values['discount_percent']
        if type(value) not in (int, float):
            raise ValueError('할인율은 0~100 사이 숫자로 입력해 주세요.')
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 100 or amount != amount.quantize(Decimal('0.01')):
            raise ValueError('할인율은 0~100 사이, 소수점 둘째 자리까지 입력해 주세요.')
        result['discount_percent'] = float(amount)
    return result


def unit_amount(book: dict, discount: float) -> int:
    return int((Decimal(book['price'] or 0) * (100 - Decimal(str(discount))) / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


class LibraryStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.data_dir / 'library.sqlite3'
        for folder in ('sources', 'templates', 'backups'):
            (self.data_dir / folder).mkdir(exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lists (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS books (id TEXT PRIMARY KEY, list_id TEXT NOT NULL REFERENCES lists(id),
                    data TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS book_list ON books(list_id, deleted);
                CREATE TABLE IF NOT EXISTS holdings (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, filename TEXT NOT NULL, path TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS templates (id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL);
            ''')
            if not db.execute('SELECT 1 FROM lists LIMIT 1').fetchone():
                data = {**validate_list({}), 'id': identifier(), 'created_at': now()}
                db.execute('INSERT INTO lists VALUES (?,?)', (data['id'], json.dumps(data, ensure_ascii=False)))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        with self.connection() as db:
            rows = dict(db.execute('SELECT key,value FROM settings'))
        size = rows.get('text_size', '16')
        density = rows.get('row_density', 'comfortable')
        return {'school_name': rows.get('school_name', '우리 학교'),
                'nl_api_key_configured': bool(rows.get('nl_api_key')),
                'text_size': int(size) if size in ('16', '18', '20') else 16,
                'row_density': density if density in ('comfortable', 'compact') else 'comfortable'}

    def secret(self, name):
        with self.connection() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (name,)).fetchone()
            return row['value'] if row else ''

    def update_settings(self, values):
        allowed = {'school_name', 'nl_api_key', 'text_size', 'row_density'}
        for key, value in values.items():
            if key not in allowed:
                raise ValueError('설정값을 확인해 주세요.')
            if key == 'text_size':
                if type(value) is not int or value not in (16, 18, 20):
                    raise ValueError('글자 크기는 16, 18, 20 중에서 선택해 주세요.')
            elif key == 'row_density':
                if not isinstance(value, str) or value not in ('comfortable', 'compact'):
                    raise ValueError('행 간격을 다시 선택해 주세요.')
            elif not isinstance(value, str) or len(value) > 1000:
                raise ValueError('설정값을 확인해 주세요.')
        with self.connection() as db:
            for key, value in values.items():
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value).strip()))
        return self.settings()

    def lists(self):
        with self.connection() as db:
            return [json.loads(r['data']) for r in db.execute('SELECT data FROM lists ORDER BY rowid DESC')]

    def get_list(self, list_id):
        with self.connection() as db:
            row = db.execute('SELECT data FROM lists WHERE id=?', (list_id,)).fetchone()
        if not row:
            raise KeyError('목록을 찾을 수 없습니다.')
        return json.loads(row['data'])

    def create_list(self, values):
        data = {**validate_list(values), 'id': identifier(), 'created_at': now()}
        with self.connection() as db:
            db.execute('INSERT INTO lists VALUES (?,?)', (data['id'], json.dumps(data, ensure_ascii=False)))
        return data

    def update_list(self, list_id, values):
        data = validate_list(values, self.get_list(list_id))
        with self.connection() as db:
            db.execute('UPDATE lists SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), list_id))
        return data

    def add_book(self, list_id, values):
        return self.add_books(list_id, [values])[0]

    def add_books(self, list_id, values):
        self.get_list(list_id)
        if len(values) > 20000:
            raise ValueError('한 번에 최대 20,000권까지 가져올 수 있습니다.')
        books = [{**validate_book(v), 'id': identifier()} for v in values]
        with self.connection() as db:
            db.executemany('INSERT INTO books (id,list_id,data) VALUES (?,?,?)',
                           [(b['id'], list_id, json.dumps(b, ensure_ascii=False)) for b in books])
        return books

    def update_book(self, list_id, book_id, values):
        with self.connection() as db:
            row = db.execute('SELECT data FROM books WHERE id=? AND list_id=? AND deleted=0', (book_id, list_id)).fetchone()
            if not row:
                raise KeyError('도서를 찾을 수 없습니다.')
            data = validate_book(values, json.loads(row['data']))
            db.execute('UPDATE books SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), book_id))
        return data

    def delete_book(self, list_id, book_id):
        with self.connection() as db:
            result = db.execute('UPDATE books SET deleted=1 WHERE id=? AND list_id=?', (book_id, list_id))
            if not result.rowcount:
                raise KeyError('도서를 찾을 수 없습니다.')

    def restore_book(self, list_id, book_id):
        with self.connection() as db:
            result = db.execute('UPDATE books SET deleted=0 WHERE id=? AND list_id=?', (book_id, list_id))
            if not result.rowcount:
                raise KeyError('도서를 찾을 수 없습니다.')
        return self.update_book(list_id, book_id, {})

    def add_holdings(self, rows):
        if len(rows) > 100000:
            raise ValueError('소장목록은 한 번에 100,000행까지 가져올 수 있습니다.')
        books = [validate_book(row) for row in rows]
        with self.connection() as db:
            db.executemany('INSERT INTO holdings VALUES (?,?)', [(identifier(), json.dumps(b, ensure_ascii=False)) for b in books])
        return len(books)

    def replace_holdings(self, rows):
        if not rows or len(rows) > 100000:
            raise ValueError('소장목록은 1~100,000행의 자료를 선택해 주세요. 기존 소장목록을 유지합니다.')
        books = [validate_book(row) for row in rows]
        if not any(canonical_isbn13(b['isbn']) or (b['title'] and b['author']) for b in books):
            raise ValueError('소장목록에서 ISBN 또는 도서명·저자를 확인할 수 없습니다. 열 연결을 확인해 주세요.')
        with self.connection() as db:
            db.execute('DELETE FROM holdings')
            db.executemany('INSERT INTO holdings VALUES (?,?)', [(identifier(), json.dumps(b, ensure_ascii=False)) for b in books])
        return len(books)

    def list_state(self, list_id):
        info = self.get_list(list_id)
        with self.connection() as db:
            books = [json.loads(r['data']) for r in db.execute('SELECT data FROM books WHERE list_id=? AND deleted=0 ORDER BY rowid', (list_id,))]
            holdings = [json.loads(r['data']) for r in db.execute('SELECT data FROM holdings')]
        owned_isbns = {canonical_isbn13(b['isbn']) for b in holdings if canonical_isbn13(b['isbn'])}
        owned_titles = {(normalize_key(b['title']), normalize_author(b['author'])) for b in holdings if b['title'] and b['author']}
        unidentified_titles = {(normalize_key(b['title']), normalize_author(b['author'])) for b in holdings if b['title'] and b['author'] and not canonical_isbn13(b['isbn'])}
        identities = []
        for b in books:
            isbn = canonical_isbn13(b['isbn'])
            identities.append(('isbn', isbn) if isbn else ('text', normalize_key(b['title']), normalize_author(b['author'])))
        from collections import Counter
        counts = Counter(identities)
        for b, identity in zip(books, identities):
            isbn = canonical_isbn13(b['isbn'])
            title_key = (normalize_key(b['title']), normalize_author(b['author']))
            title_matches = unidentified_titles if isbn else owned_titles
            b['held'] = bool(isbn in owned_isbns or (all(title_key) and title_key in title_matches))
            b['duplicate'] = counts[identity] > 1 and bool(isbn or all(title_key))
            messages = list(b.get('warnings', []))
            if b['isbn'] and not isbn:
                messages.append('ISBN 확인이 필요합니다.')
            if not b['title']:
                messages.append('도서명을 입력해 주세요.')
            if b['price'] is None:
                messages.append('가격을 확인해 주세요.')
            if b['held']:
                messages.append('소장목록에 같은 책이 있습니다.')
            if b['duplicate']:
                messages.append('현재 목록에 같은 책이 있습니다.')
            b['warnings'] = list(dict.fromkeys(messages))
        selected = [b for b in books if b['selected']]
        order_total = sum(unit_amount(b, info['discount_percent']) * b['quantity'] for b in selected)
        summary = {'selected_count': len(selected), 'total_quantity': sum(b['quantity'] for b in selected),
                   'list_total': sum((b['price'] or 0) * b['quantity'] for b in selected),
                   'order_total': order_total, 'remaining': info['budget'] - order_total,
                   'missing_price_count': sum(b['price'] is None for b in selected),
                   'review_count': sum(b['needs_review'] or bool(b['warnings']) for b in books),
                   'held_count': sum(b['held'] for b in books), 'duplicate_count': sum(b['duplicate'] for b in books)}
        return {'list': info, 'books': books, 'summary': summary}

    def export_selection(self, list_id):
        state = self.list_state(list_id)
        selected = [b for b in state['books'] if b['selected']]
        if not selected:
            raise ValueError('발주할 책을 먼저 선택해 주세요.')
        if state['summary']['missing_price_count']:
            raise ValueError('가격이 비어 있는 도서가 있습니다. 가격을 확인하거나 구매 선택을 해제해 주세요.')
        if any(not b['title'] or b['needs_review'] or (b['isbn'] and not canonical_isbn13(b['isbn'])) for b in selected):
            raise ValueError('확인이 필요한 도서가 있습니다. 도서명·ISBN과 확인필요 표시를 검토해 주세요.')
        if state['summary']['remaining'] < 0:
            raise ValueError('예산을 초과했습니다. 구매 목록이나 예산을 조정해 주세요.')
        return selected, state['list']

    def _attachment_root(self, kind):
        root = (self.data_dir / _ATTACHMENT_FOLDERS[kind]).resolve()
        if self.data_dir not in root.parents or not root.is_dir():
            raise ValueError('첨부 파일 저장 위치가 올바르지 않습니다.')
        return root

    def _backup_snapshot(self, db, *, allow_unavailable_attachments=False):
        # Callers own an explicit read/write transaction. Do not use settings()
        # or lists(): their separate connections would observe another snapshot.
        setting = db.execute("SELECT value FROM settings WHERE key='school_name'").fetchone()
        payload = {
            'schema': 'suseoro-library-v2', 'version': 1,
            'settings': {'school_name': setting['value'] if setting else '우리 학교'},
            'lists': [json.loads(row['data']) for row in db.execute('SELECT data FROM lists ORDER BY rowid DESC')],
            'books': [{'list_id': row['list_id'], 'deleted': bool(row['deleted']), **json.loads(row['data'])}
                      for row in db.execute('SELECT * FROM books ORDER BY rowid')],
            'holdings': [json.loads(row['data']) for row in db.execute('SELECT data FROM holdings ORDER BY rowid')],
            'attachments': [],
        }
        if len(payload['lists']) > 1000 or len(payload['books']) > 100000 or len(payload['holdings']) > 100000:
            raise ValueError('백업에 담을 자료 수가 제한을 초과했습니다. 목록 또는 소장자료를 정리한 뒤 다시 시도해 주세요.')
        total, identities = 0, set()
        for kind, name_column in (('imports', 'filename'), ('templates', 'name')):
            root = self._attachment_root(kind)
            for row in db.execute(f'SELECT * FROM {kind} ORDER BY rowid'):
                metadata = {'kind': kind, 'id': row['id'], 'filename': row[name_column]}
                _attachment_identity(metadata)
                if row['id'] in identities:
                    raise ValueError('백업 첨부 식별자가 중복되었습니다.')
                identities.add(row['id'])
                if len(identities) > BACKUP_MAX_ATTACHMENTS:
                    raise ValueError('백업 첨부 파일 수가 제한을 초과했습니다.')
                try:
                    path = Path(row['path']).resolve(strict=True)
                    if root not in path.parents or not path.is_file():
                        raise ValueError('원본 파일 위치가 올바르지 않습니다. 앱의 첨부 폴더 안에 있는 파일만 백업할 수 있습니다.')
                    with path.open('rb') as source:
                        before = os.fstat(source.fileno())
                        if not 0 < before.st_size <= BACKUP_MAX_ATTACHMENT_BYTES:
                            raise ValueError('백업 첨부 파일의 개별 크기 제한을 초과했거나 빈 파일입니다.')
                        if total + before.st_size > BACKUP_MAX_ATTACHMENTS_BYTES:
                            raise ValueError('백업 첨부 파일의 전체 크기 제한을 초과했습니다.')
                        content = source.read(BACKUP_MAX_ATTACHMENT_BYTES + 1)
                        after = os.fstat(source.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(content) != before.st_size:
                        raise ValueError('백업 중 원본 파일이 변경되었습니다. 파일을 닫고 다시 백업해 주세요.')
                except (OSError, ValueError) as exc:
                    message = (str(exc) if isinstance(exc, ValueError) else
                               '백업할 원본 파일 위치를 확인해 주세요. 원본 파일을 찾거나 읽을 수 없습니다.')
                    if not allow_unavailable_attachments:
                        raise ValueError(message) from exc
                    # A valid backup must be able to repair missing/damaged
                    # originals. Preserve every current record and readable
                    # attachment, and explicitly record the unavailable ones
                    # in the safety copy without reading an unsafe location.
                    payload.setdefault('unavailable_attachments', []).append({**metadata, 'reason': message})
                    continue
                total += len(content)
                payload['attachments'].append({**metadata, 'size_bytes': len(content),
                                               'sha256': hashlib.sha256(content).hexdigest(),
                                               'content_base64': base64.b64encode(content).decode('ascii')})
        _backup_size(payload)
        return payload

    def backup(self):
        with self.connection() as db:
            db.execute('BEGIN')
            return self._backup_snapshot(db)

    def _safety_backup(self, payload):
        folder = (self.data_dir / 'backups').resolve()
        if self.data_dir not in folder.parents or not folder.is_dir():
            raise ValueError('안전 백업 저장 위치가 올바르지 않습니다.')
        name = f'before-restore-{identifier()}'
        pending = folder / (name + '.partial')
        complete = folder / (name + '.json')
        try:
            with pending.open('x', encoding='utf-8', newline='\n') as destination:
                json.dump(payload, destination, ensure_ascii=False, indent=2, allow_nan=False)
                destination.flush()
                os.fsync(destination.fileno())
            pending.replace(complete)
        except BaseException:
            pending.unlink(missing_ok=True)
            raise

    def restore(self, backup):
        if not isinstance(backup, dict) or backup.get('schema') != 'suseoro-library-v2' or backup.get('version') != 1:
            raise ValueError('수서로 2.0 백업 파일이 아닙니다.')
        _backup_size(backup)
        attachments = _validated_attachments(backup)
        try:
            lists = backup['lists']
            books = backup['books']
            holdings = backup['holdings']
            if not isinstance(lists, list) or not lists or len(lists) > 1000 or not isinstance(books, list) or len(books) > 100000 or not isinstance(holdings, list) or len(holdings) > 100000:
                raise ValueError('백업에 담긴 자료 수가 올바르지 않습니다.')
            ids = set()
            normalized_lists = []
            for item in lists:
                if not isinstance(item['id'], str) or not 1 <= len(item['id']) <= 64 or item['id'] in ids:
                    raise ValueError('목록 식별자가 올바르지 않습니다.')
                ids.add(item['id'])
                normalized_lists.append({**validate_list(item), 'id': item['id'], 'created_at': str(item.get('created_at', now()))})
            normalized_books = []
            book_ids = set()
            for item in books:
                if item['list_id'] not in ids or not isinstance(item['id'], str) or not 1 <= len(item['id']) <= 64 or item['id'] in book_ids or type(item.get('deleted', False)) is not bool:
                    raise ValueError('도서의 목록 연결이 올바르지 않습니다.')
                book_ids.add(item['id'])
                normalized = {**validate_book(item), 'id': item['id']}
                if 'reviewed_warnings' in item:
                    reviewed = item['reviewed_warnings']
                    if not isinstance(reviewed, list) or len(reviewed) > 100 or any(not isinstance(value, str) or len(value) > 4000 for value in reviewed):
                        raise ValueError('백업의 확인 이력이 올바르지 않습니다.')
                    normalized['reviewed_warnings'] = reviewed
                normalized_books.append((normalized, item['list_id'], item.get('deleted', False)))
            normalized_holdings = [validate_book(item) for item in holdings]
            school = backup.get('settings', {}).get('school_name', '우리 학교')
            if not isinstance(school, str) or len(school) > 1000:
                raise ValueError('학교 이름이 올바르지 않습니다.')
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('백업 구조가 올바르지 않습니다. 기존 자료를 유지합니다.') from exc
        staged = []
        try:
            # New immutable files are complete before DB references are changed.
            # Original files stay available to both the old DB and safety backup.
            for attachment in attachments or []:
                path = self._attachment_root(attachment['kind']) / (identifier() + attachment['suffix'])
                with path.open('xb') as destination:
                    staged.append((attachment, path))
                    destination.write(attachment['content'])
                    destination.flush()
                    os.fsync(destination.fileno())
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                self._safety_backup(self._backup_snapshot(db, allow_unavailable_attachments=True))
                db.execute('DELETE FROM books')
                db.execute('DELETE FROM lists')
                db.execute('DELETE FROM holdings')
                # lists() is newest-first; reverse insertion retains that order.
                db.executemany('INSERT INTO lists VALUES (?,?)', [(x['id'], json.dumps(x, ensure_ascii=False)) for x in reversed(normalized_lists)])
                db.executemany('INSERT INTO books VALUES (?,?,?,?)', [(b['id'], lid, json.dumps(b, ensure_ascii=False), int(deleted)) for b, lid, deleted in normalized_books])
                db.executemany('INSERT INTO holdings VALUES (?,?)', [(identifier(), json.dumps(b, ensure_ascii=False)) for b in normalized_holdings])
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('school_name', school))
                if attachments is not None:
                    db.execute('DELETE FROM imports')
                    db.execute('DELETE FROM templates')
                    for attachment, path in staged:
                        kind = attachment['kind']
                        db.execute(f'INSERT INTO {kind} VALUES (?,?,?)', (attachment['id'], attachment['filename'], str(path)))
        except BaseException:
            # Only this restore's freshly generated paths can be removed.
            for attachment, path in staged:
                if path.resolve().parent == self._attachment_root(attachment['kind']):
                    path.unlink(missing_ok=True)
            raise

    def remember_file(self, kind, file_id, filename, path):
        if kind not in ('imports', 'templates'):
            raise ValueError('지원하지 않는 파일 유형입니다.')
        with self.connection() as db:
            db.execute(f'INSERT INTO {kind} VALUES (?,?,?)', (file_id, filename, str(path)))

    def stored_file(self, kind, file_id):
        if kind not in ('imports', 'templates'):
            raise ValueError('지원하지 않는 파일 유형입니다.')
        with self.connection() as db:
            row = db.execute(f'SELECT * FROM {kind} WHERE id=?', (file_id,)).fetchone()
        if not row:
            raise KeyError('원본 파일을 찾을 수 없습니다.')
        path = Path(row['path']).resolve()
        if self.data_dir not in path.parents or not path.is_file():
            raise ValueError('원본 파일 위치가 올바르지 않습니다.')
        return dict(row)
