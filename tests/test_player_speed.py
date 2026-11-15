"""Held player speed target and BLE write-rate cap."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from game.game import CarState, SPEED_SEND_INTERVAL_S


class PlayerSpeedTests(unittest.TestCase):
    def setUp(self):
        vehicle = SimpleNamespace(model="Test", battery_mv=0, connected=True, on_charger=False)
        self.car = CarState(vehicle, 0, Mock(), 2000)
        self.car.car.set_speed = Mock()
        self.now = time.monotonic()

    def test_speed_writes_are_capped_but_emergency_stop_is_immediate(self):
        self.car.command_speed(100, self.now)
        self.assertEqual(self.car.speed_sent(), 100)

        self.car.ble.fire.reset_mock()
        self.car.command_speed(200, self.now + 0.02)
        self.assertEqual(self.car.speed_sent(), 100)
        self.car.ble.fire.assert_not_called()

        self.car.command_speed(200, self.now + SPEED_SEND_INTERVAL_S + 0.001)
        self.assertEqual(self.car.speed_sent(), 200)
        self.car.ble.fire.assert_called_once()

        self.car.ble.fire.reset_mock()
        self.car.command_speed(0, self.now + SPEED_SEND_INTERVAL_S + 0.01)
        self.assertEqual(self.car.speed_sent(), 0)
        self.car.ble.fire.assert_called_once()

    def test_forced_command_bypasses_the_interval(self):
        self.car.command_speed(100, self.now)
        self.car.ble.fire.reset_mock()
        self.car.command_speed(200, self.now + 0.02, force=True)
        self.assertEqual(self.car.speed_sent(), 200)
        self.car.ble.fire.assert_called_once()


    def test_sparse_ramp_sends_about_five_target_updates_per_second(self):
        self.car.command_speed(300, self.now, force=True)
        self.car.ble.fire.reset_mock()
        for i in range(1, 51):
            # 450 mm/s^2 at 50 Hz = 9 mm/s per step
            self.car.command_speed(300 + 9 * i, self.now + i * .02)
        self.assertLessEqual(self.car.ble.fire.call_count, 6)
        self.assertGreaterEqual(self.car.ble.fire.call_count, 4)

    def test_release_force_sends_the_exact_held_target_once(self):
        self.car.command_speed(600, self.now, force=True)
        self.car.ble.fire.reset_mock()
        self.car.command_speed(690, self.now + .15)
        self.car.ble.fire.reset_mock()
        self.car.command_speed(690, self.now + .21, force=True)
        self.car.ble.fire.assert_called_once()
        self.car.ble.fire.reset_mock()
        self.car.command_speed(690, self.now + .4)
        self.car.ble.fire.assert_not_called()

    def test_hard_stop_clears_the_held_target(self):
        self.car.cruise_speed = 800
        self.car.hard_stop(self.now)
        self.assertEqual(self.car.cruise_speed, 0)
        self.assertEqual(self.car.speed_sent(), 0)


if __name__ == "__main__":
    unittest.main()
