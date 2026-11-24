"""Manually run after packaging; verifies the actual frozen publisher and server."""
import hashlib
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_version import VERSION
from rocket_update import ROLES, check_release, download_release


def main():
    builds = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    with tempfile.TemporaryDirectory(prefix='rocket-packaged-smoke-') as directory:
        base = Path(directory)
        feed = base / 'feed'
        result = subprocess.run([str(builds / ROLES['receiver']), '--publish-update', VERSION,
                                 '--builds', str(builds), '--output', str(feed)],
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=45)
        assert result.returncode == 0 and (feed / 'latest.json').is_file(), 'Frozen publish failed'
        with socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            port = available.getsockname()[1]
        stop_file = base / 'stop-server'
        process = subprocess.Popen([str(builds / ROLES['sender']), '--serve-updates', str(feed), '--port', str(port), '--stop-file', str(stop_file)],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            source = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 30
            while True:
                try:
                    release = check_release(source, 'sender', '0.0.0')
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.3)
            for role, filename in ROLES.items():
                release = check_release(source, role, '0.0.0')
                assert release['version'] == VERSION
                target = base / (role + '.exe')
                download_release(release, target)
                with (builds / filename).open('rb') as original:
                    assert hashlib.file_digest(original, 'sha256').hexdigest() == release['sha256']
                assert check_release(source, role, VERSION) is None
                print(role + ': frozen publish, HTTP download, SHA256 and version check OK')
        finally:
            if process.poll() is None:
                stop_file.write_text('stop')
            process.wait(timeout=20)


if __name__ == '__main__':
    main()
