"""游戏状态管理"""
from __future__ import annotations
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Role(str, Enum):
    WEREWOLF = "werewolf"
    SEER = "seer"
    WITCH = "witch"
    GUARD = "guard"
    HUNTER = "hunter"
    VILLAGER = "villager"


class Phase(str, Enum):
    NIGHT = "night"
    DAY = "day"
    VOTE = "vote"
    ENDED = "ended"


class Camp(str, Enum):
    WOLF = "wolf"
    GOOD = "good"


ROLE_CAMP = {
    Role.WEREWOLF: Camp.WOLF,
    Role.SEER: Camp.GOOD,
    Role.WITCH: Camp.GOOD,
    Role.GUARD: Camp.GOOD,
    Role.HUNTER: Camp.GOOD,
    Role.VILLAGER: Camp.GOOD,
}

ROLE_NAMES_CN = {
    Role.WEREWOLF: "狼人",
    Role.SEER: "预言家",
    Role.WITCH: "女巫",
    Role.GUARD: "守卫",
    Role.HUNTER: "猎人",
    Role.VILLAGER: "村民",
}

# 中文名池
AI_NAMES = ["张三", "李四", "王五", "赵六", "孙七", "周八", "吴九", "郑十", "褚九"]


@dataclass
class Player:
    seat: int
    name: str
    role: Role
    is_human: bool = False
    alive: bool = True
    # 女巫专用
    has_antidote: bool = True
    has_poison: bool = True
    # 守卫专用
    last_protected: Optional[int] = None  # 上一晚保护的座位号


@dataclass
class NightAction:
    """一个夜晚中发生的所有动作"""
    wolf_target: Optional[int] = None       # 狼人杀的座位号
    guard_target: Optional[int] = None      # 守卫保护的座位号
    seer_target: Optional[int] = None       # 预言家查验的座位号
    seer_result: Optional[str] = None       # 查验结果
    witch_save: bool = False                # 女巫是否救人
    witch_poison_target: Optional[int] = None  # 女巫毒的座位号


@dataclass
class GameMessage:
    """游戏消息"""
    sender: str          # 发送者名字 or "system"
    content: str         # 消息内容
    msg_type: str = "chat"   # chat / system / action / vote
    visibility: str = "public"  # public / god / wolf_team / seer / witch / guard / hunter


@dataclass
class GameState:
    players: list[Player] = field(default_factory=list)
    phase: Phase = Phase.NIGHT
    round_number: int = 1
    messages: list[GameMessage] = field(default_factory=list)
    night_action: NightAction = field(default_factory=NightAction)
    votes: dict[int, int] = field(default_factory=dict)  # voter_seat -> target_seat
    winner: Optional[Camp] = None
    # checkpoint 历史
    checkpoints: list[dict] = field(default_factory=list)

    observer_mode: bool = False

    def init_game(self, human_name: str = "玩家", observer: bool = False) -> None:
        """初始化一局游戏，随机分配角色
        observer=True: 旁观模式，9 个全 AI，人类只看不参与
        observer=False: 参与模式，8 AI + 1 人类玩家
        """
        self.observer_mode = observer
        roles = [
            Role.WEREWOLF, Role.WEREWOLF,
            Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER,
            Role.VILLAGER, Role.VILLAGER,
            Role.VILLAGER,
        ]
        random.shuffle(roles)

        if observer:
            # 旁观模式：9 个全部是 AI，名字固定顺序
            names = AI_NAMES[:9]
            for seat in range(1, 10):
                self.players.append(Player(seat=seat, name=names[seat - 1], role=roles[seat - 1]))
        else:
            # 参与模式：8 AI + 1 人类，名字固定顺序
            ai_names = AI_NAMES[:8]
            human_seat = random.randint(1, 9)
            name_idx = 0
            for seat in range(1, 10):
                role = roles[seat - 1]
                if seat == human_seat:
                    self.players.append(Player(seat=seat, name=human_name, role=role, is_human=True))
                else:
                    self.players.append(Player(seat=seat, name=ai_names[name_idx], role=role))
                    name_idx += 1

    def get_player(self, seat: int) -> Optional[Player]:
        for p in self.players:
            if p.seat == seat:
                return p
        return None

    def get_player_by_name(self, name: str) -> Optional[Player]:
        for p in self.players:
            if p.name == name:
                return p
        return None

    def alive_players(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def alive_wolves(self) -> list[Player]:
        return [p for p in self.players if p.alive and p.role == Role.WEREWOLF]

    def alive_good(self) -> list[Player]:
        return [p for p in self.players if p.alive and ROLE_CAMP[p.role] == Camp.GOOD]

    def check_winner(self) -> Optional[Camp]:
        """检查胜负"""
        wolves = self.alive_wolves()
        goods = self.alive_good()
        if len(wolves) == 0:
            self.winner = Camp.GOOD
            return Camp.GOOD
        if len(wolves) >= len(goods):
            self.winner = Camp.WOLF
            return Camp.WOLF
        return None

    def resolve_night(self) -> list[str]:
        """结算夜晚，返回公告消息列表"""
        na = self.night_action
        announcements = []
        died_tonight: list[int] = []

        # 判定: 狼人击杀 vs 守卫保护 vs 女巫解救
        if na.wolf_target is not None:
            saved = False
            if na.guard_target == na.wolf_target:
                saved = True
            if na.witch_save:
                saved = True
            if not saved:
                died_tonight.append(na.wolf_target)

        # 女巫毒人
        if na.witch_poison_target is not None:
            if na.witch_poison_target not in died_tonight:
                died_tonight.append(na.witch_poison_target)

        # 执行死亡
        for seat in died_tonight:
            p = self.get_player(seat)
            if p:
                p.alive = False

        if died_tonight:
            names = [self.get_player(s).name for s in died_tonight if self.get_player(s)]
            announcements.append(f"昨晚，{'、'.join(names)} 死亡。")
        else:
            announcements.append("昨晚是平安夜，没有人死亡。")

        # 重置夜晚动作
        self.night_action = NightAction()
        return announcements

    def resolve_vote(self) -> tuple[Optional[int], str]:
        """结算投票，返回 (被投出的座位号, 公告)"""
        if not self.votes:
            return None, "没有人投票，无人出局。"

        from collections import Counter
        counts = Counter(self.votes.values())
        max_votes = max(counts.values())
        top = [seat for seat, cnt in counts.items() if cnt == max_votes]

        if len(top) > 1:
            # 平票 - 简化处理：无人出局
            names = [self.get_player(s).name for s in top if self.get_player(s)]
            return None, f"{'、'.join(names)} 票数相同（各 {max_votes} 票），无人出局。"

        target_seat = top[0]
        target = self.get_player(target_seat)
        if target:
            target.alive = False
            vote_detail = ", ".join(
                f"{self.get_player(v).name}→{self.get_player(t).name}"
                for v, t in self.votes.items()
                if self.get_player(v) and self.get_player(t)
            )
            return target_seat, f"{target.name} 以 {max_votes} 票被投出局。[{vote_detail}]"

        return None, "投票异常。"

    def save_checkpoint(self, label: str) -> dict:
        """保存当前状态快照"""
        import copy
        snapshot = {
            "label": label,
            "round": self.round_number,
            "phase": self.phase.value,
            "alive": [p.seat for p in self.alive_players()],
            "messages_count": len(self.messages),
        }
        self.checkpoints.append(snapshot)
        return snapshot

    def to_dict(self) -> dict:
        """序列化为前端可用的 JSON"""
        return {
            "phase": self.phase.value,
            "round": self.round_number,
            "players": [
                {
                    "seat": p.seat,
                    "name": p.name,
                    "alive": p.alive,
                    "is_human": p.is_human,
                    "role": p.role.value if (p.is_human or self.observer_mode) else None,
                    "role_cn": ROLE_NAMES_CN[p.role] if (p.is_human or self.observer_mode) else None,
                    "camp": ROLE_CAMP[p.role].value if self.observer_mode else None,
                }
                for p in self.players
            ],
            "messages": [
                {"sender": m.sender, "content": m.content, "type": m.msg_type, "visibility": m.visibility}
                for m in self.messages[-50:]  # 只发最近50条
            ],
            "winner": self.winner.value if self.winner else None,
            "checkpoints": self.checkpoints,
        }

    def to_full_dict(self) -> dict:
        """游戏结束后的完整信息（上帝视角）"""
        return {
            **self.to_dict(),
            "players": [
                {
                    "seat": p.seat,
                    "name": p.name,
                    "alive": p.alive,
                    "is_human": p.is_human,
                    "role": p.role.value,
                    "role_cn": ROLE_NAMES_CN[p.role],
                    "camp": ROLE_CAMP[p.role].value,
                }
                for p in self.players
            ],
        }
