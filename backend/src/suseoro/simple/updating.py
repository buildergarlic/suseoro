"""Official stable-release updates, with exact names and SHA-256 verification.

GitHub release metadata documents the asset digest and stable latest endpoint:
https://docs.github.com/en/rest/releases/releases#get-the-latest-release
Prepared automatic updates run only after the installed application exits.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from threading import Event
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from suseoro.simple import VERSION

REPOSITORY = "buildergarlic/suseoro"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LATEST_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_INSTALLER_BYTES = 768 * 1024 * 1024
DOWNLOAD_SECONDS = 180
_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_HASH = re.compile(r"[0-9a-fA-F]{64}")
_INSTALLER = re.compile(r"Suseoro-Setup-(\d+\.\d+\.\d+)\.exe")


class UpdateError(ValueError):
    """Safe, Korean explanation suitable for showing in the application."""


class UpdateCancelled(UpdateError):
    """A download was cancelled before it became an installable update."""


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
        return parsed.path.startswith(f"/{REPOSITORY}/releases/download/")
    return redirect and parsed.hostname in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class _TrustedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted_url(newurl, redirect=True):
            raise UpdateError("공식 배포 주소가 아닌 곳으로 연결되어 다운로드를 중단했습니다.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url: str, timeout: float):
    if not _trusted_url(url):
        raise UpdateError("공식 업데이트 주소가 아닙니다.")
    return build_opener(_TrustedRedirect()).open(Request(url, headers={
        "User-Agent": f"Suseoro/{VERSION}", "Accept": "application/vnd.github+json" if url == LATEST_API else "application/octet-stream",
        "X-GitHub-Api-Version": "2022-11-28",
    }), timeout=timeout)


def _get_json(url: str) -> Any:
    with _open_url(url, 5) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise UpdateError("업데이트 정보가 너무 큽니다.")
    return json.loads(raw.decode("utf-8"))


def check_update(current_version: str = VERSION) -> dict[str, Any]:
    result: dict[str, Any] = {"current_version": current_version, "latest_version": None,
                              "available": False, "url": RELEASES_URL,
                              "message": "현재 최신 버전을 사용하고 있습니다.", "asset": None}
    try:
        current = _version(current_version)
        release = _get_json(LATEST_API)
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
    except Exception:
        result["error"] = True
        result["message"] = "업데이트 정보를 확인하지 못했습니다. 인터넷 연결을 확인하고 다시 시도해 주세요."
        return result


def _expected_hash(asset: dict[str, Any]) -> str:
    digest = asset.get("digest", "")
    if isinstance(digest, str) and digest.startswith("sha256:") and _HASH.fullmatch(digest[7:]):
        return digest[7:].lower()
    sidecar = asset.get("checksum_asset")
    if isinstance(sidecar, dict):
        with _open_url(sidecar["browser_download_url"], 10) as response:
            raw = response.read(65_537)
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
    except UpdateError:
        raise
    except Exception as error:
        raise UpdateError("업데이트 파일을 내려받지 못했습니다. 잠시 후 다시 시도해 주세요.") from None
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
