# Car light language — implementation guide

Read this before changing the car LEDs. The hardware facts come from the tested
notes in [`lights.md`](lights.md), [`frontlights.md`](frontlights.md) and
[`taillights.md`](taillights.md); this file defines what the game *means* with
each light and where to change it.

## 1. The contract: one light group, one meaning

The car has three independent light groups. Do not blur their meanings.

| Group | Meaning | Colour |
|---|---|---|
| **Roof / engine** | Who the player is, plus battle health | Player colour |
| **Front / weapon** | Weapon activity | Red + green |
| **Tail / rear** | Rear-facing events (currently hits) | Red only |

### Roof

- Steady player colour = healthy.
- Blinking player colour = battle health. The blink gets faster as HP drops.
- Never use the roof for hit, shot, battery or other event feedback. The player
  colour must stay recognisable.
- The roof can show a countdown blink and the winner celebration. Those are
  explicit pre/post-race exceptions.

### Front

- Red/green alternating flash = the car is firing (`0x1D` preset `0x44`).
- **Yellow is reserved for future abilities**, for example a tractor beam or a
  charged weapon. Do not use yellow for ordinary driving or damage feedback.
- Orange / yellow-green are in the same warm family; leave them unused unless a
  new ability really needs another front state.
- The front has no blue or white. Both front channels must always be sent
  together: channel 4 is red, channel 5 is green. A missing channel keeps its old
  value and silently mixes into the new colour.

### Tail

- Red flash = the car was hit.
- Red steady is reserved for a sustained rear effect such as a brake light or a
  tractor beam, if one is added later. It is not implemented today.
- The tail is red only. Do not try to encode blue/green/white information there.

## 2. Hardware rules that shape the code

### 2.1 `0x33` light patterns

- One `0x33` packet carries **at most three channels**. The roof alone uses all
  three (R/G/B), so a roof update plus a tail/front update needs two packets.
- `0x33` only changes the channels present in the packet. Every other channel
  keeps its previous state. Always send both front channels when changing the
  front.
- Use `CarState._send_patterns()` for more than three channels; it splits the
  batch into valid packets. `P.lights_pattern()` raises `ValueError` on more than
  three channels instead of silently truncating.

Effect parameters:

| Effect | `start` / `end` mean | Range | Notes |
|---|---|---|---|
| `STEADY` | brightness | 0–14 | `start == end` is the usual form |
| `FADE` | brightness `start → end` | 0–14 | sawtooth fade up, then off |
| `THROB` | brightness `start → end → start` | 0–14 | smooth breathing |
| `FLASH` | **time window**, not brightness | 0–11 | LED is on between `start` and `end`; full channel brightness |
| `RANDOM` | ignored | — | erratic flashing |

`cycles` is cycles per 10 seconds (`10` = 1 Hz, `20` = 2 Hz, `30` = 3 Hz).
`cycles = 0` is effectively static for the non-flash effects.

Important: `FLASH` has no intensity parameter. It turns each active channel fully
on. This is why the engine palette uses pure RGB levels — see §2.3.

### 2.2 `0x1D` firmware presets

One-byte packets, independent of the `0x33` channel state.

| Command | Packet | Meaning |
|---|---|---|
| `P.lights_on(P.LIGHT_FRONTLIGHTS)` | `02 1d 44` | front red/green weapon flash on |
| `P.lights_off(P.LIGHT_FRONTLIGHTS)` | `02 1d 04` | front weapon flash off |
| `P.lights_on(P.LIGHT_ENGINE)` | `02 1d 88` | **tail flasher on** (misleading name) |
| `P.lights_off(P.LIGHT_ENGINE)` | `02 1d 08` | tail flasher off |
| `P.lights_on(P.LIGHT_BRAKELIGHTS)` | `02 1d 22` | tail steady red |
| `P.lights_off(P.LIGHT_BRAKELIGHTS)` | `02 1d 02` | tail steady off |
| `P.lights_off(0x0F)` | `02 1d 0f` | all presets off |

`LIGHT_HEADLIGHTS` (`0x11`/`0x01`) had no visible effect in the hardware tests.
Do not rely on it. `LIGHT_ENGINE` is really the tail flasher on firmware 11866.

`lights_off(0x0F)` clears presets only. It does **not** clear a `0x33` channel
pattern. Always explicitly set the affected `0x33` channels to steady 0 when
turning an effect off.

### 2.3 Pure engine colours

`FLASH` drives every channel in the packet at full brightness and has no
intensity parameter. A mixed colour would therefore change hue when it blinks:

- orange `(14, 7, 0)` would flash as yellow `(14, 14, 0)`;
- blue `(0, 4, 14)` would flash as cyan `(0, 14, 14)`.

More importantly, do **not** send all three channels with `FLASH` unless you
want white. `P.engine_flash()` handles this correctly: channels with a colour
component get `FLASH`, channels with value 0 get `STEADY 0`. Use the helper
rather than hand-writing three `FLASH` configs.

To keep the blinking colour identical to the steady player colour, the engine
palette in `game/colors.py` uses pure levels:

| Slot | Player colour | Engine steady / flash |
|---|---|---|
| P1 | red | `(14, 0, 0)` |
| P2 | blue | `(0, 0, 14)` |
| P3 | green | `(0, 14, 0)` |
| P4 | yellow | `(14, 14, 0)` |
| P5 | magenta | `(14, 0, 14)` |
| P6 / KB | white | `(14, 14, 14)` |

The sixth player is **white** instead of orange because orange cannot flash
distinctly with full-channel `FLASH`. If you need orange, either change the
player palette consistently or use a host-driven colour/off blink instead of the
firmware flash.

### 2.4 Known hardware quirks

- Blue, magenta and violet were observed blinking when the tail flasher was
  active and/or the battery was weak. The exact cause was not isolated. The idle
  code no longer leaves the tail flasher on, which removes most of the current
  draw. Verify a blue car with a hit flash on a charged battery.
- The tail is red only and both rear LEDs are controlled together.
- The front has no blue or white.
- A `0x33` steady-0 tail command can override and stop an active `0x88` preset.
  This is useful, but do not rely on it for other channels without testing.

## 3. Current behaviour

Implemented in `game/game.py` (`CarState` light methods and `Game` call sites).

### 3.1 Roof — battle health

| HP | State | Pattern |
|---:|---|---|
| 61–100 | healthy | `STEADY` player colour |
| 21–60 | caution | `engine_flash(..., cycles=10)` — 1 Hz |
| 1–20 | critical | `engine_flash(..., cycles=30)` — 3 Hz |
| 0 | destroyed | off |

The thresholds are tied to real hits: 60 HP is reached after two blaster hits;
20 HP is one blaster hit away from death.

In race mode the roof is always steady. After a respawn it is steady again.
During the 3-2-1 countdown the roof blinks in the player colour at 1 Hz.

### 3.2 Front — firing

- On fire: `P.lights_on(P.LIGHT_FRONTLIGHTS)` (`0x44`).
- After `LIGHT_FIRE_S` (0.35 s): `P.lights_off(P.LIGHT_FRONTLIGHTS)` (`0x04`).
- Idle: off. Pairing hover uses the same front preset to identify a car.

### 3.3 Tail — hits

- Blaster hit: tail `FLASH 0,5,20` (2 Hz) for `LIGHT_HIT_S` (0.5 s).
- Mine hit: tail `THROB 0,14,16` (1.6 Hz) for `LIGHT_MINE_HIT_S` (1.2 s).
- Mine priority 3 overrides blaster priority 2. A blaster hit never downgrades a
  running mine hit.
- Idle: off.

### 3.4 Destroyed, countdown, finish

- **Destroyed:** every light off for `DEATH_STOP_S` (3 s). No tail or roof flash.
  The map still shows the boom effect. On respawn: steady player colour.
- **Countdown:** roof blinks in the player colour for `COUNTDOWN_S`; at GO
  `lights_idle()` restores steady.
- **Finish:** losers go dark; the winner gets the existing 12 slow roof blinks,
  then steady. `lights_finish()` clears all groups and cancels pending events.
- **Pairing:** claimed cars take their driver's colour. The front preset may
  blink to identify a car. `lights_idle()` clears it when the race starts.

### 3.5 Timings and thresholds

| Constant | Value | File |
|---|---:|---|
| `LIGHT_FIRE_S` | 0.35 s | `game/game.py` |
| `LIGHT_HIT_S` | 0.5 s | `game/game.py` |
| `LIGHT_MINE_HIT_S` | 1.2 s | `game/game.py` |
| `HP_CAUTION` | 60 | `game/game.py` |
| `HP_CRITICAL` | 20 | `game/game.py` |

Change these here first; they are the intended tuning points. Do not scatter new
magic durations through `Game`.

## 4. Player colours

- One colour per **driver**, not per car. `InputSource.color_index` is assigned
  when the controller/keyboard source is created.
- `App.assign_player_colors()` applies the driver's colour to the claimed car and
  gives unclaimed AI/parked cars a free colour. It is called on claim, release,
  disconnect, race start and return to pairing.
- The pairing badge uses the driver's colour. The claimed car's `CarState.color`
  is set from the same colour index, so badge, car card, HUD, map sprite and roof
  light all match. Do not add a separate palette for only one of those views.
- `PLAYER_COLORS` and `ENGINE_COLORS` live in `game/colors.py`. Keep them
  hue-compatible. If you change one, change the other and test a blinking car.

## 5. Priorities and restoration

The light groups are independent; a hit must not block a health update.

- **Roof:** baseline from HP/mode. It has no event overlays except the countdown
  and the finish celebration, which explicitly replace the baseline.
- **Tail:** `tail_until` + `tail_priority`. Higher priority overrides; on expiry,
  `tick_lights()` sends tail steady 0. Priorities: blaster 2, mine 3.
- **Front:** `front_until`. On expiry, `tick_lights()` sends
  `lights_off(P.LIGHT_FRONTLIGHTS)` because firing uses the `0x44` preset.
- **Destroyed/finish:** `lights_dead()` / `lights_finish()` cancel all pending
  timers and clear every group.
- `Game.tick()` calls `CarState.tick_lights(now)` on every frame, including
  phases where the race is not running.

## 6. Where to change what

| File | Responsibility |
|---|---|
| `game/colors.py` | player + engine palettes |
| `anki/protocol.py` | `lights_pattern`, `engine_color`, `engine_flash`, presets |
| `game/game.py` | `CarState` light methods, timings, `Game` call sites |
| `app.py` | driver-to-car colour assignment, pairing lights |
| `game/inputs.py` | `color_index` on controller/keyboard sources |
| `game/render.py` | pairing badges use the source colour |
| `tests/test_race_features.py` | packet/state regression tests |
| `tools/lights_test.py` | raw hardware smoke test |

## 7. Recipe: add a yellow tractor-beam front light

Yellow on the front is the reserved ability colour. For a tractor beam, use a
steady or throbbing yellow, **not** `FLASH`. `FLASH` would need a 0–11 time
window and would make the light blink rather than glow.

1. Add a constant, e.g. `TRACTOR_BEAM_S = 1.0`.
2. Add a `CarState` method that starts the effect and sets the front timer:

```python
def lights_tractor_beam(self, now: float, on: bool) -> None:
    if self._lights_dark:
        return
    if on:
        self._send(P.lights_pattern([
            (P.LC_FRONTL, P.EFFECT_STEADY, 14, 14, 0),  # red + green = yellow
            (P.LC_FRONTR, P.EFFECT_STEADY, 14, 14, 0),
        ]))
        self.front_until = now + TRACTOR_BEAM_S
        self.front_is_pattern = True
    else:
        self._clear_front()
```

3. `tick_lights()` currently clears the front with the `0x04` preset. That is
   correct for the `0x44` firing flash, but it does **not** clear a `0x33`
   yellow pattern. Add an explicit pattern clear for abilities:

```python
def _clear_front(self) -> None:
    self.front_until = 0.0
    self._send(P.lights_off(P.LIGHT_FRONTLIGHTS))  # stop the 0x44 firing preset
    self._send(P.lights_pattern([
        (P.LC_FRONTL, P.EFFECT_STEADY, 0, 0, 0),
        (P.LC_FRONTR, P.EFFECT_STEADY, 0, 0, 0),
    ]))
```

4. If an ability and firing can overlap, add a `front_priority` field and extend
   `tick_lights()`/`lights_fire()` so a lower-priority effect does not overwrite a
   higher one. The tail priority implementation is the model.
5. Call the method from the game logic that starts/stops the ability.
6. Add unit tests for the exact packets, the expiry clear and priority.
7. Update §3.2 and §3.3 of this document.

For a **charging** visual instead of a steady beam, use `THROB` with intensity
values, for example both front channels `THROB 0 → 14` at 10–20 cycles/10 s.
Never pass 14 as `start`/`end` to `FLASH`.

## 8. Do / Don't

- **Do** keep the roof for identity and health only.
- **Do** reserve front yellow for abilities.
- **Do** send both front channels every time.
- **Do** split more than three `0x33` channels with `_send_patterns()`.
- **Do** clear both the `0x1D` preset and the `0x33` channel state when stopping
  an effect.
- **Do** keep long blinks at or below about 3 Hz.
- **Don't** use the roof for hit/shot/battery feedback.
- **Don't** use the tail for health; it is red only.
- **Don't** use `LIGHT_ENGINE` as if it were a roof light.
- **Don't** rely on `LIGHT_HEADLIGHTS`; it had no visible effect.
- **Don't** use `FLASH` for dimming; it has no intensity parameter.
- **Don't** send all three roof channels as `FLASH`; that blinks white. Use
  `P.engine_flash()` so zero channels stay `STEADY 0`.
- **Don't** put battery warnings on the car. They belong in the HUD.
- **Don't** add unrelated signals (boost, lane change, cooldown) to the car
  lights; that dilutes the language.

## 9. Tests and hardware checks

Offline:

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
.venv/bin/python tests/render_demo.py
```

The light regression tests cover the idle tail flasher, front fire expiry,
health bands, tail priority, all-off destroyed/finish states, the 3-channel
limit and valid `FLASH` windows.

Hardware checklist (one car, charged, off the charger):

1. Idle: roof steady, front/tail off. No permanent tail flashing.
2. Fire: `0x44` shows red/green alternating within 0.35 s.
3. Health: 1 Hz and 3 Hz roof blinks are clearly different.
4. All six player colours flash in the same hue as their steady colour.
5. Blue player + tail hit on a charged battery: roof does not blink by accident.
6. Tail: blaster flash and mine throb are distinguishable.
7. Destroyed: all three groups go dark; respawn restores the player colour.
8. Finish: winner celebration works; losers stay dark; returning to pairing
   restores every car.

Use `python tools/lights_test.py <model>` for raw packet experiments. It sends
one command at a time and checks whether the car stays connected.
