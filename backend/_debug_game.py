"""临时调试脚本 - 查看最新对局"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_game, list_games

games = list_games(limit=3)
if not games:
    print("No games found")
    sys.exit()

latest_id = games[0]["id"]
print(f"Latest game: #{latest_id}")
g = get_game(latest_id)

print("=== Players ===")
for p in g["players"]:
    print(f"  Seat {p['seat']}: {p['name']} ({p['role_cn']}) camp={p['camp']} alive={p['alive']}")

print("\n=== Key Messages (actions + night) ===")
for i, m in enumerate(g["messages"]):
    c = m.get("content", "")
    t = m["type"]
    if t == "action" or "死亡" in c or "击杀" in c or "投票" in c or "闭眼" in c or "睁眼" in c or "保护" in c or "查验" in c or "解药" in c or "毒药" in c:
        print(f"  [{i:3d}] [{t:6s}] {c[:150]}")
