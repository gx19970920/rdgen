"""Safety checks for the narrow automatic conflict resolver."""

import importlib.util
from pathlib import Path
import unittest


path = Path(__file__).with_name('merge_upstream.py')
spec = importlib.util.spec_from_file_location('merge_upstream', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ActionConflictTests(unittest.TestCase):
    def test_accepts_same_action_version_bump(self):
        conflict = (
            '      - name: download\n'
            '<<<<<<< HEAD\n'
            '      - uses: actions/download-artifact@v4\n'
            '=======\n'
            '      - uses: actions/download-artifact@v8\n'
            '>>>>>>> upstream\n'
            '        with:\n'
        )
        result = module.resolve_action_versions(conflict)
        self.assertIn('actions/download-artifact@v8', result)
        self.assertNotIn('<<<<<<<', result)

    def test_rejects_code_or_different_action(self):
        for theirs in ('      - uses: other/download-artifact@v8\n',
                       '      - run: echo changed\n'):
            with self.subTest(theirs=theirs):
                conflict = (
                    '<<<<<<< HEAD\n'
                    '      - uses: actions/download-artifact@v4\n'
                    '=======\n' + theirs + '>>>>>>> upstream\n'
                )
                self.assertIsNone(module.resolve_action_versions(conflict))

    def test_rejects_downgrade_and_multiple_changed_lines(self):
        for theirs in ('      - uses: actions/download-artifact@v3\n',
                       '      - uses: actions/download-artifact@v8\n'
                       '      - run: echo changed\n'):
            with self.subTest(theirs=theirs):
                conflict = (
                    '<<<<<<< HEAD\n'
                    '      - uses: actions/download-artifact@v4\n'
                    '=======\n' + theirs + '>>>>>>> upstream\n'
                )
                self.assertIsNone(module.resolve_action_versions(conflict))


if __name__ == '__main__':
    unittest.main()
