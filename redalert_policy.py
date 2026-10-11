"""Economy-first Red Alert policy for the AltruAgent sample runner.

The policy keeps the original public interface:
    redalert_action(state)
    log_result(state, result)

Strategy:
- Build and protect the economy before spending on an army.
- Keep up to two basic infantry at the base and one infantry scout.
- Prioritize harvesters and a second refinery over combat vehicles/defenses.
- Issue a roaming attack-move to the scout only (never the whole army).
- Keep decisions rule-based and fast; the optional LLM can add a note but cannot
  override the economy-first policy.

The scout uses the engine-provided legal_actions['attack_now'] template, narrowed to
one unit. The two guard infantry stay near the base by not receiving roaming orders.
"""

import json
import os
import threading
import time
from collections import Counter
from copy import deepcopy

import requests

LLM_URL = os.environ.get("LLM_BASE_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_KEY = os.environ.get("LLM_API_KEY", "")
LLM_EVERY_S = 25
LOG_DIR = os.environ.get("RA_LOG_DIR", "logs")

DEFAULT_PARAMS = {
    # Economy / power
    "power_margin": 30,
    "power_margin_2": 60,
    "second_proc_cash": 2400,
    "cash_reserve": 500,
    "first_proc_min_cash": 1800,
    "war_factory_min_cash": 1800,
    "extra_power_min_cash": 900,
    # Harvesters
    "harvesters_target": 2,
    "harvester_min_cash": 1800,
    # Very small security force
    "guard_count": 2,
    "scout_count": 1,
    "guard_min_cash": 900,
    "scout_min_cash": 1000,
    "scout_cooldown_s": 45,
    # Repairs/logging
    "repair_min_cash": 1000,
    "log_every_s": 5,
}

SYSTEM = (
    "You are a note-only strategist for an economy-first Red Alert RTS agent. "
    "The rule policy strictly prioritizes power, refineries, harvesters, and cash reserves. "
    "It keeps two basic infantry near the base and at most one infantry scout. "
    "Do not recommend mass production, attacks by the main force, or optional defenses. "
    'Reply with ONLY one JSON object: {"note": "max 100 chars"}. '
    "Use the game summary to identify one useful economic priority or risk."
)


def _counts(items):
    return dict(Counter(
        str(i.get("type", "")).lower()
        for i in (items or [])
        if isinstance(i, dict) and not i.get("wreck")
    ))


def _id(value):
    return value.get("id") if isinstance(value, dict) else value


def _clamp_int(value, low, high):
    return max(low, min(high, int(value)))


def _as_int(value, default=0):
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def load_params(seat):
    """Load per-seat overrides, then RA_PARAMS, then params.json."""
    which = "1" if str(seat).endswith("1") else "0"
    path = (
        os.environ.get("RA_PARAMS_" + which)
        or os.environ.get("RA_PARAMS")
        or "params.json"
    )
    user = {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            user = json.load(file)
    except FileNotFoundError:
        path = "(defaults)"
    except Exception as exc:
        print("params file problem, using defaults:", exc)
        path = "(defaults)"

    params = dict(DEFAULT_PARAMS)
    for key, value in (user or {}).items():
        if key in params and isinstance(value, (int, float)) and not isinstance(value, bool):
            params[key] = value
    return params, path


class MatchLog:
    """One JSON-lines file per seat per match."""

    def __init__(self, session_id, seat, params, params_file):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = os.path.join(LOG_DIR, "%s_%s.jsonl" % (str(session_id)[:8], seat))
        self.write({
            "type": "start",
            "seat": seat,
            "params": params,
            "params_file": params_file,
            "wall": time.time(),
        })

    def write(self, record):
        try:
            with open(self.path, "a", encoding="utf-8") as file:
                file.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass


class Strategist:
    """Optional background LLM that can annotate but never change the strategy."""

    def __init__(self):
        self.directive = {
            "stance": "econ",
            "note": "Economy first; preserve cash and harvesters.",
        }
        self._last = 0.0
        self._busy = False
        self._lock = threading.Lock()

    def maybe_update(self, summary):
        if not (LLM_URL and LLM_MODEL) or self._busy:
            return
        if time.time() - self._last < LLM_EVERY_S:
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
                json={
                    "model": LLM_MODEL,
                    "temperature": 0.1,
                    "max_tokens": 100,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": json.dumps(summary)},
                    ],
                },
                timeout=12,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            start, end = content.find("{"), content.rfind("}")
            if start < 0 or end < start:
                raise ValueError("LLM response did not contain a JSON object")
            payload = json.loads(content[start:end + 1])
            note = str(payload.get("note", "")).strip()[:100]
            with self._lock:
                # Never accept an LLM stance or unit count: economy-first is invariant.
                self.directive = {
                    "stance": "econ",
                    "note": note or "Economy first; preserve cash and harvesters.",
                }
        except Exception as exc:
            print("LLM strategist failed:", exc)
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
        self._scouted_ids = set()
        self._roles = {}
        self._warned_no_unit_list = False

    def _fresh(self, key, cooldown, game_seconds):
        """Cooldowns use game time, not wall-clock time."""
        now = float(game_seconds or 0)
        last = self._sent.get(key, float("-inf"))
        if now - last < float(cooldown):
            return False
        self._sent[key] = now
        return True

    def _summary(self, state):
        observation = state["observation"]
        timing = state.get("time") or {}
        economy = observation.get("economy") or {}
        return {
            "game_seconds": timing.get("game_seconds"),
            "remaining_seconds": timing.get("remaining_seconds"),
            "faction": observation.get("faction"),
            "enemy_faction": observation.get("enemy_faction"),
            "economy": economy,
            "my_buildings": _counts(observation.get("buildings")),
            "my_units": _counts(observation.get("units")),
            "visible_enemy_units": _counts(observation.get("visible_enemies")),
            "visible_enemy_buildings": _counts(observation.get("visible_enemy_buildings")),
            "explored_percent": observation.get("explored_percent"),
            "policy": "economy-first; 2 guards + 1 scout; harvesters before army",
        }

    def _assign_roles(self, units):
        """Persist roles: fill missing guard slots before assigning a scout."""
        infantry = [
            unit for unit in (units or [])
            if isinstance(unit, dict)
            and str(unit.get("type", "")).lower() in ("e1", "e3")
            and not unit.get("wreck")
            and unit.get("id") is not None
        ]
        live_ids = {str(unit["id"]) for unit in infantry}
        # Remove roles for units no longer present so replacement units can fill slots.
        for unit_id in list(self._roles):
            if unit_id not in live_ids:
                self._roles.pop(unit_id, None)

        guard_target = max(0, _as_int(self.p["guard_count"]))
        scout_target = max(0, _as_int(self.p["scout_count"]))
        for unit in infantry:
            unit_key = str(unit["id"])
            if unit_key in self._roles:
                continue
            live_roles = list(self._roles.values())
            if live_roles.count("guard") < guard_target:
                self._roles[unit_key] = "guard"
            elif live_roles.count("scout") < scout_target:
                self._roles[unit_key] = "scout"
            else:
                self._roles[unit_key] = "reserve"
        return infantry

    def _next_building(self, eco, available, owned, rax, harvester_count):
        """Return the next economy-first structure, but never force unaffordable builds."""
        drained = _as_int(eco.get("power_drained"))
        provided = _as_int(eco.get("power_provided"))
        cash = _as_int(eco.get("cash"))
        short = drained + _as_int(self.p["power_margin"]) > provided
        short_soon = drained + _as_int(self.p["power_margin_2"]) > provided

        plan = [
            ("powr", owned["powr"] == 0),
            (rax, owned["barr"] == 0 and owned["tent"] == 0),
            ("proc", owned["proc"] == 0),
            ("powr", short),
            ("weap", owned["weap"] == 0),
            ("powr", short_soon),
            (
                "proc",
                owned["proc"] < 2
                and owned["proc"] > 0
                and harvester_count >= 1
                and cash >= _as_int(self.p["second_proc_cash"]),
            ),
        ]
        for item, condition in plan:
            if condition and item in available:
                return item
        return None

    def _minimum_build_cash(self, item, owned):
        if item == "proc":
            if owned["proc"] == 0:
                return _as_int(self.p["first_proc_min_cash"])
            return _as_int(self.p["second_proc_cash"])
        if item == "weap":
            return _as_int(self.p["war_factory_min_cash"])
        if item == "powr" and owned["powr"] > 0:
            return _as_int(self.p["extra_power_min_cash"])
        if item in ("barr", "tent"):
            return 800
        if item == "powr":
            return 700
        return _as_int(self.p["cash_reserve"])

    def decide(self, state):
        orders = self._decide(state)
        self._cmd_counts.update(order.get("cmd", "unknown") for order in orders)
        try:
            self._snapshot(state, orders)
        except Exception as exc:
            print("log problem:", exc)
        return orders

    def _snapshot(self, state, orders):
        observation = state.get("observation")
        if not observation or state.get("phase") != "playing":
            return
        game_seconds = _as_int((state.get("time") or {}).get("game_seconds"))
        if game_seconds - self._last_snap < _as_int(self.p["log_every_s"], 5):
            return
        self._last_snap = game_seconds
        legal = state.get("legal_actions") or {}
        units = observation.get("units") or []
        military = observation.get("military") or {}
        keys = (
            "kills_cost", "deaths_cost", "assets_value", "army_value",
            "units_killed", "units_lost", "buildings_killed", "buildings_lost",
        )
        self.log.write({
            "type": "snap",
            "gs": game_seconds,
            "remaining": (state.get("time") or {}).get("remaining_seconds"),
            "eco": observation.get("economy"),
            "mil": {key: military.get(key) for key in keys},
            "bld": _counts(observation.get("buildings")),
            "units": _counts(units),
            "enemy_units": _counts(observation.get("visible_enemies")),
            "enemy_bld": _counts(observation.get("visible_enemy_buildings")),
            "available_production": observation.get("available_production"),
            "production_queues": observation.get("production"),
            "harvester_details": [
                {key: unit.get(key) for key in ("id", "type", "idle", "wreck", "health", "position") if key in unit}
                for unit in units
                if isinstance(unit, dict) and str(unit.get("type", "")).lower() == "harv"
            ],
            "combat_count": len(legal.get("combat_units") or []),
            "attack_now_available": bool(legal.get("attack_now")),
            "legal_action_keys": [key for key, value in legal.items() if value],
            "roles": dict(self._roles),
            "stance": "econ",
            "note": self.strategist.directive.get("note", ""),
            "sent": dict(self._cmd_counts),
            "orders_this_cycle": orders,
        })

    def _decide(self, state):
        observation = state.get("observation")
        if not observation or state.get("phase") != "playing":
            return []

        legal = state.get("legal_actions") or {}
        eco = observation.get("economy") or {}
        available = {str(item).lower() for item in (observation.get("available_production") or [])}
        production = observation.get("production") or []
        buildings = observation.get("buildings") or []
        units = observation.get("units") or []
        game_seconds = float((state.get("time") or {}).get("game_seconds") or 0)
        cash = _as_int(eco.get("cash"))

        # Exclude wrecks: destroyed buildings must not block rebuilding.
        owned = Counter(
            str(building.get("type", "")).lower()
            for building in buildings
            if isinstance(building, dict) and not building.get("wreck")
        )
        queued = {}
        for queue_item in production:
            if isinstance(queue_item, dict):
                queued.setdefault(str(queue_item.get("queue", "")).lower(), []).append(queue_item)

        for problem in state.get("recent_order_problems") or []:
            problem_key = (problem.get("batch"), problem.get("index"))
            if problem_key not in self._seen_problems:
                self._seen_problems.add(problem_key)
                print("order problem:", problem)
                self.log.write({"type": "problem", "problem": problem})

        self.strategist.maybe_update(self._summary(state))
        orders = []

        # 1. Deploy the MCV as soon as the legal action exists.
        if owned["fact"] == 0:
            deploy = legal.get("deploy") or []
            if deploy and self._fresh("deploy", 5, game_seconds):
                orders.append({"cmd": "deploy", "units": [_id(deploy[0])]})
            return orders

        # 2. Place completed buildings immediately; this does not spend cash.
        for queue_item in production:
            if not isinstance(queue_item, dict):
                continue
            if queue_item.get("ready") and str(queue_item.get("queue", "")).lower() in ("building", "defense"):
                item = str(queue_item.get("item", "")).lower()
                if item and self._fresh("place:" + item, 3, game_seconds):
                    orders.append({"cmd": "place", "item": item})

        # 3. Keep a stable barracks/tent choice once one exists.
        if owned["barr"]:
            rax = "barr"
        elif owned["tent"]:
            rax = "tent"
        else:
            rax = "barr" if "barr" in available else "tent"

        # Work out harvester count from both the economy field and owned-unit list.
        eco_harvesters = eco.get("harvesters", 0)
        if isinstance(eco_harvesters, list):
            eco_harvesters = len(eco_harvesters)
        unit_harvesters = sum(
            1 for unit in units
            if isinstance(unit, dict)
            and str(unit.get("type", "")).lower() == "harv"
            and not unit.get("wreck")
        )
        harvester_count = max(_as_int(eco_harvesters), unit_harvesters)

        # 4. Economy-first construction. Only queue one new structure at a time.
        building_order_sent = False
        if not queued.get("building") and cash >= _as_int(self.p["cash_reserve"]):
            item = self._next_building(eco, available, owned, rax, harvester_count)
            if item:
                min_cash = self._minimum_build_cash(item, owned)
                if cash >= min_cash and self._fresh("build:" + item, 4, game_seconds):
                    orders.append({"cmd": "build", "item": item})
                    building_order_sent = True

        # 5. Repair only when cash is healthy; avoid repairs consuming the economy reserve.
        # Repairs are deliberately limited to one per decision cycle.
        if cash >= _as_int(self.p["repair_min_cash"]):
            repairables = legal.get("repair") or []
            if repairables:
                building = repairables[0]
                building_id = _id(building)
                if self._fresh("repair:%s" % building_id, 12, game_seconds):
                    orders.append({"cmd": "repair", "building": building_id})

        # 6. Idle harvesters go back to work. This action is free and supports the economy.
        idle_harvesters = [
            unit["id"] for unit in units
            if isinstance(unit, dict)
            and str(unit.get("type", "")).lower() == "harv"
            and unit.get("idle")
            and not unit.get("wreck")
            and unit.get("id") is not None
        ]
        if idle_harvesters and self._fresh("harvest", 5, game_seconds):
            orders.append({"cmd": "harvest", "units": idle_harvesters[:50]})

        # Never stack a new production order on top of a new building order in the
        # same batch: construction gets first claim on cash this decision cycle.
        if not building_order_sent:
            # 7. Restore the second harvester before training any combat vehicles.
            # This policy intentionally does not mass-produce tanks or other vehicles.
            if (
                owned["weap"] > 0
                and owned["proc"] > 0
                and harvester_count < _as_int(self.p["harvesters_target"])
                and not queued.get("vehicle")
                and "harv" in available
                and cash >= _as_int(self.p["harvester_min_cash"])
                and self._fresh("train:harvester", 8, game_seconds)
            ):
                orders.append({"cmd": "train", "item": "harv", "count": 1})
            else:
                # 8. A tiny security force: exactly two base guards and one scout.
                # Training waits until a refinery and at least one harvester exist.
                infantry = self._assign_roles(units)
                live_infantry_count = len(infantry)
                target_infantry = max(0, _as_int(self.p["guard_count"])) + max(0, _as_int(self.p["scout_count"]))
                if (
                    owned[rax] > 0
                    and owned["proc"] > 0
                    and harvester_count >= 1
                    and live_infantry_count < target_infantry
                    and not queued.get("infantry")
                    and "e1" in available
                ):
                    guards_remaining = max(0, _as_int(self.p["guard_count"])) - sum(
                        1 for unit in infantry if self._roles.get(str(unit.get("id"))) == "guard"
                    )
                    if guards_remaining > 0:
                        count = min(guards_remaining, target_infantry - live_infantry_count)
                        minimum_cash = _as_int(self.p["guard_min_cash"])
                    else:
                        count = min(1, target_infantry - live_infantry_count)
                        minimum_cash = _as_int(self.p["scout_min_cash"])
                    if count > 0 and cash >= minimum_cash and self._fresh("train:guard_or_scout", 4, game_seconds):
                        orders.append({"cmd": "train", "item": "e1", "count": count})

        # 9. Send only the designated scout on a roaming attack-move. Guards remain at
        # the base. Restrict the engine-provided template to the scout's ID so this
        # cannot accidentally send the entire army, as the old policy did.
        infantry = self._assign_roles(units)
        scout = next(
            (unit for unit in infantry if self._roles.get(str(unit.get("id"))) == "scout"),
            None,
        )
        attack_template = legal.get("attack_now")
        if scout and attack_template and not scout.get("wreck"):
            scout_id = scout.get("id")
            scout_key = str(scout_id)
            # If idle status is present, wait for it to be idle. If missing, one initial
            # roam order is safe because the order is restricted to this single unit.
            can_roam = scout.get("idle", True)
            if (
                can_roam
                and scout_key not in self._scouted_ids
                and isinstance(attack_template, dict)
                and isinstance(attack_template.get("units"), list)
                and self._fresh("scout:" + scout_key, _as_int(self.p["scout_cooldown_s"]), game_seconds)
            ):
                roam_order = deepcopy(attack_template)
                roam_order["units"] = [scout_id]
                orders.append(roam_order)
                self._scouted_ids.add(scout_key)
            elif (
                scout_key not in self._scouted_ids
                and isinstance(attack_template, dict)
                and not isinstance(attack_template.get("units"), list)
                and not self._warned_no_unit_list
            ):
                print("Economy policy: attack_now has no units list; scout roaming disabled to avoid moving guards.")
                self._warned_no_unit_list = True

        return orders[:20]

    def note(self):
        note = str(self.strategist.directive.get("note", "Economy first."))
        return ("econ: " + note)[:400]


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
    return {
        "session_id": state["session_id"],
        "state_version": state["state_version"],
        "action": {"type": "orders", "orders": orders[:20]},
        "reasoning_summary": policy.note(),
    }


def log_result(state, result):
    """Write the final result into this seat's match log."""
    policy = _policies.get((state.get("session_id"), state.get("your_seat")))
    if policy:
        policy.log.write({"type": "final", "result": result})
