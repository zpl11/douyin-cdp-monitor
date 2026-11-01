import html
import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional
from urllib.parse import unquote, urlparse

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .ai_analyzer import (
    AIAnalyzer,
    is_telemetry_request,
    is_useful_request,
)
from .models import BrowserTab, NetworkRequest, WebSocketFrame
from .ui import (
    MAIN_STYLE,
    USEFUL_FILTER_BTN_STYLE,
    WS_BADGE_STYLE,
    AIChatPanel,
    ConfigDialog,
    RequestDetailPanel,
)
from .workers import CDPRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class CDPMonitorApp(QMainWindow):
    """抖音 & Web 流量 CDP 监听器主窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("抖音 & Web 流量 CDP 逆向副驾驶 (CDP Monitor)")
        self.resize(1500, 920)

        self.config = self._load_config()
        self.requests: list[NetworkRequest] = []
        self.req_id_to_row: dict[str, int] = {}
        self.selected_request: Optional[NetworkRequest] = None

        cdp_port = self.config.get("cdp_port", 9222)
        self.cdp_runner = CDPRunner(f"http://127.0.0.1:{cdp_port}")
        self.cdp_runner.sig_targets.connect(self._on_targets_updated)
        self.cdp_runner.sig_connected.connect(self._on_connected)
        self.cdp_runner.sig_disconnected.connect(self._on_disconnected)
        self.cdp_runner.sig_error.connect(self._on_error)
        self.cdp_runner.sig_request_added.connect(self._on_request_added)
        self.cdp_runner.sig_response_received.connect(self._on_response_received)
        self.cdp_runner.sig_loading_finished.connect(self._on_loading_finished)
        self.cdp_runner.sig_loading_failed.connect(self._on_loading_failed)
        self.cdp_runner.sig_body_ready.connect(self._on_body_ready)
        self.cdp_runner.sig_ws_frame_added.connect(self._on_ws_frame_added)
        self.cdp_runner.sig_ws_closed.connect(self._on_ws_closed)
        self.cdp_runner.start()

        self._setup_ui()
        self._apply_styles()
        self._refresh_targets()

    def _load_config(self) -> dict:
        default_config = {
            "cdp_port": 9222,
            "ai_base_url": "https://api.openai.com/v1",
            "ai_api_key": "",
            "ai_model": "gpt-4o-mini"
        }
        cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
        local_cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.local.json")

        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    default_config.update(json.load(f))
            except Exception as e:
                logger.error(f"加载主配置文件失败: {e}")

        if os.path.exists(local_cfg_path):
            try:
                with open(local_cfg_path, "r", encoding="utf-8") as f:
                    default_config.update(json.load(f))
            except Exception as e:
                logger.error(f"加载本地配置文件失败: {e}")

        return default_config

    def _save_config(self, new_config: dict):
        self.config.update(new_config)
        local_cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.local.json")
        try:
            with open(local_cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            QMessageBox.information(self, "成功", "设置已保存至本地配置 (config.local.json)")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存配置失败: {e}")

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        # ── 1. 顶部连接控制栏 ──
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        top_bar.addWidget(QLabel("CDP 调试地址:"))
        self.url_input = QLineEdit(f"http://127.0.0.1:{self.config.get('cdp_port', 9222)}")
        self.url_input.setFixedWidth(240)
        top_bar.addWidget(self.url_input)

        self.btn_refresh = QPushButton("🔄 刷新页面")
        self.btn_refresh.clicked.connect(self._refresh_targets)
        top_bar.addWidget(self.btn_refresh)

        top_bar.addWidget(QLabel("选择目标页面:"))
        self.tab_combo = QComboBox()
        self.tab_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        top_bar.addWidget(self.tab_combo, 1)

        self.btn_attach = QPushButton("▶️ 开始监听")
        self.btn_attach.setStyleSheet("background-color: #2563eb; color: white; font-weight: bold; padding: 5px 14px; border-radius: 4px;")
        self.btn_attach.clicked.connect(self._on_attach_clicked)
        top_bar.addWidget(self.btn_attach)

        self.btn_detach = QPushButton("⏹️ 停止监听")
        self.btn_detach.setEnabled(False)
        self.btn_detach.clicked.connect(self._on_detach_clicked)
        top_bar.addWidget(self.btn_detach)

        self.btn_manual_ws = QPushButton("🔗 手动 WS 连接")
        self.btn_manual_ws.clicked.connect(self._manual_ws_connect)
        top_bar.addWidget(self.btn_manual_ws)

        self.btn_config = QPushButton("⚙️ 设置")
        self.btn_config.clicked.connect(self._show_config)
        top_bar.addWidget(self.btn_config)

        self.status_badge = QLabel("⚪ 未连接")
        self.status_badge.setStyleSheet("font-weight: bold; padding: 4px 8px; border-radius: 4px; background: #e0e0e0;")
        top_bar.addWidget(self.status_badge)

        root_layout.addLayout(top_bar)

        # ── 2. 二级过滤栏 ──
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(8)

        self.chk_exclude_telemetry = QCheckBox("🚫 隐藏埋点与监控 (推荐)")
        self.chk_exclude_telemetry.setChecked(True)
        self.chk_exclude_telemetry.setStyleSheet("font-weight: bold; color: #dc2626;")
        self.chk_exclude_telemetry.stateChanged.connect(self._apply_table_filters)
        filter_bar.addWidget(self.chk_exclude_telemetry)

        filter_bar.addWidget(QLabel("类型:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部", "仅 XHR / Fetch", "WebSocket", "Document", "Script", "Image", "其它"])
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

        # ── 3. 主体左右大分割 (自由平滑拖拽) ──
        main_h_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_h_splitter.setHandleWidth(7)
        main_h_splitter.setChildrenCollapsible(False)

        # 左侧面板：垂直分割 (上方网络请求列表，下方请求与 WS 详情)
        left_v_splitter = QSplitter(Qt.Orientation.Vertical)
        left_v_splitter.setHandleWidth(6)
        left_v_splitter.setChildrenCollapsible(False)
        left_v_splitter.setMinimumWidth(300)

        # 上部：网络请求列表
        table_group = QGroupBox("捕获的网络请求 (Network Events)")
        table_layout = QVBoxLayout(table_group)

        table_filter_bar = QHBoxLayout()
        table_filter_bar.setSpacing(6)

        self.btn_filter_useful = QPushButton("⭐ 仅看有用数据接口")
        self.btn_filter_useful.setCheckable(True)
        self.btn_filter_useful.setChecked(False)
        self.btn_filter_useful.setCursor(Qt.PointingHandCursor)
        self.btn_filter_useful.setStyleSheet(USEFUL_FILTER_BTN_STYLE)
        self.btn_filter_useful.toggled.connect(self._on_useful_filter_toggled)
        table_filter_bar.addWidget(self.btn_filter_useful)

        self.btn_filter_ws = QPushButton("📡 仅看 WebSocket (0)")
        self.btn_filter_ws.setCheckable(True)
        self.btn_filter_ws.setChecked(False)
        self.btn_filter_ws.setCursor(Qt.PointingHandCursor)
        self.btn_filter_ws.setStyleSheet(WS_BADGE_STYLE)
        self.btn_filter_ws.toggled.connect(self._on_ws_filter_toggled)
        table_filter_bar.addWidget(self.btn_filter_ws)

        lbl_url_filter = QLabel("🔗 接口 URL:")
        lbl_url_filter.setStyleSheet("font-weight: bold; color: #334155; margin-left: 4px;")
        table_filter_bar.addWidget(lbl_url_filter)

        self.url_filter_input = QLineEdit()
        self.url_filter_input.setPlaceholderText("输入 URL 或路径模糊匹配...")
        self.url_filter_input.setClearButtonEnabled(True)
        self.url_filter_input.textChanged.connect(self._apply_table_filters)
        table_filter_bar.addWidget(self.url_filter_input, 1)

        self.btn_check_visible = QPushButton("☑️ 勾选可见")
        self.btn_check_visible.setCursor(Qt.PointingHandCursor)
        self.btn_check_visible.setStyleSheet("padding: 4px 8px; font-size: 11px;")
        self.btn_check_visible.clicked.connect(self._check_all_visible_requests)
        table_filter_bar.addWidget(self.btn_check_visible)

        self.btn_clear_checks = QPushButton("⬜ 取消勾选")
        self.btn_clear_checks.setCursor(Qt.PointingHandCursor)
        self.btn_clear_checks.setStyleSheet("padding: 4px 8px; font-size: 11px;")
        self.btn_clear_checks.clicked.connect(self._clear_all_checks)
        table_filter_bar.addWidget(self.btn_clear_checks)

        self.btn_ai_find_useful = QPushButton("🤖 AI 甄别")
        self.btn_ai_find_useful.setCursor(Qt.PointingHandCursor)
        self.btn_ai_find_useful.setStyleSheet("background: #0284c7; color: white; font-weight: bold; padding: 4px 10px; border-radius: 4px; font-size: 11px;")
        self.btn_ai_find_useful.clicked.connect(self._ai_identify_useful_apis)
        table_filter_bar.addWidget(self.btn_ai_find_useful)

        self.lbl_table_stats = QLabel("共 0 条")
        self.lbl_table_stats.setStyleSheet("font-size: 11px; color: #64748b; margin-left: 4px;")
        table_filter_bar.addWidget(self.lbl_table_stats)

        table_layout.addLayout(table_filter_bar)

        self.req_table = QTableWidget()
        self.req_table.setColumnCount(8)
        self.req_table.setHorizontalHeaderLabels(["[☑️]", "#", "状态", "方法", "类型", "URL 地址 (完整路径)", "大小", "时间"])
        self.req_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.req_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.req_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.req_table.setColumnWidth(0, 36)
        self.req_table.setColumnWidth(1, 46)
        self.req_table.setColumnWidth(2, 70)
        self.req_table.setColumnWidth(3, 70)
        self.req_table.setColumnWidth(4, 75)
        self.req_table.setColumnWidth(6, 70)
        self.req_table.setColumnWidth(7, 75)
        self.req_table.itemSelectionChanged.connect(self._on_table_row_selected)
        self.req_table.itemChanged.connect(self._on_table_item_changed)
        table_layout.addWidget(self.req_table)
        left_v_splitter.addWidget(table_group)

        # 下部：请求与响应详情面板 (RequestDetailPanel)
        self.detail_panel = RequestDetailPanel(self)
        self.detail_panel.sig_load_body_requested.connect(self._on_load_body_requested)
        self.detail_panel.sig_discuss_in_chat.connect(self._on_discuss_in_chat)
        self.detail_panel.sig_ws_send_to_ai.connect(self._on_ws_send_to_ai)
        left_v_splitter.addWidget(self.detail_panel)
        left_v_splitter.setStretchFactor(0, 3)
        left_v_splitter.setStretchFactor(1, 2)

        # ── 右侧面板：AI 逆向副驾驶 & Python 脚本运行工作台 ──
        self.ai_panel = AIChatPanel(lambda: self.config, self)

        main_h_splitter.addWidget(left_v_splitter)
        main_h_splitter.addWidget(self.ai_panel)
        main_h_splitter.setStretchFactor(0, 11)
        main_h_splitter.setStretchFactor(1, 9)
        main_h_splitter.setSizes([750, 650])

        root_layout.addWidget(main_h_splitter, 1)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪 - 请选择页面并点击「开始监听」")

    def _apply_styles(self):
        self.setFont(QFont("Microsoft YaHei", 9))
        self.setStyleSheet(MAIN_STYLE)

    def _refresh_targets(self):
        url = self.url_input.text().strip()
        self.status_bar.showMessage("正在发现 Chrome 页面...")
        self.cdp_runner.discover(url)

    def _on_targets_updated(self, targets: list):
        self.tab_combo.clear()
        for t in targets:
            display_title = t.title[:45] if t.title else "未命名页面"
            self.tab_combo.addItem(f"{display_title} ({t.url[:50]})", userData=t)

        if targets:
            self.status_bar.showMessage(f"成功发现 {len(targets)} 个页面，请选择并启动监听")
        else:
            self.status_bar.showMessage("未发现可调试页面，请确认已带 --remote-debugging-port 启动 Chrome")

    def _on_attach_clicked(self):
        tab: Optional[BrowserTab] = self.tab_combo.currentData()
        if not tab:
            QMessageBox.warning(self, "提示", "请先选择一个有效的浏览器页面！")
            return
        self.status_bar.showMessage(f"正在连接到: {tab.title}...")
        self.cdp_runner.connect_tab(tab)

    def _on_detach_clicked(self):
        self.cdp_runner.disconnect_client()

    def _manual_ws_connect(self):
        ws_url, ok = QInputDialog.getText(
            self,
            "手动连接 DevTools WebSocket",
            "请输入 Chrome DevTools WebSocket 地址 (ws://...):",
            text="ws://127.0.0.1:9222/devtools/page/"
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

        chk_item = QTableWidgetItem()
        chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        chk_item.setCheckState(Qt.CheckState.Unchecked)
        chk_item.setData(Qt.ItemDataRole.UserRole, req)
        self.req_table.setItem(row, 0, chk_item)

        self.req_table.setItem(row, 1, QTableWidgetItem(str(row + 1)))

        if getattr(req, "is_websocket", False):
            status_item = QTableWidgetItem("101 (WS)")
            status_item.setForeground(QColor("#7e22ce"))
            status_item.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
        else:
            status_item = QTableWidgetItem("...")
            status_item.setForeground(QColor("#f59e0b"))
        self.req_table.setItem(row, 2, status_item)

        if getattr(req, "is_websocket", False):
            method_item = QTableWidgetItem("📡 WS")
            method_item.setForeground(QColor("#7e22ce"))
            method_item.setBackground(QColor("#faf5ff"))
            method_item.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
        else:
            method_item = QTableWidgetItem(req.method)
            if is_useful_request(req):
                method_item.setForeground(QColor("#2563eb"))
        self.req_table.setItem(row, 3, method_item)

        self.req_table.setItem(row, 4, QTableWidgetItem(req.resource_type))

        url_item = QTableWidgetItem(req.url)
        if getattr(req, "is_websocket", False):
            url_item.setToolTip("📡 WebSocket 双向消息流通道")
        elif is_useful_request(req):
            url_item.setToolTip("⭐ 判定为核心业务数据接口 (非埋点/非静态)")
        self.req_table.setItem(row, 5, url_item)

        self.req_table.setItem(row, 6, QTableWidgetItem(req.formatted_size if getattr(req, "is_websocket", False) else "-"))
        self.req_table.setItem(row, 7, QTableWidgetItem(req.time_str))

        self._check_row_visibility(row, req)
        self._update_ws_filter_btn_label()
        self._update_stats_label()

    def _on_response_received(self, req_id: str, status: int, status_text: str, headers: dict, mime: str):
        if req_id in self.req_id_to_row:
            row = self.req_id_to_row[req_id]
            req = self.cdp_runner.requests_map.get(req_id)
            if req and getattr(req, "is_websocket", False):
                status_item = QTableWidgetItem(f"101 ({len(req.ws_frames)} 帧)")
                status_item.setForeground(QColor("#7e22ce"))
                status_item.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
            else:
                status_item = QTableWidgetItem(str(status))
                if 200 <= status < 300:
                    status_item.setForeground(QColor("#16a34a"))
                elif 300 <= status < 400:
                    status_item.setForeground(QColor("#0284c7"))
                else:
                    status_item.setForeground(QColor("#dc2626"))
            self.req_table.setItem(row, 2, status_item)

    def _on_loading_finished(self, req_id: str, size: int):
        if req_id in self.req_id_to_row:
            row = self.req_id_to_row[req_id]
            req = self.cdp_runner.requests_map.get(req_id)
            if req:
                self.req_table.setItem(row, 6, QTableWidgetItem(req.formatted_size))

    def _on_loading_failed(self, req_id: str, err: str):
        if req_id in self.req_id_to_row:
            row = self.req_id_to_row[req_id]
            status_item = QTableWidgetItem("Failed")
            status_item.setForeground(QColor("#dc2626"))
            self.req_table.setItem(row, 2, status_item)

    def _on_body_ready(self, req_id: str, body: str, is_b64: bool):
        self.detail_panel.set_response_body(req_id, body)

    def _on_ws_frame_added(self, req_id: str, frame: WebSocketFrame):
        if req_id in self.req_id_to_row:
            row = self.req_id_to_row[req_id]
            req = self.cdp_runner.requests_map.get(req_id)
            if req:
                cnt = len(req.ws_frames)
                status_item = QTableWidgetItem(f"101 ({cnt} 帧)")
                status_item.setForeground(QColor("#7e22ce"))
                status_item.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
                self.req_table.setItem(row, 2, status_item)
                self.req_table.setItem(row, 6, QTableWidgetItem(req.formatted_size))

        self._update_ws_filter_btn_label()
        self.detail_panel.append_ws_frame(req_id, frame)

    def _on_ws_closed(self, req_id: str):
        if req_id in self.req_id_to_row:
            row = self.req_id_to_row[req_id]
            req = self.cdp_runner.requests_map.get(req_id)
            cnt = len(req.ws_frames) if req else 0
            status_item = QTableWidgetItem(f"Closed ({cnt} 帧)")
            status_item.setForeground(QColor("#64748b"))
            self.req_table.setItem(row, 2, status_item)

    def _on_table_row_selected(self):
        selected_rows = self.req_table.selectedItems()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        if row < len(self.requests):
            req = self.requests[row]
            self.selected_request = req
            self.detail_panel.load_request(req)
            self._update_context_label()

    def _on_load_body_requested(self, req_id: str):
        self.cdp_runner.fetch_response_body(req_id)

    def _on_discuss_in_chat(self, req: NetworkRequest):
        url_p = urlparse(req.url).path
        prompt = f"请帮我深度剖析选中的接口 `[{req.method}] {url_p}`：它的业务用途、关键参数、以及返回值 JSON 的具体数据字段路径。"
        self._trigger_ai_query(prompt, target_reqs=[req])

    def _on_ws_send_to_ai(self, req: NetworkRequest):
        cnt = len(req.ws_frames)
        prompt = (
            f"我正在逆向分析这个 WebSocket 长连接接口: `{req.url}`。\n"
            f"目前已捕获到 {cnt} 条双向交互帧。请帮我深入分析：\n"
            f"1. 客户端发出的初始握手/鉴权/订阅包结构是什么？\n"
            f"2. 服务端推送的数据包采用什么序列化协议（JSON / Protobuf 等）？字段有哪些含义？\n"
            f"3. 它的心跳保活机制（Ping/Pong 频率与包体特征）是怎样的？\n"
            f"4. 请给出用 Python `websockets` 库连接并持续监听接收解析消息的完整演示代码。"
        )
        self._trigger_ai_query(prompt, target_reqs=[req])

    def _on_useful_filter_toggled(self, checked: bool):
        if checked:
            self.btn_filter_useful.setText("✅ 正在筛选: 仅看有用数据接口")
            if hasattr(self, "btn_filter_ws") and self.btn_filter_ws.isChecked():
                self.btn_filter_ws.blockSignals(True)
                self.btn_filter_ws.setChecked(False)
                self.btn_filter_ws.blockSignals(False)
                self._update_ws_filter_btn_label()
        else:
            self.btn_filter_useful.setText("⭐ 仅看有用数据接口")
        self._apply_table_filters()

    def _on_ws_filter_toggled(self, checked: bool):
        if checked:
            if hasattr(self, "btn_filter_useful") and self.btn_filter_useful.isChecked():
                self.btn_filter_useful.blockSignals(True)
                self.btn_filter_useful.setChecked(False)
                self.btn_filter_useful.setText("⭐ 仅看有用数据接口")
                self.btn_filter_useful.blockSignals(False)
        self._update_ws_filter_btn_label()
        self._apply_table_filters()

    def _update_ws_filter_btn_label(self):
        if hasattr(self, "btn_filter_ws"):
            ws_cnt = sum(1 for r in self.requests if getattr(r, "is_websocket", False) or r.resource_type.lower() == "websocket" or r.url.startswith(("ws://", "wss://")))
            if self.btn_filter_ws.isChecked():
                self.btn_filter_ws.setText(f"✅ 正在筛选: WebSocket ({ws_cnt})")
            else:
                self.btn_filter_ws.setText(f"📡 仅看 WebSocket ({ws_cnt})")

    def _ai_identify_useful_apis(self):
        if not self.requests:
            QMessageBox.information(self, "提示", "当前尚未捕获到网络流量，请先启动监听并在浏览器中操作页面！")
            return
        prompt = (
            "请帮我全面梳理当前捕获的网络流量，剔除无用埋点与静态文件，"
            "挑出真正有用的核心业务数据接口（如作品流、视频详情、评论、用户信息、搜索等），"
            "列出各接口的 URL 路径特征、核心功能、以及关键数据字段结构。"
        )
        self._trigger_ai_query(prompt)

    def _trigger_ai_query(self, user_text: str, target_reqs: Optional[list[NetworkRequest]] = None):
        targets = target_reqs if target_reqs else self._get_checked_requests()
        if not targets and self.selected_request:
            targets = [self.selected_request]

        missing_body_reqs = [r for r in targets if not r.response_body and not getattr(r, "is_websocket", False)]
        for r in missing_body_reqs:
            body, _ = self.cdp_runner.fetch_response_body_sync(r.request_id, timeout=1.2)
            if body:
                r.response_body = body

        snapshot = AIAnalyzer.build_context_snapshot(
            self.requests,
            selected_req=self.selected_request,
            target_reqs=targets,
        )

        self.ai_panel.send_user_message(user_text, context_snapshot=snapshot)
        self._update_context_label()

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

        self._update_stats_label(visible_cnt=visible_cnt, total=total, useful_cnt=useful_cnt)

    def _check_row_visibility(self, row: int, req: NetworkRequest, filter_type=None, keyword=None, url_query=None):
        if filter_type is None:
            filter_type = self.filter_combo.currentText()
        if keyword is None:
            keyword = self.search_input.text().strip().lower()
        if url_query is None:
            url_query = self.url_filter_input.text().strip().lower() if hasattr(self, "url_filter_input") else ""

        if hasattr(self, "chk_exclude_telemetry") and self.chk_exclude_telemetry.isChecked() and is_telemetry_request(req.url):
            self.req_table.setRowHidden(row, True)
            return

        if hasattr(self, "btn_filter_useful") and self.btn_filter_useful.isChecked():
            if not is_useful_request(req):
                self.req_table.setRowHidden(row, True)
                return

        if hasattr(self, "btn_filter_ws") and self.btn_filter_ws.isChecked():
            is_ws = getattr(req, "is_websocket", False) or req.resource_type.lower() == "websocket" or req.url.startswith(("ws://", "wss://"))
            if not is_ws:
                self.req_table.setRowHidden(row, True)
                return

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
        if filter_type == "仅 XHR / Fetch":
            visible = req.is_xhr or req.resource_type.lower() in ("xhr", "fetch")
        elif filter_type == "WebSocket":
            visible = getattr(req, "is_websocket", False) or req.resource_type.lower() == "websocket" or req.url.startswith(("ws://", "wss://"))
        elif filter_type == "Document":
            visible = req.resource_type.lower() == "document"
        elif filter_type == "Script":
            visible = req.resource_type.lower() == "script"
        elif filter_type == "Image":
            visible = req.resource_type.lower() == "image"
        elif filter_type == "其它":
            visible = req.resource_type.lower() not in ("xhr", "fetch", "document", "script", "image", "websocket") and not getattr(req, "is_websocket", False)

        if visible and keyword:
            visible = keyword in req.url.lower() or keyword in req.method.lower()

        self.req_table.setRowHidden(row, not visible)

    def _clear_requests(self):
        self.requests.clear()
        self.req_id_to_row.clear()
        self.req_table.setRowCount(0)
        self.selected_request = None
        self.detail_panel.clear()
        self._update_ws_filter_btn_label()
        self._update_stats_label()
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
            item_data = {
                "request_id": r.request_id,
                "timestamp": r.time_str,
                "method": r.method,
                "url": r.url,
                "resource_type": r.resource_type,
                "status": r.response_status,
                "status_text": r.response_status_text,
                "headers": r.headers,
                "response_headers": r.response_headers,
                "response_body": r.response_body,
                "post_data": r.post_data,
                "is_useful": is_useful_request(r)
            }
            if getattr(r, "is_websocket", False):
                item_data["ws_frames_count"] = len(r.ws_frames)
                item_data["ws_frames"] = [
                    {"id": f.frame_id, "time": f.time_str, "direction": f.direction, "opcode": f.opcode_str, "size": f.size, "payload": f.payload_data}
                    for f in r.ws_frames
                ]
            data.append(item_data)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            QMessageBox.information(self, "导出成功", f"已成功导出 {len(data)} 条请求数据至:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"保存 JSON 文件异常: {e}")

    def _get_checked_requests(self) -> list[NetworkRequest]:
        checked = []
        for row in range(self.req_table.rowCount()):
            item = self.req_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                req = item.data(Qt.ItemDataRole.UserRole)
                if req:
                    checked.append(req)
        return checked

    def _on_table_item_changed(self, item: QTableWidgetItem):
        if item.column() == 0:
            self._update_context_label()
            self._update_stats_label()

    def _check_all_visible_requests(self):
        cnt = 0
        self.req_table.blockSignals(True)
        for row in range(self.req_table.rowCount()):
            if not self.req_table.isRowHidden(row):
                item = self.req_table.item(row, 0)
                if item:
                    item.setCheckState(Qt.CheckState.Checked)
                    cnt += 1
        self.req_table.blockSignals(False)
        self._update_context_label()
        self._update_stats_label()
        self.status_bar.showMessage(f"已勾选 {cnt} 条当前可见请求")

    def _clear_all_checks(self):
        self.req_table.blockSignals(True)
        for row in range(self.req_table.rowCount()):
            item = self.req_table.item(row, 0)
            if item:
                item.setCheckState(Qt.CheckState.Unchecked)
        self.req_table.blockSignals(False)
        self._update_context_label()
        self._update_stats_label()
        self.status_bar.showMessage("已清空所有勾选")

    def _update_stats_label(self, visible_cnt=None, total=None, useful_cnt=None):
        if not hasattr(self, "lbl_table_stats"):
            return
        if total is None:
            total = len(self.requests)
        if useful_cnt is None:
            useful_cnt = sum(1 for r in self.requests if is_useful_request(r))
        checked = len(self._get_checked_requests())
        checked_tip = f" | 已勾选 {checked} 条" if checked else ""

        if visible_cnt is not None:
            self.lbl_table_stats.setText(f"显示 {visible_cnt}/{total} 条 (核心业务: {useful_cnt} 条{checked_tip})")
        else:
            self.lbl_table_stats.setText(f"共 {total} 条 (核心业务: {useful_cnt} 条{checked_tip})")

    def _update_context_label(self):
        checked_reqs = self._get_checked_requests()
        if checked_reqs:
            cnt = len(checked_reqs)
            ws_cnt = sum(1 for r in checked_reqs if getattr(r, "is_websocket", False))
            ws_tip = f" (含 {ws_cnt} 个 WS 长连接)" if ws_cnt else ""
            self.ai_panel.update_context_label(f"📌 重点分析上下文: 已勾选 {cnt} 个指定接口{ws_tip}")
        elif self.selected_request:
            req = self.selected_request
            p = urlparse(req.url).path
            is_ws = " [📡 WebSocket]" if getattr(req, "is_websocket", False) else ""
            self.ai_panel.update_context_label(f"📌 重点分析上下文: [{req.method}] `{p}` ({req.response_status or 'Pending'}){is_ws}")
        else:
            total = len(self.requests)
            self.ai_panel.update_context_label(f"📌 实时上下文: 未选择特定请求 | 捕获 {total} 条流量")

    def _show_config(self):
        dialog = ConfigDialog(self, self.config, self._save_config)
        dialog.exec()

    def closeEvent(self, event):
        self.cdp_runner.stop()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = CDPMonitorApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
