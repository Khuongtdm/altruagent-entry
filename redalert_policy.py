"""Red Alert policy v3: economy first, defended, staged, and built for coach-driven tuning.

What it does each cycle (all plain rules, millisecond fast):
  1. Spend cash AND stored ore (spendable funds = cash + ore).
  2. Build power -> barracks -> refinery -> war factory -> more of each, one building at a time.
  3. Keep a minimum army by game time (the "defense floor") so an early rush cannot walk in.
  4. Set every production building's rally point to a staging point in front of the base and
     pull idle units near home out to it, so troops do not sit next to the barracks.
  5. If enemies come near the base, send the nearby army at them.
  6. At combat_target units, attack; keep sending reinforcements while the attack runs.
  7. Never go dead: if the construction yard is lost it keeps training and fighting.

Everything tunable lives in PARAM_SPECS and is overridden from a JSON file (params.json, or
RA_PARAMS_0 / RA_PARAMS_1 for the two seats of a self-play match). Values are clamped to the
ranges below, so a coach can edit params.json safely. Every match writes logs/<session>_<seat>.jsonl.

Plug-in points used by agent.py:  redalert_action(state)   and   log_result(state, result)

Optional LLM note (off unless LLM_BASE_URL and LLM_MODEL are set): it only writes a short note
into reasoning_summary; it never changes decisions. Never put keys in files, prompts or logs.
"""

import json
import math
import os
import threading
import time
from collections import Counter

import requests

POLICY_VERSION = "ra-3.0"

LLM_URL = os.environ.get("LLM_BASE_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_KEY = os.environ.get("LLM_API_KEY", "")
LLM_EVERY_S = 30
LOG_DIR = os.environ.get("RA_LOG_DIR", "logs")

# name: (default, min, max, what it does)
PARAM_SPECS = {
    "power_margin": (30, 0, 200, "build a power plant when power_drained + margin > power_provided"),
    "power_margin_2": (60, 0, 300, "earlier-warning margin for an extra power plant"),
    "cash_reserve": (100, 0, 1000, "funds kept unspent when deciding army and harvester purchases"),
    "first_proc_min_cash": (1300, 0, 4000, "funds needed before the first refinery"),
    "second_proc_cash": (1200, 0, 4000, "funds needed before an extra refinery"),
    "war_factory_min_cash": (1200, 0, 4000, "funds needed before a war factory"),
    "extra_power_min_cash": (400, 0, 2000, "funds needed before an extra power plant"),
    "target_powr": (4, 1, 10, "power plants to build"),
    "target_proc": (3, 1, 5, "refineries to build (extra ones wait for 2 harvesters)"),
    "target_rax": (2, 1, 5, "barracks (tent or barr) to build"),
    "target_weap": (2, 1, 4, "war factories to build"),
    "target_fix": (1, 0, 2, "service depots (unlock heavy tanks)"),
    "target_defenses": (3, 0, 10, "defensive structures (flame tower, pillbox, turret, tesla coil)"),
    "minimum_economic_harvesters": (2, 1, 4, "harvesters to get before optional spending"),
    "harvesters_target": (4, 1, 8, "harvesters to build in total"),
    "harvester_min_cash": (1000, 300, 3000, "funds needed before training a harvester"),
    "combat_target": (30, 6, 80, "living combat units that trigger the main attack"),
    "combat_batch": (3, 1, 10, "infantry per order once past the defense floor"),
    "combat_min_cash": (700, 100, 3000, "funds needed before extra infantry past the floor"),
    "surplus_cash": (2000, 500, 10000, "above this, always keep training army (never sit on money)"),
    "floor_t1": (100, 30, 400, "game second where floor_0 ends"),
    "floor_t2": (170, 60, 600, "game second where floor_1 ends"),
    "floor_t3": (230, 90, 900, "game second where floor_2 ends"),
    "floor_0": (5, 0, 40, "minimum army before floor_t1"),
    "floor_1": (10, 0, 40, "minimum army before floor_t2"),
    "floor_2": (18, 0, 60, "minimum army before floor_t3"),
    "floor_3": (26, 0, 80, "minimum army after floor_t3"),
    "use_tanks": (1, 0, 1, "1 = also train tanks from the war factory"),
    "tank_min_cash": (1500, 500, 4000, "funds needed before training tanks"),
    "tank_count": (2, 1, 5, "tanks per order"),
    "rally_distance": (14, 0, 40, "cells in front of the base toward the enemy where troops gather (0 = off)"),
    "defend_radius": (28, 5, 80, "enemy within this many cells of base triggers a defensive response"),
    "reinforce_every_s": (15, 5, 120, "seconds between reinforcement waves during an attack"),
    "reinforce_min": (8, 1, 40, "idle units at home needed to send a reinforcement wave"),
    "all_in_cooldown_s": (25, 5, 200, "seconds between main attack launches"),
    "tiebreak_window_s": (150, 0, 600, "last seconds: hold the army if ahead, push if behind (0 = off)"),
    "repair_min_cash": (900, 0, 3000, "funds needed before repairing a building"),
    "log_every_s": (5, 1, 30, "game seconds between log snapshots"),
}
DEFAULT_PARAMS = {k: v[0] for k, v in PARAM_SPECS.items()}

COMBAT_TYPES = {
    "e1", "e2", "e3", "e4", "dog", "shok", "1tnk", "2tnk", "3tnk", "4tnk", "ttnk", "ctnk", "stnk",
    "jeep", "arty", "v2rl", "yak", "mig", "heli", "hind",
}
DEFENSE_TYPES = ("ftur", "pbox", "gun", "tsla", "hbox", "agun", "sam")
DEFENSE_PRIORITY = ("ftur", "pbox", "gun", "tsla")
TANK_PRIORITY = ("3tnk", "2tnk", "1tnk", "jeep")
PRODUCTION_BUILDINGS = ("barr", "tent", "weap")


def _as_int(value, default=0):
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _id(value):
    return value.get("id") if isinstance(value, dict) else value


def _cell(obj):
    """A cell from {"x","y"} or [x, y]; None if it is neither."""
    if isinstance(obj, (list, tuple)) and len(obj) >= 2:
        x, y = obj[0], obj[1]
    elif isinstance(obj, dict):
        x, y = obj.get("x"), obj.get("y")
    else:
        return None
    if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
        return int(x), int(y)
    return None


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _counts(items):
    return dict(Counter(
        str(i.get("type", "")).lower() for i in (items or []) if isinstance(i, dict) and not i.get("wreck")
    ))


def load_params(seat):
    """DEFAULT_PARAMS overridden by a JSON file, clamped to each parameter's range.
    File choice: RA_PARAMS_0 / RA_PARAMS_1 (per seat), else RA_PARAMS, else params.json."""
    which = "1" if str(seat).endswith("1") else "0"
    path = os.environ.get("RA_PARAMS_" + which) or os.environ.get("RA_PARAMS") or "params.json"
    user = {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            user = json.load(file)
        if not isinstance(user, dict):
            raise ValueError("the params file must hold one JSON object")
    except FileNotFoundError:
        path, user = "(defaults)", {}
    except Exception as exc:
        print("params file problem, using defaults:", exc)
        path, user = "(defaults)", {}
    params = dict(DEFAULT_PARAMS)
    ignored = []
    for key, value in user.items():
        if str(key).startswith("_"):
            continue
        spec = PARAM_SPECS.get(key)
        if spec is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            ignored.append(key)
            continue
        value = max(spec[1], min(spec[2], value))
        params[key] = int(round(value)) if isinstance(spec[0], int) else float(value)
    if ignored:
        print("params ignored (unknown name or not a number):", ignored)
    return params, path


class MatchLog:
    """One JSON-lines file per seat per match, for offline analysis."""

    def __init__(self, session_id, seat, params, params_file):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = os.path.join(LOG_DIR, "%s_%s.jsonl" % (str(session_id)[:8], seat))
        self.write({"type": "start", "seat": seat, "policy": POLICY_VERSION, "params": params,
                    "params_file": params_file, "wall": time.time()})

    def write(self, record):
        try:
            with open(self.path, "a", encoding="utf-8") as file:
                file.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass

    def event(self, gs, name, **data):
        self.write({"type": "event", "gs": gs, "name": name, **data})


class Strategist:
    """Optional background LLM that writes a short note for reasoning_summary. Never decides."""

    def __init__(self):
        self.note = ""
        self._last = 0.0
        self._busy = False

    def maybe_update(self, summary):
        if not (LLM_URL and LLM_MODEL) or self._busy or time.time() - self._last < LLM_EVERY_S:
            return
        self._busy = True
        self._last = time.time()
        threading.Thread(target=self._run, args=(summary,), daemon=True).start()

    def _run(self, summary):
        try:
            headers = {"Authorization": "Bearer " + LLM_KEY} if LLM_KEY else {}
            response = requests.post(
                LLM_URL.rstrip("/") + "/chat/completions",
                headers=headers,
                json={"model": LLM_MODEL, "temperature": 0.2, "max_tokens": 80,
                      "messages": [
                          {"role": "system", "content": (
                              "You annotate a rule-based Red Alert agent. Reply with ONLY a JSON object "
                              '{"note": "max 100 chars"} describing the situation in plain words.')},
                          {"role": "user", "content": json.dumps(summary)}]},
                timeout=12,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            payload = json.loads(content[content.index("{"): content.rindex("}") + 1])
            self.note = str(payload.get("note", "")).strip()[:100]
        except Exception as exc:
            print("LLM note failed:", exc)
        finally:
            self._busy = False


class RedAlertPolicy:
    def __init__(self, session_id, seat):
        self.p, params_file = load_params(seat)
        self.log = MatchLog(session_id, seat, self.p, params_file)
        self.strategist = Strategist()
        self._sent = {}
        self._seen_problems = set()
        self._cmd_counts = Counter()
        self._last_snap = -999.0
        self._attacking = False
        self._idle = "initializing"
        self._ctx = {}
        self._flags = set()
        self._prev_owned = Counter()
        self._post_i = 0

    # ---- small helpers ----------------------------------------------------------------

    def _fresh(self, key, cooldown, game_seconds):
        """True at most once per `cooldown` game seconds for this key."""
        now = float(game_seconds or 0)
        if now - self._sent.get(key, float("-inf")) < float(cooldown):
            return False
        self._sent[key] = now
        return True

    def _once(self, gs, name, **data):
        if name not in self._flags:
            self._flags.add(name)
            self.log.event(gs, name, **data)

    def _floor(self, gs):
        p = self.p
        if gs < p["floor_t1"]:
            return p["floor_0"]
        if gs < p["floor_t2"]:
            return p["floor_1"]
        if gs < p["floor_t3"]:
            return p["floor_2"]
        return p["floor_3"]

    def _staging(self, state, base, legal):
        """A point in front of the base toward the enemy, kept inside the playable map."""
        enemy = _cell(legal.get("enemy_base"))
        distance = self.p["rally_distance"]
        if not base or not enemy or distance <= 0:
            return None
        gap = _dist(base, enemy)
        if gap < 1:
            return None
        k = min(distance, gap * 0.4) / gap
        x, y = base[0] + (enemy[0] - base[0]) * k, base[1] + (enemy[1] - base[1]) * k
        bounds = (state.get("map") or {}).get("bounds")
        if isinstance(bounds, list) and len(bounds) == 4 and all(isinstance(v, (int, float)) for v in bounds):
            bx, by, bw, bh = bounds
            x = max(bx + 1, min(bx + bw - 2, x))
            y = max(by + 1, min(by + bh - 2, y))
        return [int(round(x)), int(round(y))]

    def _summary(self, state):
        obs, t = state["observation"], state.get("time") or {}
        return {"game_seconds": t.get("game_seconds"), "faction": obs.get("faction"),
                "economy": obs.get("economy"), "my_buildings": _counts(obs.get("buildings")),
                "my_units": _counts(obs.get("units")), "enemy_units": _counts(obs.get("visible_enemies")),
                "phase": "attack" if self._attacking else "build"}

    # ---- building plan ----------------------------------------------------------------

    def _defense_count(self, owned):
        return sum(owned[d] for d in DEFENSE_TYPES)

    def _defense_item(self, avail):
        for item in DEFENSE_PRIORITY:
            if item in avail:
                return item
        return None

    def _min_funds(self, item, owned):
        p = self.p
        if item == "proc":
            return p["first_proc_min_cash"] if owned["proc"] == 0 else p["second_proc_cash"]
        if item == "weap":
            return p["war_factory_min_cash"]
        if item == "powr":
            return 300 if owned["powr"] == 0 else p["extra_power_min_cash"]
        if item in ("barr", "tent"):
            return 650
        if item == "fix":
            return 800
        return 700  # defenses

    def _next_building(self, funds, eco, avail, owned, rax, harvesters, post_attack):
        p = self.p
        drained, provided = _as_int(eco.get("power_drained")), _as_int(eco.get("power_provided"))
        defense = self._defense_item(avail)
        core = [
            ("powr", owned["powr"] == 0),
            (rax, owned[rax] == 0),
            ("proc", owned["proc"] == 0),
            ("powr", drained + p["power_margin"] > provided),
            ("weap", owned["weap"] == 0),
            ("powr", drained + p["power_margin_2"] > provided),
        ]
        extra = [
            ("proc", owned["proc"] < p["target_proc"] and (owned["proc"] == 1 or harvesters >= 2)),
            ("powr", owned["powr"] < p["target_powr"]),
            (defense, bool(defense) and self._defense_count(owned) < min(1, p["target_defenses"])),
            ("weap", owned["weap"] < p["target_weap"]),
            (rax, owned[rax] < p["target_rax"]),
            ("fix", owned["fix"] < p["target_fix"]),
            (defense, bool(defense) and self._defense_count(owned) < p["target_defenses"]),
        ]
        for item, wanted in core + extra:
            if wanted and item and item in avail and funds >= self._min_funds(item, owned):
                return item
        if post_attack:  # keep investing surplus once the main attack is out
            cycle = ["powr", "proc", "weap", rax, defense]
            for offset in range(len(cycle)):
                item = cycle[(self._post_i + offset) % len(cycle)]
                if item and item in avail and funds >= self._min_funds(item, owned):
                    if item == "proc" and harvesters < 2:
                        continue
                    self._post_i = (self._post_i + offset + 1) % len(cycle)
                    return item
        return None

    def _assets_complete(self, avail, owned, rax):
        p = self.p
        wants = [("proc", owned["proc"], p["target_proc"]), ("powr", owned["powr"], p["target_powr"]),
                 ("weap", owned["weap"], p["target_weap"]), (rax, owned[rax], p["target_rax"]),
                 ("fix", owned["fix"], p["target_fix"])]
        for item, have, target in wants:
            if item in avail and have < target:
                return False
        return not (self._defense_item(avail) and self._defense_count(owned) < p["target_defenses"])

    # ---- main decision ------------------------------------------------------------------

    def decide(self, state):
        orders = self._decide(state)
        self._cmd_counts.update(order.get("cmd", "unknown") for order in orders)
        try:
            self._snapshot(state, orders)
        except Exception as exc:  # logging must never break play
            print("log problem:", exc)
        return orders

    def _decide(self, state):
        obs = state.get("observation")
        if not obs or state.get("phase") != "playing":
            self._idle = "not playing yet"
            return []
        p = self.p
        legal = state.get("legal_actions") or {}
        eco = obs.get("economy") or {}
        mil = obs.get("military") or {}
        timing = state.get("time") or {}
        gs = float(timing.get("game_seconds") or 0)
        remaining = timing.get("remaining_seconds")
        funds = _as_int(eco.get("cash")) + _as_int(eco.get("ore"))  # ore is spendable too
        avail = {str(a).lower() for a in (obs.get("available_production") or [])}
        buildings = [b for b in (obs.get("buildings") or []) if isinstance(b, dict) and not b.get("wreck")]
        units = [u for u in (obs.get("units") or []) if isinstance(u, dict) and not u.get("wreck")]
        owned = Counter(str(b.get("type", "")).lower() for b in buildings)
        queued = {}
        for q in obs.get("production") or []:
            if isinstance(q, dict):
                queued.setdefault(str(q.get("queue", "")).lower(), []).append(q)
        orders = []

        for problem in state.get("recent_order_problems") or []:
            key = (problem.get("batch"), problem.get("index"))
            if key not in self._seen_problems:
                self._seen_problems.add(key)
                print("order problem:", problem)
                self.log.write({"type": "problem", "gs": gs, "problem": problem})

        self.strategist.maybe_update(self._summary(state))

        # Events worth knowing for a coach (each building/fact loss is logged once per change).
        for kind in ("fact", "proc", "weap", "barr", "tent"):
            if self._prev_owned[kind] > owned[kind]:
                self.log.event(gs, "lost_" + kind, before=self._prev_owned[kind], after=owned[kind])
        self._prev_owned = owned

        # 0. No construction yard: deploy an MCV if we have one; otherwise keep fighting.
        if owned["fact"] == 0:
            deploy = legal.get("deploy") or []
            if deploy:
                if self._fresh("deploy", 5, gs):
                    return [{"cmd": "deploy", "units": [_id(deploy[0])]}]
                self._idle = "deploy sent; waiting for the yard"
                return []
            if not any(owned[t] for t in PRODUCTION_BUILDINGS):
                self._idle = "no construction yard and no production buildings"
                return []

        base = None
        for b in buildings:
            if str(b.get("type", "")).lower() == "fact":
                base = _cell(b)
        base = base or _cell(obs.get("base_center"))
        if base is None and buildings:
            base = _cell(buildings[0])
        rax = "barr" if (owned["barr"] or ("barr" in avail and not owned["tent"])) else "tent"
        staging = self._staging(state, base, legal)

        # 1. Place finished buildings right away.
        for q in obs.get("production") or []:
            if isinstance(q, dict) and q.get("ready") and str(q.get("queue", "")).lower() in ("building", "defense"):
                item = str(q.get("item", "")).lower()
                if item and self._fresh("place:" + item, 3, gs):
                    orders.append({"cmd": "place", "item": item})

        # 2. Army picture and the nearest threat to the base.
        army = [u for u in units if str(u.get("type", "")).lower() in COMBAT_TYPES and u.get("id") is not None]
        n_army = len(army)
        enemies = [e for e in (obs.get("visible_enemies") or []) if isinstance(e, dict) and not e.get("wreck")]
        nearest, near_d = None, None
        if enemies:
            self._once(gs, "enemy_first_seen", types=_counts(enemies))
        if base:
            for enemy in enemies:
                cell = _cell(enemy)
                if cell and (near_d is None or _dist(base, cell) < near_d):
                    nearest, near_d = cell, _dist(base, cell)
        threat = near_d is not None and near_d <= p["defend_radius"]

        if threat:
            self._once(gs, "base_threatened", distance=round(near_d, 1), army=n_army, enemies=len(enemies))
            if army and self._fresh("defend", 3, gs):
                near_ids = [u["id"] for u in army
                            if _cell(u) and _dist(base, _cell(u)) <= p["defend_radius"] * 1.5]
                if near_ids:
                    orders.append({"cmd": "attack_move", "units": near_ids[:50], "to": list(nearest)})
        elif staging and army and base and self._fresh("stage", 6, gs):
            home = max(15, p["rally_distance"] * 2)
            lagging = [u["id"] for u in army if u.get("idle") and _cell(u)
                       and _dist(base, _cell(u)) <= home and _dist(_cell(u), staging) > 5]
            if lagging:
                orders.append({"cmd": "attack_move", "units": lagging[:50], "to": staging})

        # 3. Rally points: new troops walk to the staging point instead of idling at the barracks.
        if staging:
            for b in buildings:
                if str(b.get("type", "")).lower() in PRODUCTION_BUILDINGS and b.get("id") is not None:
                    if _cell(b.get("rally")) != (staging[0], staging[1]) and self._fresh("rally:%s" % b["id"], 15, gs):
                        orders.append({"cmd": "rally", "building": b["id"], "to": staging})

        # 4. Economy bookkeeping.
        harvesters = eco.get("harvesters", 0)
        harvesters = len(harvesters) if isinstance(harvesters, list) else _as_int(harvesters)
        harvesters = max(harvesters, sum(1 for u in units if str(u.get("type", "")).lower() == "harv"))
        min_h = int(p["minimum_economic_harvesters"])
        idle_h = [u["id"] for u in units if str(u.get("type", "")).lower() == "harv"
                  and u.get("idle") and u.get("id") is not None]
        if idle_h and self._fresh("harvest", 4, gs):
            orders.append({"cmd": "harvest", "units": idle_h[:50]})

        has_building_q = bool(queued.get("building") or queued.get("defense"))
        has_inf_q = bool(queued.get("infantry"))
        has_veh_q = bool(queued.get("vehicle"))
        pending_proc = owned["proc"] < 2 and any(
            str(q.get("item", "")).lower() == "proc" for q in queued.get("building", []))
        reserve = int(p["cash_reserve"])
        building_sent = False
        harvester_sent = False

        # 5. One building at a time.
        if owned["fact"] and not has_building_q:
            item = self._next_building(funds, eco, avail, owned, rax, harvesters, self._attacking)
            if item and self._fresh("build:" + item, 3, gs):
                orders.append({"cmd": "build", "item": item})
                building_sent = True

        # 6. Harvesters: get out of the one-harvester trap before optional spending.
        want_h = max(min_h, int(p["harvesters_target"]))
        if (owned["weap"] and owned["proc"] and not has_veh_q and not pending_proc and "harv" in avail
                and harvesters < want_h and funds >= p["harvester_min_cash"] + reserve
                and (harvesters < min_h or not building_sent)
                and self._fresh("train:harv", 5, gs)):
            orders.append({"cmd": "train", "item": "harv", "count": 1})
            harvester_sent = True

        # 7. Army: defense floor first, then everything once assets are done or money piles up.
        floor = self._floor(gs)
        need_defense = n_army < floor
        assets_ok = self._assets_complete(avail, owned, rax)
        harvesters_ok = harvesters >= want_h
        surplus = funds >= p["surplus_cash"]
        cap = int(p["combat_target"]) * (2 if self._attacking else 1)
        unit = "e1" if "e1" in avail else ("e3" if "e3" in avail else None)
        if (unit and owned[rax] and not has_inf_q and n_army < cap and not harvester_sent
                and (need_defense or surplus or (assets_ok and harvesters_ok and not building_sent))
                and funds >= (150 if need_defense else p["combat_min_cash"] + reserve)
                and self._fresh("train:army", 4, gs)):
            batch = 5 if need_defense else int(p["combat_batch"])
            goal = floor if need_defense else cap
            orders.append({"cmd": "train", "item": unit, "count": max(1, min(batch, goal - n_army))})

        tank = next((t for t in TANK_PRIORITY if t in avail), None)
        if (p["use_tanks"] and tank and owned["weap"] and not has_veh_q and not harvester_sent
                and funds >= p["tank_min_cash"] and (n_army >= min(floor, 8) or surplus)
                and n_army < cap and self._fresh("train:tank", 5, gs)):
            orders.append({"cmd": "train", "item": tank, "count": int(p["tank_count"])})

        # 8. Rebuild an MCV if the construction yard died and a war factory survives.
        if (owned["fact"] == 0 and owned["weap"] and "mcv" in avail and not has_veh_q
                and funds >= 2600 and self._fresh("train:mcv", 20, gs)):
            orders.append({"cmd": "train", "item": "mcv", "count": 1})

        # 9. Main attack and reinforcements, with the time-limit tiebreak in mind.
        attack_now = legal.get("attack_now")
        target = _cell(attack_now.get("to")) if isinstance(attack_now, dict) else None
        target = target or _cell(legal.get("enemy_base"))
        window = p["tiebreak_window_s"]
        late = bool(window) and remaining is not None and remaining < window
        balance = _as_int(mil.get("kills_cost")) - _as_int(mil.get("deaths_cost"))
        hold = late and balance > 0
        need = int(p["combat_target"])
        if late and balance < 0:
            need = max(6, need // 2)
        if self._attacking and n_army < max(4, int(p["combat_target"]) // 4):
            self._attacking = False
            self.log.event(gs, "attack_wiped", army=n_army)
        ids = [u["id"] for u in army]
        if target and not threat and not hold and not self._attacking and n_army >= need \
                and self._fresh("all_in", p["all_in_cooldown_s"], gs):
            for i in range(0, min(len(ids), 100), 50):
                orders.append({"cmd": "attack_move", "units": ids[i:i + 50], "to": list(target)})
            self._attacking = True
            self.log.event(gs, "attack_launched", army=n_army, to=list(target))
        elif target and self._attacking and base and self._fresh("reinforce", p["reinforce_every_s"], gs):
            home = max(15, p["rally_distance"] * 2)
            fresh_units = [u["id"] for u in army if u.get("idle") and _cell(u) and _dist(base, _cell(u)) <= home]
            if len(fresh_units) >= p["reinforce_min"]:
                orders.append({"cmd": "attack_move", "units": fresh_units[:50], "to": list(target)})

        # 10. Repair one damaged building when there is money to spare.
        if funds >= p["repair_min_cash"] + reserve and not building_sent and not harvester_sent:
            repairable = legal.get("repair") or []
            if repairable and self._fresh("repair", 10, gs):
                orders.append({"cmd": "repair", "building": _id(repairable[0])})

        self._ctx = {"funds": funds, "army": n_army, "floor": floor, "staging": staging,
                     "enemy_dist": None if near_d is None else round(near_d, 1),
                     "phase": "attack" if self._attacking else "build", "assets_ok": assets_ok}
        if orders:
            self._idle = ""
        elif has_building_q:
            self._idle = "building queue active"
        elif has_inf_q and n_army < cap:
            self._idle = "infantry queue active"
        elif funds < reserve:
            self._idle = "funds below reserve (cash+ore=%d)" % funds
        elif harvesters < min_h:
            self._idle = "saving for a harvester"
        elif n_army >= cap:
            self._idle = "army at cap; waiting to launch or reinforce"
        elif not (assets_ok and harvesters_ok) and not surplus:
            self._idle = "assets or harvesters incomplete; waiting for funds or prerequisites"
        else:
            self._idle = "nothing eligible this cycle"
        return orders[:20]

    # ---- logging ---------------------------------------------------------------------------

    def _snapshot(self, state, orders):
        obs = state.get("observation")
        if not obs or state.get("phase") != "playing":
            return
        gs = _as_int((state.get("time") or {}).get("game_seconds"))
        if gs - self._last_snap < p_int(self.p["log_every_s"]):
            return
        self._last_snap = gs
        mil = obs.get("military") or {}
        keys = ("kills_cost", "deaths_cost", "assets_value", "army_value", "units_killed", "units_lost",
                "buildings_killed", "buildings_lost")
        self.log.write({
            "type": "snap", "gs": gs, "remaining": (state.get("time") or {}).get("remaining_seconds"),
            "eco": obs.get("economy"), "mil": {k: mil.get(k) for k in keys},
            "bld": _counts(obs.get("buildings")), "units": _counts(obs.get("units")),
            "enemy_units": _counts(obs.get("visible_enemies")), "enemy_bld": _counts(obs.get("visible_enemy_buildings")),
            "queues": [{k: q.get(k) for k in ("queue", "item", "progress", "ready")}
                       for q in (obs.get("production") or []) if isinstance(q, dict)],
            "ctx": self._ctx, "idle_reason": self._idle if not orders else "",
            "sent": dict(self._cmd_counts), "orders": orders,
        })

    def note(self):
        c = self._ctx
        base = "%s; army %s/%s; funds %s" % (c.get("phase", "build"), c.get("army", 0), self.p["combat_target"], c.get("funds", 0))
        return ((base + "; " + self.strategist.note) if self.strategist.note else base)[:400]


def p_int(value, default=5):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_policies = {}


def _policy(state):
    key = (state.get("session_id"), state.get("your_seat"))
    if key not in _policies:
        _policies[key] = RedAlertPolicy(*key)
    return _policies[key]


def redalert_action(state):
    """Return play_action arguments for agent.py, or None to wait."""
    policy = _policy(state)
    orders = policy.decide(state)
    if not orders:
        return None
    return {"session_id": state["session_id"], "state_version": state["state_version"],
            "action": {"type": "orders", "orders": orders[:20]}, "reasoning_summary": policy.note()}


def log_result(state, result):
    """Write the final result into this seat's match log."""
    policy = _policies.get((state.get("session_id"), state.get("your_seat")))
    if policy:
        policy.log.write({"type": "final", "result": result})
