"""Cooperative scan regressions, including recorded optical telemetry."""
import copy
from pathlib import Path
import unittest

from anki import protocol as P
from anki.track import ScanEvidence, TrackScanner


def recorded():
    return [P.parse(bytes.fromhex(line)) for line in
            Path(__file__).with_name('x52_lap.hex').read_text().splitlines() if line.strip()]


def complete_lap():
    scanner = TrackScanner()
    for message in recorded():
        scanner.on_message(*message)
    assert scanner.done
    return copy.deepcopy(scanner.evidence.attempts[-1])


class ScanFusionTests(unittest.TestCase):
    def test_recorded_lap(self):
        self.assertEqual(len(complete_lap()), 6)

    def test_complementary_cars_close_neither_complete_lap(self):
        shared = ScanEvidence()
        first, second = TrackScanner(shared, 'X52'), TrackScanner(shared, 'Mammoth')
        first.lap = complete_lap()
        first.lap[2] = dict(id=None, turn='R', reversed=False)
        first._finish_lap()
        self.assertFalse(first.done)
        second.lap = complete_lap()
        second.lap[4] = dict(id=None, turn='R', reversed=False)
        second._finish_lap()
        self.assertTrue(second.done)
        self.assertTrue(second.track.closed)
        self.assertEqual(shared.contributors, {'X52', 'Mammoth'})

    def test_conflicting_laps_are_not_fused(self):
        shared = ScanEvidence()
        first, second = TrackScanner(shared), TrackScanner(shared)
        first.lap, second.lap = complete_lap(), complete_lap()
        first.lap[2]['id'] = None
        second.lap[4]['id'] = None
        second.lap[1]['turn'] = 'L'
        first._finish_lap()
        second._finish_lap()
        self.assertFalse(second.done)

    def test_live_partial_car_fills_completed_lap(self):
        shared = ScanEvidence()
        first, second = TrackScanner(shared), TrackScanner(shared)
        first.lap, second.lap = complete_lap(), complete_lap()[:3]
        first.lap[2] = dict(id=None, turn='R', reversed=False)
        first._finish_lap()
        self.assertTrue(first.done)

    def test_preview_combines_live_prefixes_without_mutating_them(self):
        shared = ScanEvidence()
        first, second = TrackScanner(shared), TrackScanner(shared)
        first.lap, second.lap = complete_lap()[:4], complete_lap()[:3]
        first.lap[2]['id'] = None
        self.assertEqual(shared.preview()[2]['id'], second.lap[2]['id'])
        self.assertIsNone(first.lap[2]['id'])

    def test_reverse_lap_normalization(self):
        lap = complete_lap()
        backwards = [dict(row) for row in lap[:1] + list(reversed(lap[1:]))]
        for row in backwards:
            row['reversed'] = not row['reversed']
            row['turn'] = {'R': 'L', 'L': 'R'}.get(row['turn'])
        self.assertEqual(ScanEvidence.normalized(backwards), lap)

    def test_missing_transition_recovered_from_codes(self):
        messages = recorded()
        # Remove each transition independently; changed codes must still retain pieces.
        recovered = 0
        for index, (mid, _) in enumerate(messages):
            if mid != P.MSG_LOCALIZATION_TRANSITION_UPDATE:
                continue
            scanner = TrackScanner()
            for i, message in enumerate(messages):
                if i != index:
                    scanner.on_message(*message)
            if scanner.done and scanner.inferred_boundaries:
                self.assertEqual(len(scanner.track), 6)
                recovered += 1
        self.assertGreaterEqual(recovered, 4)

    def test_receive_timestamp_and_delocalization(self):
        scanner = TrackScanner()
        message = next(m for m in recorded() if m[0] == P.MSG_LOCALIZATION_POSITION_UPDATE)
        scanner.on_message(*message, received_at=123.4)
        self.assertEqual(scanner.last_position_t, 123.4)
        scanner.on_message(P.MSG_VEHICLE_DELOCALIZED, None)
        scanner.on_message(*message, received_at=125)
        self.assertTrue(scanner.stopped)
        self.assertEqual(scanner.last_position_t, 123.4)
