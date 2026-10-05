#!/usr/bin/env python3
"""Rebuild data.js (hero-vs-hero and hero-with-hero win rates) from OpenDota.

Aggregates recent ranked All Pick public matches through the OpenDota explorer.
Usage: python3 update_data.py          (every match of the current patch; time grows with patch age)
       python3 update_data.py <days>   (the last <days> days instead)
       python3 update_data.py meta     (refresh only heroes, items and item builds)
"""
import json, sys, time, urllib.parse, urllib.request, datetime

API = "https://api.opendota.com/api"
CHUNK = 400_000          # match ids per query; larger chunks hit the 15s query timeout
IDS_PER_DAY = 1_450_000  # rough match id growth per day
FILTER = "lobby_type = 7 AND game_mode = 22"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dota-draft-helper"})
    with urllib.request.urlopen(req, timeout=30) as r:
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
    """Hero basics with per-rank win rates, and an item id -> [key, name, cost, assembled, components] map."""
    heroes = []
    for h in get(API + "/heroStats"):
        heroes.append({
            "id": h["id"], "key": h["name"].replace("npc_dota_hero_", ""), "name": h["localized_name"],
            "attr": h["primary_attr"], "atk": h["attack_type"], "roles": h["roles"],
            "b": [[h.get(f"{i}_pick") or 0, h.get(f"{i}_win") or 0] for i in range(1, 9)],
            "pro": [h.get("pro_pick") or 0, h.get("pro_win") or 0, h.get("pro_ban") or 0],
        })
    # last field: 1 for assembled items (plus a few standalone ones), 0 for basic components
    standalone = {"blink", "ghost", "gem", "aghanims_shard"}
    items = {v["id"]: [k, v.get("dname") or k, v.get("cost") or 0, int(bool(v.get("components")) or k in standalone),
                        v.get("components") or []]
             for k, v in get(API + "/constants/items").items()}
    return heroes, items

def builds(heroes):
    """Per hero: item key -> [games, wins, average purchase minute], from OpenDota item timings."""
    out = {}
    for n, h in enumerate(heroes):
        acc = {}
        rows = None
        for attempt in range(4):
            try:
                rows = get(f"{API}/scenarios/itemTimings?hero_id={h['id']}")
                break
            except Exception as e:
                print("  retry:", e, flush=True); time.sleep(20 * (attempt + 1))
        if rows is None:
            sys.exit(f"could not load item timings for {h['name']}; data.js left unchanged")
        for r in rows:
            g, w = int(r["games"]), int(r["wins"])
            c = acc.setdefault(r["item"], [0, 0, 0]); c[0] += g; c[1] += w; c[2] += g * r["time"]
        out[h["id"]] = {k: [g, w, round(t / g / 60)] for k, (g, w, t) in acc.items() if g >= 5}
        if n % 20 == 0: print(f"item builds: {n}/{len(heroes)}", flush=True)
        time.sleep(1.1)
    return out

ROLE_CTE = """WITH p AS (SELECT pm.hero_id, ((pm.player_slot < 128) = m.radiant_win) AS win,
rank() OVER (PARTITION BY pm.match_id, pm.player_slot < 128 ORDER BY pm.net_worth DESC) AS r,
ARRAY[pm.item_0,pm.item_1,pm.item_2,pm.item_3,pm.item_4,pm.item_5] AS items
FROM player_matches pm JOIN matches m USING (match_id)
WHERE m.start_time > extract(epoch from now() - interval '{a} days')
AND m.start_time <= extract(epoch from now() - interval '{b} days')) """

def role_builds(items, days=180, step=60):
    """Per hero, final-inventory items in pro matches split by farm priority.

    Support = 4th or 5th in net worth on the team. Returns
    {hero_id: {"c": [games, wins, {item_key: [games, wins]}], "s": [...]}}.
    """
    out = {}
    for b in range(0, days, step):
        cte = ROLE_CTE.format(a=b + step, b=b)
        counts = sql(cte + "SELECT hero_id, (r >= 4) AS sup, count(*) g, sum(CASE WHEN win THEN 1 ELSE 0 END) w FROM p GROUP BY 1, 2")
        time.sleep(1.1)
        rows = sql(cte + "SELECT hero_id, (r >= 4) AS sup, item, count(*) g, sum(CASE WHEN win THEN 1 ELSE 0 END) w "
                         "FROM p, unnest(items) item WHERE item > 0 GROUP BY 1, 2, 3")
        time.sleep(1.1)
        if counts is None or rows is None:
            sys.exit("could not load pro role builds; data.js left unchanged")
        for r in counts:
            c = out.setdefault(r["hero_id"], {"c": [0, 0, {}], "s": [0, 0, {}]})["s" if r["sup"] else "c"]
            c[0] += int(r["g"]); c[1] += int(r["w"])
        for r in rows:
            key = items.get(r["item"], items.get(str(r["item"]), [None]))[0]
            if not key or key.startswith("recipe"):
                continue
            c = out[r["hero_id"]]["s" if r["sup"] else "c"][2].setdefault(key, [0, 0])
            c[0] += int(r["g"]); c[1] += int(r["w"])
        print(f"pro role builds: {b + step}/{days} days", flush=True)
    for h in out.values():
        for side in ("c", "s"):
            h[side][2] = {k: v for k, v in h[side][2].items() if v[0] >= 3}
    return out

def write(data):
    with open("data.js", "w") as f:
        f.write("window.DRAFT_DATA = " + json.dumps(data, separators=(",", ":")) + ";\n")

def refresh_meta():
    """Refresh heroes, items and item builds, keeping the existing matchup matrices."""
    raw = open("data.js").read()
    data = json.loads(raw[raw.index("{"):].rstrip().rstrip(";"))
    data["heroes"], data["items"] = meta()
    data["roles"] = role_builds(data["items"])
    data["builds"] = builds(data["heroes"])
    write(data)
    print("refreshed heroes, items and item builds in data.js")

def current_patch():
    """Name and start (unix time) of the latest patch, from dota2.com.

    The listed timestamp is the morning of release day, so matches count from the next midnight UTC.
    """
    p = get("https://www.dota2.com/datafeed/patchnoteslist?language=english")["patches"][-1]
    return p["patch_name"], (p["patch_timestamp"] // 86400 + 1) * 86400

def main():
    if sys.argv[1:] == ["meta"]:
        return refresh_meta()
    if len(sys.argv) > 1:
        days, patch = int(sys.argv[1]), None
        since = int(time.time()) - days * 86400
    else:
        patch, since = current_patch()
        days = max(1, round((time.time() - since) / 86400))
        print(f"patch {patch}: {days} days of matches", flush=True)
    top = sql("SELECT match_id FROM public_matches ORDER BY match_id DESC LIMIT 1")[0]["match_id"]
    lo_all = top - int((days + 1.5) * IDS_PER_DAY)   # generous id range; start_time does the exact cut
    flt = f"{FILTER} AND start_time >= {since}"
    vs, syn = {}, {}   # "a_b" -> [games, wins of a]
    matches = 0
    hi = top
    while hi > lo_all:
        lo = max(hi - CHUNK, lo_all)
        for name, tpl, acc in (("vs", VS, vs), ("syn", SYN, syn)):
            rows = sql(tpl.format(lo=lo, hi=hi, f=flt))
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
    try:   # keep the previous hero/item data so a failed refresh below cannot lose the matchups
        raw = open("data.js").read()
        data = json.loads(raw[raw.index("{"):].rstrip().rstrip(";"))
    except (OSError, ValueError):
        data = {}
    data.update({
        "updated": datetime.date.today().isoformat(),
        "days": days,
        "patch": patch,
        "matches": matches // 25,
        "source": "OpenDota public matches, ranked All Pick",
        "ids": ids,
        "vs": matrix(vs, False),    # vs[a] = [games, wins_of_a, ...] against each hero in ids order
        "syn": matrix(syn, True),   # syn[a] = [games, wins, ...] when on the same team
    })
    write(data)
    print(f"wrote data.js: {len(ids)} heroes, {data['matches']:,} matches", flush=True)
    refresh_meta()

if __name__ == "__main__":
    main()
