# 小火箭大文件传输

小火箭是一款 Windows 桌面文件传输程序，分为发送端和接收端。发送端主动连接接收端，通过 TCP 直连传输文件或文件夹；支持口令验证、断点续传和掉线恢复。

文件传输发生在发送端和接收端之间，不经过 GitHub，也不经过小火箭的更新服务器。局域网两端可以直接连接；跨网络使用时，接收端需要有可达的公网地址、端口转发或 VPN/虚拟局域网连接。

## 运行程序

本项目面向 Windows，使用 Python 3.10 或更高版本。Python 需要包含 Tcl/Tk；界面依赖 CustomTkinter，运行所需的第三方 Python 文件已放在 `vendor/`。

```powershell
git clone https://github.com/wzy020113/rocket.git
cd rocket
python receiver.py
```

在接收端选择保存目录并开始监听。另开一个终端启动发送端：

```powershell
python send.py
```

在发送端添加接收端的 IP 地址和端口（默认 `9999`），选择文件或文件夹后发送。跨网络连接时，应在接收端网络配置 TCP 端口转发或使用 VPN；公网使用请设置接收口令。当前协议验证口令，但不会加密传输内容。

运行测试：

```powershell
python -m unittest discover -s tests -v
```

## 构建 Windows 程序

打包配置为 `小火箭-发送端.spec` 和 `小火箭-接收端.spec`。发布脚本会运行测试、使用 PyInstaller 分别构建两个程序，并准备自动更新包。发布电脑需要 Python、PyInstaller 和 PowerShell。发布新版本：

```powershell
./publish_update.cmd
```

更新服务的启动和停止脚本为 `start_update_server.cmd`、`stop_update_server.cmd`。自动更新服务只负责提供版本清单和程序包，不承载文件传输。

### 更新地址配置

当前 `app_version.py` 中的默认更新地址是原维护环境的内网地址 `本机ip:18765`，外部使用者无法通过该地址获取更新。发布者应将其改成自己可访问的更新服务器地址，并在发布脚本中使用相同地址，例如：

```powershell
./publish_update.ps1 -Version 2.3.9 -Source http://<更新服务器地址>:18765
```

客户端也可以在程序底部通过“更新地址”设置自己的服务地址。不需要自动更新的集成项目可以忽略 `rocket_update.py`、`update_server.py` 及更新脚本。

## 目录结构

- `send.py`、`receiver.py`：发送端与接收端 GUI 应用。
- `xrt_proto.py`、`stream_payload.py`、`direct_folder.py`、`resume_cache.py`：传输协议和流式、续传实现。
- `rocket_ui.py`、`vendor/`：共享界面和随仓库提供的 Python 依赖。
- `accel.py`、`send_turbo.py`：Windows 加速器和发送端加速模式。
- `tests/`：协议、收发、续传、更新器测试；打包版端到端检查需要额外运行 `tests/smoke_packaged_updates.py`。
- `小火箭-*.spec` 和 `publish_update.*`：Windows 打包及发布脚本。
