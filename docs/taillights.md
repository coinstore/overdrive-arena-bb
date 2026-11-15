# Skull taillights — tested commands

Tested on a **Skull** with firmware **11866**, battery **4131 mV** (fully charged),
stationary and off the charger. Only Skull was used for this test.
Everything below was executed successfully; no firmware was changed.

## 1. Hardware / limits

- The taillights are **two red LEDs**, controlled together. There is no left/right
  split and no RGB control.
- Possible colors: **red only**, from off to bright red.
- Brightness range: **0–14** (0 = off, 14 = maximum).
- The engine and front lights were switched off during the tests so only the
  taillights were visible.

## 2. Control paths

There are two ways to address the taillights:

1. **`0x33` light pattern, channel 1 (`LC_TAIL`)** — brightness, blinking,
   fading, throbbing and random. This is the flexible path.
2. **`0x1D` presets** — two fixed firmware behaviours:
   - `0x22` = taillights solid red on, `0x02` = off
   - `0x88` = taillights flashing, `0x08` = off

### Packet layout for the tail channel

A single-channel tail packet is:

```text
07 33 01 01 <effect> <start> <end> <cycles>
^  ^  ^  ^
|  |  |  +-- channel 1 = LC_TAIL
|  |  +----- channel count = 1
|  +-------- message id 0x33
+----------- size 0x07 (2 + 5 * 1, excluding the size byte)
```

In Python:

```python
from anki import protocol as P

await car.send(P.lights_pattern([
    (P.LC_TAIL, P.EFFECT_STEADY, 14, 14, 0)
]))
```

## 3. Steady red / brightness (`EFFECT_STEADY`, value 0)

`start` and `end` are the brightness value (both set to the same value).

| Brightness | Packet | Observed |
|---:|---|---|
| 14 | `07 33 01 01 00 0e 0e 00` | bright steady red |
| 7 | `07 33 01 01 00 07 07 00` | about half brightness |
| 2 | `07 33 01 01 00 02 02 00` | very dim red glow |
| 0 | `07 33 01 01 00 00 00 00` | off |

```python
# full brightness
await car.send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_STEADY, 14, 14, 0)]))

# half brightness
await car.send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_STEADY, 7, 7, 0)]))

# off
await car.send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_STEADY, 0, 0, 0)]))
```

## 4. Blinking (`EFFECT_FLASH`, value 3)

**Important:** for `FLASH`, `start` and `end` are **time values in the range 0–11**,
not brightness values. The LED is on during the `start`..`end` window of each cycle.
`cycles` is cycles per 10 seconds.

The tested values below all worked as described:

| `start,end,cycles` | Packet | Observed |
|---|---|---|
| `0,0,5` | `07 33 01 01 03 00 00 05` | very short flash, long dark pause |
| `0,2,5` | `07 33 01 01 03 00 02 05` | short flash, longer pause |
| `0,5,5` | `07 33 01 01 03 00 05 05` | slow blink, about 1 s on / 1 s off |
| `0,8,5` | `07 33 01 01 03 00 08 05` | long on, short off |
| `0,11,5` | `07 33 01 01 03 00 0b 05` | steady on (full time window) |
| `0,5,2` | `07 33 01 01 03 00 05 02` | very slow blink, about 2.5 s on / 2.5 s off |
| `0,5,10` | `07 33 01 01 03 00 05 0a` | medium blink, about 0.5 s on / 0.5 s off |
| `0,5,20` | `07 33 01 01 03 00 05 14` | fast blink, about 0.25 s on / 0.25 s off |
| `0,5,50` | `07 33 01 01 03 00 05 32` | very fast blink / flicker |

```python
# slow tail blink
await car.send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_FLASH, 0, 5, 5)]))

# fast tail blink
await car.send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_FLASH, 0, 5, 20)]))
```

Do **not** use `(0, 14, ...)` for `FLASH`: `14` is outside the time range and the
LED then stays on instead of blinking. This is exactly what happened with the
old `(LC_TAIL, EFFECT_FLASH, 0, 14, 10)` pattern in `game/game.py`.

## 5. Fade (`EFFECT_FADE`, value 1)

For `FADE`, `start` and `end` are **brightness values (0–14)**.

Tested with `start=0, end=14, cycles=5`:

- Packet: `07 33 01 01 01 00 0e 05`
- Observed: the taillight **fades softly up to full brightness and then goes out**
  (sawtooth). It does **not** fade back down smoothly.

```python
await car.send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_FADE, 0, 14, 5)]))
```

## 6. Throb (`EFFECT_THROB`, value 2)

For `THROB`, `start` and `end` are **brightness values (0–14)**.

Tested with `start=0, end=14, cycles=5`:

- Packet: `07 33 01 01 02 00 0e 05`
- Observed: a smooth **breathing** effect — up to full brightness and back down,
  repeatedly.

```python
await car.send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_THROB, 0, 14, 5)]))
```

## 7. Random (`EFFECT_RANDOM`, value 4)

Tested with `start=0, end=14, cycles=0`:

- Packet: `07 33 01 01 04 00 0e 00`
- Observed: **irregular, erratic flashing** with no fixed rhythm.

```python
await car.send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_RANDOM, 0, 14, 0)]))
```

## 8. Firmware presets (`0x1D`)

| Command | Packet | Observed |
|---|---|---|
| Solid on | `02 1d 22` | taillights steady red |
| Solid off | `02 1d 02` | taillights off |
| Flash on | `02 1d 88` | taillights flash (fixed firmware rate) |
| Flash off | `02 1d 08` | taillights off |
| All presets off | `02 1d 0f` | all four presets off |

In Python:

```python
await car.send(P.lights_on(P.LIGHT_BRAKELIGHTS))    # 0x22, solid red
await car.send(P.lights_off(P.LIGHT_BRAKELIGHTS))   # 0x02
await car.send(P.lights_on(P.LIGHT_ENGINE))         # 0x88, tail flash
await car.send(P.lights_off(P.LIGHT_ENGINE))        # 0x08
await car.send(P.lights_off(0x0F))                  # all presets off
```

`LIGHT_ENGINE` is a misleading name on this firmware: bit 3 (`0x08`/`0x88`) drives
the **taillight flasher**, not the engine light.

## 9. Turning the taillights off

Both of the following were tested:

```python
# single-channel pattern off
await car.send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_STEADY, 0, 0, 0)]))

# all 0x1D presets off (also stops the 0x88 flasher)
await car.send(P.lights_off(0x0F))
```

A `0x33` steady-0 command **overrides and stops** an active `0x88` preset flash:
in the test the light went out immediately after the `0x33` command, even though
`0x88` had been active.

## 10. Summary for the implementing agent

- Tail = `0x33`, channel 1 (`LC_TAIL`), red only, brightness 0–14.
- Steady brightness: `EFFECT_STEADY`, `start=end=0..14`.
- Blinking: `EFFECT_FLASH`, `start`/`end` are **time window 0–11**,
  `cycles` = cycles per 10 s.
- Soft fade up then off: `EFFECT_FADE`, `start`/`end` are **intensity 0–14**.
- Smooth up/down breathing: `EFFECT_THROB`, `start`/`end` are **intensity 0–14**.
- Erratic flashing: `EFFECT_RANDOM`.
- Fixed firmware flash: `0x1D` mask `0x88`; stop it with `0x08` or `0x0F`.
- Solid firmware red: `0x1D` mask `0x22`; off with `0x02`.
- A single tail packet has size `0x07` and fits in one BLE write.
