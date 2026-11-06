"""Upload client files and atomically publish a run to a private server."""
import argparse
import http.client
import json
import os
from pathlib import Path
import ssl
import sys
import time
from urllib.parse import quote, urlsplit


def connection():
    url = urlsplit(os.environ['DIST_BASE_URL'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('DIST_BASE_URL must be an HTTPS origin without credentials')
    return http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=120, context=ssl.create_default_context()), url.path.rstrip('/')


def request_once(method, path, file=None):
    token = os.environ['DIST_UPLOAD_TOKEN']
    if '\r' in token or '\n' in token or not token:
        raise ValueError('Invalid upload token')
    conn, prefix = connection()
    try:
        conn.putrequest(method, prefix + path)
        conn.putheader('Authorization', 'Bearer ' + token)
        conn.putheader('Content-Length', str(file.stat().st_size if file else 0))
        conn.endheaders()
        if file:
            with file.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    conn.send(chunk)
        response = conn.getresponse()
        body = response.read(1024 * 1024)
        if response.status != 200:
            raise ValueError(f'Distribution server returned HTTP {response.status}')
        return json.loads(body)
    finally:
        conn.close()


def request(method, path, file=None):
    # Both staging and publishing are idempotent for the same run and file.
    # Retrying a dropped TLS connection therefore cannot replace a newer run.
    for attempt in range(5):
        try:
            return request_once(method, path, file)
        except (OSError, http.client.HTTPException):
            if attempt == 4:
                raise
            print('Distribution connection interrupted; retrying.', file=sys.stderr)
            time.sleep(2 ** (attempt + 1))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('stage', 'publish'))
    parser.add_argument('--platform', required=True)
    parser.add_argument('--file', action='append', default=[])
    parser.add_argument('--optional-file', action='append', default=[])
    args = parser.parse_args()
    # Each platform maps to exactly one workflow, so its run number is the
    # documented per-workflow sequence; attempts distinguish reruns.
    run = os.environ['GITHUB_RUN_NUMBER'] + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    if not args.platform.replace('-', '').isalnum() or not all(p.isdigit() for p in run.split('-')):
        raise ValueError('Invalid platform or run ID')
    prefix = '/' + quote(args.platform, safe='-') + '/' + run
    if args.operation == 'stage':
        files = [Path(p) for p in args.file]
        files += [Path(p) for p in args.optional_file if Path(p).is_file()]
        if not files or any(not path.is_file() or path.stat().st_size == 0 for path in files):
            raise ValueError('A required distribution file is missing or empty')
        for file in files:
            request('PUT', '/api/stage' + prefix + '/' + quote(file.name, safe=''), file)
        print(f'Staged {len(files)} distribution file(s).')
    else:
        result = request('POST', '/api/publish' + prefix)
        files = result['files']
        if not isinstance(files, list) or not files or any(not isinstance(f, str) for f in files):
            raise ValueError('Distribution server returned an invalid file list')
        output = os.environ.get('GITHUB_OUTPUT')
        if output:
            with open(output, 'a', encoding='utf-8') as stream:
                stream.write('files=' + json.dumps(files, ensure_ascii=True, separators=(',', ':')) + '\n')
        print(f'Published {len(files)} distribution file(s).')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, json.JSONDecodeError, http.client.HTTPException) as exc:
        print(f'Distribution operation failed: {type(exc).__name__}', file=sys.stderr)
        sys.exit(1)
