import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / '.github/actions/notify-wecom/send_wecom.py'
spec = importlib.util.spec_from_file_location('distribution_wecom', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class WecomTests(unittest.TestCase):
    def test_links_only_on_success_and_no_partial_urls_when_split(self):
        values = {
            'WECOM_PLATFORM': 'Linux', 'WECOM_VERSION': 'test',
            'DIST_BASE_URL': 'https://example.test:8443',
            'DIST_DOWNLOAD_TOKEN': 'private-capability', 'WECOM_DIST_PLATFORM': 'linux',
            'WECOM_DOWNLOAD_FILES': json.dumps([f'client-{n}.AppImage' for n in range(25)]),
        }
        with patch.dict(os.environ, values, clear=True):
            message = module.build_message()
        self.assertIn('https://example.test:8443/private-capability/linux/client-0.AppImage', message)
        pieces = module.split_message(message)
        self.assertEqual('\n'.join(pieces), message)
        self.assertTrue(all(len(piece.encode('utf-8')) <= 2048 for piece in pieces))
        with patch.dict(os.environ, {**values, 'WECOM_STATUS': 'failure'}, clear=True):
            self.assertNotIn('下载链接', module.build_message())


if __name__ == '__main__':
    unittest.main()
