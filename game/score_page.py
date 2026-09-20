"""Self-contained scorebook: local data, real circuit geometry, no web services."""
import json
from pathlib import Path
from anki.track import Track
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread


class LiveScores:
    """Loopback-only score display; immutable snapshots published by the game thread."""

    def __init__(self, records, current=None):
        self._lock = Lock()
        self.publish(records, current)
        owner = self
        template = Path(__file__).with_name('score_page.html').read_text(encoding='utf-8')

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ('/', '/scores.json'):
                    self.send_error(404)
                    return
                with owner._lock:
                    payload = owner._payload
                if self.path == '/':
                    safe = payload.decode().replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
                    body = template.replace('__SCORE_DATA__', safe).encode('utf-8')
                    content_type = 'text/html; charset=utf-8'
                else:
                    body, content_type = payload, 'application/json'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True, name='live-scores')
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/'

    def publish(self, records, current=None):
        payload = json.dumps(page_data(records, current)).encode('utf-8')
        with self._lock:
            self._payload = payload

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def page_data(records, current=None):
    maps = dict(records.maps)
    current_id = records.map_key(current) if current else None
    if current:
        maps[current_id] = json.loads(current.to_json())
    ids = sorted(set(maps) | {group[0] for group, _ in records.archive()})
    circuits = []
    for index, identity in enumerate(ids):
        item = dict(id=identity, name=f"Circuit {index + 1}", current=identity == current_id, path=None)
        try:
            track = Track.from_json(json.dumps(maps[identity]))
            points = [track.world_point(i, step / 32) for i in range(len(track.pieces)) for step in range(33)]
            x0, y0, x1, y1 = track.bounds()
            item.update(path="M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in points),
                        view=f"{x0-180} {y0-180} {x1-x0+360} {y1-y0+360}",
                        pieces=len(track.pieces), length=round(sum(p.length_mm for p in track.pieces) / 1000, 1))
        except (KeyError, ValueError, TypeError, IndexError):
            pass
        circuits.append(item)
    return dict(circuits=circuits, boards=[dict(group=list(group), rows=rows) for group, rows in records.archive()], error=records.error)


def write_score_page(records, current, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(page_data(records, current)).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = Path(__file__).with_name('score_page.html').read_text(encoding='utf-8')
    destination.write_text(template.replace('__SCORE_DATA__', payload), encoding='utf-8')
    return destination.resolve()
