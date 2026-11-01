"""CDP Monitor 主程序 - PySide6 桌面应用"""
import asyncio
import html
import json
import logging
import os
import sys
import threading
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .ai_analyzer import (
    AIAnalyzer,
    classify_request_category,
    is_telemetry_request,
    is_useful_request,
    markdown_to_qt_html,
)
from .cdp import CDPClient, discover_targets
from .models import BrowserTab, NetworkRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ── CDP 后台异步线程 ─────────────────────────────────────────────────────────


class CDPRunner(QThread):
    """独立的 QThread，内部维护持续运行的 asyncio 事件循环"""
    sig_targets = Signal(list)
    sig_connected = Signal(str)
    sig_disconnected = Signal()
    sig_error = Signal(str)
    sig_request_added = Signal(object)
    sig_response_received = Signal(str, int, str, dict, str)
    sig_loading_finished = Signal(str, int)
    sig_loading_failed = Signal(str, str)
    sig_body_ready = Signal(str, str, bool)

    def __init__(self, cdp_url: str):
        super().__init__()
        self.cdp_url = cdp_url
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.ready_event = threading.Event()
        self.client = CDPClient()
        self.requests_map: dict[str, NetworkRequest] = {}
        self._running = True

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready_event.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def stop(self):
        self._running = False
        self.ready_event.wait(timeout=1.0)
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_disconnect(), self.loop)
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.quit()
        self.wait(2000)

    def discover(self, cdp_url: str):
        self.cdp_url = cdp_url
        self.ready_event.wait(timeout=2.0)
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_discover(), self.loop)

    async def _async_discover(self):
        try:
            targets = await discover_targets(self.cdp_url)
            self.sig_targets.emit(targets)
        except Exception as e:
            self.sig_error.emit(f"发现页面失败: {e}")

    def connect_tab(self, tab: BrowserTab):
        self.ready_event.wait(timeout=2.0)
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_connect(tab.ws_url, tab), self.loop)

    def connect_ws_url(self, ws_url: str):
        tab = BrowserTab(title="手动连接", url=ws_url, target_id="manual", ws_url=ws_url)
        self.ready_event.wait(timeout=2.0)
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_connect(ws_url, tab), self.loop)

    async def _async_connect(self, ws_url: str, tab: BrowserTab):
        self.requests_map.clear()
        ok = await self.client.connect(ws_url, tab, self._on_cdp_event)
        if ok:
            self.sig_connected.emit(tab.title)
        else:
            self.sig_error.emit("连接 DevTools WebSocket 失败，请检查端口与页面状态")

    def disconnect_client(self):
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_disconnect(), self.loop)

    async def _async_disconnect(self):
        await self.client.disconnect()
        self.sig_disconnected.emit()

    def fetch_response_body(self, request_id: str):
        if self.loop and self.loop.is_running() and self.client.connected:
            asyncio.run_coroutine_threadsafe(self._async_fetch_body(request_id), self.loop)

    def fetch_response_body_sync(self, request_id: str, timeout: float = 1.5) -> tuple[str, bool]:
        """同步等待获取指定请求的响应体 (通常用于 AI 上下文组装前即时读取)"""
        if self.loop and self.loop.is_running() and self.client.connected:
            future = asyncio.run_coroutine_threadsafe(self.client.get_response_body(request_id), self.loop)
            try:
                body, is_b64 = future.result(timeout=timeout)
                if request_id in self.requests_map:
                    self.requests_map[request_id].response_body = body
                self.sig_body_ready.emit(request_id, body, is_b64)
                return body, is_b64
            except Exception as e:
                logger.warning(f"同步获取响应体超时或失败 (requestId={request_id}): {e}")
        return "", False

    async def _async_fetch_body(self, request_id: str):
        body, is_b64 = await self.client.get_response_body(request_id)
        self.sig_body_ready.emit(request_id, body, is_b64)

    def _on_cdp_event(self, method: str, params: dict):
        if method == "Network.requestWillBeSent":
            req = params.get("request", {})
            req_id = params.get("requestId", "")
            r_type = params.get("type", "Other")
            is_xhr = r_type.lower() in ("xhr", "fetch")

            record = NetworkRequest(
                request_id=req_id,
                timestamp=datetime.now(),
                method=req.get("method", "GET"),
                url=req.get("url", ""),
                headers=req.get("headers", {}),
                post_data=req.get("postData", ""),
                resource_type=r_type,
                is_xhr=is_xhr,
                tab_title=self.client.active_tab.title if self.client.active_tab else "",
                tab_url=self.client.active_tab.url if self.client.active_tab else "",
            )
            self.requests_map[req_id] = record
            self.sig_request_added.emit(record)

        elif method == "Network.responseReceived":
            req_id = params.get("requestId", "")
            resp = params.get("response", {})
            status = resp.get("status", 0)
            status_text = resp.get("statusText", "")
            headers = resp.get("headers", {})
            mime = resp.get("mimeType", "")

            if req_id in self.requests_map:
                r = self.requests_map[req_id]
                r.response_status = status
                r.response_status_text = status_text
                r.response_headers = headers
                r.mime_type = mime

            self.sig_response_received.emit(req_id, status, status_text, headers, mime)

        elif method == "Network.loadingFinished":
            req_id = params.get("requestId", "")
            size = params.get("encodedDataLength", 0)
            if req_id in self.requests_map:
                self.requests_map[req_id].response_size = size
                self.requests_map[req_id].is_success = True
            self.sig_loading_finished.emit(req_id, size)

        elif method == "Network.loadingFailed":
            req_id = params.get("requestId", "")
            err = params.get("errorText", "")
            if req_id in self.requests_map:
                self.requests_map[req_id].is_success = False
                self.requests_map[req_id].error = err
            self.sig_loading_failed.emit(req_id, err)


# ── 聊天输入与 AI 工作线程 ──────────────────────────────────────────────────────


class ChatTextEdit(QTextEdit):
    """支持 Enter 发送、Shift+Enter 换行的对话输入框"""
    send_requested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.send_requested.emit()
                event.accept()
        else:
            super().keyPressEvent(event)


class AIChatWorker(QThread):
    sig_chunk = Signal(str)
    sig_finished = Signal(str)
    sig_error = Signal(str)

    def __init__(self, analyzer: AIAnalyzer, messages: list[dict]):
        super().__init__()
        self.analyzer = analyzer
        self.messages = messages

    def run(self):
        try:
            full_reply = self.analyzer.chat_stream(
                self.messages,
                lambda chunk: self.sig_chunk.emit(chunk)
            )
            self.sig_finished.emit(full_reply)
        except Exception as e:
            self.sig_error.emit(str(e))


# ── 主窗口 ───────────────────────────────────────────────────────────────────


class CDPMonitorApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CDP Network Monitor & AI Analyzer")
        self.resize(1440, 920)

        self.config = {
            "cdp_url": "http://127.0.0.1:9222/json",
            "ai_base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "ai_api_key": os.getenv("OPENAI_API_KEY", ""),
            "ai_model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        }
        self._load_config()

        # 数据存储与多轮 AI 对话上下文
        self.requests: list[NetworkRequest] = []
        self.req_id_to_row: dict[str, int] = {}
        self.selected_request: Optional[NetworkRequest] = None
        self.chat_messages: list[dict] = [AIAnalyzer.build_system_message()]
        self.ai_chat_worker: Optional[AIChatWorker] = None
        self._current_ai_raw_chunks: list[str] = []
        self._current_ai_start_pos: Optional[int] = None

        # 初始化后台 CDP 线程
        self.cdp_runner = CDPRunner(self.config["cdp_url"])
        self.cdp_runner.sig_targets.connect(self._on_targets_updated)
        self.cdp_runner.sig_connected.connect(self._on_connected)
        self.cdp_runner.sig_disconnected.connect(self._on_disconnected)
        self.cdp_runner.sig_error.connect(self._on_error)
        self.cdp_runner.sig_request_added.connect(self._on_request_added)
        self.cdp_runner.sig_response_received.connect(self._on_response_received)
        self.cdp_runner.sig_loading_finished.connect(self._on_loading_finished)
        self.cdp_runner.sig_loading_failed.connect(self._on_loading_failed)
        self.cdp_runner.sig_body_ready.connect(self._on_body_ready)
        self.cdp_runner.start()

        self._setup_ui()
        self._apply_styles()

        # 启动后延迟 300ms 自动发现页面，确保线程循环就绪
        from PySide6.QtCore import QTimer
        QTimer.singleShot(300, self._refresh_targets)

    def _load_config(self):
        # 1. 加载公共默认配置
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                self.config.update(json.load(f))
        except Exception:
            pass

        # 2. 覆盖加载本地私有配置 config.local.json (被 .gitignore 忽略，安全防泄漏)
        try:
            with open("config.local.json", "r", encoding="utf-8") as f:
                self.config.update(json.load(f))
        except Exception:
            pass

    def _save_config(self):
        # 若存在本地私有配置则优先保存到 config.local.json，避免将私密信息写回公开的 config.json
        target_file = "config.local.json" if os.path.exists("config.local.json") else "config.json"
        try:
            with open(target_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        # ── 顶部工具栏 ──
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        top_bar.addWidget(QLabel("CDP 地址:"))
        self.url_input = QLineEdit(self.config["cdp_url"])
        self.url_input.setFixedWidth(260)
        top_bar.addWidget(self.url_input)

        self.btn_refresh = QPushButton("🔄 刷新页面")
        self.btn_refresh.clicked.connect(self._refresh_targets)
        top_bar.addWidget(self.btn_refresh)

        top_bar.addWidget(QLabel("目标页面:"))
        self.tab_combo = QComboBox()
        self.tab_combo.setMinimumWidth(320)
        top_bar.addWidget(self.tab_combo, 1)

        self.btn_attach = QPushButton("▶️ 开始监听")
        self.btn_attach.clicked.connect(self._on_attach_clicked)
        top_bar.addWidget(self.btn_attach)

        self.btn_detach = QPushButton("⏹️ 断开")
        self.btn_detach.setEnabled(False)
        self.btn_detach.clicked.connect(self._on_detach_clicked)
        top_bar.addWidget(self.btn_detach)

        self.btn_manual_ws = QPushButton("🔗 手动 ws URL")
        self.btn_manual_ws.clicked.connect(self._manual_ws_connect)
        top_bar.addWidget(self.btn_manual_ws)

        self.btn_config = QPushButton("⚙️ 设置")
        self.btn_config.clicked.connect(self._show_config)
        top_bar.addWidget(self.btn_config)

        self.status_badge = QLabel("⚪ 未连接")
        self.status_badge.setStyleSheet("font-weight: bold; padding: 4px 8px; border-radius: 4px; background: #e0e0e0;")
        top_bar.addWidget(self.status_badge)

        root_layout.addLayout(top_bar)

        # ── 二级过滤栏 ──
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(8)

        self.chk_exclude_telemetry = QCheckBox("🚫 隐藏埋点与监控 (推荐)")
        self.chk_exclude_telemetry.setChecked(True)
        self.chk_exclude_telemetry.setStyleSheet("font-weight: bold; color: #dc2626;")
        self.chk_exclude_telemetry.stateChanged.connect(self._apply_table_filters)
        filter_bar.addWidget(self.chk_exclude_telemetry)

        filter_bar.addWidget(QLabel("类型:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部", "仅 XHR / Fetch", "Document", "Script", "Image", "其它"])
        self.filter_combo.currentIndexChanged.connect(self._apply_table_filters)
        filter_bar.addWidget(self.filter_combo)

        filter_bar.addWidget(QLabel("搜索 URL / 关键字:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("输入关键字过滤请求...")
        self.search_input.textChanged.connect(self._apply_table_filters)
        filter_bar.addWidget(self.search_input, 1)

        self.btn_clear = QPushButton("🗑️ 清空列表")
        self.btn_clear.clicked.connect(self._clear_requests)
        filter_bar.addWidget(self.btn_clear)

        self.btn_export = QPushButton("💾 导出 JSON")
        self.btn_export.clicked.connect(self._export_json)
        filter_bar.addWidget(self.btn_export)

        root_layout.addLayout(filter_bar)

        # ── 主体 Splitter (上下分割：上方网络请求与详情，下方 AI 分析) ──
        main_v_splitter = QSplitter(Qt.Orientation.Vertical)

        # 上半部分：左右分割 (左侧请求列表，右侧请求详情)
        upper_h_splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧表格
        table_group = QGroupBox("捕获的网络请求 (Network Events)")
        table_layout = QVBoxLayout(table_group)

        # ── 表格专用：核心有用接口筛选与检索工具栏 ──
        table_filter_bar = QHBoxLayout()
        table_filter_bar.setSpacing(6)

        self.btn_filter_useful = QPushButton("⭐ 仅看有用数据接口")
        self.btn_filter_useful.setCheckable(True)
        self.btn_filter_useful.setChecked(False)
        self.btn_filter_useful.setCursor(Qt.PointingHandCursor)
        self.btn_filter_useful.setStyleSheet("""
            QPushButton {
                background-color: #eff6ff;
                color: #1d4ed8;
                border: 1px solid #bfdbfe;
                font-weight: bold;
                border-radius: 4px;
                padding: 4px 10px;
            }
            QPushButton:hover {
                background-color: #dbeafe;
            }
            QPushButton:checked {
                background-color: #2563eb;
                color: #ffffff;
                border: 1px solid #1d4ed8;
            }
        """)
        self.btn_filter_useful.toggled.connect(self._on_useful_filter_toggled)
        table_filter_bar.addWidget(self.btn_filter_useful)

        lbl_url_filter = QLabel("🔗 接口 URL:")
        lbl_url_filter.setStyleSheet("font-weight: bold; color: #334155; margin-left: 4px;")
        table_filter_bar.addWidget(lbl_url_filter)

        self.url_filter_input = QLineEdit()
        self.url_filter_input.setPlaceholderText("输入 URL 或路径模糊匹配 (例如: aweme/v1/feed, comment, post)...")
        self.url_filter_input.setClearButtonEnabled(True)
        self.url_filter_input.setStyleSheet("""
            QLineEdit {
                background-color: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 12px;
            }
            QLineEdit:focus {
                border: 1px solid #2563eb;
            }
        """)
        self.url_filter_input.textChanged.connect(self._apply_table_filters)
        table_filter_bar.addWidget(self.url_filter_input, 1)

        # 快捷勾选控制按钮
        self.btn_check_visible = QPushButton("☑️ 勾选可见")
        self.btn_check_visible.setToolTip("将当前列表中显示的所有接口批量勾选为 AI 上下文，并主动预拉取其响应体")
        self.btn_check_visible.setCursor(Qt.PointingHandCursor)
        self.btn_check_visible.setStyleSheet("padding: 4px 8px; font-size: 11px;")
        self.btn_check_visible.clicked.connect(self._check_all_visible_requests)
        table_filter_bar.addWidget(self.btn_check_visible)

        self.btn_clear_checks = QPushButton("⬜ 取消勾选")
        self.btn_clear_checks.setToolTip("取消所有接口的勾选状态")
        self.btn_clear_checks.setCursor(Qt.PointingHandCursor)
        self.btn_clear_checks.setStyleSheet("padding: 4px 8px; font-size: 11px;")
        self.btn_clear_checks.clicked.connect(self._clear_all_checks)
        table_filter_bar.addWidget(self.btn_clear_checks)

        self.btn_ai_find_useful = QPushButton("🤖 AI 甄别")
        self.btn_ai_find_useful.setToolTip("让 AI 副驾驶分析当前捕获的所有流量，挑出核心业务接口并在下方对话框中展开说明")
        self.btn_ai_find_useful.setCursor(Qt.PointingHandCursor)
        self.btn_ai_find_useful.setStyleSheet("""
            QPushButton {
                background-color: #f8fafc;
                color: #334155;
                border: 1px solid #cbd5e1;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #e2e8f0;
                color: #0f172a;
            }
        """)
        self.btn_ai_find_useful.clicked.connect(self._ai_identify_useful_apis)
        table_filter_bar.addWidget(self.btn_ai_find_useful)

        table_filter_bar.addStretch()

        self.lbl_table_stats = QLabel("共 0 条")
        self.lbl_table_stats.setStyleSheet("color: #64748b; font-size: 11px;")
        table_filter_bar.addWidget(self.lbl_table_stats)

        table_layout.addLayout(table_filter_bar)

        self.req_table = QTableWidget()
        self.req_table.setColumnCount(8)
        self.req_table.setHorizontalHeaderLabels(["☑️", "#", "状态", "方法", "类型", "URL", "大小", "时间"])
        self.req_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.req_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.req_table.setColumnWidth(0, 36)
        self.req_table.setColumnWidth(1, 46)
        self.req_table.setColumnWidth(2, 60)
        self.req_table.setColumnWidth(3, 65)
        self.req_table.setColumnWidth(4, 70)
        self.req_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.req_table.setColumnWidth(6, 70)
        self.req_table.setColumnWidth(7, 75)
        self.req_table.itemSelectionChanged.connect(self._on_table_row_selected)
        self.req_table.itemChanged.connect(self._on_table_item_changed)
        table_layout.addWidget(self.req_table)
        upper_h_splitter.addWidget(table_group)

        # 右侧详情 TabWidget
        detail_group = QGroupBox("请求与响应详情 (Details)")
        detail_layout = QVBoxLayout(detail_group)

        # 详情面板顶部：在 AI 对话中探讨此接口
        detail_action_bar = QHBoxLayout()
        self.btn_detail_discuss = QPushButton("💬 在 AI 对话中深入探讨此接口 (自动带入参数与返回值)")
        self.btn_detail_discuss.setStyleSheet("background-color: #0284c7; color: white; font-weight: bold; padding: 6px 14px; border-radius: 4px;")
        self.btn_detail_discuss.clicked.connect(self._discuss_selected_in_chat)
        detail_action_bar.addWidget(self.btn_detail_discuss)
        detail_layout.addLayout(detail_action_bar)

        self.detail_tabs = QTabWidget()

        # Tab 1: General & Headers
        self.txt_general = QTextEdit()
        self.txt_general.setReadOnly(True)
        self.detail_tabs.addTab(self.txt_general, "概览与请求头")

        # Tab 2: Query Params
        self.txt_query = QTextEdit()
        self.txt_query.setReadOnly(True)
        self.detail_tabs.addTab(self.txt_query, "Query 参数")

        # Tab 3: Request Payload / Post Data
        self.txt_post = QTextEdit()
        self.txt_post.setReadOnly(True)
        self.detail_tabs.addTab(self.txt_post, "请求体 (POST)")

        # Tab 4: Response Headers
        self.txt_resp_headers = QTextEdit()
        self.txt_resp_headers.setReadOnly(True)
        self.detail_tabs.addTab(self.txt_resp_headers, "响应头")

        # Tab 5: Response Body
        resp_body_widget = QWidget()
        resp_body_layout = QVBoxLayout(resp_body_widget)
        resp_body_toolbar = QHBoxLayout()
        self.btn_load_body = QPushButton("📥 获取/刷新响应体")
        self.btn_load_body.clicked.connect(self._request_response_body)
        resp_body_toolbar.addWidget(self.btn_load_body)
        resp_body_toolbar.addStretch()
        resp_body_layout.addLayout(resp_body_toolbar)

        self.txt_resp_body = QTextEdit()
        self.txt_resp_body.setReadOnly(True)
        resp_body_layout.addWidget(self.txt_resp_body)
        self.detail_tabs.addTab(resp_body_widget, "响应体 (Body)")

        detail_layout.addWidget(self.detail_tabs)
        upper_h_splitter.addWidget(detail_group)

        upper_h_splitter.setStretchFactor(0, 3)
        upper_h_splitter.setStretchFactor(1, 2)
        main_v_splitter.addWidget(upper_h_splitter)

        # ── 下半部分：AI 逆向副驾驶 (持续多轮对话) ──
        ai_group = QGroupBox("🤖 AI 逆向副驾驶 (持续多轮对话 · 实时上下文联动)")
        ai_layout = QVBoxLayout(ai_group)
        ai_layout.setContentsMargins(10, 10, 10, 10)
        ai_layout.setSpacing(6)

        # 1. 顶部实时上下文感知指示条 + 控制按钮
        ctx_bar = QHBoxLayout()
        self.lbl_context = QLabel("📌 实时上下文: 未选择特定请求 | 捕获 0 条流量")
        self.lbl_context.setStyleSheet("font-size: 11px; color: #475569; background: #f1f5f9; padding: 4px 8px; border-radius: 4px;")
        ctx_bar.addWidget(self.lbl_context, 1)

        self.btn_clear_chat = QPushButton("🧹 清空对话")
        self.btn_clear_chat.clicked.connect(self._clear_chat)
        ctx_bar.addWidget(self.btn_clear_chat)

        self.btn_test_model = QPushButton("🧪 测试模型")
        self.btn_test_model.clicked.connect(self._test_model)
        ctx_bar.addWidget(self.btn_test_model)
        ai_layout.addLayout(ctx_bar)

        # 2. 对话历史展示区
        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        self.chat_display.setStyleSheet("""
            QTextEdit {
                background-color: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 10px;
                font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
                font-size: 13px;
                line-height: 1.5;
            }
        """)
        ai_layout.addWidget(self.chat_display, 1)

        # 3. 快捷提问灵感标签 (Chips) - 点击自动填入输入框，用户可随时直接发送或编辑
        chips_layout = QHBoxLayout()
        chips_layout.setSpacing(6)
        chips_layout.addWidget(QLabel("💡 快捷灵感:"))

        chips = [
            ("🎯 挑出核心业务接口", "请帮我梳理当前捕获的流量，剔除埋点与监控，挑出真正有用的核心业务数据接口并说明各自用途。"),
            ("🔬 深度剖析当前选中接口", "请帮我深度剖析当前选中的这个接口：它的业务用途、关键参数、以及返回值 JSON 的具体数据字段路径。"),
            ("🐍 生成 Python 爬取代码", "针对当前选中的接口，请帮我编写一份完整的 Python requests 脚本，包含必要请求头与核心字段提取代码。"),
            ("🔐 解释防刷/签名参数", "当前选中的接口有哪些防刷或加密签名参数（如 a_bogus 等）？如果不传或伪造会发生什么？"),
            ("📊 提取返回值字段结构", "请将当前选中的接口返回值进行树状解析，指出核心列表在哪里、标题在哪里、播放或下载地址在哪个字段。"),
        ]

        for label, prompt_text in chips:
            btn = QPushButton(label)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #f1f5f9;
                    border: 1px solid #cbd5e1;
                    border-radius: 12px;
                    padding: 3px 10px;
                    font-size: 11px;
                    color: #334155;
                }
                QPushButton:hover {
                    background-color: #e2e8f0;
                    color: #1e293b;
                }
            """)
            btn.clicked.connect(lambda checked=False, p=prompt_text: self._on_chip_clicked(p))
            chips_layout.addWidget(btn)

        chips_layout.addStretch()
        ai_layout.addLayout(chips_layout)

        # 4. 对话输入框与发送按钮
        input_box_layout = QHBoxLayout()
        input_box_layout.setSpacing(8)

        self.chat_input = ChatTextEdit()
        self.chat_input.setFixedHeight(56)
        self.chat_input.setPlaceholderText("在此输入你想与 AI 交流的问题... 支持探讨接口、解析字段、编写爬虫代码等 (Enter 发送，Shift+Enter 换行)")
        self.chat_input.send_requested.connect(self._send_chat_message)
        input_box_layout.addWidget(self.chat_input, 1)

        self.btn_send = QPushButton("🚀 发送\n(Enter)")
        self.btn_send.setFixedSize(85, 56)
        self.btn_send.setStyleSheet("""
            QPushButton {
                background-color: #2563eb;
                color: white;
                font-weight: bold;
                border-radius: 6px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #1d4ed8;
            }
            QPushButton:disabled {
                background-color: #94a3b8;
            }
        """)
        self.btn_send.clicked.connect(self._send_chat_message)
        input_box_layout.addWidget(self.btn_send)

        ai_layout.addLayout(input_box_layout)

        # 初始化聊天欢迎语
        self._clear_chat()

        main_v_splitter.addWidget(ai_group)
        main_v_splitter.setStretchFactor(0, 3)
        main_v_splitter.setStretchFactor(1, 2)

        root_layout.addWidget(main_v_splitter, 1)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪 - 请选择页面并点击「开始监听」")

    def _apply_styles(self):
        font = QFont("Microsoft YaHei", 9)
        self.setFont(font)
        self.setStyleSheet("""
            QMainWindow {
                background-color: #f8fafc;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #e2e8f0;
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 14px;
                background-color: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
                color: #1e293b;
            }
            QTableWidget {
                border: 1px solid #e2e8f0;
                gridline-color: #f1f5f9;
                selection-background-color: #e0f2fe;
                selection-color: #0f172a;
                background-color: #ffffff;
            }
            QHeaderView::section {
                background-color: #f1f5f9;
                padding: 4px;
                border: none;
                border-bottom: 1px solid #cbd5e1;
                font-weight: bold;
                color: #475569;
            }
            QLineEdit, QComboBox, QTextEdit {
                border: 1px solid #cbd5e1;
                border-radius: 4px;
                padding: 4px 6px;
                background: #ffffff;
            }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
                border: 1px solid #3b82f6;
            }
            QPushButton {
                border: 1px solid #cbd5e1;
                border-radius: 4px;
                padding: 5px 12px;
                background-color: #f8fafc;
            }
            QPushButton:hover {
                background-color: #e2e8f0;
            }
            QPushButton:pressed {
                background-color: #cbd5e1;
            }
        """)

    # ── 事件与槽函数 ──────────────────────────────────────────────────────────

    def _refresh_targets(self):
        url = self.url_input.text().strip()
        self.status_bar.showMessage("正在发现 Chrome 页面...")
        self.cdp_runner.discover(url)

    def _on_targets_updated(self, targets: list):
        self.tab_combo.clear()
        for t in targets:
            display_title = t.title[:45] if t.title else "未命名页面"
            self.tab_combo.addItem(f"{display_title} ({t.url[:50]})", userData=t)

        count = len(targets)
        if count == 0:
            self.status_bar.showMessage("未发现 Chrome 页面，请确保 Chrome 已带参数 --remote-debugging-port=9222 启动")
        else:
            self.status_bar.showMessage(f"发现 {count} 个页面，请选择并连接")

    def _on_attach_clicked(self):
        tab = self.tab_combo.currentData()
        if not tab:
            QMessageBox.warning(self, "提示", "请先在下拉列表中选择一个页面！")
            return
        self.status_bar.showMessage(f"正在连接到: {tab.title}...")
        self.cdp_runner.connect_tab(tab)

    def _on_detach_clicked(self):
        self.cdp_runner.disconnect_client()

    def _manual_ws_connect(self):
        ws_url, ok = QInputDialog.getText(
            self,
            "手动连接 DevTools WebSocket",
            "请输入完整的 WebSocket URL (例如 ws://127.0.0.1:9222/devtools/page/...):",
            QLineEdit.Normal,
            "ws://127.0.0.1:9222/devtools/page/"
        )
        if ok and ws_url.strip():
            self.cdp_runner.connect_ws_url(ws_url.strip())

    def _on_connected(self, title: str):
        self.status_badge.setText("🟢 已连接")
        self.status_badge.setStyleSheet("font-weight: bold; padding: 4px 8px; border-radius: 4px; background: #dcfce7; color: #15803d;")
        self.btn_attach.setEnabled(False)
        self.btn_detach.setEnabled(True)
        self.status_bar.showMessage(f"监听中: {title}")

    def _on_disconnected(self):
        self.status_badge.setText("⚪ 未连接")
        self.status_badge.setStyleSheet("font-weight: bold; padding: 4px 8px; border-radius: 4px; background: #e0e0e0; color: #333333;")
        self.btn_attach.setEnabled(True)
        self.btn_detach.setEnabled(False)
        self.status_bar.showMessage("已断开 CDP 监听")

    def _on_error(self, err_msg: str):
        self.status_bar.showMessage(f"错误: {err_msg}")
        QMessageBox.warning(self, "CDP 提示", err_msg)

    def _on_request_added(self, req: NetworkRequest):
        self.requests.append(req)
        row = self.req_table.rowCount()
        self.req_id_to_row[req.request_id] = row

        self.req_table.insertRow(row)

        # Col 0: ☑️ Checkbox
        chk_item = QTableWidgetItem()
        chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        chk_item.setCheckState(Qt.CheckState.Unchecked)
        chk_item.setData(Qt.ItemDataRole.UserRole, req)
        self.req_table.setItem(row, 0, chk_item)

        # Col 1: #
        self.req_table.setItem(row, 1, QTableWidgetItem(str(row + 1)))

        # Col 2: 状态
        status_item = QTableWidgetItem("...")
        status_item.setForeground(QColor("#f59e0b"))
        self.req_table.setItem(row, 2, status_item)

        # Col 3: 方法
        method_item = QTableWidgetItem(req.method)
        if is_useful_request(req):
            method_item.setForeground(QColor("#2563eb"))
        self.req_table.setItem(row, 3, method_item)

        # Col 4: 类型
        self.req_table.setItem(row, 4, QTableWidgetItem(req.resource_type))

        # Col 5: URL
        url_item = QTableWidgetItem(req.url)
        if is_useful_request(req):
            url_item.setToolTip("⭐ 判定为核心业务数据接口 (非埋点/非静态)")
        self.req_table.setItem(row, 5, url_item)

        # Col 6: 大小
        self.req_table.setItem(row, 6, QTableWidgetItem("-"))

        # Col 7: 时间
        self.req_table.setItem(row, 7, QTableWidgetItem(req.time_str))

        self._check_row_visibility(row, req)
        if hasattr(self, "lbl_table_stats"):
            total = len(self.requests)
            useful = sum(1 for r in self.requests if is_useful_request(r))
            checked = len(self._get_checked_requests())
            checked_tip = f" | 已勾选 {checked} 条" if checked else ""
            self.lbl_table_stats.setText(f"共 {total} 条 (核心业务: {useful} 条{checked_tip})")

        if not self.selected_request:
            self._update_context_label()

    def _on_response_received(self, req_id: str, status: int, status_text: str, headers: dict, mime: str):
        if req_id in self.req_id_to_row:
            row = self.req_id_to_row[req_id]
            status_item = QTableWidgetItem(str(status))
            if 200 <= status < 300:
                status_item.setForeground(QColor("#16a34a"))
            elif 300 <= status < 400:
                status_item.setForeground(QColor("#0284c7"))
            else:
                status_item.setForeground(QColor("#dc2626"))
            self.req_table.setItem(row, 2, status_item)

            if self.selected_request and self.selected_request.request_id == req_id:
                self._update_details_panel(self.selected_request)
                self._update_context_label()

    def _on_loading_finished(self, req_id: str, size: int):
        if req_id in self.req_id_to_row:
            row = self.req_id_to_row[req_id]
            req = self.cdp_runner.requests_map.get(req_id)
            if req:
                self.req_table.setItem(row, 6, QTableWidgetItem(req.formatted_size))

    def _on_loading_failed(self, req_id: str, err: str):
        if req_id in self.req_id_to_row:
            row = self.req_id_to_row[req_id]
            err_item = QTableWidgetItem("Failed")
            err_item.setForeground(QColor("#dc2626"))
            self.req_table.setItem(row, 2, err_item)

    def _on_body_ready(self, req_id: str, body: str, is_b64: bool):
        # 1. 更新内部数据字典与实体
        if req_id in self.cdp_runner.requests_map:
            self.cdp_runner.requests_map[req_id].response_body = body
        for r in self.requests:
            if r.request_id == req_id:
                r.response_body = body
                break

        # 2. 如果是当前右侧正在查看详情的接口，刷新详情展示
        if self.selected_request and self.selected_request.request_id == req_id:
            self.selected_request.response_body = body
            if is_b64:
                self.txt_resp_body.setText(f"[Base64 编码二进制内容 (大小约 {len(body)} 字符)]\n\n{body[:3000]}")
            else:
                try:
                    pretty = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
                    self.txt_resp_body.setText(pretty)
                except Exception:
                    self.txt_resp_body.setText(body)

        # 3. 刷新上下文指示
        self._update_context_label()

    def _on_table_row_selected(self):
        selected_rows = self.req_table.selectedItems()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        if row < len(self.requests):
            req = self.requests[row]
            self.selected_request = req
            self._update_details_panel(req)
            self._update_context_label()

    def _update_details_panel(self, req: NetworkRequest):
        # 1. 概览与请求头
        overview_lines = [
            f"=== 通用信息 ===",
            f"URL: {req.url}",
            f"方法: {req.method}",
            f"状态: {req.response_status} {req.response_status_text}",
            f"类型: {req.resource_type}",
            f"大小: {req.formatted_size}",
            f"捕获时间: {req.time_str}",
            "",
            f"=== 请求头 (Request Headers) ==="
        ]
        for k, v in req.headers.items():
            overview_lines.append(f"{k}: {v}")
        self.txt_general.setText("\n".join(overview_lines))

        # 2. Query 参数
        parsed = urlparse(req.url)
        query_dict = parse_qs(parsed.query)
        if query_dict:
            q_lines = [f"{k}: {', '.join(v)}" for k, v in query_dict.items()]
            self.txt_query.setText("\n".join(q_lines))
        else:
            self.txt_query.setText("无 Query 参数")

        # 3. 请求体
        if req.post_data:
            try:
                pretty_post = json.dumps(json.loads(req.post_data), indent=2, ensure_ascii=False)
                self.txt_post.setText(pretty_post)
            except Exception:
                self.txt_post.setText(req.post_data)
        else:
            self.txt_post.setText("无请求体数据 (GET 或空 Payload)")

        # 4. 响应头
        if req.response_headers:
            resp_lines = [f"{k}: {v}" for k, v in req.response_headers.items()]
            self.txt_resp_headers.setText("\n".join(resp_lines))
        else:
            self.txt_resp_headers.setText("暂未收到响应头")

        # 5. 响应体预览
        if req.response_body:
            try:
                pretty = json.dumps(json.loads(req.response_body), indent=2, ensure_ascii=False)
                self.txt_resp_body.setText(pretty)
            except Exception:
                self.txt_resp_body.setText(req.response_body)
        else:
            self.txt_resp_body.setText("点击上方「📥 获取/刷新响应体」拉取完整 Response Body 数据...")

    def _request_response_body(self):
        if not self.selected_request:
            QMessageBox.information(self, "提示", "请先在表格中选择一个请求！")
            return
        self.txt_resp_body.setText("正在通过 CDP Network.getResponseBody 读取响应体...")
        self.cdp_runner.fetch_response_body(self.selected_request.request_id)

    # ── 过滤与导出 ────────────────────────────────────────────────────────────

    def _on_useful_filter_toggled(self, checked: bool):
        if checked:
            self.btn_filter_useful.setText("✅ 正在筛选: 仅看有用数据接口")
        else:
            self.btn_filter_useful.setText("⭐ 仅看有用数据接口")
        self._apply_table_filters()

    def _ai_identify_useful_apis(self):
        """让 AI 深度分析当前网络流量并挑出核心业务接口"""
        if not self.requests:
            QMessageBox.information(self, "提示", "当前尚未捕获到网络流量，请先启动监听并在浏览器中操作页面！")
            return

        prompt = (
            "请帮我全面梳理当前捕获的网络流量，剔除无用埋点与静态文件，"
            "挑出真正有用的核心业务数据接口（如作品流、视频详情、评论、用户信息、搜索等），"
            "列出各接口的 URL 路径特征、核心功能、以及关键数据字段结构。"
        )
        self.chat_input.setText(prompt)
        self._send_chat_message()

    def _apply_table_filters(self):
        filter_type = self.filter_combo.currentText()
        keyword = self.search_input.text().strip().lower()
        url_query = self.url_filter_input.text().strip().lower() if hasattr(self, "url_filter_input") else ""

        total = len(self.requests)
        visible_cnt = 0
        useful_cnt = 0

        for row in range(self.req_table.rowCount()):
            if row < total:
                req = self.requests[row]
                if is_useful_request(req):
                    useful_cnt += 1
                self._check_row_visibility(row, req, filter_type, keyword, url_query)
                if not self.req_table.isRowHidden(row):
                    visible_cnt += 1

        if hasattr(self, "lbl_table_stats"):
            filter_active = (
                (getattr(self, "btn_filter_useful", None) and self.btn_filter_useful.isChecked())
                or bool(keyword)
                or bool(url_query)
                or filter_type != "全部"
            )
            if filter_active:
                self.lbl_table_stats.setText(f"显示 {visible_cnt}/{total} 条 (核心业务接口: {useful_cnt} 条)")
            else:
                self.lbl_table_stats.setText(f"共 {total} 条 (核心业务接口: {useful_cnt} 条)")

    def _check_row_visibility(
        self,
        row: int,
        req: NetworkRequest,
        filter_type: str = None,
        keyword: str = None,
        url_query: str = None,
    ):
        if filter_type is None:
            filter_type = self.filter_combo.currentText()
        if keyword is None:
            keyword = self.search_input.text().strip().lower()
        if url_query is None:
            url_query = self.url_filter_input.text().strip().lower() if hasattr(self, "url_filter_input") else ""

        # 1. 隐藏埋点与监控请求
        if hasattr(self, "chk_exclude_telemetry") and self.chk_exclude_telemetry.isChecked() and is_telemetry_request(req.url):
            self.req_table.setRowHidden(row, True)
            return

        # 2. 仅看核心/有用数据接口
        if hasattr(self, "btn_filter_useful") and self.btn_filter_useful.isChecked():
            if not is_useful_request(req):
                self.req_table.setRowHidden(row, True)
                return

        # 3. 接口 URL 地址模糊匹配 (支持完整 URL、路径或空格分词匹配)
        if url_query:
            u_lower = req.url.lower()
            try:
                u_unquoted = unquote(u_lower)
            except Exception:
                u_unquoted = u_lower

            keywords = url_query.split()
            if not all((k in u_lower or k in u_unquoted) for k in keywords):
                self.req_table.setRowHidden(row, True)
                return

        visible = True
        # 4. 类型过滤
        if filter_type == "仅 XHR / Fetch":
            visible = req.is_xhr or req.resource_type.lower() in ("xhr", "fetch")
        elif filter_type == "Document":
            visible = req.resource_type.lower() == "document"
        elif filter_type == "Script":
            visible = req.resource_type.lower() == "script"
        elif filter_type == "Image":
            visible = req.resource_type.lower() == "image"
        elif filter_type == "其它":
            visible = req.resource_type.lower() not in ("xhr", "fetch", "document", "script", "image")

        # 5. 关键字过滤
        if visible and keyword:
            visible = keyword in req.url.lower() or keyword in req.method.lower()

        self.req_table.setRowHidden(row, not visible)

    def _clear_requests(self):
        self.requests.clear()
        self.req_id_to_row.clear()
        self.req_table.setRowCount(0)
        self.selected_request = None
        self.txt_general.clear()
        self.txt_query.clear()
        self.txt_post.clear()
        self.txt_resp_headers.clear()
        self.txt_resp_body.clear()
        if hasattr(self, "lbl_table_stats"):
            self.lbl_table_stats.setText("共 0 条")
        self._update_context_label()
        self.status_bar.showMessage("已清空请求列表")

    def _export_json(self):
        if not self.requests:
            QMessageBox.information(self, "提示", "当前没有可导出的请求数据！")
            return

        file_path, _ = QFileDialog.getSaveFileName(self, "导出捕获请求", "captured_requests.json", "JSON Files (*.json)")
        if not file_path:
            return

        data = []
        for r in self.requests:
            data.append({
                "request_id": r.request_id,
                "timestamp": r.time_str,
                "method": r.method,
                "url": r.url,
                "resource_type": r.resource_type,
                "headers": r.headers,
                "post_data": r.post_data,
                "response_status": r.response_status,
                "response_headers": r.response_headers,
                "response_size": r.response_size,
                "response_body": r.response_body,
            })

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            QMessageBox.information(self, "成功", f"已成功导出 {len(data)} 条请求至:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"文件写入失败: {e}")

    # ── 表格勾选与 AI 上下文管理 ──────────────────────────────────────────────

    def _get_checked_requests(self) -> list[NetworkRequest]:
        """获取当前表格中所有已被勾选的请求对象列表"""
        checked = []
        for row in range(self.req_table.rowCount()):
            item = self.req_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                req = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(req, NetworkRequest):
                    checked.append(req)
                elif row < len(self.requests):
                    checked.append(self.requests[row])
        return checked

    def _on_table_item_changed(self, item: QTableWidgetItem):
        """当表格项变化（如用户点击勾选框）时响应"""
        if item.column() == 0:
            req = item.data(Qt.ItemDataRole.UserRole)
            if item.checkState() == Qt.CheckState.Checked:
                # 勾选时：主动触发拉取对应接口的响应体
                if req and not req.response_body:
                    self.cdp_runner.fetch_response_body(req.request_id)
            self._update_context_label()

    def _check_all_visible_requests(self):
        """批量勾选当前列表中所有未被隐藏的请求，并主动预拉取响应体"""
        self.req_table.blockSignals(True)
        count = 0
        for row in range(self.req_table.rowCount()):
            if not self.req_table.isRowHidden(row):
                item = self.req_table.item(row, 0)
                if item:
                    item.setCheckState(Qt.CheckState.Checked)
                    req = item.data(Qt.ItemDataRole.UserRole)
                    if req and not req.response_body:
                        self.cdp_runner.fetch_response_body(req.request_id)
                    count += 1
        self.req_table.blockSignals(False)
        self._update_context_label()
        self.status_bar.showMessage(f"已批量勾选 {count} 个接口作为 AI 上下文并触发预拉取响应体")

    def _clear_all_checks(self):
        """清空所有勾选"""
        self.req_table.blockSignals(True)
        for row in range(self.req_table.rowCount()):
            item = self.req_table.item(row, 0)
            if item:
                item.setCheckState(Qt.CheckState.Unchecked)
        self.req_table.blockSignals(False)
        self._update_context_label()
        self.status_bar.showMessage("已清空所有接口的勾选状态")

    # ── AI 逆向副驾驶多轮对话系统 ──────────────────────────────────────────────

    def _update_context_label(self):
        req_count = len(self.requests)
        checked_reqs = self._get_checked_requests()

        if checked_reqs:
            loaded_count = sum(1 for r in checked_reqs if r.response_body)
            preview_urls = ", ".join(f"[{r.method}] {urlparse(r.url).path}" for r in checked_reqs[:2])
            if len(checked_reqs) > 2:
                preview_urls += f" 等 {len(checked_reqs)} 个接口"

            self.lbl_context.setText(
                f"📌 AI 上下文: 已勾选 {len(checked_reqs)} 条接口 ({preview_urls}) | 响应体已就绪: {loaded_count}/{len(checked_reqs)}"
            )
            self.lbl_context.setStyleSheet(
                "font-size: 11px; color: #166534; background: #dcfce7; padding: 4px 8px; border-radius: 4px; font-weight: bold;"
            )
        elif self.selected_request:
            req = self.selected_request
            body_info = f"已载入响应体 ({req.formatted_size})" if req.response_body else "未载入响应体"
            self.lbl_context.setText(
                f"📌 当前选定接口: [{req.method}] {req.url[:70]}... | 状态: {req.response_status} | {body_info}"
            )
            self.lbl_context.setStyleSheet("font-size: 11px; color: #0284c7; background: #e0f2fe; padding: 4px 8px; border-radius: 4px; font-weight: bold;")
        else:
            self.lbl_context.setText(f"📌 当前上下文: 未勾选特定接口 (共捕获 {req_count} 条流量，可在表格中勾选 ☑️ 作为 AI 重点分析目标)")
            self.lbl_context.setStyleSheet("font-size: 11px; color: #475569; background: #f1f5f9; padding: 4px 8px; border-radius: 4px;")

    def _clear_chat(self):
        """清空对话历史"""
        self.chat_messages = [AIAnalyzer.build_system_message()]
        self._current_ai_raw_chunks = []
        self._current_ai_start_pos = None
        self.chat_display.clear()
        welcome_html = """
        <table width="100%" border="0" cellpadding="10" cellspacing="0" bgcolor="#f8fafc" style="border: 1px solid #e2e8f0;">
        <tr>
        <td>
            <div style="font-size: 15px; font-weight: bold; color: #1e40af;">
                🤖 欢迎使用 AI 逆向副驾驶 (Copilot)
            </div>
            <div style="font-size: 12px; color: #334155; margin-top: 6px; margin-bottom: 6px;">
                已实时接入当前 CDP 抓包网络流。无需繁琐的固定操作，你可以随时与 AI 进行<b>多轮持续交谈</b>：
            </div>
            <table width="100%" border="0" cellpadding="4" cellspacing="0" style="font-size: 12px; color: #475569;">
                <tr>
                    <td width="20" valign="top"><font color="#2563eb">●</font></td>
                    <td><b>接口勾选分析</b>：在左侧表格<b>勾选一个或多个 XHR 接口</b>，AI 将主动拉取并加载完整响应体进行深度剖析</td>
                </tr>
                <tr>
                    <td width="20" valign="top"><font color="#2563eb">●</font></td>
                    <td><b>爬虫代码生成</b>：直接提问 <i>“写出提取勾选接口视频下载地址的完整 Python 爬虫”</i></td>
                </tr>
                <tr>
                    <td width="20" valign="top"><font color="#2563eb">●</font></td>
                    <td><b>反爬签名逆向</b>：深入探讨 <i>“这里的 a_bogus / msToken 是如何参与签名的”</i>、<i>“哪些 Headers 是必须的”</i></td>
                </tr>
            </table>
            <div style="margin-top: 8px; font-size: 11px; color: #64748b; background-color: #f1f5f9; padding: 6px;">
                💡 <b>使用贴士</b>：可在表格中勾选多个接口联动分析；点击下方<b>灵感标签</b>可快速填入问题；支持按 <b>Enter 发送</b>。
            </div>
        </td>
        </tr>
        </table>
        <br/>
        """
        self.chat_display.setHtml(welcome_html)
        self._update_context_label()

    def _on_chip_clicked(self, prompt_text: str):
        """点击快捷灵感标签，自动填入输入框并聚焦"""
        self.chat_input.setText(prompt_text)
        self.chat_input.setFocus()
        cursor = self.chat_input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.chat_input.setTextCursor(cursor)

    def _discuss_selected_in_chat(self):
        """在详情面板中点击，快速将当前选中的请求带入对话并自动勾选"""
        if not self.selected_request:
            QMessageBox.information(self, "提示", "请先在网络请求表格中单击选中一条请求！")
            return

        req = self.selected_request
        # 自动将该请求在表格中打勾
        if req.request_id in self.req_id_to_row:
            row = self.req_id_to_row[req.request_id]
            item = self.req_table.item(row, 0)
            if item:
                item.setCheckState(Qt.CheckState.Checked)

        if not req.response_body:
            self.cdp_runner.fetch_response_body(req.request_id)

        prompt = f"请帮我深入分析这个接口 [{req.method}] {req.url}：它的主要功能是什么？返回值包含哪些核心数据字段？如何用 Python 代码进行抓取与解析？"
        self.chat_input.setText(prompt)
        self.chat_input.setFocus()
        self._update_context_label()

    def _send_chat_message(self):
        """发送多轮交谈消息"""
        user_text = self.chat_input.toPlainText().strip()
        if not user_text:
            return

        self.chat_input.clear()
        self.btn_send.setEnabled(False)

        # 1. 渲染用户消息气泡 (使用 table 保证 Qt 渲染气泡边距与底色)
        time_str = datetime.now().strftime("%H:%M:%S")
        safe_user_text = (
            user_text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br/>")
        )
        user_html = f"""
        <table width="100%" border="0" cellpadding="8" cellspacing="0" bgcolor="#f1f5f9" style="border: 1px solid #e2e8f0; border-radius: 6px; margin-top: 6px; margin-bottom: 6px;">
        <tr>
            <td>
                <b style="color: #0f172a; font-size: 13px;">🧑 你</b> <span style="color: #94a3b8; font-size: 11px;">({time_str})</span><br/>
                <div style="color: #1e293b; margin-top: 4px; font-size: 13px; line-height: 1.5;">{safe_user_text}</div>
            </td>
        </tr>
        </table>
        """
        self.chat_display.append(user_html)
        self.chat_display.ensureCursorVisible()

        # 2. 获取用户勾选的接口作为 AI 上下文，若无勾选则回退到当前单击选中的接口
        checked_reqs = self._get_checked_requests()
        target_reqs = checked_reqs if checked_reqs else ([self.selected_request] if self.selected_request else [])

        # 3. 主动触发拉取所有目标接口的响应体 (若未载入，通过同步等待获取最新内容)
        missing_body_reqs = [r for r in target_reqs if not r.response_body]
        for r in missing_body_reqs:
            body, _ = self.cdp_runner.fetch_response_body_sync(r.request_id, timeout=1.2)
            if body:
                r.response_body = body

        self._update_context_label()

        # 4. 构造带入实时快照的 Prompt，保持多轮记忆
        snapshot = AIAnalyzer.build_context_snapshot(
            self.requests,
            selected_req=self.selected_request,
            target_reqs=target_reqs,
        )
        combined_content = (
            f"【最新抓包环境与用户指定接口详情/完整响应体快照】:\n{snapshot}\n\n"
            f"---\n"
            f"【用户提问】: {user_text}"
        )

        # 多轮会话历史防溢出控制：保留 System 提示与最近 4 轮消息 (8 条)
        if len(self.chat_messages) > 9:
            self.chat_messages = [self.chat_messages[0]] + self.chat_messages[-8:]

        self.chat_messages.append({"role": "user", "content": combined_content})

        # 4. 渲染 AI 气泡头部
        ai_head = f"""
        <table width="100%" border="0" cellpadding="6" cellspacing="0" bgcolor="#eff6ff" style="border: 1px solid #bfdbfe; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-top: 10px; margin-bottom: 2px;">
        <tr>
            <td>
                <b style="color: #1d4ed8; font-size: 13px;">🤖 AI 逆向副驾驶</b> <span style="color: #60a5fa; font-size: 11px;">({time_str})</span>
            </td>
        </tr>
        </table>
        """
        self.chat_display.append(ai_head)
        cursor = self.chat_display.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.chat_display.setTextCursor(cursor)
        self.chat_display.ensureCursorVisible()

        self._current_ai_start_pos = cursor.position()
        self._current_ai_raw_chunks = []

        # 5. 启动流式响应工作线程
        analyzer = AIAnalyzer(
            base_url=self.config.get("ai_base_url"),
            api_key=self.config.get("ai_api_key"),
            model=self.config.get("ai_model"),
        )

        self.ai_chat_worker = AIChatWorker(analyzer, list(self.chat_messages))
        self.ai_chat_worker.sig_chunk.connect(self._on_ai_chat_chunk)
        self.ai_chat_worker.sig_finished.connect(self._on_ai_chat_finished)
        self.ai_chat_worker.sig_error.connect(self._on_ai_chat_error)
        self.ai_chat_worker.start()

    def _on_ai_chat_chunk(self, chunk: str):
        self._current_ai_raw_chunks.append(chunk)
        cursor = self.chat_display.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(chunk)
        self.chat_display.setTextCursor(cursor)
        self.chat_display.ensureCursorVisible()

    def _on_ai_chat_finished(self, full_reply: str):
        # 1. 保存模型回答到会话记忆
        self.chat_messages.append({"role": "assistant", "content": full_reply})

        # 2. 拼接当前轮次完整的原始 markdown（包含推理过程与正文）
        raw_text = "".join(self._current_ai_raw_chunks).strip() if self._current_ai_raw_chunks else full_reply.strip()

        # 3. 深度解析为现代高级卡片富文本 HTML（彻底消除原生 ### 与 raw markdown）
        rendered_html = markdown_to_qt_html(raw_text)

        # 4. 用解析后的精美富文本无缝替换刚才流式打印的原生纯文本
        cursor = self.chat_display.textCursor()
        if hasattr(self, "_current_ai_start_pos") and self._current_ai_start_pos is not None:
            cursor.setPosition(self._current_ai_start_pos)
            cursor.movePosition(cursor.MoveOperation.End, cursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()

        ai_bubble_html = f"""
        <table width="100%" border="0" cellpadding="10" cellspacing="0" bgcolor="#ffffff" style="border: 1px solid #bfdbfe; border-top: none; border-bottom-left-radius: 6px; border-bottom-right-radius: 6px; margin-bottom: 8px;">
        <tr>
            <td>
                {rendered_html}
            </td>
        </tr>
        </table>
        """
        cursor.insertHtml(ai_bubble_html)
        cursor.insertHtml("<div style='margin-bottom: 6px;'></div>")
        self.chat_display.setTextCursor(cursor)
        self.chat_display.ensureCursorVisible()

        self._current_ai_raw_chunks = []
        self._current_ai_start_pos = None
        self.btn_send.setEnabled(True)
        self._update_context_label()

    def _on_ai_chat_error(self, err: str):
        tip_html = ""
        if "exceed" in err.lower() and "context" in err.lower():
            tip_html = """
            <div style="margin-top: 6px; font-size: 11.5px; color: #b91c1c; background-color: #fee2e2; padding: 6px; border-radius: 4px;">
                💡 <b>原因与优化建议</b>：勾选的接口数量过多或历史对话太长（已超出模型 Context 限制）。<br/>
                建议：取消全选，仅勾选 <b>3~8 个核心业务接口</b>，或点击顶部 <b>「🧹 清空对话」</b> 重置历史记忆。
            </div>
            """

        error_html = f"""
        <table width="100%" border="0" cellpadding="8" cellspacing="0" bgcolor="#fef2f2" style="border: 1px solid #fecaca; border-radius: 6px; margin-top: 6px; margin-bottom: 6px;">
        <tr>
            <td>
                <b style="color: #dc2626; font-size: 13px;">❌ 回答异常</b>:
                <div style="margin-top: 4px; font-size: 12px; color: #991b1b;">{html.escape(err)}</div>
                {tip_html}
            </td>
        </tr>
        </table>
        """
        self.chat_display.append(error_html)
        self.chat_display.ensureCursorVisible()
        self.btn_send.setEnabled(True)
        self._current_ai_raw_chunks = []
        self._current_ai_start_pos = None

    def _test_model(self):
        self.btn_test_model.setEnabled(False)
        self.chat_display.append("""
        <table width="100%" border="0" cellpadding="6" cellspacing="0" bgcolor="#f8fafc" style="border: 1px solid #e2e8f0; border-radius: 4px; margin: 6px 0;">
        <tr><td><span style="color: #64748b; font-size: 12px;">🧪 正在测试与大模型服务的连通性...</span></td></tr>
        </table>
        """)
        self.chat_display.ensureCursorVisible()

        analyzer = AIAnalyzer(
            base_url=self.config.get("ai_base_url"),
            api_key=self.config.get("ai_api_key"),
            model=self.config.get("ai_model"),
        )

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(analyzer.test_connection())
            res_html = markdown_to_qt_html(res)
            self.chat_display.append(f"""
            <table width="100%" border="0" cellpadding="8" cellspacing="0" bgcolor="#f0fdf4" style="border: 1px solid #bbf7d0; border-radius: 6px; margin: 6px 0;">
            <tr><td>
                <b style="color: #16a34a; font-size: 13px;">✅ 模型连通成功！</b>
                <div style="margin-top: 4px; font-size: 12px; color: #15803d;">{res_html}</div>
            </td></tr>
            </table>
            """)
        except Exception as e:
            self.chat_display.append(f"""
            <table width="100%" border="0" cellpadding="8" cellspacing="0" bgcolor="#fef2f2" style="border: 1px solid #fecaca; border-radius: 6px; margin: 6px 0;">
            <tr><td>
                <b style="color: #dc2626; font-size: 13px;">❌ 连接失败</b>:
                <div style="margin-top: 4px; font-size: 12px; color: #991b1b;">{html.escape(str(e))}</div>
            </td></tr>
            </table>
            """)
        finally:
            loop.close()
            self.btn_test_model.setEnabled(True)
            self.chat_display.ensureCursorVisible()

    # ── 配置窗口 ─────────────────────────────────────────────────────────────

    def _show_config(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("全局设置")
        dialog.setFixedWidth(420)
        layout = QFormLayout(dialog)

        cdp_edit = QLineEdit(self.config.get("cdp_url", "http://127.0.0.1:9222/json"))
        layout.addRow("CDP 默认地址:", cdp_edit)

        base_url_edit = QLineEdit(self.config.get("ai_base_url", "https://api.openai.com/v1"))
        layout.addRow("AI Base URL:", base_url_edit)

        api_key_edit = QLineEdit(self.config.get("ai_api_key", ""))
        layout.addRow("AI API Key:", api_key_edit)

        model_edit = QLineEdit(self.config.get("ai_model", "gpt-4o-mini"))
        layout.addRow("AI 模型名称:", model_edit)

        btn_box = QHBoxLayout()
        save_btn = QPushButton("保存配置")
        save_btn.clicked.connect(lambda: self._apply_config(dialog, cdp_edit, base_url_edit, api_key_edit, model_edit))
        btn_box.addWidget(save_btn)
        layout.addRow(btn_box)

        dialog.exec()

    def _apply_config(self, dialog, cdp_edit, url_edit, key_edit, model_edit):
        self.config["cdp_url"] = cdp_edit.text().strip()
        self.config["ai_base_url"] = url_edit.text().strip()
        self.config["ai_api_key"] = key_edit.text().strip()
        self.config["ai_model"] = model_edit.text().strip()
        self.url_input.setText(self.config["cdp_url"])
        self._save_config()
        dialog.accept()
        QMessageBox.information(self, "配置保存", "设置已成功保存！")

    def closeEvent(self, event):
        self.cdp_runner.stop()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = CDPMonitorApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
