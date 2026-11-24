import hashlib
import io
from pathlib import Path
import socket
import tarfile
import tempfile
import threading
import unittest

from direct_folder import DirectFolder
from receiver import safe_member_target
from stream_payload import StreamPayload, UNKNOWN_TOTAL
from test_resume_transfer import _Harness, _Root, _Value
import xrt_proto


class DirectFolderTests(unittest.TestCase):
    def archive(self):
        content = bytes(range(256)) * 91
        path = '目录/' + '很长的名称' * 28 + '/内容.bin'
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as archive:
            for name, data in [('目录/空文件', b''), (path, content), ('目录/end.txt', b'END')]:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return buffer.getvalue(), path, content

    def test_restart_at_headers_file_body_padding_and_trailer(self):
        encoded, path, content = self.archive()
        for split in (1, 511, 512, 513, 1023, 1537, 3073, 8191, 19000, len(encoded) - 1):
            with self.subTest(split=split), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                cache = root / '.xiaorocket-resume'
                cache.mkdir()
                state = cache / ('a' * 64 + '.json')
                sink = DirectFolder(state, root / 'destination', safe_member_target)
                sink.feed(encoded[:split])
                sink.checkpoint()
                sink.close()
                sink = DirectFolder(state, root / 'unused', safe_member_target)
                self.assertEqual(sink.offset, split)
                for offset in range(split, len(encoded), 337):
                    sink.feed(encoded[offset:offset + 337])
                sink.finish()
                sink.close()
                self.assertEqual((root / 'destination' / path).read_bytes(), content)
                self.assertEqual((root / 'destination/目录/end.txt').read_bytes(), b'END')
                self.assertEqual((root / 'destination/目录/空文件').stat().st_size, 0)
                self.assertLess(state.stat().st_size, 16384)
                self.assertEqual(len(list(cache.iterdir())), 1)

    def test_replay_uncommitted_bytes(self):
        encoded, path, content = self.archive()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / '.xiaorocket-resume'
            cache.mkdir()
            state = cache / ('b' * 64 + '.json')
            sink = DirectFolder(state, root / 'out', safe_member_target)
            sink.feed(encoded[:8192])
            sink.checkpoint()
            sink.feed(encoded[8192:17000])
            sink.close()
            sink = DirectFolder(state, root / 'unused', safe_member_target)
            self.assertEqual(sink.offset, 8192)
            sink.feed(encoded[sink.offset:])
            sink.finish()
            sink.close()
            self.assertEqual((root / 'out' / path).read_bytes(), content)

    def test_truncated_archive_and_unsafe_path_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / '.xiaorocket-resume'
            cache.mkdir()
            sink = DirectFolder(cache / 'state.json', root / 'out', safe_member_target)
            header = tarfile.TarInfo('../outside')
            with self.assertRaises(ValueError):
                sink.feed(header.tobuf())
            sink.close()
            with self.assertRaises(ValueError):
                sink.finish()
            self.assertFalse((root / 'outside').exists())

    def test_migrate_legacy_cache_and_resume_without_archive(self):
        encoded, path, content = self.archive()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / '.xiaorocket-resume'
            cache.mkdir()
            digest = hashlib.sha256(encoded).hexdigest()
            mode, name = xrt_proto.MODE_LAZY_FOLDER, '测试目录'
            identity = hashlib.sha256(f'{mode}\0{name}\0{UNKNOWN_TOTAL}\0{digest}'.encode()).hexdigest()
            split = 8191
            part = cache / (identity + '.part')
            part.write_bytes(encoded[:split])
            harness = _Harness()
            harness.root = _Root()
            harness.listen_token = ''
            harness.resume_lock = threading.Lock()
            harness.resume_active = set()
            harness.save_dir_var = _Value(folder)
            harness.port_var = _Value(9999)
            left, right = socket.socketpair()
            left.settimeout(5)
            right.settimeout(5)
            worker = threading.Thread(target=harness._handle_client, args=(right, ('127.0.0.1', 1)))
            worker.start()
            try:
                xrt_proto.send_negotiate_request(left, mode, name, UNKNOWN_TOTAL, 0, '', digest)
                _, offset = xrt_proto.read_ack(left)
                self.assertEqual(offset, split)
                self.assertFalse(part.exists())
                xrt_proto.send_data_frame(left, offset, encoded[offset:])
                left.sendall(hashlib.sha256(encoded[offset:]).digest())
                xrt_proto.send_data_frame(left, len(encoded), b'')
                left.sendall(hashlib.sha256(b'').digest())
                self.assertEqual(xrt_proto.recv_exact(left, 4), xrt_proto.COMPLETE_MAGIC)
            finally:
                left.close()
                worker.join(6)
                self.assertFalse(worker.is_alive())
            self.assertFalse(list(cache.glob('*.part')))
            self.assertFalse(list(cache.glob('*.tar')))
            received = list(root.rglob('内容.bin'))
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].read_bytes(), content)

    def test_real_payload_generator_compatibility(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / '发送目录'
            source.mkdir()
            (source / 'empty').mkdir()
            content = b'payload' * 50000
            (source / '文件.dat').write_bytes(content)
            payload = StreamPayload(source, threading.Event())
            cache = root / '.xiaorocket-resume'
            cache.mkdir()
            sink = DirectFolder(cache / 'test.json', root / 'received', safe_member_target)
            for chunk in payload.iter_chunks(0, payload.total, 8192):
                sink.feed(chunk)
            sink.finish()
            sink.close()
            self.assertEqual((root / 'received/发送目录/文件.dat').read_bytes(), content)
            self.assertTrue((root / 'received/发送目录/empty').is_dir())
