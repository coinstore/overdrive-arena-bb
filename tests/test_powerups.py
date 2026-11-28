"""Energy-based powerups: U-turn, hook, anchor and booster."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from anki.track import Piece, Track, START, STRAIGHT
from game.game import (CarState, Game, HOOK_RANGE_MM, UTURN_MIN_ENERGY)
from game.inputs import POWERUP


def make_car(index=0):
    vehicle = SimpleNamespace(model=f"Test{index}", battery_mv=0, connected=True,
                              on_charger=False, send=Mock(), set_speed=Mock())
    return CarState(vehicle, index, Mock(), 800)


def straight_track():
    return Track([Piece(34, START)] + [Piece(36, STRAIGHT) for _ in range(8)])


def place(car, index, frac, lane=0.0, speed=0.0):
    loc = car.loc
    loc.index = index
    loc.frac = frac
    loc.lane_mm = lane
    loc.direction = 1
    loc.on_track = True
    loc.speed = speed
    loc.last_update = time.monotonic()


class PowerupTests(unittest.TestCase):
    def setUp(self):
        self.now = time.monotonic()
        self.car = make_car(0)
        self.other = make_car(1)
        self.game = Game(Mock(), [self.car, self.other], straight_track(), True, 800)
        self.game.running = True
        self.game.mode = "battle"
        place(self.car, 1, 0.1, lane=23, speed=600)
        place(self.other, 1, 0.5, lane=-23, speed=600)
        self.other.target_offset = -23
        self.car._last_speed_sent = 600

    def test_uturn_energy_lasts_three_and_a_half_seconds_from_full(self):
        self.car.powerup_energy = 100.0
        self.game.update_uturn(self.car, True, self.now, 0.02)
        self.assertEqual(self.car.uturn_state, "flipping_out")
        self.assertTrue(self.car.manual_reverse)

        self.car.loc.direction = -1
        self.game.update_uturn(self.car, True, self.now + 0.1, 0.1)
        self.assertEqual(self.car.uturn_state, "reversing")

        self.game.update_uturn(self.car, True, self.now + 3.6, 3.5)
        self.assertEqual(self.car.powerup_energy, 0.0)
        self.assertEqual(self.car.uturn_state, "flipping_back")
        self.assertFalse(self.car.manual_reverse)

    def test_uturn_needs_fifty_percent(self):
        self.car.powerup_energy = UTURN_MIN_ENERGY - 1
        self.game.update_uturn(self.car, True, self.now, 0.02)
        self.assertEqual(self.car.uturn_state, "idle")
        self.assertEqual(self.car.powerup_energy, UTURN_MIN_ENERGY - 1)

    def test_reverse_speed_is_capped_at_500(self):
        source = Mock()
        source.pressed.return_value = False
        source.held.return_value = False
        source.throttle.return_value = 1.0
        source.brake.return_value = 0.0
        source.steer.return_value = 0.0
        self.car.source = source
        self.car.manual_reverse = True
        self.car.cruise_speed = 800.0
        self.game._human(self.car, 0.02, self.now)
        self.assertEqual(self.car.speed_sent(), 500)

    def test_hook_slows_the_nearest_car_ahead_in_any_lane(self):
        self.car.loadout.powerup = "hook"
        self.car.powerup_energy = 100.0
        self.game.update_hook(self.car, True, self.now, 0.1)
        self.assertIs(self.car.hook_target, self.other)
        self.assertLess(self.car.powerup_energy, 100.0)
        self.assertGreater(self.other.hooked_until, self.now)
        self.assertTrue(self.game.tethers)
        self.other.command_speed(800, self.now, force=True)
        self.assertEqual(self.other._last_speed_sent, 600)

    def test_hook_full_energy_lasts_about_five_seconds(self):
        self.car.loadout.powerup = "hook"
        self.car.powerup_energy = 100.0
        self.game.update_hook(self.car, True, self.now, 0.1)
        self.assertIs(self.car.hook_target, self.other)
        self.game.update_hook(self.car, True, self.now + 0.2, 5.0)
        self.assertEqual(self.car.powerup_energy, 0.0)
        self.assertIsNone(self.car.hook_target)

    def test_hook_out_of_range_does_not_attach(self):
        self.car.loadout.powerup = "hook"
        self.car.powerup_energy = 100.0
        place(self.other, 5, 0.5, lane=-23, speed=600)
        self.assertGreater(self.game._hook_gap(self.car, self.other), HOOK_RANGE_MM)
        self.game.update_hook(self.car, True, self.now, 0.1)
        self.assertIsNone(self.car.hook_target)
        self.assertEqual(self.other.hooked_until, 0.0)

    def test_hook_shows_front_light_without_a_target(self):
        self.car.loadout.powerup = "hook"
        self.car.powerup_energy = 100.0
        place(self.other, 5, 0.5, lane=-23, speed=600)
        self.game.update_hook(self.car, True, self.now, 0.1)
        self.assertIsNone(self.car.hook_target)
        self.assertTrue(self.car.hook_light_active)
        self.game.update_hook(self.car, False, self.now + 0.1, 0.1)
        self.assertFalse(self.car.hook_light_active)

    def test_hook_reaches_beyond_the_old_range(self):
        self.car.loadout.powerup = "hook"
        self.car.powerup_energy = 100.0
        place(self.other, 4, 0.5, lane=-23, speed=600)
        gap = self.game._hook_gap(self.car, self.other)
        self.assertGreater(gap, 1000.0)
        self.assertLessEqual(gap, HOOK_RANGE_MM)
        self.game.update_hook(self.car, True, self.now, 0.1)
        self.assertIs(self.car.hook_target, self.other)

    def test_anchor_stops_while_held_and_drains_energy(self):
        self.car.loadout.powerup = "anchor"
        self.car.powerup_energy = 50.0
        self.game.start_anchor(self.car, self.now)
        self.assertTrue(self.car.anchor_active)
        self.assertEqual(self.car.powerup_energy, 50.0)
        self.assertEqual(self.car.speed_sent(), 0)
        self.game.update_anchor(self.car, True, self.now + 1.0, 1.0)
        self.assertTrue(self.car.anchor_active)
        self.assertEqual(self.car.powerup_energy, 30.0)

    def test_anchor_runs_until_energy_is_empty(self):
        self.car.loadout.powerup = "anchor"
        self.car.powerup_energy = 30.0
        self.game.start_anchor(self.car, self.now)
        self.game.update_anchor(self.car, True, self.now + 1.4, 1.4)
        self.assertTrue(self.car.anchor_active)
        self.assertLess(self.car.powerup_energy, 5.0)
        self.game.update_anchor(self.car, True, self.now + 1.6, 0.2)
        self.assertFalse(self.car.anchor_active)
        self.assertEqual(self.car.powerup_energy, 0.0)

    def test_anchor_release_ends_it_and_resumes_the_old_target(self):
        self.car.loadout.powerup = "anchor"
        self.car.powerup_energy = 100.0
        self.car.cruise_speed = 600.0
        source = Mock()
        source.pressed.return_value = False
        source.held.side_effect = lambda action: action == POWERUP
        source.throttle.return_value = 0.0
        source.brake.return_value = 0.0
        source.steer.return_value = 0.0
        self.car.source = source
        self.game.start_anchor(self.car, self.now)
        self.game._human(self.car, 0.02, self.now + 0.2)
        self.assertTrue(self.car.anchor_active)
        self.assertEqual(self.car.speed_sent(), 0)
        source.held.side_effect = lambda action: False
        self.game._human(self.car, 0.02, self.now + 0.4)
        self.assertFalse(self.car.anchor_active)
        self.assertEqual(self.car.speed_sent(), 600)

    def test_booster_full_energy_lasts_about_two_and_a_half_seconds(self):
        self.car.loadout.powerup = "booster"
        self.car.powerup_energy = 100.0
        self.car.target_offset = 23
        self.game.update_booster(self.car, True, self.now, 0.1)
        self.assertTrue(self.car.booster_active)
        self.game.update_booster(self.car, True, self.now + 0.1, 2.4)
        self.assertEqual(self.car.powerup_energy, 0.0)
        self.assertFalse(self.car.booster_active)

    def test_booster_raises_target_above_normal_limit(self):
        self.car.loadout.powerup = "booster"
        self.car.powerup_energy = 100.0
        self.car.target_offset = 23
        self.game.update_booster(self.car, True, self.now, 0.1)
        self.assertTrue(self.car.booster_active)
        self.assertLess(self.car.powerup_energy, 100.0)
        self.assertGreater(self.game.booster_target(self.car), self.car.max_speed)
        self.game.update_booster(self.car, False, self.now + 0.2, 0.1)
        self.assertFalse(self.car.booster_active)
        self.assertGreaterEqual(self.car.booster_ready_at, self.now + 0.2)


if __name__ == '__main__':
    unittest.main()
