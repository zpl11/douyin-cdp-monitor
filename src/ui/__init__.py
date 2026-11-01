"""UI components package for CDP Monitor."""
from .chat_widgets import AIChatPanel, ChatTextEdit
from .code_runner_panel import CodeRunnerPanel
from .config_dialog import ConfigDialog
from .detail_panel import RequestDetailPanel
from .styles import MAIN_STYLE, USEFUL_FILTER_BTN_STYLE, WS_BADGE_STYLE
from .ws_panel import WebSocketPanel

__all__ = [
    "AIChatPanel",
    "ChatTextEdit",
    "CodeRunnerPanel",
    "ConfigDialog",
    "RequestDetailPanel",
    "WebSocketPanel",
    "MAIN_STYLE",
    "USEFUL_FILTER_BTN_STYLE",
    "WS_BADGE_STYLE",
]
