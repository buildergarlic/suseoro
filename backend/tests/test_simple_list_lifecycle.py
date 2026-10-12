from copy import deepcopy

import pytest

from suseoro.simple.store import LibraryStore


def test_edit_archive_and_restore_preserve_books_and_separate_holdings(tmp_path):
    store = LibraryStore(tmp_path)
    first = store.lists()[0]
    second = store.create_list({'name': '학생 희망도서'})
    book = store.add_book(second['id'], {'title': '살려 둘 책', 'price': 10000})
    removed = store.add_book(second['id'], {'title': '개별 삭제한 책'})
    store.delete_book(second['id'], removed['id'])
    store.add_holdings([{'title': '학교 소장 책'}])
    changed = store.update_list(second['id'], {'name': '학급 희망도서', 'year': 2027, 'budget': 50000, 'discount_percent': 10})
    assert changed['id'] == second['id'] and changed['created_at'] == second['created_at']
    assert store.list_state(second['id'])['summary']['order_total'] == 9000

    archived = store.delete_list(second['id'])
    assert archived['deleted_at']
    assert [item['id'] for item in store.lists()] == [first['id']]
    assert store.deleted_lists() == [archived]
    assert store.delete_list(second['id']) == archived
    with pytest.raises(KeyError):
        store.list_state(second['id'])
    reopened = LibraryStore(tmp_path)
    assert reopened.deleted_lists() == [archived]
    restored = reopened.restore_list(second['id'])
    assert 'deleted_at' not in restored and restored['name'] == '학급 희망도서'
    assert [item['id'] for item in reopened.list_state(second['id'])['books']] == [book['id']]
    assert reopened.list_holdings()['total'] == 1
    assert reopened.deleted_lists() == []


def test_last_active_list_cannot_be_deleted(tmp_path):
    store = LibraryStore(tmp_path)
    first = store.lists()[0]
    second = store.create_list({'name': '다른 목록'})
    store.delete_list(second['id'])
    before = store.backup()
    with pytest.raises(ValueError, match='마지막 구입 목록'):
        store.delete_list(first['id'])
    assert store.backup() == before


def test_archived_list_cannot_be_modified_by_stale_requests(tmp_path):
    store = LibraryStore(tmp_path)
    first = store.lists()[0]
    store.create_list({'name': '유지할 목록'})
    book = store.add_book(first['id'], {'title': '보관 도서'})
    store.delete_list(first['id'])
    before = store.backup()
    for change in (
        lambda: store.update_list(first['id'], {'name': '덮어쓰기'}),
        lambda: store.add_book(first['id'], {'title': '추가'}),
        lambda: store.update_book(first['id'], book['id'], {'title': '변경'}),
        lambda: store.delete_book(first['id'], book['id']),
        lambda: store.restore_book(first['id'], book['id']),
        lambda: store.bulk_books(first['id'], {'book_ids': [book['id']], 'action': 'delete'}),
    ):
        with pytest.raises(KeyError):
            change()
    assert store.backup() == before


def test_backup_retains_archived_lists_and_old_backups_remain_compatible(tmp_path):
    source = LibraryStore(tmp_path / 'source')
    first = source.lists()[0]
    source.create_list({'name': '사용 중 목록'})
    book = source.add_book(first['id'], {'title': '복원할 책'})
    source.delete_list(first['id'])
    backup = source.backup()
    target = LibraryStore(tmp_path / 'target')
    target.restore(backup)
    assert target.lists() == source.lists()
    assert target.deleted_lists() == source.deleted_lists()
    target.restore_list(first['id'])
    assert target.list_state(first['id'])['books'][0]['id'] == book['id']
    legacy = deepcopy(backup)
    for item in legacy['lists']:
        item.pop('deleted_at', None)
    target.restore(legacy)
    assert len(target.lists()) == 2 and target.deleted_lists() == []


def test_invalid_archive_metadata_does_not_change_existing_data(tmp_path):
    store = LibraryStore(tmp_path)
    before = store.backup()
    for marker in (False, '', 'not-a-date', 'x' * 65):
        damaged = deepcopy(before)
        damaged['lists'][0]['deleted_at'] = marker
        with pytest.raises(ValueError, match='삭제 이력'):
            store.restore(damaged)
        assert store.backup() == before
    damaged = deepcopy(before)
    damaged['lists'][0]['deleted_at'] = '2026-10-12T00:00:00+00:00'
    with pytest.raises(ValueError, match='사용할 구입 목록'):
        store.restore(damaged)
