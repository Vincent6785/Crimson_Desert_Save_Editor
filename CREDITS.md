# Credits

## Upstream project

This editor is a pure-Python reimplementation built on the work of
[CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS](https://github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS)
(MPL-2.0). No code from that project is included, but the following comes from it:

- the save format: encryption key derivation, header layout, schema and compact-element structure;
- the "clone + swap" method used to add items;
- `items_db.json` (item names, categories, max stacks, native inventories, max enchant levels) and
  `knowledge_names.json` (skill / knowledge names), derived from its data files
  (`item_names.json`, `knowledge_keys_all.json`, `max_enchant_map.json`).

Its own credits, reproduced from its
[CREDITS.md](https://github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS/blob/main/CrimsonGameMods/CREDITS.md):

### Author & Maintainer

- **Rice (RicePaddySoftware / NattKh)** — editor development, internationalization framework, save-format work,
  releases. Support their work: [Patreon](https://www.patreon.com/cw/RicePaddyCheater) ·
  [Buy Me a Coffee](https://buymeacoffee.com/sungoku)

### Core Contributors

- **gek** — original Qt desktop save editor; the first save editor was built on this foundation.
- **potter4208467** — crimson_rs Rust parser toolkit: PAZ packing, iteminfo parser, PackGroupBuilder, integrity
  algorithms.
- **LukeFZ** — [pycrimson](https://github.com/LukeFZ/pycrimson) Python utilities: PABGB index reader, archive helpers.
- **fire** — 3.2.0 modular GUI refactor, socket fill/clear engine, ongoing co-development.

### Code incorporated upstream

- Community PAZ archive tooling, community CMOD/ASI loader, and a vendor-patch JSON format compatible with
  **Pldada**'s Korean community tooling.

### Other

- **Dameon** — standalone Crimson Desert Teleporter (separate project).

## This project

### Libraries

| Library | Use | License |
|---|---|---|
| [python-lz4](https://github.com/python-lz4/python-lz4) | LZ4 compression of the save | BSD-3-Clause |
| [cryptography](https://github.com/pyca/cryptography) | ChaCha20 encryption of the save | Apache-2.0 / BSD |
| [Sun Valley ttk theme](https://github.com/rdbende/Sun-Valley-ttk-theme) (`sv-ttk`) | Light / dark GUI theme | MIT |
| Tkinter (Python standard library) | GUI | PSF |

### Tools

- **Claude Code (Anthropic)** — AI coding assistant used during development.

## Game

**Crimson Desert** © Pearl Abyss. This is an unofficial, non-commercial tool that edits the user's own local save
files. It is not affiliated with or endorsed by Pearl Abyss. No game binaries or assets are included; the JSON tables
only contain item and skill names and properties needed to display and validate edits.
