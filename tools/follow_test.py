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

from anki import protocol as P
from anki.track import Track
from anki.vehicle import Vehicle, discover
from game.game import (CarState, Game, CAR_MIN_GAP_MM, FOLLOW_LINK_MM,
                       FOLLOW_STALE_S, LANE_OVERLAP_MM)


class DiscardBle:
    """The harness drives the Vehicle directly; discard queued coroutines."""

    def fire(self, awaitable) -> None:
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()


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
        car = CarState(vehicle, index, DiscardBle(), 800)
        car.set_track(track)
        vehicle.on_timed_message = car.enqueue_message
        cars.append(car)
    try:
        for vehicle in vehicles:
            await vehicle.connect()
            await vehicle.request_status()
            await asyncio.sleep(0.7)
            print(f"connected {vehicle.model} {vehicle.address} status={vehicle.status}")
            if not vehicle.status or vehicle.status.on_charger:
                print("both cars must be off their chargers")
                return 2
        game = Game(Mock(), cars, track, False, None)
        game.running = True

        async def settle(seconds: float) -> None:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                now = time.monotonic()
                for car in cars:
                    car.drain_messages()
                    if car.loc:
                        car.loc.advance(1 / 50, now)
                await asyncio.sleep(1 / 50)

        async def place_lanes(targets, rounds: int = 3) -> None:
            """One command per round, then let the car finish before retrying."""
            for _ in range(rounds):
                pending = 0
                for vehicle, car, (target, direction) in zip(vehicles, cars, targets):
                    lane_track = (car.loc.code_lane_mm if car.loc.code_lane_mm is not None
                                  else car.loc.lane_mm)
                    if abs(lane_track - target) <= 12:
                        continue
                    pending += 1
                    target_car = target * direction
                    await vehicle.send(P.set_offset_from_road_center(lane_track * direction))
                    await vehicle.change_lane(target_car, 250, 1000)
                    if args.verbose:
                        print(f"steer {car.name} lane={lane_track} "
                              f"target={target} car={target_car}")
                if not pending:
                    return
                await settle(2.5)

        for vehicle in vehicles:
            await vehicle.set_speed(300, 450)
        await settle(1.5)
        dirs = [car.loc.direction or 1 for car in cars]
        if dirs[0] != dirs[1]:
            print(f"cars drive in different directions {dirs}; aborting")
            return 1
        targets = [(args.lane, dirs[0]), (args.follower_lane, dirs[1])]
        await place_lanes(targets)
        p = [car.loc.progress() for car in cars]
        if p[0] is None or p[1] is None:
            print("cars did not localize; aborting",
                  [(car.name, car.loc.on_track, car.loc.index, car.loc.speed)
                   for car in cars])
            return 1
        lanes = [car.loc.code_lane_mm if car.loc.code_lane_mm is not None
                 else car.loc.lane_mm for car in cars]
        print(f"actual lanes={lanes} direction={[car.loc.direction for car in cars]}")
        if None in lanes:
            print("cars have no confirmed lane; aborting")
            return 1
        separated = abs(lanes[0] - lanes[1]) > LANE_OVERLAP_MM
        if args.expect == "same" and separated:
            print("cars did not reach the same lane; aborting (rerun when they "
                  "are both settled in the requested lane)")
            return 1
        if args.expect == "different" and not separated:
            print("cars did not separate into different lanes; aborting")
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
                gap_before = game._following_gap(follower, leader)
                linked_before = follower.following is leader
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
                        "gap_before": None if gap_before is None else round(gap_before, 1),
                        "gap": None if gap is None else round(gap, 1),
                        "linked_before": linked_before,
                        "following": follower.following is leader,
                        "leader_lane": round(leader.loc.lane_mm, 1),
                        "follower_lane": round(follower.loc.lane_mm, 1),
                        "leader_dir": leader.loc.direction,
                        "follower_dir": follower.loc.direction,
                        "leader_age": round(now - leader.loc.last_update, 2),
                        "follower_age": round(now - follower.loc.last_update, 2),
                        "leader_raw_speed": round(leader.loc.speed, 1),
                        "follower_raw_speed": round(follower.loc.speed, 1),
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
        below = [r for r in rows if r["following"]
                 and r["gap"] is not None and r["gap"] < CAR_MIN_GAP_MM - 5]
        stale = [r for r in rows if r["leader_age"] > FOLLOW_STALE_S]
        same_lane = [r for r in rows
                     if abs(r["leader_lane"] - r["follower_lane"]) <= LANE_OVERLAP_MM]
        missed = [r for r in same_lane if r["leader_age"] <= FOLLOW_STALE_S
                  and not r["following"] and r["gap"] is not None
                  and 0 < r["gap"] <= FOLLOW_LINK_MM]
        cross = [r for r in rows if r["following"]
                 and abs(r["leader_lane"] - r["follower_lane"]) > LANE_OVERLAP_MM]
        clamped = [r for r in rows if r["linked_before"]
                   and r["gap_before"] is not None and r["gap_before"] < CAR_MIN_GAP_MM]
        linked = sum(1 for r in rows if r["following"])
        raw_l = [r["leader_raw_speed"] for r in rows if r["leader_raw_speed"] > 0]
        raw_f = [r["follower_raw_speed"] for r in rows if r["follower_raw_speed"] > 0]
        print(f"rows={len(rows)} same_lane={len(same_lane)} linked={linked} "
              f"below_gap={len(below)} stale_leader={len(stale)} "
              f"missed_link={len(missed)} cross_lane_link={len(cross)} "
              f"clamps={len(clamped)}")
        if raw_l and raw_f:
            print(f"raw speed leader~{sum(raw_l)/len(raw_l):.0f} "
                  f"follower~{sum(raw_f)/len(raw_f):.0f} mm/s")
        if stale:
            print("WARNING: leader telemetry dropped; this run is not a valid "
                  "following test (charge/position the cars and rerun).")
        failed = bool(below or missed or stale or cross)
        if args.expect == "different" and linked:
            print("FAIL: cars in different lanes were linked")
            failed = True
        return 0 if not failed else 1
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
    ap.add_argument("--follower-lane", type=float, default=23.0)
    ap.add_argument("--expect", choices=("same", "different"), default="same")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--leader-speed", type=int, default=400)
    ap.add_argument("--follower-speed", type=int, default=700)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--output")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))
