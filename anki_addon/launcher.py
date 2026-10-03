"""Start and stop the Cartable server as a separate process, from Anki.

Two layouts:
- packaged add-on: the server code ships in <add-on>/server/; uv installs
  Python 3.13 and the server's dependencies into Anki2/cartable-runtime/ (removed
  with the add-on), data goes to <add-on>/user_files/data (kept across updates);
- development: the add-on folder is a symlink to anki_addon/ in the Cartable
  repository; the repository itself and its .venv are used, with its data/.

Everything can be overridden in the add-on config (Tools → Add-ons → Config).
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parent
USER_FILES = Path(__file__).parent / "user_files"  # not resolved: stays in addons21 in dev too
# What the running server keeps open — its Python and libraries, its log, its working
# folder — lives outside the add-on: to update it, Anki moves user_files/ away and
# deletes the add-on's folder, which Windows refuses while a file in it is in use.
# Anki2/cartable-runtime (next to addons21), removed with the add-on.
RUNTIME = Path(__file__).parent.parent.parent / "cartable-runtime"


class LaunchError(Exception):
    """Carries a translation key (see Messages in __init__.py)."""


@dataclass
class Layout:
    source: Path  # folder containing app/ and static/
    python: Path | None  # None: a venv must be created with uv
    data: Path
    dev: bool


def layout(config: dict) -> Layout:
    packaged = ADDON_DIR / "server"
    repo = ADDON_DIR.parent  # the Cartable repository, when the add-on is a symlink
    if config.get("source"):
        source, dev = Path(config["source"]).expanduser(), True
    elif (packaged / "app" / "main.py").is_file():
        source, dev = packaged, False
    elif (repo / "app" / "main.py").is_file():
        source, dev = repo, True
    else:
        raise LaunchError("errors.addon.source_not_found")

    python = Path(config["python"]).expanduser() if config.get("python") else None
    if python is None and dev:
        venv_python = source / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python = venv_python if venv_python.exists() else None

    if config.get("data"):
        data = Path(config["data"]).expanduser()
    else:
        data = source / "data" if dev else USER_FILES / "data"
    return Layout(source, python, data, dev)


# --- Python environment (packaged add-on) -------------------------------------

# uv installs Python and the dependencies. Anki 25.07 to 26.05 ship it
# (aqt.package.uv_binary); otherwise this pinned release is downloaded from
# GitHub, and checked against these SHA-256 (from the release's .sha256 files):
# a download that doesn't match is refused.
UV_VERSION = "0.12.21"
UV_SHA256 = {
    "x86_64-pc-windows-msvc.zip": "5d223efa0bf00208c3853246af09420419dfbd352536aa6bb8163d6170e23890",
    "aarch64-pc-windows-msvc.zip": "93ed53b94e9cec000cacdfd18ca67bc4cb2b6a5f5ec041edd7f2a3dae365ce79",
    "x86_64-apple-darwin.tar.gz": "2b336763b396ec6afa20c5a8b083538ca7402445b868311979d740a4344c17d8",
    "aarch64-apple-darwin.tar.gz": "b88bda573e566ef9bced66b155fe0408626fbbc053aee1c30ba686f0728c9447",
    # musl builds are static: they run on any Linux distribution
    "x86_64-unknown-linux-musl.tar.gz": "d69d543a55ec9cdf9d3d9f2648b0a161847e3dbddc477e3be6b5813a6d46f639",
    "aarch64-unknown-linux-musl.tar.gz": "67389a674e62adffa5a395d9a3b80688731c4aa7b33a6def3e62d00f7fec821f",
}
UV_URL = "https://github.com/astral-sh/uv/releases/download/{version}/uv-{archive}"
EXE = ".exe" if os.name == "nt" else ""


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _requirements_hash(source: Path) -> str:
    return hashlib.sha256((source / "requirements.txt").read_bytes()).hexdigest()


def install_needed(config: dict) -> bool:
    """True when starting will first download and install Python and the dependencies
    (first start, or new dependencies after an update): the user is asked first."""
    lay = layout(config)
    if lay.python:
        return False
    venv = RUNTIME / "venv"
    stamp = venv / ".cartable-requirements"
    ready = _venv_python(venv).exists() and stamp.exists() and stamp.read_text() == _requirements_hash(lay.source)
    return not ready


def uv_archive(system: str | None = None, machine: str | None = None) -> str | None:
    """Name of the uv release archive for this computer, None if uv has none."""
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    target = {"Windows": "pc-windows-msvc.zip", "Darwin": "apple-darwin.tar.gz", "Linux": "unknown-linux-musl.tar.gz"}
    if arch is None or system not in target:
        return None
    return f"{arch}-{target[system]}"


def download_uv(log, archive: str | None = None, destination: Path | None = None) -> Path:
    """Download the pinned uv into cartable-runtime/uv/, after checking its SHA-256."""
    archive = archive or uv_archive()
    if archive is None:
        raise LaunchError("errors.addon.uv_unsupported")
    destination = destination or RUNTIME / "uv"
    url = UV_URL.format(version=UV_VERSION, archive=archive)
    log.write(f"downloading {url}\n")
    log.flush()
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
    except OSError as e:
        log.write(f"{e}\n")
        raise LaunchError("errors.addon.uv_download_failed") from e
    if hashlib.sha256(data).hexdigest() != UV_SHA256[archive]:
        log.write(f"SHA-256 mismatch for {url}: refused\n")
        raise LaunchError("errors.addon.uv_download_failed")

    name = f"uv{EXE}"
    if archive.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(m for m in z.namelist() if m.rsplit("/", 1)[-1] == name)
            binary = z.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = next(m for m in t.getmembers() if m.isfile() and m.name.rsplit("/", 1)[-1] == name)
            binary = t.extractfile(member).read()
    destination.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=destination)
    with os.fdopen(fd, "wb") as f:
        f.write(binary)
    os.chmod(tmp, 0o755)
    path = destination / name
    os.replace(tmp, path)
    return path


def find_uv(log) -> str:
    """Anki's own uv (25.07 to 26.05), else one downloaded before, else one on PATH,
    else download it."""
    try:
        from aqt.package import uv_binary

        path = uv_binary()
        if path and Path(path).exists():
            return str(path)
    except Exception:
        pass
    downloaded = RUNTIME / "uv" / f"uv{EXE}"
    if downloaded.exists():
        return str(downloaded)
    return shutil.which("uv") or str(download_uv(log))


def ensure_venv(source: Path, log) -> Path:
    """Create or update cartable-runtime/venv from requirements.txt. Slow the first time."""
    venv = RUNTIME / "venv"
    stamp = venv / ".cartable-requirements"
    wanted = _requirements_hash(source)
    python = _venv_python(venv)
    if python.exists() and stamp.exists() and stamp.read_text() == wanted:
        return python

    uv = find_uv(log)
    env = {
        **os.environ,
        # Everything stays in cartable-runtime: removed with the add-on, never touches the
        # system's Python (a distribution upgrade can't break the environment).
        "UV_PYTHON_INSTALL_DIR": str(RUNTIME / "python"),
        "UV_NO_CONFIG": "1",  # ignore the user's own uv settings
    }

    def run(*args):
        subprocess.run([uv, *args], check=True, stdout=log, stderr=subprocess.STDOUT, env=env, **_no_window())

    try:
        if not python.exists():
            run("venv", "--clear", "--managed-python", "--python", "3.13", str(venv))
        run("pip", "install", "--no-cache", "--python", str(python), "-r", str(source / "requirements.txt"))
    except (OSError, subprocess.CalledProcessError) as e:
        raise LaunchError("errors.addon.install_failed") from e
    stamp.write_text(wanted)
    return python


def _remove_old_runtime() -> None:
    """Before cartable-runtime, Python, uv and the log were in user_files/: no longer used."""
    for name in ("venv", "python", "uv"):
        shutil.rmtree(USER_FILES / name, ignore_errors=True)
    (USER_FILES / "cartable.log").unlink(missing_ok=True)


def remove_runtime() -> None:
    """The add-on is being deleted: its Python, libraries and log go too (the server
    must be stopped first, or Windows keeps them)."""
    shutil.rmtree(RUNTIME, ignore_errors=True)


def _no_window() -> dict:
    """On Windows, don't flash a console window for child processes."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


# --- Server process -------------------------------------------------------------


class Server:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.log_path = RUNTIME / "cartable.log"

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, config: dict, bridge_url: str, bridge_key: str, lang: str) -> None:
        """Blocking (venv creation, process start): call it from a background thread."""
        RUNTIME.mkdir(parents=True, exist_ok=True)
        _remove_old_runtime()
        lay = layout(config)
        lay.data.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "a", encoding="utf-8")  # noqa: SIM115 (kept open: the server writes to it)
        python = lay.python or ensure_venv(lay.source, log)
        env = {
            **os.environ,
            "CARTABLE_DATA": str(lay.data),
            "CARTABLE_EMBEDDED": "1",
            "CARTABLE_LANG": lang,  # the page uses Anki's language (English if not translated)
            "CARTABLE_ANKICONNECT_URL": bridge_url,
            "CARTABLE_ANKICONNECT_KEY": bridge_key,
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",  # no __pycache__ left in the add-on's folder
        }
        # Anki's own Python settings must not leak into the server's interpreter.
        for var in ("PYTHONHOME", "PYTHONPATH"):
            env.pop(var, None)
        command = [
            str(python),
            "-m",
            "uvicorn",
            "--app-dir",  # the code, from the add-on; the working folder stays outside it
            str(lay.source),
            "app.main:app",
            "--host",
            str(config.get("host", "0.0.0.0")),
            "--port",
            str(config.get("port", 8000)),
        ]
        log.write(f"\n--- starting: {' '.join(command)}\n")
        log.flush()
        self.process = subprocess.Popen(
            command, cwd=RUNTIME, env=env, stdout=log, stderr=subprocess.STDOUT, **_no_window()
        )

    def stop(self) -> None:
        if self.running():
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def log_tail(self, lines: int = 40) -> str:
        try:
            return "".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-lines:])
        except FileNotFoundError:
            return ""
