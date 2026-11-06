import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    'generator-android.yml': ('android', 1),
    'generator-windows.yml': ('windows-x64', 1),
    'generator-windows-x86.yml': ('windows-x86', 1),
    'sh-generator-windows.yml': ('windows-selfhosted', 1),
    'generator-linux.yml': ('linux', 4),
    'generator-macos.yml': ('macos', 1),
}


class WorkflowTests(unittest.TestCase):
    def test_all_platforms_keep_webdav_and_do_not_call_distribution_server(self):
        for name, (platform, count) in WORKFLOWS.items():
            with self.subTest(workflow=name):
                data = yaml.safe_load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'))
                jobs = data['jobs']
                steps = [step for job in jobs.values() for step in job.get('steps', [])]
                webdav = [step for step in steps if 'upload_webdav.py' in step.get('run', '')]
                self.assertGreaterEqual(len(webdav), count)
                self.assertFalse(any('upload_distribution.py' in step.get('run', '') for step in steps))
                cleanup = jobs['cleanup']
                notify = next(step for step in cleanup['steps'] if step.get('name') == 'Notify WeCom')
                self.assertNotIn('dist_publish', notify['with']['status'])
                self.assertNotIn('DIST_DOWNLOAD_TOKEN', notify['env'])

    def test_upstream_sync_tests_in_network_isolation_and_does_not_force_push(self):
        data = yaml.safe_load((ROOT / '.github/workflows/sync-upstream.yml').read_text(encoding='utf-8'))
        jobs = data['jobs']
        validate = jobs['validate']
        integrate = jobs['integrate']
        self.assertEqual(validate['permissions']['contents'], 'read')
        self.assertEqual(integrate['permissions']['contents'], 'write')
        self.assertIn('unshare --net', next(s['run'] for s in validate['steps'] if s.get('name') == 'Test candidate with network isolated'))
        push = next(s['run'] for s in integrate['steps'] if s.get('name') == 'Integrate only the tested upstream tree')
        self.assertIn('EXPECTED_TREE', push)
        self.assertIn('git push origin', push)
        self.assertNotIn('--force', push)
        self.assertIn('notify-failure', jobs)


if __name__ == '__main__':
    unittest.main()
