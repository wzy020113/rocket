"""A windowless test application for real PyInstaller self-replacement checks."""
import json
from pathlib import Path
import sys

from rocket_update import bootloader_process_id, launch_replacement


if __name__ == '__main__':
    executable = Path(sys.executable)
    details = {'unpack': sys._MEIPASS, 'unpack_exists': Path(sys._MEIPASS).is_dir(),
               'bootloader_pid': bootloader_process_id()}
    if '--replace' in sys.argv:
        executable.with_suffix('.before.json').write_text(json.dumps(details), encoding='utf-8')
        launch_replacement(Path(sys.argv[2]))
    else:
        executable.with_suffix('.restarted.json').write_text(json.dumps(details), encoding='utf-8')
