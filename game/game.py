"""Game logic: per-car driving (human or AI), virtual weapons, damage and respawn."""
from __future__ import annotations

import logging
import math
from pathlib import Path
from queue import Empty, SimpleQueue
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from anki import protocol as P
from anki.ble_worker import BleWorker
from anki.track import CarLocalizer, Track, STRAIGHT, START, CRISSCROSS, TURN, START_ID, FINISH_ID
from anki.vehicle import Vehicle
from . import inputs as I
from .battery import BatteryMonitor
from .records import Records

log = logging.getLogger("game")

WARN_COLOR = (255, 176, 70)
PLAYER_COLORS = [(255, 90, 90), (90, 190, 255), (120, 240, 120), (255, 220, 90), (230, 130, 255), (255, 160, 70)]
ENGINE_COLORS = [(14, 0, 0), (0, 4, 14), (0, 14, 0), (14, 10, 0), (12, 0, 14), (14, 5, 0)]
LANES = (-68.0, -23.0, 23.0, 68.0)
MAX_ABS_OFFSET = 68.0


def grid_lane_order(count: int) -> tuple[float, ...]:
    """Spread fewer cars across the available lanes so their bodies do not touch."""
    if count <= 1:
        return (LANES[1],)
    if count == 2:
        return (LANES[0], LANES[2])
    if count == 3:
        return (LANES[0], LANES[2], LANES[3])
    return LANES

SPEED_SEND_MIN_DELTA = 15
SPEED_KEEPALIVE_S = 0.5
SPEED_SEND_INTERVAL_S = 0.05     # 20 Hz keeps the ramp smooth while still leaving BLE room for position fixes
PLAYER_ACCEL_RATE = 450.0        # mm/s^2 at full R2/W; analog trigger travel scales this
PLAYER_BRAKE_RATE = 900.0        # mm/s^2 at full L2/S
MIN_PLAYER_SPEED = 300           # below this the cars stutter and miss track codes
LANE_SEND_MIN_DELTA = 3.0
LANE_SEND_INTERVAL_S = 0.1
LANE_SYNC_IDLE_S = 1.5
STEER_LANE_THRESHOLD = 0.5   # stick deflection that requests the next lane; one step per push
# gentle acceleration: hard motor current on tired batteries browns out the BLE radio (cars reboot)
ACCEL_UP = 600
ACCEL_DOWN = 1500
ACCEL_STOP = 2500

MAX_HP = 100
BLASTER_RANGE_MM = 1400.0
BLASTER_COOLDOWN_S = 1.0
BLASTER_DAMAGE = 20
BLASTER_SLOW_FACTOR = 0.45
BLASTER_SLOW_S = 1.5
MINE_COOLDOWN_S = 4.0
MINE_MAX_ACTIVE = 2
MINE_LIFE_S = 45.0
MINE_DAMAGE = 35
MINE_STUN_S = 1.2
MINE_TRIGGER_MM = 120.0
MINE_LANE_MM = 35.0
MINE_ARM_S = 1.0
DEATH_STOP_S = 3.0
GRID_SPEED = 320            # speed used to drive to the start line
GRID_RETRY_SPEED = 500      # a missed grid attempt costs a full lap; still quick, but lane changes stay reliable
GRID_APPROACH_FRAC = 0.0    # crawl from the start of the last piece, so deceleration is early and even
GRID_CRAWL = 250            # low, but still reads the middle start code reliably
GRID_RECOVERY_DRIVE_S = 1.5  # bounded slow search, followed by a pause before retrying
GRID_RECOVERY_PAUSE_S = 2.0

COUNTDOWN_S = 3.0
BOOST_DURATION_S = 0.6
BOOST_COOLDOWN_S = 6.0
BATTERY_LOG = Path(__file__).resolve().parents[1] / "battery_log.csv"

# Multipliers of --ai-speed: straight speed, curve speed, weapon reaction rate.
AI_LEVELS = {"easy": (0.8, 0.65, 0.5), "normal": (1.0, 0.85, 1.0), "hard": (1.25, 1.05, 1.5),
             "extreme": (1.25, 1.25, 2.0)}


@dataclass
class Laser:
    x0: float
    y0: float
    x1: float
    y1: float
    color: tuple
    until: float
    hit: bool


@dataclass
class Effect:
    """Transient visual: 'ring' (hit), 'boom' (destroyed / mine), 'text' (floating damage), 'spawn'."""
    kind: str
    x: float
    y: float
    t0: float
    duration: float
    color: tuple
    text: str = ""


@dataclass
class Mine:
    owner: "CarState"
    progress: float
    lane_mm: float
    x: float
    y: float
    placed_at: float


class CarState:
    """One connected car and everything the game knows about it."""

    def __init__(self, car: Vehicle, index: int, ble: BleWorker, max_speed: int):
        self.car = car
        self.index = index
        self.color = PLAYER_COLORS[index % len(PLAYER_COLORS)]
        self.engine_rgb = ENGINE_COLORS[index % len(ENGINE_COLORS)]
        self.ble = ble
        self._ai_max_speed = max_speed
        self.player_max_speed = 2000
        self.race_speed_limit = 2000
        self.fair_penalty = 0
        self.source: Optional[I.InputSource] = None
        self.ai = False
        self.ai_pref = True                 # pairing screen: unclaimed car -> AI (True) or parked (False)
        self.ai_difficulty = "normal"
        self.loc: Optional[CarLocalizer] = None
        self.extra_listener = None          # e.g. the track scanner while scanning
        self._messages = SimpleQueue()
        self.position_lead_s = 0.0          # BLE position measurements are older than their arrival
        self.last_radio_t = 0.0
        self._last_motion_t = float("-inf")
        self.message_delay_s = 0.0
        self.battery = BatteryMonitor(car.model, BATTERY_LOG)
        if car.battery_mv:
            self.battery.update(car.battery_mv, 0)
        self.grid_state = ""              # '', 'driving', 'placed', 'off track', 'u-turn'
        self.grid_lane = 0.0
        self.grid_t0 = 0.0
        self.grid_recovery_t: Optional[float] = None
        self.grid_uturn_t = 0.0
        self.grid_laps = 0
        self.grid_retry_lap = False
        self.grid_prev_index: Optional[int] = None
        self.laps = 0
        self.lap_start_t: Optional[float] = None
        self.last_lap_s: Optional[float] = None
        self.best_lap_s: Optional[float] = None
        self.race_finish_s: Optional[float] = None
        self._prev_progress: Optional[float] = None
        self._prev_progress_t: Optional[float] = None
        self._route_epoch = None
        self.lap_note = "cross start to time a lap"
        self._lap_travel = 0.0             # net forward progress since a clean line crossing
        self._reverse_reports = 0         # BLE-thread counter: do not lose brief reversals between frames
        self._lap_reverse_seen = 0
        self._offtrack_reports = 0
        self._last_offtrack_t = 0.0
        self._lap_offtrack_seen = 0
        self._ai_offtrack_seen = 0

        self.hp = MAX_HP
        self.kills = 0
        self.deaths = 0
        self.target_offset = 0.0
        self.penalty_factor = 1.0
        self.penalty_until = 0.0
        self.stun_until = 0.0
        self.dead_until = 0.0
        self.fire_ready_at = 0.0
        self.mine_ready_at = 0.0
        self.ai_next_lane_t = 0.0
        self.ai_uturn_t = 0.0
        self.ai_telemetry_t = 0.0
        self.extreme_max_speed: Optional[float] = None
        self.ai_corner_radius = 0.0
        self.ai_last_corner_radius = 0.0
        self.ai_curve_test_limit = 0.0
        self.ai_corner_limit: Optional[float] = None
        self.ai_last_corner_speed = 0.0
        self.ai_last_corner_t = 0.0
        self.ai_recovering = False
        self.ai_forward_since: Optional[float] = None
        self.ai_has_driven = False
        self.ai_reverse_seen = 0
        self.ai_clean_corners = 0
        self.ai_learning_piece: Optional[int] = None
        self.ai_curve_entry_seen = False
        self.ai_curve_last_frac = 0.0
        self.ai_curve_test_speed = 0.0
        self.ai_track_recovery = ""       # '', 'searching', 'waiting'
        self.ai_track_recovery_t = 0.0
        self.ai_manual_track_retry = False
        self.boost_until = 0.0
        self.boost_ready_at = 0.0
        self.lights_restore_at = 0.0
        self._finish_dark = False
        self._battery_light_state = None

        self._last_speed_sent = 0
        self._last_speed_t = 0.0
        self.cruise_speed = 0.0             # player's held target speed (R2/L2 or W/S), not a direct trigger value
        self._last_lane_sent = 0.0
        self._last_lane_t = 0.0
        self._last_steer_t = 0.0
        self._last_offset_told = 0.0
        self._lane_retry_t = 0.0
        self._steer_dir = 0                 # last stick direction; one lane step per push
        self.desired_lane: Optional[float] = None   # sticky lane goal (snap / AI / grid)

    # ---- identity ----
    @property
    def max_speed(self) -> int:
        if self.source is not None:
            return min(self.player_max_speed, self.race_speed_limit)
        if self.ai_difficulty == "extreme" and self.extreme_max_speed is not None:
            return min(int(self.extreme_max_speed), self.race_speed_limit)
        return min(self._ai_max_speed, self.race_speed_limit)

    @max_speed.setter
    def max_speed(self, value: int) -> None:
        if self.source is not None:
            self.player_max_speed = value
        else:
            self._ai_max_speed = value
            if self.ai_difficulty == "extreme" and self.extreme_max_speed is not None:
                self.extreme_max_speed = value

    @property
    def name(self) -> str:
        return self.car.model

    def driver_label(self) -> str:
        if self.source is not None:
            return f"{self.source.short} · {self.source.name}"
        if self.ai:
            learned = f" · turns {self.ai_corner_limit:.0f}" if self.ai_corner_limit is not None else ""
            return f"AI · {self.ai_difficulty.title()}{learned}"
        return "parked"

    def cycle_ai(self) -> None:
        """Pairing selection: parked -> easy -> normal -> hard -> extreme -> parked."""
        if not self.ai_pref:
            self.ai_pref, self.ai_difficulty = True, "easy"
        elif self.ai_difficulty == "extreme":
            self.ai_pref = False
        else:
            levels = list(AI_LEVELS)
            self.ai_difficulty = levels[levels.index(self.ai_difficulty) + 1]

    # ---- messages (enqueue on BLE thread, apply on game thread) ----
    def enqueue_message(self, msg_id: int, decoded, received_at: Optional[float] = None) -> None:
        received = time.monotonic() if received_at is None else received_at
        self.last_radio_t = received
        self._messages.put((msg_id, decoded, received))

    def drain_messages(self) -> None:
        for _ in range(256):
            try:
                msg_id, decoded, received = self._messages.get_nowait()
            except Empty:
                break
            self.message_delay_s = max(0.0, time.monotonic() - received)
            self.on_message(msg_id, decoded, received)

    def on_message(self, msg_id: int, decoded, received_at: Optional[float] = None) -> None:
        received_at = time.monotonic() if received_at is None else received_at
        self.last_radio_t = max(self.last_radio_t, received_at)
        if msg_id in (P.MSG_LOCALIZATION_POSITION_UPDATE, P.MSG_LOCALIZATION_TRANSITION_UPDATE,
                      P.MSG_SPEED_UPDATE, P.MSG_VEHICLE_DELOCALIZED):
            if received_at < self._last_motion_t:
                return
            self._last_motion_t = received_at
        if msg_id == P.MSG_VEHICLE_DELOCALIZED:
            self._offtrack_reports += 1
            self._last_offtrack_t = received_at
        if msg_id == P.MSG_BATTERY_LEVEL_RESPONSE and isinstance(decoded, dict):
            self.battery.update(int(decoded["battery_mv"]), self._last_speed_sent, charging=bool(self.car.on_charger))
            self.update_battery_light()
        elif msg_id == P.MSG_STATUS_UPDATE and isinstance(decoded, P.VehicleStatus):
            self.battery.charging = decoded.on_charger
            self.update_battery_light()
            if decoded.on_charger and self.loc is not None:
                self.loc.on_track = False
        elif msg_id == P.MSG_SPEED_UPDATE and isinstance(decoded, P.SpeedUpdate) and self.loc is not None:
            self.loc.speed = float(decoded.actual_mm_s)
            self.loc.filter.set_speed(self.loc.direction * self.loc.speed, received_at)
        if self.loc is not None:
            self.loc.on_message(msg_id, decoded, received_at=received_at,
                                measurement_delay=self.position_lead_s)
            if (msg_id == P.MSG_LANE_CHANGE_UPDATE and isinstance(decoded, dict)
                    and "offset_mm" in decoded and "target_mm" in decoded):
                self.loc.on_lane_update(float(decoded["offset_mm"]),
                                        float(decoded["target_mm"]))
            if msg_id == P.MSG_LOCALIZATION_POSITION_UPDATE and self.loc.direction < 0:
                self._reverse_reports += 1
        if self.extra_listener is not None:
            self.extra_listener(msg_id, decoded, received_at=received_at)

    def set_track(self, track: Optional[Track]) -> None:
        self.loc = CarLocalizer(track) if track else None
        self.ai_corner_limit = None
        self.ai_corner_radius = self.ai_last_corner_radius = 0.0
        self.ai_last_corner_t = 0.0
        self.ai_clean_corners = 0
        self.ai_learning_piece = None

    @property
    def charging(self) -> bool:
        return bool(self.car.on_charger)

    def track_status(self) -> str:
        """'charging' | 'on track' | 'off track' | 'idle' | 'link lost'. A stopped car reads no codes, so its
        position is simply unknown until it moves; only call it off track when it has been driving without codes."""
        if not self.car.connected:
            return "link lost"
        if self.charging:
            return "charging"
        if self.localized():
            return "on track"
        now = time.monotonic()
        moving_for = now - self._last_speed_t if self._last_speed_sent > 0 else 0.0
        if self._last_speed_sent > 0 and moving_for > 3.0:
            return "off track"
        return "idle"

    def localized(self) -> bool:
        return (self.car.connected and self.loc is not None and self.loc.on_track and self.loc.index is not None
                and time.monotonic() - self.loc.last_update < 6.0)

    def map_position_known(self) -> bool:
        """Keep a stopped car's last pose visible without claiming fresh telemetry."""
        if self.loc is None or self.loc.index is None or not self.loc.on_track or self.charging:
            return False
        return self.localized() or (self.speed_sent() == 0 and self.loc.speed == 0)

    # ---- status ----
    def is_dead(self, now: float) -> bool:
        return now < self.dead_until

    def speed_sent(self) -> int:
        return self._last_speed_sent

    # ---- commands ----
    def _send(self, payload: bytes) -> None:
        self.ble.fire(self.car.send(payload))

    def command_speed(self, desired: float, now: float, force: bool = False) -> None:
        factor = self.penalty_factor if now < self.penalty_until else 1.0
        if now < self.stun_until or now < self.dead_until:
            speed = 0
        else:
            speed = int(round(desired * factor))
        speed = max(0, min(2000, self.race_speed_limit, speed))
        speed = max(0, speed - self.fair_penalty)
        accel = ACCEL_STOP if speed == 0 else (ACCEL_DOWN if speed < self._last_speed_sent else ACCEL_UP)
        changed = abs(speed - self._last_speed_sent) >= SPEED_SEND_MIN_DELTA
        zeroing = speed == 0 and self._last_speed_sent != 0
        stale = speed != self._last_speed_sent and now - self._last_speed_t > SPEED_KEEPALIVE_S
        # Human analog input changes the target almost every frame. A write for
        # each change can saturate the car's BLE link, delaying its position
        # notifications (the map then freezes for a second and jumps). Cap the
        # rate; zeroing an emergency stop is always immediate.
        due = force or now - self._last_speed_t >= SPEED_SEND_INTERVAL_S
        if zeroing or ((changed or stale) and due):
            self._last_speed_sent, self._last_speed_t = speed, now
            self.ble.fire(self.car.set_speed(speed, accel))
        if speed == 0 and self.loc is not None:
            self.loc.speed = 0.0
            self.loc.filter.set_speed(0, now)

    def hard_stop(self, now: float, accel: int = ACCEL_STOP) -> None:
        self.cruise_speed = 0.0
        self._last_speed_sent, self._last_speed_t = 0, now
        self.ble.fire(self.car.set_speed(0, accel))
        if self.loc is not None:
            self.loc.speed = 0.0
            self.loc.filter.set_speed(0, now)

    def participates(self) -> bool:
        return self.source is not None or self.ai

    def steer(self, amount: float, dt: float, now: float) -> None:
        """Human steering: one discrete lane step per stick push, not an analog offset."""
        direction = 1 if amount > STEER_LANE_THRESHOLD else (-1 if amount < -STEER_LANE_THRESHOLD else 0)
        if direction == 0:
            self._steer_dir = 0
            return
        if direction != self._steer_dir:
            self._steer_dir = direction
            self.snap_lane(direction, now)

    def snap_lane(self, direction: int, now: float) -> None:
        if direction < 0:
            cands = [o for o in LANES if o < self.target_offset - 1]
            self.target_offset = max(cands) if cands else LANES[0]
        else:
            cands = [o for o in LANES if o > self.target_offset + 1]
            self.target_offset = min(cands) if cands else LANES[-1]
        self.desired_lane = self.target_offset
        self._last_steer_t = now

    def set_lane(self, lane: float, now: float) -> None:
        """Go to (and stay in) this lane."""
        self.desired_lane = lane
        self.target_offset = lane
        self._last_steer_t = now

    def actual_lane(self) -> Optional[float]:
        """Lane confirmed by the track codes, in the car's own left/right frame."""
        if not self.localized() or self.loc is None:
            return None
        lane = self.loc.code_lane_mm if self.loc.code_lane_mm is not None else self.loc.lane_mm
        return max(-MAX_ABS_OFFSET, min(MAX_ABS_OFFSET, lane * self.loc.direction))

    def _tell_offset(self, lane_car: float) -> None:
        self._last_offset_told = lane_car
        self._send(P.set_offset_from_road_center(lane_car))

    def apply_lane(self, now: float) -> None:
        """Lane control. The car only knows the offset we tell it (0 at connect), so every lane command is
        preceded by a sync of its offset from the track codes; a sticky desired lane is re-tried when the
        codes show the car settled somewhere else."""
        self.target_offset = max(-MAX_ABS_OFFSET, min(MAX_ABS_OFFSET, self.target_offset))
        steering = now - self._last_steer_t < 0.3
        lane_car = self.actual_lane()
        if abs(self.target_offset - self._last_lane_sent) >= LANE_SEND_MIN_DELTA and now - self._last_lane_t > LANE_SEND_INTERVAL_S:
            if lane_car is not None and abs(lane_car - self._last_offset_told) > 6 and now - self._last_lane_t > 0.8:
                self._tell_offset(lane_car)
            h_speed = 250 if steering else 400
            self.ble.fire(self.car.change_lane(self.target_offset, h_speed=h_speed, h_accel=1500))
            if self.loc is not None:
                self.loc.begin_lane_change(self.target_offset, now)
            self._last_lane_sent, self._last_lane_t = self.target_offset, now
            return
        idle = now - self._last_steer_t > LANE_SYNC_IDLE_S and now - self._last_lane_t > LANE_SYNC_IDLE_S
        if not idle or lane_car is None:
            return
        if self.desired_lane is not None and abs(lane_car - self.desired_lane) > 15 and now - self._lane_retry_t > 2.0:
            # settled in the wrong lane: correct the car's belief and ask again
            self._tell_offset(lane_car)
            self.ble.fire(self.car.change_lane(self.desired_lane, h_speed=400, h_accel=1500))
            if self.loc is not None:
                self.loc.begin_lane_change(self.desired_lane, now)
            self.target_offset = self._last_lane_sent = self.desired_lane
            self._last_lane_t = self._lane_retry_t = now
            return
        if self.desired_lane is None:
            self.target_offset = self._last_lane_sent = lane_car
        if abs(lane_car - self._last_offset_told) > 6:
            self._tell_offset(lane_car)

    def u_turn(self) -> None:
        self._send(P.turn(P.TURN_UTURN, P.TURN_TRIGGER_IMMEDIATE))

    # ---- lights ----
    def lights_idle(self) -> None:
        self._finish_dark = False
        self.lights_restore_at = 0.0
        self.update_battery_light(force=True)
        self._send(P.lights_on(P.LIGHT_HEADLIGHTS | P.LIGHT_ENGINE))

    def update_battery_light(self, force: bool = False) -> None:
        """Keep player color; blink only for low or critical charge."""
        if self._finish_dark or self.lights_restore_at:
            return
        warning = self.battery.status in ("low", "critical")
        state = (self.engine_rgb, warning)
        if not force and state == self._battery_light_state:
            return
        self._battery_light_state = state
        if warning:
            self._send(P.lights_pattern([(channel, P.EFFECT_FLASH, 0, value, 0)
                       for channel, value in zip((P.LC_RED, P.LC_GREEN, P.LC_BLUE), self.engine_rgb)]))
        else:
            self._send(P.engine_color(*self.engine_rgb))

    def lights_flash(self, now: float, seconds: float = 1.0) -> None:
        self._send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_FLASH, 0, 14, 10), (P.LC_RED, P.EFFECT_FLASH, 0, 14, 10),
                                     (P.LC_FRONTL, P.EFFECT_FLASH, 0, 14, 10)]))
        self.lights_restore_at = now + seconds

    def lights_identify(self, on: bool) -> None:
        self._send(P.lights_on(P.LIGHT_HEADLIGHTS | P.LIGHT_FRONTLIGHTS) if on else P.lights_off(P.LIGHT_HEADLIGHTS | P.LIGHT_FRONTLIGHTS))

    def lights_finish(self, winner: bool) -> None:
        self.lights_restore_at = 0.0
        self._finish_dark = not winner
        self._battery_light_state = None
        self._send(P.lights_pattern([(channel, P.EFFECT_STEADY, 0, 0, 0) for channel in range(6)]))
        self._send(P.lights_off(P.LIGHT_HEADLIGHTS | P.LIGHT_BRAKELIGHTS | P.LIGHT_FRONTLIGHTS | P.LIGHT_ENGINE))
        if winner:
            self.lights_idle()

    def world(self) -> Optional[tuple[float, float]]:
        return self.loc.world() if self.loc else None

    def render_world(self, now: float) -> Optional[tuple[float, float]]:
        """Best current pose for the map; corrections are applied smoothly by the renderer."""
        return self.loc.render_world(now) if self.loc else None


class Game:
    def __init__(self, ble: BleWorker, cars: list[CarState], track: Optional[Track], ai_enabled: bool, ai_speed: Optional[int]):
        self.ble = ble
        self.cars = cars
        self.track = track
        self.ai_enabled = ai_enabled
        self.ai_speed = ai_speed
        self.mines: list[Mine] = []
        self.lasers: list[Laser] = []
        self.fx: list[Effect] = []
        self.events: deque[tuple[float, str, tuple]] = deque(maxlen=8)
        self.race_start_t: Optional[float] = None
        self.phase = "idle"                 # idle | grid | countdown | race
        self.countdown_t0 = 0.0
        self._countdown_pulse = None
        self.mode = "battle"                # battle (weapons, kills) | race (laps only, no weapons)
        self.lap_target = 5
        self.fair_game = False
        self.winner: Optional[CarState] = None
        self._finish_lights_winner = None
        self.finish_celebration_t = 0.0
        self._finish_blink = None
        self.race_speed_limit = 2000
        self.finish_t = 0.0
        self.running = False
        self.records = Records()
        for c in cars:
            c.set_track(track)

    # ---- lifecycle ----
    @property
    def records_mode(self) -> str:
        return "race_fair" if self.mode == "race" and self.fair_game else self.mode

    def update_fair_penalties(self) -> None:
        for car in self.cars:
            car.fair_penalty = 0
        if not (self.fair_game and self.mode == "race" and self.running and not self.finished):
            return
        racers = [car for car in self.ranking() if car.participates() and car.car.connected and not car.charging]
        if len(racers) < 2:
            return
        for rank, car in enumerate(racers):
            if car.localized() and not car.ai_track_recovery:
                car.fair_penalty = max(0, 20 - rank * 10)

    def set_race_speed_limit(self, speed: int) -> None:
        self.race_speed_limit = max(200, min(2000, int(speed)))
        for car in self.cars:
            car.race_speed_limit = self.race_speed_limit

    def set_track(self, track: Optional[Track]) -> None:
        self.track = track
        for c in self.cars:
            c.set_track(track)
        self.mines.clear()

    # ---- starting grid --------------------------------------------------
    def assign_roles(self) -> None:
        for c in self.cars:
            c.ai = self.ai_enabled and c.source is None and c.ai_pref

    def can_start(self, claimed_cars) -> bool:
        return any(c.car.connected and not c.charging and
                   (c in claimed_cars or (self.ai_enabled and c.ai_pref)) for c in self.cars)

    def begin_grid(self) -> None:
        """Send every participating car to the start line, one lane each."""
        now = time.monotonic()
        self.assign_roles()
        self.phase = "grid"
        self.running = False
        self.update_fair_penalties()
        participants = sum(1 for c in self.cars if c.participates() and self.track is not None)
        lane_order = grid_lane_order(participants)
        lane_i = 0
        for c in self.cars:
            c.lights_idle()
            if not c.participates() or self.track is None:
                c.grid_state = ""
                c.hard_stop(now)
                continue
            c.grid_lane = lane_order[lane_i % len(lane_order)]
            lane_i += 1
            c.grid_state = "driving"
            c.grid_t0 = now
            c.grid_recovery_t = None
            c.penalty_until = c.dead_until = c.stun_until = 0.0
            c.grid_uturn_t = 0.0
            c.grid_laps = 0
            c.grid_retry_lap = False
            c.grid_prev_index = None
            c.cruise_speed = 0.0
            c.set_lane(c.grid_lane, now)
            c._last_lane_sent = 999.0     # force a lane command once the car is localized
            if c.car.connected and not c.charging:
                c.command_speed(GRID_SPEED, now)
            else:
                c.hard_stop(now)
        self.log("forming the grid: cars drive to the start line")

    def grid_tick(self, dt: float) -> bool:
        """Wait for every participant; only explicitly parked cars are excluded."""
        now = time.monotonic()
        for c in self.cars:
            if c.loc:
                c.loc.advance(dt, now)
        done = True
        for c in self.cars:
            st = c.grid_state
            if not c.participates():
                c.grid_state = ""
                c.command_speed(0, now)
                continue
            if not c.car.connected or c.charging:
                c.grid_state = "waiting for connection" if not c.car.connected else "waiting for charger removal"
                if c.car.connected:
                    c.command_speed(0, now)
                done = False
                continue
            if st == "placed":
                c.command_speed(0, now)
                continue
            done = False
            if not c.localized():
                if c.grid_recovery_t is None:
                    c.grid_recovery_t = now
                    self.log(f"{c.name}: recovering track position; retrying until ready", c.color)
                cycle = (now - c.grid_recovery_t) % (GRID_RECOVERY_DRIVE_S + GRID_RECOVERY_PAUSE_S)
                searching = cycle < GRID_RECOVERY_DRIVE_S
                c.grid_state = "recovering" if searching else "recovery pause"
                c.command_speed(min(250, c.max_speed) if searching else 0, now)
                continue
            if c.grid_recovery_t is not None:
                c.grid_recovery_t = None
                c.grid_prev_index = None
                c._last_lane_sent = 999.0
            loc = c.loc
            assert loc is not None
            previous_index = c.grid_prev_index
            c.grid_prev_index = loc.index
            if loc.direction < 0:
                # driving the wrong way round: turn around, then keep going
                if now - c.grid_uturn_t > 4.0:
                    c.u_turn()
                    c.grid_uturn_t = now
                    c.grid_state = "u-turn"
                c.command_speed(GRID_SPEED, now)
                continue
            if c.grid_retry_lap:
                n = len(self.track.pieces) if self.track else 1
                approaching = (loc.index == n - 1 and loc.frac > GRID_APPROACH_FRAC) or (loc.index == 0 and loc.frac < 0.4)
                if approaching:
                    c.grid_retry_lap = False
                else:
                    c.grid_state = "retry lap"
                    c.command_speed(GRID_RETRY_SPEED, now)
                    c.apply_lane(now)
                    continue
            n = len(self.track.pieces) if self.track else 1
            lane_now = c.actual_lane()
            in_lane = self._grid_lane_ok(c, lane_now)
            # final approach: crawl from the start of the last piece so the cars slow down early
            # and enter the start piece slowly; the middle start code then triggers the stop
            approaching = (loc.index == n - 1 and loc.frac > GRID_APPROACH_FRAC) or (loc.index == 0 and loc.frac < 0.8)
            if approaching and in_lane:
                c.grid_state = "approach"
                c.command_speed(GRID_CRAWL, now)
            else:
                c.grid_state = "driving"
                c.command_speed(GRID_SPEED, now)
            c.apply_lane(now)
            # The last code of the pre-finish half is read in every lane at the same
            # physical longitudinal position. Stop right on it.
            marker_frac = 0.4167
            on_marker = (loc.index == 0 and loc.sub_id == START_ID
                         and marker_frac - 0.02 <= loc.frac < marker_frac + 0.04)
            on_bar = loc.index == 0 and loc.sub_id == FINISH_ID and 0.5 <= loc.frac < 0.6
            overshot = (loc.index == 0 and loc.frac >= 0.6) or (previous_index == 0 and loc.index != 0)
            if on_marker and in_lane:
                c.hard_stop(now)
                loc.frac, loc.sub_id = marker_frac, START_ID
                loc.filter.position = loc.filter.distance(0, marker_frac)
                loc.filter.last_fix = loc.filter.time = now
                loc.filter.speed = 0.0
                loc._display_distance, loc._display_time = loc.filter.position, now
                loc._sensor_pose = loc._code_pose = (0, marker_frac, START_ID)
                loc.last_update = now
                c.grid_lane = lane_now
                c.grid_state = "placed"
                self.log(f"{c.name} one marker before the start line (lane {lane_now:+.0f})", c.color)
            elif c.grid_state != "placed" and (on_bar or overshot):
                c.grid_laps += 1
                c.grid_retry_lap = True
                c.grid_state = "retry lap"
                c._last_lane_sent = 999.0
                c.command_speed(GRID_RETRY_SPEED, now)
                if overshot:
                    why = "overshot"
                elif on_bar:
                    why = "missed the marker"
                else:
                    why = "wrong lane"
                self.log(f"{c.name}: {why}; another lap to retry the start", c.color)
        return done

    def _grid_lane_ok(self, c: "CarState", lane_now: Optional[float]) -> bool:
        """Placement lane: the planned lane, or any lane no other placed car occupies.

        Lane changes occasionally do not complete at grid speed. Insisting on one
        exact lane then costs multiple full laps, so a car may settle in another
        free lane instead (still equal longitudinally).
        """
        if lane_now is None:
            return False
        if abs(lane_now - c.grid_lane) <= 15:
            return True
        for other in self.cars:
            if other is c or other.grid_state != "placed":
                continue
            # a stopped car stops sending lane codes; its accepted placement lane is authoritative
            if abs(other.grid_lane - lane_now) <= 15:
                return False
        return True

    def grid_summary(self) -> tuple[int, int]:
        placed = sum(1 for c in self.cars if c.grid_state == "placed")
        total = sum(1 for c in self.cars if c.grid_state)
        return placed, total

    def begin_countdown(self) -> None:
        self.phase = "countdown"
        self.countdown_t0 = time.monotonic()
        self._countdown_pulse = None
        self.countdown_feedback()
        for c in self.cars:
            if c.grid_state == "placed":
                c.lights_flash(self.countdown_t0, COUNTDOWN_S)

    def countdown_left(self) -> float:
        return max(0.0, COUNTDOWN_S - (time.monotonic() - self.countdown_t0))

    def countdown_feedback(self) -> None:
        beat = math.ceil(self.countdown_left())
        if beat == self._countdown_pulse:
            return
        self._countdown_pulse = beat
        for c in self.cars:
            if c.grid_state == "placed" and c.source is not None and c.source.alive():
                c.source.rumble(0.8 if beat == 0 else 0.35, 0.9 if beat == 0 else 0.35,
                                400 if beat == 0 else 140)

    @property
    def weapons_enabled(self) -> bool:
        return self.mode == "battle"

    @property
    def finished(self) -> bool:
        return self.winner is not None

    def wrong_way(self, c: CarState) -> bool:
        return c.localized() and c.loc is not None and c.loc.direction < 0

    def start(self) -> None:
        now = time.monotonic()
        self.running = True
        self.phase = "race"
        self.race_start_t = now
        self.winner = None
        self._finish_lights_winner = None
        self.finish_celebration_t = 0.0
        self._finish_blink = None
        self.finish_t = 0.0
        self.mines.clear()
        self.lasers.clear()
        self.fx.clear()
        for c in self.cars:
            c.hp = MAX_HP
            c.kills = c.deaths = c.laps = 0
            c.last_lap_s = c.best_lap_s = None
            c.race_finish_s = None
            c.lap_start_t = None
            c._prev_progress = None
            c._prev_progress_t = None
            c._route_epoch = c.loc.route_epoch if c.loc else None
            c.lap_note = "cross start to time a lap"
            if c.loc:
                c.loc.motion_samples.clear()
            c._lap_travel = 0.0
            c._lap_reverse_seen = c.ai_reverse_seen = c._reverse_reports
            c._lap_offtrack_seen = c._ai_offtrack_seen = c._offtrack_reports
            c.ai_recovering = c.ai_has_driven = False
            c.ai_forward_since = None
            c.ai_last_corner_t = c.ai_last_corner_speed = 0.0
            c.ai_clean_corners = 0
            c.ai_learning_piece = None
            c.ai_track_recovery = ""
            c.boost_until = 0.0
            c.boost_ready_at = now + 3.0
            c.cruise_speed = 0.0
            if (c.grid_state == "placed" and c.map_position_known() and c.loc is not None
                    and c.loc.direction > 0 and c.loc.index == 0 and 0.5 <= c.loc.frac < 0.6):
                # A car correctly parked on the bar starts its first timed lap at GO.
                c.lap_start_t = now
                c.lap_note = "timing"
                c._prev_progress = c.loc.progress()
                if c.loc._display_distance is not None:
                    c._prev_progress = self._route_progress(c, c.loc._display_distance)
                c._prev_progress_t = now
            c.dead_until = c.stun_until = c.penalty_until = 0.0
            if c.grid_state in ("off track", "charging", "missed start"):
                c.ai = False
                c.source = None
            elif not c.grid_state:
                c.ai = self.ai_enabled and c.source is None and c.ai_pref
            c.ai_next_lane_t = now + (0.35 if c.ai_difficulty == "extreme" else random.uniform(3, 6))
            c.lights_idle()
        self.log("race started")

    def stop_all(self) -> None:
        now = time.monotonic()
        self.running = False
        self.phase = "idle"
        for c in self.cars:
            c.grid_state = ""
        for c in self.cars:
            c.hard_stop(now)

    def log(self, text: str, color: tuple = (232, 232, 236)) -> None:
        self.events.appendleft((time.monotonic(), text, color))
        log.info(text)

    def effect(self, kind: str, pos: Optional[tuple[float, float]], color: tuple, duration: float, text: str = "") -> None:
        if pos is None:
            return
        self.fx.append(Effect(kind, pos[0], pos[1], time.monotonic(), duration, color, text))

    def ranking(self) -> list["CarState"]:
        def progress(c: CarState) -> float:
            p = c.loc.progress() if c.map_position_known() and c.loc else None
            return (p - 0.5) % len(self.track.pieces) if p is not None and self.track else -1.0
        if self.mode == "race":
            return sorted(self.cars, key=lambda c: (-c.laps, c.race_finish_s if c.race_finish_s is not None else float("inf"),
                                                    -progress(c), c.index))
        return sorted(self.cars, key=lambda c: (-c.kills, c.deaths, -c.laps, c.index))

    def _track_laps(self, c: CarState, now: float) -> None:
        """Only credit a complete forward circuit between clean finish-line crossings."""
        if self.mode == "race" and c.race_finish_s is not None:
            if c.loc:
                c.loc.motion_samples.clear()
            return
        reversed_since_tick = c._reverse_reports != c._lap_reverse_seen
        c._lap_reverse_seen = c._reverse_reports
        offtrack_since_tick = c._offtrack_reports != c._lap_offtrack_seen
        c._lap_offtrack_seen = c._offtrack_reports
        pose_lost = c.loc is None or c.loc.index is None or not c.loc.on_track or c.charging
        if pose_lost or c.ai_track_recovery or self.wrong_way(c) or reversed_since_tick or offtrack_since_tick:
            reason = ("lap reset: reverse" if self.wrong_way(c) or reversed_since_tick else
                      "lap reset: off track" if offtrack_since_tick or (c.loc and not c.loc.on_track) else
                      "lap reset: recovery" if c.ai_track_recovery else "lap reset: position unavailable")
            self._lap_note(c, reason)
            c._prev_progress = None
            c._lap_travel = 0.0
            c.lap_start_t = None
            c._prev_progress_t = None
            if c.loc:
                c.loc.motion_samples.clear()
            return
        if not c.localized() and c.speed_sent() > 0:
            self._lap_note(c, "position stale; holding estimate")
        elif c.lap_start_t is not None:
            c.lap_note = "timing"
        if c.loc.filter.position is not None:
            # The very same unwrapped estimate drives the map and finish events.
            c.loc.advance(0, now)
            while c.loc.motion_samples:
                distance, received, epoch = c.loc.motion_samples.popleft()
                if received < (self.race_start_t or 0):
                    continue
                if distance is None or (c._route_epoch is not None and epoch != c._route_epoch):
                    c._prev_progress = c._prev_progress_t = c.lap_start_t = None
                    c._lap_travel = 0.0
                    self._lap_note(c, "lap reset: position reacquired")
                c._route_epoch = epoch
                if distance is not None:
                    progress = self._route_progress(c, distance)
                    self._track_lap_sample(c, progress, received, unwrapped=True)
            return
        self._track_lap_sample(c, c.loc.progress(), now)

    @staticmethod
    def _route_progress(c: CarState, distance: float) -> float:
        route = c.loc.filter
        index, frac = route.pose(distance)
        return math.floor(distance / route.length) * len(c.loc.track.pieces) + index + frac

    def _lap_note(self, c: CarState, reason: str) -> None:
        if c.lap_note != reason:
            c.lap_note = reason
            self.log(f"{c.name}: {reason}", c.color)

    def _track_lap_sample(self, c: CarState, p: Optional[float], now: float, allowed: float = 1.25, *, unwrapped: bool = False) -> None:
        if self.mode == "race" and c.race_finish_s is not None:
            return
        prev = c._prev_progress
        previous_t = c._prev_progress_t
        c._prev_progress = p
        c._prev_progress_t = now
        if p is None or prev is None:
            # Establish a baseline; an arbitrary initial pose is not a lap.
            if prev is None and c.lap_start_t is None:
                c._lap_travel = 0.0
            return
        n = len(self.track.pieces) if self.track else 1
        delta = p - prev if unwrapped else (p - prev + n / 2) % n - n / 2
        if not unwrapped and abs(delta) > allowed:
            # Relocalization/teleport is not proof that the skipped track was driven.
            c.lap_start_t = None
            c._lap_travel = 0.0
            return
        c._lap_travel += delta
        to_line = (0.5 - prev) % n
        if delta > 0 and 0 < to_line <= delta + 1e-9:
            fraction = to_line / delta
            if self.track:
                lengths = [piece.length_at(c.loc.lane_mm) for piece in self.track.pieces]
                def distance(progress):
                    progress %= n
                    index = int(progress)
                    return sum(lengths[:index]) + (progress - index) * lengths[index]
                total = sum(lengths)
                travelled = (distance(p) - distance(prev)) % total
                if travelled > 0:
                    fraction = ((distance(.5) - distance(prev)) % total) / travelled
            crossing_t = now if previous_t is None else previous_t + (now - previous_t) * fraction
            overshoot = delta - to_line
            if c.lap_start_t is not None and c._lap_travel - overshoot >= n - 0.15:
                lap = crossing_t - c.lap_start_t
                if lap > 0:
                    c.laps += 1
                    c.last_lap_s = lap
                    self.records.record(self.track, self.records_mode, c, "lap", lap)
                    if c.best_lap_s is None or lap < c.best_lap_s:
                        c.best_lap_s = lap
                        self.log(f"{c.name} lap {c.laps}: {lap:.2f}s (best)", c.color)
                    else:
                        self.log(f"{c.name} lap {c.laps}: {lap:.2f}s", c.color)
                    if self.mode == "race" and c.laps >= self.lap_target and c.race_finish_s is None and self.race_start_t is not None:
                        c.race_finish_s = crossing_t - self.race_start_t
                        self.records.record(self.track, self.records_mode, c, "race", c.race_finish_s, self.lap_target)
                    if self.mode == "race" and c.laps >= self.lap_target and (self.winner is None or crossing_t < self.finish_t):
                        self.winner = c
                        self.finish_t = crossing_t
                        self.effect("boom", c.world(), c.color, 1.5)
                        self.log(f"{c.name} WINS the race!", c.color)
                c.lap_start_t = crossing_t
                c.lap_note = "timing"
                c._lap_travel = overshoot
            elif c.lap_start_t is None:
                c.lap_start_t = crossing_t
                c.lap_note = "timing"
                c._lap_travel = overshoot

    # ---- per frame ----
    def tick(self, dt: float) -> None:
        now = time.monotonic()
        for c in self.cars:
            if c.loc:
                c.loc.advance(dt, now)
        self.fx = [e for e in self.fx if now - e.t0 < e.duration]
        self.update_fair_penalties()
        if not self.running:
            return
        if self.finished:
            # let the others roll for a moment, then everybody stops
            for c in self.cars:
                self._track_laps(c, now)
                if now - self.finish_t > 2.0:
                    c.command_speed(0, now)
                else:
                    c.command_speed(min(c.speed_sent(), 300), now) if c is not self.winner else c.command_speed(0, now)
            if self._finish_lights_winner is not self.winner:
                for c in self.cars:
                    c.lights_finish(c is self.winner)
                    if c is not self.winner and c.participates() and c.source is not None and c.source.alive():
                        c.source.rumble(0.7, 0.4, 1200)
                self._finish_lights_winner = self.winner
                self.finish_celebration_t = now
                self._finish_blink = None
            # Twelve slow, full-color blinks over twelve seconds. Drive the
            # timing here instead of depending on firmware effect timing.
            elapsed = now - self.finish_celebration_t
            blink = int(elapsed / 0.5) if elapsed < 12.0 else 24
            if blink != self._finish_blink:
                self._finish_blink = blink
                if blink == 24:
                    self.winner.lights_idle()
                else:
                    self.winner.lights_restore_at = now + 1.0
                    rgb = self.winner.engine_rgb if blink % 2 == 0 else (0, 0, 0)
                    self.winner._send(P.engine_color(*rgb))
            return
        for c in self.cars:
            self._track_laps(c, now)
            if c.hp <= 0 and not c.is_dead(now):
                c.hp = MAX_HP
                c.lights_idle()
                self.effect("spawn", c.world(), c.color, 0.8)
                self.log(f"{c.name} respawned", c.color)
            if c.lights_restore_at and now > c.lights_restore_at:
                c.lights_restore_at = 0.0
                c.lights_idle()
            if c.source is not None and c.source.alive():
                self._human(c, dt, now)
            elif c.ai:
                self._ai(c, dt, now)
            else:
                c.command_speed(0, now)
            if not c.ai_track_recovery:
                c.apply_lane(now)
        self._resolve_mines(now)
        self.lasers = [l for l in self.lasers if l.until > now]
        self.mines = [m for m in self.mines if now - m.placed_at < MINE_LIFE_S]

    def _human(self, c: CarState, dt: float, now: float) -> None:
        src = c.source
        assert src is not None
        if c.ai_track_recovery:
            if src.pressed(I.STOP) or src.brake() > 0.1:
                c.ai_track_recovery = "waiting"
                c.hard_stop(now)
                return
            if self._recover_ai_track(c, now):
                return
        if src.pressed(I.LIMIT_UP):
            c.max_speed = min(2000, c.max_speed + 100)
        if src.pressed(I.LIMIT_DOWN):
            c.max_speed = max(200, c.max_speed - 100)
        if src.pressed(I.BOOST):
            self.request_boost(c, now)
        if src.pressed(I.STOP):
            c.hard_stop(now)
            src.rumble(0.8, 0.8, 150)
        else:
            # R2/L2 (or W/S) change a held target speed. Releasing the input
            # keeps the target, like the slider in the official app. Analog
            # travel scales the rate, so a light touch is fine and a full pull
            # accelerates quickly. command_speed caps the BLE write rate.
            up = max(0.0, min(1.0, src.throttle()))
            down = max(0.0, min(1.0, src.brake()))
            was_moving = c.cruise_speed > 0
            c.cruise_speed = max(0.0, min(c.max_speed, c.cruise_speed +
                                          (up * PLAYER_ACCEL_RATE - down * PLAYER_BRAKE_RATE) * dt))
            if c.cruise_speed > 0 or (was_moving and down > 0):
                c.cruise_speed = max(c.cruise_speed, min(MIN_PLAYER_SPEED, c.max_speed))
            if down > 0 or c.cruise_speed <= 0:
                c.boost_until = 0.0
            target = self.boost_target(c, c.cruise_speed, now) if c.cruise_speed > 0 else 0.0
            c.command_speed(target, now)
        c.steer(src.steer(), dt, now)
        if src.pressed(I.LANE_LEFT):
            c.snap_lane(-1, now)
        if src.pressed(I.LANE_RIGHT):
            c.snap_lane(+1, now)
        if src.pressed(I.UTURN):
            c.u_turn()
            src.rumble(0.3, 0.6, 120)
        if src.pressed(I.FIRE):
            self.fire(c, now)
        if src.pressed(I.MINE):
            self.drop_mine(c, now)

    def ai_speed_ceiling(self, c: CarState) -> float:
        if c.ai_difficulty == "extreme" and c.extreme_max_speed is not None:
            return min(2000, c.extreme_max_speed)
        return c.max_speed

    def learned_corner_speed(self, c: CarState, radius: float) -> float:
        limit = c.ai_corner_limit if c.ai_corner_limit is not None else float("inf")
        if c.ai_difficulty == "extreme" and c.ai_corner_radius > 0 and radius > 0:
            limit *= math.sqrt(radius / c.ai_corner_radius)
        return limit

    def corner_radius(self, c: CarState, piece) -> float:
        return max(1.0, min(piece.length_at(l) * 2 / math.pi
                           for l in (c.loc.lane_mm, c.target_offset * c.loc.direction)))

    def ai_target_speed(self, c: CarState, straight_override: Optional[float] = None) -> float:
        straight_factor, curve_factor, _ = AI_LEVELS[c.ai_difficulty]
        # Without an explicit override, Hard uses the full car limit; Normal
        # and Easy use 80% and 64%. Do not silently cap every AI at 450 mm/s.
        base = self.ai_speed if self.ai_speed is not None else self.ai_speed_ceiling(c) / AI_LEVELS["hard"][0]
        straight = max(0, min(2000, self.ai_speed_ceiling(c), base * straight_factor))
        curve = min(straight, max(0, base * curve_factor))
        cruise_straight = straight
        if straight_override is not None:
            straight = max(straight, min(1500, straight_override))
        if not c.localized() or not self.track or c.loc is None:
            return min(curve, 300)
        loc = c.loc
        pieces = self.track.pieces
        piece = pieces[loc.index]
        # Unknown/special pieces use the conservative curve limit too.
        fast_kinds = (STRAIGHT, START, CRISSCROSS)
        def limit_for(p):
            if p.kind in fast_kinds:
                return straight
            learned = c.ai_corner_limit if c.ai_corner_limit is not None else float("inf")
            if c.ai_difficulty != "extreme":
                return min(curve, learned)
            if p.kind != TURN or p.turn not in ("L", "R"):
                return min(curve, learned, 300)
            # Calibratable lateral acceleration budget (mm/s²). A tighter lane
            # needs a lower speed at high limits: a = v²/r. Include a pending move.
            radius = self.corner_radius(c, p)
            if c.ai_corner_radius > 0:
                learned *= math.sqrt(radius / c.ai_corner_radius)
            return min(cruise_straight, learned, math.sqrt(3200.0 * radius))

        distance = (1.0 - loc.frac) * piece.length_at(loc.lane_mm)
        target = limit_for(piece)
        for step in range(1, len(pieces) + 1):
            upcoming = pieces[(loc.index + step) % len(pieces)]
            if upcoming.kind not in fast_kinds:
                # Higher difficulty brakes later while retaining a telemetry margin.
                margin = {"easy": 0.25, "normal": 0.18, "hard": 0.12, "extreme": 0.08}[c.ai_difficulty]
                usable = max(0.0, distance - max(straight, loc.speed) * margin)
                target = min(target, math.sqrt(limit_for(upcoming) ** 2 + 2 * ACCEL_DOWN * usable))
            distance += min(upcoming.length_at(l) for l in
                            (loc.lane_mm, c.target_offset * loc.direction))
        return target

    def _boost_clear(self, c: CarState, now: float) -> bool:
        if (not c.localized() or not self.track or c.charging or c.ai_track_recovery or c.ai_recovering
                or c.is_dead(now) or now < c.stun_until or now < c.penalty_until
                or c.battery.status == "critical"):
            return False
        loc = c.loc
        if (loc.direction < 0 or now - loc.last_update > 0.4 or loc.speed < 300
                or self.track.pieces[loc.index].kind not in (STRAIGHT, START)
                or abs(c.target_offset - loc.lane_mm) > 12):
            return False
        for other in self.cars:
            if other is c or not other.localized():
                continue
            low, high = self._occupied_lanes(other)
            gap = self._traffic_distance(c, other)
            if low - 32 < loc.lane_mm < high + 32 and 0 <= gap < max(350, loc.speed * 0.6):
                return False
        return True

    def request_boost(self, c: CarState, now: float) -> bool:
        if not self.running or self.finished or now < c.boost_ready_at or not self._boost_clear(c, now):
            return False
        peak = min(1500, c.max_speed * 1.5)
        if self.ai_target_speed(c, peak) <= c.max_speed + 40:
            return False  # too little braking room to gain speed
        c.boost_until = now + BOOST_DURATION_S
        c.boost_ready_at = now + BOOST_COOLDOWN_S
        self.log(f"{c.name}: straight boost (up to {peak:.0f} mm/s)", c.color)
        return True

    def boost_target(self, c: CarState, cruise: float, now: float) -> float:
        if now >= c.boost_until:
            return cruise
        if not self._boost_clear(c, now):
            c.boost_until = 0.0
            return cruise
        return max(cruise, self.ai_target_speed(c, min(1500, c.max_speed * 1.5)))

    def _traffic_distance(self, c: CarState, other: CarState) -> float:
        """Signed centerline distance, wrapping at the finish line."""
        pieces = self.track.pieces
        def position(loc):
            return sum(p.length_mm for p in pieces[:loc.index]) + loc.frac * pieces[loc.index].length_mm
        length = sum(p.length_mm for p in pieces)
        return (position(other.loc) - position(c.loc) + length / 2) % length - length / 2

    @staticmethod
    def _occupied_lanes(c: CarState) -> tuple[float, float]:
        """Include the entire lane-change path, reserving a pending AI move."""
        lane = c.loc.lane_mm
        goal = c.target_offset * c.loc.direction
        return min(lane, goal), max(lane, goal)

    def ai_race_traffic(self, c: CarState, target: float, now: float) -> float:
        if not c.localized() or not self.track or c.loc is None:
            return target
        traffic = [o for o in self.cars if o is not c and o.localized()]
        lane = c.loc.lane_mm
        low, high = self._occupied_lanes(c)
        blockers = []
        for other in traffic:
            olo, ohi = self._occupied_lanes(other)
            gap = self._traffic_distance(c, other)
            if 0 <= gap < max(400, target * 1.2) and low < ohi + 32 and high > olo - 32:
                blockers.append((gap, other))
        piece = self.track.pieces[c.loc.index]
        settled = abs(c.target_offset - lane) < 12
        if (settled and now >= c.ai_next_lane_t and piece.kind in (STRAIGHT, START, CRISSCROSS)
                and c.loc.frac < 0.6):
            # Score the next three pieces by travel distance, including the move.
            # Adjacent moves converge toward the inside without sweeping across lanes.
            upcoming = [self.track.pieces[(c.loc.index + step) % len(self.track.pieces)]
                        for step in range(1, min(3, len(self.track.pieces) - 1) + 1)]
            def path_cost(candidate):
                return sum(p.length_at(candidate) for p in upcoming) + abs(candidate - lane) * 0.2
            candidates = sorted((l for l in LANES if 15 < abs(l - lane) <= 55), key=path_cost)
            for candidate in candidates:
                if not blockers and path_cost(candidate) >= path_cost(lane) - 10:
                    continue
                safe = True
                path_low, path_high = sorted((lane, candidate))
                for other in traffic:
                    olo, ohi = self._occupied_lanes(other)
                    if path_low >= ohi + 32 or path_high <= olo - 32:
                        continue
                    distance = self._traffic_distance(c, other)
                    relative = other.loc.speed * other.loc.direction - c.loc.speed
                    future = distance + relative * 0.8
                    if min(distance, future) < 180 and max(distance, future) > -180:
                        safe = False
                        break
                if safe:
                    c.set_lane(candidate, now)
                    c.ai_next_lane_t = now + (0.35 if c.ai_difficulty == "extreme" else 1.2)
                    log.debug("AI %s %s: lane %.0f -> %.0f", c.name,
                              "passing" if blockers else "shorter line", lane, candidate)
                    break
        if not blockers:
            return target
        gap, lead = min(blockers, key=lambda item: item[0])
        # Follow until telemetry confirms lateral clearance, even after requesting a pass.
        lead_speed = max(0.0, lead.loc.speed * lead.loc.direction)
        usable = max(0.0, gap - 160 - max(c.loc.speed, target) * 0.3)
        following = math.sqrt(lead_speed ** 2 + 2 * ACCEL_DOWN * usable)
        if gap < 160:
            following = min(following, lead_speed * max(0.0, gap - 80) / 80)
        return min(target, following)

    def _learn_corner_recovery(self, c: CarState, now: float) -> None:
        """One reduction per spin, not per repeated U-turn command."""
        reversal = self.wrong_way(c) or c._reverse_reports != c.ai_reverse_seen
        c.ai_reverse_seen = c._reverse_reports
        if reversal:
            c.ai_forward_since = None
            if not c.ai_recovering and c.ai_has_driven:
                reference = (c.ai_last_corner_speed if now - c.ai_last_corner_t < 3.0
                             else max(c.speed_sent(), 300))
                if c.ai_corner_limit is not None:
                    reference = min(reference, self.learned_corner_speed(c, c.ai_last_corner_radius))
                c.ai_corner_limit = min(reference, max(200.0, reference * 0.85))
                c.ai_corner_radius = c.ai_last_corner_radius
                c.ai_clean_corners = 0
                self.log(f"{c.name} learned: corners limited to {c.ai_corner_limit:.0f} mm/s after spin", c.color)
            c.ai_recovering = True
        elif c.localized():
            if c.ai_forward_since is None:
                c.ai_forward_since = now
            if now - c.ai_forward_since >= 2.0:
                c.ai_recovering = False
            if c.speed_sent() > 0:
                c.ai_has_driven = True
        else:
            c.ai_forward_since = None

    def _learn_clean_corner(self, c: CarState, now: float) -> None:
        """Probe upward slowly after three fully observed, forward, clean turns."""
        if not c.localized() or c.ai_recovering or self.wrong_way(c) or not self.track:
            c.ai_learning_piece = None
            c.ai_clean_corners = 0
            return
        loc = c.loc
        index = loc.index
        previous = c.ai_learning_piece
        if previous != index:
            if (previous is not None and self.track.pieces[previous].kind == TURN
                    and c.ai_curve_entry_seen and c.ai_curve_last_frac >= 0.65
                    and c.ai_curve_test_speed >= c.ai_curve_test_limit * 0.9
                    and index == (previous + 1) % len(self.track.pieces)):
                c.ai_clean_corners += 1
                if c.ai_clean_corners >= 3 and c.ai_corner_limit is not None:
                    base = self.ai_speed if self.ai_speed is not None else self.ai_speed_ceiling(c) / AI_LEVELS["hard"][0]
                    ceiling = min(2000, self.ai_speed_ceiling(c), base * AI_LEVELS[c.ai_difficulty][1])
                    if c.ai_difficulty == "extreme" and c.ai_corner_radius > 0:
                        ceiling = min(ceiling, math.sqrt(3200 * c.ai_corner_radius))
                    old = c.ai_corner_limit
                    raised = min(ceiling, old * 1.02)
                    if raised > old:
                        c.ai_corner_limit = raised
                        self.log(f"{c.name} learned: trying {raised:.0f} mm/s in corners after 3 clean turns", c.color)
                    c.ai_clean_corners = 0
            elif previous is not None and index != (previous + 1) % len(self.track.pieces):
                c.ai_clean_corners = 0
            c.ai_learning_piece = index
            c.ai_curve_entry_seen = self.track.pieces[index].kind == TURN and loc.frac <= 0.35
            c.ai_curve_test_limit = c.ai_corner_limit or 0
            if c.ai_difficulty == "extreme" and c.ai_corner_radius > 0:
                c.ai_curve_test_limit *= math.sqrt(self.corner_radius(c, self.track.pieces[index]) / c.ai_corner_radius)
            c.ai_curve_test_speed = min(loc.speed, c.speed_sent())
        else:
            c.ai_curve_test_speed = min(c.ai_curve_test_speed, loc.speed, c.speed_sent())
        c.ai_curve_last_frac = loc.frac

    def retry_recovery(self, source=None) -> None:
        """Retry waiting AI and the requesting player's car after replacement."""
        self.retry_ai_recovery()
        if not self.running or self.finished or not self.track:
            return
        now = time.monotonic()
        for c in self.cars:
            if c.source is None or (source is not None and c.source is not source):
                continue
            if not c.car.connected or c.charging:
                continue
            if self.wrong_way(c):
                c.u_turn()
            if not c.localized() or now - c.loc.last_update >= 1.5 or c.ai_track_recovery:
                c.ai_track_recovery = "searching"
                c.ai_track_recovery_t = now
                c.ai_manual_track_retry = True
                c._prev_progress = c.lap_start_t = None
                c._lap_travel = 0.0
                self.log(f"{c.name}: retrying track detection", c.color)

    def retry_ai_recovery(self) -> None:
        """User has replaced a stopped car; allow one bounded attempt to read codes."""
        if not self.running or self.finished or not self.track:
            return
        now = time.monotonic()
        for c in self.cars:
            if c.ai and c.ai_track_recovery == "waiting" and c.car.connected and not c.charging:
                c.ai_track_recovery = "searching"
                c.ai_track_recovery_t = now
                c.ai_manual_track_retry = True
                self.log(f"{c.name}: retrying track detection", c.color)

    def _recover_ai_track(self, c: CarState, now: float) -> bool:
        """Return True while normal driving must yield to track recovery."""
        loc = c.loc
        available = c.car.connected and not c.charging and loc is not None
        reported = c._offtrack_reports != c._ai_offtrack_seen
        c._ai_offtrack_seen = c._offtrack_reports
        incident_t = c._last_offtrack_t if reported else (c.ai_track_recovery_t if c.ai_track_recovery else now)
        if (available and (reported or not loc.on_track) and c.ai_has_driven and not c.ai_recovering
                and 0 <= incident_t - c.ai_last_corner_t < 3.0 and c.ai_last_corner_speed > 0):
            reference = min(c.ai_last_corner_speed, self.learned_corner_speed(c, c.ai_last_corner_radius))
            c.ai_corner_limit = min(reference, max(200.0, reference * 0.85))
            c.ai_corner_radius = c.ai_last_corner_radius
            c.ai_recovering = True
            c.ai_forward_since = None
            c.ai_clean_corners = 0
            self.log(f"{c.name} learned: corners limited to {c.ai_corner_limit:.0f} mm/s after leaving track", c.color)
        fresh = available and loc.on_track and loc.index is not None and now - loc.last_update < 1.5
        if fresh and (not c.ai_track_recovery or loc.last_update > c.ai_track_recovery_t):
            if c.ai_track_recovery:
                c.ai_track_recovery = ""
                c.ai_forward_since = None
                self.log(f"{c.name}: track found, resuming", c.color)
            return False
        if not c.ai_track_recovery:
            c.ai_track_recovery_t = now
            c.ai_manual_track_retry = False
            # A positive delocalized report means stop; missing telemetry alone
            # gets one 1.5 s search at 250 mm/s. Never keep driving blindly.
            can_probe = available and loc.on_track and (c.speed_sent() > 0 or not c.ai_has_driven)
            c.ai_track_recovery = "searching" if can_probe else "waiting"
            c._prev_progress = c.lap_start_t = None
            c._lap_travel = 0.0
            c.ai_learning_piece = None
            c.ai_clean_corners = 0
            self.log(f"{c.name}: {'searching for track codes' if c.ai_track_recovery == 'searching' else 'put back on track, press R / right-stick click'}", WARN_COLOR)
        if c.ai_track_recovery == "searching" and (not available or now - c.ai_track_recovery_t >= 1.5
                                                  or (not loc.on_track and not c.ai_manual_track_retry)):
            c.ai_track_recovery = "waiting"
            self.log(f"{c.name}: stopped; put back on track, press R / right-stick click", WARN_COLOR)
        if c.car.connected:
            c.command_speed(min(250, c.max_speed) if c.ai_track_recovery == "searching" else 0, now)
        else:
            c._last_speed_sent = 0
        return True

    def _ai(self, c: CarState, dt: float, now: float) -> None:
        if self._recover_ai_track(c, now):
            return
        self._learn_corner_recovery(c, now)
        self._learn_clean_corner(c, now)
        if self.wrong_way(c):
            # never race against the track direction: turn around (once every few seconds until it sticks)
            if now - c.ai_uturn_t > 4.0:
                c.u_turn()
                c.ai_uturn_t = now
                self.log(f"{c.name} turns around (wrong way)", c.color)
            c.command_speed(min(self.ai_target_speed(c), 300), now)
            return
        target = self.ai_target_speed(c)
        if c.ai_difficulty in ("hard", "extreme"):
            self.request_boost(c, now)
        target = self.boost_target(c, target, now)
        if self.mode == "race":
            target = self.ai_race_traffic(c, target, now)
            # A newly selected inside lane can have a tighter curve speed limit.
            target = min(target, self.boost_target(c, self.ai_target_speed(c), now))
        c.command_speed(target, now)
        if (c.localized() and self.track and c.loc is not None
                and self.track.pieces[c.loc.index].kind == TURN and not c.ai_recovering):
            # Learn from the intended corner limit, not a traffic slowdown or
            # motor overshoot that could otherwise raise the cap after a spin.
            c.ai_last_corner_speed = self.ai_target_speed(c)
            c.ai_last_corner_radius = self.corner_radius(c, self.track.pieces[c.loc.index])
            c.ai_last_corner_t = now
        if log.isEnabledFor(logging.DEBUG) and now >= c.ai_telemetry_t:
            c.ai_telemetry_t = now + 0.25
            loc = c.loc
            log.debug("AI %s %s target=%d actual=%.0f limit=%d piece=%s frac=%.2f pos_age=%.2f radio_age=%.2f queue_delay=%.3f",
                      c.name, c.ai_difficulty, c.speed_sent(), loc.speed if loc else 0,
                      c.max_speed, self.track.pieces[loc.index].kind
                      if self.track and loc and loc.index is not None else "unknown", loc.frac if loc else 0,
                      now - loc.last_update if loc else -1, now - c.last_radio_t if c.last_radio_t else -1,
                      c.message_delay_s)
        on_straight = (c.localized() and c.loc is not None and self.track is not None
                       and self.track.pieces[c.loc.index].kind in (STRAIGHT, START, CRISSCROSS))
        if self.mode != "race" and on_straight and now > c.ai_next_lane_t:
            c.set_lane(random.choice(LANES), now)
            c.ai_next_lane_t = now + random.uniform(4, 9)
        if not self.weapons_enabled:
            return
        reaction = AI_LEVELS[c.ai_difficulty][2]
        if now > c.fire_ready_at and random.random() < dt * 0.6 * reaction and self.target_ahead(c) is not None:
            self.fire(c, now)
        if now > c.mine_ready_at and random.random() < dt * 0.06 * reaction:
            self.drop_mine(c, now)

    # ---- weapons ----
    def target_ahead(self, shooter: CarState) -> Optional[tuple[CarState, float]]:
        if not shooter.localized():
            return None
        best = None
        for o in self.cars:
            if o is shooter or not o.localized() or o.hp <= 0:
                continue
            d = shooter.loc.distance_ahead_mm(o.loc)  # type: ignore[union-attr]
            if d is None or d <= 0 or d > BLASTER_RANGE_MM:
                continue
            if best is None or d < best[1]:
                best = (o, d)
        return best

    def fire(self, shooter: CarState, now: float) -> None:
        if not self.weapons_enabled or now < shooter.fire_ready_at or shooter.is_dead(now) or not shooter.localized():
            return
        shooter.fire_ready_at = now + BLASTER_COOLDOWN_S
        p0 = shooter.world()
        if p0 is None:
            return
        target = self.target_ahead(shooter)
        if target is not None:
            victim, _d = target
            p1 = victim.world() or p0
            self.lasers.append(Laser(p0[0], p0[1], p1[0], p1[1], shooter.color, now + 0.25, True))
            self.damage(victim, shooter, BLASTER_DAMAGE, now, slow=True)
            if shooter.source:
                shooter.source.rumble(0.2, 0.7, 120)
        else:
            a = shooter.loc.heading()  # type: ignore[union-attr]
            p1 = (p0[0] + math.cos(a) * 500, p0[1] + math.sin(a) * 500)
            self.lasers.append(Laser(p0[0], p0[1], p1[0], p1[1], shooter.color, now + 0.15, False))

    def drop_mine(self, owner: CarState, now: float) -> None:
        if not self.weapons_enabled or now < owner.mine_ready_at or owner.is_dead(now) or not owner.localized():
            return
        if sum(1 for m in self.mines if m.owner is owner) >= MINE_MAX_ACTIVE:
            return
        prog = owner.loc.progress()  # type: ignore[union-attr]
        w = owner.world()
        if prog is None or w is None:
            return
        owner.mine_ready_at = now + MINE_COOLDOWN_S
        lane = owner.loc.lane_mm  # type: ignore[union-attr]
        self.mines.append(Mine(owner, prog, lane, w[0], w[1], now))
        self.log(f"{owner.name} dropped a mine", owner.color)

    def _resolve_mines(self, now: float) -> None:
        if not self.track or not self.mines:
            return
        n = len(self.track.pieces)
        avg_len = sum(p.length_mm for p in self.track.pieces) / n
        exploded: list[Mine] = []
        for m in self.mines:
            if now - m.placed_at < MINE_ARM_S:
                continue
            for c in self.cars:
                if c is m.owner or not c.localized() or c.is_dead(now):
                    continue
                prog = c.loc.progress()  # type: ignore[union-attr]
                if prog is None:
                    continue
                d = abs(prog - m.progress) % n
                d = min(d, n - d) * avg_len
                if d < MINE_TRIGGER_MM and abs(c.loc.lane_mm - m.lane_mm) < MINE_LANE_MM:  # type: ignore[union-attr]
                    exploded.append(m)
                    self.effect("boom", (m.x, m.y), (255, 150, 60), 0.9)
                    self.damage(c, m.owner, MINE_DAMAGE, now, stun=True)
                    self.log(f"{c.name} ran into {m.owner.name}'s mine", (255, 170, 80))
                    break
        for m in exploded:
            if m in self.mines:
                self.mines.remove(m)

    def damage(self, victim: CarState, attacker: CarState, amount: int, now: float, slow: bool = False, stun: bool = False) -> None:
        if victim.is_dead(now) or victim.hp <= 0:
            return
        victim.hp = max(0, victim.hp - amount)
        w = victim.world()
        self.effect("ring", w, attacker.color, 0.45)
        self.effect("text", w, (255, 235, 200), 0.9, f"-{amount}")
        if slow:
            victim.penalty_factor = BLASTER_SLOW_FACTOR
            victim.penalty_until = now + BLASTER_SLOW_S
        if stun:
            victim.stun_until = now + MINE_STUN_S
        victim.lights_flash(now, 0.8)
        if victim.source:
            victim.source.rumble(0.9, 0.4, 250)
        if victim.hp <= 0:
            victim.dead_until = now + DEATH_STOP_S
            victim.deaths += 1
            attacker.kills += 1
            victim.lights_flash(now, DEATH_STOP_S)
            self.effect("boom", w, victim.color, 1.2)
            self.log(f"{attacker.name} destroyed {victim.name}!", attacker.color)
        elif slow:
            self.log(f"{attacker.name} hit {victim.name} (-{amount})", attacker.color)
