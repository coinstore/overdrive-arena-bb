# Ground Shock roof/engine LED — tested commands

Scope: this document contains **only what was actually executed successfully** on a
**Ground Shock** with firmware **11866**. Only the roof/engine RGB LED and the
tail-flasher side effect were exercised. Front lights, tail color patterns and
non-steady light effects were **not** part of this test.

## 1. Set the roof/engine color (`0x33`)

```python
from anki import protocol as P

await car.send(P.engine_color(14, 0, 0))     # red
await car.send(P.engine_color(0, 14, 0))     # green
await car.send(P.engine_color(0, 0, 14))     # blue
await car.send(P.engine_color(14, 14, 14))   # white
await car.send(P.engine_color(0, 0, 0))      # off
```

`P.engine_color(r, g, b)` uses `EFFECT_STEADY` and three channels. The tested
channel mapping inside `0x33` is:

| Channel | Constant | LED |
|---:|---|---|
| 0 | `LC_RED` | Engine red |
| 3 | `LC_GREEN` | Engine green |
| 2 | `LC_BLUE` | Engine blue |

The order is **R, G, B in the API**, but **R, B, G on the wire**:

```text
# P.engine_color(14, 0, 0) -> red
11 33 03
  00 00 0e 0e 00     # channel 0 = RED,   STEADY, start=14, end=14, cycles=0
  03 00 00 00 00     # channel 3 = GREEN, STEADY, start=0,  end=0,  cycles=0
  02 00 00 00 00     # channel 2 = BLUE,  STEADY, start=0,  end=0,  cycles=0
```

Packet size for the tested three-channel packet is `0x11` (17), i.e.
`2 + 5 * 3` (excluding the size byte).

## 2. Turn everything off

```python
await car.send(P.lights_off(0x0F))        # all four 0x1D presets off
await car.send(P.engine_color(0, 0, 0))   # roof/engine RGB off
```

`0x0F` sends `02 1d 0f`. This was used at the start of the successful color run;
the tail lights were off during the following color sequence.

## 3. Observed side effect: `0x88` is the tail flasher

In this test, `P.lights_on(P.LIGHT_ENGINE)` was sent as a presumed “engine on”.
It sends:

```text
02 1d 88
```

Observed result: the **rear red lights started flashing**. They stayed flashing
even after later `0x33` color commands, until the successful run started with
`lights_off(0x0F)`. It did **not** switch the roof RGB LED on; the roof was already
controllable with `0x33` alone. `LIGHT_ENGINE` is therefore a misleading name for
bit 3 on this firmware.

Only bit 3 of the `0x1D` presets was exercised in this test. The other preset bits
were not tested here.

## 4. Blinking observation (blue / magenta / violet)

Observed during the tests:

- In the runs where the `0x88` tail flasher was active, these colors visibly blinked:
  - blue `(0, 0, 14)`
  - magenta `(14, 0, 14)`
  - violet `(8, 0, 14)`
- Red, green, yellow, cyan, white, orange and dim red were steady in the same runs.
- The successful run started with `lights_off(0x0F)` (tail flasher off) after the
  car had been charged. In that run blue 14 was steady and the full color sequence
  worked correctly.
- Battery readings: **3.741 V** during a blinking run, **3.691 V** at the start of
  the steady run, and **3.646 V** after about one minute of the steady run.

Because both the charge state and the tail-flasher state changed between the
blinking and the steady run, the exact cause was **not isolated**. Two candidate
factors are battery voltage/current sag and the extra current draw of the active
tail flasher.

For later tests: start with `lights_off(0x0F)`, use a charged battery, and read
`car.battery_mv`. If blue/magenta/violet blink, check both the battery and whether
the tail flasher is active.

## 5. Battery/version readout

```python
await car.request_status()
await asyncio.sleep(0.5)
print(car.battery_mv, car.version, car.status)
```

Observed on Ground Shock: firmware `11866`, `battery_mv` in the 3.6–3.7 V range
during the test.

## 6. Tested colors

Red, green, yellow, white, orange and dim red were steady in the runs. Blue,
magenta and violet blinked while the tail flasher was active and were steady in the
successful run with the tail flasher off.

| Color | RGB | Observed |
|---|---|---:|
| Red | 14, 0, 0 | steady red |
| Green | 0, 14, 0 | steady green |
| Blue | 0, 0, 14 | steady with tail flasher off |
| Yellow | 14, 14, 0 | steady yellow |
| Cyan | 0, 14, 14 | steady cyan |
| Magenta | 14, 0, 14 | steady with tail flasher off |
| White | 14, 14, 14 | steady white |
| Orange | 14, 7, 0 | steady orange |
| Violet | 8, 0, 14 | steady with tail flasher off |
| Dim red | 7, 0, 0 | steady, visibly darker |
| Off | 0, 0, 0 | off |

## 7. Copy-paste smoke test

This is the same color list and order that was run successfully. It starts with
all `0x1D` presets off, so the tail flasher is not active.

```python
import asyncio
from anki import protocol as P
from anki.vehicle import Vehicle, discover

COLORS = [
    ("red", (14, 0, 0)),
    ("green", (0, 14, 0)),
    ("blue", (0, 0, 14)),
    ("yellow", (14, 14, 0)),
    ("cyan", (0, 14, 14)),
    ("magenta", (14, 0, 14)),
    ("white", (14, 14, 14)),
    ("orange", (14, 7, 0)),
    ("violet", (8, 0, 14)),
    ("dim red", (7, 0, 0)),
    ("off", (0, 0, 0)),
]

async def main():
    cars = await discover(5)
    dev, model = next((d, m) for d, m in cars if m == "Ground Shock")
    car = Vehicle(dev, model)
    await car.connect()
    try:
        await car.send(P.lights_off(0x0F))
        await car.send(P.engine_color(0, 0, 0))
        await car.request_status()
        await asyncio.sleep(0.5)
        print("battery_mv:", car.battery_mv)
        for name, rgb in COLORS:
            await car.send(P.engine_color(*rgb))
            print(name, rgb, flush=True)
            await asyncio.sleep(1.5)
    finally:
        await car.disconnect()

asyncio.run(main())
```

## 8. Project files used

- `anki/protocol.py` — `engine_color()`, `lights_on()`, `lights_off()`.
- `anki/vehicle.py` — `discover()`, `Vehicle.connect()`, `Vehicle.send()`,
  `Vehicle.request_status()`.
- `tools/scan.py` — used to verify the car was advertising before connecting.
