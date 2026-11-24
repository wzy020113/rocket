#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小火箭共享协议。

应用使用 NEGOTIATE_VERSION=2：认证沿用 XRT1 口令应答；XRT2 协商
包含模式、名称、实际载荷长度和 SHA-256。ACK 的非零端口仅作接受标记，
协商与 XRT3 数据帧均使用原 TCP 连接，无需额外开放端口。
模式 255 专用于探活，文件长度为零仍代表合法空文件。
文件和稳定 tar 载荷都支持断点；接收端持有任务锁并保留 .part。
接收端校验、保存和解包完成后返回 XROK，发送端才显示完成。
快速模式 2/3 使用属性与少量样本生成任务标识，每个 XRT3 负载后附加
32 字节 SHA-256 校验值；文件夹直接从原文件按需生成 tar 字节，无预打包。

其余端口池和心跳工具保留供既有调用者使用。
"""

from __future__ import annotations

import socket
import struct
import time

# 控制协商魔法（阶段一）
NEGOTIATE_MAGIC = b"XRT2"
NEGOTIATE_VERSION = 2

# 数据帧魔法（阶段二）
DATA_MAGIC = b"XRT3"

# 心跳单字节帧
HEARTBEAT_BYTE = 0x48  # 'H'

HEARTBEAT_INTERVAL = 4.0   # 秒，互发间隔
HEARTBEAT_DEADLINE = 12.0  # 秒，连续 3 次无响应判定断连

# 限制单帧内存占用，兼容 TURBO 64MB 数据块
DATA_FRAME_MAX = 64 * 1024 * 1024

# 保留历史探活总量常量；v2 使用 MODE_PROBE 区分空文件与探活
PROBE_TOTAL = 0
MODE_PROBE = 255
MODE_STREAM_FILE = 2
MODE_STREAM_FOLDER = 3
MODE_LAZY_FOLDER = 4
COMPLETE_MAGIC = b"XROK"


def enable_disconnect_detection(sock):
    """Bound silent network failure detection without interrupting slow unpacking."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        if hasattr(socket, 'SIO_KEEPALIVE_VALS'):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))
        else:
            for name, value in [('TCP_KEEPIDLE', 10), ('TCP_KEEPINTVL', 3), ('TCP_KEEPCNT', 3)]:
                if hasattr(socket, name):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except OSError:
        pass


class ProtocolError(Exception):
    """协议帧非法或版本不兼容。"""


class NegotiateRejected(Exception):
    """接收端拒绝协商。"""


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        packet = sock.recv(size - len(data))
        if not packet:
            raise ConnectionError("连接中断")
        data.extend(packet)
    return bytes(data)


def send_frame(sock: socket.socket, *parts: bytes) -> None:
    sock.sendall(b"".join(parts))


def pack_u8(v: int) -> bytes:
    return struct.pack(">B", v)


def pack_u16(v: int) -> bytes:
    return struct.pack(">H", v)


def pack_u32(v: int) -> bytes:
    return struct.pack(">I", v)


def pack_u64(v: int) -> bytes:
    return struct.pack(">Q", v)


def build_nego_request(
    mode: int,
    name: str,
    total: int,
    offset: int,
    digest: str = "",
) -> bytes:
    """控制请求帧（不含认证块，认证由调用方单独发送）。

    digest：文件指纹（文件模式），用于断点续传时防止同名不同文件串档。
    """
    name_bytes = name.encode("utf-8")
    digest_bytes = digest.encode("utf-8")[:128]
    return (
        NEGOTIATE_MAGIC
        + pack_u8(NEGOTIATE_VERSION)
        + pack_u8(mode)
        + pack_u32(len(name_bytes))
        + name_bytes
        + pack_u64(total)
        + pack_u64(offset)
        + pack_u16(len(digest_bytes))
        + digest_bytes
    )


def build_ack(data_port: int, resume_offset: int = 0) -> bytes:
    """ACK 确认帧：附带空闲数据端口与断点位置。data_port=0 表示拒绝/探活应答。"""
    return NEGOTIATE_MAGIC + pack_u8(NEGOTIATE_VERSION) + pack_u16(data_port) + pack_u64(resume_offset)


def send_negotiate_request(
    sock: socket.socket,
    mode: int,
    name: str,
    total: int,
    offset: int,
    token: str,
    digest: str = "",
) -> None:
    """发送认证块（可选）+ 控制请求帧。"""
    token_bytes = token.encode("utf-8")
    if token_bytes:
        if len(token_bytes) > 1024:
            raise ValueError("接收口令过长")
        send_frame(sock, b"XRT1", pack_u16(len(token_bytes)), token_bytes)
        if recv_exact(sock, 1) != b"\x01":
            raise PermissionError("接收口令错误")
    send_frame(sock, build_nego_request(mode, name, total, offset, digest))


def read_negotiate_request(sock: socket.socket, token: str) -> tuple[int, str, int, int, str]:
    """读取认证块（若带）+ 控制请求帧，校验口令与协议版本。

    返回 (mode, name, total, offset, digest)。
    """
    first = recv_exact(sock, 4)
    if first == b"XRT1":
        token_len = struct.unpack(">H", recv_exact(sock, 2))[0]
        supplied = recv_exact(sock, token_len)
        expected = token.encode("utf-8")
        if not expected or not _constant_time_eq(supplied, expected):
            raise PermissionError("接收口令错误")
        first = recv_exact(sock, 4)
    elif token:
        raise PermissionError("此接收端需要口令")

    if first != NEGOTIATE_MAGIC:
        raise ProtocolError("握手标识无效（旧版协议或非法客户端）")
    version = struct.unpack(">B", recv_exact(sock, 1))[0]
    if version != NEGOTIATE_VERSION:
        raise ProtocolError(f"协议版本不兼容：{version}")
    mode = struct.unpack(">B", recv_exact(sock, 1))[0]
    name_len = struct.unpack(">I", recv_exact(sock, 4))[0]
    if name_len <= 0 or name_len > 8192:
        raise ValueError("文件名长度异常")
    name = recv_exact(sock, name_len).decode("utf-8", errors="replace")
    total = struct.unpack(">Q", recv_exact(sock, 8))[0]
    offset = struct.unpack(">Q", recv_exact(sock, 8))[0]
    if offset > total:
        raise ValueError("断点位置超出文件范围")
    digest_len = struct.unpack(">H", recv_exact(sock, 2))[0]
    digest = recv_exact(sock, digest_len).decode("utf-8", errors="replace") if digest_len else ""
    return mode, name, total, offset, digest


def read_ack_frame(sock: socket.socket) -> tuple[int, int]:
    """读取 ACK 帧原始值，返回 (data_port, resume_offset)。

    不校验端口合法性（探活应答 data_port=0 也是合法应答）。
    """
    magic = recv_exact(sock, 4)
    if magic != NEGOTIATE_MAGIC:
        raise ProtocolError("非法 ACK 帧")
    version = struct.unpack(">B", recv_exact(sock, 1))[0]
    if version != NEGOTIATE_VERSION:
        raise ProtocolError(f"协议版本不兼容：{version}")
    data_port = struct.unpack(">H", recv_exact(sock, 2))[0]
    resume_offset = struct.unpack(">Q", recv_exact(sock, 8))[0]
    return data_port, resume_offset


def read_ack(sock: socket.socket) -> tuple[int, int]:
    """读取 ACK 帧，返回 (data_port, resume_offset)。data_port=0 抛 NegotiateRejected。"""
    data_port, resume_offset = read_ack_frame(sock)
    if data_port == 0:
        raise NegotiateRejected("接收端拒绝协商")
    if not 1 <= data_port <= 65535:
        raise ProtocolError("非法数据端口")
    return data_port, resume_offset


def _constant_time_eq(a: bytes, b: bytes) -> bool:
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= x ^ y
    return diff == 0


# ---------------- 数据帧 ----------------


def send_data_frame(sock: socket.socket, offset: int, payload: bytes) -> None:
    sock.sendall(DATA_MAGIC + pack_u64(offset) + pack_u32(len(payload)) + payload)


class FrameStream:
    """数据连接帧读取器（单向数据流方向使用）。

    - 在帧边界跳过对方发来的 HB 心跳字节（发送方写锁保证心跳只出现在帧边界）
    - 任何字节到达都会刷新心跳截止时间；超时判定断连（ConnectionError）
    """

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = bytearray()
        self.deadline_box = [time.monotonic() + HEARTBEAT_DEADLINE]

    def _fill(self) -> None:
        remaining = self.deadline_box[0] - time.monotonic()
        if remaining <= 0:
            raise ConnectionError("心跳超时，连接已断开")
        self.sock.settimeout(min(remaining, 10.0))
        try:
            packet = self.sock.recv(1 << 20)
        except socket.timeout:
            raise ConnectionError("心跳超时，连接已断开") from None
        if not packet:
            raise ConnectionError("连接中断")
        self.deadline_box[0] = time.monotonic() + HEARTBEAT_DEADLINE
        self._buf.extend(packet)

    def _take(self, n: int) -> bytes:
        if n < 0:
            raise ValueError("读取长度不能为负数")
        while len(self._buf) < n:
            self._fill()
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def read_frame(self, verify_digest=False) -> tuple[int, bytes]:
        """读取一个数据帧，返回 (offset, payload)。"""
        first = self._next_header_byte()
        header = bytearray([first])
        header.extend(self._take(15))
        if bytes(header[:4]) != DATA_MAGIC:
            raise ProtocolError("非法数据帧")
        offset = struct.unpack(">Q", header[4:12])[0]
        length = struct.unpack(">I", header[12:16])[0]
        if length > DATA_FRAME_MAX:
            raise ProtocolError("数据帧过大")
        payload = self._take(length)
        if verify_digest:
            import hashlib
            if self._take(32) != hashlib.sha256(payload).digest():
                raise ProtocolError("数据块校验失败，请重传该块")
        return offset, payload

    def _next_header_byte(self) -> int:
        """帧边界处：跳过对方心跳字节，返回下一个数据字节。"""
        while True:
            if not self._buf:
                self._fill()
            b = self._buf[0]
            del self._buf[0]
            if b != HEARTBEAT_BYTE:
                return b


# ---------------- 心跳 ----------------


def send_heartbeat(sock: socket.socket, write_lock=None) -> None:
    if write_lock is not None:
        with write_lock:
            sock.sendall(bytes([HEARTBEAT_BYTE]))
    else:
        sock.sendall(bytes([HEARTBEAT_BYTE]))


def wait_for_heartbeat(sock: socket.socket, deadline_box: list, cancel=None) -> bool:
    """等待对方心跳字节，任何字节到达即视为存活并刷新 deadline_box。

    返回 True 存活 / False 超时或取消；连接断开抛 ConnectionError。
    """
    while True:
        remaining = deadline_box[0] - time.monotonic()
        if remaining <= 0:
            return False
        if cancel is not None and cancel.is_set():
            return False
        sock.settimeout(min(remaining, 1.0))
        try:
            packet = sock.recv(16)
        except socket.timeout:
            continue
        except OSError:
            raise ConnectionError("心跳期间连接断开")
        if not packet:
            raise ConnectionError("对端已关闭连接")
        deadline_box[0] = time.monotonic() + HEARTBEAT_DEADLINE
        return True


def heartbeat_sender(sock: socket.socket, stop: "threading.Event", write_lock=None) -> None:
    """心跳发送循环：每 HEARTBEAT_INTERVAL 秒发一个字节，直到 stop 置位。"""
    try:
        while not stop.is_set():
            if stop.wait(HEARTBEAT_INTERVAL):
                break
            send_heartbeat(sock, write_lock)
    except OSError:
        pass


def heartbeat_monitor(sock: socket.socket, stop: "threading.Event", deadline_box: list) -> None:
    """监测对端心跳；超时或断开时关闭连接以唤醒发送线程。"""
    try:
        while not stop.is_set():
            remaining = deadline_box[0] - time.monotonic()
            if remaining <= 0:
                raise ConnectionError("心跳超时，连接已断开")
            sock.settimeout(min(remaining, 1.0))
            try:
                packet = sock.recv(1)
            except socket.timeout:
                continue
            if not packet:
                raise ConnectionError("对端已关闭连接")
            if packet != bytes([HEARTBEAT_BYTE]):
                raise ProtocolError("数据连接收到非法控制字节")
            deadline_box[0] = time.monotonic() + HEARTBEAT_DEADLINE
    except (OSError, ConnectionError, ProtocolError):
        if not stop.is_set():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


# ---------------- 端口池 ----------------


def create_listen_socket(port: int) -> socket.socket:
    """在指定端口（0 表示系统自动分配）创建监听 socket。"""
    if socket.has_dualstack_ipv6():
        try:
            server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("::", port))
            server.listen(2)
            return server
        except OSError:
            pass
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(2)
    return server


def listen_port(server: socket.socket) -> int:
    return server.getsockname()[1]
