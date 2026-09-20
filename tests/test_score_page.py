import json
import tempfile
import unittest
from pathlib import Path
from anki.track import Track, Piece, START, TURN
from game.records import Records
from game.score_page import page_data, write_score_page
from game.score_page import LiveScores
from urllib.request import urlopen


class ScorePageTests(unittest.TestCase):
    def test_live_server_publishes_new_scores_without_reopening(self):
        records = Records()
        server = LiveScores(records)
        try:
            with urlopen(server.url + 'scores.json') as response:
                self.assertEqual(json.load(response)['boards'], [])
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            records.entries['track:race:human:lap:0'] = dict(car='Test', seconds=7.2)
            server.publish(records)
            with urlopen(server.url + 'scores.json') as response:
                self.assertEqual(json.load(response)['boards'][0]['rows'][0]['seconds'], 7.2)
            with urlopen(server.url) as response:
                self.assertIn(b'refreshScores', response.read())
        finally:
            server.close()
        self.assertFalse(server.thread.is_alive())
    def test_geometry_persists_and_current_legacy_map_is_recovered(self):
        track = Track([Piece(34, START), Piece(18, TURN, 'R')])
        with tempfile.TemporaryDirectory() as folder:
            records = Records(Path(folder) / 'records.json')
            identity = records.map_key(track)
            records.entries[f'{identity}:race:human:lap:0'] = dict(car='Test', seconds=8)
            before = page_data(records, track)
            self.assertTrue(before['circuits'][0]['path'].startswith('M '))
            records.remember_track(track)
            restored = Records(records.path)
            self.assertEqual(page_data(restored)['circuits'][0]['path'], before['circuits'][0]['path'])

    def test_legacy_missing_layout_and_safe_embedded_data(self):
        records = Records()
        records.entries['old:race:human:lap:0'] = dict(car='</script><script>alert(1)</script>', seconds=8)
        self.assertIsNone(page_data(records)['circuits'][0]['path'])
        with tempfile.TemporaryDirectory() as folder:
            path = write_score_page(records, None, Path(folder) / 'scores.html')
            text = path.read_text(encoding='utf-8')
            payload = text.split('<script id="data" type="application/json">')[1].split('</script>')[0]
            self.assertNotIn('</script>', payload)
            self.assertEqual(json.loads(payload)['boards'][0]['rows'][0]['car'], records.entries['old:race:human:lap:0']['car'])
