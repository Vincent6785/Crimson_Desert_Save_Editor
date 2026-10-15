# SPDX-License-Identifier: MPL-2.0
"""Inventory structure and item insertion in a Crimson Desert save.

"Clone + swap" method from NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS
(inventory_editor.cpp / parc_engine.cpp SpliceIntoList): copy an existing item
from the same sub-inventory, change itemKey / itemNo / slot / quantity, insert it at the end
of _itemList, then fix up everything the insertion shifts:
  - the POs (u32 values always equal to "their own position + 4") located after the insertion point,
  - the trailing size of the parent InventoryElementSaveData,
  - the list counter,
  - the TOC (block size, offsets of the following blocks) and the stream_size.

Difference from the editor: elements are decoded recursively using the schema
instead of locating the trailing size by scanning (which yields false positives, e.g. stack=22).
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

import cd_items as c

ITEMS_DB = Path(__file__).with_name("items_db.json")

# Character bag: 50 base slots + expansions, 240 max
# (vanilla inventory.pabgb, see the editor's paz_patcher.py).
BAG = 2
BAG_BASE, BAG_MAX = 50, 240
# Camp resources / contributions: one slot per possible item type.
RESOURCES = 1
INVENTORY_NAMES = {RESOURCES: "Camp resources", BAG: "Character bag"}
# items_db "inventory" value -> sub-inventory they are added to. Inventory 5 items already live
# in the bag in real saves; items of any other inventory are not offered.
ADD_TARGETS = {RESOURCES: RESOURCES, BAG: BAG, 5: BAG}


# Items whose insertion broke a save: never offered nor inserted.
BLOCKED_NAMES = ("Sealed Abyss Artifact",)


def blocked(info: dict) -> bool:
    return any(n in (info.get("name") or "") for n in BLOCKED_NAMES)


def destination(item_key: int, db: dict) -> int | None:
    """Sub-inventory the item is added to, or None if it cannot be added safely.

    Only two sub-inventories have a known capacity: items are only added there."""
    return ADD_TARGETS.get(db.get(item_key, {}).get("inventory", BAG))


class DecodeError(Exception):
    pass


def _u16(b, o): return struct.unpack_from("<H", b, o)[0]
def _u32(b, o): return struct.unpack_from("<I", b, o)[0]
def _u64(b, o): return struct.unpack_from("<Q", b, o)[0]


@dataclass
class Element:
    start: int
    mask: bytes
    type_name: str
    payload: int          # position of the reserved u32; PO = payload
    end: int              # end (after the trailing size)
    fields: dict = field(default_factory=dict)   # name -> (offset, size) for fixed fields
    lists: dict = field(default_factory=dict)    # name -> ListInfo


@dataclass
class ListInfo:
    start: int
    count_off: int
    count_size: int       # 3 (u24 LE) or 4 (u32 LE)
    count: int
    elements: list
    elems_end: int        # end of the last element = insertion point
    end: int


class Decoder:
    def __init__(self, blob: bytes):
        self.b = blob
        self.types, self.schema_end = c.parse_schema(blob)
        self.names = [t[0] for t in self.types]

    def present(self, mask, i):
        return i // 8 < len(mask) and mask[i // 8] >> (i % 8) & 1

    def element(self, s: int, limit: int) -> Element:
        b = self.b
        mbc = _u16(b, s)
        if not 1 <= mbc <= 16:
            raise DecodeError(f"0x{s:X}: mbc={mbc}")
        mask = bytes(b[s + 2:s + 2 + mbc])
        t = _u16(b, s + 2 + mbc)
        if t >= len(self.types):
            raise DecodeError(f"0x{s:X}: type {t}")
        po = s + 2 + mbc + 3 + 8
        if _u32(b, po) != po + 4:
            raise DecodeError(f"0x{s:X}: PO not self-referencing")
        p = po + 4
        el = Element(s, mask, self.names[t], p, 0)
        cur = p + 4
        for i, (fname, _tname, kind, size) in enumerate(self.types[t][1]):
            if not self.present(mask, i):
                if kind in (6, 7):
                    # an absent list still takes up one 0x01 byte
                    if b[cur] != 1:
                        raise DecodeError(f"0x{s:X}: absent-list marker expected for {fname}")
                    cur += 1
                continue
            if kind in (0, 2) and size > 0:
                el.fields[fname] = (cur, size)
                cur += size
            elif kind in (6, 7):
                lst = self.list(cur, limit)
                el.lists[fname] = lst
                cur = lst.end
            elif kind in (4, 5):
                # inline object (or pointer): 0 to 3 prefix bytes followed by a full element
                for d in range(4):
                    try:
                        cur = self.element(cur + d, limit).end
                        break
                    except (DecodeError, struct.error, IndexError):
                        continue
                else:
                    raise DecodeError(f"0x{s:X}: object {fname} not decodable")
            else:
                raise DecodeError(f"0x{s:X}: field {fname} kind={kind} not supported")
        if cur + 4 > limit or _u32(b, cur) != cur - p:
            raise DecodeError(f"0x{s:X}: invalid trailing size")
        el.end = cur + 4
        return el

    def list(self, o: int, limit: int) -> ListInfo:
        b = self.b
        if b[o] != 0:
            raise DecodeError(f"0x{o:X}: list prefix {b[o]} not supported")
        elif b[o + 1] == b[o + 2] == b[o + 3] == 0:
            count_off, count_size, count, cur = o + 4, 4, _u32(b, o + 4), o + 18
        else:
            count_off, count_size = o + 1, 3
            count, cur = b[o + 1] | b[o + 2] << 8 | b[o + 3] << 16, o + 18
        elements = []
        for _ in range(count):
            el = self.element(cur, limit)
            elements.append(el)
            cur = el.end
        return ListInfo(o, count_off, count_size, count, elements, cur, cur)

    def toc(self):
        return c.parse_toc(self.b, self.schema_end, self.types)


@dataclass
class SubInventory:
    key: int
    expand: int
    element: Element
    items: list           # list[Element]

    @property
    def itemlist(self) -> ListInfo:
        return self.element.lists["_itemList"]

    def capacity(self, db: dict | None = None):
        if self.key == BAG:
            return min(BAG_BASE + self.expand, BAG_MAX)
        if self.key == RESOURCES and db:
            return sum(1 for v in db.values() if v.get("inventory") == RESOURCES)
        return None

    def used_slots(self, dec: Decoder) -> set:
        return {_u16(dec.b, it.fields["_slotNo"][0]) for it in self.items if "_slotNo" in it.fields}


def read_inventories(dec: Decoder) -> dict[int, SubInventory]:
    b = dec.b
    toc = dec.toc()
    boff, bsz = next(((o, s) for n, o, s in toc if n == "InventorySaveData"), (None, None))
    if boff is None:
        raise DecodeError("InventorySaveData block missing")
    end = boff + bsz
    mbc = _u16(b, boff)
    mask = b[boff + 2:boff + 2 + mbc]
    if not mask[0] & 1:
        raise DecodeError("_inventorylist missing")
    lst = dec.list(boff + 2 + mbc + 4, end)
    out = {}
    for el in lst.elements:
        if el.type_name != "InventoryElementSaveData":
            raise DecodeError(f"unexpected type {el.type_name}")
        key = _u16(b, el.fields["_inventoryKey"][0]) if "_inventoryKey" in el.fields else None
        expand = _u16(b, el.fields["_varyExpandSlotCount"][0]) if "_varyExpandSlotCount" in el.fields else 0
        items = el.lists["_itemList"].elements if "_itemList" in el.lists else []
        out[key] = SubInventory(key, expand, el, items)
    return out


def read_block(dec: Decoder, type_name: str) -> Element:
    """Decode a top-level block (TOC entry): u16 mbc | mask | reserved u32 | fields.
    No PO and no trailing size: the size comes from the TOC."""
    b = dec.b
    off, size = next(((o, s) for n, o, s in dec.toc() if n == type_name), (None, None))
    if off is None:
        raise DecodeError(f"Block {type_name} missing from the save.")
    end = off + size
    t = dec.names.index(type_name)
    mbc = _u16(b, off)
    mask = bytes(b[off + 2:off + 2 + mbc])
    el = Element(off, mask, type_name, off + 2 + mbc, end)
    cur = off + 2 + mbc + 4
    for i, (fname, _tname, kind, size_) in enumerate(dec.types[t][1]):
        if not dec.present(mask, i):
            if kind in (6, 7):
                if b[cur] != 1:
                    raise DecodeError(f"{type_name}: absent-list marker expected for {fname}")
                cur += 1
            continue
        if kind in (0, 2) and size_ > 0:
            el.fields[fname] = (cur, size_)
            cur += size_
        elif kind in (6, 7):
            lst = dec.list(cur, end)
            el.lists[fname] = lst
            cur = lst.end
        else:
            raise DecodeError(f"{type_name}: field {fname} kind={kind} not supported")
    if cur > end:
        raise DecodeError(f"{type_name}: exceeds the size declared by the TOC")
    return el


def load_items_db() -> dict[int, dict]:
    try:
        return {int(k): v for k, v in json.loads(ITEMS_DB.read_text()).items()}
    except OSError:
        return {}


# ---------------------------------------------------------------- insertion

def _struct_pos(blob: bytes, start: int, ntypes: int) -> list[int]:
    """Positions of POs (u32 == position + 4 preceded by a valid header) from start onwards."""
    out = []
    for q in range(start, len(blob) - 3):
        if _u32(blob, q) != q + 4:
            continue
        if _u16(blob, q - 11) >= ntypes:
            continue
        for mbc in range(1, 17):
            if q - 13 - mbc >= 0 and _u16(blob, q - 13 - mbc) == mbc:
                out.append(q)
                break
    return out


def _splice(blob: bytes, dec: Decoder, at: int, insert: bytes = b"", delete: int = 0,
            trailing: tuple = ()) -> bytearray:
    """Replace blob[at:at+delete] with insert and fix up everything the shift affects:
    POs located after it, trailing sizes (pre-operation positions, all after the zone),
    TOC (size of the block containing at, offsets of the following blocks) and stream_size.
    POs inside insert must already be correct for their final position."""
    end_del = at + delete
    growth = len(insert) - delete
    pos_after = _struct_pos(blob, end_del, len(dec.types))
    out = bytearray(blob[:at]) + bytes(insert) + blob[end_del:]
    for q in pos_after:
        nq = q + growth
        struct.pack_into("<I", out, nq, nq + 4)
    for ts in trailing:
        if ts < end_del:
            raise ValueError("trailing size located before the modified zone")
        nts = ts + growth
        struct.pack_into("<I", out, nts, _u32(out, nts) + growth)
    toc_start = dec.schema_end + 12
    for i in range(_u32(blob, dec.schema_end + 4)):
        e = toc_start + i * 20
        off, size = _u32(out, e + 12), _u32(out, e + 16)
        if off <= at < off + size:
            struct.pack_into("<I", out, e + 16, size + growth)
        elif off >= end_del:
            struct.pack_into("<I", out, e + 12, off + growth)
    struct.pack_into("<I", out, dec.schema_end + 8, len(out))
    return out


def _set_list_count(out: bytearray, lst: ListInfo, n: int):
    if lst.count_size == 4:
        struct.pack_into("<I", out, lst.count_off, n)
    else:
        out[lst.count_off:lst.count_off + 3] = bytes((n & 0xFF, n >> 8 & 0xFF, n >> 16 & 0xFF))


def add_item(blob: bytearray, item_key: int, count: int, inventory_key: int | None = None) -> tuple[bytearray, dict]:
    """Return (new blob, info). Raise DecodeError / ValueError on failure."""
    db = load_items_db()
    info = db.get(item_key, {})
    if inventory_key is None:
        inventory_key = destination(item_key, db)
        if inventory_key is None:
            raise ValueError(f"\"{info.get('name', item_key)}\" belongs to an inventory this editor cannot add to.")
    if blocked(info):
        raise ValueError(f"\"{info.get('name')}\" cannot be added (it has already broken a save).")
    max_stack = info.get("maxStack") or 1
    if not 1 <= count <= max_stack:
        raise ValueError(f"Invalid quantity (1 to {max_stack}).")

    dec = Decoder(blob)
    invs = read_inventories(dec)
    inv = invs.get(inventory_key)
    if inv is None:
        raise ValueError(f"Sub-inventory {inventory_key} missing from the save.")
    if not inv.items:
        raise ValueError(f"Sub-inventory {inventory_key} is empty: no item to clone.")

    # stackable item already present: its quantity must be changed instead of adding a second one
    present = [el for el in inv.items
               if "_itemKey" in el.fields and _u32(blob, el.fields["_itemKey"][0]) == item_key]
    if present and max_stack > 1:
        raise ValueError("Item already present: change its quantity instead.")

    # available space
    cap = inv.capacity(db)
    if cap is None:
        raise ValueError(f"Capacity of sub-inventory {inventory_key} unknown: insertion refused for safety.")
    used = inv.used_slots(dec)
    if len(inv.items) >= cap:
        raise ValueError(f"Inventory full ({len(inv.items)}/{cap}).")
    slot = next(s for s in range(cap) if s not in used)

    # donor: same category if possible, with no pointer or exotic list (sockets only)
    cat = info.get("category")
    def donor_score(el):
        k = _u32(blob, el.fields["_itemKey"][0])
        exotic = "_characterConversionData" in el.fields or any(
            l.elements for n, l in el.lists.items() if n != "_socketSaveDataList")
        return (db.get(k, {}).get("category") != cat, exotic, el.end - el.start)
    candidates = [el for el in inv.items
                  if all(f in el.fields for f in ("_itemNo", "_itemKey", "_slotNo", "_stackCount"))
                  and "_characterConversionData" not in el.fields]
    if not candidates:
        raise ValueError("No clonable item in this sub-inventory.")
    donor = min(candidates, key=donor_score)

    # unique itemNo: max over all ItemSaveData in the save
    # (items placed in the world have itemNo = -1: unassigned)
    max_no = max(i["itemNo"] for i in c.find_items(blob) if i["itemNo"] < 2**63)

    # build the cloned element (relative offsets)
    new = bytearray(blob[donor.start:donor.end])
    rel = lambda name: donor.fields[name][0] - donor.start
    struct.pack_into("<Q", new, rel("_itemNo"), max_no + 1)
    struct.pack_into("<I", new, rel("_itemKey"), item_key)
    struct.pack_into("<H", new, rel("_slotNo"), slot)
    struct.pack_into("<Q", new, rel("_stackCount"), count)
    if "_transferredItemKey" in donor.fields:   # equals the itemKey in the game data
        struct.pack_into("<I", new, rel("_transferredItemKey"), item_key)
    if "_enchantLevel" in donor.fields:
        struct.pack_into("<H", new, rel("_enchantLevel"), 0)
    if "_isLocked" in donor.fields:
        new[rel("_isLocked")] = 0
    if "_isNewMark" in donor.fields:
        new[rel("_isNewMark")] = 1

    insert_at = inv.itemlist.elems_end
    growth = len(new)
    ntypes = len(dec.types)

    # POs inside the clone: rebased onto their final position
    for q in _struct_pos(blob[:donor.end], donor.start, ntypes):
        r = q - donor.start
        struct.pack_into("<I", new, r, insert_at + r + 4)

    # insertion + trailing size of the parent InventoryElementSaveData + list counter
    out = _splice(blob, dec, insert_at, insert=new, trailing=(inv.element.end - 4,))
    _set_list_count(out, inv.itemlist, inv.itemlist.count + 1)

    # verification: decode again and find the item
    dec2 = Decoder(out)
    inv2 = read_inventories(dec2)[inventory_key]
    last = inv2.items[-1]
    if (len(inv2.items) != len(inv.items) + 1
            or _u32(out, last.fields["_itemKey"][0]) != item_key
            or _u64(out, last.fields["_stackCount"][0]) != count):
        raise DecodeError("Post-insertion verification failed.")
    for k, other in invs.items():   # the other sub-inventories must be untouched
        o2 = read_inventories(dec2)[k]
        if k != inventory_key and len(o2.items) != len(other.items):
            raise DecodeError("Verification: another sub-inventory has changed.")

    donor_key = _u32(blob, donor.fields["_itemKey"][0])
    return out, {"slot": slot, "itemNo": max_no + 1, "inventory": inventory_key,
                 "donor": donor_key, "growth": growth}


# ---------------------------------------------------------------- removal

def references(blob: bytes, item_no: int, start: int, end: int) -> int:
    """Number of occurrences of the itemNo (u64) outside the item itself (shortcuts, reservations...)."""
    pat = struct.pack("<Q", item_no)
    n, i = 0, blob.find(pat)
    while i != -1:
        if not start <= i < end:
            n += 1
        i = blob.find(pat, i + 1)
    return n


def remove_item(blob: bytearray, element_start: int) -> tuple[bytearray, dict]:
    """Remove the item whose element starts at element_start. Return (new blob, info)."""
    dec = Decoder(blob)
    invs = read_inventories(dec)
    found = next(((k, inv, el) for k, inv in invs.items() for el in inv.items if el.start == element_start), None)
    if found is None:
        raise ValueError("Item not found in the inventory.")
    inv_key, inv, el = found
    if len(inv.items) == 1:
        # the game never writes an empty list (the field is then absent from the mask): don't create one
        raise ValueError("This is the last item in this sub-inventory: removal refused for safety.")
    shrink = el.end - el.start

    # removal + trailing size of the parent InventoryElementSaveData + list counter
    out = _splice(blob, dec, el.start, delete=shrink, trailing=(inv.element.end - 4,))
    _set_list_count(out, inv.itemlist, inv.itemlist.count - 1)

    # verification: everything decodes again, one item fewer in this sub-inventory only
    invs2 = read_inventories(Decoder(out))
    for k, other in invs.items():
        expected = len(other.items) - (k == inv_key)
        if len(invs2[k].items) != expected:
            raise DecodeError("Post-removal verification failed.")
    item_key = _u32(blob, el.fields["_itemKey"][0]) if "_itemKey" in el.fields else None
    return out, {"inventory": inv_key, "itemKey": item_key, "shrink": shrink}
