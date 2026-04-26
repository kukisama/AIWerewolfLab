"""本地 SQLite 数据库 —— 持久化每局游戏记录"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "game_history.db"

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """建表（幂等）"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  REAL    NOT NULL,
            ended_at    REAL,
            winner      TEXT,               -- 'good' / 'wolf' / NULL / 'aborted'
            rounds      INTEGER DEFAULT 0,
            observer    INTEGER DEFAULT 0,  -- 1=观战 0=参与
            players     TEXT    NOT NULL,    -- JSON array
            messages    TEXT    NOT NULL,    -- JSON array (完整上帝视角)
            traces      TEXT,               -- JSON array (agent 追踪日志)
            summary     TEXT                -- AI 总结 (可后补)
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id     INTEGER NOT NULL,
            perspective TEXT    NOT NULL,    -- 'god' 或玩家名
            created_at  REAL    NOT NULL,
            title       TEXT,               -- 会话标题（可选，自动生成）
            FOREIGN KEY (game_id) REFERENCES games(id)
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role            TEXT    NOT NULL,    -- 'user' / 'assistant'
            content         TEXT    NOT NULL,
            created_at      REAL    NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        );
    """)
    conn.close()


def save_game(state, traces: list[dict], observer: bool = False) -> int:
    """游戏结束时保存全量记录，返回 game id"""
    from game.state import ROLE_NAMES_CN, ROLE_CAMP, Phase

    players_json = json.dumps([
        {
            "seat": p.seat,
            "name": p.name,
            "alive": p.alive,
            "is_human": p.is_human,
            "role": p.role.value,
            "role_cn": ROLE_NAMES_CN[p.role],
            "camp": ROLE_CAMP[p.role].value,
        }
        for p in state.players
    ], ensure_ascii=False)

    messages_json = json.dumps([
        {"sender": m.sender, "content": m.content, "type": m.msg_type, "visibility": getattr(m, 'visibility', 'public')}
        for m in state.messages
    ], ensure_ascii=False)

    traces_json = json.dumps(traces, ensure_ascii=False) if traces else "[]"

    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO games (started_at, ended_at, winner, rounds, observer, players, messages, traces)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            time.time() - len(state.messages) * 0.5,  # 粗略估计开始时间
            time.time(),
            state.winner.value if state.winner else None,
            state.round_number,
            1 if observer else 0,
            players_json,
            messages_json,
            traces_json,
        ),
    )
    game_id = cur.lastrowid
    conn.commit()
    conn.close()
    return game_id


def list_games(limit: int = 50) -> list[dict]:
    """列出最近的游戏记录（不含完整消息体）"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, started_at, ended_at, winner, rounds, observer, players, summary
           FROM games ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        players = json.loads(r["players"])
        result.append({
            "id": r["id"],
            "ended_at": r["ended_at"],
            "winner": r["winner"],
            "rounds": r["rounds"],
            "observer": bool(r["observer"]),
            "player_count": len(players),
            "players_preview": ", ".join(p["name"] for p in players[:4]) + ("..." if len(players) > 4 else ""),
            "has_summary": bool(r["summary"]),
        })
    return result


def get_game(game_id: int) -> dict | None:
    """获取单局完整记录"""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "ended_at": row["ended_at"],
        "winner": row["winner"],
        "rounds": row["rounds"],
        "observer": bool(row["observer"]),
        "players": json.loads(row["players"]),
        "messages": json.loads(row["messages"]),
        "traces": json.loads(row["traces"] or "[]"),
        "summary": row["summary"],
    }


def get_game_log_text(game_id: int, perspective: str = "god") -> str | None:
    """获取单局的文本日志。perspective='god' 返回上帝视角（全部），否则按玩家名过滤。"""
    game = get_game(game_id)
    if not game:
        return None

    if perspective == "god":
        messages = game["messages"]
    else:
        # 按玩家视角过滤
        player_info = None
        for p in game["players"]:
            if p["name"] == perspective:
                player_info = p
                break
        if not player_info:
            messages = game["messages"]  # fallback to god
        else:
            role = player_info["role"]
            camp = player_info.get("camp", "good")
            messages = []
            player_dead = False

            import re as _re

            def _is_death(msg_content: str, pname: str) -> bool:
                if "死亡" in msg_content:
                    m2 = _re.match(r"^昨晚[，,]\s*(.+?)\s*死亡", msg_content)
                    if m2:
                        return pname in m2.group(1)
                    if msg_content.strip().startswith(pname) and "死亡" in msg_content:
                        return True
                if "被投出局" in msg_content or "出局" in msg_content:
                    m2 = _re.match(r"^(.+?)\s*以\s*\d+\s*票被投出局", msg_content)
                    if m2:
                        return pname == m2.group(1).strip()
                    if msg_content.strip().startswith(pname):
                        return True
                if "淘汰" in msg_content:
                    if msg_content.strip().startswith(pname):
                        return True
                return False

            for m in game["messages"]:
                content = m.get("content", "")
                vis = m.get("visibility", "public")
                if m["type"] == "system" and _is_death(content, perspective):
                    messages.append(m)
                    player_dead = True
                    continue
                # 死亡后仍可看到公开信息，但看不到私密行动
                if player_dead:
                    if m["type"] in ("system", "chat"):
                        messages.append(m)
                    elif m["type"] == "action" and vis == "public":
                        messages.append(m)
                    continue
                if m["type"] in ("system", "chat"):
                    messages.append(m)
                elif m["type"] == "action":
                    if vis == "public":
                        messages.append(m)
                    elif vis == "god":
                        # 自己的内心独白可见
                        if f"💭 {perspective}:" in content or f"💭 {perspective}：" in content:
                            messages.append(m)
                    elif vis == "wolf_team" and camp == "wolf":
                        messages.append(m)
                    elif vis == role:
                        messages.append(m)

    lines = []
    for m in messages:
        if m["type"] == "system":
            lines.append(f"[系统] {m['content']}")
        elif m["type"] == "action":
            lines.append(f"[行动] {m['content']}")
        elif m["type"] == "chat":
            lines.append(f"{m['sender']}: {m['content']}")
    return "\n".join(lines)


def update_summary(game_id: int, summary: str):
    """保存 AI 总结到数据库"""
    conn = _get_conn()
    conn.execute("UPDATE games SET summary = ? WHERE id = ?", (summary, game_id))
    conn.commit()
    conn.close()


# ═══ 复盘多轮对话 ═══

def create_conversation(game_id: int, perspective: str, title: str = "") -> int:
    """创建一个新的复盘对话会话，返回 conversation_id"""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO conversations (game_id, perspective, created_at, title) VALUES (?, ?, ?, ?)",
        (game_id, perspective, time.time(), title or None),
    )
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return cid


def list_conversations(game_id: int) -> list[dict]:
    """列出某局的所有对话会话"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT c.id, c.perspective, c.created_at, c.title,
                  (SELECT COUNT(*) FROM chat_messages WHERE conversation_id = c.id) AS msg_count
           FROM conversations c WHERE c.game_id = ? ORDER BY c.id DESC""",
        (game_id,),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "perspective": r["perspective"], "created_at": r["created_at"],
             "title": r["title"], "msg_count": r["msg_count"]} for r in rows]


def get_conversation_messages(conversation_id: int) -> list[dict]:
    """获取一个会话的所有消息"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, role, content, created_at FROM chat_messages WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]


def add_chat_message(conversation_id: int, role: str, content: str) -> int:
    """向会话追加一条消息，返回 message_id"""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (conversation_id, role, content, time.time()),
    )
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return mid


def update_chat_message(message_id: int, content: str):
    """更新消息内容（用于流式写入时持续更新）"""
    conn = _get_conn()
    conn.execute("UPDATE chat_messages SET content = ? WHERE id = ?", (content, message_id))
    conn.commit()
    conn.close()


def get_conversation_info(conversation_id: int) -> dict | None:
    """获取会话元信息"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, game_id, perspective, created_at, title FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row["id"], "game_id": row["game_id"], "perspective": row["perspective"],
            "created_at": row["created_at"], "title": row["title"]}


def update_conversation_title(conversation_id: int, title: str):
    """更新会话标题"""
    conn = _get_conn()
    conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
    conn.commit()
    conn.close()


# 启动时自动建表
init_db()
