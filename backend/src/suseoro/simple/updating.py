"""Official stable-release updates, with exact names and SHA-256 verification.

GitHub release metadata documents the asset digest and stable latest endpoint:
https://docs.github.com/en/rest/releases/releases#get-the-latest-release
Prepared automatic updates run only after the installed application exits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import ssl
import time
from typing import Any
from threading import Event
from urllib.parse import urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from suseoro.simple import VERSION

REPOSITORY = "buildergarlic/suseoro"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LATEST_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
LATEST_MANIFEST = f"{RELEASES_URL}/latest/download/update.json"
MAX_INSTALLER_BYTES = 768 * 1024 * 1024
DOWNLOAD_SECONDS = 180
_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_HASH = re.compile(r"[0-9a-fA-F]{64}")
_INSTALLER = re.compile(r"Suseoro-Setup-(\d+\.\d+\.\d+)\.exe")


class UpdateError(ValueError):
    """Safe, Korean explanation suitable for showing in the application."""

    def __init__(self, message: str, *, category: str = "validation", code: str = "UpdateError"):
        super().__init__(message)
        self.category = category
        self.code = code


class UpdateCancelled(UpdateError):
    """A download was cancelled before it became an installable update."""


def failure_code(error: Exception) -> str:
    """Log a useful network code without URLs, credentials, or response bodies."""
    if isinstance(error, UpdateError):
        return error.code
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, URLError):
        error = error.reason
    code = type(error).__name__
    number = getattr(error, "winerror", None) or getattr(error, "errno", None)
    return f"{code} {number}" if isinstance(number, int) else code


def failure_category(error: Exception) -> str:
    if isinstance(error, UpdateError):
        return error.category
    if isinstance(error, HTTPError):
        if error.code == 407:
            return "proxy"
        if error.code == 429 or (error.code == 403 and (error.headers or {}).get("X-RateLimit-Remaining") == "0"):
            return "rate_limit"
        return "blocked" if error.code in (401, 403, 451) else "server"
    if isinstance(error, URLError):
        error = error.reason
    number = getattr(error, "winerror", None) or getattr(error, "errno", None)
    if isinstance(error, (ssl.SSLCertVerificationError, ssl.SSLError)) or number in (12037, 12038, 12045, 12057, 12175):
        return "certificate"
    if isinstance(error, (TimeoutError, socket.timeout)) or number in (12002, 10060):
        return "timeout"
    if isinstance(error, socket.gaierror) or number in (12007, 11001):
        return "dns"
    if number in (12015, 12166, 12167, 12178, 12180):
        return "proxy"
    return "network" if isinstance(error, (OSError, str)) else "metadata"


def failure_message(error: Exception) -> str:
    if isinstance(error, UpdateError):
        return str(error)
    return {
        "proxy": "학교망 프록시 연결 또는 인증을 확인해 주세요. 학교 전산 담당자에게 문의할 수 있습니다.",
        "rate_limit": "GitHub의 학교망 공동 사용 요청 한도에 도달했습니다. 잠시 후 다시 확인해 주세요.",
        "blocked": "학교망에서 GitHub 업데이트 주소 접근을 제한했을 수 있습니다. 학교 전산 담당자에게 문의해 주세요.",
        "certificate": "업데이트 서버 인증서를 확인하지 못했습니다. PC 날짜와 학교망 보안 인증서 설정을 확인해 주세요.",
        "timeout": "업데이트 서버 응답이 늦어 확인을 마치지 못했습니다. 잠시 후 다시 시도해 주세요.",
        "dns": "GitHub 업데이트 주소를 찾지 못했습니다. 학교망 DNS 또는 주소 차단 설정을 확인해 주세요.",
        "server": "업데이트 서버가 요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        "metadata": "업데이트 배포 정보를 읽지 못했습니다. 잠시 후 다시 시도해 주세요.",
    }.get(failure_category(error), "업데이트 서버에 연결하지 못했습니다. 인터넷 및 학교망 프록시 설정을 확인해 주세요.")


def _check_cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled("업데이트 다운로드를 중지했습니다.")


def supports_automatic_install(version: str) -> bool:
    parsed = _version(version)
    return parsed is not None and parsed >= (2, 0, 2)


def _version(text: Any) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    return tuple(map(int, match.groups())) if match else None


def _trusted_url(url: str, *, redirect: bool = False) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
        return False
    if parsed.hostname == "api.github.com":
        return parsed.path == f"/repos/{REPOSITORY}/releases/latest"
    if parsed.hostname == "github.com":
        return parsed.path == f"/{REPOSITORY}/releases/latest/download/update.json" or parsed.path.startswith(f"/{REPOSITORY}/releases/download/")
    return redirect and parsed.hostname in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class _TrustedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted_url(newurl, redirect=True):
            raise UpdateError("공식 배포 주소가 아닌 곳으로 연결되어 다운로드를 중단했습니다.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _headers(url: str) -> dict[str, str]:
    return {
        "User-Agent": f"Suseoro/{VERSION}", "Accept": "application/vnd.github+json" if url == LATEST_API else "application/octet-stream",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class _WindowsResponse:
    """Use the user's Windows proxy/PAC and certificate settings, never ignore TLS errors."""

    def __init__(self, url: str, timeout: float):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._api = ctypes.WinDLL("wininet", use_last_error=True)
        declarations = {
            "InternetOpenW": (wintypes.HANDLE, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]),
            "InternetOpenUrlW": (wintypes.HANDLE, [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]),
            "InternetSetOptionW": (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]),
            "HttpQueryInfoW": (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]),
            "InternetReadFile": (wintypes.BOOL, [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]),
            "InternetCloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        }
        for name, (result, args) in declarations.items():
            function = getattr(self._api, name)
            function.restype, function.argtypes = result, args
        self._session = self._api.InternetOpenW(f"Suseoro/{VERSION}", 0, None, None, 0)  # PRECONFIG
        self._request = None
        if not self._session:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            deadline = time.monotonic() + timeout
            # Reload, no cache writes, no automatic redirects, no cookies or cookie UI.
            flags = 0x80000000 | 0x04000000 | 0x00200000 | 0x00080000 | 0x00000200
            for _ in range(5):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                milliseconds = wintypes.DWORD(max(1, int(remaining * 1000)))
                for option in (2, 5, 6):  # connect, send, receive timeout
                    if not self._api.InternetSetOptionW(self._session, option, ctypes.byref(milliseconds), ctypes.sizeof(milliseconds)):
                        raise ctypes.WinError(ctypes.get_last_error())
                headers = "".join(f"{key}: {value}\r\n" for key, value in _headers(url).items())
                self._request = self._api.InternetOpenUrlW(self._session, url, headers, len(headers), flags, 0)
                if not self._request:
                    raise ctypes.WinError(ctypes.get_last_error())
                status, length = wintypes.DWORD(), wintypes.DWORD(ctypes.sizeof(wintypes.DWORD))
                if not self._api.HttpQueryInfoW(self._request, 19 | 0x20000000, ctypes.byref(status), ctypes.byref(length), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if status.value == 200:
                    return
                if status.value not in (301, 302, 303, 307, 308):
                    remaining = self._header(65535, "X-RateLimit-Remaining")  # CUSTOM
                    raise HTTPError(url, status.value, "Update server response", {"X-RateLimit-Remaining": remaining}, None)
                next_url = urljoin(url, self._header(33))  # LOCATION
                if not _trusted_url(next_url, redirect=True):
                    raise UpdateError("공식 배포 주소가 아닌 곳으로 연결되어 다운로드를 중단했습니다.")
                self._api.InternetCloseHandle(self._request)
                self._request = None
                url = next_url
            raise UpdateError("업데이트 서버의 연결 변경이 너무 많아 중단했습니다.")
        except BaseException:
            self.close()
            raise

    def _header(self, query: int, name: str = "") -> str:
        from ctypes import wintypes
        buffer = self._ctypes.create_unicode_buffer(8192)
        buffer.value = name
        length = wintypes.DWORD(self._ctypes.sizeof(buffer))
        if not self._api.HttpQueryInfoW(self._request, query, buffer, self._ctypes.byref(length), None):
            return ""
        return buffer.value

    def read1(self, size: int) -> bytes:
        from ctypes import wintypes
        buffer = self._ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not self._api.InternetReadFile(self._request, buffer, size, self._ctypes.byref(read)):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        return buffer.raw[:read.value]

    def read(self, size: int) -> bytes:
        chunks, remaining = [], size
        while remaining:
            chunk = self.read1(min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        for name in ("_request", "_session"):
            handle = getattr(self, name, None)
            if handle:
                self._api.InternetCloseHandle(handle)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _open_url(url: str, timeout: float):
    if not _trusted_url(url):
        raise UpdateError("공식 업데이트 주소가 아닙니다.")
    try:
        return build_opener(_TrustedRedirect()).open(Request(url, headers=_headers(url)), timeout=timeout)
    except (URLError, OSError) as error:
        if os.name != "nt" or (isinstance(error, HTTPError) and error.code not in (403, 407, 429)):
            raise
        logging.getLogger(__name__).info("Retrying update using Windows network settings: %s", failure_code(error))
        return _WindowsResponse(url, timeout)


def _read_bounded(response, maximum: int, seconds: float) -> bytes:
    deadline = time.monotonic() + seconds
    result = bytearray()
    while len(result) <= maximum:
        if time.monotonic() >= deadline:
            raise TimeoutError()
        chunk = response.read1(min(65_536, maximum + 1 - len(result)))
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


def _get_json(url: str) -> Any:
    with _open_url(url, 5) as response:
        raw = _read_bounded(response, 2_000_000, 5)
    if len(raw) > 2_000_000:
        raise UpdateError("업데이트 정보가 너무 큽니다.")
    return json.loads(raw.decode("utf-8-sig"))


def check_update(current_version: str = VERSION) -> dict[str, Any]:
    result: dict[str, Any] = {"current_version": current_version, "latest_version": None,
                              "available": False, "url": RELEASES_URL,
                              "message": "현재 최신 버전을 사용하고 있습니다.", "asset": None}
    try:
        current = _version(current_version)
        try:
            release = _get_json(LATEST_API)
            result["metadata_source"] = "github_api"
        except (URLError, OSError, json.JSONDecodeError, UnicodeError) as error:
            logging.getLogger(__name__).info("Trying official release manifest: %s", failure_code(error))
            try:
                release = _get_json(LATEST_MANIFEST)
                result["metadata_source"] = "release_manifest"
            except Exception:
                raise error
        if not current or not isinstance(release, dict):
            raise UpdateError("버전 정보가 올바르지 않습니다.")
        latest = _version(release.get("tag_name"))
        if release.get("draft") or release.get("prerelease") or not latest:
            result["message"] = "설치할 정식 업데이트가 없습니다."
            return result
        version = ".".join(map(str, latest))
        result["latest_version"] = version
        assets = release.get("assets", [])
        if not isinstance(assets, list):
            raise UpdateError("배포 파일 정보가 올바르지 않습니다.")
        name = f"Suseoro-Setup-{version}.exe"
        prefix = f"{RELEASES_URL}/download/{release['tag_name']}/"
        asset = next((dict(item) for item in assets if isinstance(item, dict)
                      and item.get("name") == name
                      and item.get("browser_download_url") == prefix + name), None)
        if asset:
            for sidecar_name in (name + ".sha256", "SHA256SUMS", "SHA256SUMS.txt"):
                sidecar = next((item for item in assets if isinstance(item, dict)
                                and item.get("name") == sidecar_name
                                and item.get("browser_download_url") == prefix + sidecar_name), None)
                if sidecar:
                    asset["checksum_asset"] = sidecar
                    break
        result["asset"] = asset
        result["url"] = f"{RELEASES_URL}/tag/{release['tag_name']}"
        result["available"] = latest > current and asset is not None
        if result["available"]:
            result["message"] = f"새 버전 {version}을 설치할 수 있습니다."
        elif latest > current:
            result["message"] = "새 버전의 Windows 설치 파일이 아직 준비되지 않았습니다."
        return result
    except Exception as error:
        logging.getLogger(__name__).warning("GitHub release check failed: %s", failure_code(error))
        result["error"] = True
        result["error_category"] = failure_category(error)
        result["error_code"] = failure_code(error)
        result["message"] = failure_message(error)
        return result


def _expected_hash(asset: dict[str, Any]) -> str:
    digest = asset.get("digest", "")
    if isinstance(digest, str) and digest.startswith("sha256:") and _HASH.fullmatch(digest[7:]):
        return digest[7:].lower()
    sidecar = asset.get("checksum_asset")
    if isinstance(sidecar, dict):
        with _open_url(sidecar["browser_download_url"], 10) as response:
            raw = _read_bounded(response, 65_536, 10)
        if len(raw) > 65_536:
            raise UpdateError("검증 정보가 너무 큽니다.")
        text = raw.decode("utf-8-sig")
        if sidecar["name"] == asset["name"] + ".sha256" and _HASH.fullmatch(text.strip()):
            return text.strip().lower()
        for line in text.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and _HASH.fullmatch(parts[0]) and parts[1].lstrip(" *") == asset["name"]:
                return parts[0].lower()
    raise UpdateError("공식 SHA-256 검증 정보가 없어 설치 파일을 내려받을 수 없습니다.")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_update(info: dict[str, Any], data_dir: Path, *, cancel: Event | None = None) -> Path:
    """Re-fetch official metadata, then atomically save only a verified installer."""
    _check_cancelled(cancel)
    latest = check_update()
    _check_cancelled(cancel)
    if latest.get("error"):
        raise UpdateError(latest["message"], category=latest["error_category"], code=latest["error_code"])
    if not latest["available"] or latest["latest_version"] != info.get("latest_version"):
        raise UpdateError("배포 버전이 변경되었거나 설치 가능한 업데이트가 없습니다. 다시 확인해 주세요.")
    asset = latest["asset"]
    size = asset.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_INSTALLER_BYTES:
        raise UpdateError("설치 파일 크기가 허용 범위를 벗어났습니다.")
    directory = Path(data_dir).resolve() / "updates"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / asset["name"]
    temporary = destination.with_suffix(".exe.part")
    try:
        expected = _expected_hash(asset)
        _check_cancelled(cancel)
        if destination.is_file() and destination.stat().st_size == size and _file_hash(destination) == expected:
            with destination.open("rb") as source:
                if source.read(2) != b"MZ":
                    raise UpdateError("Windows 설치 파일 형식이 아닙니다.")
            _check_cancelled(cancel)
            _write_verified(destination, expected)
            return destination
        deadline = time.monotonic() + DOWNLOAD_SECONDS
        written = 0
        digest = hashlib.sha256()
        with _open_url(asset["browser_download_url"], 15) as response, temporary.open("wb") as output:
            while True:
                _check_cancelled(cancel)
                if time.monotonic() >= deadline:
                    raise UpdateError("다운로드 대기 시간이 초과되었습니다. 다시 시도해 주세요.")
                chunk = response.read1(1024 * 1024)
                _check_cancelled(cancel)
                if not chunk:
                    break
                written += len(chunk)
                if written > size or written > MAX_INSTALLER_BYTES:
                    raise UpdateError("설치 파일 크기가 배포 정보와 다릅니다.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if written != size or digest.hexdigest() != expected:
            raise UpdateError("설치 파일 검증에 실패했습니다. 다운로드한 파일은 실행하지 않습니다.")
        with temporary.open("rb") as source:
            if source.read(2) != b"MZ":
                raise UpdateError("Windows 설치 파일 형식이 아닙니다.")
        _check_cancelled(cancel)
        temporary.replace(destination)
        _write_verified(destination, expected)
        _check_cancelled(cancel)
        return destination
    except UpdateError as error:
        if not isinstance(error, UpdateCancelled):
            logging.getLogger(__name__).warning("Update download rejected: %s", failure_code(error))
        raise
    except Exception as error:
        logging.getLogger(__name__).warning("Update download failed: %s", failure_code(error))
        raise UpdateError(failure_message(error), category=failure_category(error), code=failure_code(error)) from None
    finally:
        temporary.unlink(missing_ok=True)


def _write_verified(path: Path, digest: str) -> None:
    path.with_suffix(".exe.verified.json").write_text(json.dumps({"sha256": digest, "repository": REPOSITORY}), encoding="utf-8")


def verify_download(path: Path, data_dir: Path) -> bool:
    path = Path(path).resolve()
    if path.parent != (Path(data_dir).resolve() / "updates") or not _INSTALLER.fullmatch(path.name):
        raise UpdateError("검증된 업데이트 설치 파일만 실행할 수 있습니다.")
    try:
        proof = json.loads(path.with_suffix(".exe.verified.json").read_text(encoding="utf-8"))
        if proof.get("repository") != REPOSITORY or not _HASH.fullmatch(proof.get("sha256", "")) or _file_hash(path) != proof["sha256"]:
            raise ValueError()
        with path.open("rb") as source:
            if source.read(2) != b"MZ":
                raise ValueError()
    except Exception:
        raise UpdateError("설치 파일이 변경되었거나 검증되지 않았습니다. 다시 내려받아 주세요.") from None
    return True
