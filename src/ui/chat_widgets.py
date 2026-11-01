import html
import re
from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..ai_analyzer import AIAnalyzer, markdown_to_qt_html
from ..workers import AIChatWorker
from .code_runner_panel import CodeRunnerPanel


class ChatTextEdit(QTextEdit):
    """支持 Enter 快捷发送、Shift+Enter 换行的聊天输入框"""
    send_requested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.send_requested.emit()
                event.accept()
        else:
            super().keyPressEvent(event)


class AIChatPanel(QWidget):
    """右侧 AI 逆向副驾驶 & 代码运行器综合面板"""
    sig_request_snapshot = Signal()  # 请求主窗口生成最新的抓包快照
    sig_code_extracted = Signal(str)

    def __init__(self, config_provider: Callable[[], dict], parent=None):
        super().__init__(parent)
        self.config_provider = config_provider
        self.chat_messages: list[dict] = []
        self.ai_chat_worker: Optional[AIChatWorker] = None
        self._current_ai_start_pos: Optional[int] = None
        self._current_ai_raw_chunks: list[str] = []
        self._init_ui()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(4)

        ai_group = QGroupBox("🤖 AI 逆向副驾驶 & Python 代码调试终端")
        ai_group.setMinimumWidth(260)
        ai_layout = QVBoxLayout(ai_group)
        ai_layout.setContentsMargins(8, 8, 8, 8)
        ai_layout.setSpacing(6)

        # 1. 顶部上下文提示与按钮
        ctx_bar = QHBoxLayout()
        ctx_bar.setSpacing(6)
        self.lbl_context = QLabel("📌 实时上下文: 未选择特定请求 | 捕获 0 条流量")
        self.lbl_context.setStyleSheet("font-size: 11px; color: #475569; background: #f1f5f9; padding: 4px 8px; border-radius: 4px;")
        ctx_bar.addWidget(self.lbl_context, 1)

        self.btn_clear_chat = QPushButton("🧹 清空对话")
        self.btn_clear_chat.clicked.connect(self.clear_chat)
        ctx_bar.addWidget(self.btn_clear_chat)

        self.btn_test_model = QPushButton("🧪 测试模型")
        self.btn_test_model.clicked.connect(self.test_model)
        ctx_bar.addWidget(self.btn_test_model)
        ai_layout.addLayout(ctx_bar)

        # 2. 垂直分割器：上半部对话展示，下半部代码运行器
        ai_v_splitter = QSplitter(Qt.Orientation.Vertical)
        ai_v_splitter.setHandleWidth(5)

        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        self.chat_display.setStyleSheet("""
            QTextEdit {
                background-color: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 8px;
            }
        """)
        ai_v_splitter.addWidget(self.chat_display)

        # 嵌入代码运行器
        self.code_runner = CodeRunnerPanel(self)
        self.code_runner.sig_feedback_to_ai.connect(self.send_user_message)
        self.code_runner.sig_code_extracted.connect(self._extract_code_from_chat)
        ai_v_splitter.addWidget(self.code_runner)

        ai_v_splitter.setStretchFactor(0, 5)
        ai_v_splitter.setStretchFactor(1, 4)
        ai_layout.addWidget(ai_v_splitter, 1)

        # 3. 快捷提问模版栏
        chips_layout = QHBoxLayout()
        chips_layout.setSpacing(4)
        lbl_chip = QLabel("💡 快捷灵感:")
        lbl_chip.setStyleSheet("font-size: 11px; color: #475569; font-weight: bold;")
        chips_layout.addWidget(lbl_chip)

        self.chips_data = [
            ("🎯 挑出核心业务接口", "请帮我梳理当前捕获的流量，剔除埋点与监控，挑出真正有用的核心业务数据接口并说明各自用途。"),
            ("🔬 深度剖析当前接口", "请帮我深度剖析当前选中的这个接口：它的业务用途、关键参数、以及返回值 JSON 的具体数据字段路径。"),
            ("🐍 生成 Python 爬虫代码", "针对当前选中的接口，请帮我编写一份完整的 Python requests 脚本，包含必要请求头与核心字段提取代码。"),
            ("🔐 解释防刷/签名参数", "当前选中的接口有哪些防刷或加密签名参数（如 a_bogus 等）？如果不传或伪造会发生什么？"),
            ("📊 提取返回值字段结构", "请将当前选中的接口返回值进行树状解析，指出核心列表在哪里、标题在哪里、播放或下载地址在哪个字段。"),
        ]

        self.combo_prompt_templates = QComboBox()
        self.combo_prompt_templates.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.combo_prompt_templates.addItem("✨ 选择提问模版直接填入...", "")
        for label, prompt_text in self.chips_data:
            self.combo_prompt_templates.addItem(label, prompt_text)
        self.combo_prompt_templates.currentIndexChanged.connect(self._on_prompt_template_selected)
        chips_layout.addWidget(self.combo_prompt_templates, 1)

        btn_chip_core = QPushButton("🎯 挑核心")
        btn_chip_core.setCursor(Qt.PointingHandCursor)
        btn_chip_core.setStyleSheet("background: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 4px; padding: 2px 6px; font-size: 11px;")
        btn_chip_core.clicked.connect(lambda: self._on_chip_clicked(self.chips_data[0][1]))
        chips_layout.addWidget(btn_chip_core)

        btn_chip_code = QPushButton("🐍 写代码")
        btn_chip_code.setCursor(Qt.PointingHandCursor)
        btn_chip_code.setStyleSheet("background: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 4px; padding: 2px 6px; font-size: 11px;")
        btn_chip_code.clicked.connect(lambda: self._on_chip_clicked(self.chips_data[2][1]))
        chips_layout.addWidget(btn_chip_code)

        ai_layout.addLayout(chips_layout)

        # 4. 对话输入框与发送按钮
        input_box_layout = QHBoxLayout()
        input_box_layout.setSpacing(6)

        self.chat_input = ChatTextEdit()
        self.chat_input.setFixedHeight(54)
        self.chat_input.setPlaceholderText("在此输入与 AI 交流的问题... 支持探讨接口、解析字段、编写爬虫代码等 (Enter 发送，Shift+Enter 换行)")
        self.chat_input.send_requested.connect(self._on_send_clicked)
        input_box_layout.addWidget(self.chat_input, 1)

        self.btn_send = QPushButton("🚀 发送\n(Enter)")
        self.btn_send.setFixedSize(72, 54)
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
        self.btn_send.clicked.connect(self._on_send_clicked)
        input_box_layout.addWidget(self.btn_send)

        ai_layout.addLayout(input_box_layout)
        main_layout.addWidget(ai_group)

        self.clear_chat()

    def update_context_label(self, text: str):
        self.lbl_context.setText(text)

    def _on_prompt_template_selected(self, index: int):
        if index <= 0:
            return
        prompt_text = self.combo_prompt_templates.itemData(index)
        if prompt_text:
            self._on_chip_clicked(prompt_text)
        self.combo_prompt_templates.blockSignals(True)
        self.combo_prompt_templates.setCurrentIndex(0)
        self.combo_prompt_templates.blockSignals(False)

    def _on_chip_clicked(self, prompt_text: str):
        self.chat_input.setText(prompt_text)
        self.chat_input.setFocus()

    def clear_chat(self):
        self.chat_messages = [AIAnalyzer.build_system_message()]
        welcome_html = """
        <div style="font-family: 'Microsoft YaHei', sans-serif; line-height: 1.5; color: #1e293b; padding: 8px;">
            <div style="font-size: 14px; font-weight: bold; color: #1d4ed8; margin-bottom: 6px;">
                🚀 AI 逆向副驾驶 & Python 执行器已就绪
            </div>
            <div style="font-size: 12px; color: #475569; margin-bottom: 8px;">
                我是一名专注于现代 Web 前端逆向、反爬分析与爬虫编写的副驾驶助手。
            </div>
            <div style="background-color: #f8fafc; border-left: 3px solid #3b82f6; padding: 6px 10px; font-size: 11.5px; color: #334155; margin-bottom: 6px;">
                💡 <b>使用技巧</b>：<br/>
                1. 在左侧表格中点击某个接口或勾选重点接口，即可在提问时代入该接口参数与返回值；<br/>
                2. 支持直接让 AI 生成 Python 脚本，生成后自动提取至下方「交互式代码运行器」，点击「▶️ 运行」即可本地测试！
            </div>
        </div>
        """
        self.chat_display.setHtml(welcome_html)
        self._current_ai_raw_chunks = []
        self._current_ai_start_pos = None

    def _on_send_clicked(self):
        text = self.chat_input.toPlainText().strip()
        if not text:
            return
        self.chat_input.clear()
        self.send_user_message(text)

    def send_user_message(self, user_text: str, context_snapshot: str = ""):
        self.btn_send.setEnabled(False)
        time_str = datetime.now().strftime("%H:%M:%S")

        # 渲染用户气泡
        user_escaped = html.escape(user_text).replace("\n", "<br/>")
        user_html = f"""
        <table width="100%" border="0" cellpadding="8" cellspacing="0" bgcolor="#f8fafc" style="border: 1px solid #e2e8f0; border-radius: 6px; margin-top: 10px; margin-bottom: 6px;">
        <tr>
            <td>
                <b style="color: #0f172a; font-size: 13px;">👤 用户提问</b> <span style="color: #94a3b8; font-size: 11px;">({time_str})</span>:
                <div style="margin-top: 4px; font-size: 12.5px; color: #1e293b; line-height: 1.5;">{user_escaped}</div>
            </td>
        </tr>
        </table>
        """
        self.chat_display.append(user_html)
        self.chat_display.ensureCursorVisible()

        # 压缩历史 user 消息，防止 502 溢出
        for msg in self.chat_messages:
            if msg.get("role") == "user":
                c = msg.get("content", "")
                if "【用户提问】:" in c:
                    parts = c.split("【用户提问】:")
                    msg["content"] = f"【历史提问】: {parts[-1].strip()}"

        if len(self.chat_messages) > 7:
            self.chat_messages = [self.chat_messages[0]] + self.chat_messages[-6:]

        if context_snapshot:
            combined = (
                f"【最新抓包环境与用户指定接口详情/完整响应体快照】:\n{context_snapshot}\n\n"
                f"---\n"
                f"【用户提问】: {user_text}"
            )
        else:
            combined = f"【用户提问】: {user_text}"

        self.chat_messages.append({"role": "user", "content": combined})

        # 渲染 AI 气泡头部
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

        cfg = self.config_provider()
        analyzer = AIAnalyzer(
            base_url=cfg.get("ai_base_url"),
            api_key=cfg.get("ai_api_key"),
            model=cfg.get("ai_model"),
        )

        self.ai_chat_worker = AIChatWorker(analyzer, list(self.chat_messages))
        self.ai_chat_worker.sig_chunk.connect(self._on_ai_chunk)
        self.ai_chat_worker.sig_finished.connect(self._on_ai_finished)
        self.ai_chat_worker.sig_error.connect(self._on_ai_error)
        self.ai_chat_worker.start()

    def _on_ai_chunk(self, chunk: str):
        self._current_ai_raw_chunks.append(chunk)
        cursor = self.chat_display.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(chunk)
        self.chat_display.setTextCursor(cursor)
        self.chat_display.ensureCursorVisible()

    def _on_ai_finished(self, full_reply: str):
        raw_text = "".join(self._current_ai_raw_chunks) if self._current_ai_raw_chunks else full_reply
        self.chat_messages.append({"role": "assistant", "content": raw_text})

        rendered_html = markdown_to_qt_html(raw_text)

        cursor = self.chat_display.textCursor()
        if self._current_ai_start_pos is not None:
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

        # 自动提取生成的 Python 代码到运行器
        try:
            py_blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", raw_text, re.DOTALL)
            if py_blocks:
                latest_code = py_blocks[-1].strip()
                self.code_runner.set_code(latest_code)
        except Exception:
            pass

    def _on_ai_error(self, err: str):
        tip_html = ""
        err_lower = err.lower()
        if "502" in err or "bad gateway" in err_lower:
            tip_html = """
            <div style="margin-top: 6px; font-size: 11.5px; color: #b91c1c; background-color: #fee2e2; padding: 6px; border-radius: 4px; line-height: 1.5;">
                💡 <b>502 Bad Gateway 原因分析与排查建议</b>：<br/>
                1. <b>远端大模型后端已崩溃或未启动</b>：经测试，服务器上的大模型后端进程（llama.cpp / vLLM）当前未响应，反向代理网关无法连接到后端进程。<br/>
                2. <b>请登录服务器检查并重启模型服务</b>（例如通过 SSH 检查 supervisorctl 或重新启动 llama.cpp 命令）；<br/>
                3. 您也可点击右上角 <b>「⚙️ 设置」</b> 切换为其它可用的 API 服务（如 DeepSeek、Ollama 本地服务等）。
            </div>
            """
        elif "exceed" in err_lower and "context" in err_lower:
            tip_html = """
            <div style="margin-top: 6px; font-size: 11.5px; color: #b91c1c; background-color: #fee2e2; padding: 6px; border-radius: 4px;">
                💡 <b>原因与优化建议</b>：勾选的接口数量过多或历史对话太长（已超出模型 Context 限制）。<br/>
                建议：取消全选，仅勾选 <b>1~3 个核心业务接口</b>，或点击顶部 <b>「🧹 清空对话」</b> 重置历史记忆。
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

    def _extract_code_from_chat(self, _=""):
        code_found = ""
        for msg in reversed(self.chat_messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", content, re.DOTALL)
                if blocks:
                    code_found = blocks[-1].strip()
                    break

        if code_found:
            self.code_runner.set_code(code_found)
        else:
            QMessageBox.information(self, "提示", "当前会话历史中未找到任何 Python 代码块！")

    def test_model(self):
        self.btn_test_model.setEnabled(False)
        self.chat_display.append("""
        <table width="100%" border="0" cellpadding="6" cellspacing="0" bgcolor="#f8fafc" style="border: 1px solid #e2e8f0; border-radius: 4px; margin: 6px 0;">
        <tr><td><span style="color: #64748b; font-size: 12px;">🧪 正在测试与大模型服务的连通性...</span></td></tr>
        </table>
        """)
        self.chat_display.ensureCursorVisible()

        cfg = self.config_provider()
        analyzer = AIAnalyzer(
            base_url=cfg.get("ai_base_url"),
            api_key=cfg.get("ai_api_key"),
            model=cfg.get("ai_model"),
        )

        test_worker = AIChatWorker(analyzer, [
            {"role": "system", "content": "请简明回答。"},
            {"role": "user", "content": "测试连接：请回复「模型连接正常」及当前模型名称。"}
        ])

        def _on_test_ok(reply):
            self.btn_test_model.setEnabled(True)
            self.chat_display.append(f"""
            <table width="100%" border="0" cellpadding="8" cellspacing="0" bgcolor="#f0fdf4" style="border: 1px solid #bbf7d0; border-radius: 6px; margin: 6px 0;">
            <tr><td>
                <b style="color: #16a34a; font-size: 13px;">✅ 模型连通性测试成功</b>:
                <div style="margin-top: 4px; font-size: 12px; color: #166534;">{html.escape(reply)}</div>
            </td></tr>
            </table>
            """)
            self.chat_display.ensureCursorVisible()

        def _on_test_fail(err):
            self.btn_test_model.setEnabled(True)
            self._on_ai_error(f"模型连通测试失败: {err}")

        test_worker.sig_finished.connect(_on_test_ok)
        test_worker.sig_error.connect(_on_test_fail)
        test_worker.start()
