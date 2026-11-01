"""数据模型定义"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class BrowserTab:
    """一个浏览器标签页"""
    title: str
    url: str
    target_id: str
    type: str = "page"
    ws_url: str = ""


@dataclass
class WebSocketFrame:
    """一条 WebSocket 消息帧"""
    frame_id: int = 0
    timestamp: datetime = field(default_factory=datetime.now)
    direction: str = "recv"  # "send" (发往服务端) 或 "recv" (服务端推送)
    payload_data: str = ""   # 文本/Base64/JSON 字符串
    opcode: int = 1          # 1: Text, 2: Binary, 8: Close, 9: Ping, 10: Pong
    size: int = 0            # 字节大小

    @property
    def time_str(self) -> str:
        return self.timestamp.strftime("%H:%M:%S.%f")[:-3]

    @property
    def opcode_str(self) -> str:
        mapping = {1: "Text", 2: "Binary", 8: "Close", 9: "Ping", 10: "Pong"}
        return mapping.get(self.opcode, f"Opcode({self.opcode})")


@dataclass
class NetworkRequest:
    """一个网络请求的完整记录（支持 HTTP / HTTPS / WebSocket）"""
    request_id: str
    timestamp: datetime = field(default_factory=datetime.now)
    method: str = "GET"
    url: str = ""
    headers: dict = field(default_factory=dict)
    post_data: str = ""
    resource_type: str = "Other"  # XHR, Fetch, Document, Script, Stylesheet, Image, Media, WebSocket, Other
    
    # 响应信息
    response_status: int = 0
    response_status_text: str = ""
    response_headers: dict = field(default_factory=dict)
    response_body: str = ""
    response_size: int = 0
    mime_type: str = ""
    
    # 状态标志
    is_xhr: bool = False
    is_websocket: bool = False
    is_success: bool = True
    error: str = ""
    
    # WebSocket 专属消息帧列表
    ws_frames: list = field(default_factory=list)
    
    # 标签页关联
    tab_title: str = ""
    tab_url: str = ""
    
    # AI 分析结果
    ai_summary: str = ""

    @property
    def time_str(self) -> str:
        return self.timestamp.strftime("%H:%M:%S")

    @property
    def formatted_size(self) -> str:
        if self.response_size <= 0:
            return "-"
        if self.response_size < 1024:
            return f"{self.response_size} B"
        if self.response_size < 1024 * 1024:
            return f"{self.response_size / 1024:.1f} KB"
        return f"{self.response_size / (1024 * 1024):.2f} MB"
