import json
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import pytest

from suseoro.simple import reporting


ERROR = {"stage": "check", "category": "proxy", "code": "HTTP 407"}


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_constructor_stays_offline_and_report_has_only_safe_fields(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(reporting, "_send", calls.append)
    reporter = reporting.ErrorReporter(tmp_path)
    assert reporter.status()["configured"] and reporter.status()["enabled"]
    assert not calls
    reporter.record({**ERROR, "school_name": "private", "error": "https://private"})
    saved = json.loads((tmp_path / "error-reports.json").read_text())
    item = saved["queue"][0]
    assert set(item) == {"stage", "category", "code", "version", "install_id", "occurred_at", "os", "id"}
    assert "private" not in json.dumps(saved)
    assert not calls
    restored = reporting.ErrorReporter(tmp_path)
    assert restored._state["install_id"] == saved["install_id"]
    assert restored.status()["queued_count"] == 1


@pytest.mark.parametrize("payload", [
    {**ERROR, "code": "https://private/server"}, {**ERROR, "code": "a" * 81},
    {**ERROR, "category": "password"}, {**ERROR, "stage": []}, {**ERROR, "code": "x@y.example"},
])
def test_unstructured_or_private_error_text_is_never_queued(tmp_path, payload):
    reporter = reporting.ErrorReporter(tmp_path)
    reporter.record(payload)
    assert reporter.status()["queued_count"] == 0


def test_daily_dedup_queue_bound_and_disabled_setting_persist(monkeypatch, tmp_path):
    monkeypatch.setattr(reporting, "_now", lambda: "2026-10-12T09:00:00+00:00")
    reporter = reporting.ErrorReporter(tmp_path)
    for _ in range(10):
        reporter.record(ERROR)
    assert reporter.status()["queued_count"] == 1
    reporter = reporting.ErrorReporter(tmp_path)
    reporter.record(ERROR)
    assert reporter.status()["queued_count"] == 1
    monkeypatch.setattr(reporting, "_now", lambda: "2026-10-13T09:00:00+00:00")
    reporter.record(ERROR)
    assert reporter.status()["queued_count"] == 2
    for number in range(70):
        reporter.record({**ERROR, "code": f"HTTP {number}"})
    assert reporter.status()["queued_count"] == 50
    reporter.set_enabled(False)
    restored = reporting.ErrorReporter(tmp_path)
    restored.record({**ERROR, "code": "HTTP 499"})
    assert not restored.status()["enabled"] and restored.status()["queued_count"] == 50
    with pytest.raises(ValueError):
        restored.set_enabled("false")


def test_started_worker_flushes_persisted_reports_and_tracks_server_acceptance(monkeypatch, tmp_path):
    reporter = reporting.ErrorReporter(tmp_path)
    reporter.record(ERROR)
    reporter = reporting.ErrorReporter(tmp_path)
    entered, release = threading.Event(), threading.Event()
    sent = []
    def send(report):
        sent.append(report)
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(reporting, "_send", send)
    reporter.start()
    try:
        assert entered.wait(2)
        worker = reporter._worker
        reporter.start()
        assert reporter._worker is worker
        release.set()
        wait_for(lambda: reporter.status()["queued_count"] == 0)
        assert len(sent) == 1 and "id" not in sent[0]
        assert reporter.status()["last_sent_at"]
        assert "서버" in reporter.status()["message"]
        assert reporting.ErrorReporter(tmp_path).status()["queued_count"] == 0
    finally:
        reporter.close()


def test_failed_delivery_keeps_queue_and_retries_later(monkeypatch, tmp_path, caplog):
    calls = []
    def send(report):
        calls.append(report)
        if len(calls) == 1:
            raise URLError("https://private endpoint")
    monkeypatch.setattr(reporting, "_send", send)
    monkeypatch.setattr(reporting, "RETRY_SECONDS", 0.05)
    reporter = reporting.ErrorReporter(tmp_path)
    reporter.record(ERROR)
    reporter.start()
    try:
        wait_for(lambda: len(calls) == 1)
        assert reporter.status()["queued_count"] == 1
        assert not reporter.status()["last_sent_at"]
        wait_for(lambda: reporter.status()["queued_count"] == 0)
        assert len(calls) == 2
        assert "private" not in caplog.text
    finally:
        reporter.close()


def test_form_post_is_bounded_and_does_not_send_private_local_details(monkeypatch):
    requests = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def open_request(request, timeout):
        requests.append(request)
        assert timeout == 5 and request.full_url == reporting.REPORT_ENDPOINT
        return Response()
    monkeypatch.setattr(reporting, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    report = {**ERROR, "version": "2.3.0", "install_id": "a" * 32,
              "occurred_at": "2026-10-12T09:00:00+00:00", "os": "Windows 11"}
    reporting._send(report)
    fields = parse_qs(requests[0].data.decode(), keep_blank_values=True)
    assert fields["form-name"] == ["update-errors"]
    assert fields["bot-field"] == [""]
    assert "자동 업데이트 오류가 발생" in fields["report"][0]
    assert json.loads(fields["report"][0].split("\n\n진단 데이터:\n", 1)[1]) == report
    assert fields["name"] == ["수서로 자동 업데이트 진단"]
    assert fields["email"] == [reporting.RECIPIENT]
    assert requests[0].get_method() == "POST"
    assert "학교명, 사용자 이름, 도서 목록과 인증키는 포함하지 않았습니다." in fields["report"][0]
    with pytest.raises(ValueError):
        reporting._send({**report, "unexpected": "a" * 10000})
    assert len(requests) == 1


def test_post_redirect_is_rejected_and_proxy_failure_uses_windows(monkeypatch):
    with pytest.raises(HTTPError):
        reporting._NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.example")
    monkeypatch.setattr(reporting, "os", SimpleNamespace(name="nt"))
    def fail(*args, **kwargs):
        raise URLError("private proxy")
    monkeypatch.setattr(reporting, "build_opener", lambda *args: SimpleNamespace(open=fail))
    native = []
    monkeypatch.setattr(reporting, "_windows_post", native.append)
    reporting._send({**ERROR, "version": "2.3.0", "install_id": "a" * 32, "occurred_at": "2026-10-12T09:00:00+00:00"})
    assert len(native) == 1


def test_native_post_keeps_tls_checks_and_rejects_non_success(monkeypatch):
    import ctypes
    closed = []
    status = [200]
    def request(connection, method, path, version, referrer, accept, flags, context):
        assert method == "POST" and path == "/" and flags & 0x00800000
        assert flags & 0x00200000 and flags & 0x00080000
        assert not flags & (0x00002000 | 0x00001000)
        return 3
    def query(handle, flag, buffer, length, index):
        buffer._obj.value = status[0]
        return True
    native = SimpleNamespace(InternetOpenW=lambda *args: 1, InternetConnectW=lambda *args: 2,
        HttpOpenRequestW=request, HttpSendRequestW=lambda *args: True,
        InternetSetOptionW=lambda *args: True, HttpQueryInfoW=query,
        InternetCloseHandle=lambda handle: closed.append(handle))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: native, raising=False)
    reporting._windows_post(b"form-name=update-errors")
    assert closed == [3, 2, 1]
    status[0] = 302
    with pytest.raises(HTTPError):
        reporting._windows_post(b"form-name=update-errors")
    assert closed == [3, 2, 1, 3, 2, 1]


def test_test_report_honors_opt_out_and_enqueues_only_a_self_test(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(reporting, "_send", sent.append)
    reporter = reporting.ErrorReporter(tmp_path)
    reporter.set_enabled(False)
    with pytest.raises(ValueError):
        reporter.test_report()
    assert not sent
    reporter.set_enabled(True)
    reporter.test_report()
    try:
        wait_for(lambda: bool(sent))
        assert sent[0]["stage"] == "test" and sent[0]["code"] == "SelfTest"
    finally:
        reporter.close()
