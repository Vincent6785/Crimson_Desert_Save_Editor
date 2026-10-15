#!/usr/bin/env python3
"""List / change item quantities in a Crimson Desert save.

Format taken from NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS (save_crypto.py, save_parser.py):
  0x80-byte header -> ChaCha20(nonce @0x1A) -> LZ4 block -> blob; HMAC-SHA256 of the LZ4 data @0x2A.
  The blob starts with a schema (types + fields), followed by a table (TOC) of object blocks.
  Each object is a "compact" element:
    u16 mask_len | mask | u16 type | u8 | u64 0xFFFF.. | u32 payload_off | payload (u32 + present fields)

Usage:
  cd_items.py list SAVE [--all] [--filter TEXT]
  cd_items.py set  SAVE (--id N | --key ITEMKEY [--slot N]) --count N [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import struct
import sys
import time
from pathlib import Path

import lz4.block
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

_BASE_KEY = bytes.fromhex("C41B8E730DF259A637CC04E9B12F9668DA107A853E61F9224DB80AD75C13EF90")[:31]
_PREFIXES = {1: b'^Qgbrm/.#@`zsr]\\@rvfal#"', 2: b"^Pearl--#Abyss__@!!"}

HEADER_SIZE = 0x80
UNCOMP_SIZE_OFF = 0x12
PAYLOAD_SIZE_OFF = 0x16
NONCE_OFF = 0x1A
HMAC_OFF = 0x2A

ITEM_NAMES = Path(__file__).with_name("item_names.json")
BACKUP_ROOT = Path.home() / "crimson_save_backups"
APP_ID = "3321460"
SAVE_SUBDIR = Path("pfx/drive_c/users/steamuser/AppData/Local/Pearl Abyss/CD/save")
STEAM_ROOTS = (".local/share/Steam", ".steam/steam", ".var/app/com.valvesoftware.Steam/.local/share/Steam")


class SaveError(Exception):
    """Unreadable save, unexpected format or refused write."""


def save_roots() -> list[Path]:
    """Save folders: $CD_SAVE_DIR if set, otherwise every known Steam library."""
    if os.environ.get("CD_SAVE_DIR"):
        return [Path(os.environ["CD_SAVE_DIR"]).expanduser()]
    libs = []
    for root in STEAM_ROOTS:
        root = Path.home() / root
        libs.append(root)
        try:
            vdf = (root / "steamapps/libraryfolders.vdf").read_text(errors="replace")
            libs += [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
        except OSError:
            pass
    roots, seen = [], set()
    for lib in libs:
        d = lib / "steamapps/compatdata" / APP_ID / SAVE_SUBDIR
        if d.is_dir() and d.resolve() not in seen:
            seen.add(d.resolve())
            roots.append(d)
    return roots


def find_saves() -> list[Path]:
    return [p for root in save_roots() for p in root.glob("*/slot*/save.save")]


def save_key(version: int) -> bytes:
    material = _PREFIXES[version] + b"PRIVATE_HMAC_SECRET_CHECK"
    return bytes(a ^ b for a, b in zip(_BASE_KEY, material)) + b"\x00"


def chacha20(data: bytes, nonce: bytes, key: bytes) -> bytes:
    return Cipher(algorithms.ChaCha20(key, nonce), mode=None).encryptor().update(data)


# ---------------------------------------------------------------- crypto I/O

def load_save(path: Path) -> tuple[bytes, bytearray]:
    data = path.read_bytes()
    if data[:4] != b"SAVE":
        raise SaveError(f"{path}: missing SAVE signature")
    version = struct.unpack_from("<H", data, 4)[0]
    usize, csize = struct.unpack_from("<II", data, UNCOMP_SIZE_OFF)
    key = save_key(version)
    compressed = chacha20(data[HEADER_SIZE:HEADER_SIZE + csize], data[NONCE_OFF:NONCE_OFF + 16], key)
    if not hmac.compare_digest(hmac.new(key, compressed, hashlib.sha256).digest(), data[HMAC_OFF:HMAC_OFF + 32]):
        raise SaveError(f"{path}: invalid HMAC (unknown format or corrupted file)")
    blob = lz4.block.decompress(compressed, uncompressed_size=usize)
    return data[:HEADER_SIZE], bytearray(blob)


def build_save(header: bytes, blob: bytes) -> bytes:
    version = struct.unpack_from("<H", header, 4)[0]
    key = save_key(version)
    compressed = lz4.block.compress(bytes(blob), store_size=False, mode="high_compression", compression=9)
    nonce = os.urandom(16)
    out = bytearray(header)  # keep the rest of the original header (0x4A-0x7F)
    struct.pack_into("<II", out, UNCOMP_SIZE_OFF, len(blob), len(compressed))
    out[NONCE_OFF:NONCE_OFF + 16] = nonce
    out[HMAC_OFF:HMAC_OFF + 32] = hmac.new(key, compressed, hashlib.sha256).digest()
    return bytes(out) + chacha20(compressed, nonce, key)


# ---------------------------------------------------------------- blob parsing

def _u16(b, o): return struct.unpack_from("<H", b, o)[0]
def _u32(b, o): return struct.unpack_from("<I", b, o)[0]
def _u64(b, o): return struct.unpack_from("<Q", b, o)[0]


def _str(b, o):
    n = _u32(b, o)
    return b[o + 4:o + 4 + n].decode("ascii", "replace"), o + 4 + n


def parse_schema(blob):
    """Return (types, schema_end); types = [(name, [(field, type, kind, size)])]."""
    pos = 0x0E + 4
    count = _u16(blob, pos)
    pos += 2
    name, pos = _str(blob, pos)
    types = []
    for i in range(count):
        nfields = _u16(blob, pos)
        pos += 2
        fields = []
        for _ in range(nfields):
            fname, pos = _str(blob, pos)
            tname, pos = _str(blob, pos)
            fields.append((fname, tname, _u16(blob, pos), _u16(blob, pos + 2)))
            pos += 8
        types.append((name, fields))
        if i != count - 1:
            name, pos = _str(blob, pos)
    return types, pos


def parse_toc(blob, schema_end, types):
    count = _u32(blob, schema_end + 4)
    toc = []
    for i in range(count):
        cls, _, _, off, size = struct.unpack_from("<5I", blob, schema_end + 12 + i * 20)
        toc.append((types[cls][0] if cls < len(types) else f"<{cls}>", off, size))
    return toc


def find_items(blob):
    types, schema_end = parse_schema(blob)
    names = [t[0] for t in types]
    item_type = names.index("ItemSaveData")
    fields = types[item_type][1]
    wanted = ["_saveVersion", "_itemNo", "_itemKey", "_slotNo", "_stackCount"]
    if [f[0] for f in fields[:5]] != wanted:
        raise SaveError(f"Unexpected ItemSaveData schema: {[f[0] for f in fields[:6]]}")
    sizes = [f[3] for f in fields[:5]]  # 4, 8, 4, 2, 8
    toc = parse_toc(blob, schema_end, types)
    type_bytes = struct.pack("<H", item_type)
    sentinel = b"\xff" * 8

    items = []
    pos = blob.find(sentinel, schema_end)
    while pos != -1:
        # header: u16 mask_len | mask | u16 type | u8 | ff*8 | u32 payload_off
        for mlen in range(1, 9):
            start = pos - 3 - mlen - 2
            if start < 0 or _u16(blob, start) != mlen or blob[pos - 3:pos - 1] != type_bytes:
                continue
            payload = pos + 8 + 4
            if _u32(blob, pos + 8) != payload:
                continue
            mask = blob[start + 2:start + 2 + mlen]
            if (mask[0] & 0x1F) != 0x1F:  # the first 5 fields must be present
                continue
            o = payload + 4
            offs = []
            for s in sizes:
                offs.append(o)
                o += s
            key = _u32(blob, offs[2])
            container = next((n for n, off, sz in toc if off <= start < off + sz), "?")
            items.append({
                "id": len(items),
                "header": start,
                "itemNo": _u64(blob, offs[1]),
                "key": key,
                "slot": _u16(blob, offs[3]),
                "stack": _u64(blob, offs[4]),
                "stack_off": offs[4],
                "container": container,
            })
            break
        pos = blob.find(sentinel, pos + 1)
    return items


def load_names():
    try:
        return {i["itemKey"]: i for i in json.loads(ITEM_NAMES.read_text())["items"]}
    except OSError:
        return {}


# ---------------------------------------------------------------- commands

def resolve_save(arg: str) -> Path:
    """Accept a path, or a slot name (slot0, slot100...)."""
    p = Path(arg)
    if p.is_dir():
        p = p / "save.save"
    if not p.exists():
        cands = [s for s in find_saves() if s.parent.name == arg]
        if len(cands) == 1:
            p = cands[0]
    if not p.exists():
        raise SaveError(f"Save not found: {arg}")
    return p


def cmd_list(args):
    path = resolve_save(args.save)
    _, blob = load_save(path)
    names = load_names()
    items = find_items(blob)
    if not args.all:
        items = [i for i in items if i["container"] == "InventorySaveData"]
    print(f"{path}\n")
    print(f"{'#':>4} {'itemKey':>10} {'slot':>4} {'qty':>8} {'max':>8}  {'container':<22} name")
    for it in sorted(items, key=lambda i: (i["container"], i["slot"])):
        info = names.get(it["key"], {})
        name = info.get("name", "?")
        if args.filter and args.filter.lower() not in name.lower() and args.filter != str(it["key"]):
            continue
        print(f"{it['id']:>4} {it['key']:>10} {it['slot']:>4} {it['stack']:>8} {str(info.get('maxStack', '?')):>8}  {it['container']:<22} {name}")
    print(f"\n{len(items)} item(s)")


def cmd_set(args):
    path = resolve_save(args.save)
    header, blob = load_save(path)
    names = load_names()
    items = find_items(blob)
    if args.id is not None:
        matches = [i for i in items if i["id"] == args.id]
    elif args.key is not None:
        matches = [i for i in items
                   if i["key"] == args.key and i["container"] == "InventorySaveData"
                   and (args.slot is None or i["slot"] == args.slot)]
    else:
        sys.exit("Specify --id or --key.")
    if not matches:
        sys.exit("No matching item in the inventory (see the list command).")
    if len(matches) > 1:
        print("Several stacks match, specify --id:")
        for m in matches:
            print(f"  #{m['id']} slot {m['slot']}: {m['stack']}")
        sys.exit(1)
    it = matches[0]
    info = names.get(it["key"], {})
    max_stack = info.get("maxStack")
    if args.count < 1:
        sys.exit("The quantity must be >= 1.")
    if max_stack and args.count > max_stack and not args.force:
        sys.exit(f"{args.count} exceeds the max stack ({max_stack}) of \"{info.get('name')}\". Use --force to override.")

    struct.pack_into("<Q", blob, it["stack_off"], args.count)
    backup = write_save(path, header, blob)
    print(f"\"{info.get('name', it['key'])}\" slot {it['slot']}: {it['stack']} -> {args.count}")
    print(f"Original save backed up to: {backup}")


def write_save(path: Path, header: bytes, blob: bytes) -> Path:
    """Encrypt and write blob to path after verification; return the path of the original's backup copy."""
    tmp = path.with_suffix(".save.tmp")
    tmp.write_bytes(build_save(header, blob))
    # verification: read back what we just produced
    _, check = load_save(tmp)
    if check != blob:
        tmp.unlink()
        raise SaveError("Verification failed, original file left untouched.")

    backup = BACKUP_ROOT / time.strftime("%Y-%m-%d_%H-%M-%S") / path.parent.name / path.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    os.replace(tmp, path)
    return backup


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="list items")
    p.add_argument("save", help="path to save.save, slot folder, or slot name (e.g. slot100)")
    p.add_argument("--all", action="store_true", help="include equipment, shops, etc.")
    p.add_argument("--filter", help="filter by name or itemKey")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("set", help="change the quantity of an inventory item")
    p.add_argument("save")
    p.add_argument("--id", type=int, help="# identifier (list column)")
    p.add_argument("--key", type=int, help="itemKey (list column)")
    p.add_argument("--slot", type=int, help="slot, if the item has several stacks")
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--force", action="store_true", help="ignore the stack limit")
    p.set_defaults(func=cmd_set)
    args = ap.parse_args()
    try:
        args.func(args)
    except SaveError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
