import hashlib
import socket
import tempfile
import threading
import time
import unittest
import io
import tarfile
import struct
import send
from unittest.mock import patch
from pathlib import Path

import receiver
import xrt_proto


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return str(self.value)


class _Root:
    def after(self, _delay, callback, *args):
        callback(*args)


class _Harness:
    _handle_client = receiver.ReceiverApp._handle_client
    _buffered_socket = receiver.ReceiverApp._buffered_socket
    _receive_resumable_file = receiver.ReceiverApp._receive_resumable_file
    _receive_file = receiver.ReceiverApp._receive_file
    _receive_folder = receiver.ReceiverApp._receive_folder

    def _start_progress(self, *_args):
        pass

    def _progress_threadsafe(self, *_args, **_kwargs):
        pass

    def _mark_complete(self, *_args):
        pass

    def _mark_failed(self, *_args):
        pass

    def _log_threadsafe(self, *_args):
        pass

    def _add_history(self, *_args):
        pass


class _Sender:
    _send_worker = send.App._send_worker
    _payload_chunks = send.App._payload_chunks
    _tune_socket = send.App._tune_socket
    _send_header = send.App._send_header
    _send_legacy_transfer = send.App._send_legacy_transfer
    _legacy_payload = send.App._legacy_payload

    def _ui_update(self, *_args, **_kwargs):
        pass

    def _set_status(self, *_args):
        pass

    def _log(self, *_args):
        pass

    def _done(self, ok, message):
        self.result = ok, message


class ResumeTransferTest(unittest.TestCase):
    def run_transfer(self, mode, payload, token='', legacy=False, split=None):
        with tempfile.TemporaryDirectory() as folder:
            harness = _Harness()
            harness.root = _Root()
            harness.listen_token = token
            harness.resume_lock = threading.Lock()
            harness.resume_active = set()
            harness.save_dir_var = _Value(folder)
            control = socket.socket()
            control.bind(('127.0.0.1', 0))
            control.listen(2)
            harness.port_var = _Value(control.getsockname()[1])
            digest = hashlib.sha256(payload).hexdigest()
            name = '测试目录' if mode in (receiver.MODE_FOLDER, xrt_proto.MODE_STREAM_FOLDER, xrt_proto.MODE_LAZY_FOLDER) else 'sample.bin'
            def receive_once():
                conn, address = control.accept()
                conn.settimeout(3)
                harness._handle_client(conn, address)
            try:
                offsets = [0] if split is None else [0, split]
                for attempt, expected_offset in enumerate(offsets):
                    worker = threading.Thread(target=receive_once, daemon=True)
                    worker.start()
                    with socket.create_connection(control.getsockname(), timeout=3) as connection:
                        if legacy:
                            send.authenticate_socket(connection, token)
                            encoded = name.encode('utf-8')
                            connection.sendall(struct.pack('>BI', mode, len(encoded)) + encoded + struct.pack('>Q', len(payload)))
                            connection.sendall(payload)
                        else:
                            xrt_proto.send_negotiate_request(connection, mode, name, (1 << 63) - 1 if mode == xrt_proto.MODE_LAZY_FOLDER else len(payload), 0, token, digest)
                            _port, offset = xrt_proto.read_ack(connection)
                            self.assertEqual(offset, expected_offset)
                            end = split if split is not None and attempt == 0 else len(payload)
                            if end > offset:
                                xrt_proto.send_data_frame(connection, offset, payload[offset:end])
                                if mode in (xrt_proto.MODE_STREAM_FILE, xrt_proto.MODE_STREAM_FOLDER, xrt_proto.MODE_LAZY_FOLDER):
                                    connection.sendall(hashlib.sha256(payload[offset:end]).digest())
                            if end == len(payload):
                                if mode == xrt_proto.MODE_LAZY_FOLDER:
                                    xrt_proto.send_data_frame(connection,end,b'')
                                    connection.sendall(hashlib.sha256(b'').digest())
                                self.assertEqual(xrt_proto.recv_exact(connection, 4), xrt_proto.COMPLETE_MAGIC)
                        connection.shutdown(socket.SHUT_WR)
                    worker.join(4)
                    self.assertFalse(worker.is_alive())
                if mode in (receiver.MODE_FILE, xrt_proto.MODE_STREAM_FILE):
                    received = list(Path(folder).glob('sample_*.bin'))
                    self.assertEqual(len(received), 1)
                    self.assertEqual(received[0].read_bytes(), payload)
                else:
                    received = list(Path(folder).rglob('内容.txt'))
                    self.assertEqual(len(received), 1)
                    self.assertEqual(received[0].read_bytes(), '文件夹内容'.encode())
            finally:
                control.close()

    def test_stream_folder_resumes_inside_file_bytes(self):
        content = '文件夹内容'.encode()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            entry = tarfile.TarInfo('测试目录/内容.txt')
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        self.run_transfer(xrt_proto.MODE_STREAM_FOLDER, stream.getvalue(), 'secret', split=515)
        self.run_transfer(xrt_proto.MODE_LAZY_FOLDER, stream.getvalue(), 'secret', split=515)

    def test_sender_worker_file_folder_and_legacy(self):
        for is_dir, resume in [(False, True), (True, True), (False, False), (True, False)]:
            with self.subTest(is_dir=is_dir, resume=resume), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                destination = base / 'destination'
                destination.mkdir()
                source = base / '测试目录'
                source.mkdir()
                content = '文件夹内容'.encode()
                source_file = source / '内容.txt'
                source_file.write_bytes(content)
                harness = _Harness()
                harness.root = _Root()
                harness.listen_token = 'secret'
                harness.resume_lock = threading.Lock()
                harness.resume_active = set()
                harness.save_dir_var = _Value(destination)
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1', 0))
                    listener.listen()
                    listener.settimeout(4)
                    port = listener.getsockname()[1]
                    harness.port_var = _Value(port)
                    def accept_requests():
                        for _ in range(2):
                            connection, addr = listener.accept()
                            connection.settimeout(3)
                            harness._handle_client(connection, addr)
                    worker = threading.Thread(target=accept_requests, daemon=True)
                    worker.start()
                    sender = _Sender()
                    sender.root = _Root()
                    sender.cancel_event = threading.Event()
                    sender.active_socket = None
                    sender.transfer_resume = resume
                    sender.transfer_auto_test = True
                    sender.result = None
                    sender._send_worker(str(source if is_dir else source_file), {'ip': '127.0.0.1', 'port': port, 'token': 'secret'})
                    self.assertIsNotNone(sender.result)
                    self.assertTrue(sender.result[0], sender.result)
                    worker.join(4)
                    self.assertFalse(worker.is_alive())
                    saved = list(destination.rglob('内容.txt')) if is_dir else list(destination.glob('内容_*.txt'))
                    self.assertEqual(len(saved), 1)
                    self.assertEqual(saved[0].read_bytes(), content)

    def test_sender_automatically_reconnects_after_interruption(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            source=base/'source.bin'
            payload=bytes(range(256))*128
            source.write_bytes(payload)
            destination=base/'received'
            destination.mkdir()
            harness=_Harness()
            harness.root=_Root()
            harness.listen_token='secret'
            harness.resume_lock=threading.Lock()
            harness.resume_active=set()
            harness.save_dir_var=_Value(destination)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0))
                listener.listen()
                listener.settimeout(8)
                port=listener.getsockname()[1]
                harness.port_var=_Value(port)
                def receive_requests():
                    for _ in range(2):
                        connection, addr=listener.accept()
                        connection.settimeout(5)
                        harness._handle_client(connection,addr)
                worker=threading.Thread(target=receive_requests,daemon=True)
                worker.start()
                sender=_Sender()
                sender.root=_Root()
                sender.cancel_event=threading.Event()
                sender.transfer_resume=True
                sender.transfer_auto_test=False
                sender.active_socket=None
                attempts=[]
                def chunks(path,offset,total):
                    attempts.append(offset)
                    if len(attempts)==1:
                        yield payload[:4096]
                        raise ConnectionResetError('simulated interruption')
                    yield payload[offset:]
                sender._payload_chunks=chunks
                sender._send_worker(str(source),{'ip':'127.0.0.1','port':port,'token':'secret'})
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertTrue(sender.result[0],sender.result)
                self.assertEqual(attempts,[0,4096])
                saved=list(destination.glob('source_*.bin'))
                self.assertEqual(len(saved),1)
                self.assertEqual(saved[0].read_bytes(),payload)

    def test_task_waits_past_five_disconnects_and_recovers_automatically(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.bin'
            source.write_bytes(b'keep the same task')
            sender=_Sender()
            sender.root=_Root()
            sender.cancel_event=threading.Event()
            sender.transfer_resume=True
            sender.transfer_auto_test=True
            sender.active_socket=None
            sender.result=None
            waits=[]
            real_connect=send.socket.create_connection
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0))
                listener.listen()
                listener.settimeout(3)
                port=listener.getsockname()[1]
                harness=_Harness()
                harness.root=_Root()
                harness.listen_token=''
                harness.resume_lock=threading.Lock()
                harness.resume_active=set()
                destination=Path(folder)/'destination'
                harness.save_dir_var=_Value(destination)
                harness.port_var=_Value(port)
                def receive():
                    connection,addr=listener.accept()
                    connection.settimeout(3)
                    harness._handle_client(connection,addr)
                worker=threading.Thread(target=receive,daemon=True)
                worker.start()
                attempts=[0]
                def unavailable_then_restored(*args,**kwargs):
                    attempts[0]+=1
                    if attempts[0] <= 8:
                        raise ConnectionRefusedError('recipient temporarily offline')
                    return real_connect(*args,**kwargs)
                with patch.object(send,'probe_receiver',return_value=(False,'timed out')), patch.object(send.socket,'create_connection',side_effect=unavailable_then_restored), patch.object(sender.cancel_event,'wait',side_effect=lambda duration: waits.append(duration) or False):
                    sender._send_worker(str(source),{'ip':'127.0.0.1','port':port})
                worker.join(4)
                self.assertFalse(worker.is_alive())
                self.assertTrue(sender.result[0],sender.result)
                self.assertEqual(attempts[0],9)
                self.assertEqual(len(waits),8)
                self.assertEqual(next(destination.glob('source_*.bin')).read_bytes(),source.read_bytes())

    def test_waiting_task_cancels_without_manual_reconnect(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.bin'
            source.write_bytes(b'data')
            sender=_Sender()
            sender.root=_Root()
            sender.cancel_event=threading.Event()
            sender.transfer_resume=True
            sender.transfer_auto_test=False
            sender.active_socket=None
            def cancel_on_wait(_seconds):
                sender.cancel_event.set()
                return True
            with patch.object(send.socket,'create_connection',side_effect=ConnectionRefusedError('offline')), patch.object(sender.cancel_event,'wait',side_effect=cancel_on_wait):
                sender._send_worker(str(source),{'ip':'127.0.0.1'})
            self.assertFalse(sender.result[0])
            self.assertIn('取消',sender.result[1])

    def test_empty_file(self):
        self.run_transfer(receiver.MODE_FILE, b'')

    def test_file_without_password(self):
        self.run_transfer(receiver.MODE_FILE, bytes(range(256)) * 100)

    def test_legacy_file_with_and_without_password(self):
        for token in ('', 'secret'):
            with self.subTest(token=token):
                self.run_transfer(receiver.MODE_FILE, b'legacy file', token=token, legacy=True)

    def test_folder_and_interrupted_folder(self):
        stream = io.BytesIO()
        content = '文件夹内容'.encode()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            entry = tarfile.TarInfo('测试目录/内容.txt')
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        for token, legacy, split in [('', False, None), ('secret', False, 512), ('secret', True, None)]:
            with self.subTest(token=token, legacy=legacy, split=split):
                self.run_transfer(receiver.MODE_FOLDER, stream.getvalue(), token, legacy, split)

    def test_interrupted_authenticated_file_resumes_from_saved_offset(self):
        payload = bytes(range(256)) * 32
        split = 4096
        digest = hashlib.sha256(payload).hexdigest()

        with tempfile.TemporaryDirectory() as folder:
            harness = _Harness()
            harness.root = _Root()
            harness.listen_token = "transfer-secret"
            harness.resume_lock = threading.Lock()
            harness.resume_active = set()
            harness.save_dir_var = _Value(folder)
            harness.port_var = _Value(9999)

            control = socket.socket()
            control.bind(("127.0.0.1", 0))
            control.listen(2)
            harness.port_var = _Value(control.getsockname()[1])

            def receive_once():
                conn, address = control.accept()
                conn.settimeout(2)
                harness._handle_client(conn, address)

            first_receiver = threading.Thread(target=receive_once)
            first_receiver.start()
            first = socket.create_connection(control.getsockname())
            xrt_proto.send_negotiate_request(first, receiver.MODE_FILE, "sample.bin", len(payload), 0, harness.listen_token, digest)
            _port, offset = xrt_proto.read_ack(first)
            self.assertEqual(offset, 0)
            xrt_proto.send_data_frame(first, 0, payload[:split])
            first.shutdown(socket.SHUT_RDWR)
            first.close()
            first_receiver.join(timeout=5)
            self.assertFalse(first_receiver.is_alive())

            second_receiver = threading.Thread(target=receive_once)
            second_receiver.start()
            second = socket.create_connection(control.getsockname())
            xrt_proto.send_negotiate_request(second, receiver.MODE_FILE, "sample.bin", len(payload), 0, harness.listen_token, digest)
            _port, offset = xrt_proto.read_ack(second)
            self.assertEqual(offset, split)
            xrt_proto.send_data_frame(second, split, payload[split:])
            self.assertEqual(xrt_proto.recv_exact(second, 4), xrt_proto.COMPLETE_MAGIC)
            second.shutdown(socket.SHUT_WR)
            second.close()
            second_receiver.join(timeout=5)
            self.assertFalse(second_receiver.is_alive())
            control.close()

            received = list(Path(folder).glob("sample_*.bin"))
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
