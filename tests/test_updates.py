import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
import os
import subprocess
from unittest.mock import patch
from urllib.error import HTTPError

import rocket_update as update
import update_server


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.builds = self.base / 'builds'
        self.builds.mkdir()
        for role, filename in update.ROLES.items():
            (self.builds / filename).write_bytes(b'MZ' + role.encode() * 1000)
        self.feed = self.base / 'feed'
        self.manifest = update_server.publish('2.2.0', self.builds, self.feed, 'test release')
        self.server = update_server.server(self.feed, 0, '127.0.0.1')
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.directory.cleanup()

    def test_checks_versions_and_downloads_correct_role(self):
        for role in update.ROLES:
            release = update.check_release(self.url, role, '2.1.0')
            target = self.base / (role + '.exe')
            update.download_release(release, target)
            self.assertEqual(target.read_bytes(), (self.builds / update.ROLES[role]).read_bytes())
        self.assertIsNone(update.check_release(self.url, 'sender', '2.2.0'))
        self.assertIsNone(update.check_release(self.url, 'sender', '3.0.0'))

    def test_corrupt_package_cannot_replace_existing_download(self):
        release = update.check_release(self.url, 'sender', '2.1.0')
        (self.feed / release['file']).write_bytes(b'MZcorrupted')
        target = self.base / 'target.exe'
        target.write_bytes(b'old version')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            update.download_release(release, target)
        self.assertEqual(target.read_bytes(), b'old version')
        self.assertFalse(target.with_suffix('.part').exists())

    def test_server_does_not_expose_other_files_or_listings(self):
        (self.feed / 'secret.txt').write_text('private')
        for path in ['/', '/secret.txt', '/../app_version.py']:
            with self.subTest(path=path), self.assertRaises(HTTPError):
                update.open_url(self.url + path)

    def test_rejects_traversal_and_malformed_manifest(self):
        self.manifest['artifacts']['sender']['file'] = '../private.exe'
        (self.feed / 'latest.json').write_text(json.dumps(self.manifest))
        with self.assertRaises(ValueError):
            update.check_release(self.url, 'sender', '2.1.0')

    def test_config_and_literal_installer_paths(self):
        with patch.object(update, 'CONFIG', self.base / 'config.json'):
            update.save_source(self.url)
            self.assertEqual(update.load_source(), self.url)
        script = update.replacement_script("C:/A B/O'Brien.exe", 'C:/new.exe', 123, True)
        self.assertIn("O''Brien.exe", script)
        self.assertIn('Wait-Process -Timeout 120', script)
        self.assertIn('--resume-listening', script)
        self.assertIn('.previous', script)
        self.assertNotIn('Invoke-Expression', script)

    def test_busy_transfer_prevents_replacement(self):
        class App:
            is_sending = False
            active_clients = 1
        controller = object.__new__(update.UpdateController)
        controller.app = App()
        self.assertTrue(controller.busy())
        controller.app.active_clients = 0
        self.assertFalse(controller.busy())
        controller.app.is_sending = True
        self.assertTrue(controller.busy())

    def test_install_blocks_new_receiver_connections_before_launch(self):
        class App:
            active_clients = 0
            is_listening = True
            resume_lock = threading.Lock()
            def on_close(self):
                self.closed = True
        controller = object.__new__(update.UpdateController)
        controller.app = App()
        controller.role = 'receiver'
        def launched(_destination, listener):
            self.assertTrue(controller.app._updating)
            self.assertTrue(listener)
            self.assertTrue(controller.app.resume_lock.acquire(blocking=False))
            controller.app.resume_lock.release()
        with patch.object(update, 'launch_replacement', side_effect=launched):
            controller.install(self.base / 'download.exe')
        self.assertTrue(controller.app.closed)

    @unittest.skipUnless(os.name == 'nt', 'Windows installer')
    def test_powershell_installer_replaces_and_restores_on_restart_failure(self):
        for fail_restart in (False, True):
            with self.subTest(fail_restart=fail_restart):
                directory = self.base / ("O'Brien [test] " + str(fail_restart))
                directory.mkdir()
                target, incoming = directory / 'renamed.exe', directory / 'incoming.exe'
                target.write_bytes(b'MZold')
                incoming.write_bytes(b'MZnew')
                marker = directory / 'restarted.txt'
                mock_launch = '''function Start-UpdatedApplication {
if (FAIL_RESTART -and [IO.File]::ReadAllText($target) -eq 'MZnew') { throw 'restart test failure' }
[IO.File]::WriteAllText(MARKER, '--resume-listening')
}
'''.replace('FAIL_RESTART', '$true' if fail_restart else '$false').replace('MARKER', update.ps_literal(marker))
                script = directory / 'replace.ps1'
                source = update.replacement_script(target, incoming, 2147483646, True)
                source = source.replace('# A restarted one-file', mock_launch + '# A restarted one-file')
                script.write_text(source, encoding='utf-8-sig')
                result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)], capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                log_path = Path(str(target) + '.update.log')
                log = log_path.read_text(encoding='utf-8-sig') if log_path.exists() else ''
                self.assertEqual(target.read_bytes(), b'MZold' if fail_restart else b'MZnew', log)
                if not fail_restart:
                    self.assertFalse(Path(str(target) + '.previous').exists())
                    self.assertFalse(log_path.exists())
                else:
                    # 失败时备份已被移回原位，不应残留 .previous；日志保留供排查
                    self.assertFalse(Path(str(target) + '.previous').exists())
                    self.assertTrue(log_path.exists())
                self.assertEqual(marker.read_text(), '--resume-listening')
                self.assertEqual(incoming.exists(), fail_restart)

    @unittest.skipUnless(os.name == 'nt', 'Windows executable sharing')
    def test_installer_can_replace_file_held_open_without_write_sharing(self):
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        target, incoming = self.base / 'busy.exe', self.base / 'incoming.exe'
        target.write_bytes(b'MZold')
        incoming.write_bytes(b'MZnew')
        handle = kernel.CreateFileW(str(target), 0x80000000, 1 | 4, None, 3, 0, None)
        self.assertNotEqual(handle, w.HANDLE(-1).value)
        try:
            # Reproduce Windows error 32 from the original overwrite strategy.
            with self.assertRaises(PermissionError):
                target.write_bytes(b'overwrite')
            mock_launch = 'function Start-UpdatedApplication { }\n'
            script = self.base / 'replace.ps1'
            source = update.replacement_script(target, incoming, 2147483646)
            script.write_text(source.replace('# A restarted one-file', mock_launch + '# A restarted one-file'), encoding='utf-8-sig')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)], capture_output=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertEqual(target.read_bytes(), b'MZnew')
            self.assertFalse(Path(str(target) + '.previous').exists())
            self.assertFalse(Path(str(target) + '.update.log').exists())
        finally:
            kernel.CloseHandle(handle)

    def test_installer_waits_for_bootloader_and_resets_unpack_environment(self):
        script = update.replacement_script('C:/app.exe', 'C:/new.exe', 123, bootloader_pid=456)
        self.assertIn('@(123, 456)', script)
        self.assertIn("$env:PYINSTALLER_RESET_ENVIRONMENT = '1'", script)


if __name__ == '__main__':
    unittest.main()
