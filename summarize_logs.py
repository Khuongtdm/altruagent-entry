"""Summarize Red Alert match logs (logs/*.jsonl) for a human or a coach agent.

    python summarize_logs.py                 # table of every match in ./logs, grouped by params file
    python summarize_logs.py path/to/logs    # another folder
    python summarize_logs.py --timeline logs/ab12cd34_Multi0.jsonl   # one match, every ~25 game seconds

Columns: result (1.0 win, 0.0 loss, 0.5 draw), seconds played, first enemy sighting, first time the
base was threatened, when the main attack launched, when the construction yard was lost, army size at
200 s and 300 s, stall% (share of snapshots after 120 s with >1500 funds and no order sent: money sitting
idle), refused orders, kills and losses (cost), final assets.
"""
import glob
import json
import os
import sys
from collections import defaultdict


def load(path):
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    pass
    return recs


def army_at(snaps, t):
    best = None
    for s in snaps:
        if s.get("gs", 0) <= t:
            best = s
    if best is None:
        return None
    ctx = best.get("ctx") or {}
    if "army" in ctx:
        return ctx["army"]
    return sum(v for k, v in (best.get("units") or {}).items() if k not in ("harv", "mcv", "e6"))


def event_time(events, name):
    for e in events:
        if e.get("name") == name:
            return e.get("gs")
    return None


def funds_of(snap):
    ctx = snap.get("ctx") or {}
    if "funds" in ctx:
        return ctx["funds"]
    eco = snap.get("eco") or {}
    return (eco.get("cash") or 0) + (eco.get("ore") or 0)


def summarize(path):
    recs = load(path)
    start = next((r for r in recs if r.get("type") == "start"), {})
    snaps = [r for r in recs if r.get("type") == "snap"]
    events = [r for r in recs if r.get("type") == "event"]
    final = next((r for r in recs if r.get("type") == "final"), None)
    problems = [r for r in recs if r.get("type") == "problem"]
    result = ((final or {}).get("result") or {}).get("your_return")
    last = snaps[-1] if snaps else {}
    mil = last.get("mil") or {}
    late = [s for s in snaps if s.get("gs", 0) > 120]
    stalled = sum(1 for s in late if funds_of(s) > 1500 and s.get("idle_reason"))
    return {
        "file": os.path.basename(path),
        "params": start.get("params_file", "?"),
        "result": result,
        "secs": last.get("gs"),
        "enemy_seen": event_time(events, "enemy_first_seen"),
        "threatened": event_time(events, "base_threatened"),
        "attack": event_time(events, "attack_launched"),
        "yard_lost": event_time(events, "lost_fact"),
        "army200": army_at(snaps, 200),
        "army300": army_at(snaps, 300),
        "stall%": round(100 * stalled / len(late)) if late else None,
        "refused": len(problems),
        "kills": mil.get("kills_cost"),
        "losses": mil.get("deaths_cost"),
        "assets": mil.get("assets_value"),
    }


def timeline(path):
    recs = load(path)
    last = -100
    for r in recs:
        if r.get("type") == "event":
            print("EVENT gs=%s %s %s" % (r.get("gs"), r.get("name"), {k: v for k, v in r.items() if k not in ("type", "gs", "name")}))
        elif r.get("type") == "snap" and r["gs"] - last >= 25:
            last = r["gs"]
            eco = r.get("eco") or {}
            print("gs=%-5s cash=%s ore=%s harv=%s power=%s/%s army=%s bld=%s idle=%r" % (
                r["gs"], eco.get("cash"), eco.get("ore"), eco.get("harvesters"), eco.get("power_provided"),
                eco.get("power_drained"), (r.get("ctx") or {}).get("army"), r.get("bld"), r.get("idle_reason")))
        elif r.get("type") == "final":
            print("FINAL", r.get("result"))


def main():
    args = sys.argv[1:]
    if args and args[0] == "--timeline":
        timeline(args[1])
        return
    folder = args[0] if args else "logs"
    rows = [summarize(p) for p in sorted(glob.glob(os.path.join(folder, "*.jsonl")))]
    if not rows:
        print("no logs found in", folder)
        return
    cols = list(rows[0])
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join(str(r[c]) for c in cols))
    groups = defaultdict(list)
    for r in rows:
        if r["result"] is not None:
            groups[r["params"]].append(r["result"])
    print("\nwin rate by params file (finished matches only; small samples are noisy):")
    for k, v in groups.items():
        wins = sum(1 for x in v if x == 1.0)
        print("  %s: %d/%d (%.0f%%)" % (k, wins, len(v), 100 * wins / len(v)))


if __name__ == "__main__":
    main()
