"""Loopback-only API and bundled screen for the personal desktop application."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from suseoro.simple import VERSION
from suseoro.simple.bibliography import lookup_isbn
from suseoro.simple.covers import CoverService
from suseoro.simple.help_pages import default_help_dir, help_policy, manual_response
from suseoro.simple.store import BACKUP_MAX_JSON_BYTES, LibraryStore, identifier
from suseoro.simple.update_coordinator import UpdateCoordinator

PREFIX = '/api/library'
UPLOAD_LIMIT = 50 * 1024 * 1024
ALLOWED_FORMATS = {'.xlsx', '.xls', '.xlsb', '.ods', '.csv', '.tsv', '.txt', '.pdf', '.hwp', '.hwpx', '.docx'}


def default_data_dir():
    return Path(os.environ.get('SUSEORO_DATA_DIR') or (Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local' / 'share') / 'Suseoro'))


def download(data: bytes, filename: str, mime: str):
    return Response(data, media_type=mime, headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename),
        'Cache-Control': 'no-store',
    })


def create_app(data_dir: Path | None = None, frontend_dir: Path | None = None,
               help_dir: Path | None = None) -> FastAPI:
    store = LibraryStore(data_dir or default_data_dir())
    manuals = Path(help_dir) if help_dir is not None else default_help_dir()
    app = FastAPI(title='수서로 2.0', version=VERSION, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.update_coordinator = UpdateCoordinator(store.data_dir)
    app.state.shutdown_callback = None
    app.state.install_update_callback = None
    app.state.lookup_cache = {}
    app.state.covers = CoverService()

    @app.exception_handler(ValueError)
    async def value_error(_request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def not_found(_request, exc):
        return JSONResponse({'detail': str(exc.args[0])}, status_code=404)

    @app.exception_handler(sqlite3.Error)
    async def database_error(_request, _exc):
        return JSONResponse({'detail': '자료를 저장하지 못했습니다. 다른 작업이 끝난 뒤 다시 시도해 주세요.'}, status_code=503)

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        try:
            host = urlsplit('http://' + request.headers.get('host', '')).hostname
        except ValueError:
            host = None
        if host not in ('127.0.0.1', 'localhost', '::1'):
            return JSONResponse({'detail': '이 PC에서만 사용할 수 있습니다.'}, status_code=403)
        origin = request.headers.get('origin')
        own_origin = str(request.base_url).rstrip('/')
        dev_origin = os.environ.get('SUSEORO_DEV_ORIGIN')
        if origin and origin != own_origin and origin != dev_origin:
            return JSONResponse({'detail': '외부 사이트의 요청은 허용하지 않습니다.'}, status_code=403)
        length = request.headers.get('content-length')
        request_limit = BACKUP_MAX_JSON_BYTES if request.url.path == PREFIX + '/restore' else UPLOAD_LIMIT
        if length and (not length.isdigit() or int(length) > request_limit + 1024 * 1024):
            return JSONResponse({'detail': f'파일은 한 번에 {request_limit // (1024 * 1024)} MB까지 가져올 수 있습니다.'}, status_code=413)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.headers.get('x-suseoro-token', '')
            if not secrets.compare_digest(token, app.state.csrf_token):
                return JSONResponse({'detail': '화면을 새로 열고 다시 시도해 주세요.'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        help_nonce = getattr(request.state, 'help_nonce', None)
        response.headers['X-Frame-Options'] = 'SAMEORIGIN' if help_nonce else 'DENY'
        response.headers['Content-Security-Policy'] = help_policy(help_nonce) if help_nonce else "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; object-src 'none'; frame-src 'self'; frame-ancestors 'none'"
        if request.url.path.startswith(PREFIX) and not request.url.path.startswith(PREFIX + '/covers/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/help')
    def help_redirect():
        return RedirectResponse('/help/')

    @app.get('/help/')
    def help_index(request: Request):
        return manual_response(request, manuals, 'index.html')

    @app.get('/help/{filename:path}')
    def help_page(filename: str, request: Request):
        return manual_response(request, manuals, filename)

    @app.get(PREFIX + '/health')
    def health():
        return {'version': VERSION, 'status': 'ready'}

    @app.get(PREFIX + '/covers/{isbn}')
    def cover_image(isbn: str):
        image = app.state.covers.get(isbn)
        if image is None:
            return Response(status_code=404, headers={'Cache-Control': 'no-store'})
        return Response(image.data, media_type='image/jpeg', headers={
            'Cache-Control': 'private, max-age=86400', 'X-Cover-Source': image.source,
        })

    @app.get(PREFIX + '/bootstrap')
    def bootstrap():
        return {'version': VERSION, 'csrf_token': app.state.csrf_token, 'settings': store.settings(),
                'lists': store.lists(), 'update': app.state.update_coordinator.status()}

    @app.patch(PREFIX + '/settings')
    def settings(values: dict):
        result = store.update_settings(values)
        app.state.lookup_cache.clear()
        return result

    @app.post(PREFIX + '/lists')
    def new_list(values: dict):
        return store.create_list(values)

    @app.get(PREFIX + '/lists/{list_id}')
    def list_state(list_id: str):
        return store.list_state(list_id)

    @app.patch(PREFIX + '/lists/{list_id}')
    def edit_list(list_id: str, values: dict):
        return store.update_list(list_id, values)

    @app.post(PREFIX + '/lists/{list_id}/books')
    def add_book(list_id: str, values: dict):
        return store.add_book(list_id, values)

    @app.post(PREFIX + '/lists/{list_id}/books/bulk')
    def bulk_books(list_id: str, values: dict):
        return store.bulk_books(list_id, values)

    @app.post(PREFIX + '/lists/{list_id}/operations/{operation_id}/undo')
    def undo_operation(list_id: str, operation_id: str):
        return store.undo_operation(list_id, operation_id)

    @app.patch(PREFIX + '/lists/{list_id}/books/{book_id}')
    def edit_book(list_id: str, book_id: str, values: dict):
        return store.update_book(list_id, book_id, values)

    @app.delete(PREFIX + '/lists/{list_id}/books/{book_id}')
    def remove_book(list_id: str, book_id: str):
        store.delete_book(list_id, book_id)
        return {'ok': True}

    @app.post(PREFIX + '/lists/{list_id}/books/{book_id}/restore')
    def undo_remove(list_id: str, book_id: str):
        return store.restore_book(list_id, book_id)

    @app.post(PREFIX + '/lookup')
    def lookup(values: dict):
        isbn = values.get('isbn')
        if not isinstance(isbn, str) or len(isbn) > 80:
            raise ValueError('ISBN을 입력해 주세요.')
        cached = app.state.lookup_cache.get(isbn)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        result = lookup_isbn(isbn, nl_api_key=store.secret('nl_api_key'))
        if result.get('found'):
            if len(app.state.lookup_cache) > 2000:
                app.state.lookup_cache.clear()
            app.state.lookup_cache[isbn] = (time.monotonic(), result)
        return result

    async def save_upload(file: UploadFile, folder: str, allowed: set[str]):
        filename = (file.filename or '자료').replace('\\', '/').split('/')[-1]
        filename = re.sub(r'[\x00-\x1f<>:"|?*]', '_', filename)[:180]
        suffix = Path(filename).suffix.lower()
        if suffix not in allowed:
            raise ValueError('지원하지 않는 파일 형식입니다. 엑셀·PDF·한글·문서·텍스트 파일을 선택해 주세요.')
        file_id = identifier()
        path = store.data_dir / folder / (file_id + suffix)
        size = 0
        try:
            with path.open('xb') as target:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > UPLOAD_LIMIT:
                        raise ValueError('50 MB 이하의 파일로 나누어 가져와 주세요.')
                    target.write(chunk)
            if not size:
                raise ValueError('파일이 비어 있습니다.')
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        return file_id, filename, path

    @app.post(PREFIX + '/imports/preview')
    async def preview_import(file: UploadFile = File(...)):
        from starlette.concurrency import run_in_threadpool
        from suseoro.simple.documents import parse_upload
        file_id, filename, path = await save_upload(file, 'sources', ALLOWED_FORMATS)
        result = await run_in_threadpool(parse_upload, path, filename)
        store.remember_file('imports', file_id, filename, path)
        return {**result, 'import_id': file_id, 'filename': filename}

    @app.post(PREFIX + '/imports/{import_id}/preview')
    def preview_mapping(import_id: str, values: dict):
        from suseoro.simple.documents import parse_upload
        item = store.stored_file('imports', import_id)
        mapping = values.get('mapping', {})
        if not isinstance(mapping, dict) or len(mapping) > 30 or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
            raise ValueError('열 연결을 다시 확인해 주세요.')
        result = parse_upload(Path(item['path']), item['filename'], mapping=mapping)
        return {**result, 'import_id': import_id, 'filename': item['filename']}

    @app.post(PREFIX + '/lists/{list_id}/imports')
    def commit_import(list_id: str, values: dict):
        item = store.stored_file('imports', values.get('import_id', ''))
        rows = values.get('rows')
        kind = values.get('kind', 'recommendations')
        if not isinstance(rows, list) or not rows or kind not in ('recommendations', 'holdings'):
            raise ValueError('가져올 도서를 선택하고 자료 종류를 확인해 주세요.')
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('도서 행 형식이 올바르지 않습니다.')
            row.setdefault('source', item['filename'])
        store.get_list(list_id)
        if kind == 'holdings':
            added = store.replace_holdings(rows)
        else:
            added = len(store.add_books(list_id, rows))
        return {'added': added, 'warnings': []}

    @app.post(PREFIX + '/lists/{list_id}/imports/batch')
    def commit_batch_import(list_id: str, values: dict):
        return store.import_batch(list_id, values)

    @app.get(PREFIX + '/templates')
    def templates():
        from suseoro.simple.exporting import inspect_template
        with store.connection() as db:
            items = [dict(r) for r in db.execute('SELECT * FROM templates ORDER BY rowid DESC')]
        result = []
        for item in items:
            try:
                stored = store.stored_file('templates', item['id'])
                info = inspect_template(Path(stored['path']))
                result.append({**info, 'id': item['id'], 'name': item['name']})
            except (OSError, ValueError):
                continue
        return result

    @app.post(PREFIX + '/templates')
    async def add_template(file: UploadFile = File(...)):
        from starlette.concurrency import run_in_threadpool
        from suseoro.simple.exporting import inspect_template
        file_id, filename, path = await save_upload(file, 'templates', {'.xlsx'})
        result = await run_in_threadpool(inspect_template, path)
        store.remember_file('templates', file_id, filename, path)
        return {**result, 'id': file_id, 'name': filename}

    @app.post(PREFIX + '/lists/{list_id}/export')
    def export(list_id: str, values: dict):
        from suseoro.simple.exporting import export_books
        books, list_info = store.export_selection(list_id)
        format = values.get('format', 'xlsx')
        if format not in ('xlsx', 'csv', 'html'):
            raise ValueError('저장할 파일 형식을 다시 선택해 주세요.')
        template_path = None
        if values.get('template_id'):
            item = store.stored_file('templates', values['template_id'])
            template_path = Path(item['path'])
        data, filename, mime = export_books(books, list_info, store.settings()['school_name'], format=format, template_path=template_path)
        return download(data, filename, mime)

    @app.get(PREFIX + '/backup')
    def backup():
        payload = json.dumps(store.backup(), ensure_ascii=False, indent=2).encode('utf-8')
        return download(payload, '수서로-자료백업.json', 'application/json')

    @app.post(PREFIX + '/restore')
    async def restore(file: UploadFile = File(...)):
        from starlette.concurrency import run_in_threadpool
        try:
            data = await file.read(BACKUP_MAX_JSON_BYTES + 1)
            if len(data) > BACKUP_MAX_JSON_BYTES:
                raise ValueError('백업 파일이 너무 큽니다.')
            try:
                content = json.loads(data.decode('utf-8-sig'))
            except (ValueError, UnicodeError) as exc:
                raise ValueError('올바른 수서로 백업 파일을 선택해 주세요.') from exc
            await run_in_threadpool(store.restore, content)
            return {'ok': True}
        finally:
            await file.close()

    @app.get(PREFIX + '/updates')
    def updates():
        return app.state.update_coordinator.request_check()

    @app.get(PREFIX + '/updates/status')
    def update_status():
        return app.state.update_coordinator.status()

    @app.patch(PREFIX + '/updates/preferences')
    def update_preferences(values: dict):
        if set(values) != {'auto_enabled'}:
            raise ValueError('자동 업데이트 사용 여부를 확인해 주세요.')
        return app.state.update_coordinator.set_preferences(values['auto_enabled'])

    @app.post(PREFIX + '/updates/install')
    def install_update():
        if not app.state.install_update_callback:
            raise ValueError('설치형 수서로에서 업데이트하거나 GitHub에서 새 설치 파일을 받아 주세요.')
        app.state.update_coordinator.install_manual(app.state.install_update_callback)
        return {'started': True}

    @app.post(PREFIX + '/shutdown')
    def shutdown():
        if app.state.shutdown_callback:
            app.state.shutdown_callback()
        return {'ok': True}

    if frontend_dir and Path(frontend_dir).is_dir():
        frontend_dir = Path(frontend_dir).resolve()
        assets = frontend_dir / 'assets'
        if assets.is_dir():
            app.mount('/assets', StaticFiles(directory=assets), name='assets')

        @app.get('/')
        def index():
            return FileResponse(frontend_dir / 'index.html')
    else:
        @app.get('/')
        def index():
            return Response('수서로 화면 파일을 찾을 수 없습니다. 개발 환경에서는 frontend 빌드 후 실행해 주세요.', media_type='text/plain')

    return app
