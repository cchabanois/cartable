"""Build dist/cartable-<version>.ankiaddon: the Anki add-on with the Cartable server inside.

Usage: .venv/bin/python tools/build_addon.py
The add-on installs the server's dependencies with uv on first start (see
anki_addon/launcher.py); only source files are packaged here.
"""

import json
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
OUT = ROOT / "dist" / f"cartable-{VERSION}.ankiaddon"
SKIP = {"__pycache__", "user_files", "meta.json"}  # meta.json: the user's own config, written by Anki


def files(folder: Path):
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not SKIP.intersection(path.relative_to(folder).parts) and path.suffix != ".pyc":
            yield path


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        # Anki expects the add-on files at the root of the archive.
        for path in files(ROOT / "anki_addon"):
            if path.name == "manifest.json":  # Anki shows human_version in Tools → Add-ons
                manifest = json.loads(path.read_text(encoding="utf-8")) | {"human_version": VERSION}
                z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=4) + "\n")
            else:
                z.write(path, path.relative_to(ROOT / "anki_addon"))
        for folder in ("app", "static"):
            for path in files(ROOT / folder):
                z.write(path, Path("server") / path.relative_to(ROOT))
        for name in ("requirements.txt", "pyproject.toml"):  # pyproject.toml: the version
            z.write(ROOT / name, f"server/{name}")
    print(f"{OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
