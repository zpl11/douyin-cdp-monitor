import json
from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..models import NetworkRequest, WebSocketFrame


class WebSocketPanel(QWidget):
    """WebSocket 专属双向消息帧透视面板"""
    sig_send_to_ai = Signal(object)  # 发送目标 NetworkRequest 给 AI 分析

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_req: Optional[NetworkRequest] = None
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # 顶部操作与筛选栏
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        toolbar.addWidget(QLabel("方向:"))
        self.ws_dir_combo = QComboBox()
        self.ws_dir_combo.addItems(["全部", "🟢 仅客户端发送 (Send)", "🔵 仅服务端推送 (Recv)"])
        self.ws_dir_combo.currentIndexChanged.connect(self._apply_frame_filter)
        toolbar.addWidget(self.ws_dir_combo)

        toolbar.addWidget(QLabel("过滤 Payload:"))
        self.ws_search_input = QLineEdit()
        self.ws_search_input.setPlaceholderText("过滤帧内容关键字...")
        self.ws_search_input.textChanged.connect(self._apply_frame_filter)
        toolbar.addWidget(self.ws_search_input, 1)

        self.chk_autoscroll = QCheckBox("自动滚到底部")
        self.chk_autoscroll.setChecked(True)
        toolbar.addWidget(self.chk_autoscroll)

        self.btn_send_to_ai = QPushButton("🤖 送入 AI 分析帧序列")
        self.btn_send_to_ai.setStyleSheet("background: #0284c7; color: white; font-weight: bold; padding: 4px 10px; border-radius: 4px;")
        self.btn_send_to_ai.clicked.connect(self._on_send_to_ai_clicked)
        toolbar.addWidget(self.btn_send_to_ai)

        layout.addLayout(toolbar)

        # 垂直分割器：上方帧列表，下方帧 Payload
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setHandleWidth(5)

        # 上方：帧列表表格
        self.ws_table = QTableWidget()
        self.ws_table.setColumnCount(5)
        self.ws_table.setHorizontalHeaderLabels(["#", "方向", "时间", "大小", "数据摘要 (Payload Preview)"])
        self.ws_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.ws_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.ws_table.setColumnWidth(0, 45)
        self.ws_table.setColumnWidth(1, 80)
        self.ws_table.setColumnWidth(2, 90)
        self.ws_table.setColumnWidth(3, 70)
        self.ws_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.ws_table.itemSelectionChanged.connect(self._on_frame_selected)
        splitter.addWidget(self.ws_table)

        # 下方：单帧完整内容展示
        frame_detail_widget = QWidget()
        frame_detail_layout = QVBoxLayout(frame_detail_widget)
        frame_detail_layout.setContentsMargins(0, 0, 0, 0)
        frame_detail_layout.setSpacing(4)

        frame_detail_toolbar = QHBoxLayout()
        self.lbl_selected_frame_info = QLabel("选中帧详情: 未选择")
        self.lbl_selected_frame_info.setStyleSheet("font-size: 11px; color: #475569; font-weight: bold;")
        frame_detail_toolbar.addWidget(self.lbl_selected_frame_info, 1)

        self.btn_format_json = QPushButton("⚡ JSON 格式化")
        self.btn_format_json.clicked.connect(self._format_selected_payload)
        frame_detail_toolbar.addWidget(self.btn_format_json)

        self.btn_copy_payload = QPushButton("📋 复制 Payload")
        self.btn_copy_payload.clicked.connect(self._copy_selected_payload)
        frame_detail_toolbar.addWidget(self.btn_copy_payload)

        frame_detail_layout.addLayout(frame_detail_toolbar)

        self.txt_frame_detail = QTextEdit()
        self.txt_frame_detail.setReadOnly(True)
        self.txt_frame_detail.setPlaceholderText("选择上方一条消息帧以查看完整 Payload 数据 (支持 JSON / Protobuf 诊断)...")
        frame_detail_layout.addWidget(self.txt_frame_detail)

        splitter.addWidget(frame_detail_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout.addWidget(splitter)

    def load_request(self, req: Optional[NetworkRequest]):
        self.current_req = req
        self.ws_table.setRowCount(0)
        if not req or not getattr(req, "is_websocket", False):
            self.lbl_selected_frame_info.setText("当前为普通 HTTP 请求 (无 WS 数据帧)")
            self.txt_frame_detail.setText("当前选中的是常规 HTTP/HTTPS 请求。\n如需查看实时双向数据帧，请在上方列表中点击类型为 📡 WS 的接口。")
            return

        for f in req.ws_frames:
            self.append_frame(f)

        cnt = len(req.ws_frames)
        self.lbl_selected_frame_info.setText(f"当前长连接累计捕获 {cnt} 帧消息")
        self.txt_frame_detail.setText("请在上方表格中选择一条消息帧以查看完整的 Payload 内容...")

    def append_frame(self, frame: WebSocketFrame):
        direction_filter = self.ws_dir_combo.currentText()
        keyword = self.ws_search_input.text().strip().lower()

        if "仅客户端发送" in direction_filter and frame.direction != "send":
            return
        if "仅服务端推送" in direction_filter and frame.direction != "recv":
            return
        if keyword and keyword not in frame.payload_data.lower():
            return

        row = self.ws_table.rowCount()
        self.ws_table.insertRow(row)

        # Col 0: #
        self.ws_table.setItem(row, 0, QTableWidgetItem(str(frame.frame_id)))

        # Col 1: 方向
        if frame.direction == "send":
            dir_item = QTableWidgetItem("🟢 发送")
            dir_item.setForeground(QColor("#16a34a"))
        else:
            dir_item = QTableWidgetItem("🔵 接收")
            dir_item.setForeground(QColor("#2563eb"))
        self.ws_table.setItem(row, 1, dir_item)

        # Col 2: 时间
        self.ws_table.setItem(row, 2, QTableWidgetItem(frame.time_str))

        # Col 3: 大小
        self.ws_table.setItem(row, 3, QTableWidgetItem(f"{frame.size} B"))

        # Col 4: 摘要
        preview = frame.payload_data.replace("\r", " ").replace("\n", " ").strip()
        if len(preview) > 120:
            preview = preview[:120] + "..."
        summary_item = QTableWidgetItem(f"[{frame.opcode_str}] {preview}")
        summary_item.setData(Qt.ItemDataRole.UserRole, frame)
        self.ws_table.setItem(row, 4, summary_item)

        if self.chk_autoscroll.isChecked():
            self.ws_table.scrollToBottom()

    def _on_frame_selected(self):
        selected = self.ws_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        item = self.ws_table.item(row, 4)
        if not item:
            return
        frame = item.data(Qt.ItemDataRole.UserRole)
        if not frame:
            return

        arrow = "🟢 客户端发出 (Client -> Server)" if frame.direction == "send" else "🔵 服务端推送 (Server -> Client)"
        self.lbl_selected_frame_info.setText(
            f"帧 #{frame.frame_id} | {arrow} | {frame.opcode_str} | 大小: {frame.size} 字节 | 时间: {frame.time_str}"
        )

        p = frame.payload_data
        try:
            parsed = json.loads(p)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            self.txt_frame_detail.setText(pretty)
        except Exception:
            self.txt_frame_detail.setText(p)

    def _format_selected_payload(self):
        curr = self.txt_frame_detail.toPlainText()
        if not curr.strip():
            return
        try:
            parsed = json.loads(curr)
            self.txt_frame_detail.setText(json.dumps(parsed, indent=2, ensure_ascii=False))
        except Exception:
            pass

    def _copy_selected_payload(self):
        curr = self.txt_frame_detail.toPlainText()
        if curr:
            QApplication.clipboard().setText(curr)

    def _apply_frame_filter(self):
        if not self.current_req:
            return
        self.ws_table.setRowCount(0)
        for f in self.current_req.ws_frames:
            self.append_frame(f)

    def _on_send_to_ai_clicked(self):
        if self.current_req:
            self.sig_send_to_ai.emit(self.current_req)

    def clear(self):
        self.current_req = None
        self.ws_table.setRowCount(0)
        self.txt_frame_detail.clear()
        self.lbl_selected_frame_info.setText("选中帧详情: 未选择")
