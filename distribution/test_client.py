import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / '.github/scripts/upload_distribution.py'
spec = importlib.util.spec_from_file_location('distribution_client', path)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class ClientTests(unittest.TestCase):
    def test_network_errors_retry_but_http_rejection_does_not(self):
        with patch.object(client, 'request_once', side_effect=[ConnectionResetError(), {'ok': True}]) as call, \
             patch.object(client.time, 'sleep') as sleep:
            self.assertEqual(client.request('PUT', '/test'), {'ok': True})
            self.assertEqual(call.call_count, 2)
            sleep.assert_called_once_with(2)
        with patch.object(client, 'request_once', side_effect=ValueError('HTTP 400')) as call, \
             patch.object(client.time, 'sleep') as sleep:
            with self.assertRaises(ValueError):
                client.request('PUT', '/test')
            self.assertEqual(call.call_count, 1)
            sleep.assert_not_called()

    def test_stage_uses_per_workflow_run_number_and_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'client.apk'
            artifact.write_bytes(b'apk')
            env = {'GITHUB_RUN_NUMBER': '17', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_RUN_ID': '9999'}
            with patch.dict(os.environ, env, clear=True), patch.object(sys, 'argv',
                 ['client', 'stage', '--platform', 'android', '--file', str(artifact)]), \
                 patch.object(client, 'request', return_value={'ok': True}) as request:
                client.main()
            self.assertEqual(request.call_args.args[1], '/api/stage/android/17-2/client.apk')


if __name__ == '__main__':
    unittest.main()
