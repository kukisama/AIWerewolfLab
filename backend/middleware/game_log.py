"""日志中间件 —— 记录每个 Agent 的交互"""
from __future__ import annotations
import time
import json

# 简单的全局日志存储
_trace_log: list[dict] = []


def reset_traces():
    global _trace_log
    _trace_log = []


def get_traces() -> list[dict]:
    return list(_trace_log)


def log_agent_action(agent_name: str, action_type: str, detail: str):
    """记录一条 Agent 动作日志"""
    entry = {
        "timestamp": time.time(),
        "agent": agent_name,
        "type": action_type,       # "llm_call", "tool_call", "speech", "vote"
        "detail": detail,
    }
    _trace_log.append(entry)


def log_llm_call(agent_name: str, prompt_preview: str, response_preview: str, tokens: int = 0):
    """记录一次 LLM 调用"""
    entry = {
        "timestamp": time.time(),
        "agent": agent_name,
        "type": "llm_call",
        "detail": json.dumps({
            "prompt": prompt_preview[:800],
            "response": response_preview[:500],
            "tokens_approx": tokens,
        }, ensure_ascii=False),
    }
    _trace_log.append(entry)
