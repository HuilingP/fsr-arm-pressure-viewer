# FSR 双侧腕托采集与受压分布

ESP32-C3 Super Mini 双板前端：实时显示 BNO080 与 8 路 FSR 数据，按次录制，支持下载 ZIP。受压分布视图按实际贴片顺序显示左右两组 **F0 → F1 → F3 → F4**。

- `frontend/`：连接硬件的本地采集程序，Python + 原生 HTML/CSS/JavaScript。
- `demo/`：无需硬件的交互式演示，全部电压为模拟数据。

此分享版本不包含实验记录、本机虚拟环境或真实设备编号。在线演示不连接任何人的 USB 板子。

## 分享链接

- 在线模拟演示：https://fsr-arm-pressure-huiling.huilingpan.chatgpt.site
- GitHub 源码：https://github.com/HuilingP/fsr-arm-pressure-viewer

## 本地运行

需要 **Python 3.10+**；串口依赖为 `pyserial>=3.5,<4`。不需要 Conda，也没有前端编译步骤。

### macOS

双击 `frontend/start.command`，或在下载后的项目目录运行：

```sh
chmod +x frontend/start.command
./frontend/start.command
```

首次运行会创建项目独立环境并安装依赖，随后自动打开 `http://127.0.0.1:5182/`。

### Windows

安装 Python 3.10+，双击 `frontend/start.bat`；也可以在 PowerShell 中运行：

```powershell
cd frontend
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe launch.py
```

### Linux / 手动启动

```sh
cd frontend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py --no-auto-connect
```

在浏览器打开 `http://127.0.0.1:5182/`。使用期间保持启动程序运行；停止录制后按 `Ctrl-C` 退出。

## 接入设备

在“连接、读数与波形”标签页分别选择两块板子的串口，然后连接。A 为左侧，B 为右侧。也可选择 Wi-Fi / TCP，手动输入各板的 IP。使用兼容的 V3/V4/V5 协议固件；本仓库不包含硬件固件。

没有板子也可以选择“模拟演示（非真实数据）”，分别连接 A、B，体验热图、波形和录制流程。

可选：将 `frontend/device_profiles.example.json` 复制为 `frontend/device_profiles.json`，把示例中的 USB `serial_number` 和固件 `board_id` 替换成实际编号，实现按身份自动连接。这个本机配置文件被 Git 忽略。未使用自动绑定时，不需要创建它。

## 记录与下载

连接设备后先实时预览。点击“开始记录”，结束后点“停止记录”，再点击“下载本次数据（ZIP）”。每次记录按 A/B 分开保存，重连后另建 segment 分段。原文件位于 `frontend/data/`。

`samples.csv` 保存采样值与时间戳，`stream.bin` 保存原始传输，JSON/JSONL 文件保存设备信息和处理状态。默认未启用事件流的固件可能使 `imu_events.csv` 只有表头。

## 读图边界

颜色表示电压高低，左右共用固定标尺；可调低标尺上限查看微小变化。超出颜色上限时保留实际数值，仅颜色饱和。断连或数据过期时清除实时颜色。

电压尚未标定为力（N）或压强（kPa）。每片 FSR 只有一个读数，图中不推测片内或空隙的分布。速度等运动学量为估计；两块板独立计时，统一开始记录不代表硬件同步。

## 检查

```sh
cd frontend
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_app.cjs tests/test_pressure_map.cjs
```

Node.js 仅用于运行页面逻辑检查，日常运行不需要安装。

## 来源

本地采集程序以提供的 `frontend_esp32_super_mini_duo` 前端包为基础，加入左右身份绑定、腕托受压分布、连接恢复及记录下载。保留原有协议解析和运动处理。
