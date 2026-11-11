"""Discrete four-lane steering and the short lane-change model."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from anki.track import CarLocalizer, Piece, Track, START, STRAIGHT, LANE_MODEL_ENABLED
from game.game import CarState


class LaneSteeringTests(unittest.TestCase):
    def setUp(self):
        vehicle = SimpleNamespace(model="Test", battery_mv=0, connected=True, on_charger=False)
        self.car = CarState(vehicle, 0, Mock(), 800)
        self.car.target_offset = 23.0
        self.now = time.monotonic()

    def test_one_lane_step_per_stick_push(self):
        self.car.steer(0.8, 0.02, self.now)
        self.assertEqual(self.car.target_offset, 68.0)
        self.car.steer(0.8, 0.02, self.now + 0.1)   # held: no repeat
        self.assertEqual(self.car.target_offset, 68.0)
        self.car.steer(0.0, 0.02, self.now + 0.2)   # release
        self.car.steer(-0.8, 0.02, self.now + 0.3)
        self.assertEqual(self.car.target_offset, 23.0)

    def test_control_uses_code_lane_not_model_lane(self):
        self.car.set_track(Track([Piece(34, START), Piece(36, STRAIGHT)]))
        self.car.loc.on_track = True
        self.car.loc.index = 0
        self.car.loc.last_update = self.now
        self.car.loc.direction = 1
        self.car.loc.lane_mm = 68.0       # model is ahead
        self.car.loc.code_lane_mm = 23.0  # last confirmed code
        self.assertEqual(self.car.actual_lane(), 23.0)


class LaneModelTests(unittest.TestCase):
    def setUp(self):
        self.track = Track([Piece(34, START), Piece(36, STRAIGHT)])
        self.loc = CarLocalizer(self.track)
        self.loc.on_track = True
        self.loc.index = 0
        self.loc.frac = 0.1
        self.now = time.monotonic()

    @unittest.skipUnless(LANE_MODEL_ENABLED, "lane model disabled")
    def test_model_moves_toward_command_and_stops_at_target(self):
        self.loc.lane_mm = 0.0
        self.loc.begin_lane_change(68.0, self.now, 400)
        self.loc.advance(0.1, self.now + 0.1)
        self.assertGreater(self.loc.lane_mm, 0.0)
        self.assertLess(self.loc.lane_mm, 68.0)
        self.loc.advance(0.1, self.now + 0.3)
        self.assertEqual(self.loc.lane_mm, 68.0)
        self.assertFalse(self.loc._lane_active)

    @unittest.skipUnless(LANE_MODEL_ENABLED, "lane model disabled")
    def test_lane_update_reanchors_and_stops_at_target(self):
        self.loc.lane_mm = -23.0
        self.loc.on_lane_update(-23.0, 68.0, self.now)
        self.assertTrue(self.loc._lane_active)
        self.loc.advance(0.1, self.now + 0.1)
        self.assertGreater(self.loc.lane_mm, -23.0)
        self.loc.on_lane_update(68.0, 68.0, self.now + 0.5)
        self.assertEqual(self.loc.lane_mm, 68.0)
        self.assertFalse(self.loc._lane_active)

    def test_stationary_lane_status_does_not_overwrite_code_lane(self):
        self.loc.lane_mm = 23.0
        self.loc.on_lane_update(0.0, 0.0, self.now)
        self.assertEqual(self.loc.lane_mm, 23.0)


if __name__ == "__main__":
    unittest.main()
