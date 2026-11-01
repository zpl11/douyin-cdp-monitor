import json
from typing import Optional
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..models import NetworkRequest, WebSocketFrame
from .ws_panel import WebSocketPanel


class RequestDetailPanel(QGroupBox):
    """请求与响应详情面板 (包含概览、Query、POST、响应头、响应体、WebSocket 帧)"""

    sig_load_body_requested = Signal(str)  # request_id
    sig_discuss_in_chat = Signal(object)  # NetworkRequest
    sig_ws_send_to_ai = Signal(object)  # NetworkRequest

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("请求与响应详情 (Details)", parent)
        self.current_req: Optional[NetworkRequest] = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        action_bar = QHBoxLayout()
        self.btn_detail_discuss = QPushButton("💬 在 AI 对话中深入探讨此接口")
        self.btn_detail_discuss.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_detail_discuss.setStyleSheet(
            "background-color: #0284c7; color: white; font-weight: bold; padding: 6px 14px; border-radius: 4px;"
        )
        self.btn_detail_discuss.clicked.connect(self._on_discuss_clicked)
        action_bar.addWidget(self.btn_detail_discuss)
        layout.addLayout(action_bar)

        self.detail_tabs = QTabWidget()

        self.txt_general = QTextEdit()
        self.txt_general.setReadOnly(True)
        self.detail_tabs.addTab(self.txt_general, "概览与请求头")

        self.txt_query = QTextEdit()
        self.txt_query.setReadOnly(True)
        self.detail_tabs.addTab(self.txt_query, "Query 参数")

        self.txt_post = QTextEdit()
        self.txt_post.setReadOnly(True)
        self.detail_tabs.addTab(self.txt_post, "请求体 (POST)")

        self.txt_resp_headers = QTextEdit()
        self.txt_resp_headers.setReadOnly(True)
        self.detail_tabs.addTab(self.txt_resp_headers, "响应头")

        resp_body_widget = QWidget()
        resp_body_layout = QVBoxLayout(resp_body_widget)
        resp_body_toolbar = QHBoxLayout()
        self.btn_load_body = QPushButton("📥 获取/刷新响应体")
        self.btn_load_body.clicked.connect(self._on_load_body_clicked)
        resp_body_toolbar.addWidget(self.btn_load_body)
        resp_body_toolbar.addStretch()
        resp_body_layout.addLayout(resp_body_toolbar)

        self.txt_resp_body = QTextEdit()
        self.txt_resp_body.setReadOnly(True)
        resp_body_layout.addWidget(self.txt_resp_body)
        self.detail_tabs.addTab(resp_body_widget, "响应体 (Body)")

        # WebSocket 专属面板
        self.ws_panel = WebSocketPanel(self)
        self.ws_panel.sig_send_to_ai.connect(self.sig_ws_send_to_ai.emit)
        self.detail_tabs.addTab(self.ws_panel, "📡 WebSocket 消息帧")

        layout.addWidget(self.detail_tabs)

    def _on_discuss_clicked(self):
        if self.current_req:
            self.sig_discuss_in_chat.emit(self.current_req)

    def _on_load_body_clicked(self):
        if self.current_req:
            self.txt_resp_body.setText("正在通过 CDP Network.getResponseBody 读取响应体...")
            self.sig_load_body_requested.emit(self.current_req.request_id)

    def load_request(self, req: Optional[NetworkRequest]):
        self.current_req = req
        if not req:
            self.clear()
            return

        overview_lines = [
            "=== 通用信息 ===",
            f"URL: {req.url}",
            f"方法: {req.method}",
            f"状态: {req.response_status} {req.response_status_text}",
            f"类型: {req.resource_type}",
            f"大小: {req.formatted_size}",
            f"捕获时间: {req.time_str}",
            "",
            "=== 请求头 (Request Headers) ===",
        ]
        for k, v in req.headers.items():
            overview_lines.append(f"{k}: {v}")
        self.txt_general.setText("\n".join(overview_lines))

        parsed = urlparse(req.url)
        query_dict = parse_qs(parsed.query)
        if query_dict:
            q_lines = [f"{k}: {', '.join(v)}" for k, v in query_dict.items()]
            self.txt_query.setText("\n".join(q_lines))
        else:
            self.txt_query.setText("无 Query 参数")

        if req.post_data:
            try:
                pretty_post = json.dumps(json.loads(req.post_data), indent=2, ensure_ascii=False)
                self.txt_post.setText(pretty_post)
            except Exception:
                self.txt_post.setText(req.post_data)
        else:
            self.txt_post.setText("无请求体数据 (GET 或空 Payload)")

        if req.response_headers:
            resp_lines = [f"{k}: {v}" for k, v in req.response_headers.items()]
            self.txt_resp_headers.setText("\n".join(resp_lines))
        else:
            self.txt_resp_headers.setText("暂未收到响应头")

        if req.response_body:
            try:
                pretty = json.dumps(json.loads(req.response_body), indent=2, ensure_ascii=False)
                self.txt_resp_body.setText(pretty)
            except Exception:
                self.txt_resp_body.setText(req.response_body)
        else:
            self.txt_resp_body.setText("点击上方「📥 获取/刷新响应体」拉取完整 Response Body 数据...")

        if getattr(req, "is_websocket", False):
            self.ws_panel.load_request(req)
            cnt = len(req.ws_frames)
            self.detail_tabs.setTabText(5, f"📡 WebSocket 消息帧 ({cnt} 帧)")
            self.detail_tabs.setCurrentIndex(5)
        else:
            self.ws_panel.load_request(None)
            self.detail_tabs.setTabText(5, "📡 WebSocket 消息帧")

    def set_response_body(self, req_id: str, body: str):
        if self.current_req and self.current_req.request_id == req_id:
            self.current_req.response_body = body
            try:
                pretty = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
                self.txt_resp_body.setText(pretty)
            except Exception:
                self.txt_resp_body.setText(body)

    def append_ws_frame(self, req_id: str, frame: WebSocketFrame):
        if self.current_req and self.current_req.request_id == req_id:
            self.ws_panel.append_frame(frame)
            cnt = len(self.current_req.ws_frames)
            self.detail_tabs.setTabText(5, f"📡 WebSocket 消息帧 ({cnt} 帧)")

    def clear(self):
        self.current_req = None
        self.txt_general.clear()
        self.txt_query.clear()
        self.txt_post.clear()
        self.txt_resp_headers.clear()
        self.txt_resp_body.clear()
        self.ws_panel.clear()
        self.detail_tabs.setTabText(5, "📡 WebSocket 消息帧")
