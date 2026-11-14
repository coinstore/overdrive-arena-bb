"""Measurement time and exact engine-to-display mapping."""
import time
import unittest

from anki import protocol as P
from anki.track import CarLocalizer, Piece, Track, START, STRAIGHT, locate


class LocalizerLeadTests(unittest.TestCase):
    def setUp(self):
        self.track = Track([Piece(34, START), Piece(36, STRAIGHT)])
        self.loc = CarLocalizer(self.track)
        self.now = time.monotonic()
        self.code = next(c for c in range(256) if locate(36, c, False) is not None)

    def test_measurement_delay_shifts_fix_but_keeps_hud_age_at_arrival(self):
        delay = 0.03
        self.loc.on_message(
            P.MSG_LOCALIZATION_POSITION_UPDATE,
            P.PositionUpdate(self.code, 36, 0.0, 1000, 0),
            received_at=self.now,
            measurement_delay=delay,
        )
        self.assertAlmostEqual(self.loc.filter.last_fix, self.now - delay)
        self.assertAlmostEqual(self.loc.last_update, self.now)
        self.assertIsNotNone(self.loc.filter.position)

    def test_render_shows_the_engine_position_exactly(self):
        self.loc.on_track = True
        self.loc.lane_mm = 0.0
        self.loc.direction = 1
        self.loc.filter.position = 500.0
        self.loc.filter.last_fix = self.now
        self.loc.filter.time = self.now
        self.loc.filter.speed = 0.0
        self.loc._display_distance = 1000.0
        self.loc._set_engine_anchor(0, 0.8, self.now, 0.0)
        world = self.loc.render_world(self.now)
        index, frac = self.loc.filter.pose(0.8 * 560.0)
        self.assertEqual(world, self.track.world_point(index, frac, 0.0))
        # A backward engine correction is visible immediately; the display
        # must not keep the old monotonic pose.
        self.loc._set_engine_anchor(0, 0.4, self.now + 0.02, 0.0)
        world = self.loc.render_world(self.now + 0.02)
        index, frac = self.loc.filter.pose(0.4 * 560.0)
        self.assertEqual(world, self.track.world_point(index, frac, 0.0))


if __name__ == "__main__":
    unittest.main()
