"""Replay a timestamped telemetry log through the current localizer.

Used to compare tracking changes on identical real-car data.  The final
acceptance test is still a real run on the track; this tool makes the
before/after difference visible and reproducible.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anki import protocol as P
from anki.position_filter import PositionFilter
from anki.track import CarLocalizer, MAX_RENDER_BACKWARD_MM, Track

FRAME = 1 / 50


def load_events(path: Path):
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind") != "msg" or "raw" not in row:
            continue
        try:
            mid, decoded = P.parse(bytes.fromhex(row["raw"]))
        except Exception:
            continue
        events.append((float(row["t"]), mid, decoded))
    events.sort(key=lambda item: item[0])
    return events


def _distance(loc: CarLocalizer, index, frac):
    return loc.filter.distance(index, frac)


def analyse(path: Path, track_path: Path, lead: float):
    track = Track.load(track_path)
    if track is None:
        raise SystemExit(f"could not load {track_path}")
    loc = CarLocalizer(track)
    events = load_events(path)
    if not events:
        raise SystemExit("no messages in log")

    counters = {"pos": 0, "tran": 0, "speed": 0, "intersection": 0, "lane": 0,
                "deloc": 0, "cycle_overtime": 0, "status": 0}
    pos_gaps = []
    last_pos_t = None
    transition_rejects = 0
    transition_errors = []
    filter_resets = 0
    fix_back = []
    index_back = []
    render_back = []
    estimate_back = []
    last_estimate = None
    last_render = None
    max_age = 0.0
    max_fix_age = 0.0
    frames = 0

    observe = loc.filter.observe

    def observed(*args, **kwargs):
        nonlocal filter_resets
        reset = observe(*args, **kwargs)
        if reset:
            filter_resets += 1
        return reset

    loc.filter.observe = observed
    end = events[-1][0] + 0.5
    ei = 0
    t = 0.0
    while t <= end:
        while ei < len(events) and events[ei][0] <= t:
            received, mid, decoded = events[ei]
            ei += 1
            before_pos = loc.filter.position
            before_index = loc.index
            before_frac = loc.frac
            if mid == P.MSG_LOCALIZATION_POSITION_UPDATE:
                counters["pos"] += 1
                if last_pos_t is not None:
                    pos_gaps.append((received - last_pos_t) * 1000)
                last_pos_t = received
                loc.on_message(mid, decoded, received_at=received, measurement_delay=lead)
            elif mid == P.MSG_LOCALIZATION_TRANSITION_UPDATE:
                counters["tran"] += 1
                if (loc.filter.position is not None and loc.on_track
                        and received - lead - loc.filter.last_fix <= loc.filter.HORIZON):
                    predicted = loc.filter.predict(received - lead)
                    boundaries = [(loc.filter.offsets[i], i, 0.0)
                                  for i in range(len(track.pieces))]
                    boundaries += [(loc.filter.distance(i, .5), i, .5)
                                   for i, p in enumerate(track.pieces) if p.kind == "Start"]
                    def error(boundary):
                        return abs((boundary[0] - predicted + loc.filter.length / 2)
                                   % loc.filter.length - loc.filter.length / 2)
                    boundary = min(boundaries, key=error)
                    transition_errors.append(error(boundary))
                    if error(boundary) > max(150, loc.speed * .12):
                        transition_rejects += 1
                loc.on_message(mid, decoded, received_at=received, measurement_delay=lead)
            elif mid == P.MSG_SPEED_UPDATE and isinstance(decoded, P.SpeedUpdate):
                counters["speed"] += 1
                loc.speed = float(decoded.actual_mm_s)
                loc.filter.set_speed(loc.direction * loc.speed, received)
            elif mid == P.MSG_LOCALIZATION_INTERSECTION_UPDATE:
                counters["intersection"] += 1
            elif mid == P.MSG_LANE_CHANGE_UPDATE:
                counters["lane"] += 1
            elif mid == P.MSG_VEHICLE_DELOCALIZED:
                counters["deloc"] += 1
                loc.on_message(mid, decoded, received_at=received, measurement_delay=lead)
            elif mid == P.MSG_CYCLE_OVERTIME:
                counters["cycle_overtime"] += 1
            elif mid == P.MSG_STATUS_UPDATE:
                counters["status"] += 1
            after_pos = loc.filter.position
            if before_pos is not None and after_pos is not None:
                delta = after_pos - before_pos
                if loc.direction > 0 and delta < -10:
                    fix_back.append(-delta)
                elif loc.direction < 0 and delta > 10:
                    fix_back.append(delta)
            if (before_index is not None and loc.index is not None
                    and (before_index, before_frac) != (loc.index, loc.frac)):
                d0 = _distance(loc, before_index, before_frac)
                d1 = _distance(loc, loc.index, loc.frac)
                delta = (d1 - d0 + loc.filter.length / 2) % loc.filter.length - loc.filter.length / 2
                if loc.direction > 0 and delta < -10:
                    index_back.append(-delta)
                elif loc.direction < 0 and delta > 10:
                    index_back.append(delta)
        loc.advance(FRAME, t)
        frames += 1
        if loc.filter.position is not None:
            estimate = loc.filter.predict(t)
            if estimate is not None:
                if last_estimate is not None:
                    delta = estimate - last_estimate
                    if loc.direction > 0 and delta < -1:
                        estimate_back.append(-delta)
                    elif loc.direction < 0 and delta > 1:
                        estimate_back.append(delta)
                last_estimate = estimate
            # Mirror render_world(raw=True)'s current backward clamp.
            raw = loc.filter.predict(t)
            if raw is not None:
                if loc._display_distance is not None:
                    if loc.direction > 0:
                        raw = max(raw, loc._display_distance - MAX_RENDER_BACKWARD_MM)
                    else:
                        raw = min(raw, loc._display_distance + MAX_RENDER_BACKWARD_MM)
                if last_render is not None:
                    delta = raw - last_render
                    if loc.direction > 0 and delta < -1:
                        render_back.append(-delta)
                    elif loc.direction < 0 and delta > 1:
                        render_back.append(delta)
                last_render = raw
            if loc.last_update:
                max_age = max(max_age, t - loc.last_update)
        t += FRAME

    def stats(values):
        if not values:
            return "0"
        values = sorted(values)
        return (f"n={len(values)} mean={statistics.mean(values):.1f} "
                f"p95={values[int(.95 * len(values))]:.1f} max={max(values):.1f} sum={sum(values):.1f}")

    print(f"file: {path}  track: {track_path}  duration={end:.1f}s  frames={frames}")
    print(f"messages: {counters}")
    if pos_gaps:
        gaps = sorted(pos_gaps)
        print(f"position gaps: mean={statistics.mean(gaps):.0f}ms "
              f"p95={gaps[int(.95 * len(gaps))]:.0f}ms max={max(gaps):.0f}ms")
    if transition_errors:
        errs = sorted(transition_errors)
        print(f"transition prediction error: mean={statistics.mean(errs):.0f}mm "
              f"p95={errs[int(.95 * len(errs))]:.0f}mm max={max(errs):.0f}mm "
              f"rejects={transition_rejects}/{len(errs)}")
    print(f"filter resets: {filter_resets}")
    print(f"estimate backward steps: {stats(estimate_back)}")
    print(f"render backward steps:   {stats(render_back)}")
    print(f"fix backward jumps:      {stats(fix_back)}")
    print(f"index backward jumps:    {stats(index_back)}")
    print(f"max fix age: {max_age:.2f}s")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", type=Path)
    ap.add_argument("--track", type=Path, default=Path(__file__).resolve().parents[1] / "track.json")
    ap.add_argument("--lead", type=float, default=0.03)
    args = ap.parse_args()
    sys.exit(analyse(args.log, args.track, args.lead))
