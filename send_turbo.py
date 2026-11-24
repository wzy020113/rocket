#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小火箭 · 加速抢带宽发送端 (TURBO)

在协议完全兼容接收端（1 byte mode, 4 bytes name len, UTF-8 name,
8 bytes payload size, then payload）的前提下，通过以下手段让发送端更快：
- mmap 零拷贝读取 + memoryview 直发，减少数据拷贝
- 超大发送缓冲 SO_SNDBUF（128MB，按系统上限自动降级）
- TCP_NODELAY + QoS DSCP(EF) 标记，抢占网络转发优先级
- 提升进程/线程优先级，抢占 CPU 时间片
- 64MB 大块发送，减少系统调用次数
"""

from __future__ import annotations

import mmap
import os
import socket
import struct
import sys
import tarfile
import threading
import time
from pathlib import Path

# 复用原发送端的 UI 与协议实现
import send

# ---------- TURBO 常量（比原版更激进） ----------
TURBO_CHUNK = 64 * 1024 * 1024          # 64MB 大块发送
TURBO_SNDBUF = 128 * 1024 * 1024        # 128MB 发送缓冲（按上限降级）
TURBO_DSCP = 0xB8                        # DSCP EF（加速转发）


def _boost_priority() -> None:
    """Windows 下把当前进程与线程提到高于普通，抢占 CPU 时间片。"""
    if os.name != "nt":
        return
    try:
        from ctypes import windll

        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        THREAD_PRIORITY_ABOVE_NORMAL = 1
        windll.kernel32.SetPriorityClass(windll.kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS)
        windll.kernel32.SetThreadPriority(windll.kernel32.GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL)
    except Exception:
        pass


def _tune_socket_turbo(sock: socket.socket) -> None:
    """比原版更激进的 socket 调优：大缓冲 + 禁 Nagle + QoS 标记。"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass
    for size in (TURBO_SNDBUF, TURBO_SNDBUF // 2, TURBO_SNDBUF // 4, 32 * 1024 * 1024):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, size)
            break
        except OSError:
            continue
    # QoS DSCP 标记（部分网卡/系统不支持，失败则忽略）
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, TURBO_DSCP)
    except OSError:
        pass


def turbo_send_payload(
    sock: socket.socket,
    is_dir: bool,
    path: str,
    name: str,
    total: int,
    cancel_event: threading.Event,
    on_progress,
    token: str = "",
) -> None:
    """发送协议头 + 载荷（文件用 mmap 零拷贝，文件夹用流式 tar）。

    与接收端协议完全兼容：mode(1B) + name_len(4B) + name + total(8B) + payload。
    """
    _tune_socket_turbo(sock)

    name_bytes = name.encode("utf-8")
    token_bytes = token.encode("utf-8")
    if token_bytes:
        if len(token_bytes) > 1024:
            raise ValueError("接收口令过长")
        send.authenticate_socket(sock, token)
    sock.sendall(struct.pack(">B", send.MODE_FOLDER if is_dir else send.MODE_FILE))
    sock.sendall(struct.pack(">I", len(name_bytes)))
    sock.sendall(name_bytes)
    sock.sendall(struct.pack(">Q", total))

    if is_dir:
        writer = send.SocketProgressWriter(
            sock,
            cancel_event,
            on_progress,
            buffer_size=TURBO_CHUNK,
        )
        with tarfile.open(fileobj=writer, mode="w|") as tar:
            tar.add(path, arcname=name)
        writer.flush()
        on_progress(total)
        return

    # 文件模式：mmap 零拷贝直发
    if total <= 0:
        on_progress(0)
        return
    with open(path, "rb") as file:
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            sent = 0
            while sent < total:
                if cancel_event.is_set():
                    raise send.TransferCancelled("用户已取消传输")
                data = mm[sent : sent + TURBO_CHUNK]
                sock.sendall(data)
                sent += len(data)
                on_progress(sent)
    on_progress(total)


class TurboApp(send.App):
    """与原生发送端 UI 一致，仅发送内核换成 TURBO 加速版。"""

    def __init__(self, root):
        super().__init__(root)
        self.root.title("小火箭 · 加速抢带宽发送端")
        try:
            self.header_status.set_badge(f"TURBO · {send.get_local_ip()} · {send.PORT}", send.WARN)
        except Exception:
            pass
        self._log("TURBO 加速模式已启用：mmap 零拷贝 + 大缓冲 + QoS 抢占。")

    def _send_worker(self, path: str, receiver: dict[str, str]) -> None:
        _boost_priority()
        return super()._send_worker(path, receiver)

    def _tune_socket(self, sock):
        _tune_socket_turbo(sock)

    def _legacy_payload(self, sock, path, name, total, is_dir, token, progress):
        turbo_send_payload(sock, is_dir, path, name, total, self.cancel_event, progress, token)

    def _payload_chunks(self, path, offset, total):
        if isinstance(path, send.StreamPayload):
            yield from path.iter_chunks(offset, total, TURBO_CHUNK)
            return
        if total == 0:
            return
        with open(path, 'rb') as source:
            with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                while offset < total:
                    end = min(total, offset + TURBO_CHUNK)
                    yield mapped[offset:end]
                    offset = end


def main() -> None:
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = send.ctk.CTk()
    TurboApp(root)
    send.apply_windows_chrome(root)
    root.mainloop()


if __name__ == "__main__":
    main()
