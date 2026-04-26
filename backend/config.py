"""AI 配置管理 —— 持久化 endpoint/model/key 配置，支持双引擎切换和 per-agent 模型分配"""
from __future__ import annotations
import json
import os
from pathlib import Path

# 配置文件路径（与 backend 同级）
CONFIG_PATH = Path(__file__).resolve().parent.parent / "ai_config.json"

# 可配置的 Agent 角色列表
AGENT_ROLES = ["werewolf", "seer", "witch", "guard", "hunter", "villager"]

ROLE_LABELS = {
    "werewolf": "狼人",
    "seer": "预言家",
    "witch": "女巫",
    "guard": "守卫",
    "hunter": "猎人",
    "villager": "村民",
}

# 引擎模式
ENGINE_MODES = {
    "manual": "🔧 手搓版 (REST API 直连)",
    "sdk": "🧠 SDK 版 (Semantic Kernel)",
}

# API 类型
API_TYPES = {
    "responses": "Responses API (主流/多模态)",
    "azure": "Azure Chat Completions",
    "openai": "OpenAI 兼容 Chat Completions",
}

DEFAULT_CONFIG = {
    "engine_mode": "manual",       # "manual" | "sdk"
    "api_type": "responses",       # "responses" | "azure" | "openai"  (全局默认)
    "api_version": "2025-03-01-preview",
    "temperature": 0.8,
    "max_tokens": 300,
    "speak_delay_ms": 300,
    # 模型仓库 —— 一组一组录入，每项独立完整
    "model_pool": [
        # { "name": "GPT-4o", "endpoint": "...", "model": "gpt-4o", "api_key": "...", "api_type": "responses", "api_version": "2025-03-01-preview" }
    ],
    # Per-Agent 绑定池中第几号模型（从 0 开始），-1 或缺省 = 用第 0 号
    "agent_model_index": {}
    # agent_model_index 格式: { "werewolf": 0, "seer": 1, ... }
}


def load_config() -> dict:
    """加载配置文件，不存在则返回默认"""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # 补全缺失字段
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    """保存配置到 JSON 文件"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_pool_item(player_name: str | None = None) -> dict:
    """获取玩家绑定的完整模型池条目（含 api_type、api_version）。
    按 player_name 在 agent_model_index 中查找索引；
    找不到则降级到 pool[0]。
    """
    cfg = load_config()
    pool = cfg.get("model_pool", [])
    indices = cfg.get("agent_model_index", {})
    if player_name:
        idx = indices.get(player_name, 0)
        if isinstance(idx, int) and 0 <= idx < len(pool):
            return pool[idx]
    if pool:
        return pool[0]
    # 降级
    return {
        "name": "default",
        "endpoint": cfg.get("default_endpoint", ""),
        "model": cfg.get("default_model", "gpt-4o-mini"),
        "api_key": cfg.get("default_api_key", ""),
        "api_type": cfg.get("api_type", "responses"),
        "api_version": cfg.get("api_version", "2025-03-01-preview"),
    }
