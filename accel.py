#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小火箭 · 发送端加速器

独立的加速 exe，不改动原发送端，运行后让原「小火箭-发送端.exe」跑得更快：
1. 自动启动原发送端，并提升其进程/线程优先级（抢占 CPU 调度，无需管理员）
2. 应用 Windows TCP 优化（快速 ACK、窗口缩放、大发送窗口等，需要管理员，
   可一键恢复默认）
3. 常驻监控：只要发送端在运行，就持续保持其高优先级
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext

APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
SENDER_EXE = APP_DIR / "小火箭-发送端.exe"
BACKUP_FILE = (
    Path(os.environ.get("APPDATA", str(Path.home()))) / "XiaoRocketAccel" / "tcp-backup.json"
)

# ---------- 优先级常量 ----------
PROCESS_SET_INFORMATION = 0x0200
THREAD_SET_INFORMATION = 0x0020
HIGH_PRIORITY_CLASS = 0x80
ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
THREAD_PRIORITY_HIGHEST = 2
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004

# ---------- TCP 优化（HKLM，需管理员） ----------
TCPIP_PATH = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
GLOBAL_VALUES = {
    "Tcp1323Opts": 3,               # 启用时间戳与窗口缩放
    "GlobalMaxTcpWindowSize": 0x400000,  # 4MB 全局最大接收窗口
    "TcpWindowSize": 0x400000,      # 4MB 默认窗口
    "EnableWsd": 1,                 # 启用窗口缩放
    "DefaultTTL": 64,
    "TcpMaxDupAcks": 2,
    "SackOpts": 1,                  # 选择性确认
    "TcpMaxDataRetransmissions": 5,
}
INTERFACE_VALUES = {
    "TcpAckFrequency": 1,           # 立即回 ACK，不攒包
    "TcpDelAckTicks": 0,
}

BG = "#0d141c"
SURFACE = "#101820"
BORDER = "#263747"
TEXT = "#f4f8fb"
TEXT_DIM = "#9db0c1"
ACCENT = "#3e9bff"
ACCENT_2 = "#34df9d"
WARN = "#f2bd68"
DANGER = "#ff6b6b"
FONT = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 11, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 18, "bold")
FONT_MONO = ("Consolas", 9)


# ---------------- Windows 辅助 ----------------

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """以管理员权限重新启动自身，成功返回 True。"""
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{Path(sys.argv[0]).resolve()}"', None, 1
        )
        return result > 32
    except Exception:
        return False


def _k32():
    k = ctypes.windll.kernel32
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.OpenProcess.restype = ctypes.c_void_p
    k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.SetPriorityClass.restype = ctypes.c_int
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
    ]


def find_sender_pid() -> int | None:
    """按 exe 文件名查找原发送端进程 PID。"""
    try:
        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == -1:
            return None
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.lower() == "小火箭-发送端.exe":
                    return entry.th32ProcessID
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        pass
    return None


def boost_process(pid: int) -> bool:
    """把指定进程提升到 HIGH 优先级。"""
    try:
        kernel32 = _k32()
        handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.SetPriorityClass(handle, HIGH_PRIORITY_CLASS))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def boost_threads(pid: int) -> int:
    """把指定进程的所有线程提升到 HIGHEST，返回成功提升的线程数。"""
    boosted = 0
    try:
        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snapshot == -1:
            return 0
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == pid:
                    thread = kernel32.OpenThread(THREAD_SET_INFORMATION, False, entry.th32ThreadID)
                    if thread:
                        if kernel32.SetThreadPriority(thread, THREAD_PRIORITY_HIGHEST):
                            boosted += 1
                        kernel32.CloseHandle(thread)
                ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        pass
    return boosted


# ---------------- TCP 注册表优化 ----------------

def _read_backup() -> dict:
    try:
        return json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_backup(data: dict) -> None:
    try:
        BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def apply_tcp_optimization() -> tuple[bool, str]:
    """应用 TCP 优化并备份原值；失败返回 (False, 原因)。"""
    import winreg

    backup: dict = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, TCPIP_PATH, 0, winreg.KEY_READ) as key:
            for name in GLOBAL_VALUES:
                try:
                    backup[name], _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    backup[name] = None
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, TCPIP_PATH, 0, winreg.KEY_SET_VALUE) as key:
            for name, value in GLOBAL_VALUES.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)

        # 每个网卡接口的快速 ACK
        interfaces_path = TCPIP_PATH + r"\Interfaces"
        iface_backup: dict[str, dict] = {}
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, interfaces_path) as base:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(base, index)
                except OSError:
                    break
                index += 1
                sub_path = interfaces_path + "\\" + sub_name
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub_path, 0, winreg.KEY_READ) as key:
                        saved = {}
                        for name in INTERFACE_VALUES:
                            try:
                                saved[name], _ = winreg.QueryValueEx(key, name)
                            except FileNotFoundError:
                                saved[name] = None
                        iface_backup[sub_path] = saved
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub_path, 0, winreg.KEY_SET_VALUE) as key:
                        for name, value in INTERFACE_VALUES.items():
                            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
                except OSError:
                    continue

        _write_backup({"global": backup, "interfaces": iface_backup})
        return True, "TCP 优化已应用（原值已备份，可一键恢复）"
    except PermissionError:
        return False, "需要管理员权限才能修改 TCP 注册表"
    except OSError as exc:
        return False, f"应用 TCP 优化失败：{exc}"


def restore_tcp_defaults() -> tuple[bool, str]:
    """按备份恢复 TCP 注册表原值。"""
    import winreg

    backup = _read_backup()
    if not backup:
        return False, "没有找到备份记录，无需恢复"
    try:
        for name, old in backup.get("global", {}).items():
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, TCPIP_PATH, 0, winreg.KEY_SET_VALUE) as key:
                if old is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(old))
        for sub_path, saved in backup.get("interfaces", {}).items():
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub_path, 0, winreg.KEY_SET_VALUE) as key:
                    for name, old in saved.items():
                        if old is None:
                            try:
                                winreg.DeleteValue(key, name)
                            except FileNotFoundError:
                                pass
                        else:
                            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(old))
            except OSError:
                continue
        try:
            BACKUP_FILE.unlink()
        except OSError:
            pass
        return True, "已恢复 TCP 默认设置"
    except PermissionError:
        return False, "需要管理员权限才能恢复 TCP 注册表"
    except OSError as exc:
        return False, f"恢复 TCP 设置失败：{exc}"


# ---------------- 界面 ----------------

class AcceleratorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("小火箭 · 发送端加速器")
        self.root.geometry("520x560")
        self.root.minsize(500, 520)
        self.root.configure(bg=BG)
        self.stop_monitor = threading.Event()
        self.boosted_pid: int | None = None

        header = tk.Frame(root, bg="#0b1219")
        header.pack(fill="x")
        tk.Label(
            header, text="🚀 小火箭 · 发送端加速器", bg="#0b1219", fg=ACCENT_2,
            font=FONT_TITLE, anchor="w",
        ).pack(fill="x", padx=20, pady=(18, 4))
        self.admin_label = tk.Label(
            header, text="", bg="#0b1219", font=FONT, anchor="w",
        )
        self.admin_label.pack(fill="x", padx=20, pady=(0, 14))

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=12)

        # 状态卡片
        status = tk.Frame(body, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        status.pack(fill="x", pady=(0, 12))
        self.lbl_sender = tk.Label(status, text="发送端：未运行", bg=SURFACE, fg=TEXT_DIM, font=FONT_BOLD, anchor="w")
        self.lbl_sender.pack(fill="x", padx=14, pady=(12, 2))
        self.lbl_boost = tk.Label(status, text="优先级：—", bg=SURFACE, fg=TEXT_DIM, font=FONT, anchor="w")
        self.lbl_boost.pack(fill="x", padx=14, pady=2)
        self.lbl_tcp = tk.Label(status, text="TCP 优化：未应用", bg=SURFACE, fg=TEXT_DIM, font=FONT, anchor="w")
        self.lbl_tcp.pack(fill="x", padx=14, pady=(2, 12))

        # 按钮
        btn_row = tk.Frame(body, bg=BG)
        btn_row.pack(fill="x", pady=(0, 12))
        self.btn_accel = tk.Button(
            btn_row, text="⚡ 启动发送端并加速", command=self.accel_now,
            bg=ACCENT, fg="#ffffff", activebackground="#5aaeff", activeforeground="#ffffff",
            font=FONT_BOLD, bd=0, padx=12, pady=8, cursor="hand2",
        )
        self.btn_accel.pack(side="left", fill="x", expand=True)
        self.btn_restore = tk.Button(
            btn_row, text="♻ 恢复默认", command=self.restore_now,
            bg="#17232e", fg=TEXT, activebackground="#263747", activeforeground=TEXT,
            font=FONT, bd=0, padx=12, pady=8, cursor="hand2",
        )
        self.btn_restore.pack(side="left", padx=(10, 0))

        # 日志
        log_frame = tk.Frame(body, bg=BG)
        log_frame.pack(fill="both", expand=True)
        tk.Label(log_frame, text="运行日志", bg=BG, fg=TEXT_DIM, font=FONT, anchor="w").pack(fill="x", pady=(0, 4))
        self.log = scrolledtext.ScrolledText(
            log_frame, bg="#0a1118", fg=TEXT, insertbackground=TEXT,
            font=FONT_MONO, relief="flat", wrap="word", height=14,
        )
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

        hint = tk.Label(
            root, text="提示：TCP 优化需要以管理员身份运行本加速器；发送端优先级提升无需管理员。",
            bg=BG, fg="#637688", font=("Microsoft YaHei UI", 9), anchor="w",
        )
        hint.pack(fill="x", padx=20, pady=(0, 12))

        self._refresh_admin()
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    # ---- 工具 ----
    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_admin(self) -> None:
        ok = is_admin()
        self.admin_label.config(
            text=f"管理员权限：{'✔ 已获取' if ok else '✘ 未获取（TCP 优化不可用）'}",
            fg=ACCENT_2 if ok else WARN,
        )

    # ---- 核心动作 ----
    def accel_now(self) -> None:
        # 1) 若需要 TCP 优化但无管理员权限，先请求提权重启
        if not is_admin():
            self._log("未检测到管理员权限，TCP 优化需要提权。")
            if messagebox.askyesno(
                "需要管理员权限",
                "TCP 网络优化需要管理员权限。\n是否以管理员身份重新启动加速器？",
                parent=self.root,
            ):
                if relaunch_as_admin():
                    self._log("正在以管理员身份重启...")
                    self.root.after(800, self.root.destroy)
                else:
                    messagebox.showerror("提权失败", "无法以管理员身份启动，请手动右键选择“以管理员身份运行”。", parent=self.root)
            return

        # 2) 应用 TCP 优化
        ok, msg = apply_tcp_optimization()
        self.lbl_tcp.config(text=f"TCP 优化：{'已应用' if ok else msg}", fg=ACCENT_2 if ok else DANGER)
        self._log(msg)

        # 3) 启动发送端
        if not SENDER_EXE.exists():
            self._log(f"未找到发送端：{SENDER_EXE}")
            messagebox.showerror("未找到发送端", f"请把加速器放在与小火箭-发送端.exe 相同的目录。", parent=self.root)
            return
        pid = find_sender_pid()
        if pid is None:
            self._log("启动 小火箭-发送端.exe ...")
            subprocess.Popen([str(SENDER_EXE)], cwd=str(APP_DIR))
            time.sleep(1.2)
            pid = find_sender_pid()
            if pid is None:
                self._log("发送端已启动（等待其完成初始化）")
                self.lbl_sender.config(text="发送端：运行中", fg=ACCENT_2)
                return
        self._boost(pid)

    def _boost(self, pid: int) -> None:
        self.boosted_pid = pid
        ok_p = boost_process(pid)
        n = boost_threads(pid)
        self.lbl_sender.config(text=f"发送端：运行中 (PID {pid})", fg=ACCENT_2)
        self.lbl_boost.config(
            text=f"优先级：HIGH（进程{'✔' if ok_p else '✘'}，{n} 个线程已提升）",
            fg=ACCENT_2 if ok_p and n else WARN,
        )
        self._log(f"已提升发送端 PID {pid} 的优先级：进程={'成功' if ok_p else '失败'}，线程提升 {n} 个")

    def restore_now(self) -> None:
        ok, msg = restore_tcp_defaults()
        self.lbl_tcp.config(text=f"TCP 优化：{'已恢复默认' if ok else msg}", fg=ACCENT_2 if ok else DANGER)
        self._log(msg)
        if not ok and "管理员" in msg:
            messagebox.showinfo("需要管理员", "请以管理员身份运行本加速器后再执行恢复。", parent=self.root)

    # ---- 监控：持续保持高优先级 ----
    def _monitor_loop(self) -> None:
        last_pid: int | None = None
        while not self.stop_monitor.is_set():
            pid = find_sender_pid()
            if pid is not None:
                if pid != last_pid:
                    self._boost(pid)
                    last_pid = pid
                elif self.boosted_pid is None:
                    self._boost(pid)
            else:
                if last_pid is not None:
                    self.root.after(0, self._mark_gone)
                last_pid = None
            time.sleep(2)

    def _mark_gone(self) -> None:
        self.boosted_pid = None
        self.lbl_sender.config(text="发送端：未运行", fg=TEXT_DIM)
        self.lbl_boost.config(text="优先级：—", fg=TEXT_DIM)
        self._log("发送端已退出，等待下次启动...")


def main() -> None:
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = AcceleratorApp(root)
    try:
        root.mainloop()
    finally:
        app.stop_monitor.set()


if __name__ == "__main__":
    main()
