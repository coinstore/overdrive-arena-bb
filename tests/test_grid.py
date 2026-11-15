"""Start-line overshoot handling without physical cars."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch
from anki import protocol as P
from anki.track import Track, Piece, START, START_ID, FINISH_ID, STRAIGHT, TURN
from game.game import CarState, Game, GRID_RETRY_SPEED

class GridTests(unittest.TestCase):
    def setUp(self):
        v = SimpleNamespace(model='Test', battery_mv=0, connected=True, on_charger=False, send=Mock())
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
        self.c.stop_on_transition = Mock()
        self.c.lights_idle = Mock()

    def _begin_at(self, index, frac, sub_id):
        self.c.loc.index, self.c.loc.frac, self.c.loc.sub_id = index, frac, sub_id
        self.c.loc.on_track = True
        self.c.loc.last_update = time.monotonic()
        self.c.loc.direction = 1
        self.c.loc.lane_mm = 23
        self.g.begin_grid()
        self.g.grid_tick(0)

    def test_start_after_the_line_forces_a_full_lap(self):
        self._begin_at(0, .52, 33)
        self.assertEqual(self.c.grid_state, 'retry lap')
        self.assertTrue(self.c.grid_force_lap)
        self.c.command_speed.assert_any_call(GRID_RETRY_SPEED, ANY)
        self.assertNotEqual(self.c.grid_state, 'placed')

    def test_start_before_the_line_forces_a_full_lap(self):
        self._begin_at(0, .45, 34)
        self.assertEqual(self.c.grid_state, 'retry lap')
        self.assertTrue(self.c.grid_force_lap)

    def test_forced_lap_from_the_last_piece_must_pass_the_start(self):
        self._begin_at(1, .7, None)
        self.assertTrue(self.c.grid_force_lap)
        self.assertFalse(self.c.grid_seen_start)
        self.c.loc.index, self.c.loc.frac = 0, .2
        self.g.grid_tick(0)
        self.assertTrue(self.c.grid_force_lap)
        self.assertEqual(self.c.grid_state, 'retry lap')
        self.c.loc.index, self.c.loc.frac = 1, .7
        self.g.grid_tick(0)
        self.assertFalse(self.c.grid_force_lap)
        self.assertFalse(self.c.grid_retry_lap)

    def test_forced_lap_finishes_on_the_line(self):
        self._begin_at(0, .52, 33)
        self.c.loc.index, self.c.loc.frac = 1, .7
        self.g.grid_tick(0)
        self.c.loc.index, self.c.loc.frac, self.c.loc.sub_id = 0, .55, 33
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'placed')
        self.assertTrue(self.c.start_bar_locked)

    def _pose_at_distance(self, distance):
        """Put the car at an unwrapped route distance (no filtering, no engine)."""
        bar = self.g.track.start_bar_frac
        index, frac = self.c.loc.filter.pose(distance)
        self.c.loc.index, self.c.loc.frac = index, frac
        self.c.loc.sub_id = ((FINISH_ID if frac >= bar else START_ID)
                             if index == 0 else None)
        self.c.loc.on_track = True
        self.c.loc.last_update = time.monotonic()
        self.c.loc.direction = 1
        self.c.loc.lane_mm = self.c.loc.code_lane_mm = self.c.grid_lane = 23
        return index

    def test_car_just_past_the_line_drives_only_one_lap(self):
        """Piece 1 behind the line is inside the run-up zone.

        A car there has already passed the start piece. Requiring piece 0
        again would turn the single forced lap into two full laps.
        """
        track = Track([Piece(34, START), Piece(36, STRAIGHT), Piece(23, TURN, 'L'),
                       Piece(36, STRAIGHT), Piece(18, TURN, 'R')], 0.61)
        self.g.set_track(track)
        start = self.c.loc.filter.distance(0, track.start_bar_frac) + 300.0
        self._pose_at_distance(start)
        self.g.begin_grid()
        self.g.grid_tick(0)
        self.assertTrue(self.c.grid_force_lap)
        self.assertTrue(self.c.grid_seen_start)
        self.assertEqual(self.c.loc.index, 1)

        last = len(track.pieces) - 1
        cleared_on_first_last_piece = False
        distance = start
        for _ in range(4000):
            distance += 10.0
            index = self._pose_at_distance(distance)
            self.g.grid_tick(0)
            if index == last:
                cleared_on_first_last_piece = not self.c.grid_retry_lap
                break
        self.assertTrue(cleared_on_first_last_piece)
        while self.c.grid_state != 'placed' and distance < start + 2 * self.c.loc.filter.length:
            distance += 10.0
            self._pose_at_distance(distance)
            self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'placed')
        self.assertLess(distance - start, 1.1 * self.c.loc.filter.length)

    def test_far_start_still_gets_the_normal_approach(self):
        track = Track([Piece(34, START)] + [Piece(36, STRAIGHT) for _ in range(10)])
        self.c.set_track(track)
        self.g.track = track
        self._begin_at(5, .5, None)
        self.assertFalse(self.c.grid_force_lap)
        self.assertFalse(self.c.grid_retry_lap)
        self.assertNotEqual(self.c.grid_state, 'retry lap')

    def test_first_valid_pose_after_recovery_is_checked(self):
        self.c.loc.on_track = False
        self.g.begin_grid()
        self.g.grid_tick(0)
        self.assertTrue(self.c.grid_runup_pending)
        self.c.loc.index, self.c.loc.frac, self.c.loc.sub_id = 0, .52, 33
        self.c.loc.on_track = True
        self.c.loc.last_update = time.monotonic()
        self.c.loc.direction = 1
        self.c.loc.lane_mm = 23
        self.g.grid_tick(0)
        self.assertTrue(self.c.grid_force_lap)
        self.assertEqual(self.c.grid_state, 'retry lap')

    def test_overshoot_retries_only_once_until_next_lap(self):
        self.c.loc.frac = 0.85
        self.g.grid_tick(0)
        for _ in range(10):
            self.g.grid_tick(0)
        self.assertEqual(self.c.grid_laps, 1)
        self.c.hard_stop.assert_not_called()
        self.c.loc.index, self.c.loc.frac = 1, 0.2
        self.g.grid_tick(0)
        self.c.loc.index, self.c.loc.frac = 0, 0.1
        self.g.grid_tick(0)
        self.c.loc.index, self.c.loc.frac, self.c.loc.sub_id = 0, 0.52, 33
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'placed')
        self.c.stop_on_transition.assert_called_once()
        self.c.hard_stop.assert_not_called()



    def test_overshoot_uses_the_measured_start_bar_not_hardcoded_half(self):
        self.g.track.start_bar_frac = self.c.loc.track.start_bar_frac = .62
        self.c.loc.frac, self.c.loc.sub_id = .61, 34
        self.c.grid_fw_stop = True
        self.g.grid_tick(0)
        self.assertNotEqual(self.c.grid_state, 'retry lap')
        self.assertEqual(self.c.grid_laps, 0)
        self.c.loc.frac = .63
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'bar pending')

    def test_firmware_stop_accepts_bar_after_engine_predicted_past_it(self):
        self.c.grid_fw_stop = True
        self.c.loc.index, self.c.loc.frac, self.c.loc.sub_id = 1, .9, 33
        self.c.loc.on_track = True
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'placed')
        self.assertFalse(self.c.grid_fw_stop)
        self.assertTrue(self.c.start_bar_locked)
        self.assertEqual(self.c.loc.index, 0)
        self.assertAlmostEqual(self.c.loc.frac, self.g.track.start_bar_frac)
        # The engine must stay on the bar even without a 0x36 actual=0.
        self.c.loc.advance(2.0, time.monotonic() + 2.0)
        self.assertEqual(self.c.loc.index, 0)
        self.assertAlmostEqual(self.c.loc.frac, self.g.track.start_bar_frac)
        # A late speed/position packet during the countdown must not move it.
        self.c.loc._set_engine_anchor(1, 0.0, time.monotonic(), 300.0)
        self.c.on_message(P.MSG_SPEED_UPDATE, P.SpeedUpdate(300, 450, 300),
                          received_at=time.monotonic())
        self.assertEqual(self.c.loc.index, 0)
        self.assertAlmostEqual(self.c.loc.frac, self.g.track.start_bar_frac)
        self.assertEqual(self.c.command_speed.call_args[0][0], 0)

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
        self.c.command_speed.assert_called_with(300, 1000)
        with patch('game.game.time.monotonic', return_value=1002):
            self.assertFalse(self.g.grid_tick(0))
        self.c.command_speed.assert_called_with(0, 1002)
        with patch('game.game.time.monotonic', return_value=1350):
            self.assertFalse(self.g.grid_tick(0))
        self.c.command_speed.assert_called_with(300, 1350)
        self.assertTrue(self.c.participates())

    def test_recovered_car_can_become_ready(self):
        self.c.loc.on_track = False
        self.assertFalse(self.g.grid_tick(0))
        self.c.loc.on_track = True
        self.c.loc.frac, self.c.loc.sub_id = .52, 33
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'placed')
        self.assertTrue(self.g.grid_tick(0))

    def test_accepts_a_free_lane_instead_of_another_full_lap(self):
        self.c.loc.lane_mm = -68
        self.c.loc.frac, self.c.loc.sub_id = 0.52, 33
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'placed')
        self.assertEqual(self.c.grid_lane, -68)

    def test_does_not_share_a_lane_with_a_placed_car(self):
        other = SimpleNamespace(loc=None, grid_state='placed', grid_lane=-68, actual_lane=lambda: -68, grid_fw_stop=False, grid_fw_repeat_t=0.0,
                                participates=lambda: True, command_speed=lambda *a: None,
                                car=SimpleNamespace(connected=True), charging=False)
        self.g.cars.append(other)
        self.c.loc.lane_mm = -68
        self.c.loc.frac, self.c.loc.sub_id = 0.4167, 34
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'driving')
        self.c.loc.frac, self.c.loc.sub_id = 0.52, 33
        self.g.grid_tick(0)
        self.assertEqual(self.c.grid_state, 'retry lap')

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

if __name__ == '__main__':
    unittest.main()
