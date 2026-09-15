import hashlib
import json
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from suseoro.simple import VERSION, updating
from suseoro.simple.app import create_app
from suseoro.simple.update_coordinator import UpdateCoordinator


def info(version='2.1.0'):
    return {'current_version': VERSION, 'latest_version': version,
            'available': True, 'url': updating.RELEASES_URL, 'message': '새 버전',
            'asset': {'name': f'Suseoro-Setup-{version}.exe'}}


def verified_file(data_dir, version='2.1.0'):
    directory = data_dir / 'updates'
    directory.mkdir(exist_ok=True)
    path = directory / f'Suseoro-Setup-{version}.exe'
    path.write_bytes(b'MZverified-installer')
    updating._write_verified(path, hashlib.sha256(path.read_bytes()).hexdigest())
    return path


def finish(coordinator):
    with coordinator._lock:
        worker = coordinator._worker
    if worker:
        worker.join(timeout=3)
        assert not worker.is_alive()


@pytest.fixture
def available(monkeypatch, tmp_path):
    path = verified_file(tmp_path)
    calls = []
    monkeypatch.setattr(updating, 'check_update', lambda *args: calls.append('check') or info())
    monkeypatch.setattr(updating, 'download_update', lambda *args, **kwargs: calls.append('download') or path)
    return path, calls


def test_only_a_finished_update_is_handed_off_on_normal_exit(tmp_path, available):
    path, calls = available
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    assert calls == []
    assert coordinator.status()['auto_enabled'] is True
    coordinator.start()
    finish(coordinator)
    assert calls == ['check', 'download']
    assert coordinator.status()['phase'] == 'ready'
    assert coordinator.close(normal_exit=True) == path
    assert coordinator.close(normal_exit=True) is None


def test_abnormal_exit_never_installs_a_ready_update(tmp_path, available):
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    coordinator.start()
    finish(coordinator)
    assert coordinator.close(normal_exit=False) is None


def test_disabling_auto_after_download_prevents_exit_install_and_persists(tmp_path, available):
    _, calls = available
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    coordinator.start()
    finish(coordinator)
    status = coordinator.set_preferences(False)
    assert status['auto_enabled'] is False
    assert status['phase'] == 'manual'
    assert coordinator.close(normal_exit=True) is None
    restored = UpdateCoordinator(tmp_path, auto_supported=True)
    restored.start()
    assert restored.status()['auto_enabled'] is False
    assert calls == ['check', 'download']
    assert json.loads((tmp_path / 'update-preferences.json').read_text()) == {'auto_enabled': False}


@pytest.mark.parametrize('saved', ['broken json', '[]', '{"auto_enabled": "false"}'])
def test_bad_preferences_use_the_enabled_default_without_network(tmp_path, available, saved):
    _, calls = available
    (tmp_path / 'update-preferences.json').write_text(saved)
    assert UpdateCoordinator(tmp_path).status()['auto_enabled'] is True
    assert calls == []


def test_portable_only_checks_when_requested_and_never_downloads_automatically(tmp_path, available):
    _, calls = available
    coordinator = UpdateCoordinator(tmp_path)
    coordinator.start()
    assert calls == []
    assert coordinator.status()['auto_supported'] is False
    assert '기본 경로에 설치한' in coordinator.status()['message']
    coordinator.request_check()
    finish(coordinator)
    assert calls == ['check']
    assert coordinator.status()['phase'] == 'manual'
    assert coordinator.close(normal_exit=True) is None


def test_older_installer_without_exit_protocol_is_manual_only(monkeypatch, tmp_path, available):
    _, calls = available
    monkeypatch.setattr(updating, 'check_update', lambda *args: info('2.0.1'))
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    coordinator.start()
    finish(coordinator)
    assert calls == []
    assert coordinator.status()['phase'] == 'manual'
    assert coordinator.close(normal_exit=True) is None


def test_network_failure_is_retryable_without_cached_false_latest(monkeypatch, tmp_path, available):
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    monkeypatch.setattr(updating, 'check_update', lambda *args: {**info(), 'available': False, 'error': True})
    coordinator.start()
    finish(coordinator)
    assert coordinator.status()['phase'] == 'error'
    monkeypatch.setattr(updating, 'check_update', lambda *args: info())
    coordinator.request_check()
    finish(coordinator)
    assert coordinator.status()['phase'] == 'ready'


def test_download_failure_and_tampering_do_not_produce_an_exit_installer(monkeypatch, tmp_path, available):
    path, _ = available
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    coordinator.start()
    finish(coordinator)
    path.write_bytes(b'MZtampered')
    assert coordinator.close(normal_exit=True) is None
    assert coordinator.status()['phase'] == 'error'
    def fail(*args, **kwargs):
        raise updating.UpdateError('검증 실패')
    monkeypatch.setattr(updating, 'download_update', fail)
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    coordinator.start()
    finish(coordinator)
    assert coordinator.status()['phase'] == 'error'
    assert coordinator.close(normal_exit=True) is None


@pytest.mark.parametrize('action', ['disable', 'close'])
def test_unfinished_download_is_cancelled_without_becoming_ready(monkeypatch, tmp_path, action):
    entered = threading.Event()
    cancelled = threading.Event()
    monkeypatch.setattr(updating, 'check_update', lambda *args: info())
    def download(*args, cancel):
        entered.set()
        assert cancel.wait(3)
        cancelled.set()
        raise updating.UpdateCancelled('중지')
    monkeypatch.setattr(updating, 'download_update', download)
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    coordinator.start()
    assert entered.wait(3)
    if action == 'disable':
        coordinator.set_preferences(False)
        finish(coordinator)
    assert coordinator.close(normal_exit=True) is None
    assert cancelled.is_set()
    assert coordinator.status()['phase'] != 'ready'


def test_manual_install_cancels_and_serializes_with_background_download(monkeypatch, tmp_path, available):
    path, _ = available
    entered = threading.Event()
    count = 0
    active = 0
    callbacks = []
    def download(*args, cancel):
        nonlocal count, active
        count += 1
        active += 1
        assert active == 1
        try:
            if count == 1:
                entered.set()
                assert cancel.wait(3)
                raise updating.UpdateCancelled('수동 설치로 전환')
            return path
        finally:
            active -= 1
    monkeypatch.setattr(updating, 'download_update', download)
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    coordinator.start()
    assert entered.wait(3)
    assert callbacks == []
    coordinator.install_manual(callbacks.append)
    finish(coordinator)
    assert callbacks == [path]
    assert count == 2
    with pytest.raises(ValueError):
        coordinator.install_manual(callbacks.append)
    assert coordinator.close(normal_exit=True) is None


def test_reenabling_during_cancellation_queues_one_fresh_prepare(monkeypatch, tmp_path, available):
    path, _ = available
    entered = threading.Event()
    release = threading.Event()
    downloads = 0
    def download(*args, cancel):
        nonlocal downloads
        downloads += 1
        if downloads == 1:
            entered.set()
            assert release.wait(3)
            updating._check_cancelled(cancel)
        return path
    monkeypatch.setattr(updating, 'download_update', download)
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    coordinator.start()
    assert entered.wait(3)
    coordinator.set_preferences(False)
    coordinator.set_preferences(True)
    release.set()
    finish(coordinator)
    assert downloads == 2
    assert coordinator.status()['phase'] == 'ready'
    assert coordinator.close(normal_exit=True) == path


def test_exiting_while_manual_preparation_runs_cancels_it_without_calling_installer(monkeypatch, tmp_path, available):
    entered = threading.Event()
    calls, errors = [], []
    def download(*args, cancel):
        entered.set()
        assert cancel.wait(3)
        raise updating.UpdateCancelled('종료')
    monkeypatch.setattr(updating, 'download_update', download)
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    def install():
        try:
            coordinator.install_manual(calls.append)
        except updating.UpdateCancelled as error:
            errors.append(error)
    worker = threading.Thread(target=install)
    worker.start()
    assert entered.wait(3)
    assert coordinator.close(normal_exit=True) is None
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert calls == [] and len(errors) == 1


def test_repeated_check_requests_do_not_spawn_parallel_downloads(monkeypatch, tmp_path, available):
    path, _ = available
    entered = threading.Event()
    release = threading.Event()
    active = 0
    downloads = 0
    def download(*args, cancel):
        nonlocal active, downloads
        active += 1
        downloads += 1
        assert active == 1
        try:
            entered.set()
            assert release.wait(3)
            return path
        finally:
            active -= 1
    monkeypatch.setattr(updating, 'download_update', download)
    coordinator = UpdateCoordinator(tmp_path, auto_supported=True)
    coordinator.start()
    assert entered.wait(3)
    for _ in range(10):
        coordinator.request_check()
    release.set()
    finish(coordinator)
    assert downloads == 2
    assert coordinator.close(normal_exit=True) == path


def test_api_status_does_not_check_network_and_preferences_require_a_bool_and_token(tmp_path, available):
    _, calls = available
    app = create_app(tmp_path / 'data')
    with TestClient(app, base_url='http://127.0.0.1:3847') as client:
        bootstrap = client.get('/api/library/bootstrap').json()
        assert bootstrap['update']['auto_enabled'] is True
        for _ in range(3):
            status = client.get('/api/library/updates/status')
            assert status.json()['phase'] == 'manual'
            assert 'asset' not in status.json()
        assert calls == []
        route = '/api/library/updates/preferences'
        assert client.patch(route, json={'auto_enabled': False}).status_code == 403
        client.headers['X-Suseoro-Token'] = bootstrap['csrf_token']
        assert client.patch(route, json={'auto_enabled': 'false'}).status_code == 400
        assert client.patch(route, json={'auto_enabled': False, 'unknown': True}).status_code == 400
        assert client.patch(route, json={'auto_enabled': False}).json()['auto_enabled'] is False
        assert client.get('/api/library/updates').status_code == 200
        finish(app.state.update_coordinator)
        assert calls == ['check']
