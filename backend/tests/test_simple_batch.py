"""Synthetic multi-file librarian workflows: atomicity, identity and undo."""
import copy

import pytest
from fastapi.testclient import TestClient

from suseoro.simple.app import create_app
from suseoro.simple.store import LibraryStore


@pytest.fixture
def store(tmp_path):
    return LibraryStore(tmp_path)


def book(**values):
    return {'title': 'Matilda 1', 'author': 'Roald Dahl', 'publisher': 'Puffin',
            'published_date': '1988', 'price': 12000, **values}


def source(store, name, rows):
    file_id = name.replace('.', '-')
    path = store.data_dir / 'sources' / name
    path.write_text('synthetic source', encoding='utf-8')
    store.remember_file('imports', file_id, name, path)
    return {'import_id': file_id, 'rows': rows}


def commit(store, imports, **kwargs):
    return store.import_batch(store.lists()[0]['id'], {'imports': imports,
        'request_id': 'request-1', **kwargs})


def records(store):
    return store.list_state(store.lists()[0]['id'])['books']


def test_cross_file_isbn_merge_retains_provenance_without_summing_quantity(store):
    first = source(store, 'school.csv', [book(isbn='0140328726', quantity=2,
        selected=False, raw_values={'original': 'A'}, source='학교')])
    second = source(store, 'library.csv', [book(isbn='9780140328721', quantity=4,
        raw_text='original B', source='도서관')])
    result = commit(store, [first, second])
    assert (result['added'], result['merged'], result['input_count']) == (1, 1, 2)
    merged = records(store)[0]
    assert merged['isbn'] == '9780140328721'
    assert merged['quantity'] == 2 and merged['selected'] is False
    assert merged['recommendation_count'] == 2
    assert merged['sources'] == ['학교', '도서관']
    assert {p['filename'] for p in merged['contributions']} == {'school.csv', 'library.csv'}
    assert merged['contributions'][0]['raw_values'] == {'original': 'A'}
    assert merged['contributions'][1]['raw_text'] == 'original B'
    assert merged['contributions'][0]['values']['isbn'] == '0140328726'


def test_isbnless_duplicates_and_distinct_volume_editions(store):
    rows = [book(), book(), book(title='Matilda 2'), book(title='Matilda 1 revised'),
            book(author='Other author'), book(publisher='Other'),
            book(published_date='2000'), book(title='Matilda-1')]
    result = commit(store, [source(store, 'rows.csv', rows)])
    assert (result['added'], result['merged']) == (7, 1)
    assert not any(row['duplicate'] for row in records(store))


def test_valid_and_isbnless_cross_match_only_for_unambiguous_exact_identity(store):
    rows = [book(), book(isbn='9780140328721')]
    assert commit(store, [source(store, 'rows.csv', rows)])['merged'] == 1
    assert records(store)[0]['isbn'] == '9780140328721'


@pytest.mark.parametrize('rows', [
    [book(), book(isbn='9780140328721'), book(isbn='9788936434267')],
    [book(isbn='9788936434267'), book(), book(isbn='9780140328721')],
    [book(isbn='broken'), book()],
])
def test_ambiguous_or_invalid_isbn_never_cross_merges(store, rows):
    result = commit(store, [source(store, 'rows.csv', rows)])
    assert result['added'] == len(rows)


@pytest.mark.parametrize('change', [{'title': 'Different title'}, {'price': 15000},
    {'author': 'Other author'}, {'publisher': 'Other'}, {'published_date': '2002'}])
def test_conflicting_isbn_information_is_retained_for_review(store, change):
    rows = [book(isbn='9780140328721'), book(isbn='9780140328721', **change)]
    result = commit(store, [source(store, 'rows.csv', rows)])
    assert result['added'] == 2 and result['merged'] == 0
    assert all(row['needs_review'] and row['warnings'] for row in records(store))


def test_import_reuses_existing_identity_without_overwriting_review_selection_price(store):
    lid = store.lists()[0]['id']
    original = store.add_book(lid, book(isbn='9780140328721', selected=False,
        needs_review=False, quantity=3))
    result = commit(store, [source(store, 'new.csv', [book(isbn='0140328726', needs_review=True)])])
    row = records(store)[0]
    assert (result['added'], result['merged']) == (0, 1)
    assert row['id'] == original['id'] and row['selected'] is False
    assert row['quantity'] == 3 and row['price'] == 12000 and row['needs_review'] is False


def test_import_is_idempotent_and_rejects_reusing_request_for_different_payload(store):
    uploads = [source(store, 'rows.csv', [book()])]
    result = commit(store, uploads)
    assert commit(store, uploads) == result
    assert len(records(store)) == 1
    with pytest.raises(ValueError):
        commit(store, uploads, deduplicate=False)
    assert records(store)[0]['recommendation_count'] == 1


def test_invalid_late_rows_missing_files_and_aggregate_limit_are_atomic(store):
    first = source(store, 'first.csv', [book()])
    second = source(store, 'second.csv', [book(price=-1)])
    for uploads in ([first, second], [first, {'import_id': 'missing', 'rows': [book()]}],
                    [source(store, 'many.csv', [book()] * 10001), source(store, 'more.csv', [book()] * 10000)]):
        with pytest.raises((ValueError, KeyError)):
            commit(store, uploads)
        assert records(store) == []


def test_disabled_dedup_retains_every_row_and_reports_duplicates(store):
    result = commit(store, [source(store, 'rows.csv', [book(), book()])], deduplicate=False)
    assert (result['added'], result['merged']) == (2, 0)
    assert all(row['duplicate'] for row in records(store))


def test_200_book_confirmation_acknowledges_isbnless_metadata_but_keeps_purchase_choices(store):
    lid = store.lists()[0]['id']
    books = store.add_books(lid, [book(title=f'Book {i}', needs_review=True,
        warnings=['ISBN 없음', '원본 확인'], selected=False) for i in range(200)])
    result = store.bulk_books(lid, {'book_ids': [b['id'] for b in books], 'action': 'confirm_metadata'})
    assert result['updated'] == 200 and result['skipped'] == []
    rows = records(store)
    assert all(not b['needs_review'] and not b['selected'] for b in rows)
    assert all(b['reviewed_warnings'] == ['ISBN 없음', '원본 확인'] and b['review_history'] for b in rows)
    assert store.list_state(lid)['summary']['review_count'] == 0
    selected = store.bulk_books(lid, {'book_ids': [b['id'] for b in books], 'action': 'select'})
    assert selected['updated'] == 200
    assert len(store.export_selection(lid)[0]) == 200


def test_confirm_skips_unresolved_rows_and_select_is_separate_from_review(store):
    lid = store.lists()[0]['id']
    books = store.add_books(lid, [book(price=None, needs_review=True),
        book(isbn='broken', needs_review=True), book(title='', needs_review=True),
        book(author='', needs_review=True), book(publisher='', needs_review=True)])
    result = store.bulk_books(lid, {'book_ids': [b['id'] for b in books], 'action': 'confirm_metadata'})
    assert result['updated'] == 0 and len(result['skipped']) == 5
    held = store.bulk_books(lid, {'book_ids': [b['id'] for b in books], 'action': 'hold'})
    assert held['updated'] == 5
    assert all(b['needs_review'] and not b['selected'] for b in records(store))


def test_bulk_rejects_mixed_foreign_and_unknown_ids_without_partial_changes(store):
    lid = store.lists()[0]['id']
    own = store.add_book(lid, book())
    foreign_list = store.create_list({'name': 'Other'})['id']
    foreign = store.add_book(foreign_list, book())
    for wrong in ('unknown', foreign['id']):
        with pytest.raises(KeyError):
            store.bulk_books(lid, {'book_ids': [own['id'], wrong], 'action': 'hold'})
        assert store.list_state(lid)['books'][0]['selected'] is True


def test_bulk_delete_hides_only_selected_books_and_undo_restores_the_exact_rows(store):
    lid = store.lists()[0]['id']
    first, second, third = store.add_books(lid, [
        book(title='First', quantity=2, note='Keep my note'),
        book(title='Second'),
        book(title='Third', selected=False),
    ])
    original = copy.deepcopy(records(store))

    result = store.bulk_books(lid, {'book_ids': [first['id'], third['id']], 'action': 'delete'})

    assert result['updated'] == 2 and result['skipped'] == []
    assert result['operation_id']
    state = store.list_state(lid)
    assert [row['id'] for row in state['books']] == [second['id']]
    assert state['summary']['selected_count'] == 1
    assert state['summary']['order_total'] == 12000
    saved = {row['id']: row for row in store.backup()['books']}
    assert saved[first['id']]['deleted'] is True
    assert saved[second['id']]['deleted'] is False
    assert saved[third['id']]['deleted'] is True
    assert saved[first['id']]['note'] == 'Keep my note'

    assert store.undo_operation(lid, result['operation_id']) == {'restored': 2}
    assert records(store) == original
    assert store.undo_operation(lid, result['operation_id']) == {'restored': 0}


def test_bulk_delete_rejects_wrong_or_already_deleted_ids_without_partial_changes(store):
    lid = store.lists()[0]['id']
    own, removed = store.add_books(lid, [book(title='Own'), book(title='Already deleted')])
    foreign_list = store.create_list({'name': 'Other'})['id']
    foreign = store.add_book(foreign_list, book(title='Foreign'))
    store.delete_book(lid, removed['id'])
    before = copy.deepcopy(store.backup()['books'])

    for wrong in ('unknown', foreign['id'], removed['id']):
        with pytest.raises(KeyError):
            store.bulk_books(lid, {'book_ids': [own['id'], wrong], 'action': 'delete'})
        assert store.backup()['books'] == before

    for invalid in ([], [own['id'], own['id']]):
        with pytest.raises(ValueError):
            store.bulk_books(lid, {'book_ids': invalid, 'action': 'delete'})
        assert store.backup()['books'] == before


def test_bulk_delete_undo_refuses_to_overwrite_a_later_restore_and_edit(store):
    lid = store.lists()[0]['id']
    first, second = store.add_books(lid, [book(title='First'), book(title='Second')])
    result = store.bulk_books(lid, {'book_ids': [first['id'], second['id']], 'action': 'delete'})
    store.restore_book(lid, first['id'])
    store.update_book(lid, first['id'], {'note': 'Later edit'})

    with pytest.raises(ValueError, match='변경'):
        store.undo_operation(lid, result['operation_id'])

    assert [row['id'] for row in records(store)] == [first['id']]
    assert records(store)[0]['note'] == 'Later edit'
    saved = {row['id']: row for row in store.backup()['books']}
    assert saved[second['id']]['deleted'] is True


def test_import_and_bulk_undo_restore_exact_values_and_refuse_newer_edits(store):
    lid = store.lists()[0]['id']
    original = store.add_book(lid, book())
    before = copy.deepcopy(records(store))
    imported = commit(store, [source(store, 'rows.csv', [book(), book(title='New book')])])
    assert store.undo_operation(lid, imported['operation_id']) == {'restored': 2}
    assert records(store) == before
    assert store.undo_operation(lid, imported['operation_id']) == {'restored': 0}
    with pytest.raises(ValueError):
        commit(store, [source(store, 'retry.csv', [book()])])
    changed = store.bulk_books(lid, {'book_ids': [original['id']], 'action': 'hold'})
    store.update_book(lid, original['id'], {'note': 'newer edit'})
    with pytest.raises(ValueError, match='변경'):
        store.undo_operation(lid, changed['operation_id'])
    row = records(store)[0]
    assert row['note'] == 'newer edit' and row['selected'] is False


def test_undo_rejects_edit_then_revert_and_preserves_all_other_rows(store):
    lid = store.lists()[0]['id']
    rows = store.add_books(lid, [book(), book(title='Second')])
    result = store.bulk_books(lid, {'book_ids': [b['id'] for b in rows], 'action': 'hold'})
    store.update_book(lid, rows[0]['id'], {'note': 'changed'})
    store.update_book(lid, rows[0]['id'], {'note': ''})
    with pytest.raises(ValueError):
        store.undo_operation(lid, result['operation_id'])
    assert all(not b['selected'] for b in records(store))


def test_backup_roundtrip_preserves_contributions_and_review_audit(store, tmp_path):
    lid = store.lists()[0]['id']
    commit(store, [source(store, 'first.csv', [book(needs_review=True, warnings=['ISBN 없음'])]),
                   source(store, 'second.csv', [book()])])
    store.bulk_books(lid, {'book_ids': [records(store)[0]['id']], 'action': 'confirm_metadata'})
    before = records(store)
    restored = LibraryStore(tmp_path / 'restored')
    restored.restore(store.backup())
    assert records(restored) == before


def test_confirmation_acknowledges_duplicate_and_holdings_warnings_without_selecting(store):
    lid = store.lists()[0]['id']
    store.add_holdings([book(isbn='9780140328721')])
    rows = store.add_books(lid, [book(isbn='9780140328721', selected=False),
                                book(isbn='9780140328721', selected=False)])
    result = store.bulk_books(lid, {'book_ids': [b['id'] for b in rows], 'action': 'confirm_metadata'})
    assert result['updated'] == 2
    state = store.list_state(lid)
    assert state['summary']['review_count'] == 0
    assert all(not b['selected'] and b['held'] and b['duplicate'] for b in state['books'])
    assert all(len(b['reviewed_warnings']) == 2 for b in state['books'])


def test_undo_from_reopened_store_and_rollback_after_injected_write_failure(store, monkeypatch):
    import suseoro.simple.batch as batch
    lid = store.lists()[0]['id']
    uploads = [source(store, 'rows.csv', [book(), book(title='Second')])]
    real_encoded = batch.encoded
    def fail_second_write(value):
        if isinstance(value, dict) and value.get('title') == 'Second' and value.get('id'):
            raise RuntimeError('synthetic write failure')
        return real_encoded(value)
    with monkeypatch.context() as context:
        context.setattr(batch, 'encoded', fail_second_write)
        with pytest.raises(RuntimeError):
            commit(store, uploads)
    assert records(store) == []
    result = commit(store, uploads)
    reopened = LibraryStore(store.data_dir)
    assert reopened.undo_operation(lid, result['operation_id']) == {'restored': 2}
    assert records(reopened) == []


def test_maximum_aggregate_merges_twenty_thousand_contributions_without_losing_sources(store):
    uploads = [source(store, 'large.csv', [book(source=f'추천기관 {i}', raw_values={'row': i})
                                         for i in range(20000)])]
    result = commit(store, uploads)
    assert (result['added'], result['merged'], result['input_count']) == (1, 19999, 20000)
    row = records(store)[0]
    assert row['recommendation_count'] == len(row['sources']) == len(row['contributions']) == 20000


@pytest.mark.parametrize('key,value', [('contributions', 'invalid'), ('sources', [None]),
                                      ('recommendation_count', -1), ('review_history', [False])])
def test_invalid_restored_metadata_is_atomic(store, key, value):
    store.add_book(store.lists()[0]['id'], book())
    before = records(store)
    backup = store.backup()
    backup['books'][0][key] = value
    with pytest.raises(ValueError):
        store.restore(backup)
    assert records(store) == before


def test_reupload_of_same_file_keeps_recommendation_count_and_edited_rows_are_reviewed(store):
    first = source(store, 'original.csv', [book(isbn='9780140328721')])
    commit(store, [first])
    original_id = records(store)[0]['id']
    again = source(store, 'renamed.csv', [book(isbn='9780140328721')])
    result = commit(store, [again], request_id='reupload')
    row = records(store)[0]
    assert (result['added'], result['merged']) == (0, 1)
    assert row['id'] == original_id and row['recommendation_count'] == 1
    assert len(row['contributions']) == 1
    edited = copy.deepcopy(again)
    edited['rows'][0]['price'] = 15000
    result = commit(store, [edited], request_id='edited-row')
    assert result['added'] == 1 and all(row['needs_review'] for row in records(store))


def test_two_distinct_source_files_with_same_rows_count_separately(store):
    first = source(store, 'first.csv', [book()])
    second = source(store, 'second.csv', [book()])
    (store.data_dir / 'sources' / 'second.csv').write_text('another synthetic source', encoding='utf-8')
    commit(store, [first, second])
    assert records(store)[0]['recommendation_count'] == 2


def test_concurrent_normal_edit_cannot_overwrite_batch_provenance(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    import suseoro.simple.store as store_module
    lid = store.lists()[0]['id']
    original = store.add_book(lid, book())
    upload = source(store, 'source.csv', [book()])
    read, release = Event(), Event()
    real_validate = store_module.validate_book
    def paused_validation(values, previous=None):
        if values.get('note') == 'concurrent edit' and previous:
            read.set()
            assert release.wait(5)
        return real_validate(values, previous)
    monkeypatch.setattr(store_module, 'validate_book', paused_validation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        editing = pool.submit(store.update_book, lid, original['id'], {'note': 'concurrent edit'})
        assert read.wait(5)
        importing = pool.submit(commit, store, [upload])
        try:
            importing.result(timeout=0.2)
        except TimeoutError:
            pass
        finally:
            release.set()
        editing.result(timeout=5)
        importing.result(timeout=5)
    row = records(store)[0]
    assert row['note'] == 'concurrent edit' and row['recommendation_count'] == 2


def test_partial_bibliography_enrichment_never_merges_different_valid_isbns(store):
    rows = [book(isbn='9780140328721', publisher=''),
            book(isbn='9780140328721', author=''),
            book(isbn='9788936434267')]
    result = commit(store, [source(store, 'partial.csv', rows)])
    assert (result['added'], result['merged']) == (2, 1)
    assert {row['isbn'] for row in records(store)} == {'9780140328721', '9788936434267'}


def test_late_enrichment_ambiguity_cannot_attach_earlier_isbnless_row(store):
    rows = [book(), book(isbn='9788936434267'),
            book(isbn='9780140328721', publisher=''),
            book(isbn='9780140328721', author='')]
    result = commit(store, [source(store, 'partial.csv', rows)])
    assert (result['added'], result['merged']) == (3, 1)
    assert {row['isbn'] for row in records(store)} == {'', '9780140328721', '9788936434267'}


def test_new_merged_rows_keep_incoming_ocr_review_concerns(store):
    rows = [book(), book(needs_review=True, warnings=['OCR 원본 확인'])]
    commit(store, [source(store, 'ocr.csv', rows)])
    row = records(store)[0]
    assert row['needs_review'] is True and 'OCR 원본 확인' in row['warnings']


@pytest.mark.parametrize('damage', [
    {'source': {'unexpected': 'object'}}, {'filename': ['not a filename']},
    {'raw_text': {'not': 'text'}}, {'import_id': 123}, {'row_number': '1'},
    {'values': {'title': {'not': 'text'}}}, {'values': []},
    {'provenance': []}, {'raw_values': 'not a mapping'},
    {'origin_fingerprint': ['not a checksum']},
])
def test_malformed_contributions_in_backup_cannot_break_the_library_screen(store, damage):
    commit(store, [source(store, 'first.csv', [book()])])
    before = records(store)
    backup = store.backup()
    backup['books'][0]['contributions'][0].update(damage)
    with pytest.raises(ValueError):
        store.restore(backup)
    assert records(store) == before


def test_reupload_uses_original_sheet_row_when_preview_rows_are_reordered(store):
    first = book(title='First', provenance={'sheet': '도서', 'row': 2, 'filename': 'original.csv'})
    second = book(title='Second', provenance={'sheet': '도서', 'row': 3, 'filename': 'original.csv'})
    commit(store, [source(store, 'original.csv', [first, second])])
    commit(store, [source(store, 'renamed.csv', [second, first])], request_id='reordered')
    assert all(row['recommendation_count'] == 1 for row in records(store))


def test_batch_endpoints_and_foreign_operation_boundary(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:3847') as client:
        bootstrap = client.get('/api/library/bootstrap').json()
        client.headers['X-Suseoro-Token'] = bootstrap['csrf_token']
        lid = bootstrap['lists'][0]['id']
        base = f'/api/library/lists/{lid}'
        upload = source(app.state.store, 'input.csv', [book()])
        response = client.post(base + '/imports/batch', json={'imports': [upload], 'request_id': 'api-1'})
        assert response.status_code == 200, response.text
        row = client.get(base).json()['books'][0]
        changed = client.post(base + '/books/bulk', json={'book_ids': [row['id']], 'action': 'hold'})
        assert changed.status_code == 200, changed.text
        operation_id = changed.json()['operation_id']
        foreign = client.post('/api/library/lists', json={'name': 'Other'}).json()['id']
        assert client.post(f'/api/library/lists/{foreign}/operations/{operation_id}/undo').status_code == 404
        assert client.post(base + f'/operations/{operation_id}/undo').json() == {'restored': 1}
        deleted = client.post(base + '/books/bulk', json={'book_ids': [row['id']], 'action': 'delete'})
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()['updated'] == 1 and deleted.json()['skipped'] == []
        assert client.get(base).json()['books'] == []
        assert client.post(base + f"/operations/{deleted.json()['operation_id']}/undo").json() == {'restored': 1}
        assert [book['id'] for book in client.get(base).json()['books']] == [row['id']]
