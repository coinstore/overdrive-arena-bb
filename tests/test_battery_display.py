import unittest

from game.render import BAD, FAINT, OK, WARN, battery_level_color


class BatteryDisplayTests(unittest.TestCase):
    def test_good_charge_is_green(self):
        self.assertEqual(battery_level_color(80, "ok"), OK)
        self.assertEqual(battery_level_color(46, "ok"), OK)

    def test_mid_charge_is_orange(self):
        self.assertEqual(battery_level_color(45, "ok"), WARN)
        self.assertEqual(battery_level_color(21, "ok"), WARN)

    def test_low_charge_is_red(self):
        self.assertEqual(battery_level_color(20, "ok"), BAD)
        self.assertEqual(battery_level_color(3, "critical"), BAD)
        self.assertEqual(battery_level_color(50, "low"), BAD)

    def test_weak_cell_is_orange_even_when_charged(self):
        self.assertEqual(battery_level_color(90, "weak"), WARN)

    def test_unknown_is_faint(self):
        self.assertEqual(battery_level_color(None, "unknown"), FAINT)


if __name__ == "__main__":
    unittest.main()
