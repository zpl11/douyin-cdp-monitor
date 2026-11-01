import json
import os
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


class ConfigDialog(QDialog):
    """设置对话框，支持实时配置 CDP 端口与 AI 大模型服务参数"""

    def __init__(self, parent, config: dict, on_save_callback):
        super().__init__(parent)
        self.config = config
        self.on_save_callback = on_save_callback
        self.setWindowTitle("⚙️ CDP Monitor 系统设置")
        self.setFixedWidth(460)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        tip_lbl = QLabel("可在此配置 Chrome 远程调试端口以及 AI 逆向副驾驶 API：")
        tip_lbl.setStyleSheet("color: #475569; font-size: 12px;")
        layout.addWidget(tip_lbl)

        form = QFormLayout()
        form.setSpacing(8)

        self.cdp_edit = QLineEdit(str(self.config.get("cdp_port", 9222)))
        form.addRow("Chrome 远程调试端口:", self.cdp_edit)

        self.url_edit = QLineEdit(self.config.get("ai_base_url", "https://api.openai.com/v1"))
        self.url_edit.setPlaceholderText("例如: http://127.0.0.1:11434/v1 或 OpenAI 兼容地址")
        form.addRow("AI Base URL:", self.url_edit)

        self.key_edit = QLineEdit(self.config.get("ai_api_key", ""))
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("私有部署无密钥可填 sk-no-key")
        form.addRow("AI API Key:", self.key_edit)

        self.model_edit = QLineEdit(self.config.get("ai_model", "gpt-4o-mini"))
        self.model_edit.setPlaceholderText("例如: qwen3.8-35b-a3b / deepseek-chat / gpt-4o-mini")
        form.addRow("AI Model 模型名:", self.model_edit)

        layout.addLayout(form)

        btn_box = QHBoxLayout()
        btn_box.setSpacing(8)

        btn_save = QPushButton("💾 保存配置")
        btn_save.setStyleSheet("background-color: #2563eb; color: white; font-weight: bold; padding: 6px 14px; border-radius: 4px;")
        btn_save.clicked.connect(self._save)

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)

        btn_box.addStretch()
        btn_box.addWidget(btn_cancel)
        btn_box.addWidget(btn_save)

        layout.addLayout(btn_box)

    def _save(self):
        try:
            port = int(self.cdp_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "输入错误", "CDP 端口必须为有效整数 (默认 9222)")
            return

        new_cfg = {
            "cdp_port": port,
            "ai_base_url": self.url_edit.text().strip(),
            "ai_api_key": self.key_edit.text().strip(),
            "ai_model": self.model_edit.text().strip()
        }

        if self.on_save_callback:
            self.on_save_callback(new_cfg)

        self.accept()
