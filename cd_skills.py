# SPDX-License-Identifier: MPL-2.0
"""Kliff's skill points in a Crimson Desert save.

Skills are "knowledge" entries (KnowledgeSaveData block):
  - _list                    : KnowledgeElementSaveData (_key, _level...); level >= 2 = learned
  - _skillLearnSaveDataList  : SkillLearnElementSaveData (_knowledgeKey, _usedArtifactCount, _skillPointOwnerType)
  - _skillPointSaveDataList  : SkillPointSaveData (_skillPointOwnerType, _currentSkillPoint, _elapsedSkillPoint)

Invariant observed in every save: elapsed = sum(usedArtifactCount) + current
(1 Abyss Artifact absorbed = 1 point; current = points still to spend in the tree).
No skill is unlocked directly: we set the available points and the player
spends them in the in-game menu, which handles prerequisites and costs.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

import cd_inventory as ci

KLIFF = 0                 # _skillPointOwnerType of the main character
U16_MAX = 0xFFFF
KNOWLEDGE_NAMES = Path(__file__).with_name("knowledge_names.json")


@dataclass
class SkillState:
    current: int                  # available points
    elapsed: int                  # total earned
    spent: int                    # sum of _usedArtifactCount
    learned: list = field(default_factory=list)   # [(key, name, level, points spent)]
    others: dict = field(default_factory=dict)    # other owners: type -> (current, elapsed)

    @property
    def consistent(self) -> bool:
        return self.elapsed == self.spent + self.current


def load_knowledge_names() -> dict[int, str]:
    try:
        return {int(k): v for k, v in json.loads(KNOWLEDGE_NAMES.read_text()).items()}
    except OSError:
        return {}


def _val(b, el, name, default=0):
    if name not in el.fields:
        return default
    off, size = el.fields[name]
    return int.from_bytes(b[off:off + size], "little")


def _point_elements(dec: ci.Decoder, block: ci.Element) -> dict[int, ci.Element]:
    lst = block.lists.get("_skillPointSaveDataList")
    if lst is None:
        raise ci.DecodeError("_skillPointSaveDataList missing.")
    return {_val(dec.b, el, "_skillPointOwnerType"): el for el in lst.elements}


def read_skills(blob: bytes, names: dict | None = None) -> SkillState:
    dec = ci.Decoder(blob)
    block = ci.read_block(dec, "KnowledgeSaveData")
    b = dec.b
    points = _point_elements(dec, block)
    if KLIFF not in points:
        raise ci.DecodeError("Kliff's skill points not found.")
    kliff = points[KLIFF]

    levels = {_val(b, el, "_key"): _val(b, el, "_level")
              for el in block.lists["_list"].elements} if "_list" in block.lists else {}
    names = names if names is not None else load_knowledge_names()
    learned, spent = [], 0
    for el in block.lists.get("_skillLearnSaveDataList", ci.ListInfo(0, 0, 3, 0, [], 0, 0)).elements:
        if _val(b, el, "_skillPointOwnerType") != KLIFF:
            continue
        key, used = _val(b, el, "_knowledgeKey"), _val(b, el, "_usedArtifactCount")
        spent += used
        learned.append((key, names.get(key, f"Knowledge {key}"), levels.get(key, 0), used))

    others = {t: (_val(b, el, "_currentSkillPoint"), _val(b, el, "_elapsedSkillPoint"))
              for t, el in points.items() if t != KLIFF}
    return SkillState(_val(b, kliff, "_currentSkillPoint"), _val(b, kliff, "_elapsedSkillPoint"),
                      spent, learned, others)


def _field_index(dec: ci.Decoder, type_name: str, field_name: str) -> int:
    fields = dec.types[dec.names.index(type_name)][1]
    return next(i for i, f in enumerate(fields) if f[0] == field_name)


def set_skill_points(blob: bytes, available: int) -> bytearray:
    """Set Kliff's available points (elapsed follows to keep the invariant)."""
    before = read_skills(blob, names={})
    if not before.consistent:
        raise ValueError(f"Atypical save (earned {before.elapsed} != spent {before.spent} "
                         f"+ available {before.current}): modification refused.")
    if available < 0:
        raise ValueError("The number of points cannot be negative.")
    if before.spent + available > U16_MAX:
        raise ValueError(f"Maximum: {U16_MAX - before.spent} available points.")

    dec = ci.Decoder(blob)
    block = ci.read_block(dec, "KnowledgeSaveData")
    el = _point_elements(dec, block)[KLIFF]
    out = bytearray(blob)

    if "_currentSkillPoint" in el.fields:
        struct.pack_into("<H", out, el.fields["_currentSkillPoint"][0], available)
    elif available:
        # field absent: set the mask bit + insert the 2 bytes in its place (after _skillPointOwnerType)
        idx = _field_index(dec, "SkillPointSaveData", "_currentSkillPoint")
        mbc = struct.unpack_from("<H", out, el.start)[0]
        if idx // 8 >= mbc:
            raise ci.DecodeError("Mask too short to enable _currentSkillPoint.")
        if "_skillPointOwnerType" not in el.fields:
            raise ci.DecodeError("_skillPointOwnerType missing: insertion position unknown.")
        out[el.start + 2 + idx // 8] |= 1 << (idx % 8)
        off, size = el.fields["_skillPointOwnerType"]
        out = ci._splice(out, dec, off + size, insert=struct.pack("<H", available), trailing=(el.end - 4,))

    # elapsed = spent + available (decode again: offsets may have moved)
    dec2 = ci.Decoder(out)
    el2 = _point_elements(dec2, ci.read_block(dec2, "KnowledgeSaveData"))[KLIFF]
    if "_elapsedSkillPoint" not in el2.fields:
        raise ci.DecodeError("_elapsedSkillPoint missing: modification not supported.")
    struct.pack_into("<H", out, el2.fields["_elapsedSkillPoint"][0], before.spent + available)

    # verification
    after = read_skills(out, names={})
    if (after.current != available or not after.consistent or after.spent != before.spent
            or after.others != before.others or len(after.learned) != len(before.learned)):
        raise ci.DecodeError("Verification after changing the points failed.")
    ci.read_inventories(ci.Decoder(out))  # the rest of the save must still decode
    return out
