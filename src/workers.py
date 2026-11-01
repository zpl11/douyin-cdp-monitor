import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .cdp import CDPClient, discover_targets
from .models import BrowserTab, NetworkRequest, WebSocketFrame
from .ai_analyzer import AIAnalyzer

logger = logging.getLogger(__name__)


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
    sig_ws_frame_added = Signal(str, object)
    sig_ws_closed = Signal(str)

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
            req_id = params.get("requestId", "")
            req_data = params.get("request", {})
            record = NetworkRequest(
                request_id=req_id,
                timestamp=datetime.now(),
                method=req_data.get("method", "GET"),
                url=req_data.get("url", ""),
                headers=req_data.get("headers", {}),
                post_data=req_data.get("postData"),
                resource_type=params.get("type", "Other"),
                tab_title=self.client.active_tab.title if self.client.active_tab else "",
                tab_url=self.client.active_tab.url if self.client.active_tab else "",
            )
            self.requests_map[req_id] = record
            self.sig_request_added.emit(record)

        elif method == "Network.responseReceived":
            req_id = params.get("requestId", "")
            resp_data = params.get("response", {})
            status = resp_data.get("status", 0)
            status_text = resp_data.get("statusText", "")
            headers = resp_data.get("headers", {})
            mime_type = resp_data.get("mimeType", "")
            encoded_len = resp_data.get("encodedDataLength", 0)

            if req_id in self.requests_map:
                r = self.requests_map[req_id]
                r.response_status = status
                r.response_status_text = status_text
                r.response_headers = headers
                r.mime_type = mime_type
                if encoded_len:
                    r.response_size = encoded_len

            self.sig_response_received.emit(req_id, status, status_text, headers, mime_type)

        elif method == "Network.loadingFinished":
            req_id = params.get("requestId", "")
            size = params.get("encodedDataLength", 0)
            if req_id in self.requests_map:
                r = self.requests_map[req_id]
                r.response_size = size
                r.is_success = True
            self.sig_loading_finished.emit(req_id, size)

        elif method == "Network.loadingFailed":
            req_id = params.get("requestId", "")
            err = params.get("errorText", "")
            if req_id in self.requests_map:
                self.requests_map[req_id].is_success = False
                self.requests_map[req_id].error = err
            self.sig_loading_failed.emit(req_id, err)

        # ── WebSocket 专属协议事件 ──────────────────────────────────────────────
        elif method == "Network.webSocketCreated":
            req_id = params.get("requestId", "")
            url = params.get("url", "")
            record = NetworkRequest(
                request_id=req_id,
                timestamp=datetime.now(),
                method="WS",
                url=url,
                resource_type="WebSocket",
                is_websocket=True,
                response_status=101,
                response_status_text="Connecting",
                tab_title=self.client.active_tab.title if self.client.active_tab else "",
                tab_url=self.client.active_tab.url if self.client.active_tab else "",
            )
            self.requests_map[req_id] = record
            self.sig_request_added.emit(record)

        elif method == "Network.webSocketWillSendHandshakeRequest":
            req_id = params.get("requestId", "")
            req_data = params.get("request", {})
            headers = req_data.get("headers", {})
            if req_id in self.requests_map:
                self.requests_map[req_id].headers.update(headers)

        elif method == "Network.webSocketHandshakeResponseReceived":
            req_id = params.get("requestId", "")
            resp_data = params.get("response", {})
            status = resp_data.get("status", 101)
            status_text = resp_data.get("statusText", "Switching Protocols")
            headers = resp_data.get("headers", {})
            if req_id in self.requests_map:
                r = self.requests_map[req_id]
                r.response_status = status
                r.response_status_text = status_text
                r.response_headers.update(headers)
            self.sig_response_received.emit(req_id, status, status_text, headers, "websocket")

        elif method == "Network.webSocketFrameSent":
            req_id = params.get("requestId", "")
            frame_resp = params.get("response", {})
            payload = frame_resp.get("payloadData", "")
            opcode = frame_resp.get("opcode", 1)
            if req_id in self.requests_map:
                r = self.requests_map[req_id]
                size = len(payload.encode("utf-8", errors="ignore"))
                frame = WebSocketFrame(
                    frame_id=len(r.ws_frames) + 1,
                    timestamp=datetime.now(),
                    direction="send",
                    payload_data=payload,
                    opcode=opcode,
                    size=size
                )
                r.ws_frames.append(frame)
                r.response_size += size
                self.sig_ws_frame_added.emit(req_id, frame)

        elif method == "Network.webSocketFrameReceived":
            req_id = params.get("requestId", "")
            frame_resp = params.get("response", {})
            payload = frame_resp.get("payloadData", "")
            opcode = frame_resp.get("opcode", 1)
            if req_id in self.requests_map:
                r = self.requests_map[req_id]
                size = len(payload.encode("utf-8", errors="ignore"))
                frame = WebSocketFrame(
                    frame_id=len(r.ws_frames) + 1,
                    timestamp=datetime.now(),
                    direction="recv",
                    payload_data=payload,
                    opcode=opcode,
                    size=size
                )
                r.ws_frames.append(frame)
                r.response_size += size
                self.sig_ws_frame_added.emit(req_id, frame)

        elif method == "Network.webSocketClosed":
            req_id = params.get("requestId", "")
            if req_id in self.requests_map:
                r = self.requests_map[req_id]
                r.response_status_text = "Closed"
            self.sig_ws_closed.emit(req_id)


class AIChatWorker(QThread):
    """大模型流式对话后台工作线程"""
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


class CodeRunnerWorker(QThread):
    """独立的 Python 代码执行工作线程，使用当前 Python 解释器与虚拟环境异步运行"""
    sig_stdout = Signal(str)
    sig_stderr = Signal(str)
    sig_finished = Signal(int, float)  # returncode, elapsed_seconds

    def __init__(self, code_str: str):
        super().__init__()
        self.code_str = code_str
        self.proc: Optional[subprocess.Popen] = None
        self._killed = False

    def run(self):
        t0 = time.time()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-u", "-c", self.code_str],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )

            def _reader(pipe, is_err):
                for line in iter(pipe.readline, ""):
                    if self._killed:
                        break
                    if is_err:
                        self.sig_stderr.emit(line)
                    else:
                        self.sig_stdout.emit(line)
                pipe.close()

            th_out = threading.Thread(target=_reader, args=(self.proc.stdout, False), daemon=True)
            th_err = threading.Thread(target=_reader, args=(self.proc.stderr, True), daemon=True)
            th_out.start()
            th_err.start()

            self.proc.wait()
            th_out.join(timeout=1.0)
            th_err.join(timeout=1.0)

            elapsed = round(time.time() - t0, 2)
            ret = self.proc.returncode if self.proc else -1
            self.sig_finished.emit(ret, elapsed)

        except Exception as e:
            elapsed = round(time.time() - t0, 2)
            self.sig_stderr.emit(f"\n[执行异常]: {e}\n")
            self.sig_finished.emit(-1, elapsed)

    def stop(self):
        self._killed = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
