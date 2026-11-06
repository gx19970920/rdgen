"""Atomic, bounded storage for the rdgen distribution service."""
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

PLATFORMS = {'android', 'windows-x64', 'windows-x86', 'windows-selfhosted', 'linux', 'macos'}
NAME = re.compile(r'^[A-Za-z0-9_-][A-Za-z0-9._-]{0,199}$')
RUN = re.compile(r'^[1-9][0-9]{0,24}-[1-9][0-9]{0,5}$')


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.active = {}

    @staticmethod
    def validate(platform, run, filename=None):
        if platform not in PLATFORMS or not RUN.fullmatch(run):
            raise ValueError('Invalid platform or run')
        if filename is not None and (not NAME.fullmatch(filename) or '..' in filename or filename.endswith('.part')):
            raise ValueError('Invalid filename')

    def stage(self, platform, run, filename, source, size):
        self.validate(platform, run, filename)
        if size <= 0 or size > 1024 * 1024 * 1024:
            raise ValueError('Invalid file size')
        key = (platform, run)
        with self.lock:
            folder = self.root / 'staging' / platform / run
            folder.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.upload-', suffix='.part', dir=folder)
            temporary = Path(name)
            dest = folder / filename
            self.active[key] = self.active.get(key, 0) + 1
        try:
            with os.fdopen(fd, 'wb') as out:
                remaining = size
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError('Incomplete upload')
                    out.write(block)
                    remaining -= len(block)
            with self.lock:
                if self._is_obsolete(platform, run):
                    raise ValueError('A newer build is already published')
                os.replace(temporary, dest)
        finally:
            with self.lock:
                temporary.unlink(missing_ok=True)
                self.active[key] -= 1
                if self.active[key] == 0:
                    del self.active[key]
                    if folder.exists() and self._is_obsolete(platform, run):
                        shutil.rmtree(folder)

    def _is_obsolete(self, platform, run):
        current = self.root / 'current' / (platform + '.json')
        if not current.exists():
            return False
        previous = json.loads(current.read_text(encoding='utf-8'))
        return tuple(map(int, previous['run'].split('-'))) > tuple(map(int, run.split('-')))

    def publish(self, platform, run):
        self.validate(platform, run)
        with self.lock:
            if self.active.get((platform, run), 0):
                raise ValueError('A file upload is still in progress')
            group = self.root / 'releases' / platform
            group.mkdir(parents=True, exist_ok=True)
            current = self.root / 'current' / (platform + '.json')
            current.parent.mkdir(parents=True, exist_ok=True)
            if current.exists():
                previous = json.loads(current.read_text(encoding='utf-8'))
                if previous['run'] == run:
                    self._cleanup(platform, run)
                    return previous['files']
                if tuple(map(int, previous['run'].split('-'))) > tuple(map(int, run.split('-'))):
                    raise ValueError('A newer build is already published')
            staged = self.root / 'staging' / platform / run
            if not staged.is_dir():
                raise ValueError('No staged files')
            for part in staged.iterdir():
                if part.name.endswith('.part') and part.is_file() and not part.is_symlink():
                    part.unlink()
            files = sorted(p.name for p in staged.iterdir() if p.is_file() and not p.name.endswith('.part'))
            if not files or any(not NAME.fullmatch(name) or '..' in name for name in files):
                raise ValueError('No valid files')
            release = group / run
            if release.exists():
                raise ValueError('Run already published')
            os.replace(staged, release)
            temp = current.with_suffix('.tmp')
            try:
                temp.write_text(json.dumps({'run': run, 'files': files}), encoding='utf-8')
                os.replace(temp, current)
            except OSError:
                temp.unlink(missing_ok=True)
                os.replace(release, staged)
                raise
            self._cleanup(platform, run)
            return files

    def _cleanup(self, platform, run):
        # Only validated directories inside this service-owned root are eligible.
        group = self.root / 'releases' / platform
        for old in group.iterdir():
            if old.name != run and old.is_dir() and RUN.fullmatch(old.name) and not old.is_symlink():
                if tuple(map(int, old.name.split('-'))) < tuple(map(int, run.split('-'))):
                    shutil.rmtree(old)
        stage_group = self.root / 'staging' / platform
        if stage_group.exists():
            for old in stage_group.iterdir():
                if old.is_dir() and not old.is_symlink() and RUN.fullmatch(old.name) and not self.active.get((platform, old.name), 0):
                    if tuple(map(int, old.name.split('-'))) < tuple(map(int, run.split('-'))):
                        shutil.rmtree(old)

    def resolve(self, platform, filename):
        self.validate(platform, '1-1', filename)
        with self.lock:
            manifest = json.loads((self.root / 'current' / (platform + '.json')).read_text(encoding='utf-8'))
            self.validate(platform, manifest['run'], filename)
            if filename not in manifest['files']:
                raise FileNotFoundError(filename)
            return self.root / 'releases' / platform / manifest['run'] / filename

    def open(self, platform, filename):
        # Hold the publication lock until the descriptor is open: a new
        # publication may delete the old directory immediately afterwards.
        with self.lock:
            return self.resolve(platform, filename).open('rb')

    def cleanup_stale(self, max_age_seconds=48 * 60 * 60):
        """Remove abandoned staging runs, never published or active files."""
        cutoff = time.time() - max_age_seconds
        with self.lock:
            root = self.root / 'staging'
            if not root.exists() or root.is_symlink():
                return
            for group in root.iterdir():
                if group.name not in PLATFORMS or group.is_symlink() or not group.is_dir():
                    continue
                for entry in group.iterdir():
                    if (entry.is_dir() and not entry.is_symlink() and RUN.fullmatch(entry.name)
                            and not self.active.get((group.name, entry.name), 0)
                            and entry.stat().st_mtime < cutoff):
                        shutil.rmtree(entry)
