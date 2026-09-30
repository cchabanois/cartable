"""The add-on's launcher (plain Python, loaded without Anki)."""

import hashlib
import importlib.util
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("launcher", Path("anki_addon/launcher.py"))
launcher = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = launcher  # needed by @dataclass
spec.loader.exec_module(launcher)


@pytest.mark.parametrize(
    ("system", "machine", "archive"),
    [
        ("Windows", "AMD64", "x86_64-pc-windows-msvc.zip"),
        ("Windows", "ARM64", "aarch64-pc-windows-msvc.zip"),
        ("Darwin", "arm64", "aarch64-apple-darwin.tar.gz"),
        ("Darwin", "x86_64", "x86_64-apple-darwin.tar.gz"),
        ("Linux", "x86_64", "x86_64-unknown-linux-musl.tar.gz"),
        ("Linux", "aarch64", "aarch64-unknown-linux-musl.tar.gz"),
        ("Linux", "armv7l", None),
        ("FreeBSD", "amd64", None),
    ],
)
def test_uv_archive(system, machine, archive):
    assert launcher.uv_archive(system, machine) == archive
    assert archive is None or archive in launcher.UV_SHA256


def _tar_gz(name: str, content: bytes) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as t:
        info = tarfile.TarInfo(f"uv-x86_64-unknown-linux-musl/{name}")
        info.size = len(content)
        t.addfile(info, io.BytesIO(content))
    return out.getvalue()


def _serve(monkeypatch, data: bytes, archive: str, sha: str | None = None):
    monkeypatch.setitem(launcher.UV_SHA256, archive, sha or hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(data))


def test_download_uv_checks_the_sha256(monkeypatch, tmp_path):
    archive = "x86_64-unknown-linux-musl.tar.gz"
    monkeypatch.setattr(launcher, "EXE", "")
    data = _tar_gz("uv", b"#!/bin/sh\necho uv\n")
    _serve(monkeypatch, data, archive)
    path = launcher.download_uv(io.StringIO(), archive, tmp_path)
    assert path == tmp_path / "uv" and path.read_bytes() == b"#!/bin/sh\necho uv\n"
    assert path.stat().st_mode & 0o111  # executable

    # A download that doesn't match the pinned checksum is refused
    (tmp_path / "uv").unlink()
    _serve(monkeypatch, data, archive, sha="0" * 64)
    with pytest.raises(launcher.LaunchError, match="uv_download_failed"):
        launcher.download_uv(io.StringIO(), archive, tmp_path)
    assert not (tmp_path / "uv").exists()


def test_download_uv_windows_zip(monkeypatch, tmp_path):
    archive = "x86_64-pc-windows-msvc.zip"
    monkeypatch.setattr(launcher, "EXE", ".exe")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("uvx.exe", b"not me")
        z.writestr("uv.exe", b"MZ uv")
    _serve(monkeypatch, out.getvalue(), archive)
    assert launcher.download_uv(io.StringIO(), archive, tmp_path).read_bytes() == b"MZ uv"


def test_download_uv_unsupported_or_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "uv_archive", lambda: None)
    with pytest.raises(launcher.LaunchError, match="uv_unsupported"):
        launcher.download_uv(io.StringIO(), destination=tmp_path)

    def offline(url, timeout):
        raise OSError("no network")

    monkeypatch.setattr(launcher.urllib.request, "urlopen", offline)
    with pytest.raises(launcher.LaunchError, match="uv_download_failed"):
        launcher.download_uv(io.StringIO(), "x86_64-unknown-linux-musl.tar.gz", tmp_path)


def test_install_needed(monkeypatch, tmp_path):
    source = tmp_path / "server"
    (source / "app").mkdir(parents=True)
    (source / "app" / "main.py").write_text("")
    (source / "requirements.txt").write_text("fastapi\n")
    monkeypatch.setattr(launcher, "USER_FILES", tmp_path / "user_files")
    config = {"source": str(source), "python": ""}
    assert launcher.install_needed(config)  # first start

    venv = tmp_path / "user_files" / "venv"
    python = launcher._venv_python(venv)
    python.parent.mkdir(parents=True)
    python.write_text("")
    (venv / ".cartable-requirements").write_text(launcher._requirements_hash(source))
    assert not launcher.install_needed(config)  # installed

    (source / "requirements.txt").write_text("fastapi\nnew-package\n")
    assert launcher.install_needed(config)  # an update changed the dependencies

    assert not launcher.install_needed({**config, "python": "/usr/bin/python3"})  # own Python: nothing to install
