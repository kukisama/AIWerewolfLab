"""游戏引擎 —— 协调 Agent Framework 驱动的狼人杀游戏流程

为了最大兼容性和稳定性，本引擎通过 Azure Foundry REST API 直接调用 LLM，
并利用 Agent Framework 的核心设计理念：
  - 多 Agent 各自独立人设
  - Tool/Function Calling
  - 中间件日志 (Middleware tracing)
  - Human-in-the-loop (暂停等待前端输入)
  - SSE 流式推送
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import time
from typing import Optional, Callable, Awaitable

import aiohttp
from dotenv import load_dotenv

from game.state import GameState, GameMessage, Phase, Role, Camp, ROLE_NAMES_CN
from agents.prompts import (
    build_werewolf_instructions, build_seer_instructions,
    build_witch_instructions, build_guard_instructions,
    build_hunter_instructions, build_villager_instructions,
    build_vote_instructions, build_hunter_shoot_instructions,
)
from middleware.game_log import log_agent_action, log_llm_call, reset_traces
from config import load_config

load_dotenv()


def _get_speak_delay() -> float:
    cfg = load_config()
    return cfg.get("speak_delay_ms", 300) / 1000

# Azure 鉴权 —— 使用 Azure CLI credential
_access_token: str | None = None
_token_expiry: float = 0


async def _get_azure_token() -> str:
    """通过 az CLI 获取 token (缓存到过期)"""
    global _access_token, _token_expiry
    if _access_token and time.time() < _token_expiry - 60:
        return _access_token

    proc = await asyncio.create_subprocess_exec(
        "az", "account", "get-access-token",
        "--resource", "https://cognitiveservices.azure.com",
        "--query", "accessToken", "-o", "tsv",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"az login 失败: {stderr.decode().strip()}")
    _access_token = stdout.decode().strip()
    _token_expiry = time.time() + 3000  # ~50 分钟
    return _access_token


# ─── Tool 定义 (OpenAI function calling 格式) ─────────
TOOL_SCHEMAS = {
    "kill_player": {
        "type": "function",
        "function": {
            "name": "kill_player",
            "description": "狼人选择击杀目标。target_seat 是目标玩家的座位号，参见存活玩家列表中的编号",
            "parameters": {
                "type": "object",
                "properties": {"target_seat": {"type": "integer", "description": "目标玩家座位号（参见存活玩家列表中 X号 对应的数字）"}},
                "required": ["target_seat"],
            },
        },
    },
    "check_identity": {
        "type": "function",
        "function": {
            "name": "check_identity",
            "description": "预言家查验一名玩家的身份。target_seat 是目标的座位号",
            "parameters": {
                "type": "object",
                "properties": {"target_seat": {"type": "integer", "description": "目标玩家座位号（参见存活玩家列表中 X号 对应的数字）"}},
                "required": ["target_seat"],
            },
        },
    },
    "use_antidote": {
        "type": "function",
        "function": {
            "name": "use_antidote",
            "description": "女巫使用解药救活今晚被杀的人",
            "parameters": {
                "type": "object",
                "properties": {"target_seat": {"type": "integer", "description": "要救的玩家座位号"}},
                "required": ["target_seat"],
            },
        },
    },
    "use_poison": {
        "type": "function",
        "function": {
            "name": "use_poison",
            "description": "女巫使用毒药毒杀一名玩家",
            "parameters": {
                "type": "object",
                "properties": {"target_seat": {"type": "integer", "description": "目标玩家座位号（参见存活玩家列表）"}},
                "required": ["target_seat"],
            },
        },
    },
    "protect_player": {
        "type": "function",
        "function": {
            "name": "protect_player",
            "description": "守卫保护一名玩家不被狼人杀。seat 参见存活玩家列表",
            "parameters": {
                "type": "object",
                "properties": {"target_seat": {"type": "integer", "description": "目标玩家座位号（参见存活玩家列表）"}},
                "required": ["target_seat"],
            },
        },
    },
    "shoot_player": {
        "type": "function",
        "function": {
            "name": "shoot_player",
            "description": "猎人开枪带走一名存活玩家",
            "parameters": {
                "type": "object",
                "properties": {"target_seat": {"type": "integer", "description": "目标玩家座位号（参见存活玩家列表）"}},
                "required": ["target_seat"],
            },
        },
    },
    "vote": {
        "type": "function",
        "function": {
            "name": "vote",
            "description": "投票处决一名存活玩家。座位号参见存活玩家列表",
            "parameters": {
                "type": "object",
                "properties": {
                    "voter_seat": {"type": "integer", "description": "投票者自己的座位号"},
                    "target_seat": {"type": "integer", "description": "要投票处决的目标座位号（必须是存活玩家列表中的号码）"},
                },
                "required": ["voter_seat", "target_seat"],
            },
        },
    },
    "skip_action": {
        "type": "function",
        "function": {
            "name": "skip_action",
            "description": "跳过当前操作，不做任何事",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "get_alive_players": {
        "type": "function",
        "function": {
            "name": "get_alive_players",
            "description": "查看当前存活的玩家列表",
            "parameters": {"type": "object", "properties": {}},
        },
    },
}


async def call_llm(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict] | None = None,
    agent_name: str = "unknown",
    max_tokens_override: int | None = None,
) -> dict:
    """LLM 调用统一入口（支持 Responses API / Azure Chat Completions / OpenAI 兼容）

    始终返回 Chat Completions 格式的 message dict，方便下游统一处理。
    每个玩家可绑定模型池中不同模型（含不同 api_type）。
    max_tokens_override 可覆盖配置中的 max_tokens（如总结场景需要更长输出）。
    """
    cfg = load_config()
    if max_tokens_override is not None:
        cfg = {**cfg, "max_tokens": max_tokens_override}

    from config import get_pool_item
    pool_item = get_pool_item(agent_name)
    api_type = pool_item.get("api_type") or cfg.get("api_type", "responses")

    if api_type == "responses":
        return await _call_responses_api(system_prompt, user_prompt, tools, agent_name, cfg)
    else:
        return await _call_chat_completions_api(system_prompt, user_prompt, tools, agent_name, cfg)


async def call_llm_stream(
    system_prompt: str,
    user_prompt: str,
    agent_name: str = "unknown",
    max_tokens_override: int | None = None,
):
    """流式 LLM 调用，yield 文本 chunk（仅文本，不支持 tool calling）"""
    cfg = load_config()
    if max_tokens_override is not None:
        cfg = {**cfg, "max_tokens": max_tokens_override}

    from config import get_pool_item
    pool_item = get_pool_item(agent_name)
    api_type = pool_item.get("api_type") or cfg.get("api_type", "responses")

    if api_type == "responses":
        async for chunk in _stream_responses_api(system_prompt, user_prompt, agent_name, cfg):
            yield chunk
    else:
        async for chunk in _stream_chat_completions_api(system_prompt, user_prompt, agent_name, cfg):
            yield chunk


async def _stream_responses_api(system_prompt, user_prompt, agent_name, cfg):
    """Responses API 流式"""
    from config import get_pool_item
    pool_item = get_pool_item(agent_name)
    endpoint = pool_item.get("endpoint", "").strip().rstrip("/")
    model = pool_item.get("model", "").strip()
    api_key = pool_item.get("api_key", "").strip()
    api_ver = pool_item.get("api_version") or cfg.get("api_version", "2025-03-01-preview")

    if endpoint.endswith("/openai"):
        url = f"{endpoint}/responses?api-version={api_ver}"
    else:
        url = f"{endpoint}/openai/responses?api-version={api_ver}"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {await _get_azure_token()}"

    body = {
        "model": model,
        "instructions": system_prompt,
        "input": user_prompt,
        "temperature": cfg.get("temperature", 0.8),
        "max_output_tokens": cfg.get("max_tokens", 300),
        "stream": True,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"LLM API 错误 ({resp.status}): {text[:300]}")
            buffer = ""
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8", errors="replace")
                buffer += line
                while "\n" in buffer:
                    one_line, buffer = buffer.split("\n", 1)
                    one_line = one_line.strip()
                    if not one_line or not one_line.startswith("data:"):
                        continue
                    payload = one_line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    # Responses API 流: type=response.output_text.delta → delta
                    if evt.get("type") == "response.output_text.delta":
                        delta = evt.get("delta", "")
                        if delta:
                            yield delta


async def _stream_chat_completions_api(system_prompt, user_prompt, agent_name, cfg):
    """Chat Completions API 流式"""
    from config import get_pool_item
    pool_item = get_pool_item(agent_name)
    endpoint = pool_item.get("endpoint", "").strip().rstrip("/")
    model = pool_item.get("model", "").strip()
    api_key = pool_item.get("api_key", "").strip()
    api_type = pool_item.get("api_type") or cfg.get("api_type", "azure")
    api_ver = pool_item.get("api_version") or cfg.get("api_version", "2024-12-01-preview")

    if api_type == "openai":
        url = f"{endpoint}/chat/completions"
    else:
        if endpoint.endswith("/openai"):
            url = f"{endpoint}/deployments/{model}/chat/completions?api-version={api_ver}"
        else:
            url = f"{endpoint}/openai/deployments/{model}/chat/completions?api-version={api_ver}"

    headers = {"Content-Type": "application/json"}
    if api_type == "openai":
        headers["Authorization"] = f"Bearer {api_key}"
    elif api_key:
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {await _get_azure_token()}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    body = {
        "messages": messages,
        "temperature": cfg.get("temperature", 0.8),
        "max_completion_tokens": cfg.get("max_tokens", 300),
        "stream": True,
    }
    if api_type == "openai":
        body["model"] = model

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=headers) as resp:
            if resp.status == 400:
                text = await resp.text()
                if "max_completion_tokens" in text:
                    body.pop("max_completion_tokens", None)
                    body["max_tokens"] = cfg.get("max_tokens", 300)
                    async with session.post(url, json=body, headers=headers) as resp2:
                        if resp2.status != 200:
                            text2 = await resp2.text()
                            raise RuntimeError(f"LLM API 错误 ({resp2.status}): {text2[:300]}")
                        async for chunk in _parse_chat_stream(resp2):
                            yield chunk
                        return
                else:
                    raise RuntimeError(f"LLM API 错误 ({resp.status}): {text[:300]}")
            elif resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"LLM API 错误 ({resp.status}): {text[:300]}")
            async for chunk in _parse_chat_stream(resp):
                yield chunk


async def _parse_chat_stream(resp):
    """解析 Chat Completions SSE 流"""
    buffer = ""
    async for raw_line in resp.content:
        line = raw_line.decode("utf-8", errors="replace")
        buffer += line
        while "\n" in buffer:
            one_line, buffer = buffer.split("\n", 1)
            one_line = one_line.strip()
            if not one_line or not one_line.startswith("data:"):
                continue
            payload = one_line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield content


async def _call_responses_api(
    system_prompt: str, user_prompt: str,
    tools: list[dict] | None,
    agent_name: str, cfg: dict,
) -> dict:
    """OpenAI Responses API (/responses)"""
    from config import get_pool_item
    pool_item = get_pool_item(agent_name)
    endpoint = pool_item.get("endpoint", "").strip()
    model = pool_item.get("model", "").strip()
    api_key = pool_item.get("api_key", "").strip()
    api_ver = pool_item.get("api_version") or cfg.get("api_version", "2025-03-01-preview")

    endpoint = endpoint.rstrip("/")
    if not endpoint:
        raise RuntimeError("未配置 AI 端点")
    # 拼 URL: endpoint 可能已含 /openai，也可能不含
    if endpoint.endswith("/openai"):
        url = f"{endpoint}/responses?api-version={api_ver}"
    else:
        url = f"{endpoint}/openai/responses?api-version={api_ver}"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {await _get_azure_token()}"

    temperature = cfg.get("temperature", 0.8)
    max_tokens = cfg.get("max_tokens", 300)

    body: dict = {
        "model": model,
        "instructions": system_prompt,
        "input": user_prompt,
        "temperature": temperature,
        "max_output_tokens": max_tokens,
    }

    # Responses API 的 tools 格式：扁平化，不需要外层 function wrapper
    if tools:
        resp_tools = []
        for t in tools:
            func = t.get("function", t)
            resp_tools.append({
                "type": "function",
                "name": func["name"],
                "description": func.get("description", ""),
                "parameters": func.get("parameters", {}),
            })
        body["tools"] = resp_tools

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"LLM API 错误 ({resp.status}): {text[:300]}")
            data = await resp.json()

    tokens_used = data.get("usage", {}).get("total_tokens", 0)

    # 将 Responses API 的 output 转换为统一的 message 格式
    message = _responses_output_to_message(data.get("output", []))

    content_preview = message.get("content", "") or ""
    log_llm_call(agent_name, user_prompt[:200], content_preview[:200], tokens_used)
    return message


def _responses_output_to_message(output: list[dict]) -> dict:
    """将 Responses API 的 output 数组转为 Chat Completions 兼容的 message dict"""
    content_parts = []
    tool_calls = []

    for item in output:
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    content_parts.append(c.get("text", ""))
        elif item.get("type") == "function_call":
            tool_calls.append({
                "id": item.get("call_id", ""),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                },
            })

    msg: dict = {"role": "assistant", "content": "\n".join(content_parts) if content_parts else None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


async def _call_chat_completions_api(
    system_prompt: str, user_prompt: str,
    tools: list[dict] | None,
    agent_name: str, cfg: dict,
) -> dict:
    """Chat Completions API (Azure 原生 / OpenAI 兼容)"""
    from config import get_pool_item
    pool_item = get_pool_item(agent_name)
    endpoint = pool_item.get("endpoint", "").strip()
    model = pool_item.get("model", "").strip()
    api_key = pool_item.get("api_key", "").strip()
    api_type = pool_item.get("api_type") or cfg.get("api_type", "azure")
    api_ver = pool_item.get("api_version") or cfg.get("api_version", "2024-12-01-preview")

    endpoint = endpoint.rstrip("/")
    if not endpoint:
        raise RuntimeError("未配置 AI 端点")

    if api_type == "openai":
        url = f"{endpoint}/chat/completions"
    else:
        if endpoint.endswith("/openai"):
            url = f"{endpoint}/deployments/{model}/chat/completions?api-version={api_ver}"
        else:
            url = f"{endpoint}/openai/deployments/{model}/chat/completions?api-version={api_ver}"

    headers = {"Content-Type": "application/json"}
    if api_type == "openai":
        headers["Authorization"] = f"Bearer {api_key}"
    elif api_key:
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {await _get_azure_token()}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    temperature = cfg.get("temperature", 0.8)
    max_tokens = cfg.get("max_tokens", 300)
    body: dict = {"messages": messages, "temperature": temperature, "max_completion_tokens": max_tokens}
    if api_type == "openai":
        body["model"] = model
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=headers) as resp:
            if resp.status == 400:
                text = await resp.text()
                if "max_completion_tokens" in text:
                    body.pop("max_completion_tokens", None)
                    body["max_tokens"] = max_tokens
                    async with session.post(url, json=body, headers=headers) as resp2:
                        if resp2.status != 200:
                            text2 = await resp2.text()
                            raise RuntimeError(f"LLM API 错误 ({resp2.status}): {text2[:300]}")
                        data = await resp2.json()
                else:
                    raise RuntimeError(f"LLM API 错误 ({resp.status}): {text[:300]}")
            elif resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"LLM API 错误 ({resp.status}): {text[:300]}")
            else:
                data = await resp.json()

    choice = data["choices"][0]
    message = choice["message"]
    tokens_used = data.get("usage", {}).get("total_tokens", 0)

    content_preview = message.get("content", "") or ""
    log_llm_call(agent_name, user_prompt[:200], content_preview[:200], tokens_used)
    return message


def extract_tool_call(message: dict) -> tuple[str | None, dict | None]:
    """从 LLM 响应中提取 tool call"""
    tool_calls = message.get("tool_calls")
    if not tool_calls:
        return None, None
    tc = tool_calls[0]
    name = tc["function"]["name"]
    try:
        args = json.loads(tc["function"]["arguments"])
    except (json.JSONDecodeError, KeyError):
        args = {}
    return name, args


class GameEngine:
    """协调游戏流程的核心引擎"""

    def __init__(self):
        self.state = GameState()
        self.event_queue: asyncio.Queue[dict] = asyncio.Queue()
        self.human_input_event = asyncio.Event()
        self.human_input_data: dict = {}
        self.seer_results: list[tuple[int, str]] = []  # 预言家历史查验
        self.day_speeches: list[tuple[str, str]] = []   # 本轮白天发言
        self._running = False
        # tool 实例
        self._tools: dict = {}

    async def emit(self, event_type: str, data: dict):
        """推送事件到前端 SSE 队列"""
        await self.event_queue.put({"event": event_type, "data": data})

    async def emit_system(self, content: str):
        msg = GameMessage(sender="system", content=content, msg_type="system")
        self.state.messages.append(msg)
        await self.emit("message", {"sender": "system", "content": content, "type": "system"})

    async def emit_speech(self, name: str, content: str):
        msg = GameMessage(sender=name, content=content, msg_type="chat")
        self.state.messages.append(msg)
        await self.emit("message", {"sender": name, "content": content, "type": "chat"})

    async def emit_action(self, content: str, visibility: str = "public"):
        msg = GameMessage(sender="system", content=content, msg_type="action", visibility=visibility)
        self.state.messages.append(msg)
        await self.emit("message", {"sender": "system", "content": content, "type": "action", "visibility": visibility})

    async def request_human_input(self, prompt: str, input_type: str, options: list[dict] | None = None):
        """请求人类玩家输入 (Human-in-the-loop)"""
        self.human_input_event.clear()
        await self.emit("human_input_request", {
            "prompt": prompt,
            "input_type": input_type,  # "select_player", "speak", "vote"
            "options": options or [],
        })
        # 等待前端提交
        await self.human_input_event.wait()
        return self.human_input_data

    def submit_human_input(self, data: dict):
        """前端调用此方法提交人类输入"""
        self.human_input_data = data
        self.human_input_event.set()

    def _get_tool_func(self, name: str):
        """获取 tool 执行函数"""
        from tools.game_actions import make_tools
        if not self._tools:
            self._tools = make_tools(self.state)
        return self._tools.get(name)

    async def _execute_tool(self, agent_name: str, tool_name: str, args: dict) -> str:
        """执行 tool 并记录日志"""
        func = self._get_tool_func(tool_name)
        if not func:
            return f"未知工具: {tool_name}"
        result = func(**args)
        log_agent_action(agent_name, "tool_call", f"{tool_name}({args}) -> {result}")
        return result

    # tool_name 到可见性的映射
    _TOOL_VISIBILITY = {
        "kill_player": "wolf_team",
        "check_identity": "seer",
        "use_antidote": "witch",
        "use_poison": "witch",
        "protect_player": "guard",
        "shoot_player": "public",   # 猎人开枪是公开的
        "vote": "public",
        "skip_action": "god",       # 跳过只在上帝视角可见
    }

    async def _agent_act(self, player, system_prompt: str, user_prompt: str, tools: list[str]) -> str:
        """让一个 Agent 行动（LLM 调用 + Tool 执行）"""
        tool_schemas = [TOOL_SCHEMAS[t] for t in tools if t in TOOL_SCHEMAS]
        try:
            message = await call_llm(system_prompt, user_prompt, tool_schemas, player.name)
        except Exception as e:
            err_msg = f"{player.name} 的 AI 调用失败: {str(e)[:120]}"
            await self.emit_system(f"⚠️ {err_msg}")
            log_agent_action(player.name, "error", err_msg)
            return ""

        tool_name, tool_args = extract_tool_call(message)
        if tool_name:
            result = await self._execute_tool(player.name, tool_name, tool_args or {})
            # 使用实际结果构建详细行动消息
            role_cn = ROLE_NAMES_CN.get(player.role, "未知")
            visibility = self._TOOL_VISIBILITY.get(tool_name, "god")
            detail_msg = f"[{role_cn}行动] {player.name} {result}"
            await self.emit_action(detail_msg, visibility=visibility)
            # 提取内心独白（LLM 在调用工具时附带的文本内容）
            monologue = (message.get("content") or "").strip()
            if monologue:
                await self.emit_action(
                    f"💭 {player.name}: {monologue[:50]}",
                    visibility="god",
                )
            return result

        # 没有 tool call 时，也给出反馈
        content = message.get("content", "")
        if content:
            role_cn = ROLE_NAMES_CN.get(player.role, "未知")
            await self.emit_action(f"[{role_cn}行动] {player.name} 思考完毕", visibility="god")
        return content

    # ═══════════════════════════════════════════════════
    # 历史上下文构建
    # ═══════════════════════════════════════════════════

    def _build_history_context(self, player=None) -> str:
        """从 state.messages 提取该玩家可见的历史记录。

        可见规则（基于 visibility 标签）：
        - public：所有人可见（系统公告、投票、猎人开枪等）
        - god：仅上帝视角（跳过动作等）—— 玩家不可见
        - wolf_team：狼人阵营可见
        - seer：预言家可见
        - witch：女巫可见
        - guard：守卫可见
        - hunter：猎人可见
        - system/chat 消息：全部可见
        """
        from game.state import ROLE_CAMP, Camp

        player_role = player.role.value if player else None
        player_camp = ROLE_CAMP[player.role].value if player else None

        lines = []
        for m in self.state.messages:
            if m.msg_type == "system":
                lines.append(f"[系统] {m.content}")
            elif m.msg_type == "chat":
                lines.append(f"{m.sender}: {m.content}")
            elif m.msg_type in ("action", "vote"):
                vis = getattr(m, 'visibility', 'public')
                if vis == "public":
                    lines.append(f"[行动] {m.content}")
                elif vis == "god":
                    # 上帝视角专属，玩家看不到
                    continue
                elif vis == "wolf_team":
                    if player_camp == "wolf":
                        lines.append(f"[行动] {m.content}")
                elif vis == player_role:
                    # 角色专属信息（seer/witch/guard/hunter）
                    lines.append(f"[行动] {m.content}")
                # 其他情况：该玩家看不到
        if not lines:
            return ""
        # 限制最近 120 条，避免 token 爆炸
        recent = lines[-120:]
        return "## 历史记录（你已知道的公开信息）\n" + "\n".join(recent)

    # ═══════════════════════════════════════════════════
    # 游戏主流程
    # ═══════════════════════════════════════════════════

    async def start(self, human_name: str = "玩家", observer: bool = False):
        """启动游戏"""
        reset_traces()
        self.state = GameState()
        self._tools = {}
        self.seer_results = []
        self.state.init_game(human_name, observer=observer)
        self._running = True

        if observer:
            await self.emit_system("游戏开始！旁观模式 —— 9 个 AI 玩家对战，你是上帝视角。")
            await self.emit_system("所有角色分配已展示，尽情观战！")
        else:
            human = [p for p in self.state.players if p.is_human][0]
            await self.emit_system(f"游戏开始！共 9 名玩家。")
            await self.emit_system(f"你是【{human.name}】，角色是【{ROLE_NAMES_CN[human.role]}】")
        await self.emit("state_update", self.state.to_dict())

        # 主循环
        try:
            while self._running:
                # 夜晚
                self.state.phase = Phase.NIGHT
                await self.emit("state_update", self.state.to_dict())
                await self._night_phase()

                # 结算夜晚
                announcements = self.state.resolve_night()
                self.state.phase = Phase.DAY
                await self.emit("state_update", self.state.to_dict())

                for ann in announcements:
                    await self.emit_system(ann)
                    await asyncio.sleep(1)

                # 检查猎人技能
                await self._check_hunter_death()

                # 检查胜负
                winner = self.state.check_winner()
                if winner:
                    await self._end_game(winner)
                    return

                self.state.save_checkpoint(f"第{self.state.round_number}轮_天亮")

                # 白天讨论
                await self._day_phase()

                # 投票
                self.state.phase = Phase.VOTE
                await self.emit("state_update", self.state.to_dict())
                await self._vote_phase()

                # 检查猎人技能
                await self._check_hunter_death()

                # 检查胜负
                winner = self.state.check_winner()
                if winner:
                    await self._end_game(winner)
                    return

                self.state.save_checkpoint(f"第{self.state.round_number}轮_投票后")
                self.state.round_number += 1
                self.state.votes = {}
                self.day_speeches = []
        except asyncio.CancelledError:
            pass

    async def _night_phase(self):
        """夜晚阶段"""
        await self.emit_system(f"═══ 第 {self.state.round_number} 个夜晚 ═══")
        await self.emit_system("🌙 天黑请闭眼...")
        await asyncio.sleep(1)

        # 1. 守卫行动
        await self._guard_action()
        await self.emit_system("守卫闭眼。")
        await asyncio.sleep(0.5)

        # 2. 狼人行动
        await self._wolf_action()
        await self.emit_system("狼人闭眼。")
        await asyncio.sleep(0.5)

        # 3. 预言家行动
        await self._seer_action()
        await self.emit_system("预言家闭眼。")
        await asyncio.sleep(0.5)

        # 4. 女巫行动
        await self._witch_action()
        await self.emit_system("女巫闭眼。天快亮了...")
        await asyncio.sleep(0.5)

    async def _guard_action(self):
        """守卫夜晚行动"""
        guard = None
        for p in self.state.players:
            if p.role == Role.GUARD and p.alive:
                guard = p
                break
        if not guard:
            await self.emit_system("守卫已出局，跳过守卫环节。")
            return

        await self.emit_system("守卫请睁眼，选择要保护的人...")

        if guard.is_human:
            options = [{"seat": p.seat, "name": p.name} for p in self.state.alive_players()]
            result = await self.request_human_input(
                "你是守卫，请选择要保护的玩家（或跳过）",
                "select_player", options
            )
            target = result.get("target_seat")
            if target:
                from tools.game_actions import make_tools
                tools = make_tools(self.state)
                r = tools["protect_player"](target)
                await self.emit_action(f"[守卫行动] {r}", visibility="guard")
        else:
            instructions = build_guard_instructions(guard, self.state)
            history = self._build_history_context(guard)
            user_prompt = f"{history}\n\n现在是夜晚，请选择保护对象。" if history else "现在是夜晚，请选择保护对象。"
            await self._agent_act(guard, instructions, user_prompt, ["protect_player", "skip_action", "get_alive_players"])

        await asyncio.sleep(_get_speak_delay())

    async def _wolf_action(self):
        """狼人夜晚行动"""
        wolves = self.state.alive_wolves()
        if not wolves:
            await self.emit_system("所有狼人已出局。")
            return

        await self.emit_system("狼人请睁眼，选择击杀目标...")

        human_wolf = None
        ai_wolves = []
        for w in wolves:
            if w.is_human:
                human_wolf = w
            else:
                ai_wolves.append(w)

        # AI 狼人先讨论 / 选目标
        if ai_wolves:
            wolf = ai_wolves[0]
            teammate = wolves[1] if len(wolves) > 1 else wolf
            instructions = build_werewolf_instructions(wolf, teammate, self.state)
            history = self._build_history_context(wolf)
            user_prompt = f"{history}\n\n现在是夜晚，请与队友协商并选择击杀目标。" if history else "现在是夜晚，请与队友协商并选择击杀目标。"
            await self._agent_act(wolf, instructions, user_prompt, ["kill_player", "get_alive_players"])

        if human_wolf:
            options = [
                {"seat": p.seat, "name": p.name}
                for p in self.state.alive_players() if p.seat != human_wolf.seat
            ]
            result = await self.request_human_input(
                "你是狼人，请选择今晚要击杀的目标",
                "select_player", options
            )
            target = result.get("target_seat")
            if target:
                from tools.game_actions import make_tools
                tools = make_tools(self.state)
                tools["kill_player"](target)

        await asyncio.sleep(_get_speak_delay())

    async def _seer_action(self):
        """预言家夜晚行动"""
        seer = None
        for p in self.state.players:
            if p.role == Role.SEER and p.alive:
                seer = p
                break
        if not seer:
            await self.emit_system("预言家已出局，跳过查验环节。")
            return

        await self.emit_system("预言家请睁眼，选择要查验的人...")

        if seer.is_human:
            options = [
                {"seat": p.seat, "name": p.name}
                for p in self.state.alive_players() if p.seat != seer.seat
            ]
            result = await self.request_human_input(
                "你是预言家，请选择要查验身份的玩家",
                "select_player", options
            )
            target = result.get("target_seat")
            if target:
                from tools.game_actions import make_tools
                tools = make_tools(self.state)
                r = tools["check_identity"](target)
                await self.emit_action(f"[查验结果] {r}", visibility="seer")
                self.seer_results.append((target, self.state.night_action.seer_result or ""))
        else:
            instructions = build_seer_instructions(seer, self.state, self.seer_results)
            history = self._build_history_context(seer)
            user_prompt = f"{history}\n\n现在是夜晚，请选择要查验的玩家。" if history else "现在是夜晚，请选择要查验的玩家。"
            result = await self._agent_act(seer, instructions, user_prompt, ["check_identity", "get_alive_players"])
            if self.state.night_action.seer_result:
                self.seer_results.append((
                    self.state.night_action.seer_target or 0,
                    self.state.night_action.seer_result,
                ))

        await asyncio.sleep(_get_speak_delay())

    async def _witch_action(self):
        """女巫夜晚行动"""
        witch = None
        for p in self.state.players:
            if p.role == Role.WITCH and p.alive:
                witch = p
                break
        if not witch:
            await self.emit_system("女巫已出局，跳过女巫环节。")
            return

        await self.emit_system("女巫请睁眼...")

        killed_name = None
        if self.state.night_action.wolf_target:
            killed_p = self.state.get_player(self.state.night_action.wolf_target)
            if killed_p:
                killed_name = killed_p.name

        if witch.is_human:
            options = []
            if killed_name and witch.has_antidote:
                options.append({"action": "save", "label": f"使用解药救 {killed_name}"})
            if witch.has_poison:
                for p in self.state.alive_players():
                    if p.seat != witch.seat:
                        options.append({"action": "poison", "label": f"毒杀 {p.name}", "seat": p.seat})
            options.append({"action": "skip", "label": "什么都不做"})

            prompt = f"你是女巫。"
            if killed_name:
                prompt += f" 今晚 {killed_name} 被杀了。"
            result = await self.request_human_input(prompt, "witch_action", options)
            action = result.get("action")
            if action == "save" and self.state.night_action.wolf_target:
                from tools.game_actions import make_tools
                tools = make_tools(self.state)
                tools["use_antidote"](self.state.night_action.wolf_target)
            elif action == "poison":
                target = result.get("target_seat")
                if target:
                    from tools.game_actions import make_tools
                    tools = make_tools(self.state)
                    tools["use_poison"](target)
        else:
            instructions = build_witch_instructions(witch, self.state, killed_name)
            history = self._build_history_context(witch)
            user_prompt = f"{history}\n\n现在是夜晚，请决定是否使用药水。" if history else "现在是夜晚，请决定是否使用药水。"
            await self._agent_act(
                witch, instructions,
                user_prompt,
                ["use_antidote", "use_poison", "skip_action", "get_alive_players"],
            )

        await asyncio.sleep(_get_speak_delay())

    async def _day_phase(self):
        """白天讨论阶段"""
        await self.emit_system(f"═══ 第 {self.state.round_number} 个白天 ═══")
        await self.emit_system("☀️ 天亮了，请各位发言。")
        await asyncio.sleep(0.5)

        self.day_speeches = []
        for p in self.state.alive_players():
            if not p.alive:
                continue

            await self.emit_system(f"请 {p.name} 发言——")

            if p.is_human:
                result = await self.request_human_input(
                    f"轮到你发言了（{ROLE_NAMES_CN[p.role]}）",
                    "speak", None
                )
                speech = result.get("content", "我没什么要说的。")
                self.day_speeches.append((p.name, speech))
                await self.emit_speech(p.name, speech)
            else:
                # 根据角色生成适当指令
                instructions = self._build_day_instructions(p)
                previous = "\n".join(f"{name}: {text}" for name, text in self.day_speeches)
                history = self._build_history_context(p)
                parts = []
                if history:
                    parts.append(history)
                if previous:
                    parts.append(f"本轮前面的发言:\n{previous}")
                parts.append("现在轮到你发言了。请结合历史信息做出有深度的分析和判断。")
                user_prompt = "\n\n".join(parts)

                try:
                    message = await call_llm(instructions, user_prompt, None, p.name)
                    speech = message.get("content", "我同意大家的意见。")
                except Exception as e:
                    speech = "我暂时没有什么要说的。"
                    await self.emit_system(f"⚠️ {p.name} 的 AI 发言失败: {str(e)[:80]}")
                speech = speech.strip()[:200]  # 限制长度
                self.day_speeches.append((p.name, speech))
                await self.emit_speech(p.name, speech)

            await asyncio.sleep(_get_speak_delay())

    def _build_day_instructions(self, player) -> str:
        """为白天发言构建指令"""
        if player.role == Role.WEREWOLF:
            wolves = self.state.alive_wolves()
            teammate = [w for w in wolves if w.seat != player.seat]
            tm = teammate[0] if teammate else player
            return build_werewolf_instructions(player, tm, self.state)
        elif player.role == Role.SEER:
            return build_seer_instructions(player, self.state, self.seer_results)
        elif player.role == Role.WITCH:
            return build_witch_instructions(player, self.state, None)
        elif player.role == Role.GUARD:
            return build_guard_instructions(player, self.state)
        elif player.role == Role.HUNTER:
            return build_hunter_instructions(player, self.state)
        else:
            return build_villager_instructions(player, self.state)

    async def _vote_phase(self):
        """投票阶段"""
        await self.emit_system("═══ 投票阶段 ═══")
        await self.emit_system("请所有存活玩家投票。")
        self.state.votes = {}

        for p in self.state.alive_players():
            if p.is_human:
                options = [
                    {"seat": op.seat, "name": op.name}
                    for op in self.state.alive_players() if op.seat != p.seat
                ]
                result = await self.request_human_input(
                    "请选择你要投票的对象", "vote", options
                )
                target = result.get("target_seat")
                if target:
                    self.state.votes[p.seat] = target
                    target_p = self.state.get_player(target)
                    await self.emit_action(f"✋ {p.name} 投票给 {target_p.name if target_p else target}")
            else:
                instructions = build_vote_instructions(p, self.state, self.day_speeches)
                history = self._build_history_context(p)
                vote_prompt = f"请投票。你是 {p.name}。"
                if history:
                    vote_prompt = f"{history}\n\n{vote_prompt}"
                try:
                    msg = await call_llm(
                        instructions,
                        vote_prompt,
                        [TOOL_SCHEMAS["vote"]],
                        p.name,
                    )
                except Exception as e:
                    await self.emit_system(f"⚠️ {p.name} 的 AI 投票失败: {str(e)[:80]}")
                    await asyncio.sleep(0.3)
                    continue
                tool_name, tool_args = extract_tool_call(msg)
                if tool_name == "vote" and tool_args:
                    target_seat = tool_args.get("target_seat", 0)
                    target_p = self.state.get_player(target_seat)
                    # 验证目标存活
                    if target_p and target_p.alive:
                        args = {**tool_args, "voter_seat": p.seat}
                        await self._execute_tool(p.name, "vote", args)
                        await self.emit_action(f"✋ {p.name} 投票给 {target_p.name}")
                        # 提取投票内心独白
                        monologue = (msg.get("content") or "").strip()
                        if monologue:
                            await self.emit_action(f"💭 {p.name}: {monologue[:50]}", visibility="god")
                    else:
                        await self.emit_system(f"⚠️ {p.name} 投票目标无效（已死亡或不存在），本票作废")
                else:
                    # 如果 LLM 没有调用 vote tool，尝试从文本提取
                    content = msg.get("content", "")
                    # 尝试按名字匹配存活玩家
                    voted = False
                    for ap in self.state.alive_players():
                        if ap.seat != p.seat and ap.name in content:
                            self.state.votes[p.seat] = ap.seat
                            await self.emit_action(f"✋ {p.name} 投票给 {ap.name}")
                            # 文本回退时，content 就是独白
                            if content.strip():
                                await self.emit_action(f"💭 {p.name}: {content.strip()[:50]}", visibility="god")
                            voted = True
                            break
                    if not voted:
                        numbers = re.findall(r"(\d+)", content)
                        if numbers:
                            target_seat = int(numbers[0])
                            target_p = self.state.get_player(target_seat)
                            if target_p and target_p.alive:
                                self.state.votes[p.seat] = target_seat
                                await self.emit_action(f"✋ {p.name} 投票给 {target_p.name}")
                            else:
                                await self.emit_system(f"⚠️ {p.name} 投票目标无效（已死亡或不存在），本票作废")

            await asyncio.sleep(0.3)

        # 结算投票
        eliminated_seat, announcement = self.state.resolve_vote()
        await self.emit_system(announcement)

    async def _check_hunter_death(self):
        """检查猎人是否刚死亡，触发开枪"""
        for p in self.state.players:
            if p.role == Role.HUNTER and not p.alive:
                # 已处理过则跳过 (简化: 用一个标记)
                if hasattr(p, '_hunter_shot'):
                    continue
                p._hunter_shot = True  # type: ignore

                await self.emit_system(f"💥 猎人 {p.name} 死亡，可以开枪带走一个人！")

                if p.is_human:
                    options = [
                        {"seat": op.seat, "name": op.name}
                        for op in self.state.alive_players()
                    ]
                    result = await self.request_human_input(
                        "你是猎人，请选择开枪带走的目标（或跳过）",
                        "select_player", options
                    )
                    target = result.get("target_seat")
                    if target:
                        from tools.game_actions import make_tools
                        tools = make_tools(self.state)
                        r = tools["shoot_player"](target)
                        await self.emit_action(r)
                else:
                    # 猎人开枪：注入当天发言和投票上下文，强制重试最多3次
                    vote_lines = []
                    for voter_seat, target_seat in self.state.votes.items():
                        voter = self.state.get_player(voter_seat)
                        target_p = self.state.get_player(target_seat)
                        if voter and target_p:
                            vote_lines.append(f"  {voter.name} → {target_p.name}")
                    vote_ctx = "\n".join(vote_lines) if vote_lines else None

                    instructions = build_hunter_shoot_instructions(
                        p, self.state,
                        day_speeches=self.day_speeches if self.day_speeches else None,
                        vote_context=vote_ctx,
                    )
                    shoot_prompt = (
                        f"你是猎人 {p.name}，你刚刚死亡了。"
                        f"你必须立刻使用 shoot_player 工具开枪带走一个你最怀疑的人！"
                        f"请立刻调用 shoot_player 工具，不要犹豫！"
                    )
                    shot_fired = False
                    for attempt in range(3):
                        result = await self._agent_act(
                            p, instructions, shoot_prompt,
                            ["shoot_player", "get_alive_players"],
                        )
                        # 检查是否真的开枪了（查看是否有人因此死亡）
                        if "带走了" in result or "开枪" in result:
                            shot_fired = True
                            break
                        # AI 没调工具，尝试从文本提取目标
                        if result:
                            for ap in self.state.alive_players():
                                if ap.name in result:
                                    from tools.game_actions import make_tools
                                    tools = make_tools(self.state)
                                    r = tools["shoot_player"](ap.seat)
                                    await self.emit_action(f"[猎人行动] {p.name} {r}", visibility="public")
                                    shot_fired = True
                                    break
                        if shot_fired:
                            break
                    if not shot_fired:
                        # 最后兜底：随机射一个存活玩家
                        import random
                        alive = self.state.alive_players()
                        if alive:
                            target = random.choice(alive)
                            from tools.game_actions import make_tools
                            tools = make_tools(self.state)
                            r = tools["shoot_player"](target.seat)
                            await self.emit_action(f"[猎人行动] {p.name} {r}", visibility="public")
                            await self.emit_system(f"猎人 {p.name} 死亡时必须开枪，随机射出！")

    async def _end_game(self, winner: Camp):
        """游戏结束"""
        self.state.phase = Phase.ENDED
        self._running = False

        winner_text = "好人阵营" if winner == Camp.GOOD else "狼人阵营"
        await self.emit_system(f"🏆 游戏结束！{winner_text}获胜！")

        # 揭示所有角色
        for p in self.state.players:
            camp = "🐺" if ROLE_NAMES_CN[p.role] == "狼人" else "👤"
            status = "存活" if p.alive else "死亡"
            await self.emit_system(f"  {p.name} — {ROLE_NAMES_CN[p.role]} {camp} ({status})")

        # 自动存入本地数据库
        try:
            from db import save_game
            from middleware.game_log import get_traces
            game_id = save_game(self.state, get_traces(), observer=self.state.observer_mode)
            self._last_game_id = game_id
            await self.emit_system(f"📁 对局已保存 (#{game_id})，可在历史记录中查看。")
        except Exception as e:
            await self.emit_system(f"⚠️ 保存对局失败: {str(e)[:100]}")

        await self.emit("game_over", self.state.to_full_dict())
