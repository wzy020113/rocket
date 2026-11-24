"""Incremental tar receiver with small durable state, never an archive-sized cache."""
import base64
import io
import json
import os
from pathlib import Path
import tarfile


class DirectFolder:
    HEADER_LIMIT = 1024 * 1024

    def __init__(self, state_path, destination, safe_target):
        self.state_path = Path(state_path)
        self.safe_target = safe_target
        self.file = None
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
            if state.get('version') != 1:
                raise ValueError('不支持的文件夹断点版本')
            # Only reuse a direct child of the configured save directory.
            saved_name = state['destination']
            if Path(saved_name).name != saved_name or saved_name in ('', '.', '..'):
                raise ValueError('文件夹断点路径无效')
            self.destination = self.state_path.parent.parent / saved_name
            self.offset = state['offset']
            self.pending = bytearray(base64.b64decode(state['header'], validate=True))
            self.scan = state['scan']
            self.need = state['need']
            self.extension_body = state['extension_body']
            self.current = state['current']
            self.ended = state['ended']
            self.completed = state.get('completed', False)
            if self.current and self.current['kind'] == 'file':
                target = self.safe_target(self.destination, self.current['name'])
                if not target.is_file() or target.stat().st_size < self.current['written']:
                    raise IOError('未完成文件已被移动或截断，请恢复文件后继续')
        else:
            self.destination = Path(destination)
            self.offset = self.scan = 0
            self.pending = bytearray()
            self.need = 512
            self.extension_body = self.ended = self.completed = False
            self.current = None
            self.destination.mkdir(parents=True, exist_ok=False)
            self.checkpoint()

    def _open(self):
        if self.file is None:
            target = self.safe_target(self.destination, self.current['name'])
            self.file = open(target, 'r+b', buffering=8 * 1024 * 1024)
            self.file.seek(self.current['written'])

    def _close_file(self):
        if self.file is not None:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            self.file = None

    def _header(self):
        if self.extension_body:
            self.scan = self.need
            self.need += 512
            self.extension_body = False
            return
        block = bytes(self.pending[self.scan:self.scan + 512])
        if block == bytes(512):
            if self.scan:
                raise ValueError('文件夹扩展头不完整')
            self.pending.clear()
            self.ended = True
            return
        member = tarfile.TarInfo.frombuf(block, 'utf-8', 'surrogateescape')
        if member.size < 0 or member.issparse():
            raise ValueError('不支持的归档文件类型')
        if member.type in (tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            self.need += (member.size + 511) // 512 * 512
            if self.need + 512 > self.HEADER_LIMIT:
                raise ValueError('文件夹扩展头过大')
            self.extension_body = True
            if len(self.pending) == self.need:
                self._header()
            return
        if member.type in (tarfile.XGLTYPE, tarfile.GNUTYPE_SPARSE):
            raise ValueError('不支持的归档扩展类型')
        # Let the stdlib decode PAX Unicode/long paths and large-file sizes.
        with tarfile.open(fileobj=io.BytesIO(bytes(self.pending)), mode='r:') as archive:
            member = archive.next()
        if member is None or member.size < 0 or member.issparse():
            raise ValueError('文件夹文件头无效')
        target = self.safe_target(self.destination, member.name)
        kind = 'file' if member.isfile() else 'skip'
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, 'wb'):
                pass
        self.current = dict(name=member.name, kind=kind, size=member.size,
                            written=0, padding=(-member.size) % 512)
        self.pending.clear()
        self.scan, self.need, self.extension_body = 0, 512, False

    def feed(self, payload):
        view = memoryview(payload)
        consumed = 0
        while consumed < len(view):
            if self.ended:
                if any(view[consumed:]):
                    raise ValueError('文件夹尾部数据无效')
                consumed = len(view)
            elif self.current is None:
                count = min(self.need - len(self.pending), len(view) - consumed)
                self.pending.extend(view[consumed:consumed + count])
                consumed += count
                if len(self.pending) == self.need:
                    self._header()
            else:
                remaining = self.current['size'] - self.current['written']
                if remaining:
                    count = min(remaining, len(view) - consumed)
                    if self.current['kind'] == 'file':
                        self._open()
                        self.file.write(view[consumed:consumed + count])
                    self.current['written'] += count
                    consumed += count
                elif self.current['padding']:
                    count = min(self.current['padding'], len(view) - consumed)
                    self.current['padding'] -= count
                    consumed += count
                else:
                    self._close_file()
                    self.current = None
        self.offset += len(payload)

    def checkpoint(self):
        if self.file is not None:
            self.file.flush()
            os.fsync(self.file.fileno())
        state = dict(version=1, destination=self.destination.name, offset=self.offset,
                     header=base64.b64encode(self.pending).decode('ascii'), scan=self.scan,
                     need=self.need, extension_body=self.extension_body, current=self.current,
                     ended=self.ended, completed=self.completed)
        temporary = self.state_path.with_suffix('.json.tmp')
        with open(temporary, 'w', encoding='utf-8') as target:
            json.dump(state, target, ensure_ascii=True)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, self.state_path)

    def finish(self):
        if not self.ended or self.pending:
            raise ValueError('文件夹数据未完整接收')
        self._close_file()
        self.completed = True
        # Keep a tiny receipt so a lost final ACK does not duplicate the folder.
        self.checkpoint()

    def close(self):
        # Do not commit parser state after a failed frame/write. Replay from
        # the previous durable checkpoint overwrites any uncommitted bytes.
        if self.file is not None:
            self.file.close()
            self.file = None


def receive_folder_frames(app, conn, source, name, total, sink, started_at, frame_stream, unknown_total):
    offset = sink.offset
    app.root.after(0, app._start_progress, f'正在直接接收文件夹：{name}', source)
    stream = frame_stream(conn)
    while sink.offset < total:
        position, payload = stream.read_frame(verify_digest=True)
        if position != sink.offset:
            raise ValueError('数据帧位置无效')
        if not payload and total == unknown_total:
            total = sink.offset
            break
        if not payload or sink.offset + len(payload) > total:
            raise ValueError('数据帧长度无效')
        sink.feed(payload)
        sink.checkpoint()
        app._progress_threadsafe(sink.offset, total, started_at, initial_offset=offset)
    sink.finish()
    app._progress_threadsafe(total, total, started_at, initial_offset=offset)
    return total
