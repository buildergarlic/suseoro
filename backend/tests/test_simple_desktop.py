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
    assert "2.0.0" in capsys.readouterr().out


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
    monkeypatch.setattr(desktop(), "_daily_update_check", lambda *args: None)
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
