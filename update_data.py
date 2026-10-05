#!/usr/bin/env python3
"""Rebuild data.js (hero-vs-hero and hero-with-hero win rates) from OpenDota.

Aggregates recent ranked All Pick public matches through the OpenDota explorer.
Usage: python3 update_data.py [days]   (default 14)
"""
import json, sys, time, urllib.parse, urllib.request, datetime

API = "https://api.opendota.com/api"
CHUNK = 400_000          # match ids per query; larger chunks hit the 15s query timeout
IDS_PER_DAY = 1_450_000  # rough match id growth per day
FILTER = "lobby_type = 7 AND game_mode = 22"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dota-draft-helper"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def sql(q):
    for attempt in range(4):
        try:
            d = get(API + "/explorer?sql=" + urllib.parse.quote(q))
            if d.get("rows") is not None:
                return d["rows"]
        except Exception as e:
            print("  retry:", e, flush=True)
        time.sleep(5 * (attempt + 1))
    return None

VS = """SELECT r, e, count(*) g, sum(CASE WHEN radiant_win THEN 1 ELSE 0 END) w
FROM public_matches, unnest(radiant_team) r, unnest(dire_team) e
WHERE match_id > {lo} AND match_id <= {hi} AND {f} AND r > 0 AND e > 0 GROUP BY r, e"""

SYN = """SELECT a, b, count(*) g, sum(CASE WHEN t.win THEN 1 ELSE 0 END) w
FROM public_matches
CROSS JOIN LATERAL (VALUES (radiant_team, radiant_win), (dire_team, NOT radiant_win)) t(team, win),
unnest(t.team) a, unnest(t.team) b
WHERE match_id > {lo} AND match_id <= {hi} AND {f} AND a > 0 AND a < b GROUP BY a, b"""

def meta():
    """Hero basics with per-rank win rates, and an item id -> [key, name, cost] map."""
    heroes = []
    for h in get(API + "/heroStats"):
        heroes.append({
            "id": h["id"], "key": h["name"].replace("npc_dota_hero_", ""), "name": h["localized_name"],
            "attr": h["primary_attr"], "atk": h["attack_type"], "roles": h["roles"],
            "b": [[h.get(f"{i}_pick") or 0, h.get(f"{i}_win") or 0] for i in range(1, 9)],
            "pro": [h.get("pro_pick") or 0, h.get("pro_win") or 0, h.get("pro_ban") or 0],
        })
    items = {v["id"]: [k, v.get("dname") or k, v.get("cost") or 0]
             for k, v in get(API + "/constants/items").items()}
    return heroes, items

def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    top = sql("SELECT match_id FROM public_matches ORDER BY match_id DESC LIMIT 1")[0]["match_id"]
    lo_all = top - days * IDS_PER_DAY
    vs, syn = {}, {}   # "a_b" -> [games, wins of a]
    matches = 0
    hi = top
    while hi > lo_all:
        lo = max(hi - CHUNK, lo_all)
        for name, tpl, acc in (("vs", VS, vs), ("syn", SYN, syn)):
            rows = sql(tpl.format(lo=lo, hi=hi, f=FILTER))
            if rows is None:
                print(f"  skipped {name} chunk {lo}-{hi}", flush=True)
                continue
            for row in rows:
                g, w = int(row["g"]), int(row["w"])
                if name == "vs":
                    a, b = row["r"], row["e"]
                    for k, ww in ((f"{a}_{b}", w), (f"{b}_{a}", g - w)):
                        c = acc.setdefault(k, [0, 0]); c[0] += g; c[1] += ww
                    matches += g
                else:
                    c = acc.setdefault(f"{row['a']}_{row['b']}", [0, 0]); c[0] += g; c[1] += w
            time.sleep(1.1)
        print(f"chunk down to {lo}: {matches // 25:,} matches so far", flush=True)
        hi = lo

    ids = sorted({int(k.split("_")[0]) for k in vs})
    def matrix(acc, symmetric):
        out = {}
        for a in ids:
            row = []
            for b in ids:
                k = f"{min(a, b)}_{max(a, b)}" if symmetric else f"{a}_{b}"
                g, w = acc.get(k, [0, 0])
                row += [g, w]
            out[a] = row
        return out
    heroes, items = meta()
    data = {
        "heroes": heroes,
        "items": items,
        "updated": datetime.date.today().isoformat(),
        "days": days,
        "matches": matches // 25,
        "source": "OpenDota public matches, ranked All Pick",
        "ids": ids,
        "vs": matrix(vs, False),    # vs[a] = [games, wins_of_a, ...] against each hero in ids order
        "syn": matrix(syn, True),   # syn[a] = [games, wins, ...] when on the same team
    }
    with open("data.js", "w") as f:
        f.write("window.DRAFT_DATA = " + json.dumps(data, separators=(",", ":")) + ";\n")
    print(f"wrote data.js: {len(ids)} heroes, {data['matches']:,} matches")

if __name__ == "__main__":
    main()
