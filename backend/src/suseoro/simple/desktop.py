"""Native desktop launcher; one local server per user data folder.

pywebview window lifecycle/download API: https://pywebview.flowrl.com/api/
Inno app mutex: https://jrsoftware.org/ishelp/topic_setup_appmutex.htm
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen
import webbrowser

from suseoro.simple import VERSION


def default_data_dir() -> Path:
    return Path(os.environ.get("SUSEORO_DATA_DIR") or (Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "Suseoro"))


def frontend_dir() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "ui"
    return Path(__file__).resolve().parents[4] / "frontend" / "dist"


def _configure_bundled_ocr() -> None:
    base = Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parents[4]
    directory = base / "portable_tesseract"
    executable = directory / "tesseract.exe"
    if executable.is_file():
        os.environ["SUSEORO_TESSERACT"] = str(executable)
        os.environ["TESSDATA_PREFIX"] = str(directory / "tessdata")
        # Tesseract finds its DLLs beside its executable. Keep Python's loader
        # search path free of the OCR distribution's incompatible OpenSSL DLLs.


class InstanceLock:
    """An OS-released lock; stale files cannot prevent recovery after a crash."""
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.file = None

    def acquire(self) -> bool:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file = (self.data_dir / "desktop.lock").open("a+b")
        try:
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close()
            self.file = None
            return False

    def release(self) -> None:
        if self.file:
            self.file.close()
            self.file = None


class LocalServer:
    def __init__(self, app, port: int = 0):
        import uvicorn
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            self.socket.bind(("127.0.0.1", port))
            self.socket.listen(128)
            self.socket.setblocking(False)
        except Exception:
            self.socket.close()
            raise
        self.port = self.socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                    log_config=None, log_level="warning", access_log=False,
                                    loop="asyncio", http="h11", ws="none", timeout_graceful_shutdown=4))
        self.thread = threading.Thread(target=lambda: self.server.run(sockets=[self.socket]),
                                       name="suseoro-local-server", daemon=True)

    def start(self, timeout: float = 10) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return
            if not self.thread.is_alive():
                break
            time.sleep(0.02)
        self.stop()
        raise RuntimeError("수서로를 시작하지 못했습니다. 잠시 후 다시 실행해 주세요.")

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=6)
        self.socket.close()


def _message(text: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, "수서로", 0x40)
    elif sys.stderr:
        print(text, file=sys.stderr)


def launch_installer(path: Path, data_dir: Path) -> None:
    from suseoro.simple.updating import verify_download
    verify_download(path, data_dir)
    # Show the installation wizard; no silent install and no forced process close.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    subprocess.Popen([str(Path(path).resolve())], cwd=str(Path(path).parent), close_fds=True, creationflags=flags)


def _daily_update_check(app, data_dir: Path) -> None:
    from suseoro.simple.updating import check_update
    cache = data_dir / "update-check.json"
    try:
        saved = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}
        if saved.get("version") == VERSION and 0 <= time.time() - saved.get("checked_at", 0) < 86400:
            app.state.update_info = saved["info"]
            return
        info = {key: value for key, value in check_update(VERSION).items() if key != "asset"}
        app.state.update_info = info
        temporary = cache.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": VERSION, "checked_at": time.time(), "info": info}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(cache)
    except Exception:
        logging.getLogger(__name__).info("Automatic update check unavailable")


def _installer_mutex():
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    return kernel.CreateMutexW(None, False, "Local\\Suseoro.App")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="수서로: 학교도서관 도서 구입 도우미")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--browser", action="store_true", help="기본 브라우저에서 열기")
    parser.add_argument("--headless", action="store_true", help="테스트용 내부 서버 실행")
    parser.add_argument("--smoke-test", action="store_true", help="시작 상태 확인 후 종료")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("포트는 0부터 65535 사이여야 합니다.")
    if args.version:
        if sys.stdout:
            print(VERSION)
        return 0
    data_dir = (args.data_dir or default_data_dir()).resolve()
    lock = InstanceLock(data_dir)
    if not lock.acquire():
        if not (args.headless or args.smoke_test):
            _message("수서로가 이미 실행 중입니다. 열려 있는 수서로 창을 확인해 주세요.")
        return 0
    logging.basicConfig(filename=data_dir / "desktop.log", level=logging.WARNING,
                        encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
    server = None
    mutex = None
    pending_installer: list[Path] = []
    exit_requested = threading.Event()
    windows: list = []

    def request_shutdown() -> None:
        def close() -> None:
            time.sleep(0.3)  # Allow the local HTTP response to finish before stopping.
            exit_requested.set()
            if windows:
                try:
                    windows[0].destroy()
                except Exception:
                    logging.getLogger(__name__).exception("Could not close window")
        threading.Thread(target=close, daemon=True).start()

    def install_update(path: Path) -> None:
        from suseoro.simple.updating import verify_download
        verify_download(path, data_dir)
        if not pending_installer:
            pending_installer.append(Path(path))
            request_shutdown()

    try:
        from suseoro.simple.app import create_app
        _configure_bundled_ocr()
        if hasattr(sys, "_MEIPASS") and not (frontend_dir() / "index.html").is_file():
            raise RuntimeError("설치 파일에 화면 파일이 없습니다.")
        app = create_app(data_dir=data_dir, frontend_dir=frontend_dir())
        app.state.shutdown_callback = request_shutdown
        app.state.install_update_callback = install_update
        server = LocalServer(app, args.port)
        server.start()
        (data_dir / "desktop-instance.json").write_text(json.dumps({"url": server.url, "pid": os.getpid()}), encoding="utf-8")
        if args.smoke_test:
            with urlopen(server.url + "/api/library/health", timeout=5) as response:
                healthy = json.load(response).get("status") == "ready"
            return 0 if healthy else 1
        if args.headless:
            if sys.stdout:
                print(server.url, flush=True)
            while server.thread.is_alive() and not exit_requested.wait(0.1):
                pass
        else:
            mutex = _installer_mutex()
            threading.Thread(target=_daily_update_check, args=(app, data_dir), daemon=True,
                             name="suseoro-update-check").start()
            if args.browser:
                webbrowser.open(server.url)
                while server.thread.is_alive() and not exit_requested.wait(0.1):
                    pass
            else:
                import webview
                webview.settings["ALLOW_DOWNLOADS"] = True
                webview.settings["ALLOW_FILE_URLS"] = False
                window = webview.create_window("수서로", server.url, width=1400, height=940,
                                               min_size=(1024, 720), text_select=True, background_color="#F4F6F8")
                windows.append(window)
                window.events.closed += exit_requested.set
                webview.start(private_mode=False, storage_path=str(data_dir / "webview"), debug=False)
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger(__name__).exception("Desktop startup failed")
        if not (args.headless or args.smoke_test):
            _message("수서로를 열지 못했습니다. Microsoft Edge WebView2가 설치되어 있는지 확인해 주세요. 브라우저용 실행으로도 열 수 있습니다.")
        return 1
    finally:
        if server:
            server.stop()
        (data_dir / "desktop-instance.json").unlink(missing_ok=True)
        lock.release()
        if mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))
    if pending_installer:
        try:
            launch_installer(pending_installer[0], data_dir)
        except Exception:
            logging.getLogger(__name__).exception("Installer handoff failed")
            _message("설치 프로그램을 열지 못했습니다. 수서로를 다시 열고 업데이트를 시도해 주세요.")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
