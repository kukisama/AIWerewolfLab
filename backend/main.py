"""HTTP 服务器 —— aiohttp + SSE 为 Tauri 前端提供 API"""
from __future__ import annotations
import asyncio
import json
import os
import sys

from aiohttp import web
from dotenv import load_dotenv

# 确保 backend 目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from middleware.game_log import get_traces
from config import load_config, save_config, AGENT_ROLES, ROLE_LABELS, ENGINE_MODES, get_pool_item


def _create_engine():
    """根据配置创建对应引擎"""
    cfg = load_config()
    mode = cfg.get("engine_mode", "manual")
    if mode == "sdk":
        try:
            from workflow.engine_sk import GameEngine
            print("🧠 使用 Semantic Kernel 引擎")
        except ImportError as e:
            print(f"⚠️ SK 引擎加载失败 ({e})，回退到手搓版")
            from workflow.engine import GameEngine
    else:
        from workflow.engine import GameEngine
        print("🔧 使用手搓版引擎")
    return GameEngine()


engine = _create_engine()
_game_task: asyncio.Task | None = None
# 正在运行的 LLM 后台任务 {assistant_msg_id: asyncio.Task}
_llm_tasks: dict[int, asyncio.Task] = {}


async def handle_start(request: web.Request) -> web.Response:
    """POST /game/start — 开始新游戏"""
    global _game_task
    body = await request.json()
    player_name = body.get("player_name", "玩家")
    game_mode = body.get("game_mode", "player")  # "observer" or "player"
    observer = game_mode == "observer"

    # 如果有正在运行的游戏，取消
    if _game_task and not _game_task.done():
        _game_task.cancel()
        try:
            await _game_task
        except asyncio.CancelledError:
            pass

    _game_task = asyncio.create_task(engine.start(player_name, observer=observer))
    return web.json_response({"status": "started", "player_name": player_name, "game_mode": game_mode})


async def handle_state(request: web.Request) -> web.Response:
    """GET /game/state — 获取当前游戏状态"""
    return web.json_response(engine.state.to_dict())


async def handle_action(request: web.Request) -> web.Response:
    """POST /game/action — 人类玩家提交动作"""
    body = await request.json()
    engine.submit_human_input(body)
    return web.json_response({"status": "ok"})


async def handle_stream(request: web.Request) -> web.StreamResponse:
    """GET /game/stream — SSE 事件流"""
    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await response.prepare(request)

    try:
        while True:
            event = await engine.event_queue.get()
            event_type = event.get("event", "message")
            data = json.dumps(event.get("data", {}), ensure_ascii=False)
            payload = f"event: {event_type}\ndata: {data}\n\n"
            await response.write(payload.encode("utf-8"))
    except (asyncio.CancelledError, ConnectionResetError):
        pass

    return response


async def handle_traces(request: web.Request) -> web.Response:
    """GET /game/traces — OpenTelemetry 追踪数据"""
    return web.json_response(get_traces())


async def handle_checkpoints(request: web.Request) -> web.Response:
    """GET /game/checkpoints — Checkpoint 列表"""
    return web.json_response(engine.state.checkpoints)


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — 健康检查"""
    return web.json_response({"status": "ok"})


async def handle_get_config(request: web.Request) -> web.Response:
    """GET /config — 获取当前配置（含元数据）"""
    cfg = load_config()
    cfg["roles"] = AGENT_ROLES
    cfg["role_labels"] = ROLE_LABELS
    cfg["engine_modes"] = ENGINE_MODES           # {"manual": "🔧 手搓版", "sdk": "🧠 SDK 版"}
    from config import API_TYPES
    cfg["api_types"] = API_TYPES                  # {"azure": ..., "openai": ...}
    from game.state import AI_NAMES
    cfg["ai_names"] = AI_NAMES
    return web.json_response(cfg)


async def handle_save_config(request: web.Request) -> web.Response:
    """POST /config — 保存全量配置"""
    body = await request.json()
    old_cfg = load_config()
    cfg = {
        "engine_mode":      body.get("engine_mode", old_cfg.get("engine_mode", "manual")),
        "api_type":          body.get("api_type", old_cfg.get("api_type", "responses")),
        "api_version":       body.get("api_version", old_cfg.get("api_version", "2025-03-01-preview")),
        "temperature":       body.get("temperature", old_cfg.get("temperature", 0.8)),
        "max_tokens":        body.get("max_tokens", old_cfg.get("max_tokens", 300)),
        "speak_delay_ms":    body.get("speak_delay_ms", old_cfg.get("speak_delay_ms", 300)),
        "model_pool":        body.get("model_pool", old_cfg.get("model_pool", [])),
        "agent_model_index": body.get("agent_model_index", old_cfg.get("agent_model_index", {})),
    }
    save_config(cfg)
    need_restart = old_cfg.get("engine_mode") != cfg["engine_mode"]
    return web.json_response({"status": "saved", "need_restart": need_restart})


async def handle_restart_engine(request: web.Request) -> web.Response:
    """POST /engine/restart — 切换引擎后重载"""
    global engine, _game_task
    if _game_task and not _game_task.done():
        _game_task.cancel()
        _game_task = None
    engine = _create_engine()
    return web.json_response({"status": "restarted", "engine_mode": load_config().get("engine_mode", "manual")})


async def handle_summary(request: web.Request) -> web.Response:
    """POST /game/summary — AI 总结本局对战表现（上帝视角完整日志）"""
    if not engine.state or not engine.state.players:
        return web.json_response({"error": "没有进行中的游戏"}, status=400)

    from game.state import ROLE_NAMES_CN, ROLE_CAMP
    # 汇总**所有**消息（上帝视角，包含夜晚行动）
    messages_text = []
    for m in engine.state.messages:
        if m.msg_type == "system":
            messages_text.append(f"[系统] {m.content}")
        elif m.msg_type == "action":
            messages_text.append(f"[行动] {m.content}")
        elif m.msg_type == "chat":
            messages_text.append(f"{m.sender}: {m.content}")
    log = "\n".join(messages_text)  # 不再截断

    # 玩家身份表
    roster = []
    for p in engine.state.players:
        camp_cn = "狼人阵营" if ROLE_CAMP[p.role].value == "wolf" else "好人阵营"
        status = "存活" if p.alive else "死亡"
        roster.append(f"  {p.name} — {ROLE_NAMES_CN[p.role]}（{camp_cn}） — {status}")

    winner = engine.state.winner
    winner_text = "好人阵营获胜" if winner and winner.value == "good" else "狼人阵营获胜" if winner else "游戏尚未结束"

    system_prompt = f"""你是一个狼人杀游戏的专业解说和分析师。你拥有上帝视角，知道每个玩家的真实身份和所有夜晚行动。

## 玩家身份
{chr(10).join(roster)}

## 结果
{winner_text}

## 要求
1. 总结本局的关键转折点
2. 评价每个玩家的表现（发言质量、逻辑推理、伪装/破绽）
3. 评选本局 MVP（最佳表现者）和最差表现者
4. 用中文输出，语言生动有趣，1000字左右"""

    user_prompt = f"以下是完整的对战记录（上帝视角，包含所有夜晚行动）：\n\n{log}"

    try:
        from workflow.engine import call_llm
        message = await call_llm(system_prompt, user_prompt, None, "summary", max_tokens_override=4000)
        summary = message.get("content", "总结生成失败。")
        # 如果当前有 game_id，存入数据库
        if hasattr(engine, '_last_game_id') and engine._last_game_id:
            from db import update_summary
            update_summary(engine._last_game_id, summary)
        return web.json_response({"summary": summary})
    except Exception as e:
        return web.json_response({"error": f"总结生成失败: {str(e)[:200]}"}, status=500)


# ═══════════════════════════════════════════════════
# 历史记录 API
# ═══════════════════════════════════════════════════

async def handle_history_list(request: web.Request) -> web.Response:
    """GET /history — 列出历史对局"""
    from db import list_games
    games = list_games(limit=50)
    return web.json_response(games)


async def handle_history_detail(request: web.Request) -> web.Response:
    """GET /history/{game_id} — 获取单局完整记录"""
    game_id = int(request.match_info["game_id"])
    from db import get_game
    game = get_game(game_id)
    if not game:
        return web.json_response({"error": "对局不存在"}, status=404)
    return web.json_response(game)


async def handle_history_ask(request: web.Request) -> web.Response:
    """POST /history/{game_id}/ask — 提交问题，立即返回，后台异步跑 LLM 写 DB

    body: { question, conversation_id?, perspective? }
    返回: { conversation_id, assistant_msg_id }
    前端通过轮询 /conversations/{conv_id} 获取进度。
    """
    game_id = int(request.match_info["game_id"])
    body = await request.json()
    question = body.get("question", "").strip()
    if not question:
        return web.json_response({"error": "请输入问题"}, status=400)

    conversation_id = body.get("conversation_id")
    perspective = body.get("perspective", "god")

    from db import (get_game, get_game_log_text, create_conversation,
                    add_chat_message, get_conversation_messages, get_conversation_info,
                    update_conversation_title, update_chat_message)

    game = get_game(game_id)
    if not game:
        return web.json_response({"error": "对局不存在"}, status=404)

    # 自动创建或验证会话
    if not conversation_id:
        conversation_id = create_conversation(game_id, perspective, title=question[:30])
    else:
        info = get_conversation_info(conversation_id)
        if not info or info["game_id"] != game_id:
            return web.json_response({"error": "会话不存在"}, status=404)
        perspective = info["perspective"]

    # 保存用户消息 + 创建空 assistant 占位
    add_chat_message(conversation_id, "user", question)
    assistant_msg_id = add_chat_message(conversation_id, "assistant", "")

    # 构建 prompt 所需上下文（在主协程中完成，避免并发读写问题）
    log_text = get_game_log_text(game_id, perspective=perspective)

    if perspective == "god":
        players_text = "\n".join(
            f"  {p['name']} — {p['role_cn']}（{'狼人阵营' if p['camp'] == 'wolf' else '好人阵营'}）— {'存活' if p['alive'] else '死亡'}"
            for p in game["players"]
        )
        role_hint = "你拥有上帝视角，知道所有玩家的身份和夜晚行动。"
    else:
        player_info = next((p for p in game["players"] if p["name"] == perspective), None)
        if player_info:
            players_text = f"  你是 {player_info['name']}，角色是 {player_info['role_cn']}（{'狼人阵营' if player_info['camp'] == 'wolf' else '好人阵营'}）"
            role_hint = f"你正在以 {perspective} 的视角分析这局游戏。你只知道 {perspective} 能看到的信息。"
        else:
            players_text = ""
            role_hint = "你拥有上帝视角。"

    winner_text = "好人阵营获胜" if game["winner"] == "good" else "狼人阵营获胜" if game["winner"] == "wolf" else "游戏被终止" if game["winner"] == "aborted" else "未结束"

    system_prompt = f"""你是一个狼人杀游戏分析师。{role_hint}请根据对战记录回答用户的问题。

## 玩家信息
{players_text}

## 结果
{winner_text}（共 {game['rounds']} 轮）

回答要准确、有证据支持，引用对局中的具体发言或行动。用中文回答。"""

    history_msgs = get_conversation_messages(conversation_id)
    conversation_context = f"## 对战记录（{perspective} 视角）\n{log_text}\n\n"
    for msg in history_msgs[:-2]:  # 排除刚存入的 user + assistant 占位
        if msg["role"] == "user":
            conversation_context += f"用户: {msg['content']}\n"
        else:
            conversation_context += f"分析师: {msg['content']}\n"
    conversation_context += f"用户: {question}"

    # 启动后台 Task 跑 LLM
    async def _run_llm_task():
        full_answer = ""
        chunk_count = 0
        try:
            from workflow.engine import call_llm_stream
            async for chunk in call_llm_stream(system_prompt, conversation_context, "historian", max_tokens_override=2000):
                full_answer += chunk
                chunk_count += 1
                if chunk_count % 8 == 0:
                    update_chat_message(assistant_msg_id, full_answer)
        except Exception as e:
            full_answer = f"❌ 回答失败: {str(e)[:200]}"
        finally:
            answer = full_answer.strip() or "回答生成失败。"
            update_chat_message(assistant_msg_id, answer)
            if len(history_msgs) <= 2:
                update_conversation_title(conversation_id, question[:30])
            _llm_tasks.pop(assistant_msg_id, None)

    task = asyncio.create_task(_run_llm_task())
    _llm_tasks[assistant_msg_id] = task

    return web.json_response({
        "conversation_id": conversation_id,
        "assistant_msg_id": assistant_msg_id,
    })


async def handle_history_summary(request: web.Request) -> web.Response:
    """POST /history/{game_id}/summary — 为历史对局生成/重新生成 AI 总结"""
    game_id = int(request.match_info["game_id"])
    from db import get_game, get_game_log_text, update_summary

    game = get_game(game_id)
    if not game:
        return web.json_response({"error": "对局不存在"}, status=404)

    log_text = get_game_log_text(game_id)
    players_text = "\n".join(
        f"  {p['name']} — {p['role_cn']}（{'狼人阵营' if p['camp'] == 'wolf' else '好人阵营'}）— {'存活' if p['alive'] else '死亡'}"
        for p in game["players"]
    )
    winner_text = "好人阵营获胜" if game["winner"] == "good" else "狼人阵营获胜" if game["winner"] == "wolf" else "未结束"

    system_prompt = f"""你是一个狼人杀游戏的专业解说和分析师。你拥有上帝视角，知道每个玩家的真实身份和所有夜晚行动。

## 玩家身份
{players_text}

## 结果
{winner_text}

## 要求
1. 总结本局的关键转折点
2. 评价每个玩家的表现（发言质量、逻辑推理、伪装/破绽）
3. 评选本局 MVP（最佳表现者）和最差表现者
4. 用中文输出，语言生动有趣，1000字左右"""

    user_prompt = f"以下是完整的对战记录（上帝视角）：\n\n{log_text}"

    try:
        from workflow.engine import call_llm
        message = await call_llm(system_prompt, user_prompt, None, "summary", max_tokens_override=4000)
        summary = message.get("content", "总结生成失败。")
        update_summary(game_id, summary)
        return web.json_response({"summary": summary})
    except Exception as e:
        return web.json_response({"error": f"总结生成失败: {str(e)[:200]}"}, status=500)


async def handle_history_perspective(request: web.Request) -> web.Response:
    """GET /history/{game_id}/perspective/{player_name} — 获取某玩家视角的消息

    过滤规则（基于 visibility 标签）：
    - public：所有人可见（系统公告、投票、猎人开枪等）
    - god：仅上帝视角（内心独白等）—— 玩家不可见
    - wolf_team：狼人阵营可见
    - seer/witch/guard/hunter：对应角色可见
    - system/chat 消息：全部可见
    - 玩家死亡后停止显示后续消息
    """
    game_id = int(request.match_info["game_id"])
    player_name = request.match_info["player_name"]

    from db import get_game
    game = get_game(game_id)
    if not game:
        return web.json_response({"error": "对局不存在"}, status=404)

    # 找到该玩家的信息
    player_info = None
    for p in game["players"]:
        if p["name"] == player_name:
            player_info = p
            break
    if not player_info:
        return web.json_response({"error": f"玩家 {player_name} 不存在"}, status=404)

    role = player_info["role"]       # werewolf, seer, etc.
    camp = player_info.get("camp", "good")  # wolf or good

    filtered = []
    player_dead = False

    import re as _re

    def _is_player_death(msg_content: str, pname: str) -> bool:
        """精确判断该消息是否表示 pname 本人死亡/出局，
        排除仅在投票列表中被提及的情况。"""
        # 格式: "昨晚，周八 死亡。"  "昨晚，周八、张三 死亡。"
        if "死亡" in msg_content:
            # 提取逗号之前的死亡名单部分（排除投票详情）
            m = _re.match(r"^昨晚[，,]\s*(.+?)\s*死亡", msg_content)
            if m:
                return pname in m.group(1)
            # 其他死亡格式: 直接 "XX 死亡"
            if msg_content.strip().startswith(pname) and "死亡" in msg_content:
                return True
        # 格式: "李四 以 7 票被投出局。[张三→李四, ...]"
        if "被投出局" in msg_content or "出局" in msg_content:
            m = _re.match(r"^(.+?)\s*以\s*\d+\s*票被投出局", msg_content)
            if m:
                return pname == m.group(1).strip()
            # "XX 出局" / "XX 被淘汰"
            if msg_content.strip().startswith(pname):
                return True
        if "淘汰" in msg_content:
            if msg_content.strip().startswith(pname):
                return True
        return False

    for m in game["messages"]:
        content = m.get("content", "")
        vis = m.get("visibility", "public")

        # 精确检测该玩家的死亡
        if m["type"] == "system" and _is_player_death(content, player_name):
            filtered.append(m)
            player_dead = True
            continue

        # 死亡后仍可看到公开信息（系统、聊天、公开行动），但看不到私密行动
        if player_dead:
            if m["type"] in ("system", "chat"):
                filtered.append(m)
            elif m["type"] == "action" and vis == "public":
                filtered.append(m)
            continue

        if m["type"] == "system":
            filtered.append(m)
        elif m["type"] == "chat":
            filtered.append(m)
        elif m["type"] == "action":
            if vis == "public":
                filtered.append(m)
            elif vis == "god":
                # 自己的内心独白可见（💭 张三: ...）
                if f"💭 {player_name}:" in content or f"💭 {player_name}：" in content:
                    filtered.append(m)
            elif vis == "wolf_team":
                if camp == "wolf":
                    filtered.append(m)
            elif vis == role:
                # 角色专属信息（seer/witch/guard/hunter）
                filtered.append(m)
            # 其他：该玩家看不到

    return web.json_response({
        "player": player_info,
        "messages": filtered,
        "total_messages": len(game["messages"]),
        "visible_messages": len(filtered),
    })


async def handle_conversations_list(request: web.Request) -> web.Response:
    """GET /history/{game_id}/conversations — 列出某局的所有对话会话"""
    game_id = int(request.match_info["game_id"])
    from db import list_conversations
    convs = list_conversations(game_id)
    return web.json_response(convs)


async def handle_conversation_messages(request: web.Request) -> web.Response:
    """GET /history/{game_id}/conversations/{conv_id} — 获取某个会话的消息"""
    conv_id = int(request.match_info["conv_id"])
    from db import get_conversation_info, get_conversation_messages
    info = get_conversation_info(conv_id)
    if not info:
        return web.json_response({"error": "会话不存在"}, status=404)
    msgs = get_conversation_messages(conv_id)
    return web.json_response({"conversation": info, "messages": msgs})


async def handle_stop_game(request: web.Request) -> web.Response:
    """POST /game/stop — 终止正在进行的游戏"""
    global _game_task
    if not _game_task or _game_task.done():
        return web.json_response({"error": "没有正在进行的游戏"}, status=400)

    # 取消游戏协程
    _game_task.cancel()
    try:
        await _game_task
    except asyncio.CancelledError:
        pass
    _game_task = None

    # 标记引擎停止
    engine._running = False

    # 发出终止消息
    from game.state import GameMessage
    abort_msg = GameMessage(sender="system", content="⛔ 游戏已被人类玩家/上帝终止。", msg_type="system", visibility="public")
    engine.state.messages.append(abort_msg)
    try:
        await engine.event_queue.put({"event": "message", "data": {"sender": "system", "content": abort_msg.content, "type": "system"}})
    except Exception:
        pass

    # 保存到数据库
    try:
        from db import save_game
        from middleware.game_log import get_traces
        from game.state import Phase, Camp
        engine.state.phase = Phase.ENDED
        engine.state.winner = None  # 无胜者
        game_id = save_game(engine.state, get_traces(), observer=engine.state.observer_mode)
        engine._last_game_id = game_id
        # 标记为 aborted
        from db import _get_conn
        conn = _get_conn()
        conn.execute("UPDATE games SET winner = 'aborted' WHERE id = ?", (game_id,))
        conn.commit()
        conn.close()

        try:
            await engine.event_queue.put({"event": "message", "data": {"sender": "system", "content": f"📁 对局已保存 (#{game_id})，标记为「终止」。", "type": "system"}})
            await engine.event_queue.put({"event": "game_over", "data": engine.state.to_full_dict()})
        except Exception:
            pass
        return web.json_response({"status": "stopped", "game_id": game_id})
    except Exception as e:
        return web.json_response({"status": "stopped", "error": f"保存失败: {str(e)[:100]}"})


def create_app() -> web.Application:
    app = web.Application()

    # CORS middleware
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    app.middlewares.append(cors_middleware)

    app.router.add_post("/game/start", handle_start)
    app.router.add_get("/game/state", handle_state)
    app.router.add_post("/game/action", handle_action)
    app.router.add_get("/game/stream", handle_stream)
    app.router.add_get("/game/traces", handle_traces)
    app.router.add_get("/game/checkpoints", handle_checkpoints)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/config", handle_get_config)
    app.router.add_post("/config", handle_save_config)
    app.router.add_post("/engine/restart", handle_restart_engine)
    app.router.add_post("/game/summary", handle_summary)
    app.router.add_post("/game/stop", handle_stop_game)

    # 历史记录 API
    app.router.add_get("/history", handle_history_list)
    app.router.add_get("/history/{game_id}", handle_history_detail)
    app.router.add_post("/history/{game_id}/ask", handle_history_ask)
    app.router.add_post("/history/{game_id}/summary", handle_history_summary)
    app.router.add_get("/history/{game_id}/perspective/{player_name}", handle_history_perspective)
    app.router.add_get("/history/{game_id}/conversations", handle_conversations_list)
    app.router.add_get("/history/{game_id}/conversations/{conv_id}", handle_conversation_messages)

    return app


def main():
    port = int(os.getenv("GAME_SERVER_PORT", "8088"))
    app = create_app()
    print(f"🐺 狼人杀后端启动: http://localhost:{port}")
    web.run_app(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
