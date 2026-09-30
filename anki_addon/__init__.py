"""Cartable inside Anki: runs the Cartable server while Anki is running.

- bridge.py: AnkiConnect-compatible endpoint, so "Add to Anki" writes the
  cards straight into the open profile (no AnkiConnect add-on needed); the
  server keeps running across profile switches;
- launcher.py: starts the server in its own Python environment.

The Cartable server can still run on its own (standalone mode); this add-on is
just another way to launch it.
"""

from __future__ import annotations

import atexit
import json
import socket
import urllib.parse
import urllib.request
import webbrowser

import anki.lang
from aqt import gui_hooks, mw
from aqt.qt import QAction, QApplication, QDialog, QDialogButtonBox, QLabel, QMenu, QPixmap, Qt, QVBoxLayout
from aqt.utils import showInfo, showText, showWarning, tooltip

from .bridge import Bridge
from .launcher import LaunchError, Server, layout

server = Server()
bridge: Bridge | None = None


def config() -> dict:
    return mw.addonManager.getConfig(__name__) or {}


def anki_lang() -> str:
    return anki.lang.current_lang or "en"


def t(key: str, **params) -> str:
    """Translated text from the server's language files (static/i18n), English as fallback."""
    lang = anki_lang().replace("_", "-").lower()
    try:
        folder = layout(config()).source / "static" / "i18n"
    except LaunchError:
        folder = None
    for name in (lang, lang.split("-")[0], "en"):
        path = folder / f"{name}.json" if folder else None
        if path and path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if isinstance(value, str):
                return value.format(**params)
    return key


def lan_address() -> str:
    """This computer's address on the local network, for phones on the same Wi-Fi."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def urls() -> tuple[str, str]:
    port = config().get("port", 8000)
    return f"http://localhost:{port}", f"http://{lan_address()}:{port}"


def start() -> None:
    global bridge
    if server.running():
        return
    stop()
    bridge = Bridge()
    bridge.start()
    url, key = bridge.url, bridge.key

    def on_done(future) -> None:
        try:
            future.result()
        except LaunchError as e:
            showWarning(f"{t('addon.startFailed')}\n{t(str(e))}", title="Cartable")
            return
        except Exception as e:  # unexpected: show it with the log
            showText(f"{t('addon.startFailed')} {e}\n\n{server.log_tail()}", title="Cartable")
            return
        mw.progress.single_shot(2500, check_started)

    # Creating the environment can take a minute the first time: not on the UI thread.
    lang = anki_lang()
    mw.taskman.run_in_background(lambda: server.start(config(), url, key, lang), on_done, uses_collection=False)


def check_started() -> None:
    if server.running():
        if not config().get("phone_help_shown"):
            # First start: show how to open Cartable on the phone, once.
            mw.addonManager.writeConfig(__name__, {**config(), "phone_help_shown": True})
            show_phone()
        else:
            tooltip(t("addon.ready", url=urls()[1]), period=5000)
    else:
        showText(t("addon.stoppedAtStart") + "\n\n" + server.log_tail(), title="Cartable")


def stop() -> None:
    global bridge
    server.stop()
    if bridge:
        bridge.stop()
        bridge = None


def on_main_window_ready() -> None:
    if config().get("autostart", True):
        start()
    # Stop with Anki itself, not with the profile: switching profiles keeps Cartable up.
    QApplication.instance().aboutToQuit.connect(stop)


# --- Tools → Cartable menu -----------------------------------------------------


def open_in_browser() -> None:
    if not server.running():
        start()
    webbrowser.open(urls()[0])


def qr_png(text: str) -> bytes | None:
    """QR code made by the Cartable server (Anki's Python can't install a QR library)."""
    port = config().get("port", 8000)
    url = f"http://127.0.0.1:{port}/api/qr?" + urllib.parse.urlencode({"text": text})
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.read()
    except OSError:
        return None


def show_phone() -> None:
    """How to open Cartable on the phone: a QR code of the address, and three steps."""
    _, lan = urls()
    if not server.running():
        start()
        showInfo(t("addon.notRunningYet"), title="Cartable")
        return
    dialog = QDialog(mw)
    dialog.setWindowTitle(t("addon.phoneTitle"))
    layout_ = QVBoxLayout(dialog)
    png = qr_png(lan)
    if png:
        image = QLabel()
        pixmap = QPixmap()
        pixmap.loadFromData(png)
        image.setPixmap(pixmap)
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout_.addWidget(image)
    address = QLabel(f"<p style='font-size:15px'><b>{lan}</b></p>")
    address.setAlignment(Qt.AlignmentFlag.AlignCenter)
    address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout_.addWidget(address)
    steps = QLabel(t("addon.phoneSteps"))
    steps.setWordWrap(True)
    layout_.addWidget(steps)
    if str(config().get("host", "0.0.0.0")).startswith("127."):
        warning = QLabel(t("addon.localOnly"))
        warning.setWordWrap(True)
        layout_.addWidget(warning)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    buttons.accepted.connect(dialog.accept)
    layout_.addWidget(buttons)
    dialog.exec()


def open_settings() -> None:
    """Settings open here, on the computer: in the add-on they are refused from phones."""
    if not server.running():
        start()
    webbrowser.open(urls()[0] + "/admin.html")


def show_address() -> None:
    local, lan = urls()
    state = t("addon.running") if server.running() else t("addon.stopped")
    try:
        lay = layout(config())
        where = t("addon.where", source=lay.source, data=lay.data) + (t("addon.dev") if lay.dev else "")
    except LaunchError as e:
        where = t(str(e))
    showInfo(t("addon.address", state=state, lan=lan, local=local) + "\n\n" + where, title="Cartable")


def restart() -> None:
    stop()
    start()
    tooltip(t("addon.restarting"))


def show_log() -> None:
    showText(server.log_tail(200) or t("addon.emptyLog"), title=t("addon.logTitle"))


def setup_menu() -> None:
    menu = QMenu("Cartable", mw)
    for key, handler in [
        ("addon.menuOpen", open_in_browser),
        ("addon.menuPhone", show_phone),
        ("addon.menuSettings", open_settings),
        ("addon.menuAddress", show_address),
        ("addon.menuRestart", restart),
        ("addon.menuLog", show_log),
    ]:
        action = QAction(t(key), mw)
        action.triggered.connect(handler)
        menu.addAction(action)
    mw.form.menuTools.addMenu(menu)


setup_menu()
gui_hooks.main_window_did_init.append(on_main_window_ready)
atexit.register(server.stop)  # last resort if Anki exits without aboutToQuit
