"""Unambiguous names for unpainted knives; finishes and StatTrak stay distinct."""
KNIVES = {"Bayonet", "M9 Bayonet", "Karambit", "Bowie Knife", "Butterfly Knife",
          "Falchion Knife", "Flip Knife", "Classic Knife", "Gut Knife", "Huntsman Knife",
          "Kukri Knife", "Navaja Knife", "Nomad Knife", "Paracord Knife", "Skeleton Knife",
          "Stiletto Knife", "Survival Knife", "Talon Knife", "Ursus Knife", "Shadow Daggers"}


def vanilla(name: str) -> bool:
    text = name.strip().removeprefix("★").strip().removeprefix("StatTrak™ ")
    return text in KNIVES


def canonical(name: str) -> str:
    name = name.strip()
    return "★ " + name.removeprefix("★").strip() if vanilla(name) else name


def known_name(name: str, known) -> str:
    if name in known:
        return name
    if vanilla(name):
        matches = [n for n in known if canonical(n) == canonical(name)]
        if len(matches) == 1:
            return matches[0]
    return name
