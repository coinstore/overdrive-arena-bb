"""Same-lane following constraint: engine-only, no weapon logic."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from anki.track import Track, Piece, START, STRAIGHT, TURN
from game.game import CarState, Game, CAR_MIN_GAP_MM, FOLLOW_RELEASE_MM


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
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertIs(self.rear.following, self.leader)
        self.leader.loc.set_engine_position(
            self.rear.loc.engine_distance + FOLLOW_RELEASE_MM + 100,
            time.monotonic(), 400.0)
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertIsNone(self.rear.following)


    def test_one_full_lap_apart_is_not_a_following_pair(self):
        now = time.monotonic()
        self.rear.loc.set_engine_position(500.0, now, 700.0)
        self.leader.loc.set_engine_position(500.0 + 2800.0, now, 400.0)
        before = self.rear.loc.engine_distance
        self.game._reconcile_vehicle_positions(now)
        self.assertIsNone(self.rear.following)
        self.assertAlmostEqual(self.rear.loc.engine_distance, before)


    def test_lane_change_releases_link(self):
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertIs(self.rear.following, self.leader)
        self.rear.loc.lane_mm = -23.0
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertIsNone(self.rear.following)

    def test_leader_stop_is_respected(self):
        self.leader.loc.set_engine_position(self.leader.loc.engine_distance,
                                            time.monotonic(), 0.0)
        self.game._reconcile_vehicle_positions(time.monotonic())
        self.assertLessEqual(self.rear.loc._engine_speed, 0.0)


if __name__ == "__main__":
    unittest.main()
