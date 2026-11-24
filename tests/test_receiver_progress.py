"""Exercise the real receiver UI callbacks, including during socket transfers."""
import io
import tarfile
import unittest
from unittest.mock import Mock, patch

import receiver
from stream_payload import UNKNOWN_TOTAL
import test_resume_transfer as transfers
import xrt_proto


def progress_app():
    app = object.__new__(receiver.ReceiverApp)
    app.root = transfers._Root()
    for name in ('pbar', 'current_label', 'lbl_source', 'lbl_recv',
                 'lbl_speed', 'lbl_elapsed', '_log'):
        setattr(app, name, Mock())
    return app


class ReceiverProgressTests(unittest.TestCase):
    def test_known_total_updates_all_statistics(self):
        app = progress_app()
        with patch.object(receiver.time, 'time', return_value=110):
            app._progress_threadsafe(5 * 1024**2, 20 * 1024**2, 100)
        app._log.assert_not_called()
        app.pbar.set.assert_called_once_with(25.0)
        app.pbar.set_busy.assert_not_called()
        app.lbl_recv.config.assert_called_once_with(text='5.0 MB / 20.0 MB')
        app.lbl_speed.config.assert_called_once_with(text='0.5 MB/s', fg=receiver.ACCENT_2)
        app.lbl_elapsed.config.assert_called_once_with(text='10s')

    def test_unknown_folder_total_shows_busy_and_received_bytes(self):
        app = progress_app()
        with patch.object(receiver.time, 'time', return_value=102):
            app._progress_threadsafe(4 * 1024**2, UNKNOWN_TOTAL, 100)
        app._log.assert_not_called()
        app.pbar.set_busy.assert_called_once_with('进行中')
        app.pbar.set.assert_not_called()
        app.lbl_recv.config.assert_called_once_with(text='4.0 MB')
        app.lbl_speed.config.assert_called_once_with(text='2.0 MB/s', fg=receiver.ACCENT_2)
        app.lbl_elapsed.config.assert_called_once_with(text='2s')

    def test_resume_speed_excludes_previously_received_bytes(self):
        app = progress_app()
        with patch.object(receiver.time, 'time', return_value=102):
            app._progress_threadsafe(12 * 1024**2, 16 * 1024**2, 100,
                                     initial_offset=8 * 1024**2)
        app._log.assert_not_called()
        app.pbar.set.assert_called_once_with(75.0)
        app.lbl_speed.config.assert_called_once_with(text='2.0 MB/s', fg=receiver.ACCENT_2)

    def test_empty_file_is_complete_without_dividing_by_zero(self):
        app = progress_app()
        with patch.object(receiver.time, 'time', return_value=100):
            app._progress_threadsafe(0, 0, 100)
        app._log.assert_not_called()
        app.pbar.set.assert_called_once_with(100.0)
        app.lbl_recv.config.assert_called_once_with(text='0 B / 0 B')

    def test_unknown_folder_total_switches_to_actual_size_at_end(self):
        app = progress_app()
        with patch.object(receiver.time, 'time', return_value=102):
            app._progress_threadsafe(1024, UNKNOWN_TOTAL, 100)
            app._progress_threadsafe(2048, 2048, 100)
        app._log.assert_not_called()
        app.pbar.set_busy.assert_called_once_with('进行中')
        app.pbar.set.assert_called_once_with(100.0)
        self.assertEqual(app.lbl_recv.config.call_args.kwargs['text'], '2.0 KB / 2.0 KB')

    def test_busy_widget_returns_to_determinate_on_completion(self):
        widget = Mock(_busy=False)
        receiver.ModernProgressBar.set_busy(widget)
        receiver.ModernProgressBar.set_busy(widget)
        widget.bar.start.assert_called_once()
        receiver.ModernProgressBar.set(widget, 100)
        widget.bar.stop.assert_called_once()
        self.assertFalse(widget._busy)
        widget.bar.configure.assert_called_with(mode='determinate')
        widget.bar.set.assert_called_once_with(1.0)
        widget.label.configure.assert_called_with(text='100.0%')

    def test_unpacking_preserves_received_statistics(self):
        app = progress_app()
        app._start_progress('正在解包：folder', '127.0.0.1', False)
        app.current_label.config.assert_called_once_with(text='正在解包：folder', fg=receiver.TEXT)
        app.lbl_source.config.assert_called_once_with(text='127.0.0.1')
        app.pbar.set.assert_not_called()
        app.lbl_recv.config.assert_not_called()
        app.lbl_speed.config.assert_not_called()
        app.lbl_elapsed.config.assert_not_called()

    def test_real_progress_callbacks_with_file_folder_legacy_and_resume(self):
        class ProgressHarness(transfers._Harness):
            _start_progress = receiver.ReceiverApp._start_progress
            _progress_threadsafe = receiver.ReceiverApp._progress_threadsafe

            def __init__(self):
                app = progress_app()
                self.__dict__.update(app.__dict__)

        archive = io.BytesIO()
        content = '文件夹内容'.encode()
        with tarfile.open(fileobj=archive, mode='w') as stream:
            entry = tarfile.TarInfo('测试目录/内容.txt')
            entry.size = len(content)
            stream.addfile(entry, io.BytesIO(content))

        scenarios = [
            (receiver.MODE_FILE, b'data' * 2048, False, None),
            (receiver.MODE_FILE, b'', False, None),
            (xrt_proto.MODE_STREAM_FILE, b'data' * 2048, False, 4096),
            (xrt_proto.MODE_STREAM_FOLDER, archive.getvalue(), False, 515),
            (xrt_proto.MODE_LAZY_FOLDER, archive.getvalue(), False, 515),
            (receiver.MODE_FILE, b'legacy', True, None),
            (receiver.MODE_FOLDER, archive.getvalue(), True, None),
        ]
        for mode, payload, legacy, split in scenarios:
            with self.subTest(mode=mode, legacy=legacy, split=split):
                instances = []

                def make_harness():
                    app = ProgressHarness()
                    instances.append(app)
                    return app

                with patch.object(transfers, '_Harness', side_effect=make_harness):
                    transfers.ResumeTransferTest().run_transfer(
                        mode, payload, token='secret', legacy=legacy, split=split)
                self.assertEqual(len(instances), 1)
                app = instances[0]
                app._log.assert_not_called()
                self.assertGreater(app.lbl_recv.config.call_count, 1)
                self.assertEqual(app.pbar.set.call_args.args, (100.0,))
                if mode == xrt_proto.MODE_LAZY_FOLDER:
                    app.pbar.set_busy.assert_called()


if __name__ == '__main__':
    unittest.main()
