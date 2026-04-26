import json, sqlite3

conn = sqlite3.connect("../game_history.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT id, winner, rounds FROM games ORDER BY id DESC LIMIT 1")
row = cur.fetchone()
gid = row["id"]
print(f"=== Game #{gid} | winner={row['winner']} | rounds={row['rounds']} ===")

cur.execute("SELECT players, messages FROM games WHERE id=?", (gid,))
r = cur.fetchone()
players = json.loads(r["players"])
messages = json.loads(r["messages"])

print("\n--- PLAYERS ---")
for p in players:
    print(f"  {p['name']} seat={p['seat']} role={p['role']} alive={p['alive']}")

print(f"\n--- ALL MESSAGES ({len(messages)} total) ---")
for i, m in enumerate(messages):
    vis = m.get("visibility", "public")
    tag = f"[{vis}]" if vis != "public" else ""
    print(f"  {i:3d} [{m['type']:7s}] {tag:12s} {m['content'][:140]}")

# Focus: Wolf kills, Hunter death, night summaries
print("\n--- KEY EVENTS ---")
for i, m in enumerate(messages):
    c = m["content"]
    if any(kw in c for kw in ["狼人行动", "击杀", "猎人", "死亡", "出局", "天亮", "夜晚", "开枪", "第"]):
        vis = m.get("visibility", "public")
        print(f"  {i:3d} [{m['type']:7s}] [{vis:10s}] {c[:140]}")

conn.close()
