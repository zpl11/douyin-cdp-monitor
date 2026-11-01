# 🕷️ CDP Monitor - 工业级浏览器网络流量监听与 AI 逆向副驾驶

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/GUI-PySide6%20(Qt6)-41CD52?style=for-the-badge&logo=qt&logoColor=white" alt="PySide6">
  <img src="https://img.shields.io/badge/Chrome-DevTools%20Protocol-4285F4?style=for-the-badge&logo=google-chrome&logoColor=white" alt="CDP">
  <img src="https://img.shields.io/badge/AI-OpenAI%20%7C%20DeepSeek%20%7C%20vLLM-FF6F00?style=for-the-badge&logo=openai&logoColor=white" alt="AI Engine">
  <img src="https://img.shields.io/badge/License-MIT-blue?style=for-the-badge" alt="License">
</p>

---

## 📖 简介 (Overview)

**CDP Monitor** 是一款专为**现代 Web 接口逆向、数据采集分析与协议抓包**设计的工业级桌面客户端。

### 为什么选择 CDP 原生抓包？
传统抓包工具（如 Fiddler、Charles、Mitmproxy 等）普遍依赖**系统网络代理**与**自签名 CA 证书劫持**，容易引发以下痛点：
1. **反爬与安全风控检测**：主流站点（如抖音 Web/H5、小红书、TikTok 等）会检测 TLS 指纹（JA3/JA4）、系统代理状态及证书链异常，极易触发验证码拦截或封禁；
2. **人工逆向成本高**：开发者需手动在庞杂的数百个请求中翻找核心接口，费时费力；
3. **签名分析门槛高**：面对 `a_bogus`、`msToken`、`verifyFp` 等高频防爬参数，传统工具无法提供语义化分析与代码生成。

**CDP Monitor** 彻底颠覆了上述工作流：
- 🚀 **底层零侵入**：直接通过 Chromium 原生 **Chrome DevTools Protocol (CDP)** 与浏览器底层事件管道通讯，**无需设置任何系统代理，无需安装任何 CA 证书，天然免疫中间人代理风控**。
- 🤖 **内置 AI 逆向副驾驶 (Copilot)**：深度融合 OpenAI 兼容协议的大模型（支持 OpenAI、DeepSeek、vLLM、Ollama 等），实现请求结构自动提纯、Token 智能预算控制、反爬参数识别与 Python 爬虫代码一键生成。

---

## ✨ 核心特性 (Key Features)

- ⚡ **原生 CDP 双向监听**：基于 `websockets` 与 `httpx`，毫秒级捕获 HTTP/HTTPS 请求与全量网络事件。
- 📡 **深度 WebSocket 协议与消息帧透视**：
  - 完整捕获 WebSocket 连接建立、握手 Header 及状态转换；
  - 实时捕获每一条客户端**发送 (Send)** 与服务端**推送 (Recv)** 的原始数据帧（Frame Payload）；
  - 配备专用 **「WS 消息帧 (Frames)」** 透视面板，支持按发送/接收方向筛选、关键字过滤、自动滚动与 JSON 自动格式化；
  - 支持一键将捕获的双向 WS 消息帧序列送入 AI 对话框，智能解析直播间弹幕、即时私信、Protobuf/JSON 包体协议并生成 Python 监听脚本。
- 📑 **多标签页智能感知**：自动发现 Chrome 中已开启的所有页面标签，支持一键热切换监听目标。
- 🎯 **智能流量降噪与提纯**：
  - 支持 XHR、Fetch、WebSocket、JS、CSS、Doc、Image 等资源类型精准过滤；
  - 内置**自动化遥测/埋点过滤引擎**，智能识别并剔除字节跳动/通用厂商的 APM、MCS、日志上报等干扰请求。
- 🔍 **全方位请求/响应透视**：
  - 结构化展示 URL Query 参数、Request Headers、Post Payload、Response Headers；
  - 自动 JSON 语法美化、格式化与折叠预览。
- 🤖 **工业级 AI 逆向副驾驶**：
  - **智能 Token 预算与结构摘要**：自动剔除海量同构数组，保留核心 Schema 字段结构，防止超长响应击穿模型上下文窗口；
  - **敏感与签名参数高亮**：自动识别 `bogus`、`token`、`sign`、`verify`、`sec`、`w_rid` 等防爬特征字段；
  - **流式打字机渲染**：支持 SSE 流式推理、现代卡片化 Markdown 富文本渲染与一键复制代码。
- 💾 **标准数据导出**：支持一键导出行业标准 **HAR 2.0** 归档文件或自定义 JSON 格式，方便导入 Postman/Apifox 或离线回放。
- 🖥️ **跨平台桌面架构**：基于 PySide6 (Qt6) 现代 Fusion 主题构建，异步事件循环与 GUI 线程解耦，高并发数据包吞吐下界面丝滑无卡顿。

---

## 🏗️ 架构设计 (Architecture)

```mermaid
flowchart TD
    subgraph Browser["Google Chrome / Chromium 实例"]
        CDP_Server["CDP 服务端 (--remote-debugging-port=9222)"]
    end

    subgraph CoreEngine["CDP Monitor 核心引擎 (独立 QThread)"]
        AsyncLoop["AsyncIO 事件循环"]
        TargetDiscovery["HTTP 目标发现 (trust_env=False)"]
        WSClient["WebSocket 通讯管道"]
        Decoder["Network 事件解析器与数据包缓存"]
    end

    subgraph UIModule["PySide6 桌面主界面 (Qt 主线程)"]
        PacketTable["网络请求数据流表格 (智能排序/过滤)"]
        DetailPanel["请求/响应/参数多标签透视面板"]
        FilterEngine["降噪与埋点过滤引擎"]
        AICopilot["AI 逆向副驾驶会话面板"]
    end

    subgraph AIBackend["大模型后端 (OpenAI API 兼容)"]
        LLM["DeepSeek / OpenAI / vLLM / Ollama"]
    end

    Browser <==>|HTTP /json| TargetDiscovery
    Browser <==>|WebSocket 帧| WSClient
    TargetDiscovery --> AsyncLoop
    WSClient --> Decoder
    Decoder ==>|Qt Signal 异步通信| PacketTable
    PacketTable --> DetailPanel
    PacketTable --> FilterEngine
    DetailPanel ==>|上下文提纯 & Token 预算分配| AICopilot
    AICopilot <==>|流式多轮对话 (SSE)| LLM
```

---

## 🚀 快速上手 (Quick Start)

### 1. 环境准备
- **Python**: 3.10 或更高版本
- **Chrome / Edge**: 任意基于 Chromium 的现代浏览器

### 2. 克隆仓库与安装依赖

```bash
# 克隆代码
git clone https://github.com/zpl11/douyin-cdp-monitor.git
cd douyin-cdp-monitor

# 创建虚拟环境（推荐）
python -m venv .venv
# Windows 激活
.venv\Scripts\activate
# Linux/macOS 激活
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 3. 启动 Chrome 调试端口

关闭所有正在运行的 Chrome 窗口，使用 `--remote-debugging-port` 参数启动浏览器：

**Windows 示例：**
```cmd
chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\chrome_debug_profile"
```

**macOS 示例：**
```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="/tmp/chrome_debug_profile"
```

**Linux 示例：**
```bash
google-chrome --remote-debugging-port=9222 --user-data-dir="/tmp/chrome_debug_profile"
```

> 💡 **提示**：指定独立的 `--user-data-dir` 可以避免与正在运行的日常 Chrome 产生进程冲突。

### 4. 运行程序

```bash
python main.py
```
或者在 Windows 环境下直接双击运行 `run_gui.bat`。

---

## ⚙️ 配置说明 (Configuration)

项目支持通过图形界面、配置文件或系统环境变量进行 AI 模型配置。

### 方式一：GUI 界面配置（最简便）
点击界面右上角 **「⚙️ 全局设置」**，填入你的模型配置并保存：
- **CDP 默认地址**：`http://127.0.0.1:9222/json`
- **AI Base URL**：如 `https://api.openai.com/v1` 或 `https://api.deepseek.com/v1`
- **AI API Key**：你的 API 密钥
- **AI 模型名称**：如 `gpt-4o-mini`、`deepseek-chat` 或本地模型名称

### 方式二：使用本地私有配置文件 `config.local.json`（推荐，永久防泄露）
项目默认支持并在 `.gitignore` 中屏蔽了 `config.local.json`。你只需在根目录创建 `config.local.json`：
```json
{
  "cdp_url": "http://127.0.0.1:9222/json",
  "ai_base_url": "http://your-private-ip:8000/v1",
  "ai_api_key": "your-key",
  "ai_model": "your-model"
}
```
程序启动时会自动优先读取它，即便你执行 `git commit` 或 `git push`，私有配置也绝不会被推送到 GitHub！

### 方式三：编辑通用 `config.json`
在项目根目录编辑公开配置文件 `config.json`（注意不要在此文件中直接提交个人私有服务器 IP 或敏感 Token）。

### 方式四：环境变量（云原生/自动化运行）
支持标准 OpenAI 环境变量：
```bash
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_API_KEY="sk-your-api-key"
export OPENAI_MODEL="gpt-4o-mini"
```

---

## 📦 独立 EXE 打包 (Build Windows Exe)

如果需要将项目打包为无需 Python 环境的 Windows 单文件 `.exe` 可执行程序，只需运行：

```cmd
build.bat
```
打包成功后，可在 `dist\CDP-Monitor.exe` 找到独立运行文件。

---

## 🧭 项目目录结构 (Project Layout)

```
douyin-cdp-monitor/
├── .gitignore             # 工业级 Git 忽略规则
├── build.bat              # PyInstaller 一键编译与打包脚本
├── config.example.json    # 配置文件安全模版
├── config.json            # 本地配置文件 (已脱敏)
├── main.py                # 项目启动入口文件
├── requirements.txt       # 项目核心依赖清单
├── run_gui.bat            # Windows 快捷启动脚本
├── README.md              # 工业级中英文技术文档
└── src/                   # 核心源代码目录
    ├── __init__.py        # 模块初始化
    ├── models.py          # 核心数据模型 (BrowserTab, NetworkRequest)
    ├── cdp.py             # 原生 CDP WebSocket 客户端与事件订阅
    ├── ai_analyzer.py     # AI 逆向分析器、Token 压缩算法与 Markdown 渲染引擎
    └── main.py            # PySide6 现代 UI 界面实现与业务信号调度
```

---

## ❓ 常见问题排查 (FAQ)

### Q1: 点击「连接 / 刷新」提示无法发现页面？
- 请确保 Chrome 已携带 `--remote-debugging-port=9222` 参数启动；
- 在浏览器地址栏访问 `http://127.0.0.1:9222/json`，检查是否能正常输出 JSON 页面列表；
- 检查本地 VPN / 系统网络代理软件是否对 `127.0.0.1` 进行了误拦截（本项目底层已默认禁用环境代理 `trust_env=False`）。

### Q2: 请求列表里没有显示响应 Body？
- 当接口响应体超过数十兆字节、或者被浏览器过早从内存垃圾回收时，CDP 的 `Network.getResponseBody` 可能返回空；
- 程序已具备优雅兜底机制，即使响应体未获取，依然保留完整的请求 URL、Headers、Post Data 与状态码用于 AI 分析。

### Q3: 支持本地私有化部署的大模型吗？
- 完全支持！只要服务兼容 OpenAI 标准接口协议（如通过 **vLLM**、**Ollama**、**LocalAI** 部署的 Qwen、Llama 等模型），将 Base URL 设为如 `http://localhost:11434/v1` 或对应服务地址即可。

---

## ⚖️ 免责声明 (Disclaimer)

1. 本工具仅供**计算机网络协议研究、网络安全攻防测试与软件架构学习**使用。
2. 使用者请严格遵守相关法律法规，不得将本工具用于任何侵犯第三方合法权益、未授权渗透攻击或破坏正常商业运作的行为。
3. 因使用本软件所产生的直接或间接法律责任均由使用者自行承担，作者不承担任何责任。

---

## 📄 开源许可证 (License)

本项目采用 [MIT License](https://opensource.org/licenses/MIT) 许可证。
