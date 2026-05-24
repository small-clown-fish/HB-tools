# BMS 0x038B + Heartbeat Writer

一个 Windows 小工具：持续向多个 BMS 写入：

- `0x038B = 2`
- `0x0380 = heartbeat`，心跳值 `0~255`，每秒 +1，超过 255 回到 0

## 功能

- 支持多个 BMS IP
- 可设置端口、Unit ID / Device ID、`0x038B` 发送间隔
- 心跳固定每秒写一次
- 可选择是否启用 Windows 防睡眠
- 屏幕黑屏/关闭显示器时仍可运行，只要电脑没有进入睡眠/休眠
- Start / Stop 控制
- Stop 后不会继续重连
- 连接失败后按冷却时间重试，不做高频死循环重连

## 运行源码版

```bash
pip install -r requirements.txt
python bms_038b_heartbeat_writer.py
```

## 打包 Windows exe

### 方法 1：本地 Windows 打包

```bash
build_windows.bat
```

生成文件在：

```text
dist/BMS_038B_Heartbeat_Writer.exe
```

### 方法 2：GitHub Actions 自动打包

把整个文件夹 push 到 GitHub，Actions 会自动打包。  
完成后在 GitHub Actions 页面下载 artifact：`BMS_038B_Heartbeat_Writer_Windows`

## 注意

这个工具会持续写 BMS 控制寄存器。现场使用前请确认：

- `0x038B = 2` 是你当前项目允许的控制命令
- `0x0380` 是 EMS 心跳寄存器
- Unit ID / Device ID 正确
- BMS 允许 EMS/上位机写这些寄存器
- `0x038B` 发送间隔建议先用 1s 或 2s 测试
