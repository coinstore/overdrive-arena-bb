"""Player colours shared by the UI and the car lights.

The engine values are deliberately pure RGB levels (0 or 14).  A firmware
FLASH only has on/off channels, so a mixed colour such as orange would flash
yellow.  Keeping both palettes hue-compatible means the blinking colour still
matches the steady player colour.
"""

PLAYER_COLORS = [
    (255, 90, 90),    # red
    (90, 190, 255),   # blue
    (120, 240, 120),  # green
    (255, 220, 90),   # yellow
    (230, 130, 255),  # magenta
    (235, 235, 245),  # white
]

ENGINE_COLORS = [
    (14, 0, 0),    # red
    (0, 0, 14),    # blue
    (0, 14, 0),    # green
    (14, 14, 0),   # yellow
    (14, 0, 14),   # magenta
    (14, 14, 14),  # white
]


def color_for_index(index: int) -> tuple[int, int, int]:
    return PLAYER_COLORS[index % len(PLAYER_COLORS)]


def engine_for_index(index: int) -> tuple[int, int, int]:
    return ENGINE_COLORS[index % len(ENGINE_COLORS)]
