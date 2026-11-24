#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小火箭 · 接收端

提供图形化监听、保存目录管理、传输进度和安全解包。
"""

from __future__ import annotations

import os
import json
import hmac
import posixpath
import shutil
import socket
import struct
import sys
import tarfile
import threading
import time
import hashlib
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

VENDOR_DIR = Path(__file__).resolve().with_name("vendor")
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

import customtkinter as ctk
import xrt_proto
from resume_cache import clean_cache
from direct_folder import DirectFolder, receive_folder_frames
from stream_payload import UNKNOWN_TOTAL

ctk.set_appearance_mode("dark")


PORT = 9999
MIN_PORT = 1
MAX_PORT = 65535
AUTH_MAGIC = b"XRT1"
CHUNK = 8 * 1024 * 1024
RECV_BUF_SIZE = 32 * 1024 * 1024
PROGRESS_INTERVAL = 0.2

MODE_FILE = 0
MODE_FOLDER = 1

APP_NAME = "XiaoRocketTransfer"
APP_CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
SETTINGS_FILE = APP_CONFIG_DIR / "receiver-settings.json"

from rocket_ui import (BG, SURFACE, SURFACE_2, SURFACE_3, BORDER, BORDER_BRIGHT,
                       TEXT, TEXT_DIM, MUTED, ACCENT, ACCENT_HOVER, ACCENT_2, WARN,
                       DANGER, INPUT, HEADER_LEFT, HEADER_RIGHT, HEADER_GLOW)

FONT = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 22, "bold")
FONT_MONO = ("Consolas", 9)


def ensure_config_dir() -> None:
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(settings: dict) -> None:
    ensure_config_dir()
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def default_save_dir() -> str:
    downloads = Path.home() / "Downloads"
    return str(downloads if downloads.exists() else Path.cwd())


def blend_color(left: str, right: str, ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    a = tuple(int(left[i : i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(right[i : i + 2], 16) for i in (1, 3, 5))
    mixed = tuple(round(a[i] + (b[i] - a[i]) * ratio) for i in range(3))
    return f"#{mixed[0]:02x}{mixed[1]:02x}{mixed[2]:02x}"


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
        self.label = ctk.CTkLabel(self, text="0.0%", text_color=TEXT, font=FONT_BOLD, width=62, anchor="e")
        self.label.grid(row=0, column=1, padx=(12, 0))
        self._busy = False
        self.set(0)

    def set(self, pct: float) -> None:
        if self._busy:
            self.bar.stop()
            self.bar.configure(mode='determinate')
            self._busy = False
        value = max(0.0, min(100.0, pct))
        self.bar.set(value / 100)
        self.label.configure(text=f"{value:.1f}%")

    def set_busy(self, text: str = "进行中") -> None:
        """总量未知（文件夹边遍历边发送）时滚动显示，不显示百分比。"""
        if not self._busy:
            self.bar.configure(mode='indeterminate')
            self.bar.start()
            self._busy = True
        self.label.configure(text=text)


class HistoryList(ctk.CTkScrollableFrame):
    def __init__(self, parent: tk.Widget):
        super().__init__(
            parent,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_fg_color="transparent",
            scrollbar_button_color="#4c5269",
            scrollbar_button_hover_color=ACCENT,
        )
        self.columnconfigure(0, weight=1)
        self.rows: list[ctk.CTkFrame] = []
        self.keyed: dict = {}
        self.empty_label = ctk.CTkLabel(self, text="暂无接收记录", text_color=MUTED, font=FONT)
        self.empty_label.grid(row=0, column=0, pady=30)

    def add(self, values: tuple[str, str, str, str, str], key=None) -> None:
        if self.empty_label is not None:
            self.empty_label.destroy()
            self.empty_label = None
        timestamp, name, source, size, result = values
        row = ctk.CTkFrame(
            self,
            fg_color="#272b36",
            corner_radius=7,
            border_width=1,
            border_color=BORDER,
        )
        row.grid(row=0, column=0, sticky="ew", pady=(0, 8), padx=(0, 4))
        row.columnconfigure(1, weight=1)
        ctk.CTkLabel(row, text=timestamp, text_color=MUTED, font=FONT_MONO, width=56).grid(
            row=0, column=0, rowspan=2, padx=(10, 6), pady=10
        )
        ctk.CTkLabel(row, text=name, text_color=TEXT, font=FONT_BOLD, anchor="w").grid(
            row=0, column=1, sticky="ew", padx=4, pady=(8, 0)
        )
        ctk.CTkLabel(row, text=f"{source}  ·  {size}", text_color=TEXT_DIM, font=FONT_MONO, anchor="w").grid(
            row=1, column=1, sticky="ew", padx=4, pady=(0, 8)
        )
        result_color = ACCENT_2 if result.startswith("已") else WARN
        ctk.CTkLabel(
            row,
            text="完成" if result.startswith("已") else "失败",
            text_color=result_color,
            fg_color="#102229",
            corner_radius=6,
            font=FONT,
            width=56,
        ).grid(row=0, column=2, rowspan=2, padx=10, pady=10)
        if key is not None:
            # 同一续传文件的失败记录被成功记录替换（断线→续传成功 = 一条“完成”）
            old_row = self.keyed.get(key)
            if old_row is not None and old_row in self.rows:
                self.rows.remove(old_row)
                old_row.destroy()
            self.keyed[key] = row
        self.rows.insert(0, row)
        for row_index, current_row in enumerate(self.rows):
            current_row.grid_configure(row=row_index)
        for old_row in self.rows[30:]:
            old_row.destroy()
        del self.rows[30:]
        self.keyed = {k: v for k, v in self.keyed.items() if v in self.rows}


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
    def __init__(self, parent: tk.Widget, title: str, subtitle: str, badge: str, badge_color: str = WARN):
        super().__init__(parent, height=108, bg=BG, highlightthickness=0)
        self.title_text = title
        self.subtitle_text = subtitle
        self.badge_text = badge
        self.badge_color = badge_color
        self.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    def set_badge(self, text: str, color: str) -> None:
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
        self.create_text(47, 51, text="R", fill=HEADER_GLOW, font=("Segoe UI", 18, "bold"))
        self.create_line(39, 63, 55, 63, fill=ACCENT, width=2)
        self.create_text(88, 28, anchor="nw", text=self.title_text, fill=TEXT, font=FONT_TITLE)
        self.create_text(90, 67, anchor="nw", text=self.subtitle_text, fill=TEXT_DIM, font=FONT)
        self.create_text(90, 88, anchor="sw", text="PRIVATE NETWORK  /  LARGE FILE TRANSFER", fill=MUTED, font=("Consolas", 8, "bold"))
        badge_w = max(126, min(260, len(self.badge_text) * 9 + 36))
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


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        packet = sock.recv(size - len(data))
        if not packet:
            raise ConnectionError("连接中断")
        data.extend(packet)
    return bytes(data)


def create_server_socket(port: int) -> socket.socket:
    if socket.has_dualstack_ipv6():
        server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("::", port))
            server.listen(5)
            return server
        except OSError:
            server.close()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(5)
    return server


def clean_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in name).strip(" .")
    return cleaned or "未命名"


def unique_path(folder: Path, name: str) -> Path:
    candidate = folder / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 10_000):
        next_candidate = folder / f"{stem}_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
    raise FileExistsError("无法生成唯一文件名")


def timestamped_file(folder: Path, original_name: str) -> Path:
    clean = clean_name(original_name)
    path = Path(clean)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return unique_path(folder, f"{path.stem}_{stamp}{path.suffix}")


def timestamped_folder(folder: Path, original_name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return unique_path(folder, f"{clean_name(original_name)}_{stamp}")


def safe_member_target(base: Path, member_name: str) -> Path:
    normalized = posixpath.normpath(member_name.replace("\\", "/"))
    if normalized in ("", ".") or normalized.startswith("../") or normalized == "..":
        raise ValueError(f"拒绝不安全路径：{member_name}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." or ":" in part for part in parts):
        raise ValueError(f"拒绝不安全路径：{member_name}")
    target = (base / Path(*parts)).resolve()
    base_resolved = base.resolve()
    if os.path.commonpath([str(base_resolved), str(target)]) != str(base_resolved):
        raise ValueError(f"拒绝越界路径：{member_name}")
    return target


class CountingReader:
    def __init__(self, fileobj, on_progress):
        self.fileobj = fileobj
        self.on_progress = on_progress
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self.fileobj.read(size)
        if data:
            self.bytes_read += len(data)
            self.on_progress(self.bytes_read)
        return data

    def close(self) -> None:
        self.fileobj.close()


class ProgressBar(tk.Canvas):
    def __init__(self, parent: tk.Widget):
        super().__init__(parent, height=24, bg=SURFACE, highlightthickness=0)
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


class ReceiverApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("小火箭 · 接收端")
        self.root.geometry("940x760")
        self.root.minsize(940, 760)
        if isinstance(self.root, ctk.CTk):
            self.root.configure(fg_color=BG)
        else:
            self.root.configure(bg=BG)

        self.server_sock: socket.socket | None = None
        self.server_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.is_listening = False
        self.listen_token = ""
        self.history_count = 0
        self.resume_lock = threading.Lock()
        self.resume_active = set()
        self.active_clients = 0
        self._cache_cleaning = False
        self.settings = load_settings()
        self.port_var = tk.StringVar(value=str(self.settings.get("port", PORT)))
        self.token_var = tk.StringVar(value=str(self.settings.get("token", "")))

        self._configure_style()
        self._build()
        self._log("接收端已就绪。本机 IP: " + get_local_ip())
        self.root.after(3000, self._schedule_cache_cleanup)

    def _schedule_cache_cleanup(self):
        if not self._cache_cleaning:
            self._cache_cleaning = True
            directory = Path(self.save_dir_var.get().strip()).expanduser() / '.xiaorocket-resume'
            def worker():
                try:
                    with self.resume_lock:
                        count, freed = clean_cache(directory, self.resume_active)
                    if count:
                        self._log_threadsafe(f"自动清理 {count} 个无效/过期缓存，释放 {human_size(freed)}；保留有效断点")
                finally:
                    self._cache_cleaning = False
            threading.Thread(target=worker, daemon=True).start()
        self.root.after(60000, self._schedule_cache_cleanup)

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
            "小火箭接收端",
            "接收、记录、自动保存  ·  局域网 / 公网直连",
            f"未监听 · {PORT}",
            "RX",
        )
        header.pack(fill="x")
        update, update_settings = add_footer(self.root, "接收端")
        from rocket_update import attach
        self.updater = attach(self.root, 'receiver', update, update_settings, self)
        self.listen_label = header

        body = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        body.pack(fill="both", expand=True, padx=18, pady=16)
        body.columnconfigure(0, weight=1, minsize=340)
        body.columnconfigure(1, weight=1, minsize=340)
        body.rowconfigure(2, weight=1)

        settings = self._panel(body)
        settings.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        settings.columnconfigure(1, weight=1)
        self._section_title(settings, "接收设置", "目录与监听")
        ModernLabel(settings, text="保存目录", fg_color=SURFACE, text_color=TEXT_DIM, font=FONT_BOLD).grid(
            row=1, column=0, padx=(18, 10), pady=(0, 18), sticky="w"
        )
        self.save_dir_var = tk.StringVar(value=str(self.settings.get("save_dir") or default_save_dir()))
        ctk.CTkEntry(
            settings,
            textvariable=self.save_dir_var,
            fg_color=INPUT,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            corner_radius=7,
            font=FONT,
            height=40,
        ).grid(row=1, column=1, sticky="ew", pady=(0, 18))
        self._button(settings, "选择目录", self.choose_save_dir).grid(row=1, column=2, padx=8, pady=(0, 18))
        self.toggle_btn = self._button(settings, "开始监听", self.toggle_server, ACCENT)
        self.toggle_btn.grid(row=1, column=3, padx=(0, 18), pady=(0, 18))
        ModernLabel(settings, text="监听端口", fg_color=SURFACE, text_color=TEXT_DIM, font=FONT_BOLD).grid(
            row=2, column=0, padx=(18, 10), pady=(0, 18), sticky="w"
        )
        ctk.CTkEntry(settings, textvariable=self.port_var, fg_color=INPUT, text_color=TEXT, border_color=BORDER,
                     border_width=1, corner_radius=7, font=FONT_MONO, height=40, width=110).grid(
                         row=2, column=1, sticky="w", pady=(0, 18))
        ModernLabel(settings, text="接收口令（可选）", fg_color=SURFACE, text_color=TEXT_DIM, font=FONT_BOLD).grid(
            row=2, column=2, padx=(18, 10), pady=(0, 18), sticky="e"
        )
        ctk.CTkEntry(settings, textvariable=self.token_var, show="*", fg_color=INPUT, text_color=TEXT,
                     border_color=BORDER, border_width=1, corner_radius=7, font=FONT_MONO, height=40).grid(
                         row=2, column=3, padx=(0, 18), sticky="ew", pady=(0, 18))

        progress = self._panel(body)
        progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        progress.columnconfigure(0, weight=1)
        ModernLabel(progress, text="当前传输", fg_color=SURFACE, text_color=MUTED, font=("Consolas", 8, "bold")).grid(
            row=0, column=0, sticky="w", padx=18, pady=(18, 3)
        )
        self.current_label = ModernLabel(progress, text="等待传入文件", fg_color=SURFACE, text_color=TEXT_DIM, font=FONT_BOLD, anchor="w")
        self.current_label.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        self.pbar = ModernProgressBar(progress)
        self.pbar.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 14))
        stats = ctk.CTkFrame(progress, fg_color=SURFACE, corner_radius=0)
        stats.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 16))
        for i in range(4):
            stats.columnconfigure(i, weight=1)
        self.lbl_recv = self._stat(stats, 0, "已接收")
        self.lbl_speed = self._stat(stats, 1, "速度")
        self.lbl_source = self._stat(stats, 2, "来源")
        self.lbl_elapsed = self._stat(stats, 3, "耗时")

        history_box = self._panel(body)
        history_box.grid(row=2, column=0, sticky="nsew", padx=(0, 12))
        history_box.rowconfigure(1, weight=1)
        history_box.columnconfigure(0, weight=1)
        ModernLabel(history_box, text="接收记录", fg_color=SURFACE, text_color=TEXT, font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=18, pady=(18, 12)
        )
        self.history = HistoryList(history_box)
        self.history.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))

        log_box = self._panel(body)
        log_box.grid(row=2, column=1, sticky="nsew")
        log_box.rowconfigure(1, weight=1)
        log_box.columnconfigure(0, weight=1)
        ModernLabel(log_box, text="日志", fg_color=SURFACE, text_color=TEXT, font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=18, pady=(18, 12)
        )
        log_inner = ctk.CTkFrame(
            log_box,
            fg_color=INPUT,
            corner_radius=7,
            border_width=1,
            border_color=BORDER_BRIGHT,
        )
        log_inner.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        log_inner.rowconfigure(0, weight=1)
        log_inner.columnconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(
            log_inner,
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

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _panel(self, parent: tk.Widget) -> tk.Frame:
        return ctk.CTkFrame(
            parent,
            fg_color=SURFACE,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )

    def _section_title(self, parent: tk.Widget, text: str, meta: str = "") -> None:
        row = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=0)
        row.grid(row=0, column=0, columnspan=4, sticky="ew", padx=18, pady=(18, 12))
        ctk.CTkFrame(row, width=4, height=20, fg_color=HEADER_GLOW, corner_radius=2).pack(side="left", padx=(0, 10))
        ModernLabel(row, text=text, fg_color=SURFACE, text_color=TEXT, font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        if meta:
            ModernLabel(row, text=meta, fg_color=SURFACE, text_color=MUTED, font=("Consolas", 8, "bold")).pack(side="right", pady=2)

    def _button(self, parent: tk.Widget, text: str, command, bg: str = BORDER) -> tk.Button:
        return ModernButton(
            parent,
            text=text,
            command=command,
            fg_color=bg,
            hover_color=ACCENT_HOVER if bg == ACCENT else blend_color(bg, TEXT, 0.12),
            text_color="#ffffff",
            text_color_disabled="#697386",
            corner_radius=7,
            border_width=1 if bg == BORDER else 0,
            border_color=BORDER_BRIGHT,
            width=96,
            height=38,
            cursor="hand2",
            font=FONT_BOLD if bg == ACCENT else FONT,
        )

    def _stat(self, parent: tk.Widget, column: int, title: str) -> tk.Label:
        box = ctk.CTkFrame(
            parent,
            fg_color=SURFACE_3,
            corner_radius=7,
            border_width=1,
            border_color="#3b4154",
        )
        box.grid(row=0, column=column, sticky="ew", padx=6)
        ctk.CTkFrame(box, height=2, fg_color=HEADER_GLOW, corner_radius=0).pack(fill="x")
        ModernLabel(box, text=title, fg_color=SURFACE_3, text_color=MUTED, font=FONT, anchor="w").pack(
            fill="x", padx=11, pady=(8, 0)
        )
        label = ModernLabel(
            box,
            text="-",
            fg_color=SURFACE_3,
            text_color=TEXT,
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        )
        label.pack(fill="x", padx=11, pady=(3, 10))
        return label

    def choose_save_dir(self) -> None:
        path = filedialog.askdirectory(title="选择保存目录", initialdir=self.save_dir_var.get())
        if path:
            self.save_dir_var.set(path)
            self.settings["save_dir"] = path
            save_settings(self.settings)

    def toggle_server(self) -> None:
        if self.is_listening:
            self.stop_server()
        else:
            self.start_server()

    def start_server(self) -> None:
        folder = Path(self.save_dir_var.get().strip()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("无法使用保存目录", str(exc), parent=self.root)
            return
        self.settings["save_dir"] = str(folder)
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            port = 0
        if not MIN_PORT <= port <= MAX_PORT:
            messagebox.showerror("监听失败", f"端口必须在 {MIN_PORT}-{MAX_PORT} 范围内。", parent=self.root)
            return
        token = self.token_var.get()
        if len(token.encode("utf-8")) > 1024:
            messagebox.showerror("监听失败", "接收口令不能超过 1024 字节。", parent=self.root)
            return
        self.settings["port"] = port
        self.settings["token"] = token
        save_settings(self.settings)

        try:
            server = create_server_socket(port)
        except OSError as exc:
            messagebox.showerror("监听失败", f"端口 {port} 无法监听：{exc}", parent=self.root)
            return

        self.server_sock = server
        self.listen_token = token
        self.stop_event.clear()
        self.is_listening = True
        self.toggle_btn.config(text="停止监听", bg=DANGER)
        self.listen_label.set_badge(f"监听中 · {get_local_ip()}:{port}", ACCENT_2)
        self._log(f"开始监听端口 {port}，保存目录：{folder}")

        self.server_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.server_thread.start()

    def stop_server(self) -> None:
        self.stop_event.set()
        if self.server_sock is not None:
            try:
                self.server_sock.close()
            except Exception:
                pass
        self.server_sock = None
        self.is_listening = False
        self.toggle_btn.config(text="开始监听", bg=ACCENT)
        self.listen_label.set_badge(f"未监听 · {self.port_var.get().strip() or PORT}", WARN)
        self._log("已停止监听。")

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                assert self.server_sock is not None
                conn, addr = self.server_sock.accept()
            except OSError:
                break
            self._tune_socket(conn)
            self._log_threadsafe(f"收到连接：{addr[0]}")
            threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()

    def _tune_socket(self, sock: socket.socket) -> None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        for size in (RECV_BUF_SIZE, RECV_BUF_SIZE // 2, RECV_BUF_SIZE // 4):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, size)
                break
            except OSError:
                continue

    def _handle_client(self, conn: socket.socket, addr) -> None:
        with self.resume_lock:
            if getattr(self, '_updating', False):
                conn.close()
                return
            self.active_clients = getattr(self, 'active_clients', 0) + 1
        name = "未知"
        total = 0
        is_probe = False
        result = "未完成"
        resume_key = None
        direct_sink = None
        started_at = time.time()
        try:
            first = recv_exact(conn, 4)
            buffered_conn = self._buffered_socket(conn, first)
            negotiated = False
            authenticated = False
            if first == AUTH_MAGIC:
                buffered_conn.initial.clear()
                token_len = struct.unpack(">H", recv_exact(buffered_conn, 2))[0]
                supplied = recv_exact(buffered_conn, token_len)
                expected = self.listen_token.encode("utf-8")
                if not expected or not hmac.compare_digest(supplied, expected):
                    conn.sendall(b"\x00")
                    raise PermissionError("接收口令错误")
                conn.sendall(b"\x01")
                authenticated = True
                first = recv_exact(conn, 4)
                buffered_conn = self._buffered_socket(conn, first)
                if first == xrt_proto.NEGOTIATE_MAGIC:
                    mode, name, total, _offset, digest = xrt_proto.read_negotiate_request(buffered_conn, "")
                    negotiated = True
                else:
                    buffered_conn = self._buffered_socket(conn, first[1:])
                    first = first[:1]
            elif first == xrt_proto.NEGOTIATE_MAGIC:
                mode, name, total, _offset, digest = xrt_proto.read_negotiate_request(buffered_conn, self.listen_token)
                negotiated = True

            if negotiated:
                if mode == xrt_proto.MODE_PROBE:
                    is_probe = True
                    conn.sendall(xrt_proto.build_ack(0, 1))
                    return
                save_root = Path(self.save_dir_var.get().strip()).expanduser()
                save_root.mkdir(parents=True, exist_ok=True)
                streaming = mode in (xrt_proto.MODE_STREAM_FILE, xrt_proto.MODE_STREAM_FOLDER, xrt_proto.MODE_LAZY_FOLDER)
                if mode in (MODE_FILE, MODE_FOLDER, xrt_proto.MODE_STREAM_FILE, xrt_proto.MODE_STREAM_FOLDER, xrt_proto.MODE_LAZY_FOLDER):
                    safe_name = clean_name(name)
                    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                        raise ValueError("文件指纹无效")
                    identity = hashlib.sha256(f"{mode}\0{safe_name}\0{total}\0{digest}".encode("utf-8")).hexdigest()
                    cache_root = save_root / '.xiaorocket-resume'
                    cache_root.mkdir(exist_ok=True)
                    part_path = cache_root / f"{identity}.part"
                    resume_key = str(part_path.resolve())
                    with self.resume_lock:
                        if resume_key in self.resume_active:
                            resume_key = None
                            raise ConnectionError("同一文件正在接收，请稍后重试")
                        self.resume_active.add(resume_key)
                    resume_offset = part_path.stat().st_size if part_path.exists() else 0
                    if resume_offset > total:
                        part_path.unlink()
                        resume_offset = 0
                    if mode in (xrt_proto.MODE_STREAM_FOLDER, xrt_proto.MODE_LAZY_FOLDER):
                        direct_sink = DirectFolder(cache_root / f'{identity}.json',
                                                   timestamped_folder(save_root, name), safe_member_target)
                        # Migrate an existing archive prefix once; delete it only
                        # after equivalent direct-file data is durably recorded.
                        if part_path.exists():
                            old_size = part_path.stat().st_size
                            if direct_sink.offset < old_size:
                                self._log_threadsafe('正在转换旧断点为直接保存，转换完成后释放整包缓存')
                                with open(part_path, 'rb', buffering=CHUNK) as old_cache:
                                    old_cache.seek(direct_sink.offset)
                                    while block := old_cache.read(CHUNK):
                                        direct_sink.feed(block)
                                        direct_sink.checkpoint()
                            part_path.unlink()
                            self._log_threadsafe(f'已释放旧断点包：{human_size(old_size)}')
                        resume_offset = direct_sink.offset
                else:
                    conn.sendall(xrt_proto.build_ack(0, 0))
                    raise ValueError(f"未知传输模式：{mode}")

                # Hash the existing prefix before allowing the sender to send data.
                prefix_hash = hashlib.sha256()
                if resume_offset and not streaming:
                    self._log_threadsafe(f"正在验证断点：{human_size(resume_offset)}")
                    with open(part_path, 'rb', buffering=CHUNK) as existing:
                        while chunk := existing.read(CHUNK):
                            prefix_hash.update(chunk)
                data_started_at = time.time()
                conn.sendall(xrt_proto.build_ack(int(self.port_var.get()), resume_offset))
                if direct_sink is not None:
                    total = receive_folder_frames(self, conn, addr[0], name, total, direct_sink,
                                                  data_started_at, xrt_proto.FrameStream, UNKNOWN_TOTAL)
                    result = f'已保存：{direct_sink.destination.name}'
                elif mode in (MODE_FILE, xrt_proto.MODE_STREAM_FILE):
                    final_path = timestamped_file(save_root, name)
                    self._receive_resumable_file(conn, addr[0], name, total, digest, part_path, resume_offset, final_path, data_started_at, prefix_hash, streaming)
                    result = f"已保存：{final_path.name}"
                else:
                    final_dir = timestamped_folder(save_root, name)
                    archive_path = cache_root / f"{identity}.tar"
                    self._receive_resumable_file(conn, addr[0], name, total, digest, part_path, resume_offset, archive_path, data_started_at, prefix_hash, streaming)
                    final_dir.mkdir(parents=True, exist_ok=True)
                    self.root.after(0, self._start_progress, f"正在解包：{name}", addr[0], False)
                    with tarfile.open(archive_path, 'r:') as archive:
                        for member in archive:
                            if member.isdir():
                                safe_member_target(final_dir, member.name).mkdir(parents=True, exist_ok=True)
                            elif member.isfile():
                                target = safe_member_target(final_dir, member.name)
                                target.parent.mkdir(parents=True, exist_ok=True)
                                with archive.extractfile(member) as source, open(target, 'wb') as target_file:
                                    shutil.copyfileobj(source, target_file, CHUNK)
                    if mode == xrt_proto.MODE_LAZY_FOLDER:
                        total = archive_path.stat().st_size
                    archive_path.unlink()
                    result = f"已解包：{final_dir.name}"
                try:
                    conn.sendall(xrt_proto.COMPLETE_MAGIC)
                except OSError:
                    # 发送端已不再等待确认（或已断开）；文件已保存，不能因此判为失败。
                    pass
                self.root.after(0, self._mark_complete, result)
                self._log_threadsafe(result)
                started_at = data_started_at
                return

            elif first[:1] == AUTH_MAGIC[:1]:
                raise PermissionError("握手标识无效")
            elif self.listen_token and not authenticated:
                raise PermissionError("此接收端需要口令")
            else:
                if len(first) == 4:
                    buffered_conn.initial = bytearray(first[1:])
                first = first[:1]
            mode = struct.unpack(">B", first)[0]
            name_len = struct.unpack(">I", recv_exact(buffered_conn, 4))[0]
            if name_len <= 0 or name_len > 8192:
                raise ValueError("文件名长度异常")
            name = recv_exact(buffered_conn, name_len).decode("utf-8", errors="replace")
            total = struct.unpack(">Q", recv_exact(buffered_conn, 8))[0]
            save_root = Path(self.save_dir_var.get().strip()).expanduser()
            save_root.mkdir(parents=True, exist_ok=True)

            if mode == MODE_FILE:
                final_path = timestamped_file(save_root, name)
                self._receive_file(buffered_conn, addr[0], name, total, final_path, started_at)
                result = f"已保存：{final_path.name}"
            elif mode == MODE_FOLDER:
                final_dir = timestamped_folder(save_root, name)
                final_dir.mkdir(parents=True, exist_ok=True)
                self._receive_folder(buffered_conn, addr[0], name, total, final_dir, started_at)
                result = f"已解包：{final_dir.name}"
            else:
                raise ValueError(f"未知传输模式：{mode}")

            self.root.after(0, self._mark_complete, result)
            self._log_threadsafe(result)
        except Exception as exc:
            result = f"失败：{exc}"
            self.root.after(0, self._mark_failed, result)
            self._log_threadsafe(result)
        finally:
            if direct_sink is not None:
                direct_sink.close()
            with self.resume_lock:
                self.active_clients -= 1
            if resume_key is not None:
                with self.resume_lock:
                    self.resume_active.discard(resume_key)
                    clean_cache(Path(resume_key).parent, self.resume_active)
            try:
                conn.close()
            except Exception:
                pass
            if not is_probe:
                self.root.after(0, self._add_history, name, addr[0], total, result, resume_key)

    def _buffered_socket(self, conn: socket.socket, initial: bytes):
        class BufferedSocket:
            def __init__(self, sock, initial):
                self.sock, self.initial = sock, bytearray(initial)
            def recv(self, size):
                if self.initial:
                    packet = self.initial[:size]
                    del self.initial[:size]
                    return bytes(packet)
                return self.sock.recv(size)
            def recv_into(self, buffer, size=0):
                limit = size or len(buffer)
                packet = self.recv(limit)
                buffer[:len(packet)] = packet
                return len(packet)
            def __getattr__(self, name):
                return getattr(self.sock, name)
        return BufferedSocket(conn, initial)

    def _receive_resumable_file(self, conn, source, name, total, digest, part_path, offset, final_path, started_at, prefix_hash=None, streaming=False):
        self.root.after(0, self._start_progress, f"正在接收文件：{name}", source)
        hasher = prefix_hash if prefix_hash is not None else hashlib.sha256()
        with open(part_path, "ab" if offset else "wb", buffering=CHUNK) as target:
            if offset and prefix_hash is None:
                with open(part_path, "rb", buffering=CHUNK) as existing:
                    while True:
                        chunk = existing.read(CHUNK)
                        if not chunk:
                            break
                        hasher.update(chunk)
            stream = xrt_proto.FrameStream(conn)
            received = offset
            last_ui = 0.0
            while received < total:
                frame_offset, payload = stream.read_frame(verify_digest=streaming)
                if streaming and total == UNKNOWN_TOTAL and not payload and frame_offset == received:
                    total = received
                    break
                if frame_offset != received or not payload or received + len(payload) > total:
                    raise xrt_proto.ProtocolError("数据帧位置或长度无效")
                target.write(payload)
                if not streaming:
                    hasher.update(payload)
                received += len(payload)
                if time.monotonic() - last_ui >= PROGRESS_INTERVAL or received == total:
                    target.flush()
                    last_ui = time.monotonic()
                    self._progress_threadsafe(received, total, started_at, initial_offset=offset)
            target.flush()
        if not streaming and digest and hasher.hexdigest() != digest:
            part_path.unlink()
            raise IOError("文件校验失败，已清除损坏断点，请重新发送")
        os.replace(part_path, final_path)
        self._progress_threadsafe(total, total, started_at, initial_offset=offset)

    def _receive_file(
        self,
        conn: socket.socket,
        source: str,
        original_name: str,
        total: int,
        final_path: Path,
        started_at: float,
    ) -> None:
        self.root.after(0, self._start_progress, f"正在接收文件：{original_name}", source)
        temp_path = final_path.with_suffix(final_path.suffix + ".part")
        received = 0
        last_ui = 0.0
        try:
            with open(temp_path, "wb", buffering=CHUNK) as file:
                buffer = bytearray(CHUNK)
                while received < total:
                    request_size = min(CHUNK, total - received)
                    count = conn.recv_into(memoryview(buffer)[:request_size])
                    if not count:
                        raise ConnectionError("连接中断")
                    file.write(memoryview(buffer)[:count])
                    received += count
                    now = time.time()
                    if now - last_ui >= PROGRESS_INTERVAL:
                        self._progress_threadsafe(received, total, started_at)
                        last_ui = now
            if received != total:
                raise IOError("接收大小不一致")
            os.replace(temp_path, final_path)
            self._progress_threadsafe(total, total, started_at)
        except Exception:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            raise

    def _receive_folder(
        self,
        conn: socket.socket,
        source: str,
        original_name: str,
        total: int,
        final_dir: Path,
        started_at: float,
    ) -> None:
        self.root.after(0, self._start_progress, f"正在接收文件夹：{original_name}", source)
        last_ui = 0.0

        def on_progress(received: int) -> None:
            nonlocal last_ui
            now = time.time()
            if now - last_ui >= PROGRESS_INTERVAL:
                self._progress_threadsafe(min(received, total), total, started_at)
                last_ui = now

        stream = CountingReader(conn.makefile("rb", buffering=CHUNK), on_progress)
        try:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                for member in tar:
                    if member.isdir():
                        safe_member_target(final_dir, member.name).mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        target = safe_member_target(final_dir, member.name)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        src = tar.extractfile(member)
                        if src is None:
                            continue
                        with open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst, CHUNK)
                    else:
                        self._log_threadsafe(f"已跳过不支持的条目：{member.name}")
            self._progress_threadsafe(total, total, started_at)
        finally:
            stream.close()

    def _start_progress(self, title: str, source: str, reset: bool = True) -> None:
        self.current_label.config(text=title, fg=TEXT)
        self.lbl_source.config(text=source)
        if not reset:
            return
        self.pbar.set(0)
        self.lbl_recv.config(text="-")
        self.lbl_speed.config(text="-")
        self.lbl_elapsed.config(text="-")

    def _progress_threadsafe(self, received: int, total: int, started_at: float, initial_offset: int = 0) -> None:
        elapsed = max(time.time() - started_at, 0.001)
        speed = (received - initial_offset) / elapsed / 1024 / 1024
        pct = received / total * 100 if total else 100.0

        def apply() -> None:
            try:
                self.lbl_recv.config(text=(human_size(received) if total == UNKNOWN_TOTAL
                                           else f"{human_size(received)} / {human_size(total)}"))
                if total == UNKNOWN_TOTAL:
                    self.pbar.set_busy("进行中")
                else:
                    self.pbar.set(pct)
                self.lbl_speed.config(text=f"{speed:.1f} MB/s", fg=ACCENT_2)
                self.lbl_elapsed.config(text=human_time(elapsed))
            except Exception as exc:  # 界面更新失败不能静默吞掉，否则进度永远显示 "-"
                self._log(f"进度更新失败：{exc!r}")

        self.root.after(0, apply)

    def _mark_complete(self, message: str) -> None:
        self.pbar.set(100)
        self.current_label.config(text="接收完成", fg=ACCENT_2)
        self._log(message)

    def _mark_failed(self, message: str) -> None:
        self.current_label.config(text="接收失败", fg=WARN)
        self._log(message)

    def _add_history(self, name: str, source: str, total: int, result: str, key=None) -> None:
        self.history_count += 1
        values = (time.strftime("%H:%M:%S"), clean_name(name), source, human_size(total), result)
        self.history.add(values, key=key)

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state="disabled")

    def _log_threadsafe(self, message: str) -> None:
        self.root.after(0, self._log, message)

    def on_close(self) -> None:
        self.settings["save_dir"] = self.save_dir_var.get().strip()
        save_settings(self.settings)
        if self.is_listening:
            self.stop_server()
        self.root.destroy()


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
    app = ReceiverApp(root)
    if '--resume-listening' in sys.argv:
        root.after(500, app.start_server)
    apply_windows_chrome(root)
    root.mainloop()


if __name__ == "__main__":
    main()
