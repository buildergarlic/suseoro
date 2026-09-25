"""Atomic recommendation imports and reversible librarian operations.

The product store owns connections; this module never opens a second connection
inside a write transaction. Identity indexes are built over the complete input,
so the order of files cannot attach an ISBN-less row to an ambiguous edition.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from suseoro.catalog.normalization import canonical_isbn13, normalize_author, normalize_key
from suseoro.simple.store import TEXT_FIELDS, identifier, now, validate_book


MAX_ROWS = 20000
BIB_FIELDS = ('title', 'author', 'publisher', 'published_date')
CONFLICT_WARNING = '추천자료의 서지 또는 정가가 서로 다릅니다. 원본을 비교해 주세요.'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def exact_text(value):
    # Preserve punctuation, spaces, author roles, numbers, volumes and editions.
    # Only Unicode presentation and repeated whitespace are normalized.
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value or '')).strip().casefold()


def bibliography_key(book):
    fields = tuple(exact_text(book.get(field, '')) for field in BIB_FIELDS)
    return fields if all(fields[:3]) else None


def duplicate_identity(book):
    isbn = canonical_isbn13(book.get('isbn'))
    if isbn:
        return ('isbn', isbn)
    if book.get('isbn'):
        return None
    key = bibliography_key(book)
    return ('bibliography', *key) if key else None


def annotate_review(books, holdings):
    """Build display-only warning fields from the same transaction snapshot."""
    owned_isbns = {canonical_isbn13(b['isbn']) for b in holdings if canonical_isbn13(b['isbn'])}
    owned_titles = {(normalize_key(b['title']), normalize_author(b['author'])) for b in holdings if b['title'] and b['author']}
    unidentified_titles = {(normalize_key(b['title']), normalize_author(b['author'])) for b in holdings if b['title'] and b['author'] and not canonical_isbn13(b['isbn'])}
    identities = [duplicate_identity(b) for b in books]
    counts = Counter(identities)
    for b, identity in zip(books, identities):
        isbn = canonical_isbn13(b['isbn'])
        title_key = (normalize_key(b['title']), normalize_author(b['author']))
        title_matches = unidentified_titles if isbn else owned_titles
        b['held'] = bool(isbn in owned_isbns or (all(title_key) and title_key in title_matches))
        b['held_match'] = 'isbn' if isbn in owned_isbns else 'title_author' if b['held'] else None
        b['holdings_status'] = ('unchecked' if not holdings else 'held' if b['held']
                               else 'not_held' if isbn or all(title_key) else 'uncheckable')
        b['duplicate'] = identity is not None and counts[identity] > 1
        messages = list(b.get('warnings', []))
        if b['isbn'] and not isbn:
            messages.append('ISBN 확인이 필요합니다.')
        if not b['title']:
            messages.append('도서명을 입력해 주세요.')
        if b['price'] is None:
            messages.append('가격을 확인해 주세요.')
        acknowledged = b.get('reviewed_warnings', [])
        if b['held'] and '소장목록에 같은 책이 있습니다.' not in acknowledged:
            messages.append('소장목록에 같은 책이 있습니다.')
        if b['duplicate'] and '현재 목록에 같은 책이 있습니다.' not in acknowledged:
            messages.append('현재 목록에 같은 책이 있습니다.')
        b['warnings'] = list(dict.fromkeys(messages))


def _validate_contribution(item):
    if not isinstance(item, dict):
        raise ValueError('추천 원본 정보 형식이 올바르지 않습니다.')
    for key, maximum, nullable in (('source', 2000, False), ('filename', 180, True),
                                   ('raw_text', 50000, False), ('import_id', 64, True)):
        if key in item and not (nullable and item[key] is None):
            if not isinstance(item[key], str) or len(item[key]) > maximum:
                raise ValueError('추천 원본의 출처·파일명·원문 형식이 올바르지 않습니다.')
    if item.get('row_number') is not None and (type(item['row_number']) is not int or not 1 <= item['row_number'] <= 1_000_000_000):
        raise ValueError('추천 원본 행 번호가 올바르지 않습니다.')
    for key in ('file_sha256', 'origin_fingerprint'):
        if key in item and (not isinstance(item[key], str) or re.fullmatch(r'[0-9a-f]{64}', item[key]) is None):
            raise ValueError('추천 원본 검증값이 올바르지 않습니다.')
    if 'values' not in item or not isinstance(item['values'], dict):
        raise ValueError('추천 원본의 도서 정보 형식이 올바르지 않습니다.')
    validate_book(item['values'])
    for key in ('raw_values', 'provenance'):
        if key in item and (not isinstance(item[key], dict) or len(encoded(item[key])) > 50000):
            raise ValueError('추천 원본의 위치 또는 값 형식이 올바르지 않습니다.')
    provenance = item.get('provenance', {})
    for key in ('filename', 'sheet'):
        if key in provenance and provenance[key] is not None and (not isinstance(provenance[key], str) or len(provenance[key]) > 2000):
            raise ValueError('추천 원본 위치 형식이 올바르지 않습니다.')
    for key in ('row', 'page'):
        if key in provenance and provenance[key] is not None and (type(provenance[key]) is not int or provenance[key] < 0):
            raise ValueError('추천 원본 위치 형식이 올바르지 않습니다.')


def restore_metadata(item, normalized):
    """Keep server-owned provenance/audit fields from portable JSON backups."""
    for key in ('contributions', 'sources', 'review_history'):
        if key not in item:
            continue
        values = item[key]
        if not isinstance(values, list) or len(values) > 1000000:
            raise ValueError('백업의 추천 출처 또는 확인 이력이 올바르지 않습니다.')
        if key == 'sources':
            if any(not isinstance(value, str) or len(value) > 2000 for value in values):
                raise ValueError('백업의 추천 출처가 올바르지 않습니다.')
        elif any(not isinstance(value, dict) for value in values):
            raise ValueError('백업의 원본 또는 확인 이력이 올바르지 않습니다.')
        if key == 'contributions':
            for value in values:
                _validate_contribution(value)
        if key == 'review_history':
            for value in values:
                warnings = value.get('warnings', [])
                if (value.get('action') != 'confirm_metadata' or not isinstance(value.get('at'), str)
                        or len(value['at']) > 100 or type(value.get('isbn_missing')) is not bool
                        or not isinstance(warnings, list) or len(warnings) > 110
                        or any(not isinstance(warning, str) or len(warning) > 4000 for warning in warnings)
                        or not isinstance(value.get('bibliography'), dict)):
                    raise ValueError('백업의 확인 이력이 올바르지 않습니다.')
                validate_book(value['bibliography'])
        normalized[key] = json.loads(encoded(values))
    if 'recommendation_count' in item:
        count = item['recommendation_count']
        if type(count) is not int or not 1 <= count <= 1_000_000_000:
            raise ValueError('백업의 추천 건수가 올바르지 않습니다.')
        normalized['recommendation_count'] = count


def _signature(book):
    return (*(exact_text(book.get(field, '')) for field in BIB_FIELDS), book.get('price'))


def _conflicts(left, right):
    left_isbn, right_isbn = canonical_isbn13(left.get('isbn')), canonical_isbn13(right.get('isbn'))
    if left_isbn and right_isbn and left_isbn != right_isbn:
        return True
    for field in BIB_FIELDS:
        a, b = exact_text(left.get(field, '')), exact_text(right.get(field, ''))
        if a and b and a != b:
            return True
    return left.get('price') is not None and right.get('price') is not None and left['price'] != right['price']


def _require_list(db, list_id):
    if not isinstance(list_id, str) or not db.execute('SELECT 1 FROM lists WHERE id=?', (list_id,)).fetchone():
        raise KeyError('목록을 찾을 수 없습니다.')


def _contribution(values, *, import_id=None, filename=None, row_number=None, file_sha256=None):
    result = {'import_id': import_id, 'filename': filename, 'row_number': row_number,
              'source': values.get('source', ''),
              'values': {key: values[key] for key in (*TEXT_FIELDS, 'price', 'quantity', 'warnings', 'selected', 'needs_review', 'priority') if key in values}}
    for key in ('raw_values', 'raw_text', 'provenance'):
        if key in values:
            result[key] = values[key]
    if file_sha256:
        result['file_sha256'] = file_sha256
        identity_values = dict(result['values'])
        # A different upload filename is not a second recommendation. Explicit
        # institution labels inside the data remain part of the contribution.
        if identity_values.get('source') == filename:
            identity_values['source'] = ''
        provenance = values.get('provenance')
        location = ({key: provenance[key] for key in ('sheet', 'row', 'page') if key in provenance}
                    if isinstance(provenance, dict) and provenance.get('row') is not None else {'row_number': row_number})
        result['origin_fingerprint'] = hashlib.sha256(encoded({
            'file_sha256': file_sha256, 'location': location,
            'values': identity_values}).encode('utf-8')).hexdigest()
    _validate_contribution(result)
    return result


def _ensure_history(book):
    if 'contributions' not in book:
        book['contributions'] = [_contribution(book)]
    if 'sources' not in book:
        book['sources'] = [book['source']] if book.get('source') else []
    book.setdefault('recommendation_count', len(book['contributions']))


def _save_operation(db, list_id, before, books, result, *, request_id=None, fingerprint=None,
                    deleted_ids=()):
    deleted_ids = set(deleted_ids)
    snapshots = []
    for book_id, previous in before.items():
        book = books[book_id]
        if previous is None:
            db.execute('INSERT INTO books(id,list_id,data) VALUES (?,?,?)', (book_id, list_id, encoded(book)))
        elif book_id in deleted_ids:
            # Keep the bibliographic data and provenance intact for backup and undo.
            db.execute('UPDATE books SET deleted=1 WHERE id=? AND list_id=?', (book_id, list_id))
        else:
            db.execute('UPDATE books SET data=? WHERE id=? AND list_id=?', (encoded(book), book_id, list_id))
        revision = db.execute('SELECT revision FROM book_revisions WHERE id=?', (book_id,)).fetchone()['revision']
        snapshots.append({'id': book_id, 'before': previous, 'after_revision': revision})
    operation_id = identifier()
    result = {**result, 'operation_id': operation_id}
    db.execute('INSERT INTO batch_operations(id,list_id,request_id,fingerprint,result,snapshots,created_at) VALUES (?,?,?,?,?,?,?)',
               (operation_id, list_id, request_id, fingerprint, encoded(result), encoded(snapshots), now()))
    return result


def import_batch(store, list_id, values):
    if not isinstance(values, dict):
        raise ValueError('가져오기 요청 형식을 확인해 주세요.')
    imports = values.get('imports')
    deduplicate = values.get('deduplicate', True)
    request_id = values.get('request_id')
    if (not isinstance(imports, list) or not imports or len(imports) > MAX_ROWS
            or type(deduplicate) is not bool or not isinstance(request_id, str)
            or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id) is None):
        raise ValueError('가져올 파일과 중복 합치기 설정을 확인해 주세요.')
    count = 0
    for item in imports:
        if (not isinstance(item, dict) or not isinstance(item.get('import_id'), str)
                or not isinstance(item.get('rows'), list) or not item['rows']):
            raise ValueError('파일별로 가져올 도서를 선택해 주세요.')
        count += len(item['rows'])
    if count > MAX_ROWS:
        raise ValueError('여러 파일을 합쳐 한 번에 최대 20,000권까지 가져올 수 있습니다.')
    try:
        fingerprint = hashlib.sha256(encoded({'imports': imports, 'deduplicate': deduplicate}).encode('utf-8')).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValueError('가져오기 자료 형식을 확인해 주세요.') from exc

    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _require_list(db, list_id)
        prior = db.execute('SELECT * FROM batch_operations WHERE list_id=? AND request_id=?', (list_id, request_id)).fetchone()
        if prior:
            if prior['fingerprint'] != fingerprint:
                raise ValueError('같은 가져오기 요청의 내용이 변경되었습니다. 새 요청으로 시도해 주세요.')
            if prior['undone']:
                raise ValueError('이미 되돌린 가져오기입니다. 파일을 다시 선택해 주세요.')
            return json.loads(prior['result'])

        # Validate every row and original file before any book is written.
        incoming, source_hashes = [], {}
        for item in imports:
            source = db.execute('SELECT * FROM imports WHERE id=?', (item['import_id'],)).fetchone()
            if not source:
                raise KeyError('원본 파일을 찾을 수 없습니다.')
            path = Path(source['path']).resolve()
            if (store.data_dir / 'sources').resolve() not in path.parents or not path.is_file():
                raise ValueError('원본 파일 위치가 올바르지 않습니다.')
            if str(path) not in source_hashes:
                with path.open('rb') as original_file:
                    source_hashes[str(path)] = hashlib.file_digest(original_file, 'sha256').hexdigest()
            for number, row in enumerate(item['rows'], 1):
                normalized = validate_book(row)
                original = dict(row)
                if not normalized['source']:
                    normalized['source'] = source['filename']
                    original['source'] = source['filename']
                contribution = _contribution(original, import_id=item['import_id'], filename=source['filename'], row_number=number,
                                             file_sha256=source_hashes[str(path)])
                incoming.append((normalized, contribution))

        existing_rows = list(db.execute('SELECT * FROM books WHERE list_id=? AND deleted=0 ORDER BY rowid', (list_id,)))
        originals = {row['id']: {'data': row['data'], 'deleted': row['deleted']} for row in existing_rows}
        books = {row['id']: json.loads(row['data']) for row in existing_rows}
        # Determine bibliographic ambiguity across existing and all incoming data.
        bibliography_isbns = defaultdict(set)
        isbn_fields = {}
        for row in [*books.values(), *(item[0] for item in incoming)]:
            key, isbn = bibliography_key(row), canonical_isbn13(row.get('isbn'))
            if key and isbn:
                bibliography_isbns[key].add(isbn)
            if isbn:
                fields = isbn_fields.setdefault(isbn, [set() for _ in BIB_FIELDS])
                for position, field in enumerate(BIB_FIELDS):
                    value = exact_text(row.get(field, ''))
                    if value:
                        fields[position].add(value)
        # Partial same-ISBN rows may jointly reveal a complete identity later.
        # Include that identity now so earlier ISBN-less rows stay conservative.
        for isbn, fields in isbn_fields.items():
            if all(len(field) == 1 for field in fields[:3]) and len(fields[3]) <= 1:
                bibliography_isbns[tuple(next(iter(field), '') for field in fields)].add(isbn)

        isbn_first, isbn_variants, text_first, text_variants = {}, {}, {}, {}
        conflict_isbns, conflict_bibliographies = set(), set()

        def index(book):
            isbn, key = canonical_isbn13(book['isbn']), bibliography_key(book)
            signature = _signature(book)
            if isbn:
                isbn_first.setdefault(isbn, book['id'])
                isbn_variants.setdefault((isbn, signature), book['id'])
                if key:
                    bibliography_isbns[key].add(isbn)
            if key and not (book['isbn'] and not isbn) and len(bibliography_isbns[key]) <= 1:
                text_first.setdefault(key, book['id'])
                text_variants.setdefault((key, signature), book['id'])

        for row in books.values():
            index(row)

        before, added, merged, source_sets, origin_sets = {}, 0, 0, {}, {}
        for row, contribution in incoming:
            isbn, key = canonical_isbn13(row['isbn']), bibliography_key(row)
            signature = _signature(row)
            target_id = None
            if deduplicate:
                if isbn and isbn in isbn_first:
                    anchor = books[isbn_first[isbn]]
                    if _conflicts(anchor, row):
                        conflict_isbns.add(isbn)
                    target_id = isbn_variants.get((isbn, signature), isbn_first[isbn])
                elif key and not (row['isbn'] and not isbn) and len(bibliography_isbns[key]) <= 1:
                    target_id = text_variants.get((key, signature), text_first.get(key))
                if target_id and _conflicts(books[target_id], row):
                    if isbn:
                        conflict_isbns.add(isbn)
                    elif key:
                        conflict_bibliographies.add(key)
                    target_id = None
            if target_id:
                target = books[target_id]
                _ensure_history(target)
                if target_id not in origin_sets:
                    origin_sets[target_id] = {item.get('origin_fingerprint') for item in target['contributions']
                                              if item.get('origin_fingerprint')}
                if contribution['origin_fingerprint'] in origin_sets[target_id]:
                    merged += 1
                    continue
                before.setdefault(target_id, originals.get(target_id))
                target['contributions'].append(contribution)
                origin_sets[target_id].add(contribution['origin_fingerprint'])
                target['recommendation_count'] += 1
                if target_id not in source_sets:
                    source_sets[target_id] = set(target['sources'])
                if row['source'] and row['source'] not in source_sets[target_id]:
                    target['sources'].append(row['source'])
                    source_sets[target_id].add(row['source'])
                # Fill blank bibliographic fields, retaining the first price,
                # purchase quantity, selection and librarian review decision.
                for field in TEXT_FIELDS:
                    if not target[field] and row[field]:
                        target[field] = row[field]
                if target_id not in originals or target.get('needs_review'):
                    target['needs_review'] = target['needs_review'] or row['needs_review']
                    target['warnings'] = list(dict.fromkeys([*target['warnings'], *row['warnings']]))[:100]
                index(target)
                merged += 1
            else:
                row['id'] = identifier()
                row['contributions'] = [contribution]
                row['sources'] = [row['source']] if row['source'] else []
                row['recommendation_count'] = 1
                books[row['id']] = row
                before[row['id']] = None
                index(row)
                added += 1

        # One final pass flags every conflicting variant, avoiding pairwise work.
        conflicts = 0
        for row in books.values():
            if canonical_isbn13(row['isbn']) in conflict_isbns or bibliography_key(row) in conflict_bibliographies:
                before.setdefault(row['id'], originals.get(row['id']))
                row['needs_review'] = True
                row['warnings'] = list(dict.fromkeys([CONFLICT_WARNING, *row.get('warnings', [])]))[:100]
                conflicts += 1
        warnings = [f'서지 또는 정가가 다른 도서 {conflicts}건을 합치지 않고 확인 대상으로 남겼습니다.'] if conflicts else []
        return _save_operation(db, list_id, before, books,
            {'added': added, 'merged': merged, 'input_count': count, 'warnings': warnings},
            request_id=request_id, fingerprint=fingerprint)


def bulk_books(store, list_id, values):
    if not isinstance(values, dict):
        raise ValueError('일괄 변경 요청 형식을 확인해 주세요.')
    book_ids, action = values.get('book_ids'), values.get('action')
    if (not isinstance(book_ids, list) or not 1 <= len(book_ids) <= MAX_ROWS
            or any(not isinstance(item, str) or not 1 <= len(item) <= 64 for item in book_ids)
            or len(set(book_ids)) != len(book_ids)
            or action not in ('confirm_metadata', 'select', 'hold', 'delete')):
        raise ValueError('변경할 도서와 작업을 다시 선택해 주세요.')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _require_list(db, list_id)
        # One list scan avoids SQLite parameter limits for large selections.
        rows = {row['id']: row for row in db.execute('SELECT * FROM books WHERE list_id=? AND deleted=0', (list_id,))}
        if any(item not in rows for item in book_ids):
            raise KeyError('현재 목록에서 일부 도서를 찾을 수 없습니다. 아무 도서도 변경하지 않았습니다.')
        displayed = {}
        if action == 'confirm_metadata':
            display_rows = [json.loads(row['data']) for row in rows.values()]
            holdings = [json.loads(row['data']) for row in db.execute('SELECT data FROM holdings')]
            annotate_review(display_rows, holdings)
            displayed = {row['id']: row for row in display_rows}
        before, books, skipped = {}, {}, []
        for book_id in book_ids:
            stored = rows[book_id]
            row = json.loads(stored['data'])
            if action == 'confirm_metadata':
                reason = None
                if not row['title']:
                    reason = '도서명을 입력해 주세요.'
                elif row['price'] is None:
                    reason = '가격을 확인해 주세요.'
                elif row['isbn'] and not canonical_isbn13(row['isbn']):
                    reason = '올바르지 않은 ISBN을 확인해 주세요.'
                elif not row['isbn'] and (not row['author'] or not row['publisher']):
                    reason = 'ISBN이 없으면 제목·저자·출판사를 모두 확인해 주세요.'
                if reason:
                    skipped.append({'id': book_id, 'reason': reason})
                    continue
                warnings = list(displayed[book_id]['warnings'])
                row['reviewed_warnings'] = list(dict.fromkeys([*row.get('reviewed_warnings', []), *warnings]))[:100]
                row.setdefault('review_history', []).append({'action': action, 'at': now(),
                    'warnings': warnings, 'isbn_missing': not bool(row['isbn']),
                    'bibliography': {field: row[field] for field in (*BIB_FIELDS, 'isbn', 'price')}})
                row['warnings'] = []
                row['needs_review'] = False
            elif action in ('select', 'hold'):
                row['selected'] = action == 'select'
            before[book_id] = {'data': stored['data'], 'deleted': stored['deleted']}
            books[book_id] = row
        return _save_operation(db, list_id, before, books, {'updated': len(before), 'skipped': skipped},
                               deleted_ids=book_ids if action == 'delete' else ())


def undo_operation(store, list_id, operation_id):
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _require_list(db, list_id)
        operation = db.execute('SELECT * FROM batch_operations WHERE id=? AND list_id=?', (operation_id, list_id)).fetchone()
        if not operation:
            raise KeyError('이 목록의 변경 이력을 찾을 수 없습니다.')
        if operation['undone']:
            return {'restored': 0}
        snapshots = json.loads(operation['snapshots'])
        # Validate the complete operation before restoring any individual book.
        for item in snapshots:
            current = db.execute('SELECT b.list_id,r.revision FROM books b JOIN book_revisions r ON b.id=r.id WHERE b.id=?', (item['id'],)).fetchone()
            if not current or current['list_id'] != list_id or current['revision'] != item['after_revision']:
                raise ValueError('이 작업 이후 변경된 도서가 있습니다. 최신 변경을 보존하기 위해 되돌리지 않았습니다.')
        for item in snapshots:
            if item['before'] is None:
                db.execute('DELETE FROM books WHERE id=? AND list_id=?', (item['id'], list_id))
            else:
                db.execute('UPDATE books SET data=?,deleted=? WHERE id=? AND list_id=?',
                    (item['before']['data'], item['before']['deleted'], item['id'], list_id))
        db.execute('UPDATE batch_operations SET undone=1 WHERE id=?', (operation_id,))
        return {'restored': len(snapshots)}
