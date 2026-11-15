# Skull front lights — tested commands

Tested on a **Skull** with firmware **11866**, battery roughly **3.8–4.1 V**,
stationary and off the charger. Only Skull was used for this test.
Everything below was executed successfully; no firmware was changed.

## 1. Hardware / channels

- The front lights are the two hood-mounted weapon lights; both sides are
  controlled together.
- **Channel 4 = front red**, **channel 5 = front green**.
  In `anki/protocol.py` these are named `LC_FRONTL` and `LC_FRONTR`, but they
  behave as **red** and **green**, not as left/right.
- Possible colors: **red, green, yellow, orange, yellow-green, off**.
  There is no blue or white at the front.
- Brightness range: **0–14**. Dim values are only brightness levels, not separate
  colors.

## 2. Critical rule: always send both front channels

`0x33` changes **only the channels present in the packet**. Every other channel
keeps its previous state. Therefore, when setting a single front color, the other
front channel must be sent explicitly as `0`:

```python
# Red only: green MUST be set to 0 in the same packet.
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_STEADY, 14, 14, 0),  # red
    (P.LC_FRONTR, P.EFFECT_STEADY,  0,  0, 0),  # green off
]))
```

If green is not sent, an old green value stays active and red becomes yellow.
This was the cause of the earlier color confusion; Partydrive avoids it with
`LightData.ClearFor(FRONT_GREEN)`.

## 3. Tested colors

All values are steady (`EFFECT_STEADY`). Both channels are always present in the
packet.

| Color | Channel 4 (red) | Channel 5 (green) | Packet |
|---|---:|---:|---|
| Red | 14 | 0 | `0c 33 02 04 00 0e 0e 00 05 00 00 00 00` |
| Green | 0 | 14 | `0c 33 02 04 00 00 00 00 05 00 0e 0e 00` |
| Yellow | 14 | 14 | `0c 33 02 04 00 0e 0e 00 05 00 0e 0e 00` |
| Orange | 14 | 7 | `0c 33 02 04 00 0e 0e 00 05 00 07 07 00` |
| Yellow-green | 7 | 14 | `0c 33 02 04 00 07 07 00 05 00 0e 0e 00` |
| Off | 0 | 0 | `0c 33 02 04 00 00 00 00 05 00 00 00 00` |

```python
# Green only
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_STEADY,  0,  0, 0),
    (P.LC_FRONTR, P.EFFECT_STEADY, 14, 14, 0),
]))

# Yellow
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_STEADY, 14, 14, 0),
    (P.LC_FRONTR, P.EFFECT_STEADY, 14, 14, 0),
]))

# Orange
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_STEADY, 14, 14, 0),
    (P.LC_FRONTR, P.EFFECT_STEADY,  7,  7, 0),
]))

# Yellow-green
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_STEADY,  7,  7, 0),
    (P.LC_FRONTR, P.EFFECT_STEADY, 14, 14, 0),
]))

# Off
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_STEADY, 0, 0, 0),
    (P.LC_FRONTR, P.EFFECT_STEADY, 0, 0, 0),
]))
```

A two-channel packet has size `0x0c` (12 bytes, excluding the size byte).

## 4. Effects

All effects were tested with both front channels set to the same values.

| Effect | Tested parameters | Observed |
|---|---|---|
| `FLASH` | `0,5,5` | slow blinking (about 1 s on / 1 s off) |
| `FLASH` | `0,5,20` | fast blinking (about 0.25 s on / 0.25 s off) |
| `FADE` | `0,14,5` | soft fade |
| `THROB` | `0,14,5` | smooth up/down (breathing) |
| `RANDOM` | `0,14,0` | irregular flashing |

For `FLASH`, `start` and `end` are **time values 0–11**, not brightness values.
`cycles` is cycles per 10 seconds. This matches the taillight behaviour.

```python
# slow blink
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_FLASH, 0, 5, 5),
    (P.LC_FRONTR, P.EFFECT_FLASH, 0, 5, 5),
]))

# fast blink
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_FLASH, 0, 5, 20),
    (P.LC_FRONTR, P.EFFECT_FLASH, 0, 5, 20),
]))

# fade
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_FADE, 0, 14, 5),
    (P.LC_FRONTR, P.EFFECT_FADE, 0, 14, 5),
]))

# throb
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_THROB, 0, 14, 5),
    (P.LC_FRONTR, P.EFFECT_THROB, 0, 14, 5),
]))

# random
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_RANDOM, 0, 14, 0),
    (P.LC_FRONTR, P.EFFECT_RANDOM, 0, 14, 0),
]))
```

## 5. Firmware presets (`0x1D`)

### Weapon-fire mode (original app)

`0x44` is the special shooting/weapon mode used by the original app. The two
front lights alternate **red and green**, so both colors are visible. This was
confirmed on Skull. It is a fixed firmware pattern; stop it with `0x04` or
`lights_off(0x0F)`.

```python
# Weapon fire: red/green alternating front flash
await car.send(P.lights_on(P.LIGHT_FRONTLIGHTS))    # 0x44

# Stop it
await car.send(P.lights_off(P.LIGHT_FRONTLIGHTS))   # 0x04
```

Do not confuse this with a synchronized `0x33` `FLASH` on both channels: that
switches both colors on/off together and therefore looks like a **yellow** blink,
not the red/green weapon-fire alternation.

| Command | Packet | Observed |
|---|---|---|
| Weapon-fire / front flash on | `02 1d 44` | **red/green alternating flash (original-app shooting mode)** |
| Front lights off | `02 1d 04` | front off |
| Headlights on | `02 1d 11` | **no visible effect on Skull** |
| Headlights off | `02 1d 01` | **no visible effect on Skull** |
| All presets off | `02 1d 0f` | all presets off |

```python
await car.send(P.lights_on(P.LIGHT_FRONTLIGHTS))    # 0x44, red/green weapon flash
await car.send(P.lights_off(P.LIGHT_FRONTLIGHTS))   # 0x04
await car.send(P.lights_off(0x0F))                  # all presets off
```

Use `0x44`/`0x04` for the front-light weapon mode. The `LIGHT_HEADLIGHTS` bits
(`0x11`/`0x01`) did nothing visible on this car/firmware.

## 6. Turning the front lights off

```python
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_STEADY, 0, 0, 0),
    (P.LC_FRONTR, P.EFFECT_STEADY, 0, 0, 0),
]))
```

To clear every light group:

```python
await car.send(P.lights_pattern([
    (P.LC_FRONTL, P.EFFECT_STEADY, 0, 0, 0),
    (P.LC_FRONTR, P.EFFECT_STEADY, 0, 0, 0),
]))
await car.send(P.lights_pattern([(P.LC_TAIL, P.EFFECT_STEADY, 0, 0, 0)]))
await car.send(P.engine_color(0, 0, 0))
await car.send(P.lights_off(0x0F))
```

## 7. Summary for the implementing agent

- Front light channels: **4 = red**, **5 = green**; both front lights together.
- Colors: **red, green, yellow (14/14), orange (14/7), yellow-green (7/14)**.
- **Always send both channels**; a missing channel keeps its old value and mixes
  into the new color.
- Brightness: 0–14. Dim values are brightness levels, not separate colors.
- Effects: `STEADY`, `FLASH` (time window 0–11), `FADE`, `THROB`, `RANDOM`.
- **Weapon-fire mode (original app):** preset `0x44`
  (`lights_on(P.LIGHT_FRONTLIGHTS)`) gives the red/green alternating front
  flash. Stop with `0x04` or `0x0F`.
- Presets: `0x44` weapon-fire/front flashing on, `0x04` off. `0x11`/`0x01` had
  no visible effect on Skull.
- To turn off: set both front channels to 0 and/or send `lights_off(0x0F)` for the
  presets.
