#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小火箭 · 发送端

局域网 / 公网大文件与文件夹传输。支持单连接断点续传，
连接旧版接收端时自动回退到原协议。
"""

from __future__ import annotations

import collections
import ipaddress
import json
import math
import os
import socket
import struct
import sys
import tarfile
import threading
import time
import hashlib
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

VENDOR_DIR = Path(__file__).resolve().with_name("vendor")
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

import customtkinter as ctk
import xrt_proto
from stream_payload import StreamPayload, LazyFolderPayload, UNKNOWN_TOTAL, folder_size

ctk.set_appearance_mode("dark")


PORT = 9999
MIN_PORT = 1
MAX_PORT = 65535
AUTH_MAGIC = b"XRT1"
CHUNK = 8 * 1024 * 1024
SEND_BUF_SIZE = 32 * 1024 * 1024
PROGRESS_INTERVAL = 0.2
SPEED_SAMPLES = 48
CONNECT_TIMEOUT = 4

MODE_FILE = 0
MODE_FOLDER = 1

APP_NAME = "XiaoRocketTransfer"
APP_CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_FILE = APP_CONFIG_DIR / "receivers.json"
LEGACY_CONFIG_FILE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).with_name("receivers.json")
DEFAULT_RECEIVERS = [
    {"name": "本机测试", "ip": "127.0.0.1", "port": str(PORT), "token": ""},
]

from rocket_ui import (BG, SURFACE, SURFACE_2, SURFACE_3, BORDER, BORDER_BRIGHT,
                       TEXT, TEXT_DIM, MUTED, ACCENT, ACCENT_HOVER, ACCENT_2, WARN,
                       DANGER, INPUT, HEADER_LEFT, HEADER_RIGHT, HEADER_GLOW)

FONT = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 22, "bold")
FONT_MONO = ("Consolas", 9)


class TransferCancelled(Exception):
    """Raised when the user cancels the active transfer."""


def ensure_config_dir() -> None:
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def blend_color(left: str, right: str, ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    a = tuple(int(left[i : i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(right[i : i + 2], 16) for i in (1, 3, 5))
    mixed = tuple(round(a[i] + (b[i] - a[i]) * ratio) for i in range(3))
    return f"#{mixed[0]:02x}{mixed[1]:02x}{mixed[2]:02x}"


def apply_windows_chrome(root: tk.Tk) -> None:
    if os.name != "nt":
        return
    try:
        from ctypes import byref, c_int, c_uint, sizeof, windll

        root.update_idletasks()
        hwnd = windll.user32.GetParent(root.winfo_id())
        dark_mode = c_int(1)
        windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, byref(dark_mode), sizeof(dark_mode))

        rounded = c_int(2)
        windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, byref(rounded), sizeof(rounded))

        for attribute, color in ((34, 0x473726), (35, 0x18110B), (36, 0xFBF8F4)):
            value = c_uint(color)
            windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, byref(value), sizeof(value))
    except Exception:
        pass


class ActionButton(tk.Button):
    def __init__(self, parent: tk.Widget, base_bg: str, **kwargs):
        self._base_bg = base_bg
        self._hover_bg = ACCENT_HOVER if base_bg == ACCENT else blend_color(base_bg, TEXT, 0.12)
        super().__init__(parent, bg=base_bg, **kwargs)
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")

    def _on_enter(self, _event) -> None:
        if str(self.cget("state")) != "disabled":
            super().configure(bg=self._hover_bg)

    def _on_leave(self, _event) -> None:
        super().configure(bg=self._base_bg)

    def configure(self, cnf=None, **kwargs):
        next_bg = kwargs.get("bg", kwargs.get("background"))
        if next_bg is not None:
            self._base_bg = next_bg
            self._hover_bg = ACCENT_HOVER if next_bg == ACCENT else blend_color(next_bg, TEXT, 0.12)
        return super().configure(cnf, **kwargs)

    config = configure


class ModernButton(ctk.CTkButton):
    def configure(self, require_redraw=False, **kwargs):
        if "bg" in kwargs:
            kwargs["fg_color"] = kwargs.pop("bg")
        if "background" in kwargs:
            kwargs["fg_color"] = kwargs.pop("background")
        if "fg" in kwargs:
            kwargs["text_color"] = kwargs.pop("fg")
        if "activebackground" in kwargs:
            kwargs["hover_color"] = kwargs.pop("activebackground")
        kwargs.pop("activeforeground", None)
        if "disabledforeground" in kwargs:
            kwargs["text_color_disabled"] = kwargs.pop("disabledforeground")
        return super().configure(require_redraw=require_redraw, **kwargs)

    def config(self, **kwargs):
        return self.configure(**kwargs)


class ModernLabel(ctk.CTkLabel):
    def configure(self, require_redraw=False, **kwargs):
        if "bg" in kwargs:
            kwargs["fg_color"] = kwargs.pop("bg")
        if "fg" in kwargs:
            kwargs["text_color"] = kwargs.pop("fg")
        return super().configure(require_redraw=require_redraw, **kwargs)

    def config(self, **kwargs):
        return self.configure(**kwargs)


class HeaderBar(tk.Canvas):
    def __init__(self, parent: tk.Widget, title: str, subtitle: str, badge: str):
        super().__init__(parent, height=108, bg=BG, highlightthickness=0)
        self.title_text = title
        self.subtitle_text = subtitle
        self.badge_text = badge
        self.badge_color = ACCENT_2
        self.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    def set_badge(self, text: str, color: str = ACCENT_2) -> None:
        self.badge_text = text
        self.badge_color = color
        self._draw()

    def round_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs) -> None:
        self.create_arc(x1, y1, x1 + radius * 2, y1 + radius * 2, start=90, extent=90, **kwargs)
        self.create_arc(x2 - radius * 2, y1, x2, y1 + radius * 2, start=0, extent=90, **kwargs)
        self.create_arc(x2 - radius * 2, y2 - radius * 2, x2, y2, start=270, extent=90, **kwargs)
        self.create_arc(x1, y2 - radius * 2, x1 + radius * 2, y2, start=180, extent=90, **kwargs)
        self.create_rectangle(x1 + radius, y1, x2 - radius, y2, **kwargs)
        self.create_rectangle(x1, y1 + radius, x2, y2 - radius, **kwargs)

    def _draw(self) -> None:
        self.delete("all")
        width = max(320, self.winfo_width())
        height = max(108, self.winfo_height())
        for x in range(width):
            self.create_line(x, 0, x, height, fill=blend_color(HEADER_LEFT, HEADER_RIGHT, x / width))
        for y in range(16, height - 4, 18):
            self.create_line(max(0, width - 360), y, width, y, fill="#16323a")
        for x in range(max(0, width - 360), width, 32):
            self.create_line(x, 0, x, height, fill="#122a32")
        self.create_rectangle(0, height - 2, width, height, fill=HEADER_GLOW, outline="")
        self.create_line(0, 1, width, 1, fill="#1d5360")
        self.round_rect(24, 28, 70, 74, 10, fill="#162633", outline="#35566a")
        self.create_text(47, 51, text="X", fill=HEADER_GLOW, font=("Segoe UI", 18, "bold"))
        self.create_line(39, 63, 55, 63, fill=ACCENT, width=2)
        self.create_text(88, 28, anchor="nw", text=self.title_text, fill=TEXT, font=FONT_TITLE)
        self.create_text(90, 67, anchor="nw", text=self.subtitle_text, fill=TEXT_DIM, font=FONT)
        self.create_text(90, 88, anchor="sw", text="PRIVATE NETWORK  /  LARGE FILE TRANSFER", fill=MUTED, font=("Consolas", 8, "bold"))
        badge_w = max(112, min(240, len(self.badge_text) * 9 + 36))
        x1 = width - badge_w - 24
        y1 = 37
        self.round_rect(x1, y1, width - 24, y1 + 34, 8, fill="#10242a", outline="#32606b")
        self.create_oval(x1 + 14, y1 + 13, x1 + 22, y1 + 21, fill=self.badge_color, outline="")
        self.create_text(x1 + 30, y1 + 17, anchor="w", text=self.badge_text, fill=TEXT, font=FONT_BOLD)


def human_size(value: int | float) -> str:
    n = float(max(value, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def human_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def validate_host(host: str) -> bool:
    host = host.strip()
    if not host or any(ch.isspace() for ch in host):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.")
    return all(ch in allowed for ch in host) and not host.startswith(".") and not host.endswith(".")


def validate_port(value: str | int) -> bool:
    try:
        return MIN_PORT <= int(str(value).strip()) <= MAX_PORT
    except (TypeError, ValueError):
        return False


def endpoint_key(receiver: dict[str, str]) -> str:
    return f"{receiver['ip']}:{receiver.get('port', PORT)}"


def load_receivers() -> list[dict[str, str]]:
    data = read_json(CONFIG_FILE)
    if data is None and LEGACY_CONFIG_FILE.exists():
        data = read_json(LEGACY_CONFIG_FILE)
        if data is not None:
            try:
                ensure_config_dir()
                CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
    if data is None:
        return DEFAULT_RECEIVERS[:]

    receivers: list[dict[str, str]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            ip = str(item.get("ip", "")).strip()
            port = str(item.get("port", PORT)).strip()
            token = str(item.get("token", ""))
            if name and ip and validate_host(ip) and validate_port(port):
                receivers.append({"name": name, "ip": ip, "port": port, "token": token})
    return receivers if receivers else DEFAULT_RECEIVERS[:]


def save_receivers(receivers: list[dict[str, str]]) -> None:
    ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(receivers, ensure_ascii=False, indent=2), encoding="utf-8")


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "未知"


def estimate_folder_payload(path: str) -> tuple[int, int, int, int]:
    raw_bytes = 0
    files = 0
    dirs = 1
    tar_bytes = 1024 + 512
    for root, dir_names, file_names in os.walk(path):
        dirs += len(dir_names)
        tar_bytes += len(dir_names) * 512
        for name in file_names:
            fp = os.path.join(root, name)
            try:
                size = os.path.getsize(fp)
            except OSError:
                continue
            files += 1
            raw_bytes += size
            tar_bytes += 512 + math.ceil(size / 512) * 512
    return raw_bytes, tar_bytes, files, dirs


def authenticate_socket(sock: socket.socket, token: str, timeout: float = CONNECT_TIMEOUT) -> None:
    token_bytes = token.encode("utf-8")
    if not token_bytes:
        return
    if len(token_bytes) > 1024:
        raise ValueError("接收口令过长")
    previous_timeout = sock.gettimeout()
    try:
        sock.settimeout(timeout)
        sock.sendall(AUTH_MAGIC + struct.pack(">H", len(token_bytes)) + token_bytes)
        if sock.recv(1) != b"\x01":
            raise PermissionError("接收口令错误或接收端版本过旧")
    finally:
        sock.settimeout(previous_timeout)


def probe_receiver(host: str, port: int = PORT, timeout: float = CONNECT_TIMEOUT, token: str = "") -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            xrt_proto.send_negotiate_request(sock, xrt_proto.MODE_PROBE, "probe", 0, 0, token)
            data_port, _resume_offset = xrt_proto.read_ack_frame(sock)
            if data_port != 0:
                return False, "接收端探活应答无效"
            return True, "连接成功（支持快速续传、掉线自动恢复）" if _resume_offset == 1 else "连接成功（旧版接收端，请更新以启用快速续传）"
    except (ConnectionError, xrt_proto.ProtocolError):
        # Older receivers do not understand negotiation; preserve their probe.
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                authenticate_socket(sock, token, timeout)
                return True, "连接成功（旧版接收端）"
        except (OSError, PermissionError) as exc:
            return False, str(exc)
    except (PermissionError, socket.timeout) as exc:
        return False, str(exc) or "连接超时"
    except ConnectionRefusedError:
        return False, "接收端未启动或端口被拒绝"
    except OSError as exc:
        return False, str(exc)


from rocket_ui import ProductHeader, add_footer


class ModernProgressBar(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget):
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.columnconfigure(0, weight=1)
        self.bar = ctk.CTkProgressBar(
            self,
            height=14,
            corner_radius=7,
            border_width=1,
            fg_color="#222631",
            border_color="#41485e",
            progress_color=ACCENT_2,
        )
        self.bar.grid(row=0, column=0, sticky="ew", pady=5)
        self.label = ctk.CTkLabel(
            self,
            text="0.0%",
            text_color=TEXT,
            font=FONT_BOLD,
            width=62,
            anchor="e",
        )
        self.label.grid(row=0, column=1, padx=(12, 0))
        self.set(0)

    def set(self, pct: float) -> None:
        value = max(0.0, min(100.0, pct))
        self.bar.set(value / 100)
        self.label.configure(text=f"{value:.1f}%")


class ReceiverList(ctk.CTkScrollableFrame):
    def __init__(self, parent: tk.Widget, on_select, on_open):
        super().__init__(
            parent,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_fg_color="transparent",
            scrollbar_button_color="#4c5269",
            scrollbar_button_hover_color=ACCENT,
        )
        self.on_select = on_select
        self.on_open = on_open
        self.rows: list[ctk.CTkFrame] = []
        self.selected_index: int | None = None
        self.columnconfigure(0, weight=1)

    def set_items(self, receivers: list[dict[str, str]], statuses: dict[str, str], selected: int | None) -> None:
        for row in self.rows:
            row.destroy()
        self.rows.clear()
        self.selected_index = None

        if not receivers:
            empty = ctk.CTkLabel(
                self,
                text="暂无接收方",
                text_color=MUTED,
                font=FONT,
            )
            empty.grid(row=0, column=0, sticky="ew", pady=28)
            self.rows.append(empty)
            return

        for index, receiver in enumerate(receivers):
            status = statuses.get(endpoint_key(receiver), "-")
            status_text = "未检测" if status == "-" else status
            status_color = ACCENT_2 if status == "在线" else WARN if status == "离线" else MUTED
            row = ctk.CTkFrame(
                self,
                fg_color="#272b36",
                corner_radius=7,
                border_width=1,
                border_color=BORDER,
                cursor="hand2",
            )
            row.grid(row=index, column=0, sticky="ew", pady=(0, 8), padx=(0, 4))
            row.columnconfigure(0, weight=1)

            name_label = ctk.CTkLabel(
                row,
                text=receiver["name"],
                text_color=TEXT,
                font=FONT_BOLD,
                anchor="w",
            )
            name_label.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=(9, 0))
            ip_label = ctk.CTkLabel(
                row,
                text=endpoint_key(receiver),
                text_color=TEXT_DIM,
                font=FONT_MONO,
                anchor="w",
            )
            ip_label.grid(row=1, column=0, sticky="ew", padx=(12, 8), pady=(0, 9))
            status_label = ctk.CTkLabel(
                row,
                text=f"●  {status_text}",
                text_color=status_color,
                fg_color="#313645",
                corner_radius=6,
                font=FONT,
                width=72,
            )
            status_label.grid(row=0, column=1, rowspan=2, padx=10, pady=10)

            for widget in (row, name_label, ip_label, status_label):
                widget.bind("<Button-1>", lambda _event, i=index: self.select(i), add="+")
                widget.bind("<Double-Button-1>", lambda _event, i=index: self._open(i), add="+")
            self.rows.append(row)

        target = 0 if selected is None else min(max(selected, 0), len(receivers) - 1)
        self.select(target, notify=False)

    def select(self, index: int, notify: bool = True) -> None:
        if index < 0 or index >= len(self.rows) or not isinstance(self.rows[index], ctk.CTkFrame):
            return
        self.selected_index = index
        for row_index, row in enumerate(self.rows):
            if not isinstance(row, ctk.CTkFrame):
                continue
            row.configure(
                fg_color="#3b3e59" if row_index == index else "#272b36",
                border_color=ACCENT if row_index == index else BORDER,
            )
        if notify:
            self.on_select()

    def _open(self, index: int) -> None:
        self.select(index)
        self.on_open()


class ProgressBar(tk.Canvas):
    def __init__(self, parent: tk.Widget):
        super().__init__(parent, height=24, bg=SURFACE_2, highlightthickness=0)
        self._pct = 0.0
        self.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    def set(self, pct: float) -> None:
        self._pct = max(0.0, min(100.0, pct))
        self._draw()

    def _round_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs) -> None:
        self.create_arc(x1, y1, x1 + radius * 2, y1 + radius * 2, start=90, extent=90, **kwargs)
        self.create_arc(x2 - radius * 2, y1, x2, y1 + radius * 2, start=0, extent=90, **kwargs)
        self.create_arc(x2 - radius * 2, y2 - radius * 2, x2, y2, start=270, extent=90, **kwargs)
        self.create_arc(x1, y2 - radius * 2, x1 + radius * 2, y2, start=180, extent=90, **kwargs)
        self.create_rectangle(x1 + radius, y1, x2 - radius, y2, **kwargs)
        self.create_rectangle(x1, y1 + radius, x2, y2 - radius, **kwargs)

    def _draw(self) -> None:
        self.delete("all")
        width = max(120, self.winfo_width())
        height = max(18, self.winfo_height())
        radius = height // 2
        self._round_rect(0, 0, width, height, radius, fill="#222631", outline="#41485e")
        fill_width = int(width * self._pct / 100)
        if fill_width > 2:
            self._round_rect(0, 0, max(fill_width, height), height, radius, fill=ACCENT_2, outline="")
            if fill_width > height * 2:
                self.create_rectangle(10, 4, max(12, fill_width - 10), height - 4, fill="#78efb6", outline="")
        self.create_text(width // 2, height // 2, text=f"{self._pct:.1f}%", fill=TEXT, font=FONT_BOLD)


class SpeedGraph(tk.Canvas):
    def __init__(self, parent: tk.Widget):
        super().__init__(parent, height=112, bg=SURFACE, highlightthickness=0)
        self._samples = collections.deque([0.0] * SPEED_SAMPLES, maxlen=SPEED_SAMPLES)
        self.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    def reset(self) -> None:
        self._samples = collections.deque([0.0] * SPEED_SAMPLES, maxlen=SPEED_SAMPLES)
        self._draw()

    def push(self, speed: float) -> None:
        self._samples.append(max(0.0, speed))
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = max(160, self.winfo_width())
        height = max(70, self.winfo_height())
        pad = 10
        usable_w = width - pad * 2
        usable_h = height - pad * 2
        self.create_rectangle(0, 0, width, height, fill=SURFACE_3, outline=BORDER)
        peak = max(self._samples) or 1.0

        for i in range(1, 4):
            y = height - pad - usable_h * i / 3
            self.create_line(pad, y, width - pad, y, fill="#3b4154", dash=(4, 4))

        points: list[tuple[float, float]] = []
        sample_count = len(self._samples)
        for i, value in enumerate(self._samples):
            x = pad + usable_w * i / max(1, sample_count - 1)
            y = height - pad - usable_h * (value / peak)
            points.append((x, y))

        if points:
            poly = [(pad, height - pad)] + points + [(width - pad, height - pad)]
            self.create_polygon([coord for point in poly for coord in point], fill="#0d2b2b", outline="")
        if len(points) > 1:
            self.create_line([coord for point in points for coord in point], fill=HEADER_GLOW, width=2, smooth=True)
        self.create_text(
            width - pad,
            pad,
            anchor="ne",
            text=f"{self._samples[-1]:.1f} MB/s",
            fill=ACCENT_2,
            font=FONT_BOLD,
        )
        self.create_text(pad, pad, anchor="nw", text=f"峰值 {peak:.1f} MB/s", fill=TEXT_DIM, font=FONT)


class ReceiverDialog(ctk.CTkToplevel):
    def __init__(self, parent: tk.Tk, title: str, name: str = "", ip: str = "", port: str = str(PORT), token: str = ""):
        super().__init__(parent)
        self.result: dict[str, str] | None = None
        self.title(title)
        self.geometry("460x360")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(
            self,
            text=title,
            text_color=TEXT,
            font=("Microsoft YaHei UI", 16, "bold"),
            anchor="w",
        ).pack(fill="x", padx=24, pady=(22, 14))
        form = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        form.pack(fill="x", padx=24)

        ctk.CTkLabel(form, text="备注名称", text_color=TEXT_DIM, font=FONT).grid(row=0, column=0, sticky="w", pady=6)
        self.name_var = tk.StringVar(value=name)
        ctk.CTkEntry(
            form,
            textvariable=self.name_var,
            fg_color=INPUT,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            corner_radius=7,
            font=FONT,
            height=38,
        ).grid(row=0, column=1, sticky="ew", padx=(16, 0), pady=6)

        ctk.CTkLabel(form, text="IP / 主机名", text_color=TEXT_DIM, font=FONT).grid(row=1, column=0, sticky="w", pady=6)
        self.ip_var = tk.StringVar(value=ip)
        ip_entry = ctk.CTkEntry(
            form,
            textvariable=self.ip_var,
            fg_color=INPUT,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            corner_radius=7,
            font=FONT_MONO,
            height=38,
        )
        ip_entry.grid(row=1, column=1, sticky="ew", padx=(16, 0), pady=6)
        ctk.CTkLabel(form, text="端口", text_color=TEXT_DIM, font=FONT).grid(row=2, column=0, sticky="w", pady=6)
        self.port_var = tk.StringVar(value=str(port))
        ctk.CTkEntry(form, textvariable=self.port_var, fg_color=INPUT, text_color=TEXT, border_color=BORDER,
                     border_width=1, corner_radius=7, font=FONT_MONO, height=38, width=100).grid(
                         row=2, column=1, sticky="w", padx=(16, 0), pady=6)
        ctk.CTkLabel(form, text="接收口令（可选）", text_color=TEXT_DIM, font=FONT).grid(row=3, column=0, sticky="w", pady=6)
        self.token_var = tk.StringVar(value=token)
        ctk.CTkEntry(form, textvariable=self.token_var, show="*", fg_color=INPUT, text_color=TEXT, border_color=BORDER,
                     border_width=1, corner_radius=7, font=FONT_MONO, height=38).grid(
                         row=3, column=1, sticky="ew", padx=(16, 0), pady=6)
        form.columnconfigure(1, weight=1)

        actions = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        actions.pack(fill="x", padx=24, pady=(18, 0))
        self._button(actions, "取消", self.destroy, BORDER).pack(side="right", padx=(8, 0))
        self._button(actions, "保存", self._confirm, ACCENT).pack(side="right")

        ip_entry.focus_set()
        self.bind("<Return>", lambda _event: self._confirm())
        self.bind("<Escape>", lambda _event: self.destroy())
        apply_windows_chrome(self)
        self.wait_window()

    def _button(self, parent: tk.Widget, text: str, command, bg: str) -> tk.Button:
        return ModernButton(
            parent,
            text=text,
            command=command,
            fg_color=bg,
            hover_color=ACCENT_HOVER if bg == ACCENT else blend_color(bg, TEXT, 0.12),
            text_color="#ffffff",
            corner_radius=7,
            border_width=1 if bg == BORDER else 0,
            border_color=BORDER_BRIGHT,
            width=92,
            height=38,
            cursor="hand2",
            font=FONT_BOLD,
        )

    def _confirm(self) -> None:
        name = self.name_var.get().strip()
        ip = self.ip_var.get().strip()
        if not name:
            messagebox.showwarning("提示", "请输入接收方名称。", parent=self)
            return
        if not validate_host(ip):
            messagebox.showwarning("提示", "请输入有效的 IP 地址或主机名。", parent=self)
            return
        port = self.port_var.get().strip()
        if not validate_port(port):
            messagebox.showwarning("提示", f"请输入 {MIN_PORT}-{MAX_PORT} 范围内的端口。", parent=self)
            return
        self.result = {"name": name, "ip": ip, "port": port, "token": self.token_var.get()}
        self.destroy()


class SocketProgressWriter:
    def __init__(self, sock: socket.socket, cancel_event: threading.Event, on_progress, buffer_size: int = CHUNK):
        self.sock = sock
        self.cancel_event = cancel_event
        self.on_progress = on_progress
        self.buffer_size = buffer_size
        self.buffer = bytearray()
        self.sent = 0

    def write(self, data: bytes) -> int:
        if self.cancel_event.is_set():
            raise TransferCancelled("用户已取消传输")
        self.buffer.extend(data)
        if len(self.buffer) >= self.buffer_size:
            self.flush()
        return len(data)

    def flush(self) -> None:
        if self.cancel_event.is_set():
            raise TransferCancelled("用户已取消传输")
        if not self.buffer:
            return
        self.sock.sendall(self.buffer)
        self.sent += len(self.buffer)
        self.buffer.clear()
        self.on_progress(self.sent)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("小火箭 · 大文件传输助手")
        self.root.geometry("1040x790")
        self.root.minsize(1040, 790)
        if isinstance(self.root, ctk.CTk):
            self.root.configure(fg_color=BG)
        else:
            self.root.configure(bg=BG)

        self.receivers = load_receivers()
        self.receiver_status: dict[str, str] = {}
        self.cancel_event = threading.Event()
        self.active_socket = None
        self.active_thread: threading.Thread | None = None
        self.is_sending = False
        self.testing_all = False
        self.path_scan_token = 0

        self._configure_style()
        self._build()
        self._refresh_receivers()
        self._log("发送端已就绪。本机 IP: " + get_local_ip())

    def _configure_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=INPUT,
            foreground=TEXT,
            fieldbackground=INPUT,
            bordercolor=BORDER_BRIGHT,
            lightcolor=INPUT,
            darkcolor=INPUT,
            rowheight=36,
            font=FONT,
        )
        style.configure(
            "Treeview.Heading",
            background=SURFACE_2,
            foreground="#d7dee9",
            bordercolor=BORDER_BRIGHT,
            relief="flat",
            font=FONT_BOLD,
            padding=(10, 8),
        )
        style.configure(
            "Vertical.TScrollbar",
            background="#33495b",
            troughcolor=INPUT,
            bordercolor=INPUT,
            arrowcolor=TEXT_DIM,
        )
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.map("Vertical.TScrollbar", background=[("active", ACCENT)])

    def _build(self) -> None:
        header = ProductHeader(
            self.root,
            "小火箭",
            "局域网 / 公网直连  ·  文件与文件夹",
            f"本机 {get_local_ip()} · {PORT}",
            "TX",
        )
        header.pack(fill="x")
        update, update_settings = add_footer(self.root, "发送端")
        from rocket_update import attach
        self.updater = attach(self.root, 'sender', update, update_settings, self)
        self.header_status = header

        root_body = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        root_body.pack(fill="both", expand=True, padx=18, pady=16)
        root_body.columnconfigure(0, minsize=320, weight=0)
        root_body.columnconfigure(1, weight=1, minsize=440)
        root_body.rowconfigure(0, weight=1)

        left = self._panel(root_body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right = self._panel(root_body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)

        self._build_receivers(left)
        self._build_send_area(right)

    def _panel(self, parent: tk.Widget) -> tk.Frame:
        return ctk.CTkFrame(
            parent,
            fg_color=SURFACE,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )

    def _section_title(self, parent: tk.Widget, text: str) -> None:
        row = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=0)
        row.pack(fill="x", padx=18, pady=(18, 12))
        ctk.CTkFrame(row, width=4, height=20, fg_color=HEADER_GLOW, corner_radius=2).pack(side="left", padx=(0, 10))
        ModernLabel(row, text=text, fg_color=SURFACE, text_color=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        ModernLabel(row, text="设备列表", fg_color=SURFACE, text_color=MUTED, font=("Consolas", 8, "bold")).pack(side="right", pady=2)

    def _button(self, parent: tk.Widget, text: str, command, bg: str = BORDER, fg: str = "#ffffff") -> tk.Button:
        return ModernButton(
            parent,
            text=text,
            command=command,
            fg_color=bg,
            hover_color=ACCENT_HOVER if bg == ACCENT else blend_color(bg, TEXT, 0.12),
            text_color=fg,
            text_color_disabled="#697386",
            corner_radius=7,
            border_width=1 if bg == BORDER else 0,
            border_color=BORDER_BRIGHT,
            width=88,
            height=38,
            cursor="hand2",
            font=FONT_BOLD if bg == ACCENT else FONT,
        )

    def _build_receivers(self, parent: tk.Frame) -> None:
        self._section_title(parent, "接收方")

        self.recv_list = ReceiverList(parent, self._update_selected_label, self.edit_receiver)
        self.recv_list.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        row = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=0)
        row.pack(fill="x", padx=16, pady=(0, 9))
        self._button(row, "+ 添加", self.add_receiver, ACCENT).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._button(row, "编辑", self.edit_receiver).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._button(row, "删除", self.delete_receiver, DANGER).pack(side="left", fill="x", expand=True)

        test_row = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=0)
        test_row.pack(fill="x", padx=16, pady=(0, 16))
        self.test_btn = self._button(test_row, "⌁  测试当前", self.test_receiver)
        self.test_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.test_all_btn = self._button(test_row, "全部测试", self.test_all_receivers, ACCENT)
        self.test_all_btn.pack(side="left", fill="x", expand=True)

        ctk.CTkFrame(parent, height=1, fg_color=BORDER, corner_radius=0).pack(fill="x", padx=16)
        self.selected_label = ModernLabel(parent, text="未选择接收方", fg_color=SURFACE, text_color=TEXT_DIM, font=FONT, anchor="w")
        self.selected_label.pack(fill="x", padx=18, pady=14)

    def _build_send_area(self, parent: tk.Frame) -> None:
        top = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=0)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ModernLabel(top, text="发送内容", fg_color=SURFACE, text_color=TEXT, font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=18, pady=(18, 12)
        )
        ModernLabel(top, text="01 / 内容", fg_color=SURFACE, text_color=MUTED, font=("Consolas", 8, "bold")).grid(
            row=0, column=1, sticky="e", padx=18, pady=(18, 12)
        )

        path_box = ctk.CTkFrame(
            parent,
            fg_color=SURFACE_3,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        path_box.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))
        path_box.columnconfigure(1, weight=1)
        ModernLabel(path_box, text="选择传输内容", fg_color=SURFACE_3, text_color=MUTED, font=("Consolas", 8, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(11, 5)
        )

        file_icon = tk.Canvas(path_box, width=34, height=34, bg=SURFACE_3, highlightthickness=0)
        file_icon.grid(row=1, column=0, padx=(12, 8), pady=(0, 12), sticky="n")
        file_icon.create_rectangle(8, 4, 25, 29, fill="#142735", outline="#4ba6ff", width=1)
        file_icon.create_polygon(19, 4, 25, 10, 19, 10, fill="#4ba6ff", outline="")
        file_icon.create_line(12, 16, 21, 16, fill=TEXT_DIM, width=1)
        file_icon.create_line(12, 20, 21, 20, fill=TEXT_DIM, width=1)
        file_icon.create_line(12, 24, 18, 24, fill=HEADER_GLOW, width=2)

        path_row = ctk.CTkFrame(path_box, fg_color=SURFACE_3, corner_radius=0)
        path_row.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(0, 12))
        path_row.columnconfigure(0, weight=1)
        self.path_var = tk.StringVar()
        ctk.CTkEntry(
            path_row,
            textvariable=self.path_var,
            fg_color=INPUT,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            corner_radius=7,
            font=FONT,
            height=40,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._button(path_row, "文件", self.browse_file).grid(row=0, column=1, padx=(0, 6))
        self._button(path_row, "文件夹", self.browse_folder).grid(row=0, column=2)

        info_row = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=0)
        info_row.grid(row=2, column=0, sticky="nsew", padx=16)
        info_row.columnconfigure(0, weight=1)
        info_row.rowconfigure(3, weight=1)

        file_info_box = ctk.CTkFrame(
            info_row,
            fg_color=SURFACE_3,
            corner_radius=8,
            border_width=1,
            border_color=BORDER,
        )
        file_info_box.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        file_info_box.columnconfigure(0, weight=1)
        ModernLabel(file_info_box, text="任务摘要", fg_color=SURFACE_3, text_color=MUTED, font=("Consolas", 8, "bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(9, 2)
        )
        self.file_info = ModernLabel(
            file_info_box,
            text="请选择文件或文件夹",
            fg_color=SURFACE_3,
            text_color=TEXT_DIM,
            font=FONT,
            anchor="w",
            wraplength=520,
            justify="left",
        )
        self.file_info.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))

        option_row = ctk.CTkFrame(info_row, fg_color=SURFACE, corner_radius=0)
        option_row.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.auto_test_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            option_row,
            text="发送前测试连接",
            variable=self.auto_test_var,
            bg_color="transparent",
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            border_color=BORDER_BRIGHT,
            checkmark_color="#ffffff",
            text_color=TEXT_DIM,
            corner_radius=4,
            border_width=1,
            checkbox_width=18,
            checkbox_height=18,
            font=FONT,
        ).pack(side="left")

        self.resume_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            option_row, text="断点续传", variable=self.resume_var,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=TEXT_DIM,
            border_color=BORDER_BRIGHT, checkbox_width=18, checkbox_height=18,
            border_width=1, corner_radius=4, font=FONT,
        ).pack(side="right")

        progress_box = ctk.CTkFrame(
            info_row,
            fg_color=SURFACE_2,
            corner_radius=8,
            border_width=1,
            border_color=BORDER,
        )
        progress_box.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        progress_box.columnconfigure(0, weight=1)
        progress_head = ctk.CTkFrame(progress_box, fg_color=SURFACE_2, corner_radius=0)
        progress_head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        progress_head.columnconfigure(0, weight=1)
        self.status_label = ModernLabel(
            progress_head, text="等待发送", fg_color=SURFACE_2, text_color=TEXT_DIM, font=("Microsoft YaHei UI", 11, "bold"), anchor="w"
        )
        self.status_label.grid(row=0, column=0, sticky="w")
        ModernLabel(progress_head, text="02 / 传输状态", fg_color=SURFACE_2, text_color=MUTED, font=("Consolas", 8, "bold")).grid(
            row=0, column=1, sticky="e"
        )
        self.pbar = ModernProgressBar(progress_box)
        self.pbar.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))

        stats = ctk.CTkFrame(progress_box, fg_color=SURFACE_2, corner_radius=0)
        stats.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 14))
        for i in range(4):
            stats.columnconfigure(i, weight=1)
        self.lbl_sent = self._stat(stats, 0, "已发送")
        self.lbl_speed = self._stat(stats, 1, "速度")
        self.lbl_eta = self._stat(stats, 2, "剩余")
        self.lbl_elapsed = self._stat(stats, 3, "耗时")

        activity_row = ctk.CTkFrame(info_row, fg_color=SURFACE, corner_radius=0)
        activity_row.grid(row=3, column=0, sticky="nsew", pady=(0, 14))
        activity_row.columnconfigure(0, weight=1)
        activity_row.columnconfigure(1, minsize=200, weight=0)
        activity_row.rowconfigure(1, weight=1)

        self.graph = SpeedGraph(activity_row)
        self.graph.grid(row=1, column=0, sticky="nsew", padx=(0, 12))

        log_side = ctk.CTkFrame(activity_row, fg_color=SURFACE, width=200, corner_radius=0)
        log_side.grid(row=1, column=1, sticky="nsew")
        log_side.grid_propagate(False)
        log_side.rowconfigure(1, weight=1)
        log_side.columnconfigure(0, weight=1)
        ModernLabel(log_side, text="日志", fg_color=SURFACE, text_color=TEXT, font=FONT_BOLD).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        log_frame = ctk.CTkFrame(
            log_side,
            fg_color=INPUT,
            corner_radius=7,
            border_width=1,
            border_color=BORDER_BRIGHT,
        )
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(
            log_frame,
            width=26,
            height=6,
            fg_color=INPUT,
            text_color=TEXT_DIM,
            corner_radius=6,
            border_width=0,
            scrollbar_button_color="#4c5269",
            scrollbar_button_hover_color=ACCENT,
            state="disabled",
            font=FONT_MONO,
        )
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)

        action_row = ctk.CTkFrame(info_row, fg_color=SURFACE, corner_radius=0)
        action_row.grid(row=4, column=0, sticky="ew", pady=(0, 14))
        self.cancel_btn = self._button(action_row, "取消发送", self.cancel_send, DANGER)
        self.cancel_btn.pack(side="right")
        self.cancel_btn.config(state="disabled")
        self.send_btn = self._button(action_row, "开始发送  →", self.start_send, ACCENT)
        self.send_btn.configure(width=148, height=44)
        self.send_btn.pack(side="right", padx=(0, 8))

    def _stat(self, parent: tk.Widget, col: int, title: str) -> tk.Label:
        box = ctk.CTkFrame(
            parent,
            fg_color=SURFACE_3,
            corner_radius=7,
            border_width=1,
            border_color="#3b4154",
        )
        box.grid(row=0, column=col, sticky="ew", padx=6)
        ctk.CTkFrame(box, height=2, fg_color=HEADER_GLOW, corner_radius=0).pack(fill="x")
        ModernLabel(box, text=title, fg_color=SURFACE_3, text_color=MUTED, font=FONT, anchor="w").pack(
            fill="x", padx=11, pady=(8, 0)
        )
        value = ModernLabel(
            box,
            text="-",
            fg_color=SURFACE_3,
            text_color=TEXT,
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        )
        value.pack(fill="x", padx=11, pady=(3, 10))
        return value

    def _refresh_receivers(self, selected: int | None = None) -> None:
        self.recv_list.set_items(self.receivers, self.receiver_status, selected)
        self._update_selected_label()

    def _selected_index(self) -> int | None:
        return self.recv_list.selected_index

    def _selected_receiver(self) -> dict[str, str] | None:
        index = self._selected_index()
        if index is None or index >= len(self.receivers):
            return None
        return self.receivers[index]

    def _update_selected_label(self) -> None:
        receiver = self._selected_receiver()
        if receiver:
            self.selected_label.config(text=f"当前接收方：{receiver['name']}  {endpoint_key(receiver)}", fg=TEXT_DIM)
        else:
            self.selected_label.config(text="未选择接收方", fg=TEXT_DIM)

    def add_receiver(self) -> None:
        dialog = ReceiverDialog(self.root, "添加接收方")
        if dialog.result:
            self.receivers.append(dialog.result)
            save_receivers(self.receivers)
            self._refresh_receivers(len(self.receivers) - 1)
            self._log(f"已添加接收方：{dialog.result['name']} ({dialog.result['ip']})")

    def edit_receiver(self) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("提示", "请先选择一个接收方。", parent=self.root)
            return
        old = self.receivers[index]
        dialog = ReceiverDialog(self.root, "编辑接收方", old["name"], old["ip"], old.get("port", str(PORT)), old.get("token", ""))
        if dialog.result:
            self.receivers[index] = dialog.result
            save_receivers(self.receivers)
            self._refresh_receivers(index)
            self._log(f"已更新接收方：{dialog.result['name']} ({dialog.result['ip']})")

    def delete_receiver(self) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo("提示", "请先选择一个接收方。", parent=self.root)
            return
        receiver = self.receivers[index]
        if messagebox.askyesno("确认删除", f"删除“{receiver['name']}”？", parent=self.root):
            self.receivers.pop(index)
            save_receivers(self.receivers)
            self._refresh_receivers(max(0, index - 1) if self.receivers else None)
            self._log(f"已删除接收方：{receiver['name']}")

    def test_receiver(self) -> None:
        if self.testing_all:
            return
        receiver = self._selected_receiver()
        if not receiver:
            messagebox.showinfo("提示", "请先选择一个接收方。", parent=self.root)
            return
        self.test_btn.config(state="disabled")
        self.test_all_btn.config(state="disabled")
        port = int(receiver.get("port", PORT))
        self._log(f"正在测试连接：{receiver['name']} ({receiver['ip']}:{port})")

        def worker() -> None:
            ok, detail = probe_receiver(receiver["ip"], port=port, token=receiver.get("token", ""))
            status = "在线" if ok else "离线"
            self.receiver_status[endpoint_key(receiver)] = status
            self.root.after(0, lambda: self._finish_test(receiver, ok, detail))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_test(self, receiver: dict[str, str], ok: bool, detail: str) -> None:
        self.test_btn.config(state="normal")
        self.test_all_btn.config(state="normal")
        self._refresh_receivers(self._selected_index())
        if ok:
            self._log(f"连接成功：{receiver['name']}，{detail}。")
        else:
            self._log(f"连接失败：{receiver['name']}，{detail}")

    def test_all_receivers(self) -> None:
        if self.testing_all:
            return
        if not self.receivers:
            messagebox.showinfo("提示", "当前没有可测试的接收方。", parent=self.root)
            return

        self.testing_all = True
        self.test_btn.config(state="disabled")
        self.test_all_btn.config(state="disabled", text="测试中...")
        selected = self._selected_index()
        receivers = list(self.receivers)
        total = len(receivers)
        completed = 0
        online = 0
        self._log(f"开始测试全部接收方，共 {total} 个。")

        def worker(receiver: dict[str, str]) -> None:
            nonlocal completed, online
            ok, detail = probe_receiver(receiver["ip"], port=int(receiver.get("port", PORT)), token=receiver.get("token", ""))

            def apply() -> None:
                nonlocal completed, online
                self.receiver_status[endpoint_key(receiver)] = "在线" if ok else "离线"
                self._refresh_receivers(selected)
                if ok:
                    online += 1
                    self._log(f"在线：{receiver['name']} ({receiver['ip']})，{detail}")
                else:
                    self._log(f"离线：{receiver['name']} ({receiver['ip']})，{detail}")
                completed += 1
                if completed >= total:
                    self.testing_all = False
                    self.test_btn.config(state="normal")
                    self.test_all_btn.config(state="normal", text="全部测试")
                    self._log(f"全部测试完成：{online}/{total} 个接收方在线。")

            self.root.after(0, apply)

        for receiver in receivers:
            threading.Thread(target=worker, args=(receiver,), daemon=True).start()

    def browse_file(self) -> None:
        path = filedialog.askopenfilename(title="选择要发送的文件")
        if path:
            self.path_var.set(path)
            self._inspect_path_async(path)

    def browse_folder(self) -> None:
        path = filedialog.askdirectory(title="选择要发送的文件夹")
        if path:
            self.path_var.set(path)
            self._inspect_path_async(path)

    def _inspect_path_async(self, path: str) -> None:
        if hasattr(self, 'size_scan_cancel'):
            self.size_scan_cancel.set()
        scan_cancel = self.size_scan_cancel = threading.Event()
        state = self.size_state = {'path': path, 'total': None}
        self.path_scan_token += 1
        token = self.path_scan_token
        self.file_info.config(text="正在统计内容...", fg=TEXT_DIM)

        def worker() -> None:
            try:
                if os.path.isdir(path):
                    raw, archive_size = folder_size(path, scan_cancel)
                    state['total'] = archive_size
                    text = f"文件夹 · {Path(path).name} · 总大小 {human_size(raw)}"
                else:
                    text = f"文件 · {Path(path).name} · {human_size(os.path.getsize(path))}"
                color = TEXT_DIM
            except Exception as exc:
                text = f"无法读取路径：{exc}"
                color = DANGER

            def apply() -> None:
                if token == self.path_scan_token:
                    self.file_info.config(text=text, fg=color)
                    if self.is_sending and getattr(self, 'transfer_size_state', None) is state and getattr(self, 'last_transfer_progress', None):
                        self._ui_update(*self.last_transfer_progress)

            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def start_send(self) -> None:
        if getattr(self, '_updating', False):
            return
        if self.is_sending:
            return
        path = self.path_var.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showwarning("提示", "请先选择有效的文件或文件夹。", parent=self.root)
            return
        receiver = self._selected_receiver()
        if not receiver:
            messagebox.showwarning("提示", "请先选择接收方。", parent=self.root)
            return

        self.cancel_event.clear()
        if getattr(self, 'size_state', {}).get('path') != path:
            self._inspect_path_async(path)
        self.transfer_size_state = self.size_state
        self.last_transfer_progress = None
        self.transfer_resume = self.resume_var.get()
        self.transfer_auto_test = self.auto_test_var.get()
        self.is_sending = True
        self.send_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.status_label.config(text="准备发送...", fg=TEXT_DIM)
        self.pbar.set(0)
        self.graph.reset()
        for label in (self.lbl_sent, self.lbl_speed, self.lbl_eta, self.lbl_elapsed):
            label.config(text="-", fg=TEXT)

        self._log(f"开始任务：发送到 {receiver['name']} ({receiver['ip']}:{receiver.get('port', PORT)})")
        self.active_thread = threading.Thread(target=self._send_worker, args=(path, receiver), daemon=True)
        self.active_thread.start()

    def cancel_send(self) -> None:
        if not self.is_sending:
            return
        self.cancel_event.set()
        if self.active_socket is not None:
            try:
                self.active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.cancel_btn.config(state="disabled")
        self.status_label.config(text="正在取消...", fg=WARN)
        self._log("正在取消当前传输。")

    def _send_worker(self, path: str, receiver: dict[str, str]) -> None:
        try:
            host, port = receiver["ip"], int(receiver.get("port", PORT))
            token = receiver.get("token", "")
            old_receiver = False
            if self.transfer_auto_test:
                ok, detail = probe_receiver(host, port, CONNECT_TIMEOUT, token)
                if not ok and not self.transfer_resume:
                    raise ConnectionError(detail)
                old_receiver = "旧版" in detail
            if not self.transfer_resume:
                self._send_legacy_transfer(path, host, port, token)
                return
            if old_receiver:
                self.root.after(0, self._log, "对方使用旧版；任务保持等待，请对方运行支持续传的新版接收端")
            is_dir = os.path.isdir(path)
            name = Path(path).name
            mode = MODE_FOLDER if is_dir else MODE_FILE
            self.root.after(0, self._set_status, "正在统计文件大小…", TEXT_DIM)
            scan_started = time.monotonic()
            last_scan = [0.0]
            def scan_progress(count):
                if time.monotonic() - last_scan[0] >= PROGRESS_INTERVAL:
                    self.root.after(0, self._set_status, f"正在统计文件大小…", TEXT_DIM)
                    last_scan[0] = time.monotonic()
            payload_path = LazyFolderPayload(path, self.cancel_event) if is_dir else StreamPayload(path, self.cancel_event, scan_progress)
            total, digest = UNKNOWN_TOTAL if is_dir else payload_path.total, payload_path.digest
            mode = xrt_proto.MODE_LAZY_FOLDER if is_dir else xrt_proto.MODE_STREAM_FILE
            self.root.after(0, self._log, f"开始直传：{name}，边遍历边发送，无需等待目录扫描" if is_dir else f"发送：{name} · {human_size(total)}")
            legacy = not self.transfer_resume
            retry = 0
            while True:
                if self.cancel_event.is_set():
                    raise TransferCancelled("用户已取消传输，已接收的断点保留")
                negotiated = False
                try:
                    with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as sock:
                        self.active_socket = sock
                        self._tune_socket(sock)
                        xrt_proto.enable_disconnect_detection(sock)
                        if legacy:
                            self._send_header(sock, mode, name, total, token)
                            offset = 0
                        else:
                            try:
                                xrt_proto.send_negotiate_request(sock, mode, name, total, 0, token, digest)
                                sock.settimeout(20)
                                _port, offset = xrt_proto.read_ack(sock)
                            except (OSError, ConnectionError, xrt_proto.ProtocolError, xrt_proto.NegotiateRejected):
                                raise
                            negotiated = True
                        if offset > total:
                            raise ValueError("接收端断点超出文件大小")
                        self.progress_offset = offset
                        if offset:
                            self.root.after(0, self._log, f"从 {human_size(offset)} 继续发送")
                        sock.settimeout(20)
                        started_at = time.time()
                        sent, last_ui = offset, 0.0
                        self._ui_update(sent, total, started_at)
                        for chunk in self._payload_chunks(payload_path, offset, total):
                            if self.cancel_event.is_set():
                                raise TransferCancelled("用户已取消传输，已接收的断点保留")
                            if not chunk:
                                raise IOError("源文件在传输过程中发生变化")
                            if negotiated:
                                sock.sendall(xrt_proto.DATA_MAGIC + struct.pack(">QI", sent, len(chunk)))
                            sock.sendall(chunk)
                            if negotiated:
                                sock.sendall(hashlib.sha256(chunk).digest())
                            sent += len(chunk)
                            if time.monotonic() - last_ui >= PROGRESS_INTERVAL:
                                self._ui_update(sent, total, started_at)
                                last_ui = time.monotonic()
                        if negotiated and is_dir:
                            sock.sendall(xrt_proto.DATA_MAGIC + struct.pack('>QI', sent, 0) + hashlib.sha256(b'').digest())
                        self._ui_update(sent, sent, started_at)
                        self.root.after(0, self._done, True, "发送完成（数据已全部发出，未等待接收端确认）。" if negotiated else "发送完成（原传输方式）。")
                        return
                except PermissionError:
                    raise
                except (OSError, ConnectionError, xrt_proto.ProtocolError, xrt_proto.NegotiateRejected) as exc:
                    if self.cancel_event.is_set():
                        raise TransferCancelled("用户已取消传输，已接收的断点保留") from exc
                    if legacy:
                        raise
                    retry += 1
                    self.root.after(0, self._set_status, f"等待对方恢复 · 自动重连第 {retry} 次…", WARN)
                    if retry == 1 or retry % 6 == 0:
                        self.root.after(0, self._log, f"连接不可用：{exc}；任务保留，恢复后自动续传（需新版接收端），直到完成或主动取消")
                    if self.cancel_event.wait(min(retry * 2, 10)):
                        raise TransferCancelled("用户已取消传输，已接收的断点保留")
                finally:
                    self.active_socket = None
        except Exception as exc:
            self.root.after(0, self._done, False, str(exc))

    def _payload_chunks(self, path, offset, total):
        if isinstance(path, StreamPayload):
            yield from path.iter_chunks(offset, total, CHUNK)
            return
        with open(path, "rb", buffering=CHUNK) as source:
            source.seek(offset)
            while offset < total:
                chunk = source.read(min(CHUNK, total - offset))
                if not chunk:
                    raise IOError("源文件在传输过程中发生变化")
                yield chunk
                offset += len(chunk)

    def _send_legacy_transfer(self, path, host, port, token):
        is_dir = os.path.isdir(path)
        name = Path(path).name
        total = estimate_folder_payload(path)[1] if is_dir else os.path.getsize(path)
        started_at, last_ui = time.time(), [0.0]
        self.progress_offset = 0
        def progress(sent):
            if time.monotonic() - last_ui[0] >= PROGRESS_INTERVAL:
                self._ui_update(min(sent, total), total, started_at, approximate=is_dir)
                last_ui[0] = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as sock:
                self.active_socket = sock
                sock.settimeout(None)
                self._tune_socket(sock)
                self._legacy_payload(sock, path, name, total, is_dir, token, progress)
            self.root.after(0, self._done, True, "发送完成（原传输方式）。")
        finally:
            self.active_socket = None

    def _legacy_payload(self, sock, path, name, total, is_dir, token, progress):
        self._send_header(sock, MODE_FOLDER if is_dir else MODE_FILE, name, total, token)
        if is_dir:
            writer = SocketProgressWriter(sock, self.cancel_event, progress)
            with tarfile.open(fileobj=writer, mode='w|') as archive:
                archive.add(path, arcname=name)
            writer.flush()
        else:
            sent = 0
            for chunk in self._payload_chunks(path, 0, total):
                if self.cancel_event.is_set():
                    raise TransferCancelled("用户已取消传输")
                sock.sendall(chunk)
                sent += len(chunk)
                progress(sent)

    def _tune_socket(self, sock: socket.socket) -> None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        for size in (SEND_BUF_SIZE, SEND_BUF_SIZE // 2, SEND_BUF_SIZE // 4):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, size)
                break
            except OSError:
                continue

    def _send_header(self, sock: socket.socket, mode: int, name: str, total: int, token: str = "") -> None:
        name_bytes = name.encode("utf-8")
        authenticate_socket(sock, token)
        sock.sendall(struct.pack(">B", mode))
        sock.sendall(struct.pack(">I", len(name_bytes)))
        sock.sendall(name_bytes)
        sock.sendall(struct.pack(">Q", total))

    def _ui_update(self, sent: int, total: int, started_at: float, approximate: bool = False) -> None:
        self.last_transfer_progress = (sent, total, started_at, approximate)
        if total == UNKNOWN_TOTAL:
            measured = getattr(self, 'transfer_size_state', {}).get('total')
            if measured is not None and measured >= sent:
                total = measured
        elapsed = max(time.time() - started_at, 0.001)
        speed = max(0, sent - getattr(self, "progress_offset", 0)) / elapsed / 1024 / 1024
        pct = sent / total * 100 if total else 100.0
        remain = (total - sent) / (speed * 1024 * 1024) if speed > 0 and sent < total else 0

        def apply() -> None:
            prefix = "约 " if approximate else ""
            self.pbar.set(pct)
            if total != UNKNOWN_TOTAL:
                self.pbar.bar.stop()
                self.pbar.bar.configure(mode='determinate')
            self.graph.push(speed)
            self.status_label.config(text="传输中", fg=ACCENT_2)
            self.lbl_sent.config(text=f"{prefix}{human_size(sent)} / {human_size(total)}")
            self.lbl_speed.config(text=f"{speed:.1f} MB/s", fg=ACCENT_2)
            self.lbl_eta.config(text=human_time(remain) if remain else "-")
            self.lbl_elapsed.config(text=human_time(elapsed))
            if total == UNKNOWN_TOTAL:
                self.status_label.config(text="边扫描边发送", fg=ACCENT_2)
                self.lbl_sent.config(text=human_size(sent))
                self.lbl_eta.config(text="总量统计中")
                self.pbar.label.configure(text="进行中")
                if self.pbar.bar.cget('mode') != 'indeterminate':
                    self.pbar.bar.configure(mode='indeterminate')
                    self.pbar.bar.start()

        self.root.after(0, apply)

    def _set_status(self, text: str, color: str = TEXT_DIM) -> None:
        self.status_label.config(text=text, fg=color)
        if text.startswith("等待对方恢复"):
            self.lbl_speed.config(text="0.0 MB/s")
            self.lbl_eta.config(text="等待恢复")

    def _done(self, ok: bool, message: str) -> None:
        self.pbar.bar.stop()
        self.pbar.bar.configure(mode='determinate')
        self.is_sending = False
        self.send_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if ok:
            self.pbar.set(100)
            self.status_label.config(text="完成", fg=ACCENT_2)
            self.lbl_eta.config(text="完成", fg=ACCENT_2)
        else:
            self.status_label.config(text="未完成", fg=WARN)
        self._log(message)

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state="disabled")


def main() -> None:
    from update_server import handle_command_line
    if handle_command_line():
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = ctk.CTk()
    App(root)
    apply_windows_chrome(root)
    root.mainloop()


if __name__ == "__main__":
    main()
