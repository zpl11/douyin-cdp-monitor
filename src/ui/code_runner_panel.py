import html
import re
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..workers import CodeRunnerWorker


class CodeRunnerPanel(QWidget):
    """交互式 Python 代码调试终端面板"""
    sig_feedback_to_ai = Signal(str)  # 将执行结果反馈给 AI 对话框
    sig_code_extracted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.code_worker: Optional[CodeRunnerWorker] = None
        self._init_ui()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(4)

        runner_group = QGroupBox("⚡ 交互式代码运行器 (Code Runner)")
        runner_layout = QVBoxLayout(runner_group)
        runner_layout.setContentsMargins(8, 8, 8, 8)
        runner_layout.setSpacing(6)

        # 运行器控制条 (精简紧凑)
        runner_ctrl_bar = QHBoxLayout()
        runner_ctrl_bar.setSpacing(4)

        self.btn_extract_code = QPushButton("📥 提取代码")
        self.btn_extract_code.setCursor(Qt.PointingHandCursor)
        self.btn_extract_code.setStyleSheet("background: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 4px; padding: 3px 6px; font-size: 11px;")
        self.btn_extract_code.clicked.connect(self._on_extract_clicked)
        runner_ctrl_bar.addWidget(self.btn_extract_code)

        self.btn_run_code = QPushButton("▶️ 运行")
        self.btn_run_code.setCursor(Qt.PointingHandCursor)
        self.btn_run_code.setStyleSheet("background: #16a34a; color: white; font-weight: bold; border-radius: 4px; padding: 3px 10px; font-size: 11px;")
        self.btn_run_code.clicked.connect(self.run_code)
        runner_ctrl_bar.addWidget(self.btn_run_code)

        self.btn_stop_code = QPushButton("⏹️ 终止")
        self.btn_stop_code.setEnabled(False)
        self.btn_stop_code.setCursor(Qt.PointingHandCursor)
        self.btn_stop_code.setStyleSheet("background: #dc2626; color: white; font-weight: bold; border-radius: 4px; padding: 3px 6px; font-size: 11px;")
        self.btn_stop_code.clicked.connect(self.stop_code)
        runner_ctrl_bar.addWidget(self.btn_stop_code)

        self.btn_clear_console = QPushButton("🧹 清空")
        self.btn_clear_console.setCursor(Qt.PointingHandCursor)
        self.btn_clear_console.setStyleSheet("padding: 3px 6px; font-size: 11px;")
        self.btn_clear_console.clicked.connect(self.clear_console)
        runner_ctrl_bar.addWidget(self.btn_clear_console)

        self.btn_feedback_result = QPushButton("💬 发送结果给 AI")
        self.btn_feedback_result.setCursor(Qt.PointingHandCursor)
        self.btn_feedback_result.setStyleSheet("background: #0284c7; color: white; font-weight: bold; border-radius: 4px; padding: 3px 8px; font-size: 11px;")
        self.btn_feedback_result.clicked.connect(self._on_feedback_clicked)
        runner_ctrl_bar.addWidget(self.btn_feedback_result)

        runner_ctrl_bar.addStretch()

        self.lbl_runner_status = QLabel("就绪")
        self.lbl_runner_status.setStyleSheet("color: #64748b; font-size: 11px;")
        self.lbl_runner_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        runner_ctrl_bar.addWidget(self.lbl_runner_status)

        runner_layout.addLayout(runner_ctrl_bar)

        # 选项卡：代码编辑与运行输出
        self.runner_tabs = QTabWidget()

        # Tab 1: 代码编辑框
        self.code_edit = QTextEdit()
        self.code_edit.setFont(QFont("Consolas", 10))
        self.code_edit.setStyleSheet("""
            QTextEdit {
                background-color: #f8fafc;
                border: 1px solid #cbd5e1;
                border-radius: 4px;
                color: #0f172a;
                padding: 6px;
                font-family: 'Consolas', 'Courier New', monospace;
            }
        """)
        self.code_edit.setPlaceholderText("# 在此编辑或查看 Python 代码，AI 生成的代码块会自动提取至此处...\n# 点击上方「▶️ 运行」可直接执行并查看返回结果")
        self.runner_tabs.addTab(self.code_edit, "💻 Python 待执行脚本")

        # Tab 2: 控制台输出
        self.code_console = QTextEdit()
        self.code_console.setReadOnly(True)
        self.code_console.setFont(QFont("Consolas", 10))
        self.code_console.setStyleSheet("""
            QTextEdit {
                background-color: #0f172a;
                border: 1px solid #1e293b;
                border-radius: 4px;
                color: #22c55e;
                padding: 6px;
                font-family: 'Consolas', 'Courier New', monospace;
            }
        """)
        self.code_console.setPlaceholderText("终端就绪... 点击「▶️ 运行」后在此实时输出 stdout 和 stderr 信息。")
        self.runner_tabs.addTab(self.code_console, "📟 终端执行结果 (Console)")

        runner_layout.addWidget(self.runner_tabs)
        main_layout.addWidget(runner_group)

    def set_code(self, code_str: str):
        self.code_edit.setText(code_str.strip())
        line_cnt = len(code_str.strip().splitlines())
        self.lbl_runner_status.setText(f"💡 已装填脚本 ({line_cnt} 行)")
        self.runner_tabs.setCurrentIndex(0)

    def get_code(self) -> str:
        return self.code_edit.toPlainText().strip()

    def run_code(self):
        code_str = self.get_code()
        if not code_str:
            QMessageBox.information(self, "提示", "待执行的 Python 代码为空！请先编写或提取代码。")
            return

        self.btn_run_code.setEnabled(False)
        self.btn_stop_code.setEnabled(True)
        self.lbl_runner_status.setText("⏳ 正在执行 Python 脚本...")

        self.runner_tabs.setCurrentIndex(1)
        self.code_console.append(
            f"<div style='color: #38bdf8; font-weight: bold; margin: 4px 0;'>"
            f"🚀 [开始执行 Python 脚本] ...</div>"
        )

        self.code_worker = CodeRunnerWorker(code_str)
        self.code_worker.sig_stdout.connect(self._on_code_stdout)
        self.code_worker.sig_stderr.connect(self._on_code_stderr)
        self.code_worker.sig_finished.connect(self._on_code_finished)
        self.code_worker.start()

    def _on_code_stdout(self, line: str):
        cursor = self.code_console.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(line)
        self.code_console.setTextCursor(cursor)
        self.code_console.ensureCursorVisible()

    def _on_code_stderr(self, line: str):
        cursor = self.code_console.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        escaped = html.escape(line).replace("\n", "<br/>")
        cursor.insertHtml(f"<span style='color: #f87171;'>{escaped}</span>")
        self.code_console.setTextCursor(cursor)
        self.code_console.ensureCursorVisible()

    def _on_code_finished(self, returncode: int, elapsed: float):
        self.btn_run_code.setEnabled(True)
        self.btn_stop_code.setEnabled(False)

        cursor = self.code_console.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)

        if returncode == 0:
            status_html = f"<div style='color: #4ade80; font-weight: bold; margin: 6px 0;'>✅ 执行成功 (耗时: {elapsed} 秒, 退出码: 0)</div>"
            self.lbl_runner_status.setText(f"✅ 执行成功 ({elapsed}s)")
        else:
            status_html = f"<div style='color: #ef4444; font-weight: bold; margin: 6px 0;'>❌ 执行异常退出 (耗时: {elapsed} 秒, 退出码: {returncode})</div>"
            self.lbl_runner_status.setText(f"❌ 异常退出 (码: {returncode})")

        cursor.insertHtml(status_html)
        self.code_console.setTextCursor(cursor)
        self.code_console.ensureCursorVisible()

    def stop_code(self):
        if self.code_worker and self.code_worker.isRunning():
            self.code_worker.stop()
            self.lbl_runner_status.setText("⏹️ 已请求终止执行")

    def clear_console(self):
        self.code_console.clear()
        self.lbl_runner_status.setText("终端输出已清空")

    def _on_extract_clicked(self):
        self.sig_code_extracted.emit("")

    def _on_feedback_clicked(self):
        code_str = self.get_code()
        console_output = self.code_console.toPlainText().strip()

        if not console_output and not code_str:
            QMessageBox.information(self, "提示", "当前控制台无执行输出！请先点击「▶️ 运行」执行代码。")
            return

        feedback = "我刚才在本地运行了逆向爬虫脚本，执行返回结果如下：\n\n"
        if code_str:
            show_code = code_str if len(code_str) <= 3000 else code_str[:3000] + "\n# ... (代码过长已精简)"
            feedback += f"【运行的 Python 代码】:\n```python\n{show_code}\n```\n\n"

        show_out = console_output if len(console_output) <= 3000 else console_output[-3000:] + "\n... (输出较长已保留末尾关键信息)"
        feedback += f"【终端输出信息 (Console Output)】:\n```\n{show_out}\n```\n\n"
        feedback += (
            "----------------------------------------\n"
            "请帮我根据上述执行结果进行分析：\n"
            "1. 接口是否成功响应？核心业务数据是否成功提取出来？\n"
            "2. 如果遇到报错、重定向、403 风控或 JSONDecodeError，请具体分析原因，并给出修复后的完整代码。"
        )

        self.sig_feedback_to_ai.emit(feedback)
