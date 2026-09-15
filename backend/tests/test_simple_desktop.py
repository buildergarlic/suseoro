import json
from pathlib import Path
from urllib.request import urlopen

import pytest


def desktop():
    from suseoro.simple import desktop
    return desktop


def test_data_directory_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert desktop().default_data_dir() == tmp_path / "Suseoro"


def test_frozen_static_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop().sys, "_MEIPASS", str(tmp_path), raising=False)
    assert desktop().frontend_dir() == tmp_path / "ui"


def test_development_static_directory():
    assert desktop().frontend_dir().name == "dist"
    assert desktop().frontend_dir().parent.name == "frontend"


def test_duplicate_instance_cannot_acquire_same_directory(tmp_path):
    first = desktop().InstanceLock(tmp_path)
    second = desktop().InstanceLock(tmp_path)
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_stale_instance_file_does_not_block_start(tmp_path):
    (tmp_path / "desktop-instance.json").write_text('{"url":"https://evil.example"}')
    instance = desktop().InstanceLock(tmp_path)
    assert instance.acquire()
    instance.release()


def test_server_uses_ephemeral_loopback_and_stops():
    from fastapi import FastAPI
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    server = desktop().LocalServer(app)
    server.start()
    try:
        assert server.url.startswith("http://127.0.0.1:")
        with urlopen(server.url + "/health", timeout=2) as response:
            assert json.load(response)["status"] == "ok"
    finally:
        server.stop()
    assert not server.thread.is_alive()


def test_version_does_not_open_app(monkeypatch, capsys):
    assert desktop().main(["--version"]) == 0
    assert desktop().VERSION in capsys.readouterr().out


def test_port_validation():
    with pytest.raises(SystemExit):
        desktop().main(["--headless", "--port", "70000"])


def test_unverified_installer_is_not_launched(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(desktop().subprocess, "Popen", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(Exception):
        desktop().launch_installer(tmp_path / "malicious.exe", tmp_path)
    assert not calls


def test_native_window_enables_download_and_stops_server_on_return(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from suseoro.simple import app as app_module

    calls = []
    fake_app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(app_module, "create_app", lambda **kwargs: fake_app)

    class Event:
        def __iadd__(self, callback):
            self.callback = callback
            return self

    class Server:
        url = "http://127.0.0.1:12345"
        def __init__(self, *args):
            pass
        def start(self):
            calls.append("start")
        def stop(self):
            calls.append("stop")

    window = SimpleNamespace(events=SimpleNamespace(closed=Event()), destroy=lambda: None)
    view = SimpleNamespace(settings={}, create_window=lambda *args, **kwargs: window,
                           start=lambda **kwargs: calls.append("window"))
    monkeypatch.setitem(desktop().sys.modules, "webview", view)
    monkeypatch.setattr(desktop(), "LocalServer", Server)
    monkeypatch.setattr(desktop(), "_daily_update_check", lambda *args: calls.append("unexpected-update-check"))
    monkeypatch.setattr(desktop(), "_installer_mutex", lambda: None)
    assert desktop().main(["--data-dir", str(tmp_path)]) == 0
    assert calls == ["start", "window", "stop"]
    assert view.settings["ALLOW_DOWNLOADS"] is True
    assert view.settings["ALLOW_FILE_URLS"] is False
    assert callable(fake_app.state.shutdown_callback)
    assert callable(fake_app.state.install_update_callback)


def test_bundled_ocr_is_configured_without_running_a_helper(monkeypatch, tmp_path):
    bundle = tmp_path / "portable_tesseract"
    bundle.mkdir()
    (bundle / "tesseract.exe").write_bytes(b"fixture")
    monkeypatch.setattr(desktop().sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("PATH", "original")
    monkeypatch.setenv("SUSEORO_TESSERACT", "")
    monkeypatch.setenv("TESSDATA_PREFIX", "")
    desktop()._configure_bundled_ocr()
    assert desktop().os.environ["SUSEORO_TESSERACT"] == str(bundle / "tesseract.exe")
    assert desktop().os.environ["TESSDATA_PREFIX"] == str(bundle / "tessdata")


def test_frozen_smoke_test_refuses_missing_frontend(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop().sys, "_MEIPASS", str(tmp_path), raising=False)
    assert desktop().main(["--smoke-test", "--data-dir", str(tmp_path / "data")]) == 1


def test_auto_update_only_supports_the_windows_frozen_default_installation(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(desktop().sys, 'platform', 'win32')
    monkeypatch.setattr(desktop().sys, 'frozen', True, raising=False)
    executable = tmp_path / 'Programs' / 'Suseoro' / 'Suseoro.exe'
    monkeypatch.setattr(desktop().sys, 'executable', str(executable))
    assert desktop().automatic_update_supported()
    monkeypatch.setattr(desktop().sys, 'executable', str(tmp_path / 'Portable' / 'Suseoro.exe'))
    assert not desktop().automatic_update_supported()
    monkeypatch.setattr(desktop().sys, 'executable', str(executable))
    monkeypatch.setattr(desktop().sys, 'frozen', False)
    assert not desktop().automatic_update_supported()
    monkeypatch.setattr(desktop().sys, 'frozen', True)
    monkeypatch.setattr(desktop().sys, 'platform', 'linux')
    assert not desktop().automatic_update_supported()


def test_automatic_handoff_passes_silent_and_parent_pid_options_but_manual_uses_wizard(monkeypatch, tmp_path):
    from suseoro.simple import updating
    import hashlib
    directory = tmp_path / 'updates'
    directory.mkdir()
    path = directory / 'Suseoro-Setup-2.1.0.exe'
    path.write_bytes(b'MZinstaller')
    updating._write_verified(path, hashlib.sha256(path.read_bytes()).hexdigest())
    calls = []
    monkeypatch.setattr(desktop(), 'automatic_update_supported', lambda: True)
    monkeypatch.setattr(desktop().subprocess, 'Popen', lambda *args, **kwargs: calls.append((args, kwargs)))
    desktop().launch_installer(path, tmp_path, automatic=True)
    args = calls[0][0][0]
    assert args == [str(path.resolve()), '/S', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                    '/AUTOUPDATE', f'/UPDATEPID={desktop().os.getpid()}']
    desktop().launch_installer(path, tmp_path)
    assert calls[1][0][0] == [str(path.resolve())]
    monkeypatch.setattr(desktop(), 'automatic_update_supported', lambda: False)
    with pytest.raises(updating.UpdateError):
        desktop().launch_installer(path, tmp_path, automatic=True)
    assert len(calls) == 2


def test_frozen_start_during_installer_mutex_does_not_open_or_lock_the_app(monkeypatch, tmp_path):
    messages = []
    monkeypatch.setattr(desktop().sys, 'frozen', True, raising=False)
    monkeypatch.setattr(desktop(), '_update_in_progress', lambda: True)
    monkeypatch.setattr(desktop(), '_message', messages.append)
    assert desktop().main(['--data-dir', str(tmp_path)]) == 0
    assert '잠시 후 다시 열어' in messages[0]
    assert not (tmp_path / 'desktop.lock').exists()


@pytest.mark.parametrize('failure, closed', [(False, True), (False, False), (True, False)])
def test_automatic_update_handoff_happens_after_normal_window_and_server_close_only(monkeypatch, tmp_path, failure, closed):
    from types import SimpleNamespace
    from suseoro.simple import app as app_module, update_coordinator
    calls = []
    fake_app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(app_module, 'create_app', lambda **kwargs: fake_app)
    monkeypatch.setattr(desktop(), 'automatic_update_supported', lambda: True)
    monkeypatch.setattr(desktop(), '_installer_mutex', lambda: None)
    monkeypatch.setattr(desktop(), '_message', lambda *args: None)
    path = tmp_path / 'prepared.exe'
    class Coordinator:
        def __init__(self, *args, auto_supported=False):
            self.auto_supported = auto_supported
        def start(self):
            calls.append('prepare')
        def close(self, *, normal_exit):
            calls.append(('coordinator-close', normal_exit))
            return path if normal_exit else None
    monkeypatch.setattr(update_coordinator, 'UpdateCoordinator', Coordinator)
    class Event:
        def __iadd__(self, callback):
            self.callback = callback
            return self
    class Server:
        url = 'http://127.0.0.1:12345'
        def __init__(self, *args):
            pass
        def start(self):
            calls.append('server-start')
        def stop(self):
            calls.append('server-stop')
    monkeypatch.setattr(desktop(), 'LocalServer', Server)
    def show_window(**kwargs):
        calls.append('window')
        if failure:
            raise RuntimeError('window failure')
        assert 'install' not in calls
        if closed:
            window.events.closed.callback()
    window = SimpleNamespace(events=SimpleNamespace(closed=Event()), destroy=lambda: None)
    view = SimpleNamespace(settings={}, create_window=lambda *args, **kwargs: window, start=show_window)
    monkeypatch.setitem(desktop().sys.modules, 'webview', view)
    def launch(candidate, data_dir, *, automatic):
        assert candidate == path and automatic
        assert calls[-1] == 'server-stop'
        lock = desktop().InstanceLock(data_dir)
        assert lock.acquire()
        lock.release()
        calls.append('install')
    monkeypatch.setattr(desktop(), 'launch_installer', launch)
    assert desktop().main(['--data-dir', str(tmp_path)]) == (1 if failure else 0)
    assert ('coordinator-close', closed and not failure) in calls
    assert ('install' in calls) is (closed and not failure)


def test_smoke_mode_never_starts_an_update_check(monkeypatch, tmp_path):
    from suseoro.simple import updating
    calls = []
    monkeypatch.setattr(desktop(), 'automatic_update_supported', lambda: True)
    monkeypatch.setattr(updating, 'check_update', lambda *args: calls.append('network'))
    monkeypatch.setattr(desktop(), 'launch_installer', lambda *args, **kwargs: calls.append('install'))
    assert desktop().main(['--smoke-test', '--data-dir', str(tmp_path)]) == 0
    assert calls == []
