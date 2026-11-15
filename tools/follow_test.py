"""Drive two real cars in the same lane and verify the engine following constraint.

This is a hardware diagnostic: leader/follower roles are chosen from the
localizer progress after both cars have localized in the same lane.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anki.track import Track
from anki.vehicle import Vehicle, discover
from game.game import CarState, Game, CAR_MIN_GAP_MM


async def run(args) -> int:
    track = Track.load(Path(args.track))
    if track is None:
        print("track.json could not be loaded")
        return 2
    found = await discover(args.scan)
    if len(found) < 2:
        print(f"need two advertising cars, found {len(found)}")
        return 2
    found = found[:2]
    vehicles = [Vehicle(d, m) for d, m in found]
    cars = []
    for index, vehicle in enumerate(vehicles):
        car = CarState(vehicle, index, Mock(), 800)
        car.set_track(track)
        vehicle.on_timed_message = car.enqueue_message
        cars.append(car)
    try:
        for vehicle in vehicles:
            await vehicle.connect()
            await vehicle.request_status()
            await asyncio.sleep(0.7)
            print(f"connected {vehicle.model} {vehicle.address} status={vehicle.status}")
            if not vehicle.status or not vehicle.status.on_track or vehicle.status.on_charger:
                print("both cars must be on the track, off their chargers")
                return 2
        game = Game(Mock(), cars, track, False, None)
        game.running = True
        for vehicle in vehicles:
            await vehicle.set_speed(300, 450)
        await asyncio.sleep(1.5)
        # Put both cars in the same physical lane.
        for vehicle in vehicles:
            await vehicle.change_lane(args.lane, 250, 1000)
        await asyncio.sleep(2.5)
        now = time.monotonic()
        for car in cars:
            car.loc.advance(1 / 50, now)
        p = [car.loc.progress() for car in cars]
        if p[0] is None or p[1] is None:
            print("cars did not localize; aborting")
            return 1
        # The car with the larger progress is the leader; the other is the
        # follower. This is only for the controlled test run.
        if p[0] > p[1]:
            leader, follower = cars[0], cars[1]
        else:
            leader, follower = cars[1], cars[0]
        print(f"leader={leader.name} follower={follower.name} lane={args.lane}")
        await leader.car.set_speed(args.leader_speed, 450)
        await follower.car.set_speed(args.follower_speed, 450)
        t0 = time.monotonic()
        last_log = 0.0
        rows = []
        try:
            while time.monotonic() - t0 < args.duration:
                now = time.monotonic()
                for car in cars:
                    car.drain_messages()
                    if car.loc:
                        car.loc.advance(1 / 50, now)
                game._reconcile_vehicle_positions(now)
                elapsed = now - t0
                if elapsed - last_log >= 0.1:
                    last_log = elapsed
                    gap = game._following_gap(follower, leader)
                    row = {
                        "t": round(elapsed, 3),
                        "leader": leader.name,
                        "follower": follower.name,
                        "leader_s": None if leader.loc.engine_distance is None else round(leader.loc.engine_distance, 1),
                        "follower_s": None if follower.loc.engine_distance is None else round(follower.loc.engine_distance, 1),
                        "gap": None if gap is None else round(gap, 1),
                        "following": follower.following is leader,
                        "leader_lane": round(leader.loc.lane_mm, 1),
                        "follower_lane": round(follower.loc.lane_mm, 1),
                        "leader_speed": leader.loc._engine_speed,
                        "follower_speed": follower.loc._engine_speed,
                    }
                    rows.append(row)
                    print(json.dumps(row))
                await asyncio.sleep(1 / 50)
        finally:
            for vehicle in vehicles:
                await vehicle.set_speed(0, 2500)
            await asyncio.sleep(0.8)
        if args.output:
            Path(args.output).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        bad = [r for r in rows if r["gap"] is not None and r["gap"] < CAR_MIN_GAP_MM - 5]
        print(f"rows={len(rows)} below_gap={len(bad)} ")
        return 0 if not bad else 1
    finally:
        for vehicle in vehicles:
            try:
                await vehicle.disconnect()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--track", default=str(Path(__file__).resolve().parents[1] / "track.json"))
    ap.add_argument("--scan", type=float, default=5.0)
    ap.add_argument("--lane", type=float, default=23.0)
    ap.add_argument("--leader-speed", type=int, default=400)
    ap.add_argument("--follower-speed", type=int, default=700)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--output")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))
