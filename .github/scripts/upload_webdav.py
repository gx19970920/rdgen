#!/usr/bin/env python3
"""Upload build outputs to WebDAV using only the Python standard library."""

from __future__ import annotations

import argparse
import base64
import http.client
import mimetypes
import os
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


SUCCESS_STATUSES = {200, 201, 204}
COLLECTION_EXISTS_STATUSES = SUCCESS_STATUSES | {301, 405}


class WebDAVClient:
    def __init__(self, base_url: str, username: str, password: str, directory: str):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            raise ValueError('WEBDAV_URL must include http:// or https://')
        self.parsed = parsed
        # Large APK/DMG uploads over slow home uplinks can legitimately block
        # on a socket write for several minutes. Keep this configurable while
        # using a safe default for CI.
        self.timeout = int(os.environ.get('WEBDAV_TIMEOUT', '900'))
        self.auth = 'Basic ' + base64.b64encode(
            f'{username}:{password}'.encode('utf-8')
        ).decode('ascii')
        raw_path = '/'.join(
            part.strip('/')
            for part in (parsed.path, directory)
            if part.strip('/')
        )
        self.directory = self._encode_path('/' + raw_path if raw_path else '/')

    @staticmethod
    def _encode_path(path: str) -> str:
        return '/' + '/'.join(
            quote(unquote(segment), safe='')
            for segment in path.strip('/').split('/')
            if segment
        )

    def _connection(self):
        port = self.parsed.port
        if self.parsed.scheme == 'https':
            return http.client.HTTPSConnection(
                self.parsed.hostname,
                port or 443,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(
            self.parsed.hostname,
            port or 80,
            timeout=self.timeout,
        )

    def _request(self, method: str, path: str) -> int:
        connection = self._connection()
        try:
            connection.request(
                method,
                path,
                headers={
                    'Authorization': self.auth,
                    'User-Agent': 'rdgen-webdav-uploader/1.0',
                },
            )
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def ensure_directory(self) -> None:
        segments = [segment for segment in self.directory.split('/') if segment]
        current = ''
        for segment in segments:
            current += '/' + segment
            status = self._request('MKCOL', current)
            if status not in COLLECTION_EXISTS_STATUSES:
                raise RuntimeError(f'WebDAV MKCOL {current} failed with HTTP {status}')

    def upload(self, local_path: Path, remote_name: str) -> None:
        if Path(remote_name).name != remote_name:
            raise ValueError(f'Invalid remote filename: {remote_name!r}')
        remote_path = f"{self.directory.rstrip('/')}/{quote(remote_name, safe='')}"
        content_type = mimetypes.guess_type(remote_name)[0] or 'application/octet-stream'
        last_error = None
        for attempt in range(1, 4):
            connection = self._connection()
            try:
                connection.putrequest('PUT', remote_path)
                connection.putheader('Authorization', self.auth)
                connection.putheader('User-Agent', 'rdgen-webdav-uploader/1.0')
                connection.putheader('Content-Type', content_type)
                connection.putheader('Content-Length', str(local_path.stat().st_size))
                connection.endheaders()
                with local_path.open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        connection.send(chunk)
                response = connection.getresponse()
                response.read()
                if response.status not in SUCCESS_STATUSES:
                    raise RuntimeError(
                        f'WebDAV PUT {remote_name} failed with HTTP {response.status}'
                    )
                print(f'Uploaded {local_path} -> {remote_name}')
                return
            except Exception as exc:  # retry transient network/server failures
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** attempt)
            finally:
                connection.close()
        raise RuntimeError(f'Unable to upload {local_path}: {last_error}')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--upload',
        action='append',
        nargs=2,
        metavar=('LOCAL_PATH', 'REMOTE_NAME'),
        required=True,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = WebDAVClient(
        os.environ['WEBDAV_URL'],
        os.environ['WEBDAV_USER'],
        os.environ['WEBDAV_PASS'],
        os.environ.get('WEBDAV_DIR', ''),
    )
    existing_uploads = [
        (Path(local_path), remote_name)
        for local_path, remote_name in args.upload
        if Path(local_path).is_file()
    ]
    missing = [
        local_path
        for local_path, _ in args.upload
        if not Path(local_path).is_file()
    ]
    for local_path in missing:
        print(f'::warning::Skipping missing WebDAV upload file: {local_path}')
    if not existing_uploads:
        raise FileNotFoundError('None of the requested WebDAV upload files exist')

    client.ensure_directory()
    for local_path, remote_name in existing_uploads:
        client.upload(local_path, remote_name)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'::error::{exc}', file=sys.stderr)
        raise
