import importlib
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'DIST_STORAGE': self.tmp.name,
            'DIST_UPLOAD_TOKEN': 'u' * 40,
            'DIST_DOWNLOAD_TOKEN': 'd' * 40,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        import server
        self.module = importlib.reload(server)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.module.Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, auth=False):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Authorization': 'Bearer ' + 'u' * 40} if auth else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_upload_authentication_publication_and_download(self):
        stage = '/api/stage/android/42-1/client.apk'
        self.assertEqual(self.request('PUT', stage, b'apk')[0], 403)
        self.assertEqual(self.request('PUT', stage, b'apk', True)[0], 200)
        self.assertEqual(self.request('GET', '/' + 'd' * 40 + '/android/client.apk')[0], 404)
        self.assertEqual(self.request('POST', '/api/publish/android/42-1', auth=True)[0], 200)
        url = '/' + 'd' * 40 + '/android/client.apk'
        self.assertEqual(self.request('GET', url), (200, b'apk'))
        self.assertEqual(self.request('HEAD', url), (200, b''))
        self.assertEqual(self.request('GET', '/' + 'x' * 40 + '/android/client.apk')[0], 404)


if __name__ == '__main__':
    unittest.main()
