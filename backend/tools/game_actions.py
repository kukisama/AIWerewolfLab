"""游戏动作工具 —— Agent 通过 Tool Calling 执行"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from game.state import GameState


def make_tools(state: GameState):
    """返回 tool 函数的字典，供 Agent Framework 注册"""

    def kill_player(target_seat: int) -> str:
        """狼人选择击杀目标。target_seat: 目标玩家座位号"""
        p = state.get_player(target_seat)
        if not p or not p.alive:
            return f"失败: 座位 {target_seat} 无效或已死亡"
        state.night_action.wolf_target = target_seat
        return f"已选择击杀 {target_seat} 号 {p.name}"

    def check_identity(target_seat: int) -> str:
        """预言家查验一名玩家的身份。target_seat: 目标玩家座位号"""
        from game.state import Role, ROLE_CAMP, Camp
        p = state.get_player(target_seat)
        if not p or not p.alive:
            return f"失败: 座位 {target_seat} 无效或已死亡"
        state.night_action.seer_target = target_seat
        result = "狼人" if ROLE_CAMP[p.role] == Camp.WOLF else "好人"
        state.night_action.seer_result = result
        return f"{target_seat} 号 {p.name} 的身份是: {result}"

    def use_antidote(target_seat: int) -> str:
        """女巫使用解药救人。target_seat: 要救的座位号"""
        witch = None
        from game.state import Role
        for p in state.players:
            if p.role == Role.WITCH and p.alive:
                witch = p
                break
        if not witch:
            return "失败: 女巫不存在或已死亡"
        if not witch.has_antidote:
            return "失败: 解药已用过"
        if state.night_action.wolf_target != target_seat:
            return "失败: 该玩家今晚没有被杀"
        witch.has_antidote = False
        state.night_action.witch_save = True
        return f"已使用解药救了 {target_seat} 号"

    def use_poison(target_seat: int) -> str:
        """女巫使用毒药杀人。target_seat: 目标座位号"""
        witch = None
        from game.state import Role
        for p in state.players:
            if p.role == Role.WITCH and p.alive:
                witch = p
                break
        if not witch:
            return "失败: 女巫不存在或已死亡"
        if not witch.has_poison:
            return "失败: 毒药已用过"
        target = state.get_player(target_seat)
        if not target or not target.alive:
            return f"失败: 座位 {target_seat} 无效或已死亡"
        witch.has_poison = False
        state.night_action.witch_poison_target = target_seat
        return f"已使用毒药毒杀 {target_seat} 号 {target.name}"

    def protect_player(target_seat: int) -> str:
        """守卫保护一名玩家。target_seat: 目标座位号（不能连续两晚保护同一人）"""
        guard = None
        from game.state import Role
        for p in state.players:
            if p.role == Role.GUARD and p.alive:
                guard = p
                break
        if not guard:
            return "失败: 守卫不存在或已死亡"
        if guard.last_protected == target_seat:
            return f"失败: 不能连续两晚保护同一个人 ({target_seat} 号)"
        target = state.get_player(target_seat)
        if not target or not target.alive:
            return f"失败: 座位 {target_seat} 无效或已死亡"
        guard.last_protected = target_seat
        state.night_action.guard_target = target_seat
        return f"已保护 {target_seat} 号 {target.name}"

    def shoot_player(target_seat: int) -> str:
        """猎人死亡时开枪带走一人。target_seat: 目标座位号"""
        target = state.get_player(target_seat)
        if not target or not target.alive:
            return f"失败: 座位 {target_seat} 无效或已死亡"
        target.alive = False
        return f"猎人开枪带走了 {target_seat} 号 {target.name}"

    def vote(voter_seat: int, target_seat: int) -> str:
        """投票处决一名玩家。voter_seat: 投票者座位号, target_seat: 被投者座位号"""
        voter = state.get_player(voter_seat)
        target = state.get_player(target_seat)
        if not voter or not voter.alive:
            return "失败: 投票者无效"
        if not target or not target.alive:
            return f"失败: 目标 {target_seat} 无效或已死亡"
        state.votes[voter_seat] = target_seat
        return f"{voter.name} 投票给 {target.name}"

    def get_alive_players() -> str:
        """查看当前存活的玩家列表"""
        alive = state.alive_players()
        lines = [f"  {p.seat}号 {p.name}" for p in alive]
        return f"存活玩家 ({len(alive)} 人):\n" + "\n".join(lines)

    def skip_action() -> str:
        """跳过当前操作（不做任何事）"""
        return "已跳过"

    return {
        "kill_player": kill_player,
        "check_identity": check_identity,
        "use_antidote": use_antidote,
        "use_poison": use_poison,
        "protect_player": protect_player,
        "shoot_player": shoot_player,
        "vote": vote,
        "get_alive_players": get_alive_players,
        "skip_action": skip_action,
    }
