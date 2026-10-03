**Notosaurus** — add-on settings

- `autostart`: start the Notosaurus server when Anki starts (it keeps running when you switch profiles).
- `host`: `0.0.0.0` to reach it from the phone (same Wi-Fi), `127.0.0.1` for this computer only.
- `port`: server port (8000 by default). It must be free: stop a standalone Notosaurus already using it.
- `phone_help_shown`: the "Open on the phone" window was already shown on first start.
- `source`, `python`, `data`: leave empty. For development: path of the Notosaurus repository, its Python and its data folder.

After a change: *Tools → Notosaurus → Restart the server*.
