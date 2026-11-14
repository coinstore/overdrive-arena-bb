"""Notification scheduling and localization regression tests, without Bluetooth."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from anki import protocol as P
from anki.track import CarLocalizer, Track, TrackScanner, Piece, START, STRAIGHT, TURN
from game.game import CarState


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.track = Track([Piece(17, TURN, 'R', reversed=True), Piece(17, TURN, 'R', reversed=True)])
        self.loc = CarLocalizer(self.track)
        self.loc.index, self.loc.frac = 0, 0.9
        self.loc.on_track = True
        self.pos = P.PositionUpdate(11, 17, 0, 800, 0x40)

    def test_missed_transition_between_identical_curves(self):
        self.loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE, self.pos)
        self.assertEqual(self.loc.index, 1)
        self.assertAlmostEqual(self.loc.frac, 1 / 6)

    def test_reverse_missed_transition(self):
        self.loc.index, self.loc.frac, self.loc.direction = 1, 0.1, -1
        self.pos.parsing_flags = 0
        self.pos.location = 0
        self.loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE, self.pos)
        self.assertEqual(self.loc.index, 0)
        self.assertEqual(self.loc.direction, -1)
        self.assertAlmostEqual(self.loc.frac, 5 / 6)

    def test_received_transition_is_not_counted_twice(self):
        self.loc.index, self.loc.frac = 1, 0.03
        self.loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE, self.pos)
        self.assertEqual(self.loc.index, 1)

    def test_unknown_location_does_not_refresh_or_move_pose(self):
        self.loc.last_update = 50
        self.pos.location = 255
        self.loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE, self.pos, received_at=100)
        self.assertEqual(self.loc.index, 0)
        self.assertEqual(self.loc.frac, 0.9)
        self.assertEqual(self.loc.last_update, 50)

    def test_arrival_time_survives_processing_delay(self):
        self.loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE, self.pos, received_at=100)
        self.assertEqual(self.loc.last_update, 100)
        self.assertEqual(self.loc.last_code_t, 100)

    def make_car(self):
        vehicle = SimpleNamespace(model='Test', battery_mv=0, connected=True, on_charger=False)
        car = CarState(vehicle, 0, Mock(), 800)
        car.set_track(self.track)
        car.battery = Mock()
        car._send = Mock()
        return car


    def test_start_bar_fraction_roundtrips_and_maps_both_halves(self):
        track = Track([Piece(34, START), Piece(36, STRAIGHT)], start_bar_frac=.6)
        copy = Track.from_json(track.to_json())
        self.assertAlmostEqual(copy.start_bar_frac, .6)
        loc = CarLocalizer(copy)
        loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE,
                       P.PositionUpdate(20, 34, 0, 400, 0), received_at=100)
        self.assertAlmostEqual(loc.frac, .3)
        loc.on_message(P.MSG_LOCALIZATION_POSITION_UPDATE,
                       P.PositionUpdate(10, 33, 0, 400, 0), received_at=101)
        self.assertAlmostEqual(loc.frac, .9)

    def test_scanner_learns_start_bar_from_wheel_distances(self):
        scanner = TrackScanner()
        scanner.start_lengths = {34: [340.0, 350.0], 33: [220.0, 230.0]}
        self.assertAlmostEqual(scanner.start_bar_fraction(), 345.0 / 570.0, places=6)

    def test_callback_defers_battery_io_to_drain(self):
        car = self.make_car()
        car.enqueue_message(P.MSG_BATTERY_LEVEL_RESPONSE, {'battery_mv': 3800})
        car.battery.update.assert_not_called()
        car.drain_messages()
        car.battery.update.assert_called_once()

    def test_queued_pose_preserves_arrival_time_and_order(self):
        car = self.make_car()
        with patch('game.game.time.monotonic', return_value=100):
            car.enqueue_message(P.MSG_VEHICLE_DELOCALIZED, 'delocalized')
            car.enqueue_message(P.MSG_LOCALIZATION_POSITION_UPDATE, self.pos)
        self.assertIsNone(car.loc.index)
        with patch('game.game.time.monotonic', return_value=105):
            car.drain_messages()
        self.assertEqual(car.loc.last_update, 100)
        self.assertEqual(car.last_radio_t, 100)
        self.assertEqual(car.message_delay_s, 5)
        self.assertEqual(car._offtrack_reports, 1)
        self.assertTrue(car.loc.on_track)


if __name__ == '__main__':
    unittest.main()
