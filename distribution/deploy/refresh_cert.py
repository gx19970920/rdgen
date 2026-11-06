"""Validate the existing certificate before refreshing the managed TLS chain."""
import os
from pathlib import Path
import subprocess
import tempfile

root = Path('/opt/rdgen-distribution')
source_dir = Path(os.environ['DIST_CERT_DIR'])
cert_name = os.environ['DIST_CERT_NAME']
key_name = os.environ['DIST_KEY_NAME']
if Path(cert_name).name != cert_name or Path(key_name).name != key_name:
    raise SystemExit('Certificate filenames must be basenames')
certificate = source_dir / cert_name
key = source_dir / key_name
chain = root / 'tls' / 'chain.pem'
destination = root / 'tls' / 'fullchain.pem'


def output(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


output('openssl', 'verify', '-purpose', 'sslserver', '-verify_hostname', os.environ['DIST_HOSTNAME'],
       '-untrusted', str(chain), str(certificate))
output('openssl', 'x509', '-in', str(certificate), '-noout', '-checkend', '86400')
cert_public = output('openssl', 'x509', '-in', str(certificate), '-pubkey', '-noout')
key_public = output('openssl', 'pkey', '-in', str(key), '-pubout')
if cert_public.strip() != key_public.strip():
    raise SystemExit('Certificate and key do not match')
with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
    temporary = Path(stream.name)
    try:
        stream.write(certificate.read_bytes().rstrip() + b'\n' + chain.read_bytes().strip() + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
os.chmod(temporary, 0o644)
os.replace(temporary, destination)
print('TLS certificate chain refreshed and verified.')
