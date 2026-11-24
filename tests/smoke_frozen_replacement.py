"""Run with the path to a compiled frozen_update_fixture.exe."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main(fixture):
    with tempfile.TemporaryDirectory(prefix='rocket-frozen-update-') as directory:
        base = Path(directory) / "中文 O'Brien [update test]"
        base.mkdir()
        target, incoming = base / 'renamed.exe', base / 'download.exe'
        shutil.copyfile(fixture, target)
        shutil.copyfile(fixture, incoming)
        expected = digest(incoming)
        process = subprocess.Popen([str(target), '--replace', str(incoming)], creationflags=subprocess.CREATE_NO_WINDOW)
        assert process.wait(timeout=30) == 0
        marker = target.with_suffix('.restarted.json')
        deadline = time.monotonic() + 45
        while True:
            try:
                after = json.loads(marker.read_text(encoding='utf-8'))
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    log = Path(str(target) + '.update.log')
                    raise AssertionError(log.read_text(encoding='utf-8-sig') if log.exists() else 'Updater never restarted')
                time.sleep(0.2)
        before = json.loads(target.with_suffix('.before.json').read_text(encoding='utf-8'))
        assert before['bootloader_pid'], 'Real bootloader parent was not recognized'
        assert before['unpack'] != after['unpack'], 'Restart reused the old extraction directory'
        assert after['unpack_exists'], 'Restart extraction directory is missing'
        assert digest(target) == expected
        assert not Path(str(target) + '.previous').exists(), 'Backup should be removed after a successful update'
        time.sleep(1)
        print('Real frozen executable: exit, bootloader wait, rename, backup and fresh restart OK')


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve())
