from pathlib import Path
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3

import pytest

from suseoro.simple.store import LibraryStore


def test_default_budget_and_persistent_changes(tmp_path):
    store = LibraryStore(tmp_path)
    first = store.lists()[0]
    assert first['budget'] == 15_000_000
    book = store.add_book(first['id'], {'title': '학생이 고른 책', 'price': 15000, 'quantity': 2})
    store.update_book(first['id'], book['id'], {'price': 16000, 'requester': '학생 희망도서'})
    reopened = LibraryStore(tmp_path)
    state = reopened.list_state(first['id'])
    assert state['books'][0]['price'] == 16000
    assert state['summary']['order_total'] == 32000
    assert state['summary']['remaining'] == 14_968_000


def test_holdings_status_distinguishes_unchecked_missing_and_exact_matches(tmp_path):
    store = LibraryStore(tmp_path)
    list_id = store.lists()[0]['id']
    store.add_book(list_id, {'title': '소장 책', 'author': '김작가', 'isbn': '9791160517408'})
    assert store.list_state(list_id)['books'][0]['holdings_status'] == 'unchecked'
    store.replace_holdings([{'title': '다른 책', 'author': '박작가', 'isbn': '9780140328721'}])
    assert store.list_state(list_id)['books'][0]['holdings_status'] == 'not_held'
    store.replace_holdings([{'title': '소장 책', 'author': '김작가', 'isbn': '9791160517408'}])
    state = store.list_state(list_id)
    assert state['holdings_count'] == 1
    assert state['books'][0]['holdings_status'] == 'held'
    assert state['books'][0]['held_match'] == 'isbn'
    store.replace_holdings([{'title': '소장 책', 'author': '김작가'}])
    assert store.list_state(list_id)['books'][0]['held_match'] == 'title_author'
    store.add_book(list_id, {'title': '정보 부족'})
    assert store.list_state(list_id)['books'][1]['holdings_status'] == 'uncheckable'


def test_display_preferences_default_and_survive_a_new_store_instance(tmp_path):
    store = LibraryStore(tmp_path)
    assert store.settings()['text_size'] == 16
    assert store.settings()['row_density'] == 'comfortable'
    store.update_settings({'text_size': 20, 'row_density': 'compact', 'nl_api_key': 'local-secret'})
    reopened = LibraryStore(tmp_path)
    assert reopened.settings()['text_size'] == 20
    assert reopened.settings()['row_density'] == 'compact'
    assert reopened.settings()['nl_api_key_configured'] is True
    assert 'local-secret' not in str(reopened.settings())
    assert reopened.backup()['settings'] == {'school_name': '우리 학교'}
    assert 'local-secret' not in str(reopened.backup())
    reopened.restore(reopened.backup())
    assert reopened.settings()['text_size'] == 20
    assert reopened.settings()['row_density'] == 'compact'


@pytest.mark.parametrize('size', [16, 18, 20, 22, 24])
def test_supported_text_sizes_are_returned_as_integers(tmp_path, size):
    store = LibraryStore(tmp_path)
    result = store.update_settings({'text_size': size})
    assert result['text_size'] == size and type(result['text_size']) is int
    assert LibraryStore(tmp_path).settings()['text_size'] == size


@pytest.mark.parametrize('stored_size', ['16', '18', '20'])
def test_existing_text_size_is_not_replaced_by_the_new_default(tmp_path, stored_size):
    store = LibraryStore(tmp_path)
    with store.connection() as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('text_size', stored_size))
    assert LibraryStore(tmp_path).settings()['text_size'] == int(stored_size)


def test_unreadable_saved_text_size_falls_back_to_readable_default(tmp_path):
    store = LibraryStore(tmp_path)
    with store.connection() as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('text_size', 'invalid'))
    assert LibraryStore(tmp_path).settings()['text_size'] == 16


@pytest.mark.parametrize('patch', [
    {'text_size': 17}, {'text_size': 26}, {'text_size': '18'}, {'text_size': 18.0}, {'text_size': True},
    {'text_size': None}, {'row_density': 'dense'}, {'row_density': ''},
    {'row_density': None}, {'row_density': []},
])
def test_invalid_display_preferences_do_not_partially_change_settings(tmp_path, patch):
    store = LibraryStore(tmp_path)
    store.update_settings({'text_size': 18, 'row_density': 'compact', 'school_name': '기존 학교'})
    before = store.settings()
    with pytest.raises(ValueError):
        store.update_settings({'school_name': '변경 학교', **patch})
    assert LibraryStore(tmp_path).settings() == before


def test_unknown_price_is_not_a_free_book_and_export_requires_review(tmp_path):
    store = LibraryStore(tmp_path)
    list_id = store.lists()[0]['id']
    store.add_book(list_id, {'title': '가격 확인 필요'})
    state = store.list_state(list_id)
    assert state['summary']['missing_price_count'] == 1
    with pytest.raises(ValueError, match='가격'):
        store.export_selection(list_id)


def test_wishlist_and_delete_undo_excluded_from_budget(tmp_path):
    store = LibraryStore(tmp_path)
    list_id = store.lists()[0]['id']
    book = store.add_book(list_id, {'title': '책', 'price': 20000})
    store.update_book(list_id, book['id'], {'selected': False})
    assert store.list_state(list_id)['summary']['order_total'] == 0
    store.delete_book(list_id, book['id'])
    assert store.list_state(list_id)['books'] == []
    store.restore_book(list_id, book['id'])
    assert len(store.list_state(list_id)['books']) == 1


def test_discount_rounding_and_over_budget_export(tmp_path):
    store = LibraryStore(tmp_path)
    list_id = store.lists()[0]['id']
    store.update_list(list_id, {'budget': 100, 'discount_percent': 10})
    store.add_book(list_id, {'title': '책', 'price': 105, 'quantity': 2})
    assert store.list_state(list_id)['summary']['order_total'] == 190
    with pytest.raises(ValueError, match='예산'):
        store.export_selection(list_id)


def test_fractional_discount_uses_decimal_won_rounding(tmp_path):
    store = LibraryStore(tmp_path)
    list_id = store.lists()[0]['id']
    store.update_list(list_id, {'discount_percent': 9.7})
    store.add_book(list_id, {'title': '책', 'price': 999, 'quantity': 2})
    assert store.list_state(list_id)['summary']['order_total'] == 1804


def test_holding_and_duplicate_isbn_warnings_are_not_deletion(tmp_path):
    store = LibraryStore(tmp_path)
    list_id = store.lists()[0]['id']
    row = {'title': '책', 'isbn': '9788936434267', 'price': 10000}
    store.add_book(list_id, row)
    store.add_book(list_id, row)
    store.add_holdings([row])
    state = store.list_state(list_id)
    assert len(state['books']) == 2
    assert all(b['held'] and b['duplicate'] for b in state['books'])


@pytest.mark.parametrize('patch', [{'quantity': 0}, {'price': -1}, {'price': 1.5}, {'selected': 'false'}, {'priority': 'critical'}, {'quantity': True}])
def test_invalid_book_data_rejected(tmp_path, patch):
    store = LibraryStore(tmp_path)
    with pytest.raises(ValueError):
        store.add_book(store.lists()[0]['id'], {'title': '책', **patch})


def test_backup_restore_excludes_secret_and_saves_current_data(tmp_path):
    store = LibraryStore(tmp_path)
    list_id = store.lists()[0]['id']
    store.update_settings({'school_name': '우리초', 'nl_api_key': 'secret-local-only'})
    store.add_book(list_id, {'title': '백업 도서', 'price': 10000})
    backup = store.backup()
    assert 'secret-local-only' not in str(backup)
    store.add_book(list_id, {'title': '나중 도서', 'price': 20000})
    store.restore(backup)
    assert len(store.list_state(list_id)['books']) == 1
    assert store.secret('nl_api_key') == 'secret-local-only'
    assert list((tmp_path / 'backups').glob('*.json'))


def test_restore_invalid_backup_does_not_touch_existing(tmp_path):
    store = LibraryStore(tmp_path)
    before = store.backup()
    broken = {**before, 'books': [{'id': 'broken'}]}
    with pytest.raises(ValueError):
        store.restore(broken)
    assert store.backup() == before


def test_book_is_scoped_to_purchase_list(tmp_path):
    store = LibraryStore(tmp_path)
    first = store.lists()[0]['id']
    second = store.create_list({'name': '다음 회차'})['id']
    book = store.add_book(first, {'title': '첫 회차', 'price': 1000})
    with pytest.raises(KeyError):
        store.update_book(second, book['id'], {'title': '잘못된 회차'})


def test_different_verified_edition_is_not_marked_as_same_holding(tmp_path):
    store = LibraryStore(tmp_path)
    list_id = store.lists()[0]['id']
    store.add_holdings([{'title': '같은 서명', 'author': '작가', 'isbn': '9780140328721'}])
    store.add_book(list_id, {'title': '같은 서명', 'author': '작가', 'isbn': '9788936434267', 'price': 10000})
    assert store.list_state(list_id)['books'][0]['held'] is False


def test_import_provenance_survives_edit_and_acknowledgement_clears_old_warnings(tmp_path):
    store = LibraryStore(tmp_path)
    list_id = store.lists()[0]['id']
    book = store.add_book(list_id, {'title': '원본', 'price': None, 'needs_review': True,
                                   'warnings': ['정가를 확인해 주세요.'], 'raw_values': {'책명': '원본', '가격': '?'},
                                   'provenance': {'filename': '추천.xlsx', 'sheet': '목록', 'row': 5}})
    store.update_book(list_id, book['id'], {'title': '수정 제목', 'price': 14000, 'needs_review': False})
    saved = store.list_state(list_id)['books'][0]
    assert saved['raw_values']['책명'] == '원본'
    assert saved['provenance']['row'] == 5
    assert saved['warnings'] == []


def test_new_holdings_snapshot_replaces_old_and_invalid_snapshot_preserves_it(tmp_path):
    store = LibraryStore(tmp_path)
    list_id = store.lists()[0]['id']
    first = {'title': '예전 소장', 'isbn': '9780140328721', 'price': 10000}
    second = {'title': '현재 소장', 'isbn': '9788936434267', 'price': 10000}
    store.add_book(list_id, first)
    store.replace_holdings([first])
    assert store.list_state(list_id)['books'][0]['held']
    with pytest.raises(ValueError):
        store.replace_holdings([])
    assert store.list_state(list_id)['books'][0]['held']
    store.replace_holdings([second])
    assert not store.list_state(list_id)['books'][0]['held']


def _remember_attachment(store, kind, attachment_id, filename, content):
    folder = 'sources' if kind == 'imports' else 'templates'
    path = store.data_dir / folder / filename
    path.write_bytes(content)
    store.remember_file(kind, attachment_id, filename, path)
    return path


def _school_template_bytes():
    from io import BytesIO
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(['도서명', 'ISBN', '정가', '수량'])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_backup_reads_one_consistent_snapshot_during_concurrent_import(tmp_path, monkeypatch):
    store = LibraryStore(tmp_path)
    writer = LibraryStore(tmp_path)
    original_connection = store.connection
    statements = []
    inserted = False

    def trace(statement):
        nonlocal inserted
        statements.append(statement)
        if 'SELECT data FROM lists' in statement and not inserted:
            inserted = True
            new_list = writer.create_list({'name': '동시에 만든 목록'})
            writer.add_book(new_list['id'], {'title': '동시에 저장한 책', 'price': 1000})

    @contextmanager
    def traced_connection():
        with original_connection() as db:
            db.set_trace_callback(trace)
            yield db

    monkeypatch.setattr(store, 'connection', traced_connection)
    backup = store.backup()
    assert inserted
    assert sum(statement == 'BEGIN' for statement in statements) == 1
    assert len(backup['lists']) == 1
    assert backup['books'] == []
    assert len(writer.lists()) == 2


def test_backup_restores_originals_and_school_templates_on_another_pc(tmp_path):
    source = LibraryStore(tmp_path / 'source')
    target = LibraryStore(tmp_path / 'target')
    raw = '도서명,ISBN,정가\n책,9788937464010,10000'.encode('utf-8')
    template = _school_template_bytes()
    _remember_attachment(source, 'imports', 'import-1', '추천.csv', raw)
    _remember_attachment(source, 'templates', 'template-1', '학교양식.xlsx', template)
    source.update_settings({'nl_api_key': 'source-secret'})
    target.update_settings({'nl_api_key': 'target-secret'})
    backup = source.backup()
    assert backup['version'] == 1
    assert len(backup['attachments']) == 2
    assert 'source-secret' not in json.dumps(backup)
    assert all('path' not in item for item in backup['attachments'])
    # Backup-supplied paths are never filesystem destinations.
    backup['attachments'][0]['path'] = str(tmp_path / 'outside.csv')
    target.restore(backup)
    assert not (tmp_path / 'outside.csv').exists()
    for kind, item_id, expected in [('imports', 'import-1', raw), ('templates', 'template-1', template)]:
        restored = target.stored_file(kind, item_id)
        path = Path(restored['path'])
        assert target.data_dir in path.parents
        assert len(path.stem) == 32 and all(character in '0123456789abcdef' for character in path.stem)
        assert path.read_bytes() == expected
    assert target.secret('nl_api_key') == 'target-secret'


@pytest.mark.parametrize('damage', ['base64', 'hash', 'size', 'duplicate', 'filename', 'kind', 'kind_type'])
def test_bad_backup_attachment_is_rejected_before_data_or_files_change(tmp_path, damage):
    store = LibraryStore(tmp_path)
    _remember_attachment(store, 'imports', 'import-1', '추천.csv', b'title,isbn\nbook,9788937464010')
    backup = store.backup()
    broken = deepcopy(backup)
    item = broken['attachments'][0]
    if damage == 'base64':
        item['content_base64'] = 'not base64!'
    elif damage == 'hash':
        item['sha256'] = '0' * 64
    elif damage == 'size':
        item['size_bytes'] += 1
    elif damage == 'duplicate':
        broken['attachments'].append(dict(item))
    elif damage == 'filename':
        item['filename'] = '../escape.csv'
    elif damage == 'kind':
        item['kind'] = 'settings'
    else:
        item['kind'] = []
    files_before = sorted(str(path) for path in tmp_path.rglob('*') if path.is_file())
    with pytest.raises(ValueError):
        store.restore(broken)
    assert store.backup() == backup
    assert sorted(str(path) for path in tmp_path.rglob('*') if path.is_file()) == files_before


def test_backup_refuses_external_and_database_attachment_paths(tmp_path):
    store = LibraryStore(tmp_path / 'data')
    external = tmp_path / 'external.csv'
    external.write_bytes(b'private outside data')
    store.remember_file('imports', 'bad', 'external.csv', external)
    with pytest.raises(ValueError, match='위치'):
        store.backup()
    with store.connection() as db:
        db.execute('UPDATE imports SET path=? WHERE id=?', (str(store.database), 'bad'))
    with pytest.raises(ValueError, match='위치'):
        store.backup()


def test_attachment_size_limits_apply_on_backup_and_restore(tmp_path, monkeypatch):
    import suseoro.simple.store as module

    store = LibraryStore(tmp_path)
    _remember_attachment(store, 'imports', 'import-1', 'one.csv', b'12345')
    _remember_attachment(store, 'imports', 'import-2', 'two.csv', b'12345')
    backup = store.backup()
    monkeypatch.setattr(module, 'BACKUP_MAX_ATTACHMENT_BYTES', 4)
    with pytest.raises(ValueError, match='크기'):
        store.backup()
    with pytest.raises(ValueError, match='크기'):
        store.restore(backup)
    monkeypatch.setattr(module, 'BACKUP_MAX_ATTACHMENT_BYTES', 10)
    monkeypatch.setattr(module, 'BACKUP_MAX_ATTACHMENTS_BYTES', 9)
    with pytest.raises(ValueError, match='크기'):
        store.backup()
    with pytest.raises(ValueError, match='크기'):
        store.restore(backup)


def test_legacy_version_one_backup_without_attachments_remains_compatible(tmp_path):
    store = LibraryStore(tmp_path)
    source = _remember_attachment(store, 'imports', 'import-1', 'original.csv', b'title\nbook')
    legacy = store.backup()
    legacy.pop('attachments', None)
    store.restore(legacy)
    assert Path(store.stored_file('imports', 'import-1')['path']) == source


def test_restore_sql_failure_keeps_existing_data_and_removes_new_staged_files(tmp_path, monkeypatch):
    source = LibraryStore(tmp_path / 'source')
    _remember_attachment(source, 'imports', 'import-1', 'incoming.csv', b'title\nnew')
    backup = source.backup()
    target = LibraryStore(tmp_path / 'target')
    target.add_book(target.lists()[0]['id'], {'title': '기존 자료', 'price': 1000})
    before = target.backup()
    original_connection = target.connection

    @contextmanager
    def rejecting_connection():
        with original_connection() as db:
            db.set_authorizer(lambda action, first, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and first == 'imports' else sqlite3.SQLITE_OK)
            yield db

    monkeypatch.setattr(target, 'connection', rejecting_connection)
    with pytest.raises(sqlite3.Error):
        target.restore(backup)
    assert target.backup() == before
    assert list((target.data_dir / 'sources').iterdir()) == []
    safety = list((target.data_dir / 'backups').glob('before-restore-*.json'))
    assert len(safety) == 1
    assert json.loads(safety[0].read_text(encoding='utf-8')) == before


def test_restored_backup_keeps_list_order_and_acknowledgement_history(tmp_path):
    source = LibraryStore(tmp_path / 'source')
    first = source.lists()[0]['id']
    source.create_list({'name': '두 번째 목록'})
    book = source.add_book(first, {'title': '확인할 책', 'price': 1000, 'needs_review': True, 'warnings': ['원문을 확인하세요.']})
    source.update_book(first, book['id'], {'needs_review': False})
    backup = source.backup()
    target = LibraryStore(tmp_path / 'target')
    target.restore(backup)
    assert [row['id'] for row in target.lists()] == [row['id'] for row in source.lists()]
    assert target.list_state(first)['books'][0]['reviewed_warnings'] == ['원문을 확인하세요.']


def test_json_size_limit_is_enforced_before_restore_changes(tmp_path, monkeypatch):
    import suseoro.simple.store as module

    store = LibraryStore(tmp_path)
    backup = store.backup()
    monkeypatch.setattr(module, 'BACKUP_MAX_JSON_BYTES', 100)
    with pytest.raises(ValueError, match='크기'):
        store.backup()
    with pytest.raises(ValueError, match='크기'):
        store.restore(backup)
    assert list((tmp_path / 'backups').iterdir()) == []


def test_new_backup_empty_manifest_replaces_file_registry_but_keeps_safety_copy(tmp_path):
    source = LibraryStore(tmp_path / 'source')
    target = LibraryStore(tmp_path / 'target')
    old_path = _remember_attachment(target, 'imports', 'old-import', 'old.csv', b'title\nold')
    target.restore(source.backup())
    with pytest.raises(KeyError):
        target.stored_file('imports', 'old-import')
    assert old_path.read_bytes() == b'title\nold'
    safety_path = next((target.data_dir / 'backups').glob('*.json'))
    safety = json.loads(safety_path.read_text(encoding='utf-8'))
    assert safety['attachments'][0]['id'] == 'old-import'


@pytest.mark.parametrize('damage', ['missing', 'empty', 'outside'])
def test_valid_backup_can_repair_a_damaged_current_attachment(tmp_path, damage):
    store = LibraryStore(tmp_path / 'data')
    original = _remember_attachment(store, 'imports', 'import-1', 'original.csv', b'title\nbook')
    backup = store.backup()
    if damage == 'missing':
        original.unlink()
    elif damage == 'empty':
        original.write_bytes(b'')
    else:
        outside = tmp_path / 'outside.csv'
        outside.write_bytes(b'private outside data')
        with store.connection() as db:
            db.execute('UPDATE imports SET path=? WHERE id=?', (str(outside), 'import-1'))
    with pytest.raises(ValueError):
        store.backup()
    store.restore(backup)
    restored = Path(store.stored_file('imports', 'import-1')['path'])
    assert restored.read_bytes() == b'title\nbook'
    safety_file = next((store.data_dir / 'backups').glob('*.json'))
    safety = json.loads(safety_file.read_text(encoding='utf-8'))
    assert safety['attachments'] == []
    assert safety['unavailable_attachments'][0]['id'] == 'import-1'
    assert 'path' not in safety['unavailable_attachments'][0]
