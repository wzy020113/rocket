"""Seekable tar stream: keep headers in memory and read source bytes on demand."""
import bisect
import hashlib
import os
import stat
from pathlib import Path
import tarfile

UNKNOWN_TOTAL = (1 << 63) - 1


def folder_size(path, cancel):
    """Metadata-only total, without payload objects, hashes or content reads."""
    raw, archive_bytes = 0, 1024
    pending = [(Path(path), Path(path).name, None)]
    while pending:
        if cancel.is_set():
            raise InterruptedError('已停止大小统计')
        current, name, info = pending.pop()
        info = current.lstat() if info is None else info
        is_file, is_directory = stat.S_ISREG(info.st_mode), stat.S_ISDIR(info.st_mode)
        if is_file or is_directory:
            try:
                name.encode('ascii')
                plain_header = len(name) + int(is_directory) <= 100
            except UnicodeEncodeError:
                plain_header = False
            if plain_header and (not is_file or info.st_size < 8 ** 11):
                archive_bytes += 512
            else:
                member = tarfile.TarInfo(name)
                member.type = tarfile.REGTYPE if is_file else tarfile.DIRTYPE
                member.mode = stat.S_IMODE(info.st_mode)
                member.size = info.st_size if is_file else 0
                archive_bytes += len(member.tobuf(format=tarfile.PAX_FORMAT, encoding='utf-8'))
            if is_file:
                raw += info.st_size
                archive_bytes += (info.st_size + 511) // 512 * 512
            else:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if cancel.is_set():
                            raise InterruptedError('已停止大小统计')
                        entry_info = entry.stat(follow_symlinks=False)
                        pending.append((Path(entry.path), name + '/' + entry.name, entry_info))
        elif stat.S_ISLNK(info.st_mode):
            member = tarfile.TarInfo(name)
            member.mode = stat.S_IMODE(info.st_mode)
            member.type, member.linkname = tarfile.SYMTYPE, os.readlink(current)
            archive_bytes += len(member.tobuf(format=tarfile.PAX_FORMAT, encoding='utf-8'))
    archive_bytes += (-archive_bytes) % tarfile.RECORDSIZE
    return raw, archive_bytes


class StreamPayload:
    def __init__(self, path, cancel, on_scan=None):
        self.path = Path(path)
        self.cancel = cancel
        self.segments = []
        self.ends = []
        self.total = 0
        self.identity = hashlib.sha256()
        self.files = 0
        if self.path.is_dir():
            self._walk(self.path, self.path.name, self.path.lstat(), on_scan)
            self._add(bytes(1024))
            padding = (-self.total) % tarfile.RECORDSIZE
            if padding:
                self._add(bytes(padding))
        else:
            self._source(self.path)
        self.digest = self.identity.hexdigest()

    def _check(self):
        if self.cancel.is_set():
            raise InterruptedError('用户已取消传输，已接收的断点保留')

    def _add(self, data, size=None, snapshot=None):
        size = len(data) if size is None else size
        if size:
            self.segments.append((data, snapshot))
            self.total += size
            self.ends.append(self.total)

    @staticmethod
    def _snapshot(path):
        st = path.stat()
        return st.st_size, st.st_mtime_ns

    def _source(self, path, snapshot=None):
        self._check()
        snapshot = self._snapshot(path) if snapshot is None else snapshot
        self.identity.update(str(path).encode('utf-8'))
        self.identity.update(repr(snapshot).encode('ascii'))
        self._add(path, snapshot[0], snapshot)
        self.files += 1

    def _walk(self, path, name, info, on_scan):
        self._check()
        member = tarfile.TarInfo(name)
        member.mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            member.type = tarfile.REGTYPE
            member.size = info.st_size
        elif stat.S_ISDIR(info.st_mode):
            member.type = tarfile.DIRTYPE
        elif stat.S_ISLNK(info.st_mode):
            member.type = tarfile.SYMTYPE
            member.linkname = os.readlink(path)
        elif stat.S_ISFIFO(info.st_mode):
            member.type = tarfile.FIFOTYPE
        else:
            return
        header = member.tobuf(format=tarfile.PAX_FORMAT, encoding='utf-8')
        self.identity.update(header)
        self._add(header)
        if member.isfile():
            self._source(path, (info.st_size, info.st_mtime_ns))
            if member.size % 512:
                self._add(bytes(512 - member.size % 512))
            if on_scan:
                on_scan(self.files)
        elif member.isdir():
            # DirEntry caches Windows directory metadata: no per-file open/read.
            with os.scandir(path) as entries:
                children = sorted(entries, key=lambda child: child.name)
            for child in children:
                self._walk(Path(child.path), name + '/' + child.name,
                           child.stat(follow_symlinks=False), on_scan)

    def iter_chunks(self, offset, total, chunk_size):
        if total != self.total or not 0 <= offset <= total:
            raise ValueError('断点超出文件范围')
        index = bisect.bisect_right(self.ends, offset)
        while offset < total:
            self._check()
            data, snapshot = self.segments[index]
            start = self.ends[index - 1] if index else 0
            position = offset - start
            remaining = self.ends[index] - offset
            if snapshot is None:
                while remaining:
                    self._check()
                    chunk = data[position:position + min(chunk_size, remaining)]
                    yield chunk
                    position += len(chunk)
                    offset += len(chunk)
                    remaining -= len(chunk)
            else:
                if self._snapshot(data) != snapshot:
                    raise IOError(f'源文件发生变化：{data}')
                with open(data, 'rb', buffering=chunk_size) as source:
                    source.seek(position)
                    while remaining:
                        self._check()
                        chunk = source.read(min(chunk_size, remaining))
                        if not chunk:
                            raise IOError(f'源文件读取不完整：{data}')
                        yield chunk
                        remaining -= len(chunk)
                        offset += len(chunk)
                if self._snapshot(data) != snapshot:
                    raise IOError(f'源文件发生变化：{data}')
            index += 1


class LazyFolderPayload(StreamPayload):
    """Discover one entry at a time; retain its layout for automatic reconnects."""
    def __init__(self, path, cancel):
        self.path = Path(path)
        self.cancel = cancel
        self.segments, self.ends = [], []
        self.total, self.files = 0, 0
        self.identity = hashlib.sha256()
        self.digest = hashlib.sha256((str(self.path.resolve()) + repr(self.path.stat().st_mtime_ns)).encode('utf-8')).hexdigest()
        self.finished = False
        self.discovery = self._discover(self.path, self.path.name)

    def _discover(self, path, name, info=None):
        self._check()
        info = path.lstat() if info is None else info
        member = tarfile.TarInfo(name)
        member.mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            member.type, member.size = tarfile.REGTYPE, info.st_size
        elif stat.S_ISDIR(info.st_mode):
            member.type = tarfile.DIRTYPE
        elif stat.S_ISLNK(info.st_mode):
            member.type, member.linkname = tarfile.SYMTYPE, os.readlink(path)
        else:
            return
        self._add(member.tobuf(format=tarfile.PAX_FORMAT, encoding='utf-8'))
        if member.isfile():
            self._source(path, (info.st_size, info.st_mtime_ns))
            if member.size % 512:
                self._add(bytes(512 - member.size % 512))
        yield
        if member.isdir():
            with os.scandir(path) as entries:
                children = sorted(entries, key=lambda child: child.name)
            for child in children:
                yield from self._discover(Path(child.path), name + '/' + child.name,
                                          child.stat(follow_symlinks=False))

    def iter_chunks(self, offset, total, chunk_size):
        while True:
            while offset >= self.total and not self.finished:
                try:
                    next(self.discovery)
                except StopIteration:
                    self._add(bytes(1024))
                    if self.total % tarfile.RECORDSIZE:
                        self._add(bytes((-self.total) % tarfile.RECORDSIZE))
                    self.finished = True
            if offset >= self.total:
                if offset > self.total:
                    raise ValueError('断点超出文件夹大小，请保持源目录不变')
                return
            available = self.total
            for chunk in super().iter_chunks(offset, available, chunk_size):
                yield chunk
                offset += len(chunk)
