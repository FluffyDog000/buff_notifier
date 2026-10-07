# Перенесено из csfloatpricesparcing/src/phases.py, коммит 4a7d61c, без изменений в логике.
# Править там и переносить заново, а не расходиться молча.
"""Doppler phases: one market name on CSFloat, several skins underneath.

Steam and CSFloat sell every Doppler phase under one market_hash_name -
"★ Bayonet | Doppler (Factory New)" - and tell them apart only by the paint
index of the item: Phase 1 is 418, Ruby 415, and so on. A list that names the
phases separately ("★ Bayonet | Doppler Phase 1 (Factory New)") asks CSFloat
for a name it has never heard of and gets nothing back: 178 items read zero
sales for days.

So a phase name is turned into what CSFloat understands - the real name plus
`paint_index` - wherever a request is built, and whatever comes back is kept
only if its paint index is the phase's.
"""
from __future__ import annotations

import re
from urllib.parse import quote

# Paint indices by finish and phase. Knives share them; the Glock-18's Gamma
# Doppler has its own.
KNIFE_DOPPLER = {"Ruby": 415, "Sapphire": 416, "Black Pearl": 417,
                 "Phase 1": 418, "Phase 2": 419, "Phase 3": 420, "Phase 4": 421}
KNIFE_GAMMA = {"Emerald": 568, "Phase 1": 569, "Phase 2": 570,
               "Phase 3": 571, "Phase 4": 572}
GLOCK_GAMMA = {"Emerald": 1119, "Phase 1": 1120, "Phase 2": 1121,
               "Phase 3": 1122, "Phase 4": 1123}

_PHASE = r"(?P<ph>Phase [1-4]|Ruby|Sapphire|Black Pearl|Emerald)"
_FINISH = r"(?P<fin>Gamma Doppler|Doppler)"
# "… | Doppler Phase 1 (Factory New)" and "… | Doppler (Factory New) - Phase 1"
_BEFORE = re.compile(rf"^(?P<pre>.*\| ){_FINISH} {_PHASE} (?P<wear>\([^)]*\))$")
_AFTER = re.compile(rf"^(?P<pre>.*\| ){_FINISH} (?P<wear>\([^)]*\))\s*-?\s*{_PHASE}$")


def split(name: str) -> tuple[str, int | None]:
    """(the market name CSFloat knows, the phase's paint index or None)."""
    text = (name or "").strip()
    m = _BEFORE.match(text) or _AFTER.match(text)
    if not m:
        return text, None
    fin, ph = m.group("fin"), m.group("ph")
    if fin == "Doppler":
        table = KNIFE_DOPPLER
    else:
        table = GLOCK_GAMMA if "Glock-18" in m.group("pre") else KNIFE_GAMMA
    index = table.get(ph)
    if index is None:
        return text, None
    return f"{m.group('pre')}{fin} {m.group('wear')}", index


def is_phase(name: str) -> bool:
    return split(name)[1] is not None


def query(name: str) -> str:
    """`market_hash_name=…` for a listings request, with the phase's paint
    index when the name carries one."""
    base, index = split(name)
    out = f"market_hash_name={quote(base, safe='')}"
    if index is not None:
        out += f"&paint_index={index}"
    return out


def keep(name: str, records: list, index_of=lambda r: getattr(r, "paint_index", None)) -> list:
    """Only the records of this phase. Unchanged for a name without one."""
    _, index = split(name)
    if index is None:
        return records
    return [r for r in records if index_of(r) == index]
