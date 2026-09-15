import hashlib
from io import BytesIO

import pytest


def updater():
    from suseoro.simple import updating
    return updating


def release(version="2.1.0", *, data=b"MZinstaller", digest=True):
    name = f"Suseoro-Setup-{version}.exe"
    asset = {"name": name, "size": len(data),
             "browser_download_url": f"https://github.com/buildergarlic/suseoro/releases/download/v{version}/{name}"}
    if digest:
        asset["digest"] = "sha256:" + hashlib.sha256(data).hexdigest()
    return {"tag_name": "v" + version, "draft": False, "prerelease": False,
            "html_url": f"https://github.com/buildergarlic/suseoro/releases/tag/v{version}", "assets": [asset]}


def test_stable_new_version_requires_exact_windows_asset(monkeypatch):
    monkeypatch.setattr(updater(), "_get_json", lambda url: release())
    result = updater().check_update("2.0.0")
    assert result["available"] is True
    assert result["latest_version"] == "2.1.0"
    assert result["asset"]["name"] == "Suseoro-Setup-2.1.0.exe"


@pytest.mark.parametrize("version", ["2.0.0", "1.99.0"])
def test_current_or_older_release_not_offered(monkeypatch, version):
    monkeypatch.setattr(updater(), "_get_json", lambda url: release(version))
    assert updater().check_update("2.0.0")["available"] is False


@pytest.mark.parametrize("change", [{"prerelease": True}, {"draft": True}, {"tag_name": "v3.0.0-rc1"}, {"assets": []}])
def test_unsafe_or_incomplete_release_not_offered(monkeypatch, change):
    monkeypatch.setattr(updater(), "_get_json", lambda url: {**release(), **change})
    assert updater().check_update()["available"] is False


def test_metadata_failure_returns_korean_message(monkeypatch):
    def fail(url):
        raise TimeoutError("https://example.com/private")
    monkeypatch.setattr(updater(), "_get_json", fail)
    result = updater().check_update()
    assert result["available"] is False
    assert "private" not in str(result)


def test_download_hash_verified_before_final_file(monkeypatch, tmp_path):
    data = b"MZinstaller"
    monkeypatch.setattr(updater(), "_get_json", lambda url: release(data=data))
    monkeypatch.setattr(updater(), "_open_url", lambda url, timeout: BytesIO(data))
    result = updater().check_update()
    path = updater().download_update(result, tmp_path)
    assert path.name == "Suseoro-Setup-2.1.0.exe"
    assert path.read_bytes() == data
    assert updater().verify_download(path, tmp_path)
    path.write_bytes(b"tampered")
    with pytest.raises(updater().UpdateError):
        updater().verify_download(path, tmp_path)


def test_hash_mismatch_leaves_no_executable_or_partial_file(monkeypatch, tmp_path):
    monkeypatch.setattr(updater(), "_get_json", lambda url: release())
    monkeypatch.setattr(updater(), "_open_url", lambda url, timeout: BytesIO(b"MZincorrect"))
    with pytest.raises(updater().UpdateError):
        updater().download_update(updater().check_update(), tmp_path)
    assert not list(tmp_path.rglob("*.exe"))
    assert not list(tmp_path.rglob("*.part"))


def test_official_sidecar_used_when_release_digest_missing(monkeypatch, tmp_path):
    data = b"MZinstaller"
    metadata = release(data=data, digest=False)
    asset = metadata["assets"][0]
    sidecar = {"name": asset["name"] + ".sha256", "size": 160,
               "browser_download_url": asset["browser_download_url"] + ".sha256"}
    metadata["assets"].append(sidecar)
    monkeypatch.setattr(updater(), "_get_json", lambda url: metadata)
    def open_url(url, timeout):
        if url.endswith(".sha256"):
            return BytesIO((hashlib.sha256(data).hexdigest() + "  " + asset["name"]).encode())
        return BytesIO(data)
    monkeypatch.setattr(updater(), "_open_url", open_url)
    assert updater().download_update(updater().check_update(), tmp_path).read_bytes() == data


def test_download_refuses_missing_checksum(monkeypatch, tmp_path):
    monkeypatch.setattr(updater(), "_get_json", lambda url: release(digest=False))
    with pytest.raises(updater().UpdateError):
        updater().download_update(updater().check_update(), tmp_path)


def test_forged_download_information_is_rechecked_against_official_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(updater(), "_get_json", lambda url: release())
    forged = {"latest_version": "9.0.0", "available": True,
              "asset": {"browser_download_url": "https://evil.example/payload.exe"}}
    with pytest.raises(updater().UpdateError):
        updater().download_update(forged, tmp_path)


def test_oversized_installer_is_rejected_before_download(monkeypatch, tmp_path):
    metadata = release()
    metadata["assets"][0]["size"] = 2_000_000_000
    monkeypatch.setattr(updater(), "_get_json", lambda url: metadata)
    with pytest.raises(updater().UpdateError):
        updater().download_update(updater().check_update(), tmp_path)


def test_url_and_verify_paths_are_restricted(tmp_path):
    with pytest.raises(updater().UpdateError):
        updater()._open_url("https://evil.example/file", 1)
    with pytest.raises(updater().UpdateError):
        updater().verify_download(tmp_path / "outside.exe", tmp_path)
