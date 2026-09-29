**Cartable** — add-on settings

- `autostart`: start the Cartable server when Anki starts (it keeps running when you switch profiles).
- `host`: `0.0.0.0` to reach it from the phone (same Wi-Fi), `127.0.0.1` for this computer only.
- `port`: server port (8000 by default). It must be free: stop a standalone Cartable already using it.
- `phone_help_shown`: the "Open on the phone" window was already shown on first start.
- `source`, `python`, `data`: leave empty. For development: path of the Cartable repository, its Python and its data folder.

After a change: *Tools → Cartable → Restart the server*.
