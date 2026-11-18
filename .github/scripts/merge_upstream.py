"""Merge upstream, resolving only identical-action version bumps automatically.

Other conflicts are left for review. This keeps custom workflow code and server
security settings from being silently overwritten by a merge strategy option.
"""

import re
import subprocess
import sys
from pathlib import Path


CONFLICT = re.compile(
    r"^<<<<<<< HEAD\n(.*?)^=======\n(.*?)^>>>>>>> [^\n]+\n",
    re.MULTILINE | re.DOTALL,
)
ACTION = re.compile(
    r"^\s*(?:-\s*)?uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@v([0-9]+)\s*$"
)


def resolve_action_versions(content):
    """Return resolved text only if every conflict is a single action bump."""
    matches = list(CONFLICT.finditer(content))
    if not matches:
        return None

    for match in matches:
        ours = ACTION.fullmatch(match.group(1).strip())
        theirs = ACTION.fullmatch(match.group(2).strip())
        if not ours or not theirs or ours.group(1) != theirs.group(1):
            return None
        if int(theirs.group(2)) <= int(ours.group(2)):
            return None

    return CONFLICT.sub(lambda match: match.group(2), content)


def run(*args):
    return subprocess.run(args, text=True, capture_output=True, check=False)


def main(upstream_sha):
    result = run('git', 'merge', '--no-ff', '--no-edit', '-Xrenormalize', upstream_sha)
    if result.returncode == 0:
        print(result.stdout)
        return 0

    unmerged = run('git', 'diff', '--name-only', '--diff-filter=U')
    if unmerged.returncode or not unmerged.stdout.strip():
        print(result.stdout + result.stderr, file=sys.stderr)
        return 1

    paths = [Path(name) for name in unmerged.stdout.splitlines()]
    resolved = {}
    for path in paths:
        if path.parent != Path('.github/workflows') or path.suffix not in {'.yml', '.yaml'}:
            break
        candidate = resolve_action_versions(path.read_text(encoding='utf-8'))
        if candidate is None:
            break
        resolved[path] = candidate

    if len(resolved) != len(paths):
        print('Upstream merge needs review in:', file=sys.stderr)
        for path in paths:
            print(f'  {path}', file=sys.stderr)
        print('Only single-action version bumps are resolved automatically.', file=sys.stderr)
        return 1

    for path, candidate in resolved.items():
        path.write_text(candidate, encoding='utf-8')
        staged = run('git', 'add', '--', str(path))
        if staged.returncode:
            print(staged.stderr, file=sys.stderr)
            return 1
        print(f'Accepted upstream action version in {path}')
    committed = run('git', 'commit', '--no-edit')
    if committed.returncode:
        print(committed.stdout + committed.stderr, file=sys.stderr)
    return committed.returncode


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: merge_upstream.py UPSTREAM_SHA')
    raise SystemExit(main(sys.argv[1]))
