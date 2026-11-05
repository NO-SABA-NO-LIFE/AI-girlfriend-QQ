"""Shared visual tokens for the Helldivers intelligence renderers."""

from __future__ import annotations

from typing import Final

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]

# Surfaces mirror Discord/GWW's neutral charcoal rather than a generic navy UI.
BACKGROUND: Final[RGB] = (17, 18, 22)
BACKGROUND_DEEP: Final[RGB] = (8, 10, 14)
SURFACE: Final[RGB] = (34, 35, 41)
SURFACE_RAISED: Final[RGB] = (40, 42, 49)
SURFACE_INSET: Final[RGB] = (25, 27, 33)
BORDER: Final[RGB] = (52, 55, 64)
BORDER_STRONG: Final[RGB] = (80, 85, 97)
TRACK: Final[RGB] = (76, 77, 78)

TEXT: Final[RGB] = (242, 243, 246)
TEXT_SECONDARY: Final[RGB] = (187, 190, 199)
TEXT_MUTED: Final[RGB] = (139, 144, 157)
TEXT_DIM: Final[RGB] = (98, 105, 119)

SUPER_EARTH: Final[RGB] = (93, 190, 239)
DSS: Final[RGB] = (220, 239, 250)
TERMINIDS: Final[RGB] = (255, 190, 11)
AUTOMATON: Final[RGB] = (255, 97, 97)
ILLUMINATE: Final[RGB] = (194, 121, 226)
MAJOR_ORDER: Final[RGB] = (255, 218, 0)
SUCCESS: Final[RGB] = (84, 205, 132)
DANGER: Final[RGB] = (255, 102, 109)
WARNING: Final[RGB] = (255, 190, 52)

FACTION_COLORS: Final[dict[str, RGB]] = {
    "Humans": SUPER_EARTH,
    "Super Earth": SUPER_EARTH,
    "Terminids": TERMINIDS,
    "Automaton": AUTOMATON,
    "Illuminate": ILLUMINATE,
    "Unknown": TEXT_MUTED,
}

# 4/8 based spacing rhythm for dense image dashboards.
SPACE_1: Final[int] = 4
SPACE_2: Final[int] = 8
SPACE_3: Final[int] = 12
SPACE_4: Final[int] = 16
SPACE_5: Final[int] = 24
SPACE_6: Final[int] = 32
SPACE_7: Final[int] = 48

RADIUS_SMALL: Final[int] = 4
RADIUS_PANEL: Final[int] = 7
RADIUS_HERO: Final[int] = 12
