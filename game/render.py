"""pygame rendering: live track map with effects, HUD cards, lobby, garage and scanning screens."""
from __future__ import annotations

import math
import random
import time
from collections import deque
from pathlib import Path
from typing import Optional

import pygame

from anki.track import START, TURN, Track
from .game import (MAX_HP, BATTLE_LIVES, POWERUPS, WEAPONS, Game, CarState, Loadout,
                   MINE_ARM_S)

BG = (14, 15, 19)
BG_GRID = (22, 23, 29)
BG_WALL = (17, 19, 24)
BG_WALL_ALT = (20, 22, 28)
BG_FLOOR = (12, 13, 17)
WALL_LINE = (29, 31, 39)
FLOOR_LINE = (24, 26, 33)
METAL = (58, 60, 72)
METAL_LIGHT = (92, 96, 112)
HAZARD = (118, 88, 32)
HAZARD_DARK = (52, 46, 28)
PANEL = (24, 25, 31)
CARD = (32, 34, 42)
CARD_HI = (40, 42, 52)
ROAD = (52, 54, 62)
ROAD_EDGE = (110, 114, 128)
LANE_LINE = (84, 87, 100)
TEXT = (236, 237, 240)
DIM = (156, 162, 176)
FAINT = (96, 102, 118)
OK = (110, 220, 130)
WARN = (255, 176, 70)
BAD = (255, 92, 92)
GOLD = (255, 214, 92)

ROAD_HALF_MM = 112.0
LANES_MM = (-68.0, -23.0, 23.0, 68.0)

BATTERY_COLORS = {"ok": OK, "weak": WARN, "low": WARN, "critical": BAD, "unknown": FAINT, "charging": (120, 200, 255)}

def with_alpha(color, a: int) -> tuple:
    return (color[0], color[1], color[2], max(0, min(255, int(a))))


def glow(surface: pygame.Surface, pos: tuple[int, int], radius: int, color, strength: float = 1.0) -> None:
    """Soft radial glow blitted additively."""
    r = max(2, int(radius))
    s = pygame.Surface((r * 2, r * 2), pygame.SRCALPHA)
    steps = 4
    for i in range(steps, 0, -1):
        rr = int(r * i / steps)
        a = int(26 * strength * (1 - (i - 1) / steps))
        pygame.draw.circle(s, with_alpha(color, a), (r, r), rr)
    surface.blit(s, (pos[0] - r, pos[1] - r))


def _shade(color, factor: float) -> tuple:
    return tuple(max(0, min(255, int(c * factor))) for c in color[:3])


def _mix(a, b, t: float) -> tuple:
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] * (1.0 - t) + b[i] * t) for i in range(3))


CAR_DARK = (13, 14, 18)
CAR_TIRE = (21, 22, 27)
CAR_TIRE_HI = (48, 51, 62)
CAR_RIM = (146, 152, 168)
CAR_GLASS = (48, 82, 116)
CAR_GLASS_HI = (106, 168, 214)
CAR_LIGHT = (255, 246, 190)
CAR_TAIL = (238, 62, 56)
CAR_METAL = (150, 156, 172)


def _car_wheel(s: pygame.Surface, x: int, y: int, w: int, h: int, far: bool = False,
               accent=None) -> None:
    tire = (15, 16, 20) if far else CAR_TIRE
    pygame.draw.rect(s, CAR_DARK, (x, y + 1, w, h - 2))
    pygame.draw.rect(s, CAR_DARK, (x + 1, y, w - 2, h))
    pygame.draw.rect(s, tire, (x + 1, y + 1, w - 2, h - 2))
    pygame.draw.rect(s, tire, (x + 2, y, w - 4, h))
    rim = (46, 49, 60) if far else CAR_RIM
    pygame.draw.rect(s, (28, 30, 37), (x + w // 2 - 2, y + h // 2 - 2, 4, 4))
    pygame.draw.rect(s, rim, (x + w // 2 - 1, y + h // 2 - 1, 3, 3))
    if not far:
        pygame.draw.rect(s, _shade(rim, 1.35), (x + w // 2 - 1, y + h // 2 - 1, 1, 1))
        pygame.draw.rect(s, (25, 27, 33) if accent is None else accent,
                         (x + w // 2, y + h // 2 + 1, 1, 1))
        pygame.draw.rect(s, CAR_TIRE_HI, (x + 2, y + 1, w - 4, 1))


def _car_shadow(s: pygame.Surface) -> None:
    pygame.draw.polygon(s, (9, 10, 13), [(3, 21), (8, 22), (35, 22), (41, 21), (35, 20), (9, 20)])
    pygame.draw.line(s, (26, 28, 35), (9, 21), (34, 21))


def _car_sport(s: pygame.Surface, body, kind: str, accent=None) -> None:
    out = [(1, 15), (2, 12), (6, 11), (10, 10), (13, 6), (22, 5), (26, 9), (32, 10),
           (38, 11), (41, 13), (42, 15), (42, 17), (39, 18), (6, 18), (2, 16)]
    fill = [(2, 15), (3, 13), (6, 12), (11, 11), (14, 7), (21, 6), (25, 10), (31, 11),
            (37, 12), (40, 14), (41, 15), (41, 16), (38, 17), (7, 17), (3, 16)]
    pygame.draw.polygon(s, CAR_DARK, out)
    pygame.draw.polygon(s, body, fill)
    # cabin and glass
    pygame.draw.polygon(s, _shade(body, 0.72), [(13, 8), (15, 6), (21, 6), (25, 9), (17, 10)])
    pygame.draw.polygon(s, CAR_GLASS, [(15, 7), (21, 7), (23, 9), (16, 9)])
    pygame.draw.polygon(s, CAR_GLASS_HI, [(16, 7), (18, 7), (17, 8)])
    pygame.draw.polygon(s, CAR_GLASS_HI, [(24, 7), (25, 9), (22, 9)])
    pygame.draw.line(s, CAR_DARK, (22, 7), (22, 9), 1)
    pygame.draw.rect(s, (206, 210, 220), (18, 7, 2, 1))
    pygame.draw.rect(s, (66, 78, 100), (18, 8, 2, 1))
    pygame.draw.line(s, _shade(body, 1.22), (15, 5), (21, 5), 1)
    # beltline, door and fender highlights
    pygame.draw.line(s, _shade(body, 1.22), (5, 12), (30, 12), 1)
    pygame.draw.line(s, _shade(body, 0.55), (19, 12), (19, 16), 1)
    pygame.draw.line(s, _shade(body, 1.3), (2, 11), (11, 10), 1)
    pygame.draw.line(s, _shade(body, 1.3), (31, 11), (38, 12), 1)
    pygame.draw.rect(s, _shade(body, 1.3), (22, 13, 2, 1))
    # front fascia, lights and grille
    pygame.draw.rect(s, CAR_DARK, (39, 13, 3, 5))
    pygame.draw.rect(s, _shade(body, 0.5), (39, 14, 3, 3))
    pygame.draw.rect(s, CAR_LIGHT, (40, 13, 2, 2))
    pygame.draw.rect(s, (255, 255, 235), (40, 13, 1, 1))
    pygame.draw.rect(s, CAR_LIGHT, (37, 12, 1, 1))
    pygame.draw.rect(s, CAR_DARK, (37, 17, 6, 2))
    pygame.draw.rect(s, _shade(body, 0.55), (38, 17, 4, 1))
    # tail light and side intake
    pygame.draw.rect(s, CAR_TAIL, (1, 13, 2, 3))
    pygame.draw.rect(s, (255, 150, 140), (1, 13, 1, 1))
    pygame.draw.polygon(s, CAR_DARK, [(27, 14), (33, 14), (35, 16), (28, 16)])
    pygame.draw.polygon(s, _shade(body, 1.35), [(28, 14), (32, 14), (33, 15), (29, 15)])
    _car_wheel(s, 3, 11, 9, 10, accent=accent)
    _car_wheel(s, 31, 12, 9, 10, accent=accent)
    if kind == "x52":
        # tall rear wing on two visible struts
        pygame.draw.rect(s, _shade(body, 0.72), (6, 6, 2, 5))
        pygame.draw.rect(s, _shade(body, 0.72), (10, 6, 2, 5))
        pygame.draw.rect(s, CAR_DARK, (0, 3, 14, 4))
        pygame.draw.rect(s, _shade(body, 0.95), (1, 4, 12, 2))
        pygame.draw.rect(s, _shade(body, 1.4), (2, 4, 8, 1))
        pygame.draw.rect(s, CAR_TAIL, (0, 3, 2, 4))
        pygame.draw.rect(s, CAR_DARK, (2, 17, 5, 1))
    elif kind == "dynamo":
        # integrated trunk lip and chrome trim
        pygame.draw.line(s, _shade(body, 1.25), (2, 11), (7, 11), 1)
        pygame.draw.rect(s, _shade(body, 1.3), (3, 13, 4, 1))
    else:
        pygame.draw.rect(s, CAR_DARK, (1, 8, 9, 3))
        pygame.draw.rect(s, _shade(body, 0.85), (2, 9, 7, 1))


def _car_mammoth(s: pygame.Surface, body, accent=None) -> None:
    out = [(1, 14), (2, 11), (5, 9), (9, 8), (11, 3), (27, 2), (30, 8), (37, 9),
           (41, 11), (43, 14), (43, 18), (40, 19), (6, 19), (2, 17)]
    fill = [(2, 14), (3, 11), (6, 10), (10, 9), (12, 4), (26, 3), (29, 9), (36, 10),
            (40, 12), (42, 14), (42, 18), (39, 18), (7, 18), (3, 16)]
    pygame.draw.polygon(s, CAR_DARK, out)
    pygame.draw.polygon(s, body, fill)
    # tall cabin with split windows
    pygame.draw.polygon(s, _shade(body, 0.72), [(12, 8), (13, 4), (26, 3), (28, 9), (16, 10)])
    pygame.draw.polygon(s, CAR_GLASS, [(14, 6), (20, 6), (20, 9), (15, 9)])
    pygame.draw.polygon(s, CAR_GLASS, [(21, 6), (26, 5), (27, 8), (21, 9)])
    pygame.draw.line(s, CAR_DARK, (20, 6), (20, 9), 1)
    pygame.draw.polygon(s, CAR_GLASS_HI, [(15, 6), (18, 6), (16, 7)])
    pygame.draw.polygon(s, CAR_GLASS_HI, [(22, 5), (25, 5), (23, 6)])
    pygame.draw.rect(s, (206, 210, 220), (18, 6, 2, 1))
    # roof rack with lamps
    pygame.draw.rect(s, _shade(body, 0.6), (11, 2, 16, 1))
    for lx in (12, 16, 20, 24):
        pygame.draw.rect(s, CAR_DARK, (lx - 1, 0, 4, 2))
        pygame.draw.rect(s, (255, 220, 96), (lx, 0, 2, 1))
    # cladding and bull bar
    pygame.draw.rect(s, (46, 50, 60), (5, 16, 34, 2))
    pygame.draw.rect(s, (28, 31, 39), (5, 17, 34, 1))
    pygame.draw.rect(s, CAR_METAL, (40, 11, 2, 7))
    pygame.draw.rect(s, CAR_METAL, (41, 12, 2, 1))
    pygame.draw.rect(s, CAR_METAL, (41, 15, 2, 1))
    pygame.draw.rect(s, CAR_DARK, (42, 11, 1, 7))
    # lamps, grille, tail and door
    pygame.draw.rect(s, CAR_LIGHT, (40, 12, 2, 2))
    pygame.draw.rect(s, CAR_TAIL, (1, 12, 2, 3))
    pygame.draw.rect(s, CAR_DARK, (29, 12, 9, 2))
    pygame.draw.rect(s, _shade(body, 0.7), (30, 12, 7, 1))
    pygame.draw.line(s, _shade(body, 0.55), (21, 9), (21, 16), 1)
    pygame.draw.rect(s, _shade(body, 1.25), (23, 12, 4, 1))
    _car_wheel(s, 3, 10, 10, 11, accent=accent)
    _car_wheel(s, 30, 11, 10, 11, accent=accent)


def car_sprite(model: str, body, accent=None) -> pygame.Surface:
    """44x24 pixel hero car, front three-quarter view."""
    s = pygame.Surface((44, 24), pygame.SRCALPHA)
    _car_shadow(s)
    _car_wheel(s, 8, 10, 6, 6, far=True)
    _car_wheel(s, 34, 11, 6, 6, far=True)
    m = model.lower()
    if "mammoth" in m:
        _car_mammoth(s, body, accent)
    elif "x52" in m:
        _car_sport(s, body, "x52", accent)
    elif "dynamo" in m:
        _car_sport(s, body, "dynamo", accent)
    else:
        _car_sport(s, body, "sport", accent)
    return s


def draw_battery_icon(surface, x: int, y: int, w: int, h: int, pct: Optional[int], color) -> None:
    pygame.draw.rect(surface, DIM, (x, y, w, h), 1, border_radius=2)
    pygame.draw.rect(surface, DIM, (x + w, y + h // 4, 3, h // 2), border_radius=1)
    if pct is not None:
        fill = int((w - 4) * pct / 100)
        if fill > 0:
            pygame.draw.rect(surface, color, (x + 2, y + 2, fill, h - 4), border_radius=1)


def battery_level_color(pct: Optional[int], status: str):
    """Race HUD colour: green, orange, red; unknown stays faint."""
    if status == "unknown" or pct is None:
        return FAINT
    if status in ("critical", "low") or pct <= 20:
        return BAD
    if status == "weak" or pct <= 45:
        return WARN
    return OK


def draw_battery_meter(surface, x: int, y: int, w: int, h: int, pct: Optional[int],
                       color, segments: int = 5) -> None:
    """Right-aligned segmented battery: it empties as the charge drops."""
    body_w = w - 4
    pygame.draw.rect(surface, (7, 8, 11), (x + 2, y + 2, body_w, h))
    pygame.draw.rect(surface, (10, 11, 15), (x, y, body_w, h))
    pygame.draw.rect(surface, color, (x, y, body_w, h), 2)
    pygame.draw.rect(surface, color, (x + body_w, y + h // 3, 3, h // 3))
    inner = pygame.Rect(x + 4, y + 4, body_w - 8, h - 8)
    pygame.draw.rect(surface, (10, 11, 15), inner)
    lit = 0 if pct is None else max(0, min(segments, math.ceil(pct * segments / 100)))
    for i in range(segments):
        sx = inner.x + i * inner.width // segments
        ex = inner.x + (i + 1) * inner.width // segments - 1
        col = color if i < lit else (30, 32, 40)
        pygame.draw.rect(surface, col, (sx, inner.y, max(1, ex - sx), inner.height))


class CarView:
    """Per-car display state: exact engine position and a fading trail."""

    def __init__(self):
        self.x: Optional[float] = None
        self.y: Optional[float] = None
        self.trail: deque[tuple[float, float, float]] = deque(maxlen=22)
        self.hp_lag = float(MAX_HP)

    def update(self, target: Optional[tuple[float, float]], now: float, dt: float, hp: int) -> None:
        if target is not None:
            self.x, self.y = target
            if not self.trail or math.hypot(self.x - self.trail[-1][0], self.y - self.trail[-1][1]) > 12:
                self.trail.append((self.x, self.y, now))
        self.hp_lag = max(hp, self.hp_lag - dt * 60) if self.hp_lag > hp else float(hp)


class MapView:
    def __init__(self, track: Track, rect: pygame.Rect, margin: int = 46):
        self.track = track
        self.rect = rect
        x0, y0, x1, y1 = track.bounds()
        x0 -= ROAD_HALF_MM; y0 -= ROAD_HALF_MM; x1 += ROAD_HALF_MM; y1 += ROAD_HALF_MM
        w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
        self.scale = min((rect.width - 2 * margin) / w, (rect.height - 2 * margin) / h)
        self.ox = rect.x + (rect.width - w * self.scale) / 2 - x0 * self.scale
        self.oy = rect.y + (rect.height - h * self.scale) / 2 - y0 * self.scale
        self._road: Optional[pygame.Surface] = None
        self.views: dict[int, CarView] = {}
        self.last_t = time.monotonic()

    def to_screen(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        return int(self.ox + x_mm * self.scale), int(self.oy + y_mm * self.scale)

    # ---- static road layer ----
    def _draw_road(self, surf: pygame.Surface) -> None:
        t = self.track
        # background grid
        for gx in range(0, surf.get_width(), 40):
            pygame.draw.line(surf, BG_GRID, (gx, 0), (gx, surf.get_height()))
        for gy in range(0, surf.get_height(), 40):
            pygame.draw.line(surf, BG_GRID, (0, gy), (surf.get_width(), gy))
        # drop shadow + asphalt
        for i, p in enumerate(t.pieces):
            steps = 20 if p.kind == TURN else 2
            left = [self.to_screen(*t.world_point(i, k / steps, -ROAD_HALF_MM)) for k in range(steps + 1)]
            right = [self.to_screen(*t.world_point(i, k / steps, ROAD_HALF_MM)) for k in range(steps + 1)]
            shadow = [(x + 4, y + 5) for x, y in left + right[::-1]]
            pygame.draw.polygon(surf, (10, 10, 13), shadow)
        for i, p in enumerate(t.pieces):
            steps = 20 if p.kind == TURN else 2
            left = [self.to_screen(*t.world_point(i, k / steps, -ROAD_HALF_MM)) for k in range(steps + 1)]
            right = [self.to_screen(*t.world_point(i, k / steps, ROAD_HALF_MM)) for k in range(steps + 1)]
            pygame.draw.polygon(surf, ROAD, left + right[::-1])
        # dashed lane lines
        for i, p in enumerate(t.pieces):
            steps = 24 if p.kind == TURN else 12
            for lane in LANES_MM:
                pts = [self.to_screen(*t.world_point(i, k / steps, lane)) for k in range(steps + 1)]
                for k in range(0, steps, 2):
                    pygame.draw.line(surf, LANE_LINE, pts[k], pts[k + 1], 1)
        # edges
        for i, p in enumerate(t.pieces):
            steps = 20 if p.kind == TURN else 2
            for edge in (-ROAD_HALF_MM, ROAD_HALF_MM):
                pts = [self.to_screen(*t.world_point(i, k / steps, edge)) for k in range(steps + 1)]
                pygame.draw.lines(surf, ROAD_EDGE, False, pts, 2)
        # piece joints
        for i, p in enumerate(t.pieces):
            a = self.to_screen(*t.world_point(i, 0.0, -ROAD_HALF_MM))
            b = self.to_screen(*t.world_point(i, 0.0, ROAD_HALF_MM))
            pygame.draw.line(surf, (70, 72, 84), a, b, 1)
        # start / finish
        bar = t.start_bar_frac
        for i, p in enumerate(t.pieces):
            if p.kind != START:
                continue
            n = 8
            for row in range(2):
                for j in range(n):
                    a = -ROAD_HALF_MM + j * (2 * ROAD_HALF_MM / n)
                    b = a + 2 * ROAD_HALF_MM / n
                    f0, f1 = bar - 0.03 + row * 0.03, bar + row * 0.03
                    quad = [self.to_screen(*t.world_point(i, f0, a)), self.to_screen(*t.world_point(i, f0, b)),
                            self.to_screen(*t.world_point(i, f1, b)), self.to_screen(*t.world_point(i, f1, a))]
                    pygame.draw.polygon(surf, (235, 235, 235) if (j + row) % 2 == 0 else (28, 28, 32), quad)
            ax, ay = self.to_screen(*t.world_point(i, min(0.9, bar + 0.06), 0))
            bx, by = self.to_screen(*t.world_point(i, min(0.98, bar + 0.30), 0))
            pygame.draw.line(surf, (130, 136, 150), (ax, ay), (bx, by), 3)
            ang = math.atan2(by - ay, bx - ax)
            pygame.draw.polygon(surf, (130, 136, 150), [(bx, by),
                                                        (bx - 11 * math.cos(ang - 0.5), by - 11 * math.sin(ang - 0.5)),
                                                        (bx - 11 * math.cos(ang + 0.5), by - 11 * math.sin(ang + 0.5))])

    def road(self) -> pygame.Surface:
        if self._road is None:
            s = pygame.Surface(self.rect.size)
            s.fill(PANEL)
            saved = (self.ox, self.oy)
            self.ox -= self.rect.x; self.oy -= self.rect.y
            self._draw_road(s)
            self.ox, self.oy = saved
            self._road = s
        return self._road

    # ---- dynamic layer ----
    def draw(self, screen: pygame.Surface, game: Game, font: pygame.font.Font, small: pygame.font.Font) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self.last_t)
        self.last_t = now
        screen.blit(self.road(), self.rect.topleft)
        clip = screen.get_clip()
        screen.set_clip(self.rect)

        # mines
        for m in game.mines:
            x, y = self.to_screen(m.x, m.y)
            armed = now - m.placed_at > MINE_ARM_S
            pulse = 0.5 + 0.5 * math.sin(now * 6)
            if armed:
                glow(screen, (x, y), 16 + int(6 * pulse), (255, 120, 40), 0.9)
            pygame.draw.circle(screen, (255, 130, 50) if armed else (140, 90, 50), (x, y), 6)
            pygame.draw.circle(screen, (60, 25, 10), (x, y), 3)
            pygame.draw.circle(screen, m.owner.color, (x, y), 9, 1)

        # hook cables
        for t in game.tethers:
            a = self.to_screen(t.x0, t.y0)
            b = self.to_screen(t.x1, t.y1)
            pygame.draw.line(screen, (80, 60, 20), a, b, 4)
            pygame.draw.line(screen, t.color, a, b, 2)
            glow(screen, b, 10, t.color, 0.7)

        # trails
        for c in game.cars:
            v = self.views.setdefault(c.index, CarView())
            target = c.render_world(now) if c.map_position_known() else None
            v.update(target, now, dt, c.hp)
            pts = list(v.trail)
            for i in range(1, len(pts)):
                age = now - pts[i][2]
                if age > 1.2:
                    continue
                a = int(120 * (1 - age / 1.2))
                w = 3 if i > len(pts) - 6 else 2
                s = pygame.Surface((abs(int((pts[i][0] - pts[i - 1][0]) * self.scale)) + 6,
                                    abs(int((pts[i][1] - pts[i - 1][1]) * self.scale)) + 6), pygame.SRCALPHA)
                p0 = self.to_screen(*pts[i - 1][:2]); p1 = self.to_screen(*pts[i][:2])
                ox, oy = min(p0[0], p1[0]) - 3, min(p0[1], p1[1]) - 3
                pygame.draw.line(s, with_alpha(c.color, a), (p0[0] - ox, p0[1] - oy), (p1[0] - ox, p1[1] - oy), w)
                screen.blit(s, (ox, oy))

        # lasers
        for l in game.lasers:
            a = self.to_screen(l.x0, l.y0)
            b = self.to_screen(l.x1, l.y1)
            life = max(0.0, l.until - now)
            pygame.draw.line(screen, l.color, a, b, 5 if l.hit else 2)
            pygame.draw.line(screen, (255, 255, 255), a, b, 1)
            if l.hit:
                glow(screen, b, 22, l.color, 1.2)
                pygame.draw.circle(screen, (255, 255, 255), b, 6 + int(10 * (1 - life / 0.25)), 2)

        # cars
        for c in game.cars:
            v = self.views[c.index]
            if v.x is None or not c.map_position_known():
                continue
            x, y = self.to_screen(v.x, v.y)
            ang = c.loc.heading()  # type: ignore[union-attr]
            dead = c.is_dead(now)
            paint = (80, 80, 86) if dead else c.body_color
            if not dead:
                glow(screen, (x, y), 24, c.color, 0.8)
            # body: rounded rectangle rotated along heading
            L, W = 22, 12
            cs, sn = math.cos(ang), math.sin(ang)
            def pt(dx, dy):
                return (x + dx * cs - dy * sn, y + dx * sn + dy * cs)
            body = [pt(L / 2, -W / 2 + 3), pt(L / 2 - 3, -W / 2), pt(-L / 2 + 2, -W / 2), pt(-L / 2, -W / 2 + 2),
                    pt(-L / 2, W / 2 - 2), pt(-L / 2 + 2, W / 2), pt(L / 2 - 3, W / 2), pt(L / 2, W / 2 - 3)]
            pygame.draw.polygon(screen, (12, 12, 15), [(px + 2, py + 3) for px, py in body])
            pygame.draw.polygon(screen, paint, body)
            # player-coloured outline keeps ownership readable around the real paint
            pygame.draw.polygon(screen, (20, 20, 24) if dead else c.color, body, 2)
            roof = [pt(4, -W / 2 + 3), pt(-5, -W / 2 + 3), pt(-5, W / 2 - 3), pt(4, W / 2 - 3)]
            pygame.draw.polygon(screen, (28, 30, 36), roof)
            # headlights
            for dy in (-W / 2 + 3, W / 2 - 3):
                hx, hy = pt(L / 2, dy)
                pygame.draw.circle(screen, (255, 250, 210), (int(hx), int(hy)), 2)
            label = small.render(c.name if c.localized() else f"{c.name} · stopped", True, c.color)
            lx, ly = x - label.get_width() // 2, y - 30
            pygame.draw.rect(screen, (0, 0, 0), (lx - 4, ly - 1, label.get_width() + 8, label.get_height() + 2), border_radius=4)
            screen.blit(label, (lx, ly))
            if game.mode == "battle":
                bw = 40
                pygame.draw.rect(screen, (30, 30, 36), (x - bw // 2, y + 16, bw, 5), border_radius=2)
                pygame.draw.rect(screen, OK if c.hp > 40 else (255, 150, 90),
                                 (x - bw // 2, y + 16, int(bw * c.hp / MAX_HP), 5), border_radius=2)

        # effects
        for e in game.fx:
            t = (now - e.t0) / e.duration
            x, y = self.to_screen(e.x, e.y)
            if e.kind == "ring":
                pygame.draw.circle(screen, e.color, (x, y), int(8 + 34 * t), max(1, int(4 * (1 - t))))
            elif e.kind == "boom":
                glow(screen, (x, y), int(20 + 50 * t), e.color, 2.0 * (1 - t))
                for k in range(10):
                    rnd = random.Random(k + int(e.t0 * 1000))
                    a = rnd.uniform(0, math.tau); spd = rnd.uniform(30, 90)
                    px, py = x + math.cos(a) * spd * t, y + math.sin(a) * spd * t
                    pygame.draw.circle(screen, e.color if k % 2 else (255, 230, 160), (int(px), int(py)), max(1, int(4 * (1 - t))))
                pygame.draw.circle(screen, (255, 255, 255), (x, y), int(6 + 40 * t), max(1, int(3 * (1 - t))))
            elif e.kind == "text":
                img = font.render(e.text, True, e.color)
                img.set_alpha(int(255 * (1 - t)))
                screen.blit(img, (x + 16, y - 30 - int(30 * t)))
            elif e.kind == "spawn":
                pygame.draw.circle(screen, e.color, (x, y), int(40 * (1 - t)), 2)

        screen.set_clip(clip)

        # off-track cars, parked in a corner of the map
        off = [c for c in game.cars if not c.map_position_known()]
        if off:
            x0, y0 = self.rect.x + 16, self.rect.bottom - 16 - 22 * len(off)
            for i, c in enumerate(off):
                y = y0 + i * 22
                pygame.draw.circle(screen, c.body_color, (x0 + 8, y + 8), 7)
                pygame.draw.circle(screen, c.color, (x0 + 8, y + 8), 7, 2)
                st = c.track_status()
                msg = {"link lost": "link lost", "off track": "NOT ON THE TRACK", "idle": "stopped, position unknown",
                       "charging": "on the charging pad"}.get(st, st)
                col = WARN if st == "off track" else ((120, 200, 255) if st == "charging" else DIM)
                screen.blit(small.render(f"{c.name}: {msg}", True, col), (x0 + 22, y))


class Renderer:
    def __init__(self, screen: pygame.Surface):
        self.screen = screen
        self.font = pygame.font.SysFont("consolas", 16)
        self.small = pygame.font.SysFont("consolas", 13)
        self.title = pygame.font.SysFont("segoeui,consolas", 30, bold=True)
        self.big = pygame.font.SysFont("segoeui,consolas", 22, bold=True)
        self.mono_big = pygame.font.SysFont("consolas", 22, bold=True)
        font_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts"
        try:
            self.pixel_small = pygame.font.Font(str(font_dir / "Silkscreen-Regular.ttf"), 10)
            self.pixel = pygame.font.Font(str(font_dir / "Silkscreen-Regular.ttf"), 14)
            self.pixel_big = pygame.font.Font(str(font_dir / "Silkscreen-Bold.ttf"), 18)
            self.pixel_title = pygame.font.Font(str(font_dir / "Silkscreen-Bold.ttf"), 22)
        except OSError:
            self.pixel_small = pygame.font.SysFont("consolas", 11, bold=True)
            self.pixel = pygame.font.SysFont("consolas", 15, bold=True)
            self.pixel_big = pygame.font.SysFont("consolas", 19, bold=True)
            self.pixel_title = pygame.font.SysFont("consolas", 24, bold=True)
        self.map_view: Optional[MapView] = None
        self.map_key: Optional[tuple] = None
        self._menu_bg: Optional[pygame.Surface] = None
        self._menu_bg_key: Optional[tuple] = None
        self._car_sprites: dict[tuple, pygame.Surface] = {}
        self.buttons: dict[str, pygame.Rect] = {}   # clickable areas of the current screen

    # ---- helpers ----
    def text(self, s: str, pos, color=TEXT, font=None) -> int:
        f = font or self.font
        img = f.render(s, True, color)
        self.screen.blit(img, pos)
        return img.get_height()

    def text_right(self, s: str, right: int, y: int, color=TEXT, font=None) -> None:
        f = font or self.font
        img = f.render(s, True, color)
        self.screen.blit(img, (right - img.get_width(), y))

    # ---- pixel UI kit ----
    def pixel_panel(self, rect: pygame.Rect, fill=PANEL, border=(58, 60, 72),
                    shadow: int = 4, accent=None, radius: int = 2) -> None:
        if shadow:
            pygame.draw.rect(self.screen, (6, 7, 10),
                             rect.move(shadow, shadow), border_radius=radius)
        pygame.draw.rect(self.screen, fill, rect, border_radius=radius)
        if accent is not None:
            pygame.draw.rect(self.screen, accent, (rect.x, rect.y, rect.width, 6),
                             border_top_left_radius=radius, border_top_right_radius=radius)
        pygame.draw.rect(self.screen, border, rect, 2, border_radius=radius)

    def _draw_status_toast(self, status: str, w: int, h: int) -> None:
        """Short warning toast above the control deck; routine confirmations are gone."""
        if not status:
            return
        txt = status[:110]
        while len(txt) > 8 and self.small.size(txt)[0] > w - 220:
            txt = txt[:-2].rstrip() + ".."
        box_w = min(w - 160, self.small.size(txt)[0] + 52)
        x, y = (w - box_w) // 2, h - 112
        pygame.draw.rect(self.screen, (6, 7, 10), (x + 3, y + 3, box_w, 20))
        pygame.draw.rect(self.screen, (28, 21, 14), (x, y, box_w, 20))
        pygame.draw.rect(self.screen, WARN, (x, y, box_w, 20), 1)
        pygame.draw.polygon(self.screen, WARN,
                            [(x + 12, y + 16), (x + 18, y + 4), (x + 24, y + 16)])
        pygame.draw.rect(self.screen, (30, 22, 12), (x + 17, y + 8, 2, 4))
        pygame.draw.rect(self.screen, (30, 22, 12), (x + 17, y + 13, 2, 1))
        self.text(txt, (x + 34, y + 3), TEXT, self.small)

    def machine_panel(self, rect: pygame.Rect, fill=PANEL, accent=(120, 170, 255),
                      border=(58, 60, 72), shadow: int = 4) -> None:
        self.pixel_panel(rect, fill, border, shadow=shadow, accent=accent, radius=2)
        pygame.draw.line(self.screen, (32, 34, 42), (rect.x + 3, rect.y + rect.height - 3),
                         (rect.right - 3, rect.y + rect.height - 3), 1)
        for bx, by in ((rect.x + 7, rect.y + 7), (rect.right - 10, rect.y + 7),
                       (rect.x + 7, rect.bottom - 10), (rect.right - 10, rect.bottom - 10)):
            pygame.draw.rect(self.screen, (8, 9, 12), (bx + 1, by + 1, 4, 4))
            pygame.draw.rect(self.screen, (92, 96, 112), (bx, by, 4, 4))
            pygame.draw.rect(self.screen, (36, 38, 48), (bx + 1, by + 1, 2, 2))

    def draw_podium(self, x: int, y: int, w: int, color, h: int = 8) -> None:
        """Low display platform with a player-coloured light strip."""
        pygame.draw.rect(self.screen, (8, 9, 12), (x + 3, y + 3, w, h))
        pygame.draw.rect(self.screen, (32, 34, 42), (x, y, w, h))
        pygame.draw.rect(self.screen, (12, 13, 17), (x, y, w, h), 2)
        pygame.draw.line(self.screen, (62, 66, 80), (x + 3, y + 2), (x + w - 3, y + 2), 1)
        pygame.draw.rect(self.screen, _shade(color, 0.55), (x + 10, y + h - 3, w - 20, 2))
        pygame.draw.rect(self.screen, color, (x + 10, y + h - 3, (w - 20) // 2, 2))
        for fx in (x + 8, x + w - 12):
            pygame.draw.rect(self.screen, (18, 19, 24), (fx, y + h, 4, 3))

    def draw_pegboard(self, rect: pygame.Rect) -> None:
        pygame.draw.rect(self.screen, (18, 20, 26), rect)
        pygame.draw.rect(self.screen, (38, 41, 52), rect, 1)
        for px in range(rect.x + 6, rect.right - 4, 8):
            for py in range(rect.y + 6, rect.bottom - 4, 8):
                pygame.draw.rect(self.screen, (34, 37, 47), (px, py, 1, 1))

    def ready_lamp_right(self, right: int, y: int, c: CarState) -> None:
        label, col = self.lobby_status(c)
        tw = self.small.size(label)[0]
        x = right - 12 - tw - 16
        pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 3.0)
        pygame.draw.rect(self.screen, (8, 9, 12), (x + 1, y + 1, 10, 10))
        pygame.draw.rect(self.screen, col, (x, y, 10, 10))
        pygame.draw.rect(self.screen, (12, 13, 17), (x, y, 10, 10), 1)
        hi = tuple(min(255, int(v * (0.65 + 0.35 * pulse))) for v in col)
        pygame.draw.rect(self.screen, hi, (x + 2, y + 2, 3, 3))
        self.text(label, (x + 16, y), col, self.small)

    def pixel_button(self, key: str, x: int, y: int, w: int, h: int, label: str,
                     hint: str, color, enabled: bool = True, font=None) -> pygame.Rect:
        rect = pygame.Rect(x, y, w, h)
        shadow = 4 if enabled else 3
        pygame.draw.rect(self.screen, (6, 7, 10), rect.move(shadow, shadow), border_radius=8)
        if enabled:
            pygame.draw.rect(self.screen, color, rect, border_radius=8)
            pygame.draw.rect(self.screen, (12, 13, 17), rect, 2, border_radius=8)
            fg, sub = (16, 17, 22), (30, 32, 40)
        else:
            pygame.draw.rect(self.screen, (42, 44, 54), rect, border_radius=8)
            pygame.draw.rect(self.screen, (64, 66, 78), rect, 2, border_radius=8)
            fg = sub = FAINT
        f = font or self.pixel_big
        img = f.render(label, True, fg)
        self.screen.blit(img, (x + 14, y + (h - img.get_height()) // 2))
        if hint:
            self.text_right(hint, x + w - 12, y + (h - 13) // 2, sub, self.small)
        self.buttons[key] = rect
        return rect

    def step_tab(self, x: int, y: int, w: int, h: int, number: str, label: str,
                 state: str) -> pygame.Rect:
        rect = pygame.Rect(x, y, w, h)
        if state == "active":
            fill, border, fg = TEXT, (12, 13, 17), (16, 17, 22)
        elif state == "done":
            fill, border, fg = (52, 92, 72), (130, 220, 160), (200, 255, 220)
        else:
            fill, border, fg = CARD, (58, 60, 72), FAINT
        pygame.draw.rect(self.screen, (6, 7, 10), rect.move(3, 3), border_radius=2)
        pygame.draw.rect(self.screen, fill, rect, border_radius=2)
        pygame.draw.rect(self.screen, border, rect, 2, border_radius=2)
        self.text(number, (rect.x + 10, rect.y + 7), fg, self.pixel)
        self.text(label, (rect.x + 30, rect.y + 7), fg, self.pixel)
        if state == "done":
            self.text("OK", (rect.right - 26, rect.y + 7), fg, self.pixel_small)
        return rect

    def pixel_tag(self, x: int, y: int, label: str, color) -> int:
        img = self.small.render(label, True, (16, 17, 22))
        w = img.get_width() + 10
        pygame.draw.rect(self.screen, color, (x, y, w, 17), border_radius=2)
        pygame.draw.rect(self.screen, (12, 13, 17), (x, y, w, 17), 1, border_radius=2)
        self.screen.blit(img, (x + 5, y + 2))
        return w

    def pixel_tag_right(self, right: int, y: int, label: str, color) -> None:
        w = self.small.size(label)[0] + 10
        self.pixel_tag(right - w, y, label, color)

    def pixel_check(self, cx: int, cy: int, color) -> None:
        pygame.draw.lines(self.screen, color, False,
                          [(cx - 5, cy), (cx - 1, cy + 5), (cx + 6, cy - 5)], 3)

    def selection_brackets(self, rect: pygame.Rect, color) -> None:
        arm = 9
        for x, y, dx, dy in ((rect.x, rect.y, 1, 1), (rect.right, rect.y, -1, 1),
                             (rect.x, rect.bottom, 1, -1), (rect.right, rect.bottom, -1, -1)):
            pygame.draw.line(self.screen, color, (x, y), (x + dx * arm, y), 3)
            pygame.draw.line(self.screen, color, (x, y), (x, y + dy * arm), 3)

    def pixel_stat_bar(self, x: int, y: int, units: int, color, total: int = 5) -> int:
        for i in range(total):
            col = color if i < units else (48, 50, 62)
            pygame.draw.rect(self.screen, col, (x + i * 9, y, 7, 5))
            pygame.draw.rect(self.screen, (12, 13, 17), (x + i * 9, y, 7, 5), 1)
        return total * 9

    def draw_pixel_car(self, cx: int, cy: int, body, outline, scale: int = 4,
                       model: str = "", lights: bool = False) -> None:
        key = (model.lower(), tuple(body), tuple(outline), scale)
        img = self._car_sprites.get(key)
        if img is None:
            px = car_sprite(model, body, outline)
            img = pygame.transform.scale(px, (44 * scale, 24 * scale))
            self._car_sprites[key] = img
        ground = cy + 9 * scale
        glow(self.screen, (cx, ground - 3 * scale // 2), int(16 * scale), outline,
             0.55 if scale >= 3 else 0.3)
        if lights:
            glow(self.screen, (cx + 20 * scale, cy + scale), int(6 * scale),
                 (255, 240, 180), 0.45)
        self.screen.blit(img, (cx - 22 * scale, cy - 12 * scale))

    def draw_pixel_icon(self, kind: str, cx: int, cy: int, color, scale: int = 3) -> None:
        px = pygame.Surface((12, 12), pygame.SRCALPHA)
        dark = (14, 15, 20)
        metal = (174, 184, 202)
        light = (235, 243, 255)
        if kind == "sniper":
            pygame.draw.rect(px, dark, (1, 7, 4, 3))
            pygame.draw.rect(px, (118, 78, 46), (2, 7, 2, 2))
            pygame.draw.rect(px, dark, (4, 5, 7, 2))
            pygame.draw.rect(px, metal, (5, 5, 6, 1))
            pygame.draw.rect(px, dark, (5, 2, 4, 3))
            pygame.draw.rect(px, (80, 220, 255), (6, 3, 2, 1))
            pygame.draw.rect(px, (255, 200, 40), (10, 4, 2, 4))
            pygame.draw.rect(px, (255, 120, 30), (11, 5, 1, 2))
            pygame.draw.rect(px, (255, 70, 70), (9, 5, 1, 2))
        elif kind == "blaster":
            pygame.draw.rect(px, dark, (2, 4, 8, 5))
            pygame.draw.rect(px, (60, 190, 255), (3, 5, 7, 2))
            pygame.draw.rect(px, (150, 240, 255), (4, 5, 3, 1))
            pygame.draw.rect(px, dark, (3, 8, 3, 3))
            pygame.draw.rect(px, (255, 170, 40), (9, 4, 3, 4))
            pygame.draw.rect(px, (255, 240, 120), (10, 5, 1, 1))
            pygame.draw.rect(px, (255, 80, 80), (2, 6, 1, 2))
        elif kind == "mine":
            for sx, sy in ((5, 1), (5, 10), (1, 5), (10, 5),
                           (2, 2), (9, 2), (2, 9), (9, 9)):
                pygame.draw.rect(px, (255, 210, 70), (sx, sy, 2, 2))
            pygame.draw.rect(px, (32, 34, 42), (3, 3, 6, 6))
            pygame.draw.rect(px, (96, 102, 120), (4, 4, 4, 4))
            pygame.draw.rect(px, (255, 70, 60), (5, 5, 2, 2))
            pygame.draw.rect(px, (255, 150, 120), (5, 5, 1, 1))
        elif kind == "uturn":
            pygame.draw.lines(px, (120, 230, 255), False,
                              [(8, 2), (3, 2), (3, 8), (8, 8)], 2)
            pygame.draw.polygon(px, (255, 220, 60),
                                [(8, 6), (11, 8), (8, 10)])
            pygame.draw.rect(px, light, (4, 3, 2, 1))
        elif kind == "hook":
            pygame.draw.lines(px, (255, 220, 80), False,
                              [(1, 2), (3, 5), (6, 4), (8, 7)], 1)
            pygame.draw.lines(px, (255, 160, 40), False,
                              [(8, 7), (9, 9), (7, 10)], 1)
            pygame.draw.rect(px, (255, 245, 150), (9, 8, 2, 2))
            pygame.draw.rect(px, (255, 90, 70), (2, 3, 1, 1))
        elif kind == "anchor":
            pygame.draw.rect(px, (120, 190, 255), (5, 2, 2, 8))
            pygame.draw.rect(px, (120, 190, 255), (3, 4, 6, 1))
            pygame.draw.lines(px, (120, 190, 255), False,
                              [(2, 8), (4, 10), (8, 10), (10, 8)], 1)
            pygame.draw.circle(px, (255, 220, 90), (6, 2), 1)
        elif kind == "booster":
            pygame.draw.polygon(px, (255, 160, 40),
                                [(7, 1), (4, 5), (6, 5), (5, 8), (8, 5), (6, 5)])
            pygame.draw.polygon(px, (255, 240, 120),
                                [(7, 3), (5, 6), (6, 6), (6, 8), (8, 5)])
            pygame.draw.rect(px, (90, 190, 255), (5, 8, 4, 2))
        else:
            pygame.draw.rect(px, color, (3, 3, 6, 6), border_radius=2)
        img = pygame.transform.scale(px, (12 * scale, 12 * scale))
        self.screen.blit(img, (cx - img.get_width() // 2, cy - img.get_height() // 2))

    def _draw_tire_stack(self, x: int, y: int) -> None:
        for i in range(3):
            tire = pygame.Rect(x, y + i * 11, 34, 13)
            pygame.draw.rect(self.screen, (8, 9, 12), tire.move(2, 2))
            pygame.draw.rect(self.screen, (25, 27, 33), tire)
            pygame.draw.rect(self.screen, (12, 13, 17), tire, 2)
            pygame.draw.rect(self.screen, (52, 55, 65), (x + 5, y + i * 11 + 4, 24, 2))
            pygame.draw.rect(self.screen, (18, 20, 25), (x + 13, y + i * 11 + 6, 8, 4))

    def _draw_toolbox(self, x: int, y: int) -> None:
        pygame.draw.rect(self.screen, (8, 9, 12), (x + 3, y + 3, 40, 26))
        pygame.draw.rect(self.screen, (98, 43, 38), (x, y, 40, 26))
        pygame.draw.rect(self.screen, (12, 13, 17), (x, y, 40, 26), 2)
        pygame.draw.rect(self.screen, (150, 76, 58), (x + 4, y + 4, 32, 4))
        pygame.draw.rect(self.screen, (24, 25, 31), (x + 13, y - 5, 14, 6), 2)
        pygame.draw.rect(self.screen, (182, 150, 70), (x + 17, y + 14, 6, 5))

    def _draw_cone(self, x: int, y: int) -> None:
        pygame.draw.polygon(self.screen, (8, 9, 12), [(x + 3, y + 28), (x + 13, y + 4), (x + 23, y + 28)])
        pygame.draw.polygon(self.screen, (150, 82, 34), [(x, y + 26), (x + 10, y + 2), (x + 20, y + 26)])
        pygame.draw.rect(self.screen, (218, 196, 142), (x + 7, y + 12, 7, 3))
        pygame.draw.rect(self.screen, (54, 38, 24), (x - 4, y + 26, 28, 4))

    def _draw_barrel(self, x: int, y: int) -> None:
        pygame.draw.rect(self.screen, (8, 9, 12), (x + 2, y + 2, 26, 34))
        pygame.draw.rect(self.screen, (34, 38, 48), (x, y, 26, 34))
        pygame.draw.rect(self.screen, (12, 13, 17), (x, y, 26, 34), 2)
        pygame.draw.rect(self.screen, (58, 64, 78), (x + 2, y + 7, 22, 3))
        pygame.draw.rect(self.screen, (58, 64, 78), (x + 2, y + 24, 22, 3))
        pygame.draw.rect(self.screen, (92, 78, 40), (x + 8, y + 14, 10, 6))

    def draw_menu_background(self) -> None:
        w, h = self.screen.get_size()
        key = (w, h)
        if self._menu_bg is None or self._menu_bg_key != key:
            scene = pygame.Surface((w, h))
            saved = self.screen
            self.screen = scene
            self._draw_menu_scene(w, h)
            self.screen = saved
            self._menu_bg = scene
            self._menu_bg_key = key
        self.screen.blit(self._menu_bg, (0, 0))

    def _draw_menu_scene(self, w: int, h: int) -> None:
        self.frame()
        wall_bottom = int(h * 0.58)
        pygame.draw.rect(self.screen, BG_WALL, (0, 0, w, wall_bottom))
        pygame.draw.rect(self.screen, BG_FLOOR, (0, wall_bottom, w, h - wall_bottom))
        # corrugated wall panels
        for x in range(0, w, 48):
            if (x // 48) % 2:
                pygame.draw.rect(self.screen, BG_WALL_ALT, (x, 0, 48, wall_bottom))
            pygame.draw.line(self.screen, WALL_LINE, (x, 0), (x, wall_bottom), 1)
            for ry in (26, wall_bottom - 34):
                pygame.draw.rect(self.screen, (34, 36, 45), (x + 6, ry, 2, 2))
                pygame.draw.rect(self.screen, (11, 12, 16), (x + 8, ry + 1, 1, 1))
        # roll-up garage door in the open top-middle area
        door_x = 380
        door_w = max(0, (w - 486) - door_x - 24)
        if door_w > 100:
            pygame.draw.rect(self.screen, (10, 11, 14), (door_x + 2, 2, door_w, 116))
            pygame.draw.rect(self.screen, (26, 28, 35), (door_x, 0, door_w, 116))
            pygame.draw.rect(self.screen, (48, 51, 62), (door_x, 0, door_w, 116), 2)
            for sy in range(8, 108, 14):
                pygame.draw.rect(self.screen, (20, 22, 28), (door_x + 4, sy, door_w - 8, 2))
                pygame.draw.rect(self.screen, (34, 36, 45), (door_x + 4, sy + 2, door_w - 8, 1))
            pygame.draw.rect(self.screen, (72, 66, 45), (door_x + door_w // 2 - 16, 94, 32, 4))
        # horizontal beams
        for by in (118, wall_bottom - 34):
            pygame.draw.rect(self.screen, (30, 33, 41), (0, by, w, 8))
            pygame.draw.line(self.screen, (46, 49, 60), (0, by), (w, by), 1)
            pygame.draw.line(self.screen, (9, 10, 13), (0, by + 7), (w, by + 7), 1)
        # hazard edge where the wall meets the floor
        pygame.draw.rect(self.screen, (24, 26, 33), (0, wall_bottom - 10, w, 10))
        for i, hx in enumerate(range(0, w, 16)):
            col = HAZARD if i % 2 == 0 else HAZARD_DARK
            pygame.draw.rect(self.screen, col, (hx, wall_bottom - 8, 16, 6))
        pygame.draw.line(self.screen, (58, 62, 76), (0, wall_bottom - 10), (w, wall_bottom - 10), 1)
        pygame.draw.line(self.screen, (8, 9, 12), (0, wall_bottom - 2), (w, wall_bottom - 2), 1)
        # concrete floor: large slabs with clean joints, no fake perspective
        row_h = 64
        for row, fy in enumerate(range(wall_bottom, h, row_h)):
            if row % 2:
                pygame.draw.rect(self.screen, (13, 14, 18), (0, fy, w, min(row_h, h - fy)))
            pygame.draw.line(self.screen, FLOOR_LINE, (0, fy), (w, fy), 1)
            offset = 40 if row % 2 else 0
            for fx in range(offset, w, 80):
                pygame.draw.line(self.screen, FLOOR_LINE,
                                 (fx, fy), (fx, min(h, fy + row_h)), 1)
        # pit lane: asphalt band with double yellow edges and a dashed centre line
        lane_y = max(wall_bottom + 24, h - 250)
        lane_h = 44
        pygame.draw.rect(self.screen, (9, 10, 14), (0, lane_y, w, lane_h))
        pygame.draw.line(self.screen, (66, 58, 32), (0, lane_y + 1), (w, lane_y + 1), 1)
        pygame.draw.line(self.screen, (66, 58, 32), (0, lane_y + lane_h - 2), (w, lane_y + lane_h - 2), 1)
        for fx in range(0, w, 64):
            pygame.draw.rect(self.screen, (58, 52, 30), (fx, lane_y + 4, 30, 2))
            pygame.draw.rect(self.screen, (58, 52, 30), (fx + 8, lane_y + lane_h - 8, 30, 2))
        for fx in range(10, w, 72):
            pygame.draw.rect(self.screen, (44, 40, 28), (fx + 30, lane_y + lane_h // 2 - 1, 30, 2))
        # long drain grate in the lane
        grate = pygame.Rect(w // 2 - 110, lane_y + 12, 220, 18)
        pygame.draw.rect(self.screen, (6, 7, 10), grate.move(1, 1))
        pygame.draw.rect(self.screen, (20, 22, 28), grate)
        pygame.draw.rect(self.screen, (46, 49, 60), grate, 1)
        for gx in range(grate.x + 5, grate.right - 3, 7):
            pygame.draw.rect(self.screen, (9, 10, 13), (gx, grate.y + 3, 3, grate.height - 6))
        # tyre marks and a couple of faint oil spots, kept low contrast
        for side in (-1, 1):
            x0 = w // 2 + side * 210
            pygame.draw.lines(self.screen, (15, 16, 21), False,
                              [(x0, lane_y + lane_h + 8), (x0 + 26, lane_y + lane_h + 2),
                               (x0 + 60, lane_y + lane_h + 10), (x0 + 96, lane_y + lane_h + 3)], 3)
        rnd = random.Random(19)
        for _ in range(3):
            ox = rnd.randrange(90, w - 90)
            oy = rnd.randrange(wall_bottom + 18, lane_y - 18)
            rw = rnd.randrange(26, 54)
            rh = rnd.randrange(5, 9)
            pygame.draw.ellipse(self.screen, (16, 17, 22), (ox - rw // 2, oy - rh // 2, rw, rh))
        # wall-mounted tool board in the left gutter
        board = pygame.Rect(10, 156, 36, 92)
        pygame.draw.rect(self.screen, (8, 9, 12), board.move(2, 2))
        pygame.draw.rect(self.screen, (27, 30, 37), board)
        pygame.draw.rect(self.screen, (52, 55, 66), board, 2)
        for py in range(board.y + 10, board.bottom - 8, 10):
            for px in range(board.x + 6, board.right - 4, 10):
                pygame.draw.rect(self.screen, (35, 38, 47), (px, py, 1, 1))
        pygame.draw.rect(self.screen, (118, 122, 136), (board.x + 8, board.y + 14, 2, 26))
        pygame.draw.rect(self.screen, (118, 122, 136), (board.x + 6, board.y + 12, 6, 4))
        pygame.draw.rect(self.screen, (96, 72, 40), (board.x + 20, board.y + 14, 2, 18))
        pygame.draw.rect(self.screen, (150, 154, 168), (board.x + 22, board.y + 14, 2, 6))
        pygame.draw.rect(self.screen, (70, 76, 90), (board.x + 16, board.y + 44, 12, 3))
        # ordered props in the side gutters
        self._draw_tire_stack(12, wall_bottom + 18)
        self._draw_toolbox(10, lane_y + 4)
        self._draw_barrel(w - 40, wall_bottom + 18)
        self._draw_cone(w - 38, lane_y + 2)
        # wall poster and warm pools of light, kept subtle
        poster = pygame.Rect(w - 156, 74, 118, 40)
        pygame.draw.rect(self.screen, (10, 11, 14), poster.move(2, 2))
        pygame.draw.rect(self.screen, (24, 27, 34), poster)
        pygame.draw.rect(self.screen, (58, 62, 76), poster, 2)
        pygame.draw.rect(self.screen, (44, 52, 62), (poster.x + 8, poster.y + 8, 36, 24))
        pygame.draw.rect(self.screen, (112, 54, 42), (poster.x + 12, poster.y + 18, 28, 7))
        pygame.draw.rect(self.screen, (170, 140, 58), (poster.x + 54, poster.y + 10, 52, 3))
        pygame.draw.rect(self.screen, (78, 84, 98), (poster.x + 54, poster.y + 20, 36, 2))
        pygame.draw.rect(self.screen, (78, 84, 98), (poster.x + 54, poster.y + 27, 26, 2))
        glow(self.screen, (int(w * 0.22), 18), 190, (255, 176, 92), 0.16)
        glow(self.screen, (int(w * 0.78), 18), 170, (255, 176, 92), 0.12)
        # sparse dither on the wall only
        for gx in range(8, w, 16):
            for gy in range(8, wall_bottom - 12, 16):
                if (gx + gy) % 48 == 0:
                    pygame.draw.rect(self.screen, (19, 21, 27), (gx, gy, 2, 2))

    def map_rect(self) -> pygame.Rect:
        w, h = self.screen.get_size()
        return pygame.Rect(12, 12, int(w * 0.70), h - 24)

    def panel_rect(self) -> pygame.Rect:
        w, h = self.screen.get_size()
        mr = self.map_rect()
        return pygame.Rect(mr.right + 12, 12, w - mr.right - 24, h - 24)

    def ensure_map(self, track: Optional[Track]) -> Optional[MapView]:
        if track is None:
            self.map_view = None
            self.map_key = None
            return None
        key = (id(track), self.screen.get_size())
        if self.map_view is None or self.map_key != key:
            self.map_view = MapView(track, self.map_rect())
            self.map_key = key
        return self.map_view

    def draw_no_track(self, msg: str) -> None:
        r = self.map_rect()
        self.pixel_panel(r, PANEL, (58, 60, 72), shadow=0, radius=2)
        self.text("NO TRACK MAP", (r.x + 28, r.y + 28), WARN, self.pixel_title)
        y = r.y + 80
        for line in msg.split("\n"):
            y += self.text(line, (r.x + 28, y), DIM) + 6

    def chip(self, x: int, y: int, label: str, color) -> int:
        return self.pixel_tag(x, y, label, color)

    def chip_right(self, right: int, y: int, label: str, color) -> None:
        self.pixel_tag_right(right, y, label, color)

    def battery_row(self, c: CarState, x: int, y: int, w: int, detailed: bool = True) -> None:
        b = c.battery
        st = b.status
        col = BATTERY_COLORS.get(st, FAINT)
        if st == "charging":
            pct = int((time.monotonic() * 40) % 100)
            draw_battery_icon(self.screen, x, y + 2, 26, 12, pct, col)
            full = c.car.status is not None and c.car.status.battery_charged
            label = "CHARGED" if full else "CHARGING"
            if detailed:
                mv = f"{b.mv / 1000:.2f} V" if b.mv else "?.?? V"
                self.text(f"{mv}  charging", (x + 36, y), col, self.small)
            else:
                self.chip_right(x + w, y - 2, label, OK if full else col)
            return
        draw_battery_icon(self.screen, x, y + 2, 26, 12, b.percent, col)
        pct = f"{b.percent:3d}%" if b.percent is not None else "  ?%"
        if detailed and b.mv:
            s = f"{b.mv / 1000:.2f} V {pct}"
        else:
            s = pct
        self.text(s, (x + 36, y), DIM if st in ("ok", "unknown") else col, self.small)
        if st not in ("ok", "unknown"):
            label = {"weak": "WEAK CELL", "low": "LOW", "critical": "CRITICAL"}[st]
            self.chip_right(x + w, y - 2, label, col)

    def battery_compact(self, c: CarState, right: int, y: int) -> None:
        """Race HUD: a segmented battery icon, no text."""
        b = c.battery
        st = b.status
        if st == "charging":
            full = c.car.status is not None and c.car.status.battery_charged
            pct = 100 if full else int((time.monotonic() * 55) % 100)
            color = OK if full else (120, 200, 255)
        else:
            pct = b.percent
            color = battery_level_color(pct, st)
        draw_battery_meter(self.screen, right - 36, y - 1, 36, 14, pct, color)

    # ---- screens ----
    def frame(self) -> None:
        self.screen.fill(BG)

    def draw_car_icon(self, cx: int, cy: int, color, scale: float = 2.2, dead: bool = False,
                      outline=None) -> None:
        L, W = 22 * scale, 12 * scale
        col = (80, 80, 86) if dead else color
        body = [(cx + L / 2, cy - W / 2 + 3 * scale), (cx + L / 2 - 3 * scale, cy - W / 2), (cx - L / 2 + 2 * scale, cy - W / 2),
                (cx - L / 2, cy - W / 2 + 2 * scale), (cx - L / 2, cy + W / 2 - 2 * scale), (cx - L / 2 + 2 * scale, cy + W / 2),
                (cx + L / 2 - 3 * scale, cy + W / 2), (cx + L / 2, cy + W / 2 - 3 * scale)]
        pygame.draw.polygon(self.screen, (12, 12, 15), [(x + 3, y + 4) for x, y in body])
        pygame.draw.polygon(self.screen, col, body)
        pygame.draw.polygon(self.screen, (20, 20, 24) if outline is None or dead else outline, body, 2)
        pygame.draw.rect(self.screen, (28, 30, 36), (cx - 5 * scale, cy - W / 2 + 3 * scale, 9 * scale, W - 6 * scale), border_radius=3)
        for dy in (-W / 2 + 3 * scale, W / 2 - 3 * scale):
            pygame.draw.circle(self.screen, (255, 250, 210), (int(cx + L / 2), int(cy + dy)), int(2 * scale))

    def player_badge(self, x: int, y: int, source, color, h: int = 24) -> int:
        img = self.pixel.render(source.short, True, (16, 17, 22))
        w = img.get_width() + 14
        pygame.draw.rect(self.screen, (6, 7, 10), (x + 2, y + 2, w, h), border_radius=2)
        pygame.draw.rect(self.screen, color, (x, y, w, h), border_radius=2)
        pygame.draw.rect(self.screen, (12, 13, 17), (x, y, w, h), 2, border_radius=2)
        self.screen.blit(img, (x + 7, y + (h - img.get_height()) // 2))
        return w

    def token(self, x: int, y: int, source, color, filled: bool = True) -> int:
        """Small pixel player badge like [P1] / [KB]. Returns its width."""
        img = self.pixel_small.render(source.short, True, (16, 17, 22) if filled else color)
        w = img.get_width() + 12
        pygame.draw.rect(self.screen, (6, 7, 10), (x + 2, y + 2, w, 20), border_radius=2)
        if filled:
            pygame.draw.rect(self.screen, color, (x, y, w, 20), border_radius=2)
            pygame.draw.rect(self.screen, (12, 13, 17), (x, y, w, 20), 2, border_radius=2)
        else:
            pygame.draw.rect(self.screen, color, (x, y, w, 20), 2, border_radius=2)
        self.screen.blit(img, (x + 6, y + 4))
        return w

    def lobby_status(self, c: CarState):
        st = c.track_status()
        label = {"on track": "READY", "idle": "READY", "off track": "WAIT",
                 "charging": "CHARGING", "link lost": "OFFLINE"}.get(st, st.upper())
        col = OK if st in ("on track", "idle") else (
            (120, 200, 255) if st == "charging" else (WARN if st == "off track" else BAD))
        return label, col

    def car_status(self, game: Game, c: CarState, now: float):
        """Short, player-facing status for a race card; None when everything is normal."""
        if game.phase in ("grid", "countdown") and c.grid_state:
            gs = c.grid_state
            label = {
                "driving": "TO THE GRID", "approach": "LINING UP", "placed": "ON THE LINE",
                "off track": "LEFT OUT", "u-turn": "TURNING", "retry lap": "RETRY",
                "recovering": "RECOVERING", "recovery pause": "RECOVERY", "bar pending": "LINING UP",
                "waiting for connection": "WAITING", "waiting for charger removal": "CHARGER",
            }.get(gs, gs.upper())
            return label, (OK if gs == "placed" else (WARN if gs == "off track" else DIM))
        if c.eliminated:
            return "ELIMINATED", BAD
        if c.is_dead(now):
            return "DESTROYED", BAD
        if game.wrong_way(c):
            return "WRONG WAY", BAD
        if c.charging:
            return "ON CHARGER", (120, 200, 255)
        if not c.localized():
            st = c.track_status()
            label = {"link lost": "LINK LOST", "off track": "OFF TRACK",
                     "idle": "STOPPED", "charging": "ON CHARGER"}.get(st, st.upper())
            col = WARN if st == "off track" else ((120, 200, 255) if st == "charging" else DIM)
            return label, col
        if now < c.boost_until:
            return "BOOST", OK
        if now < c.blaster_overheated_until:
            return "OVERHEAT", BAD
        if c.booster_active:
            return "BOOSTER", OK
        if c.anchor_active:
            return "ANCHOR", WARN
        if c.uturn_state in ("flipping_out", "flipping_back"):
            return "TURNING", WARN
        if c.uturn_state == "reversing":
            return "REVERSING", WARN
        if c.ai_track_recovery:
            return ("FINDING TRACK" if c.ai_track_recovery == "searching" else "NUDGE TO RESUME"), WARN
        return None

    def lives_pips(self, x: int, y: int, lives: int, color) -> None:
        self.text("LIVES", (x, y), FAINT, self.small)
        for i in range(BATTLE_LIVES):
            col = color if i < lives else (55, 57, 68)
            px = x + 48 + i * 11
            pygame.draw.rect(self.screen, col, (px, y + 3, 7, 7))
            pygame.draw.rect(self.screen, (12, 13, 17), (px, y + 3, 7, 7), 1)
        self.text(f"{lives}", (x + 48 + BATTLE_LIVES * 11 + 4, y), TEXT, self.small)

    def _draw_car_card(self, game: Game, c: CarState, box: pygame.Rect, owner, owner_color,
                       hovering: list, src_color: dict, now: float) -> None:
        claimed = owner is not None
        self.machine_panel(box, CARD_HI if claimed or hovering else CARD,
                           owner_color if claimed else (52, 54, 66),
                           border=owner_color if claimed else (58, 60, 72), shadow=4)
        if claimed:
            self.player_badge(box.x + 14, box.y + 14, owner, owner_color)
        elif game.ai_enabled and c.ai_pref:
            self.chip(box.x + 14, box.y + 14, f"AI · {c.ai_difficulty.title()}", (82, 86, 100))
        else:
            self.chip(box.x + 14, box.y + 14, "PARKED", FAINT)

        if claimed or (game.ai_enabled and c.ai_pref):
            self.ready_lamp_right(box.right - 12, box.y + 15, c)

        name_y = box.y + max(78, box.height - 88)
        scale = 4 if box.height >= 220 else (3 if box.height >= 190 else 2)
        car_y = max(box.y + 44, name_y - 6 - 12 * scale)
        bob = int(round(math.sin(now * 2.2 + c.index * 1.7) * 1.5))
        podium_y = car_y + 9 * scale - 2
        podium_w = min(210, box.width - 100)
        self.draw_podium(box.centerx - podium_w // 2, podium_y, podium_w, c.color,
                         8 if scale >= 3 else 6)
        self.draw_pixel_car(box.centerx, car_y + bob, c.body_color, c.color,
                            scale, c.name, lights=claimed or bool(hovering))
        self.text(c.name, (box.x + 16, name_y), TEXT, self.pixel_big)
        battery_y = box.y + max(96, box.height - 54)
        self.battery_row(c, box.x + 16, battery_y, box.width - 32, detailed=True)

        if hovering:
            self.selection_brackets(box.inflate(8, 8), src_color[hovering[0]])
            tx = box.x
            for s in hovering:
                tx += self.token(tx, box.y - 28, s, src_color[s]) + 6
                pygame.draw.polygon(self.screen, src_color[s],
                                    [(tx - 20, box.y - 6), (tx - 12, box.y - 6), (tx - 16, box.y - 1)])

    def _draw_player_strip(self, game: Game, sources: list, claims: dict, src_color: dict,
                           y: int, h: int) -> None:
        if not sources:
            return
        gap = 12
        width = (self.screen.get_width() - 112 - gap * (len(sources) - 1)) // len(sources)
        for i, s in enumerate(sources):
            box = pygame.Rect(56 + i * (width + gap), y, width, h)
            self.pixel_panel(box, CARD, (58, 60, 72), shadow=3, accent=src_color[s], radius=2)
            self.token(box.x + 14, box.y + 10, s, src_color[s])
            car = claims.get(s)
            if car is None:
                self.text("CHOOSE A CAR", (box.x + 58, box.y + 9), FAINT, self.pixel)
                self.text("A / Enter", (box.x + 58, box.y + 30), FAINT, self.small)
            else:
                self.text(car.name, (box.x + 58, box.y + 9), TEXT, self.pixel)
                label, col = self.lobby_status(car)
                pygame.draw.rect(self.screen, col, (box.x + 58, box.y + 33, 7, 7))
                pygame.draw.rect(self.screen, (12, 13, 17), (box.x + 58, box.y + 33, 7, 7), 1)
                self.text(label, (box.x + 70, box.y + 30), col, self.small)

    def _draw_garage_panel(self, game: Game, source, box: pygame.Rect, claims: dict,
                           loadouts: dict, cursor: tuple[int, int], src_color: dict, now: float) -> None:
        color = src_color[source]
        car = claims.get(source)
        loadout = loadouts.get(source, Loadout())
        self.machine_panel(box, PANEL, color, border=color, shadow=4)
        header_h = 34
        pygame.draw.rect(self.screen, color, (box.x, box.y, box.width, header_h),
                         border_top_left_radius=2, border_top_right_radius=2)
        title = f"{source.short}  {car.name if car else 'CHOOSE A CAR'}"
        self.text(title, (box.x + 12, box.y + 8), (16, 17, 22), self.pixel)

        cy = box.y + header_h + 8
        tall = box.height >= 400
        car_h = 96 if tall else 44
        if car is not None:
            bob = int(round(math.sin(now * 2.2 + car.index * 1.7) * 1.5))
            if tall:
                car_x = box.x + 92
                car_y = cy + 46
                self.draw_podium(car_x - 76, car_y + 34, 152, car.color, 8)
                self.draw_pixel_car(car_x, car_y + bob, car.body_color, car.color, 4, car.name,
                                    lights=True)
                battery_x = box.x + 190
                battery_w = box.width - 206
            else:
                car_x = box.x + 58
                car_y = cy + 18
                self.draw_podium(car_x - 40, car_y + 16, 80, car.color, 6)
                self.draw_pixel_car(car_x, car_y + bob, car.body_color, car.color, 2, car.name,
                                    lights=True)
                battery_x = box.x + 110
                battery_w = box.width - 126
            self.battery_row(car, battery_x, cy + (26 if tall else 12), battery_w, detailed=True)
            self.ready_lamp_right(box.right - 14, cy, car)
        y = cy + car_h

        sections = []
        if game.mode == "battle":
            sections.append(("WEAPON", WEAPONS, "weapon"))
        sections.append(("POWERUP", POWERUPS, "powerup"))
        row, col = cursor
        row %= len(sections)
        col %= len(sections[row][1])
        cursor = (row, col)
        icons = {"sniper": "sniper", "blaster": "blaster", "mine": "mine",
                 "uturn": "uturn", "hook": "hook", "anchor": "anchor", "booster": "booster"}
        names = {"sniper": "SNIPER", "blaster": "BLASTER", "mine": "MINE",
                 "uturn": "U-TURN", "hook": "HOOK", "anchor": "ANCHOR", "booster": "BOOSTER"}
        stats = {"sniper": "LONG SHOT", "blaster": "SPREAD · 500 MM", "mine": "LANE TRAP",
                 "uturn": "HOLD · REVERSE", "hook": "HOLD · SLOW", "anchor": "HOLD · STOP",
                 "booster": "HOLD · 160%"}
        tile_h = 104 if box.height >= 400 else 50
        compact = box.height < 400
        board = pygame.Rect(box.x + 14, y - 2, box.width - 28,
                            len(sections) * (16 + tile_h + 10) + 2)
        self.draw_pegboard(board)
        for row, (label, items, kind) in enumerate(sections):
            self.text(label, (box.x + 16, y), FAINT, self.pixel_small)
            y += 16
            gap = 8
            tile_w = (box.width - 32 - gap * (len(items) - 1)) // max(1, len(items))
            x = box.x + 16
            for col, item in enumerate(items):
                card = pygame.Rect(x, y, tile_w, tile_h)
                selected = (loadout.weapon == item) if kind == "weapon" else (loadout.powerup == item)
                hovered = cursor == (row, col)
                pygame.draw.rect(self.screen, (6, 7, 10), card.move(2, 2), border_radius=2)
                pygame.draw.rect(self.screen, _mix(CARD_HI, color, 0.2) if selected else CARD,
                                 card, border_radius=2)
                pygame.draw.rect(self.screen, color if selected else (58, 60, 72),
                                 card, 4 if selected else 2, border_radius=2)
                icon_scale = 2 if compact else 4
                self.draw_pixel_icon(icons.get(item, "blaster"), card.centerx,
                                     card.y + (16 if compact else 30), color, icon_scale)
                name_font = self.pixel_small if compact or tile_w < 130 else self.pixel
                name = names.get(item, item.upper())
                img = name_font.render(name, True, TEXT)
                while img.get_width() > card.width - 12 and len(name) > 3:
                    name = name[:-2] + ".."
                    img = name_font.render(name, True, TEXT)
                self.screen.blit(img, (card.centerx - img.get_width() // 2,
                                       card.y + (30 if compact else 58)))
                if not compact and tile_w >= 120:
                    desc = stats.get(item, "")
                    dimg = self.small.render(desc, True, DIM)
                    self.screen.blit(dimg, (card.centerx - dimg.get_width() // 2,
                                            card.y + 78))
                if selected:
                    badge = pygame.Rect(card.right - 24, card.y + 6, 18, 18)
                    pygame.draw.rect(self.screen, (6, 7, 10), badge.move(2, 2))
                    pygame.draw.rect(self.screen, color, badge)
                    pygame.draw.rect(self.screen, (12, 13, 17), badge, 1)
                    self.pixel_check(badge.centerx, badge.centery, (16, 17, 22))
                elif hovered:
                    self.selection_brackets(card.inflate(6, 6), color)
                x += tile_w + gap
            y += tile_h + 10


    def _draw_settings_modal(self, game: Game) -> None:
        w, h = self.screen.get_size()
        shade = pygame.Surface((w, h), pygame.SRCALPHA)
        shade.fill((0, 0, 0, 185))
        self.screen.blit(shade, (0, 0))
        pw, ph = 500, 250
        x, y = (w - pw) // 2, (h - ph) // 2
        box = pygame.Rect(x, y, pw, ph)
        self.pixel_panel(box, PANEL, (120, 170, 255), shadow=6, accent=(120, 170, 255), radius=2)
        self.text("SETTINGS", (x + 22, y + 18), TEXT, self.pixel_title)
        self.text("MAX SPEED", (x + 22, y + 66), FAINT, self.pixel_small)
        self.text(f"{game.race_speed_limit} mm/s", (x + 22, y + 88), TEXT, self.pixel)
        self.pixel_button("max_down", x + pw - 142, y + 76, 48, 38, "-", "", (110, 112, 130),
                          font=self.pixel)
        self.pixel_button("max_up", x + pw - 84, y + 76, 48, 38, "+", "", (110, 112, 130),
                          font=self.pixel)
        self.text("FAIR GAME", (x + 22, y + 126), FAINT, self.pixel_small)
        self.pixel_button("fair_toggle", x + pw - 142, y + 136, 106, 38,
                          "ON" if game.fair_game else "OFF", "H",
                          OK if game.fair_game else (70, 72, 84), font=self.pixel)
        self.text("R / R-stick: speed", (x + 22, y + 168), FAINT, self.small)
        self.text("H / LT+R-stick: fair", (x + 22, y + 186), FAINT, self.small)
        self.pixel_button("settings_close", x + pw - 142, y + 196, 106, 38, "CLOSE", "Esc",
                          (110, 112, 130), font=self.pixel)

    def draw_lobby(self, game: Game, sources: list, cursors: dict, claims: dict, loadouts: dict,
                   garage_cursors: dict, tab: str, settings_open: bool, status: str,
                   controls_cars: str, controls_garage: str) -> None:
        self.draw_menu_background()
        self.buttons = {}
        W, H = self.screen.get_size()
        sources = [s for s in sources if s.alive()]
        for s in sources:
            loadouts.setdefault(s, Loadout())
        palette = [(255, 214, 92), (120, 200, 255), (200, 150, 255), (140, 240, 170), (255, 160, 120)]
        src_color = {}
        for i, s in enumerate(sources):
            src_color[s] = getattr(s, "color", palette[i % len(palette)])

        title_rect = pygame.Rect(16, 8, 340, 44)
        self.machine_panel(title_rect, (20, 22, 28), (255, 176, 70), border=(58, 60, 72), shadow=4)
        self.text("OVERDRIVE ARENA", (28, 14), TEXT, self.pixel_title)
        pygame.draw.rect(self.screen, (60, 190, 220), (28, 47, 248, 2))
        track = game.track
        track_rect = pygame.Rect(W - 486, 16, 188, 44)
        self.pixel_panel(track_rect, (26, 28, 35), OK if track else WARN, shadow=3,
                         accent=OK if track else WARN, radius=2)
        track_label = f"TRACK  {len(track):02d} PCS" if track else "NO TRACK"
        self.text(track_label, (track_rect.x + 10, track_rect.y + 8), OK if track else WARN, self.pixel_small)
        self.text("SCAN TO LOAD" if not track else "MAP READY", (track_rect.x + 10, track_rect.y + 24),
                  DIM, self.small)
        self.pixel_button("settings", W - 286, 16, 128, 44, "SETTINGS", "F2",
                          (120, 170, 255), font=self.pixel)
        self.pixel_button("scan", W - 146, 16, 118, 44, "SCAN", "Y / T",
                          (120, 170, 255), font=self.pixel)

        self.step_tab(28, 72, 142, 40, "1", "CARS", "active" if tab == "cars" else "done")
        self.step_tab(180, 72, 166, 40, "2", "GARAGE", "active" if tab == "garage" else "todo")
        race_mode = game.mode == "race"
        self.pixel_button("mode", 366, 72, 154, 40, "RACE" if race_mode else "BATTLE",
                          "M / L3", (200, 150, 255) if race_mode else (255, 150, 90),
                          font=self.pixel)
        if race_mode:
            self.pixel_button("laps", 530, 72, 140, 40, f"{game.lap_target} LAPS", "Up/Down",
                              (110, 112, 130), font=self.pixel)

        now = time.monotonic()
        race_ok = game.can_start(claims.values())
        deck = pygame.Rect(16, H - 86, W - 32, 74)
        self.machine_panel(deck, (20, 22, 28), (255, 176, 70), border=(58, 60, 72), shadow=0)
        # console details: hazard strip, button wells and status LEDs
        for i, hx in enumerate(range(deck.x + 10, deck.right - 10, 12)):
            col = HAZARD if i % 2 == 0 else HAZARD_DARK
            pygame.draw.rect(self.screen, col, (hx, deck.y + 9, 8, 3))
        left_well = pygame.Rect(22, H - 74, 252 if tab == "garage" else 152, 56)
        right_well = pygame.Rect(W - 345, H - 79, 322, 62)
        for well in (left_well, right_well):
            pygame.draw.rect(self.screen, (7, 8, 11), well.move(2, 2))
            pygame.draw.rect(self.screen, (15, 16, 21), well)
            pygame.draw.rect(self.screen, (44, 46, 58), well, 1)
            pygame.draw.line(self.screen, (62, 66, 80), (well.x + 2, well.y + 2),
                             (well.right - 2, well.y + 2), 1)
        for i, led in enumerate(((110, 220, 130), (255, 176, 70), (120, 190, 255))):
            lx, ly = W - 396 + i * 13, H - 57
            pygame.draw.rect(self.screen, (6, 7, 10), (lx, ly, 9, 9))
            pygame.draw.rect(self.screen, led, (lx + 1, ly + 1, 7, 7))
            pygame.draw.rect(self.screen, led, (lx + 2, ly + 2, 2, 2))
            pygame.draw.rect(self.screen, (12, 13, 17), (lx, ly, 9, 9), 1)
        if tab == "cars":
            strip_y = H - 172
            content_bottom = strip_y - 12
            grid_top = 158
            self.text("CHOOSE YOUR CAR", (56, 128), FAINT, self.pixel)
            n = len(game.cars)
            if n:
                cols = 2 if n == 4 else (3 if n >= 3 else n)
                rows = (n + cols - 1) // cols
                gap = 18
                avail_w = W - 112
                avail_h = content_bottom - grid_top
                card_w = min(300, (avail_w - gap * (cols - 1)) // cols)
                card_h = min(230, (avail_h - gap * (rows - 1)) // rows)
                card_h = max(120, card_h)
                x0 = (W - (cols * card_w + gap * (cols - 1))) // 2
                y0 = grid_top + max(0, (avail_h - rows * card_h - gap * (rows - 1)) // 2)
                for i, c in enumerate(game.cars):
                    box = pygame.Rect(x0 + (i % cols) * (card_w + gap),
                                      y0 + (i // cols) * (card_h + gap), card_w, card_h)
                    owner = next((s for s, cc in claims.items() if cc is c), None)
                    hovering = [s for s in sources
                                if cursors.get(s, 0) == c.index and claims.get(s) is not c]
                    self._draw_car_card(game, c, box, owner, src_color.get(owner, DIM),
                                        hovering, src_color, now)
            else:
                self.text("NO CARS CONNECTED", (28, 190), WARN, self.pixel_big)
            self._draw_player_strip(game, sources, claims, src_color, strip_y, 60)
            self.pixel_button("quit", 28, H - 68, 140, 44, "QUIT", "Esc", (60, 62, 74),
                              font=self.pixel)
            self.text(controls_cars, (186, H - 55), FAINT, self.small)
            self.pixel_button("next", W - 340, H - 74, 312, 52, "NEXT: GARAGE",
                              "Start", (120, 170, 255) if race_ok else (60, 62, 74),
                              enabled=race_ok)
        else:
            content_bottom = H - 120
            self.text("GARAGE", (56, 128), FAINT, self.pixel)
            n = len(sources)
            if n:
                if n <= 1:
                    cols, rows = 1, 1
                elif n == 2:
                    cols, rows = 2, 1
                elif n == 3:
                    cols, rows = 3, 1
                elif n == 4:
                    cols, rows = 2, 2
                else:
                    cols, rows = 3, 2
                gap = 18
                avail_w = W - 112
                avail_h = content_bottom - 154
                panel_w = (avail_w - gap * (cols - 1)) // cols
                panel_h = (avail_h - gap * (rows - 1)) // rows
                if n == 1:
                    panel_w = min(820, avail_w)
                    x0 = (W - panel_w) // 2
                else:
                    x0 = 56
                y0 = 154
                for i, s in enumerate(sources):
                    box = pygame.Rect(x0 + (i % cols) * (panel_w + gap),
                                      y0 + (i // cols) * (panel_h + gap), panel_w, panel_h)
                    cursor = garage_cursors.get(s, (0, 0))
                    self._draw_garage_panel(game, s, box, claims, loadouts, cursor, src_color, now)
            else:
                self.text("NO CONTROLLERS", (28, 190), WARN, self.pixel_big)
            self.pixel_button("back", 28, H - 68, 240, 44, "BACK: CARS", "Esc / View",
                              (70, 72, 84), font=self.pixel)
            self.text(controls_garage, (286, H - 55), FAINT, self.small)
            self.pixel_button("race", W - 340, H - 74, 312, 52,
                              "START RACE" if race_mode else "START BATTLE",
                              "Start", OK if race_ok else (60, 62, 74),
                              enabled=race_ok)

        self._draw_status_toast(status, W, H)
        if settings_open:
            self._draw_settings_modal(game)

    def draw_scan(self, game: Game, scanners: dict, elapsed: float) -> None:
        self.draw_menu_background()
        self.draw_scan_map(scanners)
        pr = self.panel_rect()
        self.pixel_panel(pr, PANEL, (58, 60, 72), shadow=0, radius=2)
        x, y = pr.x + 18, pr.y + 14
        y += self.text("SCANNING TRACK", (x, y), TEXT, self.pixel_title) + 4
        y += self.text(f"elapsed {elapsed:3.0f}s", (x, y), DIM, self.small) + 14
        for c, sc in scanners.items():
            box = pygame.Rect(x, y, pr.width - 36, 60)
            self.pixel_panel(box, CARD, c.color, shadow=2, accent=c.color, radius=2)
            pygame.draw.rect(self.screen, c.color, (box.x, box.y, 6, box.height))
            self.text(c.name, (box.x + 18, box.y + 7), TEXT, self.pixel_big)
            with sc.lock:
                state = sc.state
                seen = sc.pieces_seen
            if state == "on the charger":
                col, label = (120, 200, 255), "CHARGER"
            elif state == "not on the track":
                col, label = WARN, "OFF TRACK"
            elif sc.done:
                col, label = OK, "DONE"
            elif sc.stopped:
                col, label = WARN, "STOPPED"
            elif seen == 0:
                col, label = DIM, "SEARCHING"
            else:
                col, label = TEXT, f"SCANNING · {seen} PCS"
            pygame.draw.rect(self.screen, col, (box.x + 18, box.y + 36, 9, 9))
            pygame.draw.rect(self.screen, (12, 13, 17), (box.x + 18, box.y + 36, 9, 9), 1)
            self.text(label, (box.x + 34, box.y + 33), col, self.small)
            y += box.height + 8
        self.text("Back / Esc: cancel", (x, pr.bottom - 30), FAINT, self.small)

    def draw_scan_map(self, scanners: dict) -> None:
        from anki.track import build_track, piece_type, UNKNOWN
        rect = self.map_rect()
        self.pixel_panel(rect, PANEL, (58, 60, 72), shadow=0, radius=2)
        evidence = next(iter(scanners.values())).evidence if scanners else None
        raw = evidence.preview() if evidence else []
        # Unknown geometry is explicitly a gap, never a guessed straight.
        prefix = []
        for row in raw:
            if row["id"] is None or piece_type(row["id"]) == UNKNOWN or (piece_type(row["id"]) == TURN and not row["turn"]):
                break
            prefix.append(row)
        self.text("LIVE TRACK RECONSTRUCTION", (rect.x + 18, rect.y + 16), TEXT, self.pixel_big)
        contributing = sum(sc.pieces_seen > 0 for sc in scanners.values())
        self.text(f"{len(raw)} pieces seen / {len(prefix)} placed / {contributing} cars reporting",
                  (rect.x + 18, rect.y + 48), DIM, self.small)
        if prefix:
            map_rect = pygame.Rect(rect.x + 12, rect.y + 78, rect.width - 24,
                                   max(80, rect.height - 102))
            signature = (tuple((r["id"], r["turn"], r["reversed"]) for r in prefix), tuple(map_rect))
            if getattr(self, "_scan_signature", None) != signature:
                self._scan_view = MapView(build_track(prefix), map_rect, margin=32)
                self._scan_signature = signature
            view = self._scan_view
            self.screen.blit(view.road(), map_rect)
            for index, row in enumerate(prefix):
                point = view.to_screen(*view.track.world_point(index, .5))
                pygame.draw.circle(self.screen, (25, 30, 40), point, 14)
                img = self.small.render(str(index + 1), True, TEXT)
                self.screen.blit(img, (point[0] - img.get_width() // 2, point[1] - img.get_height() // 2))
            end = view.to_screen(*view.track.world_point(len(prefix) - 1, 1))
            pygame.draw.circle(self.screen, GOLD, end, 7 + int(3 * (1 + math.sin(time.monotonic() * 4))))
        else:
            self.text("Waiting for start / finish codes...", (rect.x + 30, rect.centery), DIM, self.font)

    def draw_race(self, game: Game, controls: str, overlay: Optional[str] = None, sub: str = "") -> None:
        self.frame()
        self.buttons = {}
        mv = self.ensure_map(game.track)
        if mv:
            mv.draw(self.screen, game, self.font, self.small)
            pygame.draw.rect(self.screen, (58, 60, 72), self.map_rect(), 2)
        else:
            self.draw_no_track("No map: weapons and positions are disabled.\nGo back (Esc / Back) and scan the track.")
        pr = self.panel_rect()
        self.pixel_panel(pr, PANEL, (58, 60, 72), shadow=0, radius=2)
        x, y = pr.x + 16, pr.y + 12
        now = time.monotonic()
        race_mode = game.mode == "race"
        self.text("RACE" if race_mode else "BATTLE", (x, y), TEXT, self.pixel_title)
        if game.race_start_t:
            t = now - game.race_start_t
            self.text_right(f"{int(t // 60):02d}:{t % 60:05.2f}", pr.right - 16, y + 8, DIM, self.mono_big)
        y += 44
        ranks = {c.index: i + 1 for i, c in enumerate(game.ranking())}
        n = len(game.cars)
        h = min(96, max(78, (pr.height - 100) // max(1, n) - 8))
        for c in game.cars:
            box = pygame.Rect(x, y, pr.width - 32, h)
            dead = c.is_dead(now)
            self.pixel_panel(box, CARD, c.color, shadow=2, accent=c.color, radius=2)
            pygame.draw.rect(self.screen, c.color, (box.x, box.y, 6, h))
            rank_col = GOLD if ranks[c.index] == 1 else (70, 72, 84)
            pygame.draw.rect(self.screen, rank_col, (box.x + 18, box.y + 8, 24, 24))
            pygame.draw.rect(self.screen, (12, 13, 17), (box.x + 18, box.y + 8, 24, 24), 2)
            img = self.small.render(str(ranks[c.index]), True,
                                    (18, 18, 22) if ranks[c.index] == 1 else TEXT)
            self.screen.blit(img, (box.x + 30 - img.get_width() // 2, box.y + 14))
            self.text(c.name, (box.x + 50, box.y + 3), (120, 120, 126) if dead else TEXT, self.pixel_big)
            driver = c.source.short if c.source is not None else (
                f"AI · {c.ai_difficulty.title()}" if c.ai else "PARKED")
            self.text(driver, (box.x + 50, box.y + 27), DIM, self.small)
            status = self.car_status(game, c, now)
            if status:
                self.chip(box.x + 50 + self.small.size(driver)[0] + 10, box.y + 27,
                          status[0], status[1])
            bx, by, bw = box.x + 20, box.y + 46, box.width - 40
            if race_mode:
                self.text_right(f"LAP {c.laps} / {game.lap_target}", box.right - 18, box.y + 3,
                                TEXT, self.pixel_big)
                prog = c.loc.progress() if c.localized() and c.loc else None
                pieces = len(game.track.pieces) if game.track else 1
                frac = min(1.0, (c.laps + ((prog or 0.0) / pieces if prog is not None else 0.0))
                           / max(1, game.lap_target))
                pygame.draw.rect(self.screen, (40, 40, 48), (bx, by, bw, 9), border_radius=4)
                pygame.draw.rect(self.screen, c.color, (bx, by, int(bw * frac), 9), border_radius=4)
                self.battery_compact(c, box.right - 18, box.y + 62)
            else:
                self.text_right(f"HP {c.hp}", box.right - 18, box.y + 4, DIM, self.small)
                pygame.draw.rect(self.screen, (40, 40, 48), (bx, by, bw, 9), border_radius=4)
                pygame.draw.rect(self.screen, OK if c.hp > 40 else (255, 150, 90),
                                 (bx, by, int(bw * c.hp / MAX_HP), 9), border_radius=4)
                self.lives_pips(bx, by + 16, c.lives, c.color)
                self.text(f"KILLS {c.kills}", (bx + 160, by + 16), DIM, self.small)
                self.battery_compact(c, box.right - 18, by + 16)
            y += h + 8
        y = pr.bottom - 10 - 15 * (controls.count("\n") + 1)
        for line in controls.split("\n"):
            y += self.text(line, (x, y), FAINT, self.small) + 1
        if overlay:
            mr = self.map_rect()
            base = self.pixel_title.render(overlay, True, TEXT)
            factor = 3 if len(overlay) <= 6 else 2
            img = pygame.transform.scale(base, (base.get_width() * factor, base.get_height() * factor))
            simg = self.font.render(sub, True, DIM) if sub else None
            content_w = max(img.get_width(), simg.get_width() if simg else 0)
            content_h = img.get_height() + (simg.get_height() + 12 if simg else 0)
            shade = pygame.Surface((content_w + 80, content_h + 44), pygame.SRCALPHA)
            shade.fill((10, 10, 14, 180))
            ox, oy = mr.centerx - shade.get_width() // 2, mr.centery - shade.get_height() // 2
            self.screen.blit(shade, (ox, oy))
            ty = oy + 18
            self.screen.blit(img, (mr.centerx - img.get_width() // 2, ty))
            if simg:
                self.screen.blit(simg, (mr.centerx - simg.get_width() // 2,
                                        ty + img.get_height() + 6))

    def draw_message(self, title: str, lines: list[str]) -> None:
        self.frame()
        w, h = self.screen.get_size()
        self.text(title, (40, h // 2 - 60), TEXT, self.pixel_title)
        y = h // 2 - 10
        for l in lines:
            y += self.text(l, (40, y), DIM) + 4
        pygame.display.flip()
