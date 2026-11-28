"""Game logic: per-car driving (human or AI), virtual weapons, damage and respawn."""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from anki import protocol as P
from anki.ble_worker import BleWorker
from anki.track import CarLocalizer, Track, STRAIGHT, START, CRISSCROSS, TURN, FINISH_ID
from anki.vehicle import Vehicle
from . import inputs as I
from .battery import BatteryMonitor
from .colors import ENGINE_COLORS, PLAYER_COLORS, paint_for_model

log = logging.getLogger("game")

WARN_COLOR = (255, 176, 70)
LANES = (-68.0, -23.0, 23.0, 68.0)
MAX_ABS_OFFSET = 68.0
# Physical following constraint between cars in the same lane.
CAR_MIN_GAP_MM = float(os.environ.get("ARENA_CAR_GAP", "180"))
FOLLOW_LINK_MM = float(os.environ.get("ARENA_FOLLOW_LINK", "700"))
FOLLOW_RELEASE_MM = float(os.environ.get("ARENA_FOLLOW_RELEASE", "1100"))
LANE_OVERLAP_MM = float(os.environ.get("ARENA_LANE_OVERLAP", "15"))
# A following link is only trusted while the leader's own position is fresh.
# Otherwise the follower would be pinned behind a frozen ghost position and
# jump forward when its next real position update arrives.
FOLLOW_STALE_S = float(os.environ.get("ARENA_FOLLOW_STALE", "1.5"))
# A single localizer re-anchor can jump the gap by more than a metre.  Do not
# drop a valid following link on one sample; require the large gap to persist.
FOLLOW_RELEASE_HOLD_S = float(os.environ.get("ARENA_FOLLOW_RELEASE_HOLD", "0.8"))


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
SPEED_SEND_INTERVAL_S = 0.2      # 5 Hz; the firmware interpolates between targets
PLAYER_ACCEL_RATE = 450.0        # mm/s^2 at full R2/W; analog trigger travel scales this
PLAYER_BRAKE_RATE = 900.0        # mm/s^2 at full L2/S
MIN_PLAYER_SPEED = 300           # below this the cars stutter and miss track codes
LANE_SEND_MIN_DELTA = 3.0
LANE_SEND_INTERVAL_S = 0.1
LANE_SYNC_IDLE_S = 1.5
STEER_LANE_THRESHOLD = 0.5   # stick deflection that requests the next lane; one step per push
# Match the firmware ramp to the host ramp so sparse targets interpolate smoothly.
ACCEL_UP = 450
ACCEL_DOWN = 900
ACCEL_STOP = 2500

MAX_HP = 100
BATTLE_LIVES = 5
WEAPONS = ("sniper", "blaster", "mine")
POWERUPS = ("uturn", "hook", "anchor", "booster")
# A sniper shot runs straight from the car's nose until it leaves the track.
# Targets count as hit when their centre is within half a car body of that line.
SNIPER_HIT_HALF_WIDTH_MM = 25.0
# Outer lane plus a car body and a little slack; keeps the beam on the track.
SNIPER_TRACK_CORRIDOR_MM = MAX_ABS_OFFSET + SNIPER_HIT_HALF_WIDTH_MM + 2.0
SNIPER_COOLDOWN_S = 2.0
SNIPER_DAMAGE = 34
# The new blaster is a short-range spread weapon: every car in the cone is hit.
BLASTER_RANGE_MM = 500.0
BLASTER_HIT_HALF_WIDTH_MM = 58.0
BLASTER_TRACK_CORRIDOR_MM = MAX_ABS_OFFSET + BLASTER_HIT_HALF_WIDTH_MM + 2.0
BLASTER_DAMAGE = 3
BLASTER_PULSE_INTERVAL_S = 0.2
BLASTER_MAX_FIRE_S = 4.0
BLASTER_OVERHEAT_S = 2.5
BLASTER_ENERGY_MAX = 100.0
BLASTER_ENERGY_DRAIN = 25.0
BLASTER_ENERGY_REGEN = 10.0
BLASTER_REGEN_DELAY_S = 0.8
BLASTER_MIN_ENERGY = 20.0
MINE_COOLDOWN_S = 4.0
MINE_MAX_ACTIVE = 2
MINE_LIFE_S = 45.0
MINE_DAMAGE = 35
MINE_TRIGGER_MM = 120.0
MINE_LANE_MM = 35.0
MINE_ARM_S = 1.0
# Powerups share one energy pool per car, separate from the blaster's weapon energy.
POWERUP_ENERGY_MAX = 100.0
POWERUP_ENERGY_REGEN = 10.0
POWERUP_REGEN_DELAY_S = 1.0
UTURN_MIN_ENERGY = 50.0
UTURN_DRAIN = 100.0 / 3.5        # 50 % lasts 1.75 s, 100 % lasts 3.5 s
UTURN_COOLDOWN_S = 3.0
UTURN_FLIP_TIMEOUT_S = 2.6
UTURN_REVERSE_SPEED = 500
HOOK_MIN_ENERGY = 20.0
HOOK_DRAIN = 20.0                # 100 % = 5 s
HOOK_RANGE_MM = 2000.0
HOOK_BREAK_MM = 2400.0
HOOK_SLOW_FACTOR = 0.75
HOOK_MIN_SPEED = 300.0
ANCHOR_COST = 50.0
ANCHOR_HOLD_S = 2.5
BOOSTER_MIN_ENERGY = 25.0
BOOSTER_DRAIN = 40.0             # 100 % = 2.5 s
BOOSTER_FACTOR = 1.6
BOOSTER_MAX_MM_S = 2000.0
BOOSTER_COOLDOWN_S = 1.5
DEATH_STOP_S = 3.0
# Controller feedback: a destroyed car gives a clearly stronger, longer pulse than a normal hit.
HIT_RUMBLE = (0.9, 0.4, 250)
DEATH_RUMBLE = (1.0, 0.8, 900)
# The blaster fires five pulses per second; a full hit pulse would buzz constantly.
SPRAY_HIT_RUMBLE = (0.55, 0.25, 90)
BLASTER_FIRE_RUMBLE = (0.25, 0.45, 400)
BLASTER_OVERHEAT_RUMBLE = (0.9, 0.75, 300)
# Light feedback timings and battle health bands.
LIGHT_FIRE_S = 0.35
LIGHT_HIT_S = 0.5
LIGHT_MINE_HIT_S = 1.2
HP_CAUTION = 60
HP_CRITICAL = 20
GRID_SPEED = 320            # speed used to drive to the start line
GRID_RETRY_SPEED = 500      # a missed grid attempt costs a full lap; still quick, but lane changes stay reliable
GRID_APPROACH_FRAC = 0.0    # crawl from the start of the last piece, so deceleration is early and even
GRID_CRAWL = 300            # 250 mm/s loses track codes on some pieces
GRID_RECOVERY_DRIVE_S = 1.5  # bounded slow search, followed by a pause before retrying
GRID_RECOVERY_PAUSE_S = 2.0
GRID_MIN_RUNUP_MM = 600.0   # start this close to the bar: drive a full lap before lining up
WRONG_WAY_UTURN_S = 4.0     # do not repeat an automatic U-turn before the car had time to flip
WRONG_WAY_SPEED = 300       # keep the correction slow enough for the firmware U-turn to settle
START_SPEED = 400           # launch target at GO; capped by the race and personal limits
PLAYER_RESUME_WINDOW_S = 1.0  # a nudged player car rolls away without a throttle press
PLAYER_RESUME_STALE_S = 1.0   # stale codes before a transition also count as a nudge
LAP_DEBOUNCE_S = 1.0        # ignore another line crossing this soon after the previous one
FINISH_FIX_WINDOW_S = 1.0   # a lap needs a real position fix this soon after the line
FINISH_FIX_MAX_AFTER_MM = 600.0  # and the fix must be this close behind the line
FINISH_FIX_TIME_SLACK_S = 0.15   # measurement time may precede the interpolated crossing slightly

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
class Tether:
    """A hook cable from the attacker to its victim."""
    x0: float
    y0: float
    x1: float
    y1: float
    color: tuple
    until: float


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
class Loadout:
    """The one weapon and one powerup a player takes into a battle."""
    weapon: str = "sniper"
    powerup: str = "uturn"

    def weapon_name(self) -> str:
        return {"sniper": "SNIPER", "blaster": "BLASTER", "mine": "MINE"}.get(self.weapon, self.weapon.upper())

    def powerup_name(self) -> str:
        return {"uturn": "U-TURN", "hook": "HOOK", "anchor": "ANCHOR",
                "booster": "BOOSTER"}.get(self.powerup, self.powerup.upper())


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
        self.color_index = index % len(PLAYER_COLORS)
        self.color = PLAYER_COLORS[self.color_index]
        self.body_color = paint_for_model(car.model, self.color)
        self.engine_rgb = ENGINE_COLORS[self.color_index]
        self.ble = ble
        self._ai_max_speed = max_speed
        self.player_max_speed = 2000
        self.race_speed_limit = 2000
        self.fair_penalty = 0
        self.source: Optional[I.InputSource] = None
        self.ai = False
        self.ai_pref = True                 # lobby: unclaimed car -> AI (True) or parked (False)
        self.race_excluded = False          # left out of the current race (e.g. still on its charger)
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
        self.grid_force_lap = False       # too little run-up to the bar: complete a lap first
        self.grid_seen_start = False      # forced lap has passed the start piece
        self.grid_runup_pending = False   # check the first valid pose of this grid
        self.grid_prev_index: Optional[int] = None
        self.grid_fw_stop = False
        self.grid_fw_repeat_t = 0.0
        self.grid_bar_pending = False
        self.grid_bar_pending_t = 0.0
        self.start_bar_locked = False
        self.laps = 0
        self.lap_start_t: Optional[float] = None
        self.lap_ready_at = 0.0            # debounce: next crossing may count from here
        self._pending_lap_t: Optional[float] = None
        self._pending_lap_duration: Optional[float] = None
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
        self.lives = BATTLE_LIVES
        self.eliminated = False
        self.loadout = Loadout()
        self.target_offset = 0.0
        self.dead_until = 0.0
        self.fire_ready_at = 0.0
        self.mine_ready_at = 0.0
        # Blaster energy (interval [0, 100]); other weapons keep their cooldown.
        self.weapon_energy = BLASTER_ENERGY_MAX
        self.weapon_energy_empty = False
        self.weapon_last_use_t = 0.0
        self.blaster_firing = False
        self.blaster_fire_started_t = 0.0
        self.blaster_next_pulse_t = 0.0
        self.blaster_overheated_until = 0.0
        self.blaster_rumble_next_t = 0.0
        # Powerup energy, shared by the equipped powerup.
        self.powerup_energy = POWERUP_ENERGY_MAX
        self.powerup_last_use_t = 0.0
        self.uturn_state = "idle"           # idle | flipping_out | reversing | flipping_back
        self.uturn_t = 0.0
        self.uturn_ready_at = 0.0
        self.uturn_reverse_started_t = 0.0
        self.uturn_retries = 0
        self.hook_target: Optional["CarState"] = None
        self.hooked_until = 0.0
        self.hooked_by: Optional["CarState"] = None
        self.hook_light_active = False
        self.booster_light_active = False
        self.anchor_until = 0.0
        self.anchor_active = False
        self.booster_active = False
        self.booster_ready_at = 0.0
        self.ai_blaster_until = 0.0
        self.ai_blaster_next_t = 0.0
        self.ai_hook_until = 0.0
        self.ai_hook_next_t = 0.0
        self.ai_booster_until = 0.0
        self.ai_booster_next_t = 0.0
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
        self.ai_search_anyway = False     # a nudge allows one bounded search off track
        self.boost_until = 0.0
        self.boost_ready_at = 0.0
        self.front_until = 0.0
        self.tail_until = 0.0
        self.tail_priority = 0
        self._health_light_state = None
        self._lights_dark = False

        self._last_speed_sent = 0
        self._last_speed_t = 0.0
        self._force_speed_send = False       # resend even when the held target is unchanged
        self.track_resume_t = 0.0           # last off-track -> on-track transition
        self._status_on_track: Optional[bool] = None
        self._last_drive_input = 0.0        # throttle minus brake; detects edge/release
        self.cruise_speed = 0.0             # player's held target speed (R2/L2 or W/S), not a direct trigger value
        self.manual_reverse = False         # player pressed U-turn on purpose: leave the automatic correction off
        self.last_uturn_t = 0.0             # last manual or automatic U-turn; prevents double commands
        self._last_lane_sent = 0.0
        self._last_lane_t = 0.0
        self._last_steer_t = 0.0
        self._last_offset_told = 0.0
        self._lane_retry_t = 0.0
        self._steer_dir = 0                 # last stick direction; one lane step per push
        self.desired_lane: Optional[float] = None   # sticky lane goal (snap / AI / grid)
        self.following = None                         # car ahead in the same lane
        self.following_gap_mm: Optional[float] = None
        self._follow_release_since: Optional[float] = None

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
        """Lobby selection: parked -> easy -> normal -> hard -> extreme -> parked."""
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
        elif msg_id == P.MSG_STATUS_UPDATE and isinstance(decoded, P.VehicleStatus):
            self.battery.charging = decoded.on_charger
            if decoded.on_charger and self.loc is not None:
                self.loc.on_track = False
            if (not self.start_bar_locked and decoded.on_track and not decoded.on_charger
                    and self._status_on_track is False):
                self.track_resume_t = received_at
                self._force_speed_send = True
                log.info("%s: nudge detected (status on_track)", self.name)
            self._status_on_track = decoded.on_track
        elif msg_id == P.MSG_COLLISION_DETECTED:
            if not self.start_bar_locked:
                if self.source is not None and self.cruise_speed <= 0:
                    # A bump while the player car is stopped counts as a nudge,
                    # including while it is still off the track and being placed.
                    self.track_resume_t = received_at
                    self._force_speed_send = True
                    log.info("%s: nudge detected (collision)", self.name)
                if (self.ai and self.ai_track_recovery == "waiting" and self.car.connected
                        and not self.charging):
                    # The nudge is a track signal even when no code was read
                    # yet: allow one bounded slow search so the AI can find
                    # the track without a second push.
                    self.ai_track_recovery = "searching"
                    self.ai_track_recovery_t = received_at
                    self.ai_search_anyway = True
                    self._force_speed_send = True
                    log.info("%s: nudge detected (collision), searching", self.name)
        elif msg_id == P.MSG_SPEED_UPDATE and isinstance(decoded, P.SpeedUpdate) and self.loc is not None:
            self.loc.speed = float(decoded.actual_mm_s)
            self.loc.filter.set_speed(self.loc.direction * self.loc.speed, received_at)
            self.loc.rebase_engine_speed(self.loc.direction * self.loc.speed, received_at)
        if self.loc is not None:
            was_on_track = self.loc.on_track
            stale_gap = received_at - self.loc.last_update
            self.loc.on_message(msg_id, decoded, received_at=received_at,
                                measurement_delay=self.position_lead_s)
            if (not self.start_bar_locked
                    and msg_id in (P.MSG_LOCALIZATION_POSITION_UPDATE,
                                   P.MSG_LOCALIZATION_TRANSITION_UPDATE)
                    and self.loc.on_track
                    and (not was_on_track or stale_gap >= PLAYER_RESUME_STALE_S)):
                # The car was placed back on the track and nudged. A player
                # car uses this to roll away by itself; AI cars already resume
                # through their track-recovery state.
                self.track_resume_t = received_at
                # The motor may have stopped while the car was off track. A
                # fresh write is needed for AI and player cars alike, even when
                # the target speed itself did not change.
                self._force_speed_send = True
                if self.source is not None and self.cruise_speed <= 0:
                    log.info("%s: nudge detected (msg 0x%02X, code gap %.2fs)",
                             self.name, msg_id, max(0.0, stale_gap))
            if (msg_id == P.MSG_LANE_CHANGE_UPDATE and isinstance(decoded, dict)
                    and "offset_mm" in decoded and "target_mm" in decoded):
                self.loc.on_lane_update(float(decoded["offset_mm"]),
                                        float(decoded["target_mm"]))
            if msg_id == P.MSG_LOCALIZATION_POSITION_UPDATE and self.loc.direction < 0:
                self._reverse_reports += 1
            if self.start_bar_locked and msg_id in (
                    P.MSG_LOCALIZATION_POSITION_UPDATE,
                    P.MSG_LOCALIZATION_TRANSITION_UPDATE,
                    P.MSG_SPEED_UPDATE):
                # Late packets during the countdown must not move the known
                # start-grid pose before the car is released.
                self.loc.freeze_at_start_bar(received_at)
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
        return self.eliminated or now < self.dead_until

    def powerup_busy(self) -> bool:
        return (self.uturn_state != "idle" or self.hook_target is not None
                or self.booster_active or self.anchor_active)

    def speed_sent(self) -> int:
        return self._last_speed_sent

    # ---- commands ----
    def _send(self, payload: bytes) -> None:
        self.ble.fire(self.car.send(payload))

    def command_speed(self, desired: float, now: float, force: bool = False) -> None:
        if now < self.dead_until:
            speed = 0
        elif self.anchor_active and now < self.anchor_until:
            speed = 0
        else:
            speed = int(round(desired))
        # A hook slows the victim's commanded target, never below crawl speed.
        if speed > 0 and now < self.hooked_until:
            speed = int(round(max(HOOK_MIN_SPEED, speed * HOOK_SLOW_FACTOR)))
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
        due = now - self._last_speed_t >= SPEED_SEND_INTERVAL_S
        # A release/reversal must land exactly on the held target, not wait
        # for the next sparse update. A car that was stopped off-track needs a
        # fresh write even when the held target itself did not change; the
        # firmware does not resume an old target by itself.
        forced = force or self._force_speed_send
        if zeroing or (forced and speed > 0) or ((changed or stale) and due):
            self._force_speed_send = False
            self._last_speed_sent, self._last_speed_t = speed, now
            self.ble.fire(self.car.set_speed(speed, accel))
        if speed == 0 and self.loc is not None:
            self.loc.speed = 0.0
            self.loc.filter.set_speed(0, now)
            self.loc.rebase_engine_speed(0.0, now)

    def stop_on_transition(self) -> None:
        """Ask the firmware to brake itself at the next transition bar (grid lineup)."""
        self._send(P.stop_on_transition())

    def hard_stop(self, now: float, accel: int = ACCEL_STOP) -> None:
        self.cruise_speed = 0.0
        self._last_drive_input = 0.0
        self._last_speed_sent, self._last_speed_t = 0, now
        self.ble.fire(self.car.set_speed(0, accel))
        if self.loc is not None:
            self.loc.speed = 0.0
            self.loc.filter.set_speed(0, now)
            self.loc.rebase_engine_speed(0.0, now)

    def participates(self) -> bool:
        return (self.source is not None or self.ai) and not self.race_excluded

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
    def set_player_color(self, index: int) -> bool:
        """Move the car's player colour; return True when it changed."""
        index %= len(PLAYER_COLORS)
        if index == self.color_index:
            return False
        self.color_index = index
        self.color = PLAYER_COLORS[index]
        self.engine_rgb = ENGINE_COLORS[index]
        self._health_light_state = None
        return True

    def _send_patterns(self, channels: list[tuple[int, int, int, int, int]]) -> None:
        """A 0x33 packet carries at most three channels; split larger batches."""
        for i in range(0, len(channels), 3):
            self._send(P.lights_pattern(channels[i:i + 3]))

    def _clear_light_events(self) -> None:
        self.front_until = 0.0
        self.tail_until = 0.0
        self.tail_priority = 0

    def _all_lights_off_channels(self) -> list[tuple[int, int, int, int, int]]:
        return [(channel, P.EFFECT_STEADY, 0, 0, 0) for channel in range(6)]

    def set_roof_color(self) -> None:
        """Show the steady player colour without touching front or tail."""
        if self._lights_dark:
            return
        self._health_light_state = ("steady", self.engine_rgb)
        self._send(P.engine_color(*self.engine_rgb))

    def lights_idle(self) -> None:
        self._clear_light_events()
        self._lights_dark = False
        self.hook_light_active = False
        self.booster_light_active = False
        self.hooked_until = 0.0
        self._health_light_state = ("steady", self.engine_rgb)
        self._send(P.lights_off(0x0F))
        r, g, b = self.engine_rgb
        self._send_patterns([
            (P.LC_RED, P.EFFECT_STEADY, r, r, 0),
            (P.LC_GREEN, P.EFFECT_STEADY, g, g, 0),
            (P.LC_BLUE, P.EFFECT_STEADY, b, b, 0),
            (P.LC_TAIL, P.EFFECT_STEADY, 0, 0, 0),
            (P.LC_FRONTL, P.EFFECT_STEADY, 0, 0, 0),
            (P.LC_FRONTR, P.EFFECT_STEADY, 0, 0, 0),
        ])

    def lights_countdown(self) -> None:
        """Player-colour roof blink for the 3-2-1 countdown."""
        self._clear_light_events()
        self._lights_dark = False
        self.hook_light_active = False
        self.booster_light_active = False
        self.hooked_until = 0.0
        self._health_light_state = None
        self._send(P.lights_off(0x0F))
        self._send(P.engine_flash(*self.engine_rgb, cycles=10))
        self._send(P.lights_pattern([
            (P.LC_TAIL, P.EFFECT_STEADY, 0, 0, 0),
            (P.LC_FRONTL, P.EFFECT_STEADY, 0, 0, 0),
            (P.LC_FRONTR, P.EFFECT_STEADY, 0, 0, 0),
        ]))

    def update_health_light(self, battle: bool, force: bool = False) -> None:
        """Battle only: steady -> slow blink -> fast blink as HP drops."""
        if self._lights_dark:
            return
        if not battle:
            band = "steady"
        elif self.hp <= 0:
            band = "off"
        elif self.hp <= HP_CRITICAL:
            band = "fast"
        elif self.hp <= HP_CAUTION:
            band = "slow"
        else:
            band = "steady"
        state = (band, self.engine_rgb)
        if not force and state == self._health_light_state:
            return
        self._health_light_state = state
        if band == "off":
            self._send(P.engine_color(0, 0, 0))
        elif band == "slow":
            self._send(P.engine_flash(*self.engine_rgb, cycles=10))
        elif band == "fast":
            self._send(P.engine_flash(*self.engine_rgb, cycles=30))
        else:
            self._send(P.engine_color(*self.engine_rgb))

    def lights_fire(self, now: float) -> None:
        if self._lights_dark or now < self.front_until:
            return
        self._send(P.lights_on(P.LIGHT_FRONTLIGHTS))
        self.front_until = now + LIGHT_FIRE_S

    def lights_hit(self, now: float, mine: bool = False) -> None:
        """Flash only the tail; a mine hit outranks and outlasts a blaster hit."""
        if self._lights_dark:
            return
        priority = 3 if mine else 2
        if now < self.tail_until and priority <= self.tail_priority:
            return
        self.tail_priority = priority
        self.tail_until = now + (LIGHT_MINE_HIT_S if mine else LIGHT_HIT_S)
        if mine:
            pattern = [(P.LC_TAIL, P.EFFECT_THROB, 0, P.MAX_LIGHT_INTENSITY, 16)]
        else:
            pattern = [(P.LC_TAIL, P.EFFECT_FLASH, 0, 5, 20)]
        self._send(P.lights_pattern(pattern))

    def _front_pattern(self, red: int, green: int, effect: int = P.EFFECT_STEADY) -> None:
        """Always send both front channels: a missing one keeps its old colour."""
        self._send(P.lights_pattern([
            (P.LC_FRONTL, effect, red, red, 0),
            (P.LC_FRONTR, effect, green, green, 0),
        ]))

    def lights_hook_attacker(self, on: bool) -> None:
        """Yellow front lights while the hook is attached (red + green)."""
        if self._lights_dark or self.hook_light_active == on:
            return
        self.hook_light_active = on
        if on:
            self._front_pattern(P.MAX_LIGHT_INTENSITY, P.MAX_LIGHT_INTENSITY)
        elif not self.front_until and not self.booster_light_active:
            self._front_pattern(0, 0)

    def lights_booster(self, on: bool) -> None:
        """Green front lights while the booster is active."""
        if self._lights_dark or self.booster_light_active == on:
            return
        self.booster_light_active = on
        if on:
            self._front_pattern(0, P.MAX_LIGHT_INTENSITY)
        elif not self.front_until and not self.hook_light_active:
            self._front_pattern(0, 0)

    def lights_hooked(self, now: float) -> None:
        """A slow red tail pattern while the hook is attached; hits still win."""
        if self._lights_dark:
            return
        if now < self.tail_until and self.tail_priority >= 1:
            return
        self.tail_priority = 1
        self.tail_until = now + 0.7
        self._send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_FLASH, 0, 5, 2)]))

    def lights_unhook(self) -> None:
        self.hooked_until = 0.0
        if self.tail_priority <= 1:
            self.tail_priority = 0
            self.tail_until = 0.0
            self._send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_STEADY, 0, 0, 0)]))

    def lights_dead(self) -> None:
        self._clear_light_events()
        self._lights_dark = True
        self.hook_light_active = False
        self.booster_light_active = False
        self.hooked_until = 0.0
        self._health_light_state = ("off", self.engine_rgb)
        self._send(P.lights_off(0x0F))
        self._send_patterns(self._all_lights_off_channels())

    def tick_lights(self, now: float) -> None:
        """Expire the short fire/tail overlays."""
        if self.front_until and now >= self.front_until:
            self.front_until = 0.0
            if self._lights_dark:
                pass
            elif self.hook_light_active:
                self._front_pattern(P.MAX_LIGHT_INTENSITY, P.MAX_LIGHT_INTENSITY)
            elif self.booster_light_active:
                self._front_pattern(0, P.MAX_LIGHT_INTENSITY)
            else:
                self._send(P.lights_off(P.LIGHT_FRONTLIGHTS))
        if self.tail_until and now >= self.tail_until:
            self.tail_until = 0.0
            if self._lights_dark:
                self.tail_priority = 0
            elif self.hooked_until > now:
                self.tail_priority = 1
                self.tail_until = now + 0.7
                self._send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_FLASH, 0, 5, 2)]))
            else:
                self.tail_priority = 0
                self._send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_STEADY, 0, 0, 0)]))

    def lights_identify(self, on: bool) -> None:
        self._send(P.lights_on(P.LIGHT_FRONTLIGHTS) if on else P.lights_off(P.LIGHT_FRONTLIGHTS))

    def lights_finish(self, winner: bool) -> None:
        self._clear_light_events()
        if winner:
            self.lights_idle()
        else:
            self._lights_dark = True
            self._health_light_state = ("off", self.engine_rgb)
            self._send(P.lights_off(0x0F))
            self._send_patterns(self._all_lights_off_channels())

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
        self.tethers: list[Tether] = []
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
        self._battle_contenders = 0
        for c in cars:
            c.set_track(track)

    # ---- lifecycle ----
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
        self.tethers.clear()

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
        # Cars still on their charger when the grid forms are left out of this
        # race instead of blocking the start until they are removed.
        for c in self.cars:
            c.race_excluded = c.charging
        self.phase = "grid"
        self.running = False
        self.update_fair_penalties()
        participants = sum(1 for c in self.cars if c.participates() and self.track is not None)
        lane_order = grid_lane_order(participants)
        lane_i = 0
        for c in self.cars:
            c.hp = MAX_HP
            c.lives = BATTLE_LIVES
            c.eliminated = False
            c.dead_until = 0.0
            c.kills = c.deaths = 0
        for c in self.cars:
            c.lights_idle()
            if not c.participates() or self.track is None:
                c.grid_state = ""
                c.start_bar_locked = False
                c.hard_stop(now)
                if c.race_excluded:
                    self.log(f"{c.name} is on its charger - left out of this race", c.color)
                continue
            c.grid_lane = lane_order[lane_i % len(lane_order)]
            lane_i += 1
            c.grid_state = "driving"
            c.grid_t0 = now
            c.grid_recovery_t = None
            c.dead_until = 0.0
            c.grid_uturn_t = 0.0
            c.grid_laps = 0
            c.grid_retry_lap = False
            c.grid_force_lap = False
            c.grid_seen_start = False
            c.grid_runup_pending = True
            c.grid_prev_index = None
            c.grid_fw_stop = False
            c.grid_fw_repeat_t = 0.0
            c.grid_bar_pending = False
            c.grid_bar_pending_t = 0.0
            c.start_bar_locked = False
            c.cruise_speed = 0.0
            c.manual_reverse = False
            c.last_uturn_t = 0.0
            c.uturn_state = "idle"
            c.uturn_retries = 0
            c.hook_target = None
            c.hooked_until = 0.0
            c.anchor_active = False
            c.booster_active = False
            c.set_lane(c.grid_lane, now)
            c._last_lane_sent = 999.0     # force a lane command once the car is localized
            if c.car.connected and not c.charging:
                c.command_speed(GRID_SPEED, now)
            else:
                c.hard_stop(now)
        self.log("forming the grid: cars drive to the start line")

    def grid_tick(self, dt: float) -> bool:
        """Wait for every grid participant; parked and charger cars are left out."""
        now = time.monotonic()
        for c in self.cars:
            if c.loc:
                c.loc.advance(dt, now)
        done = True
        for c in self.cars:
            st = c.grid_state
            if not c.participates() or not st:
                c.grid_state = ""
                c.command_speed(0, now)
                continue
            if c.charging:
                # Put back on the charger during the grid: leave it out for
                # this race instead of blocking the start indefinitely.
                c.race_excluded = True
                c.grid_state = ""
                c.command_speed(0, now)
                self.log(f"{c.name} is on its charger - left out of this race", c.color)
                continue
            if not c.car.connected:
                c.grid_state = "waiting for connection"
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
                c.command_speed(min(300, c.max_speed) if searching else 0, now)
                continue
            if c.grid_recovery_t is not None:
                c.grid_recovery_t = None
                c.grid_prev_index = None
                c._last_lane_sent = 999.0
            loc = c.loc
            assert loc is not None
            previous_index = c.grid_prev_index
            c.grid_prev_index = loc.index
            if c.grid_runup_pending:
                c.grid_runup_pending = False
                if self._grid_runup_too_short(c):
                    c.grid_force_lap = c.grid_retry_lap = True
                    n = len(self.track.pieces) if self.track else 1
                    # Only a car on the last piece still has to pass the start
                    # piece before the forced lap counts. A car already past the
                    # line (piece 1 lies inside the trigger zone) has passed it
                    # already; waiting for piece 0 again would cost a second lap.
                    c.grid_seen_start = n > 1 and loc.index != n - 1
                    self.log(f"{c.name}: too close to the start line; one full lap first", c.color)
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
                if c.grid_force_lap:
                    if loc.index == 0:
                        c.grid_seen_start = True
                    approaching = (n > 1 and c.grid_seen_start
                                   and loc.index == n - 1 and loc.frac > GRID_APPROACH_FRAC)
                else:
                    approaching = ((loc.index == n - 1 and loc.frac > GRID_APPROACH_FRAC)
                                   or (loc.index == 0 and loc.frac < 0.4))
                if approaching:
                    was_forced = c.grid_force_lap
                    c.grid_retry_lap = False
                    c.grid_force_lap = False
                    c.grid_seen_start = False
                    if was_forced:
                        # The lap deliberately passed the start piece without
                        # stopping. Do not treat that as the normal overshoot on
                        # this same frame; crawl into the next approach.
                        c.grid_state = "approach"
                        c.command_speed(GRID_CRAWL, now)
                        c.apply_lane(now)
                        continue
                else:
                    c.grid_state = "retry lap"
                    c.command_speed(GRID_RETRY_SPEED, now)
                    c.apply_lane(now)
                    continue
            n = len(self.track.pieces) if self.track else 1
            lane_now = c.actual_lane()
            in_lane = self._grid_lane_ok(c, lane_now)
            # Entering the start piece, ask the firmware to brake itself exactly on the
            # transition bar. This removes the host/BLE latency from the grid stop.
            if (not c.grid_fw_stop and loc.index == 0 and loc.direction > 0
                    and loc.frac <= 0.30 and in_lane):
                c.stop_on_transition()
                c.grid_fw_stop = True
                c.grid_fw_repeat_t = now + 0.05   # one repeat in case the first write is lost
                c.grid_state = "approach"
                self.log(f"{c.name}: firmware stop armed (lane {lane_now:+.0f})", c.color)
            elif c.grid_fw_stop:
                if c.grid_fw_repeat_t and now >= c.grid_fw_repeat_t:
                    c.stop_on_transition()
                    c.grid_fw_repeat_t = 0.0
                c.grid_state = "approach"
            else:
                # final approach: crawl from the start of the last piece so the cars slow down early
                # and enter the start piece slowly
                approaching = ((loc.index == n - 1 and loc.frac > GRID_APPROACH_FRAC)
                               or (loc.index == 0 and loc.frac < 0.8))
                if approaching and in_lane:
                    c.grid_state = "approach"
                    c.command_speed(GRID_CRAWL, now)
                else:
                    c.grid_state = "driving"
                    c.command_speed(GRID_SPEED, now)
                c.apply_lane(now)
            # The bar between the pre-finish and finish halves is a transition that every
            # car reports in every lane at the same physical point.
            bar = self.track.start_bar_frac if self.track else 0.5
            if c.grid_fw_stop:
                # The engine must not decide "overshot" from its own stale
                # speed prediction.  Hold at the bar and wait for the real
                # bar transition / code or a short settle timeout.
                if not c.grid_bar_pending and (
                        (loc.index == 0 and loc.frac >= bar)
                        or (previous_index == 0 and loc.index != 0)):
                    c.grid_bar_pending = True
                    c.grid_bar_pending_t = now
                    loc.hold_at_start_bar(now)
                    c.command_speed(0, now)
                    c.grid_state = "bar pending"
                if c.grid_bar_pending:
                    fresh_bar = loc.sub_id == FINISH_ID
                    fresh_code = (loc.last_code_t > c.grid_bar_pending_t
                                  and loc.last_code_piece == FINISH_ID)
                    moved_on = (loc.last_transition_t > c.grid_bar_pending_t
                                and loc.index != 0)
                    if fresh_bar or fresh_code:
                        loc.freeze_at_start_bar(now)
                        c.command_speed(0, now)
                        c.grid_lane = lane_now
                        c.grid_state = "placed"
                        c.grid_fw_stop = False
                        c.grid_bar_pending = False
                        c.grid_fw_repeat_t = 0.0
                        c.start_bar_locked = True
                        self.log(f"{c.name} on the start line (lane {lane_now:+.0f})", c.color)
                    elif moved_on:
                        c.grid_laps += 1
                        c.grid_retry_lap = True
                        c.grid_fw_stop = False
                        c.grid_bar_pending = False
                        c.grid_fw_repeat_t = 0.0
                        c.grid_state = "retry lap"
                        c._last_lane_sent = 999.0
                        c.command_speed(GRID_RETRY_SPEED, now)
                        self.log(f"{c.name}: overshot; another lap to retry the start", c.color)
                    elif now - c.grid_bar_pending_t > 0.8:
                        # The firmware stopped but sent no usable bar packet.
                        loc.freeze_at_start_bar(now)
                        c.command_speed(0, now)
                        c.grid_lane = lane_now
                        c.grid_state = "placed"
                        c.grid_fw_stop = False
                        c.grid_bar_pending = False
                        c.grid_fw_repeat_t = 0.0
                        c.start_bar_locked = True
                        self.log(f"{c.name} on the start line (lane {lane_now:+.0f})", c.color)
                    else:
                        loc.hold_at_start_bar(now)
                        c.command_speed(0, now)
                        c.grid_state = "bar pending"
                    continue
            fw_bar = c.grid_fw_stop and loc.sub_id == FINISH_ID
            on_bar = fw_bar or (loc.index == 0 and loc.sub_id == FINISH_ID
                                and bar <= loc.frac < bar + 0.1)
            overshot = (loc.index == 0 and loc.frac >= bar + 0.1) or (previous_index == 0 and loc.index != 0)
            if on_bar and in_lane:
                if not c.grid_fw_stop:
                    c.hard_stop(now)   # fallback when the firmware stop was not armed
                loc.freeze_at_start_bar(now)
                c.command_speed(0, now)
                c.grid_lane = lane_now
                c.grid_state = "placed"
                c.grid_fw_stop = False
                c.grid_fw_repeat_t = 0.0
                c.start_bar_locked = True
                self.log(f"{c.name} on the start line (lane {lane_now:+.0f})", c.color)
            elif c.grid_state != "placed" and (on_bar or overshot):
                c.grid_laps += 1
                c.grid_retry_lap = True
                c.grid_fw_stop = False
                c.grid_fw_repeat_t = 0.0
                c.grid_bar_pending = False
                c.grid_state = "retry lap"
                c._last_lane_sent = 999.0
                c.command_speed(GRID_RETRY_SPEED, now)
                if overshot:
                    why = "overshot"
                else:
                    why = "wrong lane"
                self.log(f"{c.name}: {why}; another lap to retry the start", c.color)
        return done

    def _grid_runup_too_short(self, c: "CarState") -> bool:
        """True when either side of the start bar is closer than the minimum run-up.

        Cars may be parked just behind the line or have overshot it by a few
        centimetres.  Both cases otherwise look "on the bar" immediately, so the
        car would stop before it could line up cleanly.  Require a real lap in
        both directions instead.
        """
        loc = c.loc
        if self.track is None or loc is None or loc.index is None:
            return False
        bar = loc.filter.distance(0, self.track.start_bar_frac)
        here = loc.filter.distance(loc.index, loc.frac)
        length = loc.filter.length
        return min((bar - here) % length, (here - bar) % length) < GRID_MIN_RUNUP_MM

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
                c.lights_countdown()

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
        self._battle_contenders = sum(
            1 for c in self.cars
            if c.participates() and c.car.connected and not c.charging)
        for c in self.cars:
            if c.grid_state == "placed" and c.loc is not None:
                # The countdown is longer than the AI freshness window; the
                # last grid fix is still the known start-line pose.
                c.loc.last_update = now
                c.ai_track_recovery = ""
                c.start_bar_locked = False
            c.hp = MAX_HP
            c.kills = c.deaths = c.laps = 0
            c.lives = BATTLE_LIVES
            c.eliminated = False
            c.fire_ready_at = c.mine_ready_at = 0.0
            c.weapon_energy = BLASTER_ENERGY_MAX
            c.weapon_energy_empty = False
            c.weapon_last_use_t = 0.0
            c.blaster_firing = False
            c.blaster_fire_started_t = 0.0
            c.blaster_next_pulse_t = 0.0
            c.blaster_overheated_until = 0.0
            c.blaster_rumble_next_t = 0.0
            c.powerup_energy = POWERUP_ENERGY_MAX
            c.powerup_last_use_t = 0.0
            c.uturn_state = "idle"
            c.uturn_t = 0.0
            c.uturn_ready_at = 0.0
            c.uturn_reverse_started_t = 0.0
            c.uturn_retries = 0
            c.hook_target = None
            c.hooked_until = 0.0
            c.hooked_by = None
            c.hook_light_active = False
            c.booster_light_active = False
            c.anchor_until = 0.0
            c.anchor_active = False
            c.booster_active = False
            c.booster_ready_at = 0.0
            c.ai_blaster_until = 0.0
            c.ai_blaster_next_t = 0.0
            c.ai_hook_until = 0.0
            c.ai_hook_next_t = 0.0
            c.ai_booster_until = 0.0
            c.ai_booster_next_t = 0.0
            if c.ai:
                c.loadout = Loadout(weapon=random.choice(WEAPONS), powerup=random.choice(POWERUPS))
            c.last_lap_s = c.best_lap_s = None
            c.race_finish_s = None
            c.lap_start_t = None
            c.lap_ready_at = 0.0
            c._pending_lap_t = None
            c._pending_lap_duration = None
            c._prev_progress = None
            c._prev_progress_t = None
            c._route_epoch = c.loc.route_epoch if c.loc else None
            c.lap_note = "cross start to time a lap"
            if c.loc:
                c.loc.motion_samples.clear()
                c.loc.position_fixes.clear()
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
            c.manual_reverse = False
            c.last_uturn_t = 0.0
            c.uturn_state = "idle"
            c.uturn_retries = 0
            c.hook_target = None
            c.hooked_until = 0.0
            c.anchor_active = False
            c.booster_active = False
            if (c.grid_state == "placed" and c.map_position_known() and c.loc is not None
                    and c.loc.direction > 0 and c.loc.index == 0
                    and self.track.start_bar_frac <= c.loc.frac < self.track.start_bar_frac + 0.1):
                # A car correctly parked on the bar starts its first timed lap at GO.
                c.lap_start_t = now
                c.lap_note = "timing"
                c._prev_progress = c.loc.progress()
                if c.loc._display_distance is not None:
                    c._prev_progress = self._route_progress(c, c.loc._display_distance)
                c._prev_progress_t = now
            c.dead_until = 0.0
            if c.grid_state in ("off track", "charging", "missed start"):
                c.ai = False
                c.source = None
            elif not c.grid_state:
                c.ai = self.ai_enabled and c.source is None and c.ai_pref
            c.ai_next_lane_t = now + (0.35 if c.ai_difficulty == "extreme" else random.uniform(3, 6))
            if c.participates() and c.grid_state == "placed" and c.car.connected and not c.charging:
                # The race starts in motion: players keep this as their held
                # speed, while AI immediately takes over its own target.
                launch = min(START_SPEED, c.max_speed)
                if c.source is not None:
                    c.cruise_speed = float(launch)
                c.command_speed(launch, now, force=True)
            c.lights_idle()
        self.log("race started")

    def stop_all(self) -> None:
        now = time.monotonic()
        self.running = False
        self.phase = "idle"
        for c in self.cars:
            c.grid_state = ""
            c.grid_fw_stop = False
            c.grid_bar_pending = False
            c.start_bar_locked = False
            c.grid_force_lap = False
            c.grid_seen_start = False
            c.grid_runup_pending = False
            c.manual_reverse = False
            c.last_uturn_t = 0.0
            c.uturn_state = "idle"
            c.uturn_retries = 0
            c.hook_target = None
            c.hooked_until = 0.0
            c.anchor_active = False
            c.booster_active = False
        for c in self.cars:
            c.hard_stop(now)
            c.lights_idle()

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
            return ((p - self.track.start_bar_frac) % len(self.track.pieces)
                    if p is not None and self.track else -1.0)
        if self.mode == "race":
            return sorted(self.cars, key=lambda c: (-c.laps, c.race_finish_s if c.race_finish_s is not None else float("inf"),
                                                    -progress(c), c.index))
        return sorted(self.cars, key=lambda c: (not c.participates(), -c.lives, -c.kills,
                                                c.deaths, -c.laps, c.index))

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
            c._pending_lap_t = None
            c._pending_lap_duration = None
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
                    c._pending_lap_t = None
                    c._pending_lap_duration = None
                    c._lap_travel = 0.0
                    self._lap_note(c, "lap reset: position reacquired")
                c._route_epoch = epoch
                if distance is not None:
                    progress = self._route_progress(c, distance)
                    self._track_lap_sample(c, progress, received, unwrapped=True)
            self._resolve_pending_lap(c, now)
            return
        self._track_lap_sample(c, c.loc.progress(), now)
        self._resolve_pending_lap(c, now)

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
            c._pending_lap_t = None
            c._pending_lap_duration = None
            c._lap_travel = 0.0
            return
        c._lap_travel += delta
        bar = self.track.start_bar_frac if self.track else 0.5
        to_line = (bar - prev) % n
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
                    fraction = ((distance(bar) - distance(prev)) % total) / travelled
            crossing_t = now if previous_t is None else previous_t + (now - previous_t) * fraction
            overshoot = delta - to_line
            if crossing_t < c.lap_ready_at:
                # A crossing immediately after the previous one is a position
                # bounce, not another lap. Restart the attempt at the line
                # without extending the debounce window.
                c.lap_start_t = crossing_t
                c._lap_travel = overshoot
                return
            c.lap_ready_at = crossing_t + LAP_DEBOUNCE_S
            if c.lap_start_t is not None and c._lap_travel - overshoot >= n - 0.15:
                lap = crossing_t - c.lap_start_t
                if lap > 0:
                    if c._pending_lap_t is not None:
                        self._resolve_pending_lap(c, now)
                    c._pending_lap_t = crossing_t
                    c._pending_lap_duration = lap
                c.lap_start_t = crossing_t
                c.lap_note = "timing"
                c._lap_travel = overshoot
            elif c.lap_start_t is None:
                c.lap_start_t = crossing_t
                c.lap_note = "timing"
                c._lap_travel = overshoot
        self._resolve_pending_lap(c, now)

    def _finish_fix_after_line(self, c: CarState, crossing_t: float) -> bool:
        """Require a real 0x27 fix just past the line before crediting a lap."""
        loc = c.loc
        if self.track is None or loc is None or not loc.position_fixes:
            return False
        bar = loc.filter.distance(0, self.track.start_bar_frac)
        for fix_t, index, frac, direction, on_track in reversed(loc.position_fixes):
            if fix_t < crossing_t - FINISH_FIX_TIME_SLACK_S:
                break
            if fix_t > crossing_t + FINISH_FIX_WINDOW_S or not on_track or direction <= 0:
                continue
            after = (loc.filter.distance(index, frac) - bar) % loc.filter.length
            if after <= FINISH_FIX_MAX_AFTER_MM:
                return True
        return False

    def _complete_lap(self, c: CarState, crossing_t: float, lap: float) -> None:
        c.laps += 1
        c.last_lap_s = lap
        if c.best_lap_s is None or lap < c.best_lap_s:
            c.best_lap_s = lap
            self.log(f"{c.name} lap {c.laps}: {lap:.2f}s (best)", c.color)
        else:
            self.log(f"{c.name} lap {c.laps}: {lap:.2f}s", c.color)
        if self.mode == "race" and c.laps >= self.lap_target and c.race_finish_s is None and self.race_start_t is not None:
            c.race_finish_s = crossing_t - self.race_start_t
        if self.mode == "race" and c.laps >= self.lap_target and (self.winner is None or crossing_t < self.finish_t):
            self.winner = c
            self.finish_t = crossing_t
            self.effect("boom", c.world(), c.color, 1.5)
            self.log(f"{c.name} WINS the race!", c.color)

    def _resolve_pending_lap(self, c: CarState, now: float) -> None:
        """Confirm or drop a lap candidate once real position evidence arrives."""
        crossing_t = c._pending_lap_t
        if crossing_t is None:
            return
        lap = c._pending_lap_duration
        if lap is not None and lap > 0 and self._finish_fix_after_line(c, crossing_t):
            c._pending_lap_t = None
            c._pending_lap_duration = None
            self._complete_lap(c, crossing_t, lap)
            return
        if now - crossing_t > FINISH_FIX_WINDOW_S:
            c._pending_lap_t = None
            c._pending_lap_duration = None
            self.log(f"{c.name}: lap not confirmed (no position fix past the line)", WARN_COLOR)

    # ---- physical following -------------------------------------------------
    def _follow_pair_ok(self, rear: CarState, leader: CarState,
                        now: Optional[float] = None) -> bool:
        if rear is leader or rear.loc is None or leader.loc is None:
            return False
        a, b = rear.loc, leader.loc
        if not (a.on_track and b.on_track and a.index is not None and b.index is not None):
            return False
        now = time.monotonic() if now is None else now
        if now - b.last_update > FOLLOW_STALE_S:
            return False
        if rear.charging or leader.charging or a.direction != b.direction:
            return False
        if abs(a.lane_mm - b.lane_mm) > LANE_OVERLAP_MM:
            return False
        if any(self.track.pieces[loc.index].kind == CRISSCROSS for loc in (a, b)):
            return False
        return True

    def _following_gap(self, rear: CarState, leader: CarState) -> Optional[float]:
        if rear.loc is None or leader.loc is None:
            return None
        a, b = rear.loc.engine_distance, leader.loc.engine_distance
        if a is None or b is None:
            return None
        # engine_distance is the total distance each car has travelled, so the
        # two origins can differ by whole laps.  Physical following distance is
        # the shortest signed distance on the loop; otherwise a faster follower
        # that has driven one lap more looks a whole lap away and slips through.
        length = rear.loc.filter.length
        return (b - a + length / 2) % length - length / 2

    def _reconcile_vehicle_positions(self, now: float) -> None:
        """Keep same-lane cars in physical order, independent of their speed.

        The filter/engine is per car, so a faster follower would otherwise
        dead-reckon through a slower leader. A persistent following edge with
        hysteresis prevents the order from being recomputed from an already
        overturned prediction.
        """
        cars = [c for c in self.cars if c.loc is not None and c.loc.on_track
                and c.loc.index is not None and not c.charging]
        if len(cars) < 2:
            for c in cars:
                c.following = None
                c.following_gap_mm = None
                c._follow_release_since = None
            return
        for c in cars:
            leader = c.following
            if leader is None:
                c._follow_release_since = None
                continue
            gap = self._following_gap(c, leader)
            # A negative gap means the follower appears ahead; that can be a
            # stale/quantized position update and must not be allowed to flip
            # the physical order.  Only a gap clearly *behind* can release the
            # link, and only after it has persisted across localizer jumps.
            if not self._follow_pair_ok(c, leader, now) or gap is None:
                c.following = None
                c.following_gap_mm = None
                c._follow_release_since = None
            elif gap > FOLLOW_RELEASE_MM:
                if c._follow_release_since is None:
                    c._follow_release_since = now
                if now - c._follow_release_since >= FOLLOW_RELEASE_HOLD_S:
                    c.following = None
                    c.following_gap_mm = None
                    c._follow_release_since = None
                else:
                    c.following_gap_mm = gap
            else:
                c._follow_release_since = None
                c.following_gap_mm = gap
        for c in cars:
            if c.following is not None:
                continue
            candidates = []
            for o in cars:
                if o is c or o.following is c or not self._follow_pair_ok(c, o, now):
                    continue
                gap = self._following_gap(c, o)
                if gap is None or gap <= 0.0 or gap > FOLLOW_LINK_MM:
                    continue
                p = o
                cycle = False
                for _ in range(len(cars)):
                    if p is c:
                        cycle = True
                        break
                    p = p.following
                    if p is None:
                        break
                if not cycle:
                    candidates.append((gap, o))
            if candidates:
                gap, leader = min(candidates, key=lambda item: item[0])
                c.following = leader
                c.following_gap_mm = gap
                c._follow_release_since = None
        for _ in range(2):
            changed = False
            for c in cars:
                leader = c.following
                if leader is None or leader.loc is None or c.loc is None:
                    continue
                if leader.loc.engine_distance is None or c.loc.engine_distance is None:
                    continue
                gap = self._following_gap(c, leader)
                c.following_gap_mm = gap
                if gap is None:
                    continue
                if gap < CAR_MIN_GAP_MM:
                    target = leader.loc.engine_distance - CAR_MIN_GAP_MM
                    speed = leader.loc._engine_speed
                    c.loc.set_engine_position(target, now, min(c.loc._engine_speed, speed))
                    c.following_gap_mm = CAR_MIN_GAP_MM
                    changed = True
            if not changed:
                break

    # ---- per frame ----
    def tick(self, dt: float) -> None:
        now = time.monotonic()
        if not self.running:
            # Grid/idle phases must not inherit a following edge from an
            # earlier race at completely different positions.
            for c in self.cars:
                c.following = None
                c.following_gap_mm = None
                c._follow_release_since = None
        for c in self.cars:
            if c.loc:
                c.loc.advance(dt, now)
        if self.running and self.track is not None:
            self._reconcile_vehicle_positions(now)
        self.fx = [e for e in self.fx if now - e.t0 < e.duration]
        self.tethers.clear()
        self.update_fair_penalties()
        for c in self.cars:
            c.tick_lights(now)
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
                    rgb = self.winner.engine_rgb if blink % 2 == 0 else (0, 0, 0)
                    self.winner._send(P.engine_color(*rgb))
            return
        for c in self.cars:
            self._track_laps(c, now)
            if c.hp <= 0 and not c.is_dead(now):
                c.hp = MAX_HP
                c.lights_idle()
                c.uturn_state = "idle"
                c.uturn_retries = 0
                c.manual_reverse = False
                c.anchor_active = False
                c.booster_active = False
                c.hook_target = None
                self.effect("spawn", c.world(), c.color, 0.8)
                self.log(f"{c.name} respawned", c.color)
                if c.ai:
                    # The 3 s stop leaves the last track code stale. Give the
                    # car a fresh bounded search instead of waiting for a nudge.
                    c.ai_track_recovery = "searching"
                    c.ai_track_recovery_t = now
            if c.participates() and c.source is not None and c.source.alive():
                self._human(c, dt, now)
            elif c.participates() and c.ai:
                self._ai(c, dt, now)
            else:
                c.command_speed(0, now)
            if c.participates() and not c.ai_track_recovery:
                c.apply_lane(now)
            self._tick_car_energy(c, now, dt)
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
        if c.track_resume_t:
            if (now - c.track_resume_t <= PLAYER_RESUME_WINDOW_S
                    and not c.charging and not c.is_dead(now)
                    and src.brake() <= 0.1):
                # A player car placed back on the track and nudged rolls away
                # by itself, like the official app. Braking or moving the
                # throttle afterwards still works normally.
                if c.cruise_speed <= 0:
                    c.cruise_speed = min(START_SPEED, c.max_speed)
                # Re-send the target even when it did not change: the car may
                # have stopped its motor while it was off track.
                c._force_speed_send = True
                log.info("%s: nudged back onto the track, resuming at %d mm/s",
                         c.name, c.cruise_speed)
            c.track_resume_t = 0.0
        if src.pressed(I.LIMIT_UP):
            c.max_speed = min(2000, c.max_speed + 100)
        if src.pressed(I.LIMIT_DOWN):
            c.max_speed = max(200, c.max_speed - 100)
        if src.pressed(I.BOOST):
            self.request_boost(c, now)

        self.update_anchor(c, now)
        # Real input sources return a bool.  Mocks used by the offline tests
        # may return a Mock; only an explicit True counts as held.
        def held(action: int) -> bool:
            try:
                return src.held(action) is True
            except AttributeError:
                return False

        held_powerup = held(I.POWERUP)
        if c.loadout.powerup == "uturn":
            self.update_uturn(c, held_powerup, now, dt)
        elif c.loadout.powerup == "hook":
            self.update_hook(c, held_powerup, now, dt)
        elif c.loadout.powerup == "anchor":
            if src.pressed(I.POWERUP):
                self.start_anchor(c, now)
        elif c.loadout.powerup == "booster":
            self.update_booster(c, held_powerup, now, dt)

        if c.loadout.weapon == "blaster":
            self.update_blaster(c, held(I.FIRE), now, dt)
        elif c.loadout.weapon == "mine":
            if src.pressed(I.FIRE):
                self.drop_mine(c, now)
        else:
            if src.pressed(I.FIRE):
                self.fire_sniper(c, now)

        if c.anchor_active:
            c.command_speed(0, now)
            if src.pressed(I.STOP):
                c.anchor_active = False
                c.hard_stop(now)
            return

        correcting_wrong_way = (self.wrong_way(c) and not c.manual_reverse
                                and c.uturn_state == "idle")
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
            drive = up - down
            was_moving = c.cruise_speed > 0
            c.cruise_speed = max(0.0, min(c.max_speed, c.cruise_speed +
                                          (up * PLAYER_ACCEL_RATE - down * PLAYER_BRAKE_RATE) * dt))
            if c.cruise_speed > 0 or (was_moving and down > 0):
                c.cruise_speed = max(c.cruise_speed, min(MIN_PLAYER_SPEED, c.max_speed))
            if down > 0 or c.cruise_speed <= 0:
                c.boost_until = 0.0
            target = self.boost_target(c, c.cruise_speed, now) if c.cruise_speed > 0 else 0.0
            if c.booster_active:
                target = max(target, self.booster_target(c))
            if c.manual_reverse or c.uturn_state != "idle":
                # A reversing or flipping car stays controllable, but only at a safe speed.
                target = min(target, UTURN_REVERSE_SPEED)
                c.boost_until = 0.0
            if correcting_wrong_way:
                # A slow car flips reliably; the driver's own reverse mode is
                # not capped.
                target = min(target, WRONG_WAY_SPEED)
            edge = (c._last_drive_input == 0.0) != (drive == 0.0)
            reversal = c._last_drive_input * drive < 0
            c.command_speed(target, now, force=edge or reversal)
            c._last_drive_input = drive
        c.steer(src.steer(), dt, now)
        if src.pressed(I.LANE_LEFT):
            c.snap_lane(-1, now)
        if src.pressed(I.LANE_RIGHT):
            c.snap_lane(+1, now)
        if (correcting_wrong_way and c.speed_sent() > 0
                and now - c.last_uturn_t >= WRONG_WAY_UTURN_S):
            c.u_turn()
            c.last_uturn_t = now
            self.log(f"{c.name} turns around (wrong way)", c.color)

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
                or c.is_dead(now)
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

    def _recover_ai_track(self, c: CarState, now: float) -> bool:
        """Return True while normal driving must yield to track recovery."""
        if c.is_dead(now):
            # A destruction stop is intentional, not a lost track. Do not turn
            # its stale codes into a "waiting for a nudge" recovery state.
            c.command_speed(0, now)
            return True
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
                c.ai_search_anyway = False
                c.ai_forward_since = None
                self.log(f"{c.name}: track found, resuming", c.color)
            return False
        if not c.ai_track_recovery:
            c.ai_track_recovery_t = now
            c.ai_search_anyway = False
            # A positive delocalized report means stop; missing telemetry alone
            # gets one 1.5 s search at 300 mm/s. Never keep driving blindly.
            can_probe = available and loc.on_track and (c.speed_sent() > 0 or not c.ai_has_driven)
            c.ai_track_recovery = "searching" if can_probe else "waiting"
            self.cancel_powerups(c)
            c._prev_progress = c.lap_start_t = None
            c._lap_travel = 0.0
            c.ai_learning_piece = None
            c.ai_clean_corners = 0
            self.log(f"{c.name}: {'searching for track codes' if c.ai_track_recovery == 'searching' else 'put back on track and nudge to resume'}", WARN_COLOR)
        if c.ai_track_recovery == "searching" and (not available or now - c.ai_track_recovery_t >= 1.5
                                                  or (not loc.on_track and not c.ai_search_anyway)):
            c.ai_track_recovery = "waiting"
            c.ai_search_anyway = False
            self.log(f"{c.name}: stopped; put back on track and nudge to resume", WARN_COLOR)
        if c.car.connected:
            c.command_speed(min(300, c.max_speed) if c.ai_track_recovery == "searching" else 0, now)
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
        self.update_anchor(c, now)
        if c.anchor_active:
            c.command_speed(0, now)
            return
        if c.loadout.powerup == "hook":
            ahead = self.nearest_ahead(c, now, HOOK_RANGE_MM)
            if ahead is not None and now >= c.ai_hook_next_t:
                c.ai_hook_until = now + random.uniform(1.0, 2.0)
                c.ai_hook_next_t = c.ai_hook_until + random.uniform(1.0, 2.5)
            self.update_hook(c, now < c.ai_hook_until, now, dt)
        elif c.loadout.powerup == "booster":
            if (now >= c.ai_booster_next_t and c.powerup_energy >= BOOSTER_MIN_ENERGY
                    and self._boost_clear(c, now)):
                c.ai_booster_until = now + random.uniform(1.0, 2.0)
                c.ai_booster_next_t = c.ai_booster_until + random.uniform(2.0, 4.0)
            self.update_booster(c, now < c.ai_booster_until, now, dt)
        target = self.ai_target_speed(c)
        if c.ai_difficulty in ("hard", "extreme"):
            self.request_boost(c, now)
        target = self.boost_target(c, target, now)
        if c.booster_active:
            target = max(target, self.booster_target(c))
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
        if c.loadout.weapon == "mine":
            if now > c.mine_ready_at and random.random() < dt * 0.06 * reaction:
                self.drop_mine(c, now)
        elif c.loadout.weapon == "sniper":
            if (now > c.fire_ready_at and random.random() < dt * 0.6 * reaction
                    and self.target_ahead(c) is not None):
                self.fire_sniper(c, now)
        else:
            if self.targets_in_cone(c):
                if now >= c.ai_blaster_next_t:
                    c.ai_blaster_until = now + random.uniform(0.4, 1.2) * min(1.5, reaction)
                    c.ai_blaster_next_t = c.ai_blaster_until + random.uniform(0.8, 2.0) / max(0.5, reaction)
                self.update_blaster(c, now < c.ai_blaster_until, now, dt)
            else:
                self.update_blaster(c, False, now, dt)

    # ---- weapons ----
    def use_weapon(self, shooter: CarState, now: float) -> None:
        """Trigger the single equipped weapon (X)."""
        if shooter.loadout.weapon == "mine":
            self.drop_mine(shooter, now)
        elif shooter.loadout.weapon == "blaster":
            self.update_blaster(shooter, True, now, 0.0)
        else:
            self.fire_sniper(shooter, now)

    def _shot_line(self, shooter: CarState, corridor: float,
                   max_range: Optional[float] = None
                   ) -> Optional[tuple[tuple[float, float], float, float, float, tuple[float, float]]]:
        """Origin, direction, usable length and end point of a straight shot."""
        if self.track is None or not shooter.localized():
            return None
        p0 = shooter.world()
        if p0 is None:
            return None
        a = shooter.loc.heading()  # type: ignore[union-attr]
        length, x1, y1 = self.track.beam_end(p0[0], p0[1], a, corridor)
        if max_range is not None and length > max_range:
            length = max_range
            x1 = p0[0] + math.cos(a) * length
            y1 = p0[1] + math.sin(a) * length
        return p0, math.cos(a), math.sin(a), length, (x1, y1)

    def _target_on_line(self, shooter: CarState, p0: tuple[float, float], fx: float, fy: float,
                        length: float, half_width: float) -> Optional[tuple[CarState, float]]:
        best = None
        for o in self.cars:
            if o is shooter or not o.localized() or o.hp <= 0:
                continue
            p1 = o.world()
            if p1 is None:
                continue
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            ahead = dx * fx + dy * fy
            if ahead <= 0 or ahead > length:
                continue
            lateral = -dx * fy + dy * fx
            if abs(lateral) > half_width:
                continue
            if best is None or ahead < best[1]:
                best = (o, ahead)
        return best

    def target_ahead(self, shooter: CarState) -> Optional[tuple[CarState, float]]:
        """Nearest car on the sniper's straight line, in its own lane."""
        shot = self._shot_line(shooter, SNIPER_TRACK_CORRIDOR_MM)
        if shot is None:
            return None
        p0, fx, fy, length, _end = shot
        return self._target_on_line(shooter, p0, fx, fy, length, SNIPER_HIT_HALF_WIDTH_MM)

    def targets_in_cone(self, shooter: CarState) -> list[tuple[CarState, float]]:
        """Every car inside the blaster's short, wide cone (nearest first)."""
        shot = self._shot_line(shooter, BLASTER_TRACK_CORRIDOR_MM, BLASTER_RANGE_MM)
        if shot is None:
            return []
        p0, fx, fy, length, _end = shot
        found = []
        for o in self.cars:
            if o is shooter or not o.localized() or o.hp <= 0:
                continue
            p1 = o.world()
            if p1 is None:
                continue
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            ahead = dx * fx + dy * fy
            if ahead <= 0 or ahead > length:
                continue
            lateral = -dx * fy + dy * fx
            if abs(lateral) <= BLASTER_HIT_HALF_WIDTH_MM:
                found.append((o, ahead))
        found.sort(key=lambda item: item[1])
        return found

    def fire(self, shooter: CarState, now: float) -> None:
        """Trigger the equipped weapon once (used by AI and tests)."""
        if shooter.loadout.weapon == "mine":
            self.drop_mine(shooter, now)
        elif shooter.loadout.weapon == "blaster":
            self.fire_blaster_pulse(shooter, now)
        else:
            self.fire_sniper(shooter, now)

    def fire_sniper(self, shooter: CarState, now: float) -> None:
        if not self.weapons_enabled or now < shooter.fire_ready_at or shooter.is_dead(now) or not shooter.localized():
            return
        shooter.fire_ready_at = now + SNIPER_COOLDOWN_S
        shooter.lights_fire(now)
        shot = self._shot_line(shooter, SNIPER_TRACK_CORRIDOR_MM)
        if shot is None:
            return
        p0, fx, fy, length, end = shot
        target = self._target_on_line(shooter, p0, fx, fy, length, SNIPER_HIT_HALF_WIDTH_MM)
        if target is not None:
            victim, _d = target
            p1 = victim.world() or p0
            self.lasers.append(Laser(p0[0], p0[1], p1[0], p1[1], shooter.color, now + 0.25, True))
            self.damage(victim, shooter, SNIPER_DAMAGE, now)
            if shooter.source:
                shooter.source.rumble(0.2, 0.7, 120)
        else:
            self.lasers.append(Laser(p0[0], p0[1], end[0], end[1], shooter.color, now + 0.15, False))

    def _blaster_ready(self, c: CarState, now: float) -> bool:
        if now < c.blaster_overheated_until:
            return False
        if c.weapon_energy <= 0:
            return False
        if c.weapon_energy_empty and c.weapon_energy < BLASTER_MIN_ENERGY:
            return False
        return True

    def fire_blaster_pulse(self, shooter: CarState, now: float) -> None:
        if (not self.weapons_enabled or shooter.is_dead(now) or not shooter.localized()
                or not self._blaster_ready(shooter, now) or now < shooter.blaster_next_pulse_t):
            return
        shooter.blaster_next_pulse_t = now + BLASTER_PULSE_INTERVAL_S
        shooter.weapon_last_use_t = now
        shooter.lights_fire(now)
        shot = self._shot_line(shooter, BLASTER_TRACK_CORRIDOR_MM, BLASTER_RANGE_MM)
        if shot is None:
            return
        p0, _fx, _fy, _length, end = shot
        targets = self.targets_in_cone(shooter)
        for victim, _d in targets:
            p1 = victim.world() or p0
            self.lasers.append(Laser(p0[0], p0[1], p1[0], p1[1], shooter.color, now + 0.18, True))
            self.damage(victim, shooter, BLASTER_DAMAGE, now, spray=True)
        if not targets:
            self.lasers.append(Laser(p0[0], p0[1], end[0], end[1], shooter.color, now + 0.12, False))

    def _overheat_blaster(self, c: CarState, now: float) -> None:
        c.blaster_firing = False
        c.weapon_energy = 0.0
        c.weapon_energy_empty = True
        c.blaster_overheated_until = max(c.blaster_overheated_until, now + BLASTER_OVERHEAT_S)
        if c.source is not None:
            c.source.rumble(*BLASTER_OVERHEAT_RUMBLE)
        self.log(f"{c.name}: blaster overheated", WARN_COLOR)

    def update_blaster(self, shooter: CarState, held: bool, now: float, dt: float) -> None:
        """Hold-to-fire blaster with energy drain, a hard 4 s burst and overheat."""
        if shooter.loadout.weapon != "blaster":
            return
        can_fire = (held and self.weapons_enabled and not shooter.is_dead(now)
                    and not shooter.charging and shooter.localized()
                    and self._blaster_ready(shooter, now))
        if not can_fire:
            shooter.blaster_firing = False
            return
        if not shooter.blaster_firing:
            shooter.blaster_firing = True
            shooter.blaster_fire_started_t = now
            shooter.blaster_next_pulse_t = now
            if shooter.source is not None:
                shooter.source.rumble(*BLASTER_FIRE_RUMBLE)
            shooter.blaster_rumble_next_t = now + 0.35
        if now - shooter.blaster_fire_started_t >= BLASTER_MAX_FIRE_S:
            if shooter.blaster_overheated_until <= now:
                self._overheat_blaster(shooter, now)
            return
        shooter.weapon_energy = max(0.0, shooter.weapon_energy - BLASTER_ENERGY_DRAIN * dt)
        shooter.weapon_last_use_t = now
        if shooter.weapon_energy <= 0:
            self._overheat_blaster(shooter, now)
            return
        if shooter.source is not None and now >= shooter.blaster_rumble_next_t:
            shooter.source.rumble(*BLASTER_FIRE_RUMBLE)
            shooter.blaster_rumble_next_t = now + 0.35
        self.fire_blaster_pulse(shooter, now)

    def _tick_car_energy(self, c: CarState, now: float, dt: float) -> None:
        """Regenerate the blaster and powerup pools while they are not in use."""
        if c.loadout.weapon == "blaster" and not c.blaster_firing:
            if now - c.weapon_last_use_t >= BLASTER_REGEN_DELAY_S:
                c.weapon_energy = min(BLASTER_ENERGY_MAX,
                                      c.weapon_energy + BLASTER_ENERGY_REGEN * dt)
        if c.weapon_energy >= BLASTER_MIN_ENERGY:
            c.weapon_energy_empty = False
        if not c.powerup_busy():
            if now - c.powerup_last_use_t >= POWERUP_REGEN_DELAY_S:
                c.powerup_energy = min(POWERUP_ENERGY_MAX,
                                       c.powerup_energy + POWERUP_ENERGY_REGEN * dt)

    # ---- powerups ----
    def _uturn_allowed(self, c: CarState, now: float) -> bool:
        return (c.uturn_state == "idle" and now >= c.uturn_ready_at
                and c.powerup_energy >= UTURN_MIN_ENERGY and c.speed_sent() > 0
                and not c.is_dead(now) and not c.charging and not c.anchor_active
                and c.localized() and c.loc is not None and c.loc.direction > 0)

    def update_uturn(self, c: CarState, held: bool, now: float, dt: float) -> None:
        """Hold the powerup to reverse; releasing drains to zero and flips back."""
        if c.loadout.powerup != "uturn":
            return
        if c.uturn_state == "idle":
            if held and self._uturn_allowed(c, now):
                c.u_turn()
                c.uturn_state = "flipping_out"
                c.uturn_t = now
                c.uturn_retries = 0
                c.manual_reverse = True
                c.last_uturn_t = now
                self.log(f"{c.name}: reversing", c.color)
        elif c.uturn_state == "flipping_out":
            if c.loc is not None and c.loc.direction < 0:
                c.uturn_state = "reversing"
                c.uturn_reverse_started_t = now
                c.uturn_t = now
                c.uturn_retries = 0
            elif now - c.uturn_t > UTURN_FLIP_TIMEOUT_S:
                c.uturn_retries += 1
                if c.uturn_retries > 2:
                    c.uturn_state = "idle"
                    c.manual_reverse = False
                    c.uturn_ready_at = now + UTURN_COOLDOWN_S
                    self.log(f"{c.name}: U-turn did not engage", WARN_COLOR)
                else:
                    c.u_turn()
                    c.uturn_t = now
        elif c.uturn_state == "reversing":
            c.powerup_energy = max(0.0, c.powerup_energy - UTURN_DRAIN * dt)
            c.powerup_last_use_t = now
            if not held or c.powerup_energy <= 0 or c.is_dead(now):
                c.uturn_state = "flipping_back"
                c.u_turn()
                c.uturn_t = now
                c.manual_reverse = False
                self.log(f"{c.name}: turning forward", c.color)
        elif c.uturn_state == "flipping_back":
            if c.loc is not None and c.loc.direction > 0:
                c.uturn_state = "idle"
                c.uturn_ready_at = now + UTURN_COOLDOWN_S
                c.manual_reverse = False
                c.last_uturn_t = now
            elif now - c.uturn_t > UTURN_FLIP_TIMEOUT_S:
                c.uturn_retries += 1
                if c.uturn_retries > 2:
                    c.uturn_state = "idle"
                    c.manual_reverse = False
                    c.uturn_ready_at = now + UTURN_COOLDOWN_S
                    self.log(f"{c.name}: U-turn return failed", WARN_COLOR)
                else:
                    c.u_turn()
                    c.uturn_t = now

    def _hook_gap(self, owner: CarState, target: CarState) -> Optional[float]:
        if not self.track or owner.loc is None or target.loc is None:
            return None
        gap = self._traffic_distance(owner, target)
        return gap

    def _hook_valid(self, owner: CarState, target: CarState, now: float) -> bool:
        if target is owner or target.is_dead(now) or target.charging or not target.localized():
            return False
        gap = self._hook_gap(owner, target)
        return gap is not None and 0 < gap <= HOOK_BREAK_MM

    def nearest_ahead(self, owner: CarState, now: float, max_range: float = HOOK_RANGE_MM) -> Optional[CarState]:
        best: Optional[tuple[float, CarState]] = None
        for other in self.cars:
            if other is owner or other.is_dead(now) or other.charging or not other.localized():
                continue
            gap = self._hook_gap(owner, other)
            if gap is None or gap <= 0 or gap > max_range:
                continue
            if best is None or gap < best[0]:
                best = (gap, other)
        return best[1] if best else None

    def update_hook(self, owner: CarState, held: bool, now: float, dt: float) -> None:
        """Hold the hook to slow the nearest car ahead, in any lane."""
        if owner.loadout.powerup != "hook":
            return
        if (not held or owner.is_dead(now) or owner.charging or not owner.localized()):
            self.release_hook(owner)
            return
        target = owner.hook_target
        if target is None and owner.powerup_energy < HOOK_MIN_ENERGY:
            self.release_hook(owner)
            return
        if target is None or not self._hook_valid(owner, target, now):
            if target is not None:
                self.release_hook(owner, keep_light=True)
                if owner.powerup_energy < HOOK_MIN_ENERGY:
                    self.release_hook(owner)
                    return
            target = self.nearest_ahead(owner, now)
            if target is None:
                # The beam is active even without a catch, so the front stays yellow.
                owner.lights_hook_attacker(True)
                return
            owner.hook_target = target
            self.log(f"{owner.name}: hook on {target.name}", owner.color)
        owner.powerup_energy = max(0.0, owner.powerup_energy - HOOK_DRAIN * dt)
        owner.powerup_last_use_t = now
        target.hooked_until = now + 0.2
        target.hooked_by = owner
        target.lights_hooked(now)
        owner.lights_hook_attacker(True)
        p0, p1 = owner.world(), target.world()
        if p0 is not None and p1 is not None:
            self.tethers.append(Tether(p0[0], p0[1], p1[0], p1[1],
                                       (255, 220, 90), now + 0.12))
        if owner.powerup_energy <= 0:
            self.release_hook(owner)

    def release_hook(self, owner: CarState, keep_light: bool = False) -> None:
        target = owner.hook_target
        owner.hook_target = None
        if not keep_light:
            owner.lights_hook_attacker(False)
        if target is not None and target.hooked_by is owner:
            target.hooked_by = None
            target.lights_unhook()

    def start_anchor(self, c: CarState, now: float) -> None:
        if (c.loadout.powerup != "anchor" or c.anchor_active or c.powerup_energy < ANCHOR_COST
                or c.is_dead(now) or c.charging or not c.localized()):
            return
        c.powerup_energy = max(0.0, c.powerup_energy - ANCHOR_COST)
        c.powerup_last_use_t = now
        c.anchor_until = now + ANCHOR_HOLD_S
        c.anchor_active = True
        c.hard_stop(now)
        self.log(f"{c.name}: anchor", c.color)

    def update_anchor(self, c: CarState, now: float) -> None:
        if c.anchor_active and now >= c.anchor_until:
            c.anchor_active = False
            c.powerup_last_use_t = now

    def booster_target(self, c: CarState) -> float:
        return min(BOOSTER_MAX_MM_S, c.max_speed * BOOSTER_FACTOR)

    def cancel_powerups(self, c: CarState) -> None:
        """Drop active effects when a car is destroyed or loses the track."""
        self.release_hook(c)
        if c.booster_active:
            c.booster_active = False
            c.lights_booster(False)
        c.anchor_active = False
        c.uturn_state = "idle"
        c.uturn_retries = 0
        c.manual_reverse = False

    def update_booster(self, c: CarState, held: bool, now: float, dt: float) -> None:
        if c.loadout.powerup != "booster":
            return
        if (not held or c.is_dead(now) or c.charging or c.uturn_state != "idle"
                or c.anchor_active or not self._boost_clear(c, now)):
            if c.booster_active:
                c.booster_active = False
                c.booster_ready_at = now + BOOSTER_COOLDOWN_S
                c.lights_booster(False)
            return
        if not c.booster_active:
            if now < c.booster_ready_at or c.powerup_energy < BOOSTER_MIN_ENERGY:
                return
            c.booster_active = True
            c.lights_booster(True)
        c.powerup_energy = max(0.0, c.powerup_energy - BOOSTER_DRAIN * dt)
        c.powerup_last_use_t = now
        if c.powerup_energy <= 0:
            c.booster_active = False
            c.booster_ready_at = now + BOOSTER_COOLDOWN_S
            c.lights_booster(False)

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
                    self.damage(c, m.owner, MINE_DAMAGE, now, mine=True)
                    self.log(f"{c.name} ran into {m.owner.name}'s mine", (255, 170, 80))
                    break
        for m in exploded:
            if m in self.mines:
                self.mines.remove(m)

    def _check_battle_end(self, now: float) -> None:
        """The last car with lives remaining wins the battle."""
        if self.mode != "battle" or self.finished or self._battle_contenders < 2:
            return
        active = [c for c in self.cars
                  if c.participates() and not c.eliminated and c.lives > 0
                  and c.car.connected and not c.charging]
        if len(active) != 1:
            return
        winner = active[0]
        self.winner = winner
        self.finish_t = now
        self.finish_celebration_t = 0.0
        self._finish_lights_winner = None
        self.effect("boom", winner.world(), winner.color, 1.5)
        self.log(f"{winner.name} WINS the battle!", winner.color)

    def damage(self, victim: CarState, attacker: CarState, amount: int, now: float,
               mine: bool = False, spray: bool = False) -> None:
        if victim.is_dead(now) or victim.hp <= 0:
            return
        victim.hp = max(0, victim.hp - amount)
        w = victim.world()
        self.effect("ring", w, attacker.color, 0.45)
        self.effect("text", w, (255, 235, 200), 0.9, f"-{amount}")
        fatal = victim.hp <= 0
        if victim.source:
            hit = DEATH_RUMBLE if fatal else (SPRAY_HIT_RUMBLE if spray else HIT_RUMBLE)
            victim.source.rumble(*hit)
        if fatal:
            victim.deaths += 1
            attacker.kills += 1
            if self.mode == "battle":
                victim.lives = max(0, victim.lives - 1)
                if victim.lives <= 0:
                    victim.eliminated = True
                    victim.dead_until = float("inf")
                    self.log(f"{attacker.name} eliminated {victim.name}!", attacker.color)
                else:
                    victim.dead_until = now + DEATH_STOP_S
                    self.log(f"{attacker.name} destroyed {victim.name}! "
                             f"{victim.lives} lives left", attacker.color)
            else:
                victim.dead_until = now + DEATH_STOP_S
                self.log(f"{attacker.name} destroyed {victim.name}!", attacker.color)
            self.cancel_powerups(victim)
            victim.lights_dead()
            self.effect("boom", w, victim.color, 1.2)
            self._check_battle_end(now)
        else:
            victim.lights_hit(now, mine=mine)
            victim.update_health_light(self.weapons_enabled)
            if not mine:
                self.log(f"{attacker.name} hit {victim.name} (-{amount})", attacker.color)
