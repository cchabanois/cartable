"""Install the packaged add-on as Anki would, outside Anki, and start its server.

Usage: python tools/check_addon_install.py
Builds dist/notosaurus-<version>.ankiaddon, unpacks it into a numbered folder
(like an add-on installed from AnkiWeb), then lets the launcher download uv,
install Python and the dependencies, and start the server. Then updates the add-on
the way Anki does while the server runs (Windows refuses to move or delete files in
use). Used by the CI on Windows, macOS and Linux.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_addon  # noqa: E402

PORT = 8791


def load_launcher(addon: Path):
    spec = importlib.util.spec_from_file_location("launcher", addon / "launcher.py")
    launcher = importlib.util.module_from_spec(spec)
    sys.modules["launcher"] = launcher  # needed by @dataclass
    spec.loader.exec_module(launcher)
    return launcher


def wait_for_server(seconds: int = 60) -> dict:
    deadline = time.monotonic() + seconds
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/config", timeout=5) as response:
                return json.load(response)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def update_like_anki(addon: Path, backup: Path) -> None:
    """What Anki does to update an add-on (aqt/addons.py): user_files moved away, the
    add-on's folder deleted, the new version unpacked, user_files moved back."""
    os.rename(addon / "user_files", backup)
    shutil.rmtree(addon)
    with zipfile.ZipFile(build_addon.OUT) as z:
        z.extractall(addon)
    os.rename(backup, addon / "user_files")


def main() -> None:
    build_addon.main()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # Windows: files may still be locked
        addon = Path(tmp) / "addons21" / "1234567890"
        with zipfile.ZipFile(build_addon.OUT) as z:
            z.extractall(addon)
        launcher = load_launcher(addon)

        # Like on Anki 26.08+ without uv installed: the launcher must download it.
        which = shutil.which
        launcher.shutil.which = lambda name, *args, **kwargs: None if name == "uv" else which(name, *args, **kwargs)

        config = {"host": "127.0.0.1", "port": PORT}
        assert launcher.install_needed(config), "a fresh add-on must need an install"
        server = launcher.Server()
        start = time.monotonic()
        try:
            server.start(config, "http://127.0.0.1:1", "key", "en")
            print(f"installed in {time.monotonic() - start:.0f} s")
            answer = wait_for_server()
            print("server answered:", answer)
            assert answer["version"] == build_addon.VERSION, answer
            assert server.running()
            assert not launcher.install_needed(config), "installed: nothing more to install"
            assert (launcher.RUNTIME / "uv").is_dir(), "uv should have been downloaded"
            assert not (launcher.USER_FILES / "venv").exists(), "Python lives outside the add-on"

            # Updated by Anki while the server runs: nothing in the add-on's folder is in use
            update_like_anki(addon, Path(tmp) / "user_files-backup")
            print("updated while running")
            assert wait_for_server()["version"] == build_addon.VERSION, "the server still answers"
        except BaseException:
            print("--- add-on log ---\n" + server.log_tail(200))
            raise
        finally:
            server.stop()
        assert not server.running()
        print("OK")


if __name__ == "__main__":
    main()
