"""Player colours shared by the UI and the car lights.

The engine values are deliberately pure RGB levels (0 or 14).  A firmware
FLASH only has on/off channels, so a mixed colour such as orange would flash
yellow.  Keeping both palettes hue-compatible means the blinking colour still
matches the steady player colour.

The car paint palette is separate: it only colours the body sprite so the car
looks like the physical model.  Trails, HUD, lights and every other marker stay
in the player colour.
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


# Real-world body colours from the model list in anki/vehicle.py.  Two-tone
# cars use the colour that identifies them best at sprite size.
CAR_PAINT = {
    "Ground Shock": (45, 110, 235),    # blue/black
    "Skull": (200, 30, 45),            # black shell with red accents
    "Thermo": (215, 70, 35),           # red/crimson, orange-yellow trim
    "Nuke": (100, 190, 60),            # neon green/black
    "Guardian": (150, 170, 195),       # silver/white/blue police livery
    "Big Bang": (125, 135, 55),        # olive green, orange-yellow trim
    "Free Wheel": (70, 165, 105),      # silver truck with green trim
    "X52": (140, 35, 40),              # black with red graphics
    "X52 Ice": (205, 228, 240),        # white/silver/blue
    "Mammoth": (125, 140, 165),        # blue-grey digital camo
    "Dynamo": (170, 178, 188),         # silver/grey, black hood
    "Nuke Phantom": (228, 230, 238),   # white/black
}


def _paint_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


_PAINT_BY_KEY = {_paint_key(name): rgb for name, rgb in CAR_PAINT.items()}


def color_for_index(index: int) -> tuple[int, int, int]:
    return PLAYER_COLORS[index % len(PLAYER_COLORS)]


def engine_for_index(index: int) -> tuple[int, int, int]:
    return ENGINE_COLORS[index % len(ENGINE_COLORS)]


def paint_for_model(model: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    """Real body paint for a model name, or the player colour for unknown cars."""
    return _PAINT_BY_KEY.get(_paint_key(model), fallback)
