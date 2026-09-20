"""Start-line overshoot handling without physical cars."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from anki.track import Track, Piece, START, TURN
from game.game import CarState, Game

class GridTests(unittest.TestCase):
    def setUp(self):
        v = SimpleNamespace(model='Test', battery_mv=0, connected=True, on_charger=False)
        self.c = CarState(v, 0, Mock(), 800)
        self.g = Game(Mock(), [self.c], Track([Piece(34, START), Piece(18, TURN, 'R')]), True, None)
        self.c.grid_state = 'driving'
        self.c.ai = True
        self.c.grid_t0 = time.monotonic()
        self.c.loc.index, self.c.loc.on_track = 0, True
        self.c.loc.last_update = time.monotonic()
        self.c.loc.lane_mm = self.c.grid_lane = 23
        self.c.command_speed = Mock()
        self.c.hard_stop = Mock()
        self.c.apply_lane = Mock()
        self.c.lights_idle = Mock()

    def test_overshoot_retries_only_once_until_next_lap(self):
        self.c.loc.frac = 0.85
        self.g.grid_tick(0)
        for _ in range(10):
            self.g.grid_tick(0)
        self.assertEqual(self.c.grid_laps, 1)
        self.c.hard_stop.assert_not_called()
        self.c.loc.index, self.c.loc.frac = 1, 0.2
        self.g.grid_tick(0)
        self.c.loc.index, self.c.loc.frac, self.c.loc.sub_id = 0, 0.52, 33
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'placed')
        self.c.hard_stop.assert_called_once()

    def test_missing_bar_transition_does_not_park_beyond_line(self):
        self.c.loc.frac = 0.4
        self.g.grid_tick(0)
        self.c.loc.index, self.c.loc.frac = 1, 0.1
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'retry lap')
        self.c.hard_stop.assert_not_called()

    def test_failed_attempts_keep_retrying(self):
        self.c.grid_laps = 2
        self.c.loc.frac = 0.85
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'retry lap')
        self.assertTrue(self.c.participates())
        self.c.hard_stop.assert_not_called()

    def test_missing_position_retries_with_pauses_without_timeout(self):
        self.c.loc.on_track = False
        with patch('game.game.time.monotonic', return_value=1000):
            self.assertFalse(self.g.grid_tick(0))
        self.c.command_speed.assert_called_with(250, 1000)
        with patch('game.game.time.monotonic', return_value=1002):
            self.assertFalse(self.g.grid_tick(0))
        self.c.command_speed.assert_called_with(0, 1002)
        with patch('game.game.time.monotonic', return_value=1350):
            self.assertFalse(self.g.grid_tick(0))
        self.c.command_speed.assert_called_with(250, 1350)
        self.assertTrue(self.c.participates())

    def test_recovered_car_can_become_ready(self):
        self.c.loc.on_track = False
        self.assertFalse(self.g.grid_tick(0))
        self.c.loc.on_track = True
        self.c.loc.frac, self.c.loc.sub_id = .52, 33
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'placed')
        self.assertTrue(self.g.grid_tick(0))

    def test_only_parked_car_is_excluded(self):
        self.c.ai = False
        self.assertTrue(self.g.grid_tick(0))
        self.assertEqual(self.c.grid_state, '')

    def test_unavailable_participant_blocks_countdown(self):
        self.c.car.connected = False
        self.assertFalse(self.g.grid_tick(0))
        self.assertEqual(self.c.grid_state, 'waiting for connection')
        self.c.car.connected = True
        self.c.car.on_charger = True
        self.assertFalse(self.g.grid_tick(0))
        self.assertEqual(self.c.grid_state, 'waiting for charger removal')

    def test_wrong_lane_also_requires_full_retry(self):
        self.c.loc.lane_mm = -68
        self.c.loc.frac, self.c.loc.sub_id = 0.52, 33
        for _ in range(10):
            self.g.grid_tick(0)
        self.assertEqual(self.c.grid_laps, 1)
        self.assertEqual(self.c.grid_state, 'retry lap')

if __name__ == '__main__':
    unittest.main()
