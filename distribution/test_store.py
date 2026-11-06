import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def stage(self, platform, run, name, content=b'client'):
        self.store.stage(platform, run, name, io.BytesIO(content), len(content))

    def test_latest_run_keeps_all_formats_and_removes_only_old_platform_release(self):
        self.stage('android', '10-1', 'arm.apk')
        self.store.publish('android', '10-1')
        self.stage('windows-x64', '11-1', 'client.exe')
        self.store.publish('windows-x64', '11-1')
        self.stage('android', '12-1', 'arm.apk', b'new')
        self.stage('android', '12-1', 'x86.apk')
        self.assertEqual(self.store.publish('android', '12-1'), ['arm.apk', 'x86.apk'])
        self.assertEqual(self.store.resolve('android', 'arm.apk').read_bytes(), b'new')
        self.assertFalse((Path(self.temp.name) / 'releases/android/10-1').exists())
        self.assertTrue(self.store.resolve('windows-x64', 'client.exe').exists())

    def test_older_run_cannot_replace_newer_run(self):
        self.stage('android', '20-1', 'arm.apk')
        self.store.publish('android', '20-1')
        with self.assertRaises(ValueError):
            self.stage('android', '19-1', 'arm.apk')
        with self.assertRaises(ValueError):
            self.store.publish('android', '19-1')
        self.assertTrue(self.store.resolve('android', 'arm.apk').exists())

    def test_rejects_traversal_and_incomplete_upload(self):
        with self.assertRaises(ValueError):
            self.stage('android', '1-1', '../other')
        with self.assertRaises(ValueError):
            self.store.stage('android', '1-1', 'arm.apk', io.BytesIO(b'x'), 2)
        self.assertFalse((Path(self.temp.name) / 'staging/android/1-1/arm.apk').exists())

    def test_parallel_uploads_do_not_block_each_other_or_publish_incomplete_run(self):
        started = threading.Event()
        release = threading.Event()
        class SlowInput:
            def read(self, size):
                started.set()
                if not release.wait(3):
                    raise RuntimeError('Timeout waiting for concurrent upload')
                return b'x'
        errors = []
        def stage_slow():
            try:
                self.store.stage('android', '12-1', 'slow.apk', SlowInput(), 1)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=stage_slow)
        worker.start()
        self.assertTrue(started.wait(2))
        try:
            self.stage('android', '12-1', 'fast.apk')
            with self.assertRaises(ValueError):
                self.store.publish('android', '12-1')
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.publish('android', '12-1'), ['fast.apk', 'slow.apk'])

    def test_older_upload_finishing_after_publication_is_discarded(self):
        started = threading.Event()
        release = threading.Event()
        class SlowInput:
            def read(self, size):
                started.set()
                release.wait(3)
                return b'x'
        errors = []
        def old_upload():
            try:
                self.store.stage('android', '10-1', 'old.apk', SlowInput(), 1)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=old_upload)
        worker.start()
        self.assertTrue(started.wait(2))
        try:
            self.stage('android', '11-1', 'new.apk')
            self.store.publish('android', '11-1')
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertFalse((Path(self.temp.name) / 'staging/android/10-1').exists())
        self.assertTrue(self.store.resolve('android', 'new.apk').exists())

    def test_failed_manifest_commit_preserves_previous_release_and_staged_files(self):
        self.stage('android', '10-1', 'arm.apk')
        self.store.publish('android', '10-1')
        self.stage('android', '11-1', 'arm.apk', b'new')
        real_replace = __import__('os').replace
        def fail_current(src, dst):
            if str(dst).endswith('android.json'):
                raise OSError('simulated disk error')
            return real_replace(src, dst)
        with patch('store.os.replace', side_effect=fail_current):
            with self.assertRaises(OSError):
                self.store.publish('android', '11-1')
        self.assertEqual(self.store.resolve('android', 'arm.apk').read_bytes(), b'client')
        self.assertTrue((Path(self.temp.name) / 'staging/android/11-1/arm.apk').exists())

    def test_stale_staging_cleanup_does_not_remove_published_files(self):
        self.stage('android', '11-1', 'published.apk')
        self.store.publish('android', '11-1')
        self.stage('android', '12-1', 'abandoned.apk')
        staged = Path(self.temp.name) / 'staging/android/12-1'
        old = time.time() - 1000
        os.utime(staged, (old, old))
        self.store.cleanup_stale(max_age_seconds=100)
        self.assertFalse(staged.exists())
        self.assertTrue(self.store.resolve('android', 'published.apk').exists())


if __name__ == '__main__':
    unittest.main()
