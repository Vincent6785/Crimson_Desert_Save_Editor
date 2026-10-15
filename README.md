# Crimson Desert – Save Editor (Linux)

A **native Linux** save editor for Crimson Desert (Steam / Proton), written in Python with a Tkinter GUI.
No Windows DLL is needed, for reading or for writing.

![Inventory tab (dark theme)](docs/screenshots/main_dark.png)

## Features

- **Inventory**: change the quantity of any item (currency, camp resources, consumables…).
- **Add an item** that is not in the inventory, picked from a list of more than 6,000 items. The editor checks that
  there is free space and puts the item in the right inventory (character bag or camp resources).
- **Delete an item** from the inventory.
- **Kliff's skill points**: set the number of available points. You then spend them in the game's Skills menu, which
  still enforces prerequisites and costs.
- Fill up a stack of **Abyss Artifact**.
- Light and dark themes, HiDPI-aware interface.
- **Automatic backup** of the save before every write, in `~/crimson_save_backups/<date>/<slot>/`.

Tested with game version **2.03.02**. Another version may change the format.

## Screenshots

| Add an item | Kliff's skill points |
|---|---|
| ![Add item dialog](docs/screenshots/add_dark.png) | ![Skills tab](docs/screenshots/skills_light.png) |

Light theme, with pending changes highlighted before saving:

![Inventory tab (light theme)](docs/screenshots/main_light.png)

## Installation

Requirements: Python 3.10 or newer, with Tkinter (`tk` on Arch, `python3-tk` on Debian/Ubuntu).

```bash
git clone https://github.com/Vincent6785/Crimson_Desert_Save_Editor.git
cd Crimson_Desert_Save_Editor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh
```

Saves are looked up in the game's Proton prefix, in every declared Steam library (`libraryfolders.vdf`), for both
native and Flatpak Steam:
`<library>/steamapps/compatdata/3321460/pfx/drive_c/users/steamuser/AppData/Local/Pearl Abyss/CD/save/`.
If yours are elsewhere, point the `CD_SAVE_DIR` variable at the `save` folder:

```bash
CD_SAVE_DIR=/path/to/CD/save ./run.sh
```

A command-line interface is also available:

```bash
.venv/bin/python cd_items.py list  PATH/save.save --filter silver
.venv/bin/python cd_items.py set   PATH/save.save --id N --count 50000   # N = "#" column from list
```

## ⚠️ Precautions

- **Close the game** before saving: otherwise it will overwrite the save with its own version.
- **Disable Steam Cloud** for the game, or pick the **local** version if Steam reports a conflict.
- Try it on a secondary slot first. After an edit, load the game and let it save once before editing again.
- **Avoid mass additions.** Adding dozens of copies of a non-stackable item, or absurd quantities, has already made
  saves impossible to load. "Sealed Abyss Artifact" items are blocked for this reason.
- To restore a save, copy `~/crimson_save_backups/<date>/<slot>/save.save` back in place.

**Single-player** only. Unofficial tool, not affiliated with Pearl Abyss. Use at your own risk.

## Files

| File | Purpose |
|---|---|
| `cd_items.py` | Save encryption and decryption (ChaCha20 + LZ4 + HMAC), `list` / `set` CLI |
| `cd_inventory.py` | Structure decoding, adding and removing items (with offset and block-table updates) |
| `cd_skills.py` | Reading and changing skill points |
| `cd_items_gui.py` | Graphical interface |
| `items_db.json`, `item_names.json`, `knowledge_names.json` | Item and knowledge names and properties |

## Credits and license

The save format (encryption, schema, structure) and the item and knowledge name files come from the work of
**NattKh**:
[CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS](https://github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS)
(MPL-2.0). The code in this repository is an independent pure-Python reimplementation.

This project is distributed under the **Mozilla Public License 2.0** (see `LICENSE`).
