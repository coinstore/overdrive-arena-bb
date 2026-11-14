"""Record timestamped raw telemetry from one or more Anki cars.

The log is the input for the offline localizer replay and for comparing
tracking changes on identical real-car telemetry.  It deliberately does not
drive unless the car reports on-track/off-charger, and it always stops in
``finally``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anki import protocol as P
from anki.vehicle import Vehicle, discover

log = logging.getLogger("telemetry")


def _jsonable(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex(" ")
    return value


async def _record_one(device, model: str, args, out):
    car = Vehicle(device, model)
    start = time.monotonic()
    counts: dict[int, int] = {}
    received: list[tuple[float, int]] = []
    status = None

    async def on_raw(_char, data: bytearray) -> None:
        nonlocal status
        now = time.monotonic() - start
        raw = bytes(data)
        try:
            mid, decoded = P.parse(raw)
        except Exception as exc:  # noqa: BLE001 - malformed firmware data must not kill the link
            mid, decoded = -1, {"parse_error": repr(exc)}
        counts[mid] = counts.get(mid, 0) + 1
        received.append((now, mid))
        if mid == P.MSG_STATUS_UPDATE:
            status = decoded
        out.write(json.dumps({"t": now, "kind": "msg", "car": car.address, "mid": mid,
                              "raw": raw.hex(" "), "data": _jsonable(decoded)}) + "\n")
        # Keep the normal Vehicle bookkeeping and callbacks in sync.
        car._notify(_char, data)

    def event(kind: str, **fields) -> None:
        out.write(json.dumps({"t": time.monotonic() - start, "kind": kind, "car": car.address,
                              **fields}) + "\n")

    try:
        event("connect")
        await car.connect()
        # Tap raw notifications while retaining Vehicle's parsed state.
        await car.client.stop_notify(P.READ_CHAR_UUID)
        await car.client.start_notify(P.READ_CHAR_UUID, on_raw)
        await car.request_status()
        await asyncio.sleep(1.0)
        status = status or car.status
        event("status", status=_jsonable(status), version=car.version, battery_mv=car.battery_mv)
        if not status or not status.on_track or status.on_charger:
            event("abort", reason="car is not on the track or is on the charger")
            log.error("%s is not ready (status=%s)", car.model, status)
            return 1
        if args.ramp_seconds:
            t0 = time.monotonic()
            while True:
                elapsed = time.monotonic() - t0
                if elapsed >= args.ramp_seconds:
                    break
                frac = elapsed / args.ramp_seconds
                speed = int(round(args.ramp_from + (args.ramp_to - args.ramp_from) * frac))
                event("speed", value=speed, accel=args.accel, ramp_hz=args.write_hz)
                await car.set_speed(speed, args.accel)
                await asyncio.sleep(1.0 / max(1.0, args.write_hz))
        elif args.steps:
            for speed in args.steps:
                event("speed", value=speed, accel=args.accel, step_s=args.step_seconds)
                await car.set_speed(speed, args.accel if speed else 2500)
                await asyncio.sleep(args.step_seconds)
        elif args.speed > 0:
            event("speed", value=args.speed, accel=args.accel)
            await car.set_speed(args.speed, args.accel)
            await asyncio.sleep(args.duration)
        if args.ramp_seconds or args.steps or args.speed > 0:
            event("stop")
            await car.stop()
            await asyncio.sleep(1.0)
        event("disconnect")
        return 0
    except Exception as exc:  # noqa: BLE001
        event("error", error=repr(exc))
        log.exception("%s failed", car.model)
        return 1
    finally:
        try:
            if car.connected:
                await car.stop()
                await asyncio.sleep(0.2)
                await car.disconnect()
        except Exception:  # noqa: BLE001
            pass
        pos_times = [t for t, mid in received if mid == P.MSG_LOCALIZATION_POSITION_UPDATE]
        gaps = [(b - a) * 1000 for a, b in zip(pos_times, pos_times[1:])]
        if gaps:
            gaps.sort()
            p95 = gaps[int(len(gaps) * 0.95)]
        else:
            p95 = 0
        print(f"{car.model:<12} {car.address}  messages={len(received)}  duration={time.monotonic()-start:.1f}s")
        print("  " + "  ".join(f"0x{mid:02X}={n}" for mid, n in sorted(counts.items())))
        if gaps:
            print(f"  position gaps: mean={statistics.mean(gaps):.0f}ms p95={p95:.0f}ms max={max(gaps):.0f}ms")
        print(f"  position={len(pos_times)} transitions={counts.get(P.MSG_LOCALIZATION_TRANSITION_UPDATE, 0)} "
              f"intersections={counts.get(P.MSG_LOCALIZATION_INTERSECTION_UPDATE, 0)} "
              f"lane={counts.get(P.MSG_LANE_CHANGE_UPDATE, 0)} "
              f"cycle_overtime={counts.get(P.MSG_CYCLE_OVERTIME, 0)}")


async def main(args) -> int:
    found = await discover(args.scan)
    if args.car:
        wanted = {x.lower() for x in args.car}
        found = [(d, m) for d, m in found if d.address.lower() in wanted]
    if not found:
        print("No Anki cars found. Power them on and put them on the track.")
        return 2
    print("Cars:", ", ".join(f"{m}@{d.address}" for d, m in found))
    # One car at a time by default: simultaneous cars would collide on a
    # single-lane test and make the position data ambiguous.
    if not args.all and len(found) > 1:
        found = found[:1]
    out_path = Path(args.output) if args.output else Path(f"telemetry_{int(time.time())}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rc = 0
    with out_path.open("w", encoding="utf-8") as out:
        for device, model in found:
            rc |= await _record_one(device, model, args, out)
            out.flush()
            await asyncio.sleep(1.0)
    print(f"log: {out_path}")
    return rc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--car", action="append", help="address (repeatable); default: first found")
    ap.add_argument("--all", action="store_true", help="drive every found car (only on a multi-car-safe test)")
    ap.add_argument("--speed", type=int, default=400, help="constant speed in mm/s; 0 = only idle")
    ap.add_argument("--accel", type=int, default=600, help="firmware acceleration in mm/s^2")
    ap.add_argument("--duration", type=float, default=20.0, help="seconds of driving")
    ap.add_argument("--steps", type=lambda v: [int(x) for x in v.split(",") if x.strip()],
                    help="speed steps, e.g. 300,800,1200,800,400,0")
    ap.add_argument("--step-seconds", type=float, default=3.0, help="seconds per --steps entry")
    ap.add_argument("--ramp-from", type=int, default=300, help="ramp start speed")
    ap.add_argument("--ramp-to", type=int, default=1200, help="ramp end speed")
    ap.add_argument("--ramp-seconds", type=float, default=0, help="linear speed ramp duration; 0 disables")
    ap.add_argument("--write-hz", type=float, default=20, help="speed writes per second during --ramp-seconds")
    ap.add_argument("--scan", type=float, default=5.0, help="BLE scan duration")
    ap.add_argument("--output", help="JSONL output path")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a)))
