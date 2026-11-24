"""LAN release checks, verified downloads and a separate Windows replacement helper."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
from contextlib import nullcontext

from app_version import VERSION, DEFAULT_UPDATE_SOURCE

ROLES = {'sender': '小火箭-发送端.exe', 'receiver': '小火箭-接收端.exe'}
CONFIG = Path(os.environ.get('APPDATA', str(Path.home()))) / 'XiaoRocketTransfer' / 'update-source.json'
MAX_EXE_SIZE = 256 * 1024 * 1024


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('版本号须为三个数字，如 2.1.1')
    return tuple(map(int, value.split('.')))


def application_dir():
    return Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent


def normalize_source(source):
    source = source.strip().rstrip('/')
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('请输入完整更新地址，例如 http://192.168.77.3:18765')
    return source


def load_source():
    for path in (CONFIG, application_dir() / 'update-config.json'):
        try:
            source = json.loads(path.read_text(encoding='utf-8'))['source']
            return normalize_source(source)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return DEFAULT_UPDATE_SOURCE


def save_source(source):
    source = normalize_source(source)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG.with_suffix('.tmp')
    temp.write_text(json.dumps({'source': source}, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, CONFIG)
    return source


def open_url(url):
    # LAN requests must not be sent through the user's Internet proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(urllib.request.Request(url, headers={'Cache-Control': 'no-cache'}), timeout=10)


def check_release(source, role, current=VERSION):
    source = normalize_source(source)
    with open_url(source + '/latest.json') as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('更新清单过大')
    manifest = json.loads(raw)
    if version_tuple(manifest['version']) <= version_tuple(current):
        return None
    artifact = manifest['artifacts'][role]
    filename, digest, size = artifact['file'], artifact['sha256'], artifact['size']
    if not re.fullmatch(r'[A-Za-z0-9._-]+\.exe', filename) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('更新文件名或校验值无效')
    if type(size) is not int or not 0 < size <= MAX_EXE_SIZE:
        raise ValueError('更新文件大小无效')
    return {'version': manifest['version'], 'notes': str(manifest.get('notes', ''))[:2000],
            'file': filename, 'sha256': digest, 'size': size, 'source': source}


def download_release(release, destination, progress=None):
    destination = Path(destination)
    partial = destination.with_suffix('.part')
    digest, count = hashlib.sha256(), 0
    try:
        with open_url(release['source'] + '/' + release['file']) as response, open(partial, 'wb') as target:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > release['size']:
                    raise ValueError('更新包超过声明大小')
                target.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(count / release['size'])
        if count != release['size'] or digest.hexdigest() != release['sha256']:
            raise ValueError('更新包校验失败，原程序未修改')
        with open(partial, 'rb') as executable:
            if executable.read(2) != b'MZ':
                raise ValueError('更新包不是 Windows 程序')
        os.replace(partial, destination)
        return destination
    finally:
        partial.unlink(missing_ok=True)


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def replacement_script(target, downloaded, parent_pid, restart_listener=False, bootloader_pid=None):
    # Paths are literal strings, never evaluated as user-supplied shell code.
    process_ids = ', '.join(str(int(pid)) for pid in (parent_pid, bootloader_pid) if pid)
    return f'''$ErrorActionPreference = 'Stop'
$target = {ps_literal(target)}
$incoming = {ps_literal(downloaded)}
$backup = $target + '.previous'
$log = $target + '.update.log'
$stage = $target + '.incoming-' + [guid]::NewGuid().ToString('N') + '.tmp'
$backedUp = $false
$installed = $false
$restarted = $false
$exited = $false
function Write-UpdateLog([string]$message) {{
    $line = [DateTime]::Now.ToString('s') + ' ' + $message + [Environment]::NewLine
    try {{ [IO.File]::AppendAllText($log, $line) }}
    catch {{ [IO.File]::AppendAllText($incoming + '.update.log', $line) }}
}}
function Get-UpdateHash([string]$path) {{
    $hasher = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($path)
    try {{ return [BitConverter]::ToString($hasher.ComputeHash($stream)) }}
    finally {{ $stream.Dispose(); $hasher.Dispose() }}
}}
function Start-UpdatedApplication {{
    # .NET treats paths literally; Start-Process expands square brackets in
    # WorkingDirectory as wildcard syntax on Windows PowerShell 5.
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $target
    $info.WorkingDirectory = [IO.Path]::GetDirectoryName($target)
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.Arguments = {ps_literal('--resume-listening' if restart_listener else '')}
    $started = [Diagnostics.Process]::Start($info)
    $started.Dispose()
}}
# A restarted one-file application must unpack into its own fresh directory.
$env:PYINSTALLER_RESET_ENVIRONMENT = '1'
try {{
    Copy-Item -LiteralPath $incoming -Destination $stage -Force
    if ((Get-UpdateHash $stage) -ne (Get-UpdateHash $incoming)) {{ throw '同目录暂存文件校验失败' }}
    foreach ($processId in @({process_ids})) {{
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($process) {{ $process | Wait-Process -Timeout 120 }}
    }}
    $exited = $true
    if (Test-Path -LiteralPath $backup) {{
        # 只保留最近一个备份：删除旧备份；若被占用删除失败，才改名保留。
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $backup) {{
            [IO.File]::Move($backup, $backup + '.' + [guid]::NewGuid().ToString('N'))
        }}
    }}
    $success = $false
    $lastError = ''
    for ($attempt = 0; $attempt -lt 120; $attempt++) {{
        try {{
            # Renaming avoids opening an executable image for writing while a
            # different process still maps that image (for example the server).
            [IO.File]::Move($target, $backup)
            $backedUp = $true
            $success = $true
            break
        }} catch {{ $lastError = $_.Exception.ToString(); Start-Sleep -Milliseconds 250 }}
    }}
    if (-not $success) {{ throw ('无法备份改名原程序：' + $target + [Environment]::NewLine + $lastError) }}
    [IO.File]::Move($stage, $target)
    $installed = $true
    Start-UpdatedApplication
    $restarted = $true
    Write-UpdateLog ('更新成功，已启动新版本')
}} catch {{
    Write-UpdateLog ($_ | Out-String)
    Write-UpdateLog ('目标：' + $target + '；保留的下载包：' + $incoming)
    try {{
        if ($backedUp) {{
            if ($installed -and (Test-Path -LiteralPath $target)) {{
                [IO.File]::Move($target, $target + '.failed-' + [guid]::NewGuid().ToString('N'))
            }}
            [IO.File]::Move($backup, $target)
            Write-UpdateLog '已恢复原程序'
        }}
        if ($exited -and (Test-Path -LiteralPath $target)) {{
            Start-UpdatedApplication
        }}
    }} catch {{
        Write-UpdateLog ('恢复或启动失败：' + ($_ | Out-String))
    }}
}} finally {{
    Remove-Item -LiteralPath $stage -ErrorAction SilentlyContinue
    if ($restarted) {{
        # 更新成功：新版已启动，删除下载包、回滚备份和日志，不留多余文件。
        Remove-Item -LiteralPath $incoming -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
    }}
}}
'''


def bootloader_process_id():
    """Wait for the one-file bootloader too, never an unrelated parent process."""
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    pid = os.getppid()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if handle:
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                if os.path.normcase(buffer.value) == os.path.normcase(str(Path(sys.executable).resolve())):
                    return pid
        finally:
            kernel.CloseHandle(handle)
    return None


def launch_replacement(downloaded, restart_listener=False):
    if not getattr(sys, 'frozen', False) or os.name != 'nt':
        raise RuntimeError('自动替换仅用于 Windows 打包版程序')
    script = replacement_script(Path(sys.executable).resolve(), downloaded, os.getpid(), restart_listener, bootloader_process_id())
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                      '-EncodedCommand', encoded], creationflags=subprocess.CREATE_NO_WINDOW,
                     close_fds=True)


class UpdateController:
    def __init__(self, root, role, button, app):
        self.root, self.role, self.button, self.app = root, role, button, app
        self.checking, self.downloading, self.release = False, False, None
        self.source = load_source()
        self.button.configure(command=lambda: self.check(True))
        root.after(2500, self.automatic_check)

    def busy(self):
        return bool(getattr(self.app, 'is_sending', False) or getattr(self.app, 'active_clients', 0))

    def configure_source(self):
        from tkinter import simpledialog, messagebox
        if self.downloading or self.checking:
            messagebox.showinfo('请稍候', '正在检查或下载更新，请完成后再修改地址。', parent=self.root)
            return
        value = simpledialog.askstring('设置更新地址', '填写发布者电脑的更新地址：\n例如 http://192.168.77.3:18765',
                                       initialvalue=self.source, parent=self.root)
        if value is not None:
            try:
                self.source = save_source(value)
                self.release = None
                self.check(True)
            except (ValueError, OSError) as exc:
                messagebox.showerror('更新地址无效', str(exc), parent=self.root)

    def automatic_check(self):
        if self.source:
            self.check(False)
        self.root.after(30 * 60 * 1000, self.automatic_check)

    def check(self, manual):
        if self.checking or self.downloading:
            return
        if not self.source:
            if manual:
                self.configure_source()
            return
        self.checking = True
        self.button.configure(text='检查中…')
        def worker():
            try:
                result, error = check_release(self.source, self.role), None
            except Exception as exc:
                result, error = None, str(exc)
            self.root.after(0, self.checked, result, error, manual)
        threading.Thread(target=worker, daemon=True).start()

    def checked(self, release, error, manual):
        from tkinter import messagebox
        self.checking = False
        self.release = release
        self.button.configure(text='有新版 · 更新' if release else '检查更新')
        if error:
            if manual:
                messagebox.showinfo('暂时无法检查更新', f'{error}\n请确认发布者电脑已开机、更新服务已启动。', parent=self.root)
            return
        if release is None:
            if manual:
                messagebox.showinfo('检查更新', f'当前版本 {VERSION}，已是最新版本。', parent=self.root)
            return
        if self.busy():
            if manual:
                messagebox.showinfo('有新版本', '当前任务正在运行，请完成或取消任务后再点击更新。', parent=self.root)
            return
        if messagebox.askyesno('发现新版小火箭', f"当前 {VERSION} → 新版 {release['version']}\n{release['notes']}\n\n下载并重启更新？设置和断点会保留。", parent=self.root):
            self.download(release)

    def download(self, release):
        self.downloading = True
        self.button.configure(text='下载中…')
        directory = Path(tempfile.mkdtemp(prefix='xiaorocket-update-'))
        destination = directory / (self.role + '-' + uuid.uuid4().hex + '.exe')
        def progress(value):
            self.root.after(0, lambda: self.button.configure(text=f'下载 {value:.0%}'))
        def worker():
            try:
                download_release(release, destination, progress)
                self.root.after(0, self.install, destination)
            except Exception as exc:
                self.root.after(0, self.failed, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def failed(self, error):
        from tkinter import messagebox
        self.downloading = False
        self.button.configure(text='检查更新')
        messagebox.showerror('更新失败', error, parent=self.root)

    def install(self, destination):
        from tkinter import messagebox
        # Serialize receiver connection admission with the decision to restart.
        with getattr(self.app, 'resume_lock', nullcontext()):
            busy = self.busy()
            if not busy:
                self.app._updating = True
        if busy:
            self.downloading = False
            destination.unlink(missing_ok=True)
            self.button.configure(text='有新版 · 更新')
            messagebox.showinfo('暂缓更新', '下载期间启动了传输任务，请空闲后再更新。', parent=self.root)
            return
        try:
            listener = bool(getattr(self.app, 'is_listening', False))
            launch_replacement(destination, listener)
        except Exception as exc:
            self.app._updating = False
            self.failed(str(exc))
            return
        if self.role == 'receiver':
            self.app.on_close()
        else:
            self.root.destroy()


def attach(root, role, button, settings_button, app):
    controller = UpdateController(root, role, button, app)
    settings_button.configure(command=controller.configure_source)
    return controller
