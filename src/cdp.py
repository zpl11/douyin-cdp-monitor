"""CDP WebSocket 客户端 — 支持 Chrome DevTools Protocol 页面监控与数据抓取"""
import asyncio
import json
import logging
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
import websockets

from .models import BrowserTab

logger = logging.getLogger(__name__)


def normalize_cdp_url(url: str) -> str:
    """标准化 CDP 基础地址，避免 localhost 解析与代理异常"""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    if host.lower() in ("localhost", "0.0.0.0"):
        host = "127.0.0.1"
    port = parsed.port or 9222
    path = parsed.path if parsed.path and parsed.path != "/" else "/json"
    return f"http://{host}:{port}{path}"


def normalize_ws_url(ws_url: str) -> str:
    """标准化 WebSocket 地址中的主机名为 127.0.0.1"""
    if "localhost:" in ws_url:
        return ws_url.replace("localhost:", "127.0.0.1:")
    return ws_url


async def discover_targets(cdp_url: str = "http://127.0.0.1:9222/json") -> list[BrowserTab]:
    """发现 Chrome 中打开的所有页面标签（绕过系统代理）"""
    target_url = normalize_cdp_url(cdp_url)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.get(target_url)
            if resp.status_code == 200:
                raw = resp.json()
                tabs: list[BrowserTab] = []
                for item in raw:
                    t_type = item.get("type", "page")
                    if t_type == "page":
                        ws = normalize_ws_url(item.get("webSocketDebuggerUrl", ""))
                        tabs.append(BrowserTab(
                            title=item.get("title", "未命名页面"),
                            url=item.get("url", ""),
                            target_id=item.get("id", ""),
                            type=t_type,
                            ws_url=ws,
                        ))
                return tabs
            else:
                logger.error(f"发现页面失败，HTTP 状态码: {resp.status_code}")
    except Exception as e:
        logger.error(f"CDP 目标发现异常: {e}")
    return []


class CDPClient:
    """CDP WebSocket 客户端，负责与 Chrome DevTools 通信"""

    def __init__(self):
        self.active_tab: Optional[BrowserTab] = None
        self.connected = False
        self._ws = None
        self._next_id = 0
        self._pending_futures: dict[int, asyncio.Future] = {}
        self._listen_task: Optional[asyncio.Task] = None
        self._event_callback: Optional[Callable[[str, dict], None]] = None

    async def connect(self, ws_url: str, tab: BrowserTab, event_callback: Callable[[str, dict], None]) -> bool:
        """连接到指定标签页的 DevTools WebSocket"""
        await self.disconnect()

        ws_url = normalize_ws_url(ws_url)
        self.active_tab = tab
        self._event_callback = event_callback
        self._next_id = 0

        try:
            self._ws = await websockets.connect(
                ws_url,
                proxy=None,
                max_size=50 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            )
            self.connected = True

            # 启动后台监听循环
            self._listen_task = asyncio.create_task(self._listen_loop())

            # 启用 CDP 域
            await self.send_command("Network.enable", {
                "maxTotalBufferSize": 100 * 1024 * 1024,
                "maxResourceBufferSize": 20 * 1024 * 1024,
                "maxPostDataSize": 5 * 1024 * 1024,
            })
            await self.send_command("Page.enable")
            await self.send_command("Runtime.enable")

            logger.info(f"CDP 成功连接至: {tab.title} ({ws_url})")
            return True
        except Exception as e:
            logger.error(f"CDP 连接失败: {e}")
            await self.disconnect()
            return False

    async def send_command(self, method: str, params: Optional[dict] = None, timeout: float = 10.0) -> dict:
        """发送 CDP 协议命令并等待其响应"""
        if not self._ws or not self.connected:
            raise ConnectionError("CDP WebSocket 未连接")

        self._next_id += 1
        cmd_id = self._next_id
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_futures[cmd_id] = future

        msg = {"id": cmd_id, "method": method}
        if params is not None:
            msg["params"] = params

        await self._ws.send(json.dumps(msg))

        try:
            res = await asyncio.wait_for(future, timeout=timeout)
            return res
        finally:
            self._pending_futures.pop(cmd_id, None)

    async def get_response_body(self, request_id: str) -> tuple[str, bool]:
        """获取指定请求的响应体 (body, base64Encoded)"""
        try:
            res = await self.send_command("Network.getResponseBody", {"requestId": request_id}, timeout=8.0)
            result = res.get("result", {})
            return result.get("body", ""), result.get("base64Encoded", False)
        except Exception as e:
            logger.debug(f"获取响应体失败 (requestId={request_id}): {e}")
            return "", False

    async def _listen_loop(self):
        """持续接收 DevTools 消息并分派"""
        try:
            async for raw_message in self._ws:
                try:
                    data = json.loads(raw_message)
                except Exception:
                    continue

                # 处理命令响应
                msg_id = data.get("id")
                if msg_id is not None and msg_id in self._pending_futures:
                    fut = self._pending_futures[msg_id]
                    if not fut.done():
                        if "error" in data:
                            fut.set_exception(RuntimeError(data["error"].get("message", "CDP Error")))
                        else:
                            fut.set_result(data)
                    continue

                # 处理事件分发
                method = data.get("method")
                params = data.get("params", {})
                if method and self._event_callback:
                    try:
                        self._event_callback(method, params)
                    except Exception as e:
                        logger.error(f"事件回调处理异常 ({method}): {e}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.info(f"CDP 监听循环结束: {e}")
        finally:
            self.connected = False

    async def disconnect(self):
        """断开 CDP 连接并清理资源"""
        self.connected = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except Exception:
                pass
            self._listen_task = None

        for fut in list(self._pending_futures.values()):
            if not fut.done():
                fut.cancel()
        self._pending_futures.clear()

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        self.active_tab = None
        self._event_callback = None
