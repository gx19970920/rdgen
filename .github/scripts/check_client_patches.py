"""Check bundled optional patches against the supported RustDesk release tags."""
import argparse
import re
import runpy
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ('1.5.0', '1.5.1')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', choices=VERSIONS, action='append')
    args = parser.parse_args()
    patches = [p for p in (ROOT / '.github/patches').glob('*.diff')
               if not p.name.startswith('flutter_')]
    names = sorted({m.group(1) for p in patches
                    for m in re.finditer(r'^\+\+\+ b/(.+)$', p.read_text(encoding='utf-8'), re.M)})
    names = sorted(set(names) | {'src/common.rs'})
    update_patch = runpy.run_path(str(ROOT / '.github/patches/disable_update_check.py'))
    for version in args.version or VERSIONS:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            for name in names:
                target = checkout / name
                target.parent.mkdir(parents=True, exist_ok=True)
                url = f'https://raw.githubusercontent.com/rustdesk/rustdesk/{version}/{name}'
                with urllib.request.urlopen(url, timeout=60) as response:
                    target.write_bytes(response.read())
            original = (checkout / 'src/common.rs').read_text(encoding='utf-8')
            patched = update_patch['patch'](original)
            assert patched.replace(update_patch['INSERTION'], '', 1) == original
            assert update_patch['patch'](patched) == patched
            print(f'{version}: update disabling preserves all original Rust code')
            for patch in patches:
                normalized = checkout / patch.name
                normalized.write_text(patch.read_text(encoding='utf-8'),
                                      encoding='utf-8', newline='\n')
                subprocess.run(['git', 'apply', '--check', str(normalized)],
                               cwd=checkout, check=True)
                print(f'{version}: {patch.name} applies')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
