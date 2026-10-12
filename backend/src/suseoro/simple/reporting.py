"""Bounded, anonymous update diagnostics with offline retry and no mail credentials."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from suseoro.simple import VERSION
from suseoro.simple.updating import failure_code

REPORT_ENDPOINT = "https://suseoro-update-reports.netlify.app/"
RECIPIENT = "buildergarlic@gmail.com"
RETRY_SECONDS = 15 * 60
MAX_QUEUE = 50
_STAGES = {"check", "download", "install", "verify", "test"}
_CATEGORIES = {"network", "metadata", "validation", "proxy", "certificate", "timeout", "dns",
               "rate_limit", "blocked", "server", "tls", "test"}
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,59}(?: [0-9]{1,10})?")
_ID = re.compile(r"[0-9a-f]{32}")
_VERSION = re.compile(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp(value) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value).utcoffset().total_seconds() == 0
    except (ValueError, AttributeError):
        return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(REPORT_ENDPOINT, code, "Report redirect rejected", {}, None)


def _windows_post(body: bytes) -> None:
    """The fixed report host uses the user's Windows proxy/PAC and normal TLS."""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("wininet", use_last_error=True)
    declarations = {
        "InternetOpenW": (wintypes.HANDLE, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]),
        "InternetConnectW": (wintypes.HANDLE, [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.WORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]),
        "HttpOpenRequestW": (wintypes.HANDLE, [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPCWSTR), wintypes.DWORD, ctypes.c_size_t]),
        "HttpSendRequestW": (wintypes.BOOL, [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]),
        "InternetSetOptionW": (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]),
        "HttpQueryInfoW": (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]),
        "InternetCloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
    }
    for name, (result, arguments) in declarations.items():
        function = getattr(api, name)
        function.restype, function.argtypes = result, arguments
    handles = []
    def checked(handle):
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(handle)
        return handle
    try:
        session = checked(api.InternetOpenW(f"Suseoro/{VERSION}", 0, None, None, 0))
        timeout = wintypes.DWORD(5000)
        for option in (2, 5, 6):
            if not api.InternetSetOptionW(session, option, ctypes.byref(timeout), ctypes.sizeof(timeout)):
                raise ctypes.WinError(ctypes.get_last_error())
        connection = checked(api.InternetConnectW(session, "suseoro-update-reports.netlify.app", 443, None, None, 3, 0, 0))
        # TLS, no cache, no automatic redirect, no cookies or cookie UI.
        flags = 0x00800000 | 0x80000000 | 0x04000000 | 0x00200000 | 0x00080000 | 0x00000200
        request = checked(api.HttpOpenRequestW(connection, "POST", "/", None, None, None, flags, 0))
        headers = "Content-Type: application/x-www-form-urlencoded\r\nAccept: application/json\r\n"
        buffer = ctypes.create_string_buffer(body)
        if not api.HttpSendRequestW(request, headers, len(headers), buffer, len(body)):
            raise ctypes.WinError(ctypes.get_last_error())
        status, length = wintypes.DWORD(), wintypes.DWORD(ctypes.sizeof(wintypes.DWORD))
        if not api.HttpQueryInfoW(request, 19 | 0x20000000, ctypes.byref(status), ctypes.byref(length), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not 200 <= status.value < 300:
            raise HTTPError(REPORT_ENDPOINT, status.value, "Report not accepted", {}, None)
    finally:
        for handle in reversed(handles):
            api.InternetCloseHandle(handle)


def _send(report: dict) -> None:
    message = (f"수서로 {report['version']}의 자동 업데이트 오류 보고 연결을 확인하는 테스트입니다."
               if report['stage'] == 'test' else
               f"수서로 {report['version']}에서 자동 업데이트 오류가 발생하여 진단 정보를 보고합니다.")
    message += (f" 오류 단계는 {report['stage']}, 분류는 {report['category']}, 오류 코드는 {report['code']}입니다."
                " 학교명, 사용자 이름, 도서 목록과 인증키는 포함하지 않았습니다.\n\n진단 데이터:\n" +
                json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    fields = {"form-name": "update-errors", "bot-field": "", "subject": "수서로 자동 업데이트 오류",
              "name": "수서로 자동 업데이트 진단", "email": RECIPIENT, "report": message,
              **{key: report[key] for key in ("version", "stage", "category", "code", "install_id", "occurred_at")}}
    body = urlencode(fields).encode("utf-8")
    if len(body) > 8192:
        raise ValueError("Report size")
    request = Request(REPORT_ENDPOINT, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "User-Agent": f"Suseoro/{VERSION}"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=5) as response:
            if not 200 <= response.status < 300:
                raise HTTPError(REPORT_ENDPOINT, response.status, "Report not accepted", {}, None)
    except (URLError, OSError) as error:
        if os.name != "nt" or (isinstance(error, HTTPError) and error.code not in (403, 407, 429)):
            raise
        _windows_post(body)


class ErrorReporter:
    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "error-reports.json"
        self._lock = threading.RLock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._worker: threading.Thread | None = None
        self._retry_after = 0.0
        self._state = {"enabled": True, "install_id": uuid.uuid4().hex, "queue": [],
                       "last_sent_at": None, "seen_day": _now()[:10], "seen": []}
        try:
            if self._path.stat().st_size > 2_000_000:
                raise ValueError()
            saved = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                if type(saved.get("enabled")) is bool:
                    self._state["enabled"] = saved["enabled"]
                if isinstance(saved.get("install_id"), str) and _ID.fullmatch(saved["install_id"]):
                    self._state["install_id"] = saved["install_id"]
                if _timestamp(saved.get("last_sent_at")):
                    self._state["last_sent_at"] = saved["last_sent_at"]
                for item in saved.get("queue", [])[-MAX_QUEUE:]:
                    clean = self._clean(item)
                    if clean and _timestamp(item.get("occurred_at")) and _VERSION.fullmatch(str(item.get("version", ""))):
                        self._state["queue"].append({**clean, "version": item["version"],
                            "install_id": self._state["install_id"], "occurred_at": item["occurred_at"],
                            "os": self._os(), "id": item.get("id") if _ID.fullmatch(str(item.get("id", ""))) else uuid.uuid4().hex})
                if saved.get("seen_day") == self._state["seen_day"] and isinstance(saved.get("seen"), list):
                    self._state["seen"] = [key for key in saved["seen"][-200:] if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)]
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    @staticmethod
    def _os() -> str:
        value = platform.release()
        return ("Windows " if os.name == "nt" else "Other ") + (value[:30] if re.fullmatch(r"[A-Za-z0-9_.-]{1,30}", value) else "unknown")

    @staticmethod
    def _clean(payload) -> dict | None:
        if not isinstance(payload, dict) or not isinstance(payload.get("stage"), str) or not isinstance(payload.get("category"), str):
            return None
        if payload["stage"] not in _STAGES or payload["category"] not in _CATEGORIES:
            return None
        code = payload.get("code")
        if not isinstance(code, str) or len(code) > 80 or not _CODE.fullmatch(code):
            return None
        return {key: payload[key] for key in ("stage", "category", "code")}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def status(self) -> dict:
        with self._lock:
            queued = len(self._state["queue"])
            message = "업데이트 오류 분류와 수서로 버전을 익명으로 자동 보고합니다."
            if not self._state["enabled"]:
                message = "오류 자동 보고를 껐습니다."
            elif queued:
                message = "오류 보고를 이 PC에 보관했습니다. 연결되면 자동으로 다시 전송합니다."
            elif self._state["last_sent_at"]:
                message = "오류 보고 서버가 최근 보고를 접수했습니다."
            return {"enabled": self._state["enabled"], "configured": True, "recipient": RECIPIENT,
                    "queued_count": queued, "last_sent_at": self._state["last_sent_at"], "message": message}

    def set_enabled(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("오류 자동 보고 사용 여부를 확인해 주세요.")
        with self._lock:
            self._state["enabled"] = enabled
            self._save()
        self._wake.set()
        return self.status()

    def record(self, payload: dict) -> dict:
        clean = self._clean(payload)
        if clean is None:
            return self.status()
        with self._lock:
            if not self._state["enabled"]:
                return self.status()
            now = _now()
            if self._state["seen_day"] != now[:10]:
                self._state.update(seen_day=now[:10], seen=[])
            fingerprint = hashlib.sha256(json.dumps([VERSION, clean], sort_keys=True).encode()).hexdigest()
            if fingerprint in self._state["seen"]:
                return self.status()
            self._state["seen"] = (self._state["seen"] + [fingerprint])[-200:]
            self._state["queue"] = (self._state["queue"] + [{**clean, "version": VERSION,
                "install_id": self._state["install_id"], "occurred_at": now, "os": self._os(), "id": uuid.uuid4().hex}])[-MAX_QUEUE:]
            self._save()
        self._wake.set()
        return self.status()

    def test_report(self) -> dict:
        if not self.status()["enabled"]:
            raise ValueError("오류 자동 보고를 켠 뒤 테스트 보고를 보내 주세요.")
        self.record({"stage": "test", "category": "test", "code": "SelfTest"})
        with self._lock:
            self._retry_after = 0.0
        self.start()
        self._wake.set()
        return self.status()

    def start(self) -> dict:
        with self._lock:
            if not self._stop.is_set() and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._run, name="suseoro-error-reporter", daemon=True)
                self._worker.start()
        self._wake.set()
        return self.status()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            with self._lock:
                report = dict(self._state["queue"][0]) if self._state["enabled"] and self._state["queue"] else None
            if report is not None and time.monotonic() >= self._retry_after:
                try:
                    _send({key: value for key, value in report.items() if key != "id"})
                    with self._lock:
                        if self._stop.is_set():
                            return
                        self._state["queue"] = [item for item in self._state["queue"] if item["id"] != report["id"]]
                        self._state["last_sent_at"] = _now()
                        self._save()
                    continue
                except Exception as error:
                    self._retry_after = time.monotonic() + RETRY_SECONDS
                    logging.getLogger(__name__).warning("Update report not accepted; saved for retry: %s", failure_code(error))
            wait = max(0.05, self._retry_after - time.monotonic()) if report is not None else RETRY_SECONDS
            self._wake.wait(wait)

    def close(self) -> None:
        with self._lock:
            self._stop.set()
            self._wake.set()
