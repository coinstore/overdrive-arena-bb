"""Anki Overdrive arena: drive the cars with Xbox / PS5 pads or the keyboard, see them live on a
scanned track map, and shoot each other with virtual weapons.

Flow:  connect to every car  ->  LOBBY (CARS / GARAGE tabs, scan the track)
       ->  RACE (Back/Esc returns to the lobby)

Gamepad (Xbox names; PS5: Cross=A, Circle=B, Square=X, Triangle=Y, Share=Back, Options=Start):
  RT / LT             accelerate / decelerate   Left stick X   change lane (4 lanes)
                      (releasing holds speed)  D-pad up/down  speed limit +/- 100
  LB / RB, D-pad L/R  snap one lane
  X                   use weapon                A              powerup (U-turn)
  B                   emergency stop            Y              unused in a race
  Start               start / ready             Back           back / quit
  LB / RB             switch CARS / GARAGE       LT + L-stick   settings (lobby)

Keyboard:  W/S accelerate/decelerate (holds)  A/D steer  Q/E lane  F weapon  U powerup  Space stop
           +/- limit   Enter claim/equip   Tab AI/park   T scan track   Esc back/quit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import pygame
from pygame._sdl2 import controller as sdlc

from anki import protocol as P
from anki.ble_worker import BleWorker
from anki.track import Track, TrackScanner, ScanEvidence
from anki.vehicle import Vehicle, discover
from game import inputs as I
from game.game import CarState, Game, Loadout, PLAYER_COLORS, POWERUPS, WEAPONS
from game.render import Renderer

log = logging.getLogger("app")

TICK_HZ = 50
POSITION_LEAD_S = float(os.environ.get("ARENA_POSITION_LEAD", "0.03"))  # measured ping RTT/2 is ~27 ms
TRACK_FILE = Path(__file__).parent / "track.json"
SCAN_SPEED = 450
SCAN_SPEEDS = (450, 500, 400)
SCAN_TIMEOUT_S = 90
SCAN_NO_DATA_S = 8.0

PAIRING, SCANNING, GRID, READY, COUNTDOWN, RACE = "pairing", "scanning", "grid", "ready", "countdown", "race"

CONTROLS_CARS = "A/Enter choose · B release · X/Tab AI · Y/T scan · LB/RB tab"
CONTROLS_GARAGE = "A/Enter equip · Up/Down slot · Left/Right item · LB/RB tab"
CONTROLS_RACE = ("pad: R2 gas · L2 brake · stick lanes\n"
                 "X weapon · A powerup · B stop\n"
                 "key: W/S · A/D · Q/E · F/U · Space · Esc")
CONTROLS_RACE_NO_WEAPON = ("pad: R2 gas · L2 brake · stick lanes\n"
                           "A powerup · B stop · Back: lobby\n"
                           "key: W/S · A/D · Q/E · U · Space · Esc")


class App:
    def __init__(self, args):
        self.args = args
        self.ble = BleWorker()
        # Set before SDL initializes its joystick/controller subsystems.
        os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
        pygame.init()
        sdlc.init()
        self.screen = pygame.display.set_mode((1280, 760), pygame.RESIZABLE)
        pygame.display.set_caption("Anki Overdrive arena")
        self.renderer = Renderer(self.screen)
        self.keyboard = I.Keyboard()
        self.pads: list[I.Pad] = [] if args.keyboard_only else I.list_pads()
        self.cars: list[CarState] = []
        self.game: Optional[Game] = None
        self.state = PAIRING
        self.setup_tab = "cars"
        self.cursors: dict[I.InputSource, int] = {}
        self.garage_cursors: dict[I.InputSource, tuple[int, int]] = {}
        self.loadouts: dict[I.InputSource, Loadout] = {}
        self.claims: dict[I.InputSource, CarState] = {}
        self.status = ""
        self.settings_open = False
        self.scanners: dict[CarState, TrackScanner] = {}
        self.scan_started = 0.0
        self.identified: dict[int, bool] = {}
        self.click: Optional[tuple[int, int]] = None
        self.lap_choices = (3, 5, 10, 20)
        for source in self.sources:
            self.garage_cursors[source] = (0, 0)
            self.loadouts[source] = Loadout()

    # ------------------------------------------------------------ setup
    @property
    def sources(self) -> list[I.InputSource]:
        return [*self.pads, self.keyboard]

    def connect_cars(self) -> bool:
        a = self.args
        self.renderer.draw_message("Scanning for cars...", [f"{a.scan:.0f} s BLE scan", "cars must be on and off their charger"])
        found = self.ble.call(discover(a.scan), timeout=a.scan + 10)
        if a.car:
            wanted = [x.lower() for x in a.car]
            found = [(d, m) for d, m in found if d.address.lower() in wanted]
        found = found[: a.max_cars]
        if not found:
            self.renderer.draw_message("No Anki cars found", ["Power a car on and take it off the charger, then restart."])
            time.sleep(3)
            return False
        self.renderer.draw_message("Connecting...", [f"{m} ({d.address})" for d, m in found])

        async def connect(dev, model) -> Optional[Vehicle]:
            car = Vehicle(dev, model)
            try:
                await car.connect()
                await car.request_status()
                return car
            except Exception as e:  # noqa: BLE001
                log.error("%s (%s) failed: %s", model, dev.address, e)
                return None

        async def connect_all():
            vehicles = []
            for device, model in found:
                car = await connect(device, model)
                if car is not None:
                    vehicles.append(car)
            return vehicles

        vehicles = self.ble.call(connect_all(), timeout=20 * len(found) + 10)
        if not vehicles:
            self.renderer.draw_message("Could not connect to any car", [])
            time.sleep(3)
            return False
        for i, v in enumerate(vehicles):
            cs = CarState(v, i, self.ble, self.args.max_speed if self.args.max_speed is not None else 800)
            cs.position_lead_s = POSITION_LEAD_S
            cs.player_max_speed = self.args.player_max_speed
            cs.ai_difficulty = self.args.ai_difficulty
            cs.extreme_max_speed = self.args.max_speed if self.args.max_speed is not None else 2000
            v.on_timed_message = cs.enqueue_message
            self.cars.append(cs)
        track = Track.load(TRACK_FILE)
        if track:
            log.info("loaded %s: %s", TRACK_FILE.name, track.describe())
        self.game = Game(self.ble, self.cars, track, ai_enabled=not self.args.no_ai, ai_speed=self.args.ai_speed)
        self.game.mode = self.args.mode
        self.game.lap_target = self.args.laps
        self.game.set_race_speed_limit(self.args.max_speed if self.args.max_speed is not None else 2000)
        self.assign_player_colors(send_lights=False)
        for cs in self.cars:
            cs.lights_idle()
        for s in self.sources:
            self.cursors[s] = 0
        return True

    # ------------------------------------------------------------ lobby
    def assign_player_colors(self, send_lights: bool = True) -> None:
        """One colour per player: a claimed car takes its driver's colour.

        Unclaimed cars (AI / parked) take the next free palette entry so two
        cars never share a roof colour.  The map, HUD, cards and roof light all
        read CarState.color, so this is the single source of truth.
        """
        used: set[int] = set()
        claimed = {id(car) for car in self.claims.values()}
        for source, car in self.claims.items():
            index = int(getattr(source, "color_index", 0)) % len(PLAYER_COLORS)
            used.add(index)
            if car.set_player_color(index) and send_lights:
                car.set_roof_color()
        for car in self.cars:
            if id(car) in claimed:
                continue
            # Keep an unclaimed car's existing colour when it is still free;
            # only shift AI/parked cars that would collide with a player.
            index = car.color_index
            if index in used:
                index = next((i for i in range(len(PLAYER_COLORS)) if i not in used),
                             car.index % len(PLAYER_COLORS))
            used.add(index)
            if car.set_player_color(index) and send_lights:
                car.set_roof_color()

    def loadout_for(self, source: I.InputSource) -> Loadout:
        return self.loadouts.setdefault(source, Loadout())

    def garage_rows(self) -> list[tuple[str, tuple[str, ...]]]:
        """Rows shown in the garage: weapons first in battle, then powerups."""
        g = self.game
        assert g is not None
        rows: list[tuple[str, tuple[str, ...]]] = []
        if g.mode == "battle":
            rows.append(("weapon", WEAPONS))
        rows.append(("powerup", POWERUPS))
        return rows

    def garage_cursor(self, source: I.InputSource) -> tuple[int, int]:
        rows = self.garage_rows()
        row, col = self.garage_cursors.get(source, (0, 0))
        row %= max(1, len(rows))
        col %= max(1, len(rows[row][1]))
        self.garage_cursors[source] = (row, col)
        return row, col

    def move_garage_cursor(self, source: I.InputSource, dx: int, dy: int) -> None:
        rows = self.garage_rows()
        row, col = self.garage_cursor(source)
        row = (row + dy) % len(rows)
        col = (col + dx) % len(rows[row][1])
        self.garage_cursors[source] = (row, col)

    def equip_garage(self, source: I.InputSource) -> None:
        g = self.game
        assert g is not None
        rows = self.garage_rows()
        row, col = self.garage_cursor(source)
        kind, items = rows[row]
        item = items[col]
        loadout = self.loadout_for(source)
        if kind == "weapon":
            loadout.weapon = item
        else:
            loadout.powerup = item
        self.status = f"{source.short}: {loadout.weapon_name() if kind == 'weapon' else loadout.powerup_name()} equipped"
        source.rumble(0.25, 0.35, 80)

    def open_settings(self) -> None:
        self.settings_open = True
        self.status = "settings: R / R-stick speed   H / LT+R-stick fair"

    def close_settings(self) -> None:
        self.settings_open = False
        self.status = ""

    def cycle_speed(self, step: int) -> None:
        g = self.game
        assert g is not None
        value = g.race_speed_limit + step
        if value > 2000:
            value = 200
        elif value < 200:
            value = 2000
        g.set_race_speed_limit(value)

    def toggle_fair(self) -> None:
        g = self.game
        assert g is not None
        g.fair_game = not g.fair_game

    def handle_setup_click(self, pos: tuple[int, int]) -> Optional[bool]:
        g = self.game
        assert g is not None
        btns = self.renderer.buttons
        if self.settings_open:
            if "settings_close" in btns and btns["settings_close"].collidepoint(pos):
                self.close_settings()
            elif "max_down" in btns and btns["max_down"].collidepoint(pos):
                self.cycle_speed(-200)
            elif "max_up" in btns and btns["max_up"].collidepoint(pos):
                self.cycle_speed(200)
            elif "fair_toggle" in btns and btns["fair_toggle"].collidepoint(pos):
                self.toggle_fair()
            return None
        if "settings" in btns and btns["settings"].collidepoint(pos):
            self.open_settings()
        elif "tab_cars" in btns and btns["tab_cars"].collidepoint(pos):
            self.setup_tab = "cars"
        elif "tab_garage" in btns and btns["tab_garage"].collidepoint(pos):
            self.setup_tab = "garage"
        elif "scan" in btns and btns["scan"].collidepoint(pos):
            self.start_scan_all()
            return True
        elif "race" in btns and btns["race"].collidepoint(pos):
            if g.can_start(self.claims.values()):
                self.start_race()
                return True
            self.status = "assign a player or AI to a connected car off its charger"
        elif "mode" in btns and btns["mode"].collidepoint(pos):
            self.toggle_mode()
        elif "laps" in btns and btns["laps"].collidepoint(pos):
            i = self.lap_choices.index(g.lap_target) if g.lap_target in self.lap_choices else -1
            g.lap_target = self.lap_choices[(i + 1) % len(self.lap_choices)]
        elif "quit" in btns and btns["quit"].collidepoint(pos):
            return False
        return None

    def pairing_tick(self) -> bool:
        g = self.game
        assert g is not None
        n = len(self.cars)
        sources = [s for s in self.sources if s.alive()]

        if self.click is not None:
            pos, self.click = self.click, None
            result = self.handle_setup_click(pos)
            if result is False:
                return False
            if result is True:
                return True

        if self.settings_open:
            for s in sources:
                if s.pressed(I.BACK) or s.pressed(I.SETTINGS) or s.pressed(I.READY):
                    self.close_settings()
                    return True
                if s.pressed(I.SECONDARY):
                    self.toggle_fair() if s.brake() > .5 else self.cycle_speed(200)
                if s.pressed(I.FAIR_GAME):
                    self.toggle_fair()
            return True

        if any(s.pressed(I.BACK) for s in sources):
            if self.setup_tab == "garage":
                self.setup_tab = "cars"
                self.status = "back to car selection"
                return True
            return False
        if any(s.pressed(I.SETTINGS) for s in sources):
            self.open_settings()
            return True
        if any(s.pressed(I.SCAN) for s in sources):
            self.start_scan_all()
            return True
        if any(s.pressed(I.TAB_LEFT) or s.pressed(I.TAB_RIGHT) for s in sources):
            self.setup_tab = "garage" if self.setup_tab == "cars" else "cars"
            return True
        if any(s.pressed(I.READY) for s in sources):
            if not g.can_start(self.claims.values()):
                self.status = "assign a player or AI to a connected car off its charger"
            else:
                self.start_race()
            return True

        for s in sources:
            if s.pressed(I.TOGGLE_MODE):
                if s.brake() > .5:
                    self.open_settings()
                    return True
                self.toggle_mode()
            if s.pressed(I.SECONDARY):
                self.toggle_fair() if s.brake() > .5 else self.cycle_speed(200)
            if s.pressed(I.FAIR_GAME):
                self.toggle_fair()

        if self.setup_tab == "garage":
            for s in sources:
                if s.pressed(I.MENU_LEFT):
                    self.move_garage_cursor(s, -1, 0)
                if s.pressed(I.MENU_RIGHT):
                    self.move_garage_cursor(s, 1, 0)
                if s.pressed(I.MENU_UP):
                    self.move_garage_cursor(s, 0, -1)
                if s.pressed(I.MENU_DOWN):
                    self.move_garage_cursor(s, 0, 1)
                if s.pressed(I.SELECT):
                    self.equip_garage(s)
            self.update_identify_lights()
            return True

        for s in sources:
            if s.pressed(I.MENU_LEFT):
                self.cursors[s] = (self.cursors.get(s, 0) - 1) % n
            if s.pressed(I.MENU_RIGHT):
                self.cursors[s] = (self.cursors.get(s, 0) + 1) % n
            car = self.cars[self.cursors.get(s, 0)]
            if s.pressed(I.SELECT):
                owner = next((o for o, c in self.claims.items() if c is car), None)
                if owner is s:
                    del self.claims[s]
                    self.assign_player_colors()
                    self.status = f"{s.short} released {car.name}"
                elif owner is not None:
                    self.status = f"{car.name} is already driven by {owner.short}"
                else:
                    self.claims.pop(s, None)          # one car per controller
                    self.claims[s] = car
                    self.assign_player_colors()
                    self.status = f"{s.short} ({s.name}) drives {car.name}"
                    s.rumble(0.5, 0.5, 200)
            if s.pressed(I.RELEASE) and s in self.claims:
                released = self.claims.pop(s)
                self.assign_player_colors()
                self.status = f"{s.short} released {released.name}"
            if s.pressed(I.TOGGLE_AI):
                if car in self.claims.values():
                    self.status = f"{car.name} has a player; release it first"
                elif not g.ai_enabled:
                    self.status = "AI disabled by --no-ai; restart without that option to enable it"
                else:
                    car.cycle_ai()
                    self.status = f"{car.name}: AI {car.ai_difficulty.title()}" if car.ai_pref else f"{car.name}: parked"
            if s.pressed(I.LIMIT_UP) or s.pressed(I.LIMIT_DOWN):
                step = 1 if s.pressed(I.LIMIT_UP) else -1
                index = self.lap_choices.index(g.lap_target) if g.lap_target in self.lap_choices else 0
                g.lap_target = self.lap_choices[(index + step) % len(self.lap_choices)]
        self.update_identify_lights()
        return True

    def toggle_mode(self) -> None:
        g = self.game
        assert g is not None
        g.mode = "race" if g.mode == "battle" else "battle"
        for s in self.sources:
            self.garage_cursor(s)
        self.status = ("RACE mode: laps only, powerups only" if g.mode == "race"
                       else "BATTLE mode: weapons, powerups and lives")

    def update_identify_lights(self) -> None:
        """Claimed cars keep their headlights on; in the car tab a hovered car blinks."""
        blink = int(time.monotonic() * 1.5) % 2 == 0
        for c in self.cars:
            hovered = (self.setup_tab == "cars"
                       and any(idx == c.index for s, idx in self.cursors.items()
                               if s.alive() and self.claims.get(s) is not c))
            claimed = c in self.claims.values()
            want = claimed or (hovered and blink)
            if self.identified.get(c.index) != want:
                self.identified[c.index] = want
                c.lights_identify(want)

    def start_race(self) -> None:
        g = self.game
        assert g is not None
        for c in self.cars:
            c.source = None
        for s, c in self.claims.items():
            c.source = s
            c.loadout = self.loadout_for(s)
        self.assign_player_colors(send_lights=False)
        self.identified.clear()
        self.settings_open = False
        self.click = None
        g.begin_grid()
        self.state = GRID
        self.status = ""

    def grid_tick(self, dt: float) -> None:
        g = self.game
        assert g is not None
        self.click = None
        if any(s.pressed(I.BACK) for s in self.sources):
            g.stop_all()
            self.identified.clear()
            self.state = PAIRING
            self.status = "grid cancelled (cars stopped)"
            return
        if g.grid_tick(dt):
            placed, total = g.grid_summary()
            if placed == 0:
                g.stop_all()
                self.identified.clear()
                self.state = PAIRING
                self.status = "no car reached the start line - are they on the track?"
                return
            # Hold the field on the line until the start button is pressed
            # again; only then begin the 3-2-1 countdown.
            self.state = READY
            self.status = "press Start to race"

    def ready_tick(self, dt: float) -> None:
        g = self.game
        assert g is not None
        self.click = None
        if any(s.pressed(I.BACK) for s in self.sources):
            g.stop_all()
            self.identified.clear()
            self.state = PAIRING
            self.status = "start cancelled (cars stopped)"
            return
        g.tick(dt)   # keep localizers advancing while the field waits
        auto = bool(getattr(getattr(self, "args", None), "autostart", False))
        if auto or any(s.pressed(I.READY) for s in self.sources):
            g.begin_countdown()
            self.state = COUNTDOWN

    def countdown_tick(self, dt: float) -> None:
        g = self.game
        assert g is not None
        self.click = None
        if any(s.pressed(I.BACK) for s in self.sources):
            g.stop_all()
            self.identified.clear()
            self.state = PAIRING
            self.status = "start cancelled (cars stopped)"
            return
        g.tick(dt)
        g.countdown_feedback()
        if g.countdown_left() <= 0:
            g.start()
            self.state = RACE

    # ------------------------------------------------------------ scanning
    def start_scan_all(self) -> None:
        """Cars share start-aligned scan evidence until a complete loop closes."""
        g = self.game
        assert g is not None
        g.stop_all()
        self.click = None
        self.scanners = {}
        evidence = ScanEvidence()
        for c in self.cars:
            if not c.car.connected:
                continue
            sc = TrackScanner(evidence, c.name)
            self.scanners[c] = sc
            if c.charging:
                sc.state = "on the charger"
                sc.stopped = True
                continue
            c.extra_listener = sc.on_message
            c.lights_idle()
            c.dead_until = 0
            sc.commanded_speed = SCAN_SPEED
            c.command_speed(SCAN_SPEED, time.monotonic(), force=True)
        if not any(sc.state != "on the charger" for sc in self.scanners.values()):
            self.status = "every car is on its charger - take one off and put it on the track to scan"
            self.scanners = {}
            return
        self.scan_started = time.monotonic()
        self.state = SCANNING
        log.info("scanning track with %s", ", ".join(c.name for c in self.scanners))

    def _end_scan(self) -> None:
        for c, _sc in self.scanners.items():
            c.extra_listener = None
            c.hard_stop(time.monotonic())
        self.state = PAIRING
        self.click = None
        self.identified.clear()

    def scanning_tick(self) -> None:
        g = self.game
        assert g is not None
        cancel = any(s.pressed(I.BACK) for s in self.sources)
        now = time.monotonic()
        elapsed = now - self.scan_started
        # cars that read nothing after a while are not on the rails: stop them, keep scanning with the others
        for c, sc in self.scanners.items():
            if sc.state == "on the charger":
                continue
            if not c.car.connected or c.charging:
                sc.state = "disconnected" if not c.car.connected else "on the charger"
                sc.stopped = True
            if (elapsed > SCAN_NO_DATA_S and sc.pieces_seen == 0) or (sc.last_position_t and now - sc.last_position_t > 3.5):
                sc.state = "not on the track" if sc.pieces_seen == 0 else "lost track codes - stopped"
                sc.stopped = True
            if sc.stopped:
                if not getattr(sc, "stop_sent", False):
                    c.hard_stop(now)
                    sc.commanded_speed = 0
                    sc.stop_sent = True
                continue
            speed = SCAN_SPEEDS[max(sc.laps_tried, int(elapsed / 20)) % len(SCAN_SPEEDS)]
            if speed != sc.commanded_speed:
                sc.commanded_speed = speed
                c.command_speed(speed, now, force=True)
                log.info("%s scan retry: %d mm/s (%s)", c.name, speed, sc.last_retry)
        winner = next(((c, sc) for c, sc in self.scanners.items() if sc.done and sc.track), None)
        all_dead = all(sc.stopped for sc in self.scanners.values())
        if winner or cancel or all_dead or elapsed > SCAN_TIMEOUT_S:
            off = [c.name for c, sc in self.scanners.items() if sc.state == "not on the track"]
            charging = [c.name for c, sc in self.scanners.items() if sc.state == "on the charger"]
            self._end_scan()
            if winner:
                c, sc = winner
                sc.track.save(TRACK_FILE)  # type: ignore[union-attr]
                g.set_track(sc.track)
                contributors = ", ".join(sorted(sc.evidence.contributors)) or c.name
                self.status = f"combined scan ({contributors}): {len(sc.track)} pieces"  # type: ignore[arg-type]
                if off:
                    self.status += f"   -   NOT on the track: {', '.join(off)}"
                if charging:
                    self.status += f"   -   on the charger: {', '.join(charging)}"
                log.info("track: %s", sc.track.describe())  # type: ignore[union-attr]
            elif all_dead:
                self.status = "scan stopped: replace cars on the track and press Y / T to retry"
            else:
                self.status = "scan cancelled" if cancel else "scan timed out without a closed lap"

    # ------------------------------------------------------------ race
    def race_tick(self, dt: float) -> None:
        g = self.game
        assert g is not None
        self.click = None   # the race screen has no clickable controls
        for s in self.sources:
            if s.pressed(I.BACK):
                g.stop_all()
                self.assign_player_colors()
                self.identified.clear()
                self.state = PAIRING
                self.status = "back to lobby (cars stopped)"
                return
            if g.finished and s.pressed(I.READY):
                g.stop_all()
                self.start_race()
                return
        g.tick(dt)
        if g.finished and g.finish_celebration_t and time.monotonic() - g.finish_celebration_t > 14.0:
            winner = g.winner
            mode = g.mode
            g.stop_all()
            self.assign_player_colors()
            self.identified.clear()
            self.state = PAIRING
            if winner:
                self.status = f"{winner.name} won the {'battle' if mode == 'battle' else 'race'} ({winner.driver_label()})"
            else:
                self.status = "race over" if mode == "race" else "battle over"

    # ------------------------------------------------------------ main loop
    def handle_events(self) -> bool:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                self.click = ev.pos
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_F11:
                    pygame.display.toggle_fullscreen()
                else:
                    self.keyboard.feed_keydown(ev.key)
            elif ev.type == pygame.CONTROLLERDEVICEADDED:
                try:
                    if sdlc.is_controller(ev.device_index):
                        used = {int(p.short[1:]) for p in self.pads
                                if p.short.startswith("P") and p.short[1:].isdigit()}
                        number = next((n for n in range(1, len(PLAYER_COLORS) + 1) if n not in used),
                                      len(self.pads) + 1)
                        pad = I.Pad(ev.device_index, number)
                        if all(p.instance_id != pad.instance_id for p in self.pads):
                            self.pads.append(pad)
                            self.cursors[pad] = 0
                            self.garage_cursors[pad] = (0, 0)
                            self.loadouts[pad] = Loadout()
                            self.status = f"controller connected: {pad.name}"
                        else:
                            pad.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("controller add failed: %s", e)
            elif ev.type == pygame.CONTROLLERDEVICEREMOVED:
                for p in self.pads:
                    if p.instance_id == ev.instance_id:
                        p.mark_removed()
                        self.cursors.pop(p, None)
                        self.loadouts.pop(p, None)
                        self.garage_cursors.pop(p, None)
                        car = self.claims.pop(p, None)
                        if car is not None:
                            car.source = None
                            car.hard_stop(time.monotonic())
                            self.assign_player_colors()
                            self.identified.pop(car.index, None)
                            self.status = f"{p.name} disconnected, {car.name} stopped"
        return True

    def run(self) -> int:
        if not self.connect_cars():
            self.shutdown()
            return 1
        assert self.game is not None
        frame_period = 1.0 / TICK_HZ
        dt = frame_period
        t_start = time.monotonic()
        previous_tick = t_start - frame_period
        last_batt = t_start
        if self.args.autostart:
            self.start_race()
        if self.args.autoscan:
            self.start_scan_all()
        running = True
        try:
            while running:
                t0 = time.monotonic()
                dt = min(0.25, max(0.0, t0 - previous_tick))
                previous_tick = t0
                running = self.handle_events()
                for car in self.cars:
                    car.drain_messages()
                for s in self.sources:
                    if s.alive():
                        s.poll(dt)
                if self.state == PAIRING:
                    running = running and self.pairing_tick()
                elif self.state == SCANNING:
                    self.scanning_tick()
                    self.game.tick(dt)  # keeps localizers advancing, no driving (not running)
                elif self.state == GRID:
                    self.grid_tick(dt)
                elif self.state == READY:
                    self.ready_tick(dt)
                elif self.state == COUNTDOWN:
                    self.countdown_tick(dt)
                elif self.state == RACE:
                    self.race_tick(dt)
                self.keyboard.end_frame()
                self.maybe_reconnect(t0)
                if t0 - last_batt > 4:
                    for c in self.cars:
                        self.ble.fire(c.car.send(P.battery_request()))
                    last_batt = t0
                self.draw()
                if self.args.shots:
                    self._shot(t0)
                if self.args.run_for and t0 - t_start > self.args.run_for:
                    if self.args.screenshot:
                        pygame.image.save(self.screen, self.args.screenshot)
                    running = False
                time.sleep(max(0.0, frame_period - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def maybe_reconnect(self, now: float) -> None:
        """Cars occasionally drop the link (they reboot); try to get them back every few seconds."""
        if any(getattr(c, "_reconnecting", False) for c in self.cars):
            return  # one reconnect (which scans) at a time, so we do not starve the other links
        for c in self.cars:
            if c.car.connected or now - getattr(c, "_reconnect_t", 0.0) < 5.0:
                continue
            c._reconnecting = True
            c._reconnect_t = now
            self.status = f"reconnecting {c.name}..."

            async def task(cs=c):
                try:
                    await cs.car.reconnect()
                    await cs.car.request_status()
                    cs.hard_stop(time.monotonic())
                    cs.lights_idle()
                    log.info("reconnected %s", cs.name)
                except Exception as e:  # noqa: BLE001
                    log.warning("reconnect %s failed: %s", cs.name, e)
                finally:
                    cs._reconnecting = False

            self.ble.fire(task())

    def _shot(self, now: float) -> None:
        last_state = getattr(self, "_shot_state", None)
        last_t = getattr(self, "_shot_t", 0.0)
        if self.state != last_state or now - last_t > 4.0:
            self._shot_state, self._shot_t = self.state, now
            n = getattr(self, "_shot_n", 0) + 1
            self._shot_n = n
            Path(self.args.shots).mkdir(parents=True, exist_ok=True)
            pygame.image.save(self.screen, str(Path(self.args.shots) / f"{n:03d}_{self.state}.png"))

    def draw(self) -> None:
        g = self.game
        assert g is not None
        controls = CONTROLS_RACE if g.mode == "battle" else CONTROLS_RACE_NO_WEAPON
        if self.state == PAIRING:
            self.renderer.draw_lobby(g, self.sources, self.cursors, self.claims, self.loadouts,
                                     self.garage_cursors, self.setup_tab, self.settings_open,
                                     self.status, CONTROLS_CARS, CONTROLS_GARAGE)
        elif self.state == SCANNING:
            self.renderer.draw_scan(g, self.scanners, time.monotonic() - self.scan_started)
        elif self.state == GRID:
            placed, total = g.grid_summary()
            self.renderer.draw_race(g, controls, overlay=f"TO THE START LINE  {placed}/{total}",
                                    sub="cars drive themselves to the grid  -  Back / Esc cancels")
        elif self.state == READY:
            self.renderer.draw_race(g, controls, overlay="PRESS START",
                                    sub="Start / Options / Space  -  Back / Esc cancels")
        elif self.state == COUNTDOWN:
            left = g.countdown_left()
            self.renderer.draw_race(g, controls, overlay=f"{int(left) + 1}" if left > 0 else "GO!", sub="get ready")
        elif g.finished and g.winner is not None:
            if g.mode == "battle":
                sub = (f"{g.winner.kills} kills  -  Start / Options: battle again  -  Back: menu")
            else:
                sub = f"{g.lap_target} laps  -  Start / Options: race again  -  Back: menu"
            self.renderer.draw_race(g, controls, overlay=f"{g.winner.name} WINS", sub=sub)
        else:
            self.renderer.draw_race(g, controls)
        pygame.display.flip()

    def shutdown(self) -> None:
        log.info("stopping cars...")

        async def stop_all():
            await asyncio.gather(*(c.car.disconnect() for c in self.cars), return_exceptions=True)

        try:
            if self.cars:
                self.ble.call(stop_all(), timeout=10)
        except Exception as e:  # noqa: BLE001
            log.error("shutdown: %r", e)
        for s in self.sources:
            s.close()
        pygame.quit()
        self.ble.stop()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--max-speed", type=int, default=None, help="AI speed ceiling in mm/s (default 800; Extreme automatically uses up to 2000)")
    ap.add_argument("--player-max-speed", type=int, choices=range(200, 2001), metavar="200..2000", default=2000,
                    help="initial controller/keyboard speed limit (default 2000)")
    ap.add_argument("--max-cars", type=int, default=4)
    ap.add_argument("--scan", type=float, default=5.0, help="BLE scan duration in seconds")
    ap.add_argument("--car", action="append", help="only use this car address (repeatable)")
    ap.add_argument("--keyboard-only", action="store_true", help="ignore gamepads")
    ap.add_argument("--ai-difficulty", choices=("easy", "normal", "hard", "extreme"), default="normal", help="initial AI level for every car")
    ap.add_argument("--no-ai", action="store_true", help="unclaimed cars stay parked instead of being driven by AI")
    ap.add_argument("--mode", choices=["battle", "race"], default="battle", help="battle = weapons and kills; race = laps only (default battle)")
    ap.add_argument("--laps", type=int, default=5, help="race mode: laps to win (default 5)")
    ap.add_argument("--ai-speed", type=int, default=None, help="override AI normal straight speed mm/s (default: scales with car limit; Hard uses full limit)")
    ap.add_argument("--accel", type=int, default=450, help="acceleration mm/s^2 (default 450, matching the player ramp; higher may reboot weak batteries)")
    ap.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--autoscan", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--run-for", type=float, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--screenshot", help=argparse.SUPPRESS)
    ap.add_argument("--shots", help=argparse.SUPPRESS)   # directory: save a PNG on every state change and every 4 s
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


if __name__ == "__main__":
    a = parse_args()
    import game.game as _gg
    _gg.ACCEL_UP = a.accel
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("bleak").setLevel(logging.WARNING)
    sys.exit(App(a).run())
