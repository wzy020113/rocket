"""Publisher commands available from either packaged executable."""
import argparse
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.parse

from rocket_update import ROLES, version_tuple


def publish(version, builds, output, notes=''):
    version_tuple(version)
    builds, output = Path(builds), Path(output)
    sources = {role: builds / name for role, name in ROLES.items()}
    for path in sources.values():
        if not path.is_file():
            raise FileNotFoundError(f'缺少打包程序：{path}')
    output.mkdir(parents=True, exist_ok=True)
    manifest = {'version': version, 'notes': notes, 'artifacts': {}}
    for role, source in sources.items():
        with source.open('rb') as source_file:
            digest = hashlib.file_digest(source_file, 'sha256').hexdigest()
        filename = f'{role}-{version}-{digest[:12]}.exe'
        target = output / filename
        if not target.exists():
            partial_path = target.with_suffix('.tmp')
            shutil.copyfile(source, partial_path)
            os.replace(partial_path, target)
        manifest['artifacts'][role] = {'file': filename, 'sha256': digest, 'size': source.stat().st_size}
    temp = output / 'latest.json.tmp'
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, output / 'latest.json')
    return manifest


class ReleaseHandler(SimpleHTTPRequestHandler):
    def allowed_path(self):
        name = urllib.parse.urlsplit(self.path).path.lstrip('/')
        # Do not expose unrelated files or directory listings on the publisher PC.
        import re
        if name != 'latest.json' and not re.fullmatch(r'(sender|receiver)-\d+\.\d+\.\d+-[a-f0-9]{12}\.exe', name):
            self.send_error(404)
            return False
        return True

    def do_GET(self):
        if self.allowed_path():
            super().do_GET()

    def do_HEAD(self):
        if self.allowed_path():
            super().do_HEAD()

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def log_message(self, *_args):
        pass


def server(directory, port=18765, host='0.0.0.0'):
    return ThreadingHTTPServer((host, port), partial(ReleaseHandler, directory=str(Path(directory).resolve())))


def handle_command_line():
    if '--serve-updates' not in sys.argv and '--publish-update' not in sys.argv:
        return False
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve-updates')
    parser.add_argument('--publish-update')
    parser.add_argument('--builds')
    parser.add_argument('--output')
    parser.add_argument('--notes', default='')
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--stop-file')
    args = parser.parse_args()
    if args.publish_update:
        publish(args.publish_update, args.builds, args.output, args.notes)
    else:
        directory = Path(args.serve_updates)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            with server(directory, args.port) as httpd:
                if args.stop_file:
                    # Local publisher control only; never exposed through HTTP.
                    stop_file = Path(args.stop_file)
                    httpd.timeout = 0.5
                    while not stop_file.exists():
                        httpd.handle_request()
                    stop_file.unlink(missing_ok=True)
                else:
                    httpd.serve_forever()
        except OSError as exc:
            (directory / 'server-error.log').write_text(str(exc), encoding='utf-8')
            raise
    return True


if __name__ == '__main__':
    handle_command_line()
