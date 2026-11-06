"""Private upload and capability-link download API; run behind TLS."""
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

from store import Store

store = Store(os.environ.get('DIST_STORAGE', '/data'))
upload_token = os.environ['DIST_UPLOAD_TOKEN']
download_token = os.environ['DIST_DOWNLOAD_TOKEN']
if len(upload_token) < 32 or len(download_token) < 32 or upload_token == download_token:
    raise SystemExit('Distribution tokens must be distinct and at least 32 characters')


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def log_message(self, *args):
        pass  # URL paths include bearer capabilities; never log them.

    def respond(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def parts(self):
        return [unquote(p) for p in urlsplit(self.path).path.split('/') if p]

    def authorized(self):
        supplied = self.headers.get('Authorization', '').removeprefix('Bearer ')
        return hmac.compare_digest(supplied.encode('utf-8'), upload_token.encode('utf-8'))

    def do_PUT(self):
        parts = self.parts()
        if not self.authorized():
            return self.respond(403, {'error': 'Forbidden'})
        if len(parts) != 5 or parts[:2] != ['api', 'stage']:
            return self.respond(404, {'error': 'Not found'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            store.stage(parts[2], parts[3], parts[4], self.rfile, size)
        except (OSError, ValueError):
            self.close_connection = True
            return self.respond(400, {'error': 'Upload rejected'})
        self.respond(200, {'ok': True})

    def do_POST(self):
        parts = self.parts()
        if not self.authorized():
            return self.respond(403, {'error': 'Forbidden'})
        if len(parts) != 4 or parts[:2] != ['api', 'publish']:
            return self.respond(404, {'error': 'Not found'})
        try:
            files = store.publish(parts[2], parts[3])
        except (OSError, ValueError):
            return self.respond(400, {'error': 'Publish rejected'})
        self.respond(200, {'files': [quote(name, safe='') for name in files]})

    def do_HEAD(self):
        self.download(head_only=True)

    def do_GET(self):
        if self.path == '/healthz':
            return self.respond(200, {'status': 'ok'})
        self.download(head_only=False)

    def download(self, head_only):
        parts = self.parts()
        if len(parts) != 3 or not hmac.compare_digest(parts[0].encode('utf-8'), download_token.encode('utf-8')):
            return self.respond(404, {'error': 'Not found'})
        try:
            source = store.open(parts[1], parts[2])
            size = os.fstat(source.fileno()).st_size
        except (OSError, ValueError, KeyError):
            return self.respond(404, {'error': 'Not found'})
        with source:
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(size))
            self.send_header('Cache-Control', 'private, no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if not head_only:
                while chunk := source.read(1024 * 1024):
                    self.wfile.write(chunk)


def housekeeping():
    while True:
        try:
            store.cleanup_stale()
        except OSError:
            print('Distribution staging cleanup failed.', file=sys.stderr)
        time.sleep(60 * 60)


if __name__ == '__main__':
    threading.Thread(target=housekeeping, daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('DIST_PORT', '18991'))), Handler).serve_forever()
