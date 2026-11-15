"""Same-lane following constraint: engine-only, no weapon logic."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from anki.track import Track, Piece, START, STRAIGHT, TURN
from game.game import (CarState, Game, CAR_MIN_GAP_MM, FOLLOW_RELEASE_MM,
                       FOLLOW_RELEASE_HOLD_S, FOLLOW_STALE_S)


def make_car(track, index, lane=23.0, frac=0.5, offset=0.0, direction=1):
    vehicle = SimpleNamespace(model=f"Car{index}", battery_mv=0, connected=True,
                              on_charger=False)
    car = CarState(vehicle, index, Mock(), 800)
    car.set_track(track)
    car.loc.direction = direction
    car.loc.lane_mm = lane
    car.loc.on_track = True
    car.loc.last_update = time.monotonic()
    car.loc.set_engine_position(560.0 + frac * 560.0 + offset,
                                time.monotonic(), 400.0)
    return car


class FollowingTests(unittest.TestCase):
    def setUp(self):
        self.track = Track([Piece(34, START)] + [Piece(36, STRAIGHT) for _ in range(4)])
        self.leader = make_car(self.track, 0, lane=23, frac=.6, offset=0)
        self.rear = make_car(self.track, 1, lane=23, frac=.35, offset=0)
        self.game = Game(Mock(), [self.leader, self.rear], self.track, True, None)
        self.game.running = True
        now = time.monotonic()
        for car, frac, speed in ((self.leader, .6, 400.0), (self.rear, .35, 400.0)):
            car.loc.direction = 1
            car.loc.lane_mm = 23.0
            car.loc.on_track = True
            car.loc.last_update = now
            car.loc.set_engine_position(560.0 + frac * 560.0, now, speed)

    def test_faster_same_lane_rear_stays_behind(self):
        self.rear.loc.set_engine_position(self.rear.loc.engine_distance,
                                          time.monotonic(), 700.0)
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertIs(self.rear.following, self.leader)
        gap = self.game._following_gap(self.rear, self.leader)
        self.assertGreaterEqual(gap, CAR_MIN_GAP_MM - 1e-6)
        self.assertLessEqual(self.rear.loc._engine_speed, self.leader.loc._engine_speed)

    def test_adjacent_lane_is_not_constrained(self):
        self.rear.loc.lane_mm = -23.0
        before = self.rear.loc.engine_distance
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertIsNone(self.rear.following)
        self.assertAlmostEqual(self.rear.loc.engine_distance, before)

    def test_opposite_direction_is_not_constrained(self):
        self.rear.loc.direction = -1
        before = self.rear.loc.engine_distance
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertIsNone(self.rear.following)
        self.assertAlmostEqual(self.rear.loc.engine_distance, before)

    def test_link_releases_when_gap_is_large(self):
        now = time.monotonic()
        self.game._reconcile_vehicle_positions(now)
        self.assertIs(self.rear.following, self.leader)
        self.leader.loc.set_engine_position(
            self.rear.loc.engine_distance + FOLLOW_RELEASE_MM + 100,
            now, 400.0)
        self.leader.loc.last_update = now
        self.game._reconcile_vehicle_positions(now)
        # One sample is not enough: a localizer re-anchor may jump the gap.
        self.assertIs(self.rear.following, self.leader)
        later = now + FOLLOW_RELEASE_HOLD_S + 0.1
        self.leader.loc.last_update = later
        self.game._reconcile_vehicle_positions(later)
        self.assertIsNone(self.rear.following)

    def test_short_position_jump_does_not_release_link(self):
        now = time.monotonic()
        self.game._reconcile_vehicle_positions(now)
        self.assertIs(self.rear.following, self.leader)
        # Real run: the leader's estimate jumped ~1.1 m forward for one
        # sample, then the follower's estimate caught up on the next one.
        self.leader.loc.set_engine_position(
            self.rear.loc.engine_distance + 1300.0, now, 400.0)
        self.leader.loc.last_update = now
        self.game._reconcile_vehicle_positions(now)
        self.assertIs(self.rear.following, self.leader)
        self.rear.loc.set_engine_position(self.leader.loc.engine_distance - 180.0,
                                          now + 0.1, 400.0)
        self.rear.loc.last_update = now + 0.1
        self.game._reconcile_vehicle_positions(now + 0.1)
        self.assertIs(self.rear.following, self.leader)
        self.assertAlmostEqual(self.game._following_gap(self.rear, self.leader),
                               180.0, delta=1e-6)


    def test_distant_car_is_not_a_following_pair(self):
        now = time.monotonic()
        self.rear.loc.set_engine_position(500.0, now, 700.0)
        self.leader.loc.engine_distance = 500.0 + 1200.0
        self.leader.loc._engine_anchor = self.leader.loc.engine_distance
        self.leader.loc.index, self.leader.loc.frac = self.leader.loc.filter.pose(
            self.leader.loc.engine_distance)
        before = self.rear.loc.engine_distance
        self.game._reconcile_vehicle_positions(now)
        self.assertIsNone(self.rear.following)
        self.assertAlmostEqual(self.rear.loc.engine_distance, before)

    def test_lap_origin_offset_still_links_when_physically_behind(self):
        now = time.monotonic()
        length = self.leader.loc.filter.length
        self.leader.loc.engine_distance = 1000.0
        # The follower has driven almost one whole lap more, but is physically
        # only 300 mm behind the leader.
        self.rear.loc.engine_distance = 1000.0 + length - 300.0
        for car in (self.leader, self.rear):
            car.loc._engine_anchor = car.loc.engine_distance
            car.loc.index, car.loc.frac = car.loc.filter.pose(car.loc.engine_distance)
            car.loc.last_update = now
        self.rear.loc._engine_speed = 700.0
        self.leader.loc._engine_speed = 400.0
        self.game._reconcile_vehicle_positions(now)
        self.assertIs(self.rear.following, self.leader)
        self.assertAlmostEqual(self.game._following_gap(self.rear, self.leader),
                               300.0, delta=1e-6)

    def test_clamp_keeps_follower_lap_origin_continuous(self):
        now = time.monotonic()
        length = self.leader.loc.filter.length
        self.leader.loc.engine_distance = 1000.0
        self.rear.loc.engine_distance = 1000.0 + length - 100.0
        for car in (self.leader, self.rear):
            car.loc._engine_anchor = car.loc.engine_distance
            car.loc.index, car.loc.frac = car.loc.filter.pose(car.loc.engine_distance)
            car.loc.last_update = now
        self.rear.loc._engine_speed = 700.0
        self.leader.loc._engine_speed = 400.0
        self.game._reconcile_vehicle_positions(now)
        self.assertIs(self.rear.following, self.leader)
        self.assertAlmostEqual(self.game._following_gap(self.rear, self.leader),
                               CAR_MIN_GAP_MM, delta=1e-6)
        # Still in the follower's own (one lap ahead) continuous frame.
        self.assertAlmostEqual(self.rear.loc.engine_distance,
                               1000.0 + length - CAR_MIN_GAP_MM, delta=1e-6)


    def test_lane_change_releases_link(self):
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertIs(self.rear.following, self.leader)
        self.rear.loc.lane_mm = -23.0
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertIsNone(self.rear.following)

    def test_non_running_tick_clears_stale_edge(self):
        now = time.monotonic()
        self.game._reconcile_vehicle_positions(now)
        self.assertIs(self.rear.following, self.leader)
        self.game.running = False
        self.game.tick(0.02)
        self.assertIsNone(self.rear.following)

    def test_leader_stop_is_respected(self):
        self.leader.loc.set_engine_position(self.leader.loc.engine_distance,
                                            time.monotonic(), 0.0)
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertLessEqual(self.rear.loc._engine_speed, 0.0)

    def test_stale_leader_releases_link(self):
        now = time.monotonic()
        self.game._reconcile_vehicle_positions(now)
        self.assertIs(self.rear.following, self.leader)
        self.leader.loc.last_update = now - FOLLOW_STALE_S - 0.1
        self.game._reconcile_vehicle_positions(now)
        self.assertIsNone(self.rear.following)

    def test_follower_appearing_ahead_is_pulled_back_not_released(self):
        now = time.monotonic()
        self.game._reconcile_vehicle_positions(now)
        self.assertIs(self.rear.following, self.leader)
        # A quantized position update must not be allowed to reverse the order.
        self.rear.loc.set_engine_position(self.leader.loc.engine_distance + 700.0,
                                          now, 720.0)
        self.game._reconcile_vehicle_positions(now)
        self.assertIs(self.rear.following, self.leader)
        gap = self.game._following_gap(self.rear, self.leader)
        self.assertAlmostEqual(gap, CAR_MIN_GAP_MM, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
