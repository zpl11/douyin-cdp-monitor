"""AI 分析模块 - 针对网络流量逆向与数据抓取优化的多轮对话副驾驶 (Copilot)"""
import asyncio
import html
import json
import logging
import os
import re
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import openai

from .models import NetworkRequest

logger = logging.getLogger(__name__)

# 常见埋点与无用监控域名字段
TELEMETRY_KEYWORDS = (
    "mon.zijie", "mcs.zijie", "/log/", "/collect", "/telemetry",
    "/report", "/beacon", "analytics", "/stat", "/event", "track",
    "apm", "frontier", "speed", "metrics", "log-upload"
)


def is_telemetry_request(url: str) -> bool:
    """快速判断是否为埋点、日志、上报或监控类无用请求"""
    lower = url.lower()
    return any(k in lower for k in TELEMETRY_KEYWORDS)


STATIC_EXTENSIONS = (
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".ico", ".map", ".mp4", ".m3u8", ".ts"
)

USEFUL_API_PATTERNS = (
    "/api/", "/v1/", "/v2/", "/v3/", "aweme", "feed", "user",
    "comment", "detail", "search", "item", "recommend", "publish",
    "video", "web/api", "profile", "data", "list", "info", "query",
    "hot", "suggest", "relation", "music", "author", "play", "post"
)


def is_useful_request(req: NetworkRequest) -> bool:
    """判断是否为有用的核心业务数据接口 (剔除埋点、监控、日志与静态资源)"""
    url_lower = req.url.lower()

    # 1. 明确的埋点、上报、日志与打点直接排除
    if is_telemetry_request(url_lower):
        return False

    # 2. 静态资源直接排除
    path = urlparse(url_lower).path
    if any(path.endswith(ext) for ext in STATIC_EXTENSIONS):
        return False

    # 2.1 WebSocket 长连接直接认定为核心数据通道 (如直播间、弹幕、私信)
    if getattr(req, "is_websocket", False) or req.resource_type.lower() == "websocket" or req.url.startswith(("ws://", "wss://")):
        return True

    # 3. 资源类型必须属于 XHR/Fetch/Document，或者响应是 JSON
    is_xhr_type = req.is_xhr or req.resource_type.lower() in ("xhr", "fetch")
    is_json = "json" in req.mime_type.lower()

    # 4. URL 含有常见业务 API 路径关键词，或有明确的 query 参数/响应体
    has_api_pattern = any(k in url_lower for k in USEFUL_API_PATTERNS)

    if (is_xhr_type or is_json) and (has_api_pattern or "?" in req.url or req.post_data):
        return True

    return False


def classify_request_category(url: str) -> str:
    """对接口进行业务分类，辅助检索过滤"""
    u = url.lower()
    if any(k in u for k in ("aweme/v1/feed", "/feed", "aweme/v1/web/aweme", "item", "video", "detail")):
        return "video"  # 视频与作品
    elif any(k in u for k in ("user", "profile", "author", "creator")):
        return "user"  # 用户与作者
    elif any(k in u for k in ("comment", "reply")):
        return "comment"  # 评论与互动
    elif any(k in u for k in ("search", "hot", "suggest", "trend", "billboard")):
        return "search"  # 搜索与热榜
    return "other_api"


PROMPT_SYSTEM_COPILOT = """你是一名专业的前端反爬与网络接口逆向副驾驶助手（AI Reverse-Engineering Copilot）。
用户正在通过 CDP Monitor 工具实时监控浏览器的网络流量（抓取抖音、小红书等现代 Web 应用）。
你熟知前端 API 交互机制、反爬防刷机制（例如 a_bogus, X-Bogus, msToken, verifyFp, _signature 等）、常见数据流结构（如 Feed 流、评论列表、用户信息、无水印播放源等）以及 Python 爬虫编写。

在每次对话中，系统会向你提供最新的【抓包上下文信息】：
1. 当前已捕获的请求概览；
2. 用户在界面上【当前单击选中的目标接口】及其请求头、Query 参数、POST 载荷以及返回值 Response Body 内容。

你的交流原则：
- 像一位身经百战的逆向搭档一样与用户进行持续、多轮的交谈。
- 当用户说“这个接口”、“当前请求”、“该返回值”等代词时，指的就是上下文中的【当前选中的请求】。
- 给出清晰、实用、可落地的回答，包括字段解析、参数精简建议、代码示例（requests/httpx）或逆向思路。
- 条理分明，善于使用 Markdown 列表、加粗和代码块。
"""


def prune_json_for_context(
    json_str: str,
    max_chars: int = 35000,
    max_depth: int = 10,
    max_list_items: int = 2,
    max_dict_keys: int = 80,
    raw_if_within_budget: bool = True
) -> str:
    """
    智能自适应精简大型 JSON 响应体：
    1. 若原始长度在 max_chars 内且允许 raw_if_within_budget，直接返回 100% 完整原始 JSON，不隐藏任何字段
    2. 若超出预算，只对同构数组进行折叠（保留前 max_list_items 个真实样本），其余元素折叠说明
    3. 保留深度提升至 10 层，绝不隐藏深层对象（如 member.uname, content.message 等真实业务文本）
    4. 只有当字符串超过 1500 字符（如 base64、超长文本）时才截断，普通评论或视频标题 100% 完整保留
    """
    if not json_str:
        return ""
    if raw_if_within_budget and len(json_str) <= max_chars:
        return json_str

    try:
        data = json.loads(json_str)

        def _prune(obj, depth=0):
            if depth > max_depth:
                return "..."
            if isinstance(obj, str):
                if len(obj) > 1500:
                    return obj[:800] + f"... (超长字符串已折叠，原长 {len(obj)} 字符)"
                return obj
            if isinstance(obj, list):
                if len(obj) <= max_list_items:
                    return [_prune(x, depth + 1) for x in obj]
                pruned_list = [_prune(x, depth + 1) for x in obj[:max_list_items]]
                pruned_list.append(f"... (其余 {len(obj) - max_list_items} 项结构相同，已折叠节省 Token)")
                return pruned_list
            elif isinstance(obj, dict):
                new_dict = {}
                keys = list(obj.keys())
                for k in keys[:max_dict_keys]:
                    new_dict[k] = _prune(obj[k], depth + 1)
                if len(keys) > max_dict_keys:
                    new_dict["_more_keys"] = f"... (其余 {len(keys) - max_dict_keys} 个键已折叠)"
                return new_dict
            return obj

        pruned = _prune(data)
        res = json.dumps(pruned, ensure_ascii=False, indent=2)
        if len(res) > max_chars:
            return res[:max_chars] + f"\n... (已截断至字符预算上限 {max_chars}，原大小: {len(json_str)} 字符)"
        return res
    except Exception:
        return json_str[:max_chars] + (f"\n... (非标准 JSON 已截断，原长: {len(json_str)} 字符)" if len(json_str) > max_chars else "")


class AIAnalyzer:
    """支持多轮持续对话的 AI 逆向副驾驶"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or "sk-placeholder"
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        self.client = openai.OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=300.0,
            max_retries=0
        )

    async def test_connection(self) -> str:
        """测试模型连通性"""
        try:
            resp = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=self.model,
                messages=[
                    {"role": "system", "content": "请简短回复：模型连接正常。"},
                    {"role": "user", "content": "测试连接"}
                ],
                max_tokens=60,
                temperature=0.2
            )
            choice = resp.choices[0].message
            content = choice.content or getattr(choice, "reasoning_content", "") or "连接成功（返回空内容）"
            return content.strip()
        except Exception as e:
            logger.error(f"AI 连接测试失败: {e}")
            raise RuntimeError(f"AI 服务连接失败: {e}")

    def chat_stream(self, messages: list[dict], on_chunk: Callable[[str], None]) -> str:
        """多轮流式对话，支持思维链推理实时展示与完整内容回传"""
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=4096,
                temperature=0.3,
                stream=True
            )

            full_reply = []
            is_reasoning_phase = False

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                content = getattr(delta, "content", None)

                if reasoning:
                    if not is_reasoning_phase:
                        on_chunk("> 🧠 **思考过程**：\n> ")
                        is_reasoning_phase = True
                    on_chunk(reasoning.replace("\n", "\n> "))
                elif content:
                    if is_reasoning_phase:
                        on_chunk("\n\n---\n\n")
                        is_reasoning_phase = False
                    on_chunk(content)
                    full_reply.append(content)

            return "".join(full_reply)
        except Exception as e:
            logger.error(f"AI 对话生成异常: {e}")
            on_chunk(f"\n\n❌ **回答中断**: {e}")
            raise e

    @staticmethod
    def build_system_message() -> dict:
        """构造初始 System Prompt"""
        return {"role": "system", "content": PROMPT_SYSTEM_COPILOT}

    @staticmethod
    def build_context_snapshot(
        requests: list[NetworkRequest],
        selected_req: Optional[NetworkRequest] = None,
        target_reqs: Optional[list[NetworkRequest]] = None,
        max_flow_items: int = 20,
        max_detailed_targets: int = 12,
        total_body_budget_chars: int = 25000,
    ) -> str:
        """构建当前抓包状态与用户勾选/选中请求的实时上下文快照，自动进行智能 Token 预算分配"""
        sections = []

        targets = target_reqs if target_reqs else ([selected_req] if selected_req else [])

        # 1. 用户勾选/指定的重点分析接口详情（置顶，最重要）
        if targets:
            total_targets_cnt = len(targets)
            detailed_targets = targets[:max_detailed_targets]
            remaining_targets = targets[max_detailed_targets:]

            header_title = f"【📌 用户勾选指定的重点分析接口详情与响应体 (共勾选 {total_targets_cnt} 个接口)】:"
            sections.append(header_title)

            # 根据勾选的接口数量自适应分级：
            # <= 3 个核心接口时：深度剖析模式，提供高达 50,000 字符预算，只要大小适中直接输出 100% 完整 Raw Body！
            # 4 ~ 8 个接口时：中等预算，单接口 15,000 字符
            # > 8 个接口时：紧凑预算，防超限
            if total_targets_cnt <= 3:
                per_req_budget = 50000
                max_query_keys = 50
                max_post_chars = 4000
                allow_raw = True
                prune_depth = 10
                prune_items = 2
            elif total_targets_cnt <= 8:
                per_req_budget = 15000
                max_query_keys = 25
                max_post_chars = 1500
                allow_raw = True
                prune_depth = 8
                prune_items = 2
            else:
                per_req_budget = max(1000, min(4000, total_body_budget_chars // len(detailed_targets)))
                max_query_keys = 15
                max_post_chars = 800
                allow_raw = False
                prune_depth = 5
                prune_items = 1

            for idx, r in enumerate(detailed_targets, 1):
                parsed = urlparse(r.url)
                query_dict = parse_qs(parsed.query)

                req_lines = [
                    f"### 接口 #{idx}: [{r.method}] `{r.url}`",
                    f"- 状态码: {r.response_status} | 资源类型: {r.resource_type} | 响应大小: {r.formatted_size}",
                    f"- MIME: {r.mime_type}",
                ]

                if query_dict:
                    req_lines.append("- Query 参数:")
                    for k, v in list(query_dict.items())[:max_query_keys]:
                        val = ", ".join(v)
                        is_sign = any(s in k.lower() for s in ("bogus", "token", "sign", "verify", "_sig", "sec", "w_rid", "wts"))
                        flag = " 🔒[关键签名/时间戳/Token]" if is_sign else ""
                        req_lines.append(f"  * `{k}`: `{val}`{flag}")
                    if len(query_dict) > max_query_keys:
                        req_lines.append(f"  * ... (其余 {len(query_dict) - max_query_keys} 个次要参数已折叠)")

                if r.post_data:
                    post_show = r.post_data if len(r.post_data) <= max_post_chars else r.post_data[:max_post_chars] + f"\n... (POST 载荷已折叠至上限，原长: {len(r.post_data)} 字符)"
                    req_lines.append(f"- POST 载荷:\n```\n{post_show}\n```")

                if getattr(r, "is_websocket", False) or getattr(r, "ws_frames", None):
                    frames = getattr(r, "ws_frames", [])
                    send_cnt = sum(1 for f in frames if f.direction == "send")
                    recv_cnt = sum(1 for f in frames if f.direction == "recv")
                    req_lines.append(f"- 📡 WebSocket 双向消息流 (累计 {len(frames)} 帧: 发送 {send_cnt} 帧, 接收 {recv_cnt} 帧):")
                    # 取最近的最多 12 条关键帧时序
                    recent_frames = frames[-12:] if len(frames) > 12 else frames
                    for f in recent_frames:
                        arrow = "➡️ [客户端发送 (SEND)]" if f.direction == "send" else "⬅️ [服务端推送 (RECV)]"
                        p_str = f.payload_data
                        if len(p_str) > 1000:
                            p_str = p_str[:1000] + f"\n... (消息 Payload 较长已折叠，原长: {len(f.payload_data)} 字符)"
                        req_lines.append(f"  * #{f.frame_id} `{f.time_str}` {arrow} {f.opcode_str} ({f.size} B):\n```\n{p_str}\n```")

                if r.response_body:
                    is_raw_full = allow_raw and (len(r.response_body) <= per_req_budget)
                    if is_raw_full:
                        body_content = r.response_body
                        body_tag = f"返回值 Response Body (🔥 100% 完整原始数据，未做任何折叠，共 {len(r.response_body)} 字符):"
                    else:
                        body_content = prune_json_for_context(
                            r.response_body,
                            max_chars=per_req_budget,
                            max_depth=prune_depth,
                            max_list_items=prune_items,
                            raw_if_within_budget=allow_raw
                        )
                        body_tag = f"返回值 Response Body (⚡ 智能结构样本，保留深层对象与真实字段，折叠多余同构数组项以节省 Token):"
                    req_lines.append(f"- {body_tag}\n```json\n{body_content}\n```")
                elif not getattr(r, "is_websocket", False):
                    req_lines.append("- 返回值 Response Body: (该接口暂未读取到响应体或为空)")

                sections.append("\n".join(req_lines))

            # 如果勾选数量多（如 75 个），对超出上限的接口进行精炼清单归纳，防止撑爆上下文
            if remaining_targets:
                rem_lines = [
                    f"\n#### 其余勾选接口简明清单 (共 {len(remaining_targets)} 个已折叠，已提取核心路径与状态，防超 Token 上限):"
                ]
                for r_idx, r in enumerate(remaining_targets, max_detailed_targets + 1):
                    p = urlparse(r.url)
                    rem_lines.append(f"  {r_idx}. [{r.method}] `{p.path}` ({r.response_status or 'Pending'}) -> `{r.url[:90]}`")
                sections.append("\n".join(rem_lines))
        else:
            sections.append("【📌 当前重点目标接口】: 暂未在表格中勾选或选择任何特定接口。")

        # 2. 当前整体流量快照（精炼展示，剔除埋点）
        if requests:
            target_ids = {r.request_id for r in targets}
            xhr_fetch = [r for r in requests if r.is_xhr or r.resource_type.lower() in ("xhr", "fetch")]
            flow_lines = [
                f"\n【📊 捕获网络流量概览】 (总数: {len(requests)}, XHR/Fetch: {len(xhr_fetch)}):"
            ]
            target_list = xhr_fetch if xhr_fetch else requests
            clean_list = [r for r in target_list if not is_telemetry_request(r.url)]
            display_list = clean_list if clean_list else target_list

            for i, r in enumerate(display_list[:max_flow_items], 1):
                p = urlparse(r.url)
                status_str = f"Status: {r.response_status}" if r.response_status else "Pending"
                is_cur = " 👈[已勾选]" if r.request_id in target_ids else ""
                flow_lines.append(f"  {i}. [{r.method}] `{p.path}` ({status_str}) -> `{r.url[:100]}`{is_cur}")

            if len(display_list) > max_flow_items:
                flow_lines.append(f"  *(其余 {len(display_list) - max_flow_items} 条次要请求已自动折叠)*")

            sections.append("\n".join(flow_lines))

        return "\n\n".join(sections)


def markdown_to_qt_html(md_text: str) -> str:
    """
    将大模型的 Markdown 格式文本深度解析并转换为适合 Qt QTextEdit / QTextBrowser 显示的现代高级卡片式富文本 HTML。
    解决原生回显中 ### 标题、``` 代码块、行内符号未渲染的问题。
    """
    if not md_text or not md_text.strip():
        return ""

    text = md_text.replace("\r\n", "\n").replace("\r", "\n")

    # 如果存在未闭合的 ``` 代码块（如中断），自动补全以便正确闭合解析
    if text.count("```") % 2 == 1:
        text += "\n```"

    # 1. 提取并保护围栏代码块 (```lang ... ```)
    code_blocks = []
    def _save_code_block(match):
        lang = match.group(1).strip() if match.group(1) else ""
        code_content = match.group(2)
        idx = len(code_blocks)
        code_blocks.append((lang, code_content))
        return f"__CODE_BLOCK_TOKEN_{idx}__"

    pattern_code_block = re.compile(r"```([a-zA-Z0-9_\-\+]*)\n([\s\S]*?)```")
    text = pattern_code_block.sub(_save_code_block, text)

    # 2. 提取并保护行内代码 (`code`)
    inline_codes = []
    def _save_inline_code(match):
        c = match.group(1)
        idx = len(inline_codes)
        inline_codes.append(c)
        return f"__INLINE_CODE_TOKEN_{idx}__"

    pattern_inline_code = re.compile(r"`([^`\n]+?)`")
    text = pattern_inline_code.sub(_save_inline_code, text)

    # 3. 提取并处理 Markdown 表格
    def _convert_table(match):
        table_text = match.group(0).strip()
        raw_lines = [line.strip() for line in table_text.split("\n") if line.strip()]
        if len(raw_lines) < 2:
            return table_text

        header_cells = [c.strip() for c in raw_lines[0].strip("|").split("|")]
        start_row = 1
        if len(raw_lines) > 1 and re.match(r"^\|?[\s\-:]+(\|[\s\-:]+)+\|?$", raw_lines[1]):
            start_row = 2

        out = ['<table width="100%" border="0" cellpadding="6" cellspacing="1" bgcolor="#cbd5e1" style="margin: 8px 0; font-size: 12px; border-radius: 4px;">']
        out.append('<tr bgcolor="#f1f5f9">')
        for h in header_cells:
            esc_h = html.escape(h)
            out.append(f'<th align="left" style="color: #0f172a; font-weight: bold; padding: 6px 10px;">{esc_h}</th>')
        out.append('</tr>')

        for r_idx in range(start_row, len(raw_lines)):
            row_line = raw_lines[r_idx]
            cells = [c.strip() for c in row_line.strip("|").split("|")]
            bg = "#ffffff" if r_idx % 2 == 0 else "#f8fafc"
            out.append(f'<tr bgcolor="{bg}">')
            for c in cells:
                esc_c = html.escape(c)
                out.append(f'<td style="color: #334155; padding: 6px 10px;">{esc_c}</td>')
            out.append('</tr>')
        out.append('</table>')
        return "\n" + "".join(out) + "\n"

    pattern_table = re.compile(r"((?:\|[^\n]+\|\n?){2,})")
    text = pattern_table.sub(_convert_table, text)

    # 4. 逐行解析排版（标题、分割线、列表、引用块、段落）
    lines = text.split("\n")
    processed_lines = []
    in_list = False
    list_type = None  # 'ul' or 'ol'
    in_quote = False
    quote_lines = []

    def _flush_quote():
        nonlocal in_quote, quote_lines
        if in_quote and quote_lines:
            q_html = "<br/>".join(quote_lines)
            is_thought = "思考过程" in q_html or "思维链" in q_html
            accent_color = "#94a3b8" if is_thought else "#3b82f6"
            bg_color = "#f8fafc" if is_thought else "#eff6ff"
            border_style = f"border-left: 3px solid {accent_color};"
            title_tag = '<div style="font-size: 11px; font-weight: bold; color: #64748b; margin-bottom: 4px;">🧠 思考与推理过程:</div>' if is_thought else ''
            processed_lines.append(
                f'<table width="100%" border="0" cellpadding="6" cellspacing="0" bgcolor="{bg_color}" style="{border_style} margin: 6px 0; border-radius: 4px;">'
                f'<tr><td style="color: #475569; font-size: 12px; line-height: 1.45;">{title_tag}{q_html}</td></tr>'
                f'</table>'
            )
            quote_lines = []
            in_quote = False

    for line in lines:
        stripped = line.strip()

        # 表格标签行直接保留
        if stripped.startswith('<table width="100%" border="0" cellpadding="6" cellspacing="1"'):
            _flush_quote()
            if in_list:
                processed_lines.append(f"</{list_type}>")
                in_list = False
            processed_lines.append(stripped)
            continue

        # 分割线 --- 或 ***
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            _flush_quote()
            if in_list:
                processed_lines.append(f"</{list_type}>")
                in_list = False
            processed_lines.append('<div style="margin: 10px 0; border-bottom: 1px dashed #cbd5e1;"></div>')
            continue

        # 引用块 >
        quote_match = re.match(r"^>\s?(.*)$", stripped)
        if quote_match:
            if in_list:
                processed_lines.append(f"</{list_type}>")
                in_list = False
            in_quote = True
            q_content = html.escape(quote_match.group(1).strip())
            q_content = q_content.replace("&gt;", ">").replace("&lt;", "<")
            quote_lines.append(q_content)
            continue
        else:
            if in_quote:
                _flush_quote()

        # 标题处理 (#, ##, ###, ####)
        h_match = re.match(r"^(#{1,5})\s+(.*)$", stripped)
        if h_match:
            if in_list:
                processed_lines.append(f"</{list_type}>")
                in_list = False
            level = len(h_match.group(1))
            h_text = html.escape(h_match.group(2).strip())

            if level == 1:
                processed_lines.append(
                    f'<div style="font-size: 15px; font-weight: bold; color: #1e3a8a; margin-top: 12px; margin-bottom: 6px; padding-bottom: 4px; border-bottom: 2px solid #3b82f6;">'
                    f'📌 {h_text}</div>'
                )
            elif level == 2:
                processed_lines.append(
                    f'<div style="font-size: 14px; font-weight: bold; color: #1d4ed8; margin-top: 10px; margin-bottom: 5px; padding-bottom: 2px; border-bottom: 1px solid #bfdbfe;">'
                    f'🔹 {h_text}</div>'
                )
            elif level == 3:
                processed_lines.append(
                    f'<div style="font-size: 13px; font-weight: bold; color: #2563eb; margin-top: 8px; margin-bottom: 4px;">'
                    f'▫️ {h_text}</div>'
                )
            else:
                processed_lines.append(
                    f'<div style="font-size: 12.5px; font-weight: bold; color: #334155; margin-top: 6px; margin-bottom: 3px;">'
                    f'• {h_text}</div>'
                )
            continue

        # 无序列表 - 或 * 或 +
        ul_match = re.match(r"^[\*\-\+]\s+(.*)$", stripped)
        if ul_match:
            if not in_list or list_type != 'ul':
                if in_list:
                    processed_lines.append(f"</{list_type}>")
                processed_lines.append('<ul style="margin: 4px 0 6px 16px; padding: 0;">')
                in_list = True
                list_type = 'ul'
            item_text = html.escape(ul_match.group(1).strip())
            indent_style = "margin-left: 14px;" if line.startswith("  ") else ""
            processed_lines.append(f'<li style="color: #1e293b; margin: 3px 0; font-size: 12px; line-height: 1.5; {indent_style}">{item_text}</li>')
            continue

        # 有序列表 1. 2.
        ol_match = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if ol_match:
            if not in_list or list_type != 'ol':
                if in_list:
                    processed_lines.append(f"</{list_type}>")
                processed_lines.append('<ol style="margin: 4px 0 6px 16px; padding: 0;">')
                in_list = True
                list_type = 'ol'
            item_text = html.escape(ol_match.group(2).strip())
            indent_style = "margin-left: 14px;" if line.startswith("  ") else ""
            processed_lines.append(f'<li style="color: #1e293b; margin: 3px 0; font-size: 12px; line-height: 1.5; {indent_style}">{item_text}</li>')
            continue

        # 普通段落或空行
        if in_list:
            processed_lines.append(f"</{list_type}>")
            in_list = False

        if not stripped:
            processed_lines.append('<div style="height: 4px;"></div>')
        else:
            esc_line = html.escape(stripped)
            processed_lines.append(f'<div style="color: #1e293b; line-height: 1.55; font-size: 12.5px; margin: 3px 0;">{esc_line}</div>')

    _flush_quote()
    if in_list:
        processed_lines.append(f"</{list_type}>")

    html_body = "\n".join(processed_lines)

    # 5. 行内格式化：超链接、加粗、斜体
    html_body = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2" style="color: #2563eb; text-decoration: underline;">\1</a>',
        html_body
    )
    html_body = re.sub(r"\*\*(.+?)\*\*", r'<b style="color: #0f172a; font-weight: bold;">\1</b>', html_body)
    html_body = re.sub(r"(?<!\*)\*([^\*\n]+?)\*(?!\*)", r'<i>\1</i>', html_body)

    # 6. 还原行内代码 (`code` -> 灰底高亮徽章)
    for idx, raw_code in enumerate(inline_codes):
        escaped_inline = html.escape(raw_code)
        badge = (
            f'<code style="background-color: #f1f5f9; color: #0284c7; padding: 1px 5px; '
            f'border-radius: 4px; font-family: Consolas, monospace; font-size: 11.5px; '
            f'border: 1px solid #cbd5e1; font-weight: 600;">{escaped_inline}</code>'
        )
        html_body = html_body.replace(f"__INLINE_CODE_TOKEN_{idx}__", badge)

    # 7. 还原代码块 (```lang ... ``` -> 现代化深色终端容器)
    for idx, (lang, raw_code) in enumerate(code_blocks):
        escaped_code = html.escape(raw_code.rstrip())
        lang_str = lang.upper() if lang else "CODE"
        code_card = f"""
        <table width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#0f172a" style="border: 1px solid #1e293b; border-radius: 6px; margin: 8px 0;">
            <tr>
                <td bgcolor="#1e293b" style="padding: 5px 10px;">
                    <span style="color: #38bdf8; font-size: 11px; font-weight: bold; font-family: Consolas, monospace;">💻 {lang_str}</span>
                </td>
            </tr>
            <tr>
                <td style="padding: 10px 12px;">
                    <pre style="color: #f8fafc; font-family: Consolas, 'Courier New', monospace; font-size: 11.5px; line-height: 1.45; margin: 0; white-space: pre-wrap;">{escaped_code}</pre>
                </td>
            </tr>
        </table>
        """
        html_body = html_body.replace(f"__CODE_BLOCK_TOKEN_{idx}__", code_card)

    return html_body

