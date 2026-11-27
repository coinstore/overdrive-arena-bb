"""Straight-line blaster targeting on straights and curves."""
import math
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from anki.track import Piece, Track, START, STRAIGHT, TURN
from game.game import (BATTLE_LIVES, BLASTER_DAMAGE, BLASTER_TRACK_CORRIDOR_MM,
                       CarState, DEATH_STOP_S, Game, MAX_HP)


def make_car(index=0):
    vehicle = SimpleNamespace(model='Test', battery_mv=0, connected=True, on_charger=False,
                              send=Mock())
    return CarState(vehicle, index, Mock(), 800)


def place(car, index, frac, lane=0.0):
    loc = car.loc
    loc.index = index
    loc.frac = frac
    loc.lane_mm = lane
    loc.direction = 1
    loc.on_track = True
    loc.speed = 0.0
    loc.last_update = time.monotonic()


def straight_track(count=6):
    return Track([Piece(36, STRAIGHT) for _ in range(count)])


def corner_track():
    return Track([Piece(34, START), Piece(36, STRAIGHT), Piece(17, TURN, 'R'),
                  Piece(18, TURN, 'R'), Piece(39, STRAIGHT), Piece(20, TURN, 'R')])


def nearest_centerline_mm(track, x, y):
    best = float('inf')
    for i, p in enumerate(track.pieces):
        n = max(2, int(p.length_mm / 20))
        for k in range(n + 1):
            sx, sy = track.world_point(i, k / n)
            best = min(best, math.dist((x, y), (sx, sy)))
    return best


class WeaponAimTests(unittest.TestCase):
    def game(self, track, count=2):
        cars = [make_car(i) for i in range(count)]
        return Game(Mock(), cars, track, True, 800), cars

    def test_straight_shot_has_no_artificial_range(self):
        game, (shooter, target) = self.game(straight_track())
        place(shooter, 0, 0.1, lane=23)
        place(target, 4, 0.5, lane=23)

        found = game.target_ahead(shooter)

        self.assertIsNotNone(found)
        self.assertIs(found[0], target)
        self.assertGreater(found[1], 1400.0)

    def test_fire_damages_target_on_the_line(self):
        game, (shooter, target) = self.game(straight_track())
        place(shooter, 0, 0.1, lane=23)
        place(target, 0, 0.5, lane=23)
        now = time.monotonic()

        game.fire(shooter, now)

        self.assertEqual(target.hp, MAX_HP - BLASTER_DAMAGE)
        self.assertTrue(game.lasers[-1].hit)

    def test_adjacent_lane_is_not_hit(self):
        game, (shooter, target) = self.game(straight_track())
        place(shooter, 0, 0.1, lane=-23)
        place(target, 0, 0.5, lane=23)

        self.assertIsNone(game.target_ahead(shooter))

    def test_nearest_car_on_the_line_wins(self):
        game, (shooter, near, far) = self.game(straight_track(), count=3)
        place(shooter, 0, 0.1, lane=0)
        place(near, 2, 0.5, lane=0)
        place(far, 4, 0.5, lane=0)

        found = game.target_ahead(shooter)

        self.assertIsNotNone(found)
        self.assertIs(found[0], near)

    def test_car_behind_is_not_hit(self):
        game, (shooter, target) = self.game(straight_track())
        place(shooter, 2, 0.5, lane=0)
        place(target, 1, 0.5, lane=0)

        self.assertIsNone(game.target_ahead(shooter))

    def test_car_around_the_corner_is_not_hit(self):
        game, (shooter, target) = self.game(corner_track())
        place(shooter, 1, 0.1, lane=0)
        place(target, 2, 0.5, lane=0)

        self.assertIsNone(game.target_ahead(shooter))

    def test_curve_shot_hits_a_car_on_the_tangent(self):
        game, (shooter, target) = self.game(corner_track())
        place(shooter, 2, 0.1, lane=0)
        place(target, 2, 0.3, lane=0)
        now = time.monotonic()

        found = game.target_ahead(shooter)

        self.assertIsNotNone(found)
        self.assertIs(found[0], target)
        game.fire(shooter, now)
        self.assertEqual(target.hp, MAX_HP - BLASTER_DAMAGE)

    def test_beam_ends_at_the_track_edge(self):
        track = corner_track()
        x, y = track.world_point(1, 0.1)
        angle = track.heading_at(1, 0.1)

        length, x1, y1 = track.beam_end(x, y, angle, BLASTER_TRACK_CORRIDOR_MM)

        self.assertGreater(length, 0)
        self.assertLess(length, 1000)
        self.assertLessEqual(nearest_centerline_mm(track, x1, y1), BLASTER_TRACK_CORRIDOR_MM)
        beyond = (x1 + math.cos(angle) * 20, y1 + math.sin(angle) * 20)
        self.assertGreater(nearest_centerline_mm(track, *beyond), BLASTER_TRACK_CORRIDOR_MM)

    def test_long_straight_beam_is_not_capped_at_the_old_range(self):
        track = straight_track()
        x, y = track.world_point(0, 0.1)
        angle = track.heading_at(0, 0.1)

        length, _x1, _y1 = track.beam_end(x, y, angle, BLASTER_TRACK_CORRIDOR_MM)

        self.assertGreater(length, 1400.0)

    def test_miss_laser_ends_at_the_track_edge(self):
        game, (shooter,) = self.game(corner_track(), count=1)
        place(shooter, 1, 0.1, lane=0)
        now = time.monotonic()
        p0 = shooter.world()
        angle = shooter.loc.heading()
        expected, _ex, _ey = game.track.beam_end(p0[0], p0[1], angle, BLASTER_TRACK_CORRIDOR_MM)

        game.fire(shooter, now)

        laser = game.lasers[-1]
        self.assertFalse(laser.hit)
        self.assertAlmostEqual(math.dist((laser.x0, laser.y0), (laser.x1, laser.y1)), expected, places=3)
        self.assertLess(expected, 1000)


class LoadoutTests(unittest.TestCase):
    def game(self, track, count=2):
        cars = [make_car(i) for i in range(count)]
        game = Game(Mock(), cars, track, True, 800)
        game.mode = "battle"
        game.running = True
        game._battle_contenders = count
        for i, car in enumerate(cars):
            car.source = SimpleNamespace(short=f"P{i + 1}", rumble=Mock())
        return game, cars

    def test_weapon_choice_uses_mine_instead_of_blaster(self):
        game, (shooter, target) = self.game(straight_track())
        place(shooter, 0, 0.1, lane=23)
        place(target, 0, 0.5, lane=23)
        shooter.loadout.weapon = "mine"
        now = time.monotonic()

        game.use_weapon(shooter, now)

        self.assertEqual(len(game.mines), 1)
        self.assertEqual(target.hp, MAX_HP)
        self.assertEqual(game.lasers, [])

    def test_weapon_choice_uses_blaster_when_equipped(self):
        game, (shooter, target) = self.game(straight_track())
        place(shooter, 0, 0.1, lane=23)
        place(target, 0, 0.5, lane=23)
        shooter.loadout.weapon = "blaster"
        now = time.monotonic()

        game.use_weapon(shooter, now)

        self.assertEqual(game.mines, [])
        self.assertEqual(target.hp, MAX_HP - BLASTER_DAMAGE)

    def test_destruction_costs_a_life_and_keeps_the_car_in_the_battle(self):
        game, (attacker, victim) = self.game(straight_track())
        place(attacker, 0, 0.1, lane=23)
        place(victim, 0, 0.5, lane=23)
        now = time.monotonic()

        game.damage(victim, attacker, MAX_HP, now)

        self.assertEqual(victim.lives, BATTLE_LIVES - 1)
        self.assertFalse(victim.eliminated)
        self.assertAlmostEqual(victim.dead_until, now + DEATH_STOP_S)
        self.assertIsNone(game.winner)
        self.assertEqual(attacker.kills, 1)

    def test_battle_ranking_keeps_active_cars_ahead_of_parked_cars(self):
        game, (active, parked) = self.game(straight_track())
        active.lives = 4
        active.kills = 1
        parked.source = None
        parked.ai = False
        parked.lives = BATTLE_LIVES
        parked.kills = 0

        self.assertIs(game.ranking()[0], active)

    def test_last_car_with_lives_wins_after_the_final_destruction(self):
        game, (attacker, victim) = self.game(straight_track())
        place(attacker, 0, 0.1, lane=23)
        place(victim, 0, 0.5, lane=23)
        victim.lives = 1
        now = time.monotonic()

        game.damage(victim, attacker, MAX_HP, now)

        self.assertEqual(victim.lives, 0)
        self.assertTrue(victim.eliminated)
        self.assertTrue(victim.is_dead(now + 10))
        self.assertIs(game.winner, attacker)
        self.assertTrue(game.finished)


if __name__ == '__main__':
    unittest.main()
