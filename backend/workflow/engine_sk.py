"""游戏引擎 (Semantic Kernel 版) —— 使用微软官方 Agent Framework SDK

通过 Semantic Kernel 的 ChatCompletionAgent + @kernel_function 插件系统
驱动狼人杀游戏，展示真正的 SDK 级 Agent Framework 能力：
  - ChatCompletionAgent 多角色 Agent
  - @kernel_function 注册游戏工具
  - AzureChatCompletion 服务抽象
  - FunctionChoiceBehavior 控制工具调用
  - Human-in-the-loop
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import random
from typing import Optional, Annotated

from dotenv import load_dotenv

from game.state import GameState, GameMessage, Phase, Role, Camp, ROLE_NAMES_CN, ROLE_CAMP
from agents.prompts import (
    build_werewolf_instructions, build_seer_instructions,
    build_witch_instructions, build_guard_instructions,
    build_hunter_instructions, build_villager_instructions,
    build_vote_instructions, build_hunter_shoot_instructions,
)
from middleware.game_log import log_agent_action, log_llm_call, reset_traces
from config import load_config, get_pool_item

# Semantic Kernel imports
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion, OpenAIChatCompletion
from semantic_kernel.connectors.ai.open_ai import OpenAIChatPromptExecutionSettings
from semantic_kernel.connectors.ai import FunctionChoiceBehavior
from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.contents import ChatHistory, FunctionCallContent, FunctionResultContent
from semantic_kernel.functions import kernel_function, KernelArguments

load_dotenv()


def _get_speak_delay() -> float:
    cfg = load_config()
    return cfg.get("speak_delay_ms", 300) / 1000


# 中文名 -> ASCII agent name 映射（SK 要求 name 符合 ^[0-9A-Za-z_-]+$）
_CN_TO_ASCII = {
    "张三": "ZhangSan",
    "李四": "LiSi",
    "王五": "WangWu",
    "赵六": "ZhaoLiu",
    "孙七": "SunQi",
    "周八": "ZhouBa",
    "吴九": "WuJiu",
    "郑十": "ZhengShi",
    "褚九": "ChuJiu",
}


def _safe_agent_name(cn_name: str) -> str:
    """将中文名转为 SK 要求的 ASCII agent name"""
    return _CN_TO_ASCII.get(cn_name, f"Player_{hash(cn_name) % 10000}")


def _make_service_for_player(player_name: str):
    """为指定玩家创建 Chat Completion 服务实例（按玩家名查模型池）"""
    pool_item = get_pool_item(player_name)
    endpoint = pool_item.get("endpoint", "").strip()
    model = pool_item.get("model", "").strip()
    api_key = pool_item.get("api_key", "").strip()
    cfg = load_config()
    api_type = pool_item.get("api_type") or cfg.get("api_type", "azure")
    api_ver = pool_item.get("api_version") or cfg.get("api_version", "2024-12-01-preview")

    if not endpoint:
        raise RuntimeError("未配置 AI 端点，请先在设置页面配置 endpoint")

    if api_type == "openai":
        from openai import AsyncOpenAI
        base_url = endpoint.rstrip("/")
        client = AsyncOpenAI(api_key=api_key or "sk-placeholder", base_url=base_url)
        return OpenAIChatCompletion(
            ai_model_id=model,
            async_client=client,
        )
    else:
        # Azure — 需要处理 APIM 代理端点（以 /openai 结尾的情况）
        clean_endpoint = endpoint.rstrip("/")
        if clean_endpoint.endswith("/openai"):
            # APIM 风格: endpoint 已含 /openai，用 base_url 直接指向 deployments
            from openai import AsyncAzureOpenAI
            client = AsyncAzureOpenAI(
                api_key=api_key,
                api_version=api_ver,
                base_url=f"{clean_endpoint}/deployments/{model}",
            )
            return AzureChatCompletion(
                deployment_name=model,
                async_client=client,
            )
        else:
            kwargs = {
                "deployment_name": model,
                "endpoint": clean_endpoint,
                "api_version": api_ver,
            }
            if api_key:
                kwargs["api_key"] = api_key
            else:
                from azure.identity import DefaultAzureCredential
                kwargs["credential"] = DefaultAzureCredential()
            return AzureChatCompletion(**kwargs)


# ═══════════════════════════════════════════════════
# 游戏工具 Plugin（使用 @kernel_function）
# ═══════════════════════════════════════════════════

class WerewolfGamePlugin:
    """狼人杀游戏工具插件 —— 通过 @kernel_function 注册供 Agent 调用"""

    def __init__(self, state: GameState):
        self._state = state

    @kernel_function(description="狼人选择击杀目标")
    def kill_player(self, target_seat: Annotated[int, "目标玩家座位号 (1-9)"]) -> str:
        p = self._state.get_player(target_seat)
        if not p or not p.alive:
            return f"失败: 座位 {target_seat} 无效或已死亡"
        self._state.night_action.wolf_target = target_seat
        return f"已选择击杀 {target_seat} 号 {p.name}"

    @kernel_function(description="预言家查验一名玩家的身份")
    def check_identity(self, target_seat: Annotated[int, "目标玩家座位号"]) -> str:
        p = self._state.get_player(target_seat)
        if not p or not p.alive:
            return f"失败: 座位 {target_seat} 无效或已死亡"
        self._state.night_action.seer_target = target_seat
        result = "狼人" if ROLE_CAMP[p.role] == Camp.WOLF else "好人"
        self._state.night_action.seer_result = result
        return f"{target_seat} 号 {p.name} 的身份是: {result}"

    @kernel_function(description="女巫使用解药救活今晚被杀的人")
    def use_antidote(self, target_seat: Annotated[int, "要救的座位号"]) -> str:
        witch = None
        for p in self._state.players:
            if p.role == Role.WITCH and p.alive:
                witch = p
                break
        if not witch:
            return "失败: 女巫不存在或已死亡"
        if not witch.has_antidote:
            return "失败: 解药已用过"
        if self._state.night_action.wolf_target != target_seat:
            return "失败: 该玩家今晚没有被杀"
        witch.has_antidote = False
        self._state.night_action.witch_save = True
        return f"已使用解药救了 {target_seat} 号"

    @kernel_function(description="女巫使用毒药杀人")
    def use_poison(self, target_seat: Annotated[int, "目标座位号"]) -> str:
        witch = None
        for p in self._state.players:
            if p.role == Role.WITCH and p.alive:
                witch = p
                break
        if not witch:
            return "失败: 女巫不存在或已死亡"
        if not witch.has_poison:
            return "失败: 毒药已用过"
        target = self._state.get_player(target_seat)
        if not target or not target.alive:
            return f"失败: 座位 {target_seat} 无效或已死亡"
        witch.has_poison = False
        self._state.night_action.witch_poison_target = target_seat
        return f"已使用毒药毒杀 {target_seat} 号 {target.name}"

    @kernel_function(description="守卫保护一名玩家不被狼人杀")
    def protect_player(self, target_seat: Annotated[int, "目标座位号"]) -> str:
        guard = None
        for p in self._state.players:
            if p.role == Role.GUARD and p.alive:
                guard = p
                break
        if not guard:
            return "失败: 守卫不存在或已死亡"
        if guard.last_protected == target_seat:
            return f"失败: 不能连续两晚保护同一个人 ({target_seat} 号)"
        target = self._state.get_player(target_seat)
        if not target or not target.alive:
            return f"失败: 座位 {target_seat} 无效或已死亡"
        guard.last_protected = target_seat
        self._state.night_action.guard_target = target_seat
        return f"已保护 {target_seat} 号 {target.name}"

    @kernel_function(description="猎人开枪带走一名玩家")
    def shoot_player(self, target_seat: Annotated[int, "目标座位号"]) -> str:
        target = self._state.get_player(target_seat)
        if not target or not target.alive:
            return f"失败: 座位 {target_seat} 无效或已死亡"
        target.alive = False
        return f"猎人开枪带走了 {target_seat} 号 {target.name}"

    @kernel_function(description="投票处决一名玩家")
    def vote(self, voter_seat: Annotated[int, "投票者座位号"], target_seat: Annotated[int, "被投者座位号"]) -> str:
        voter = self._state.get_player(voter_seat)
        target = self._state.get_player(target_seat)
        if not voter or not voter.alive:
            return "失败: 投票者无效"
        if not target or not target.alive:
            return f"失败: 目标 {target_seat} 无效或已死亡"
        self._state.votes[voter_seat] = target_seat
        return f"{voter.name} 投票给 {target.name}"

    @kernel_function(description="查看当前存活的玩家列表")
    def get_alive_players(self) -> str:
        alive = self._state.alive_players()
        lines = [f"  {p.name}（{p.seat}号）" for p in alive]
        return f"存活玩家 ({len(alive)} 人):\n" + "\n".join(lines)

    @kernel_function(description="跳过当前操作，不做任何事")
    def skip_action(self) -> str:
        return "已跳过"


# tool_name 到可见性的映射（与手搓版一致）
_TOOL_VISIBILITY = {
    "kill_player": "wolf_team",
    "check_identity": "seer",
    "use_antidote": "witch",
    "use_poison": "witch",
    "protect_player": "guard",
    "shoot_player": "public",
    "vote": "public",
    "skip_action": "god",
}


class GameEngine:
    """协调游戏流程的核心引擎 (Semantic Kernel 版)

    与手搓版 GameEngine 保持完全相同的外部接口和游戏逻辑。
    """

    def __init__(self):
        self.state = GameState()
        self.event_queue: asyncio.Queue[dict] = asyncio.Queue()
        self.human_input_event = asyncio.Event()
        self.human_input_data: dict = {}
        self.seer_results: list[tuple[int, str]] = []
        self.day_speeches: list[tuple[str, str]] = []
        self._running = False
        self._plugin: Optional[WerewolfGamePlugin] = None

    # ─── 事件推送（与手搓版签名完全一致）───

    async def emit(self, event_type: str, data: dict):
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
        self.human_input_event.clear()
        await self.emit("human_input_request", {
            "prompt": prompt,
            "input_type": input_type,
            "options": options or [],
        })
        await self.human_input_event.wait()
        return self.human_input_data

    def submit_human_input(self, data: dict):
        self.human_input_data = data
        self.human_input_event.set()

    # ─── SK Agent 创建与调用 ───

    def _create_agent(self, player_name: str, instructions: str,
                      enable_tools: bool = True) -> ChatCompletionAgent:
        """为单个玩家创建 SK ChatCompletionAgent"""
        service = _make_service_for_player(player_name)
        cfg = load_config()
        settings = OpenAIChatPromptExecutionSettings(
            temperature=cfg.get("temperature", 0.8),
            max_completion_tokens=cfg.get("max_tokens", 300),
        )

        kwargs = {
            "service": service,
            "name": _safe_agent_name(player_name),
            "instructions": instructions,
            "arguments": KernelArguments(settings=settings),
        }
        if enable_tools:
            kwargs["plugins"] = [self._plugin]
            kwargs["function_choice_behavior"] = FunctionChoiceBehavior.Auto()
        return ChatCompletionAgent(**kwargs)

    async def _agent_act(self, player, system_prompt: str, user_prompt: str,
                         tool_names: list[str] | None = None) -> str:
        """让一个 Agent 行动，处理工具调用结果和内心独白。

        SK 的 agent.get_response() 会自动执行 function calling 流程
        （发现 tool_call → 执行 @kernel_function → 返回最终文本）。
        从 response.thread 提取完整的对话历史来检测工具调用和 token 消耗。
        """
        agent = self._create_agent(player.name, system_prompt, enable_tools=bool(tool_names))
        history = ChatHistory()
        history.add_user_message(user_prompt)

        try:
            response = await agent.get_response(messages=history)
        except Exception as e:
            err_msg = f"{player.name} 的 AI 调用失败: {str(e)[:120]}"
            await self.emit_system(f"⚠️ {err_msg}")
            log_agent_action(player.name, "error", err_msg)
            return ""

        content = str(response).strip() if response else ""

        # 从 thread 收集完整消息记录（包含中间的 function call/result）
        thread_msgs = []
        try:
            async for msg in response.thread.get_messages():
                thread_msgs.append(msg)
        except Exception:
            pass

        # 提取 token 消耗（累加所有 API 调用的 usage）
        total_tokens = 0
        for msg in thread_msgs:
            usage = msg.metadata.get('usage') if msg.metadata else None
            if usage:
                total_tokens += (getattr(usage, 'prompt_tokens', 0) or 0) + (getattr(usage, 'completion_tokens', 0) or 0)

        log_llm_call(player.name, user_prompt[:500], content[:500], total_tokens)

        # 从 thread 消息中提取工具调用记录
        tool_called = False
        tool_result = ""
        for msg in thread_msgs:
            for item in (msg.items if hasattr(msg, 'items') else []):
                if isinstance(item, FunctionCallContent):
                    fn_name = item.function_name or item.name or ''
                    if '-' in fn_name:
                        fn_name = fn_name.split('-', 1)[1]
                    log_agent_action(player.name, "tool_call", fn_name)
                elif isinstance(item, FunctionResultContent):
                    result_text = str(item.result) if item.result else ''
                    fn_name = item.function_name or item.name or ''
                    if '-' in fn_name:
                        fn_name = fn_name.split('-', 1)[1]
                    if result_text and "失败" not in result_text:
                        tool_called = True
                        tool_result = result_text
                        role_cn = ROLE_NAMES_CN.get(player.role, "未知")
                        visibility = _TOOL_VISIBILITY.get(fn_name, "god")
                        detail_msg = f"[{role_cn}行动] {player.name} {result_text}"
                        await self.emit_action(detail_msg, visibility=visibility)

        # 提取内心独白
        if tool_called and content:
            await self.emit_action(f"💭 {player.name}: {content[:50]}", visibility="god")

        if not tool_called and content:
            role_cn = ROLE_NAMES_CN.get(player.role, "未知")
            await self.emit_action(f"[{role_cn}行动] {player.name} {content[:60]}", visibility="god")

        return tool_result if tool_called else content

    # ─── 历史上下文（与手搓版一致）───

    def _build_history_context(self, player=None) -> str:
        """从 state.messages 提取该玩家可见的历史记录"""
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
                    continue
                elif vis == "wolf_team":
                    if player_camp == "wolf":
                        lines.append(f"[行动] {m.content}")
                elif vis == player_role:
                    lines.append(f"[行动] {m.content}")
        if not lines:
            return ""
        recent = lines[-120:]
        return "## 历史记录（你已知道的公开信息）\n" + "\n".join(recent)

    # ─── 游戏主流程（与手搓版同构）───

    async def start(self, human_name: str = "玩家", observer: bool = False):
        reset_traces()
        self.state = GameState()
        self._plugin = WerewolfGamePlugin(self.state)
        self.seer_results = []
        self._running = True
        self.state.init_game(human_name, observer=observer)

        if observer:
            await self.emit_system("游戏开始！旁观模式 —— 9 个 AI 玩家对战，你是上帝视角。")
            await self.emit_system("所有角色分配已展示，尽情观战！")
        else:
            human = [p for p in self.state.players if p.is_human][0]
            await self.emit_system(f"游戏开始！共 9 名玩家。")
            await self.emit_system(f"你是【{human.name}】，角色是【{ROLE_NAMES_CN[human.role]}】")
        await self.emit("state_update", self.state.to_dict())

        try:
            while self._running:
                self.state.phase = Phase.NIGHT
                await self.emit("state_update", self.state.to_dict())
                await self._night_phase()

                announcements = self.state.resolve_night()
                self.state.phase = Phase.DAY
                await self.emit("state_update", self.state.to_dict())

                for ann in announcements:
                    await self.emit_system(ann)
                    await asyncio.sleep(1)

                await self._check_hunter_death()

                winner = self.state.check_winner()
                if winner:
                    await self._end_game(winner)
                    return

                self.state.save_checkpoint(f"第{self.state.round_number}轮_天亮")

                await self._day_phase()

                self.state.phase = Phase.VOTE
                await self.emit("state_update", self.state.to_dict())
                await self._vote_phase()

                await self._check_hunter_death()

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

    # ─── 夜晚阶段 ───

    async def _night_phase(self):
        await self.emit_system(f"═══ 第 {self.state.round_number} 个夜晚 ═══")
        await self.emit_system("🌙 天黑请闭眼...")
        await asyncio.sleep(1)

        await self._guard_action()
        await self.emit_system("守卫闭眼。")
        await asyncio.sleep(0.5)

        await self._wolf_action()
        await self.emit_system("狼人闭眼。")
        await asyncio.sleep(0.5)

        await self._seer_action()
        await self.emit_system("预言家闭眼。")
        await asyncio.sleep(0.5)

        await self._witch_action()
        await self.emit_system("女巫闭眼。天快亮了...")
        await asyncio.sleep(0.5)

    async def _guard_action(self):
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
                "你是守卫，请选择要保护的玩家（或跳过）", "select_player", options
            )
            target = result.get("target_seat")
            if target:
                r = self._plugin.protect_player(target)
                await self.emit_action(f"[守卫行动] {guard.name} {r}", visibility="guard")
        else:
            instructions = build_guard_instructions(guard, self.state)
            history = self._build_history_context(guard)
            user_prompt = f"{history}\n\n现在是夜晚，请选择保护对象。" if history else "现在是夜晚，请选择保护对象。"
            await self._agent_act(guard, instructions, user_prompt,
                                  ["protect_player", "skip_action", "get_alive_players"])

        await asyncio.sleep(_get_speak_delay())

    async def _wolf_action(self):
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

        if ai_wolves:
            wolf = ai_wolves[0]
            teammate = wolves[1] if len(wolves) > 1 else wolf
            instructions = build_werewolf_instructions(wolf, teammate, self.state)
            history = self._build_history_context(wolf)
            user_prompt = f"{history}\n\n现在是夜晚，请与队友协商并选择击杀目标。" if history else "现在是夜晚，请与队友协商并选择击杀目标。"
            await self._agent_act(wolf, instructions, user_prompt,
                                  ["kill_player", "get_alive_players"])

        if human_wolf:
            options = [
                {"seat": p.seat, "name": p.name}
                for p in self.state.alive_players() if p.seat != human_wolf.seat
            ]
            result = await self.request_human_input(
                "你是狼人，请选择今晚要击杀的目标", "select_player", options
            )
            target = result.get("target_seat")
            if target:
                self._plugin.kill_player(target)

        await asyncio.sleep(_get_speak_delay())

    async def _seer_action(self):
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
                "你是预言家，请选择要查验身份的玩家", "select_player", options
            )
            target = result.get("target_seat")
            if target:
                r = self._plugin.check_identity(target)
                await self.emit_action(f"[查验结果] {r}", visibility="seer")
                self.seer_results.append((target, self.state.night_action.seer_result or ""))
        else:
            instructions = build_seer_instructions(seer, self.state, self.seer_results)
            history = self._build_history_context(seer)
            user_prompt = f"{history}\n\n现在是夜晚，请选择要查验的玩家。" if history else "现在是夜晚，请选择要查验的玩家。"
            await self._agent_act(seer, instructions, user_prompt,
                                  ["check_identity", "get_alive_players"])
            if self.state.night_action.seer_result:
                self.seer_results.append((
                    self.state.night_action.seer_target or 0,
                    self.state.night_action.seer_result,
                ))

        await asyncio.sleep(_get_speak_delay())

    async def _witch_action(self):
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
                self._plugin.use_antidote(self.state.night_action.wolf_target)
            elif action == "poison":
                target = result.get("target_seat")
                if target:
                    self._plugin.use_poison(target)
        else:
            instructions = build_witch_instructions(witch, self.state, killed_name)
            history = self._build_history_context(witch)
            user_prompt = f"{history}\n\n现在是夜晚，请决定是否使用药水。" if history else "现在是夜晚，请决定是否使用药水。"
            await self._agent_act(witch, instructions, user_prompt,
                                  ["use_antidote", "use_poison", "skip_action", "get_alive_players"])

        await asyncio.sleep(_get_speak_delay())

    # ─── 白天阶段 ───

    async def _day_phase(self):
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
                    f"轮到你发言了（{ROLE_NAMES_CN[p.role]}）", "speak", None
                )
                speech = result.get("content", "我没什么要说的。")
                self.day_speeches.append((p.name, speech))
                await self.emit_speech(p.name, speech)
            else:
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

                # 白天发言不需要工具调用
                agent = self._create_agent(p.name, instructions, enable_tools=False)
                chat = ChatHistory()
                chat.add_user_message(user_prompt)

                try:
                    response = await agent.get_response(messages=chat)
                    speech = str(response).strip()[:200] if response else "我同意大家的意见。"
                except Exception as e:
                    speech = "我暂时没有什么要说的。"
                    await self.emit_system(f"⚠️ {p.name} 的 AI 发言失败: {str(e)[:80]}")
                    response = None

                # 提取 token
                tokens = 0
                if response:
                    try:
                        async for msg in response.thread.get_messages():
                            usage = msg.metadata.get('usage') if msg.metadata else None
                            if usage:
                                tokens += (getattr(usage, 'prompt_tokens', 0) or 0) + (getattr(usage, 'completion_tokens', 0) or 0)
                    except Exception:
                        pass
                log_llm_call(p.name, user_prompt[:500], speech[:500], tokens)
                self.day_speeches.append((p.name, speech))
                await self.emit_speech(p.name, speech)

            await asyncio.sleep(_get_speak_delay())

    def _build_day_instructions(self, player) -> str:
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

    # ─── 投票阶段 ───

    async def _vote_phase(self):
        await self.emit_system("═══ 投票阶段 ═══")
        await self.emit_system("请所有存活玩家投票。")
        self.state.votes = {}

        for p in self.state.alive_players():
            if p.is_human:
                options = [
                    {"seat": op.seat, "name": op.name}
                    for op in self.state.alive_players() if op.seat != p.seat
                ]
                result = await self.request_human_input("请选择你要投票的对象", "vote", options)
                target = result.get("target_seat")
                if target:
                    self.state.votes[p.seat] = target
                    target_p = self.state.get_player(target)
                    await self.emit_action(f"✋ {p.name} 投票给 {target_p.name if target_p else target}")
            else:
                instructions = build_vote_instructions(p, self.state, self.day_speeches)
                history = self._build_history_context(p)
                vote_prompt = f"请投票。你是 {p.name}，座位号 {p.seat}。"
                if history:
                    vote_prompt = f"{history}\n\n{vote_prompt}"

                # 投票使用工具调用
                agent = self._create_agent(p.name, instructions, enable_tools=True)
                chat = ChatHistory()
                chat.add_user_message(vote_prompt)

                try:
                    response = await agent.get_response(messages=chat)
                    content = str(response).strip() if response else ""
                except Exception as e:
                    await self.emit_system(f"⚠️ {p.name} 的 AI 投票失败: {str(e)[:80]}")
                    await asyncio.sleep(0.3)
                    continue

                # 提取 token 和 tool call
                tokens = 0
                try:
                    async for msg in response.thread.get_messages():
                        usage = msg.metadata.get('usage') if msg.metadata else None
                        if usage:
                            tokens += (getattr(usage, 'prompt_tokens', 0) or 0) + (getattr(usage, 'completion_tokens', 0) or 0)
                        for item in (msg.items if hasattr(msg, 'items') else []):
                            if isinstance(item, FunctionCallContent):
                                fn_name = item.function_name or item.name or ''
                                if '-' in fn_name:
                                    fn_name = fn_name.split('-', 1)[1]
                                log_agent_action(p.name, "tool_call", fn_name)
                except Exception:
                    pass
                log_llm_call(p.name, vote_prompt[:500], content[:500], tokens)

                # 检查 SK 是否自动执行了 vote tool（会把结果写入 state.votes）
                if p.seat in self.state.votes:
                    target_seat = self.state.votes[p.seat]
                    target_p = self.state.get_player(target_seat)
                    if target_p and target_p.alive:
                        await self.emit_action(f"✋ {p.name} 投票给 {target_p.name}")
                        if content:
                            await self.emit_action(f"💭 {p.name}: {content[:50]}", visibility="god")
                    else:
                        del self.state.votes[p.seat]
                        await self.emit_system(f"⚠️ {p.name} 投票目标无效（已死亡或不存在），本票作废")
                else:
                    # SK 没自动调用 vote tool，从文本提取
                    voted = False
                    for ap in self.state.alive_players():
                        if ap.seat != p.seat and ap.name in content:
                            self.state.votes[p.seat] = ap.seat
                            await self.emit_action(f"✋ {p.name} 投票给 {ap.name}")
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

        eliminated_seat, announcement = self.state.resolve_vote()
        await self.emit_system(announcement)

    # ─── 猎人开枪 ───

    async def _check_hunter_death(self):
        for p in self.state.players:
            if p.role == Role.HUNTER and not p.alive:
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
                        r = self._plugin.shoot_player(target)
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
                        if "带走了" in result or "开枪" in result:
                            shot_fired = True
                            break
                        # AI 没调工具，尝试从文本提取目标
                        if result:
                            for ap in self.state.alive_players():
                                if ap.name in result:
                                    r = self._plugin.shoot_player(ap.seat)
                                    await self.emit_action(f"[猎人行动] {p.name} {r}", visibility="public")
                                    shot_fired = True
                                    break
                        if shot_fired:
                            break
                    if not shot_fired:
                        alive = self.state.alive_players()
                        if alive:
                            target = random.choice(alive)
                            r = self._plugin.shoot_player(target.seat)
                            await self.emit_action(f"[猎人行动] {p.name} {r}", visibility="public")
                            await self.emit_system(f"猎人 {p.name} 死亡时必须开枪，随机射出！")

    # ─── 游戏结束 ───

    async def _end_game(self, winner: Camp):
        self.state.phase = Phase.ENDED
        self._running = False

        winner_text = "好人阵营" if winner == Camp.GOOD else "狼人阵营"
        await self.emit_system(f"🏆 游戏结束！{winner_text}获胜！")

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
