"""Probe the local application using its configured public Host header."""
import http.client
import os
from urllib.parse import urlsplit


def main():
    host = urlsplit(os.environ.get('GENURL', '')).netloc or '127.0.0.1'
    connection = http.client.HTTPConnection('127.0.0.1', 8000, timeout=4)
    try:
        connection.request('GET', '/', headers={
            'Host': host,
            'X-Forwarded-Proto': os.environ.get('PROTOCOL', 'https'),
        })
        response = connection.getresponse()
        return 0 if response.status == 200 else 1
    except (OSError, http.client.HTTPException):
        return 1
    finally:
        connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
