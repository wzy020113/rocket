import hashlib
import io
import socket
import tempfile
import threading
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stream_payload import StreamPayload, LazyFolderPayload, UNKNOWN_TOTAL, folder_size
import xrt_proto


class StreamPayloadTests(unittest.TestCase):
    def test_background_size_matches_actual_archive_without_content_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / '目录'
            root.mkdir()
            (root / 'empty').mkdir()
            (root / 'a.bin').write_bytes(b'a' * 513)
            (root / '长文件名.txt').write_bytes(b'b' * 27)
            actual = LazyFolderPayload(root, threading.Event())
            encoded = b''.join(actual.iter_chunks(0, UNKNOWN_TOTAL, 4096))
            with patch('builtins.open', side_effect=AssertionError('size scan must not read content')):
                raw, total = folder_size(root, threading.Event())
            self.assertEqual(raw, 540)
            self.assertEqual(total, len(encoded))

    def test_tar_stream_and_resume_at_headers_payload_and_padding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / '游戏目录'
            root.mkdir()
            (root / '空目录').mkdir()
            nested = root / '子目录'
            nested.mkdir()
            (nested / '存档.dat').write_bytes(bytes(range(256)) * 19)
            (root / 'empty.txt').touch()
            payload = StreamPayload(root, threading.Event())
            encoded = b''.join(payload.iter_chunks(0, payload.total, 4096))
            self.assertEqual(payload.total, len(encoded))
            with tarfile.open(fileobj=io.BytesIO(encoded)) as archive:
                self.assertEqual(archive.extractfile('游戏目录/子目录/存档.dat').read(), bytes(range(256)) * 19)
                self.assertTrue(archive.getmember('游戏目录/空目录').isdir())
                self.assertEqual(archive.extractfile('游戏目录/empty.txt').read(), b'')
            reopened = StreamPayload(root, threading.Event())
            self.assertEqual(payload.digest, reopened.digest)
            for offset in (0, 1, 511, 512, 1025, len(encoded) - 1, len(encoded)):
                with self.subTest(offset=offset):
                    self.assertEqual(b''.join(reopened.iter_chunks(offset, reopened.total, 513)), encoded[offset:])

    def test_preparation_does_not_open_or_read_any_source_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'large.bin'
            with open(source, 'wb') as file:
                file.truncate(8 * 1024 * 1024)
            (Path(temporary) / 'empty.txt').touch()
            with patch('builtins.open', side_effect=AssertionError('preparation must not open file content')):
                payload = StreamPayload(source, threading.Event())
                folder_payload = StreamPayload(Path(temporary), threading.Event())
            self.assertEqual(payload.total, source.stat().st_size)
            self.assertEqual(folder_payload.files, 2)
            self.assertGreater(folder_payload.total, payload.total)

    def test_lazy_folder_starts_before_any_directory_enumeration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'folder'
            root.mkdir()
            (root/'file.bin').write_bytes(b'payload')
            with patch('os.scandir',side_effect=AssertionError('must transmit before enumeration')):
                payload=LazyFolderPayload(root,threading.Event())
                iterator=payload.iter_chunks(0,UNKNOWN_TOTAL,1024)
                self.assertGreater(len(next(iterator)),0)
                self.assertEqual(payload.files,0)
                iterator.close()
            complete=LazyFolderPayload(root,threading.Event())
            encoded=b''.join(complete.iter_chunks(0,UNKNOWN_TOTAL,1024))
            self.assertTrue(complete.finished)
            with tarfile.open(fileobj=io.BytesIO(encoded)) as archive:
                self.assertEqual(archive.extractfile('folder/file.bin').read(),b'payload')
            for offset in [1,512,515,len(encoded)]:
                with self.subTest(offset=offset):
                    reopened=LazyFolderPayload(root,threading.Event())
                    self.assertEqual(b''.join(reopened.iter_chunks(offset,UNKNOWN_TOTAL,1024)),encoded[offset:])

    def test_changed_source_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source.bin'
            source.write_bytes(b'original')
            payload = StreamPayload(source, threading.Event())
            source.write_bytes(b'changed contents')
            with self.assertRaisesRegex(IOError, '源文件发生变化'):
                list(payload.iter_chunks(0, payload.total, 1024))

    def test_corrupt_chunk_is_rejected_before_it_is_committed(self):
        first, second = socket.socketpair()
        try:
            first.sendall(xrt_proto.DATA_MAGIC + xrt_proto.pack_u64(0) + xrt_proto.pack_u32(4) + b'data' + hashlib.sha256(b'FAIL').digest())
            with self.assertRaisesRegex(xrt_proto.ProtocolError, '数据块校验失败'):
                xrt_proto.FrameStream(second).read_frame(verify_digest=True)
        finally:
            first.close()
            second.close()


if __name__ == '__main__':
    unittest.main()
