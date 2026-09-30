"""Start and stop the Cartable server as a separate process, from Anki.

Two layouts:
- packaged add-on: the server code ships in <add-on>/server/, its Python
  dependencies are installed with uv into <add-on>/user_files/venv (kept
  across add-on updates), data goes to <add-on>/user_files/data;
- development: the add-on folder is a symlink to anki_addon/ in the Cartable
  repository; the repository itself and its .venv are used, with its data/.

Everything can be overridden in the add-on config (Tools → Add-ons → Config).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parent
USER_FILES = Path(__file__).parent / "user_files"  # not resolved: stays in addons21 in dev too


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


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def find_uv() -> str | None:
    """Anki's own uv (official builds with the launcher), else one on PATH."""
    try:
        from aqt.package import uv_binary  # present on official builds only

        path = uv_binary()
        if path and Path(path).exists():
            return str(path)
    except Exception:
        pass
    return shutil.which("uv")


def ensure_venv(source: Path, log) -> Path:
    """Create or update user_files/venv from requirements.txt. Slow the first time."""
    requirements = source / "requirements.txt"
    venv = USER_FILES / "venv"
    stamp = venv / ".cartable-requirements"
    wanted = hashlib.sha256(requirements.read_bytes()).hexdigest()
    python = _venv_python(venv)
    if python.exists() and stamp.exists() and stamp.read_text() == wanted:
        return python

    uv = find_uv()
    if uv is None:
        raise LaunchError("errors.addon.uv_missing")
    run = lambda *args: subprocess.run(  # noqa: E731
        [uv, *args], check=True, stdout=log, stderr=subprocess.STDOUT, **_no_window()
    )
    try:
        if not python.exists():
            run("venv", "--python", "3.13", str(venv))
        run("pip", "install", "--python", str(python), "-r", str(requirements))
    except subprocess.CalledProcessError as e:
        raise LaunchError("errors.addon.install_failed") from e
    stamp.write_text(wanted)
    return python


def _no_window() -> dict:
    """On Windows, don't flash a console window for child processes."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


# --- Server process -------------------------------------------------------------


class Server:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.log_path = USER_FILES / "cartable.log"

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, config: dict, bridge_url: str, bridge_key: str, lang: str) -> None:
        """Blocking (venv creation, process start): call it from a background thread."""
        USER_FILES.mkdir(parents=True, exist_ok=True)
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
        }
        # Anki's own Python settings must not leak into the server's interpreter.
        for var in ("PYTHONHOME", "PYTHONPATH"):
            env.pop(var, None)
        command = [
            str(python),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            str(config.get("host", "0.0.0.0")),
            "--port",
            str(config.get("port", 8000)),
        ]
        log.write(f"\n--- starting: {' '.join(command)} (in {lay.source})\n")
        log.flush()
        self.process = subprocess.Popen(
            command, cwd=lay.source, env=env, stdout=log, stderr=subprocess.STDOUT, **_no_window()
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
