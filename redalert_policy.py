"""Red Alert policy: fast rules decide every order; an optional LLM only sets strategy.

Why this split: Red Alert is real time (25 ticks/s), so the main loop must never wait
on a model. The rules below build the base, train units and send attack waves in
milliseconds. A background thread asks an LLM every ~25 s for a high-level directive
(stance and wave size). If the LLM is missing, slow or wrong, the rules keep playing.

Plug into the docs' sample agent.py: add `from redalert_policy import redalert_action`
and make the Red Alert branch of choose_action() `return redalert_action(state)`.

Optional LLM (any OpenAI-compatible endpoint, e.g. Ollama or OpenAI):
    LLM_BASE_URL=http://localhost:11434/v1   LLM_MODEL=qwen3:8b   LLM_API_KEY=...
Never put your Official Agent Key in any prompt or log.
"""

import json
import os
import threading
import time
from collections import Counter

import requests

LLM_URL = os.environ.get("LLM_BASE_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_KEY = os.environ.get("LLM_API_KEY", "")
LLM_EVERY_S = 25

SYSTEM = (
    "You are the strategist for a Red Alert RTS agent. Rule code already handles building, "
    "training and issuing orders. Every ~25 s you receive a JSON summary of the game. "
    'Reply with ONLY one JSON object: {"stance": "econ"|"balanced"|"attack"|"defend", '
    '"wave_size": integer 6-30, "note": "max 100 chars"}. '
    "attack = send armies sooner (when ahead or the enemy looks weak). "
    "defend = hold and build up (when behind or under heavy attack). "
    "econ = delay attacks to grow. At the time limit the winner is decided by enemy value "
    "destroyed minus own value lost, then by assets, so do not waste units."
)


def _counts(items):
    return dict(Counter(i.get("type") for i in (items or []) if not i.get("wreck")))


def _id(x):
    return x.get("id") if isinstance(x, dict) else x


class Strategist:
    """Background LLM that only sets a directive; it never blocks the main loop."""

    def __init__(self):
        self.directive = {"stance": "balanced", "wave_size": 10, "note": ""}
        self._last = 0.0
        self._busy = False

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
            r = requests.post(
                LLM_URL.rstrip("/") + "/chat/completions",
                headers=headers,
                json={
                    "model": LLM_MODEL,
                    "temperature": 0.2,
                    "max_tokens": 150,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": json.dumps(summary)},
                    ],
                },
                timeout=15,
            )
            text = r.json()["choices"][0]["message"]["content"]
            d = json.loads(text[text.index("{"): text.rindex("}") + 1])
            if d.get("stance") in ("econ", "balanced", "attack", "defend"):
                self.directive["stance"] = d["stance"]
            self.directive["wave_size"] = max(6, min(30, int(d.get("wave_size", 10))))
            self.directive["note"] = str(d.get("note", ""))[:100]
        except Exception as e:  # any failure: keep the previous directive
            print("LLM strategist failed:", e)
        finally:
            self._busy = False


class RedAlertPolicy:
    def __init__(self):
        self.strategist = Strategist()
        self._sent = {}
        self._last_debug = 0.0

    def _fresh(self, key, secs):
        """True at most once per `secs` for this key (stops duplicate queuing while
        the engine's verdict is still on its way)."""
        now = time.time()
        if now - self._sent.get(key, 0.0) < secs:
            return False
        self._sent[key] = now
        return True

    def _summary(self, state):
        obs, t = state["observation"], state.get("time") or {}
        return {
            "game_seconds": t.get("game_seconds"),
            "remaining_seconds": t.get("remaining_seconds"),
            "faction": obs.get("faction"),
            "enemy_faction": obs.get("enemy_faction"),
            "economy": obs.get("economy"),
            "military": obs.get("military"),
            "my_buildings": _counts(obs.get("buildings")),
            "my_units": _counts(obs.get("units")),
            "visible_enemy_units": _counts(obs.get("visible_enemies")),
            "visible_enemy_buildings": _counts(obs.get("visible_enemy_buildings")),
            "explored_percent": obs.get("explored_percent"),
            "current_directive": self.strategist.directive,
        }

    def _next_building(self, eco, avail, owned, rax):
        short = eco["power_drained"] + 30 > eco["power_provided"]
        plan = [
            ("powr", owned["powr"] == 0),
            (rax, owned[rax] == 0),
            ("proc", owned["proc"] == 0),
            ("powr", short),
            ("weap", owned["weap"] == 0),
            ("powr", eco["power_drained"] + 60 > eco["power_provided"]),
            ("proc", owned["proc"] < 2 and eco["cash"] > 1800),
        ]
        for item, cond in plan:
            if cond and item in avail:
                return item
        return None

    def decide(self, state):
        obs = state.get("observation")
        if not obs or state.get("phase") != "playing":
            return []
        legal = state.get("legal_actions") or {}
        eco = obs["economy"]
        avail = {str(a).lower() for a in obs.get("available_production", [])}
        owned = Counter(str(b["type"]).lower() for b in obs.get("buildings", []))
        queued = {}
        for q in obs.get("production", []):
            queued.setdefault(str(q.get("queue")).lower(), []).append(q)
        orders = []

        # Debug: show why orders were refused (every 10 s at most).
        problems = state.get("recent_order_problems")
        if problems and time.time() - self._last_debug > 10:
            self._last_debug = time.time()
            print("order problems:", problems[-3:])

        self.strategist.maybe_update(self._summary(state))
        directive = self.strategist.directive
        stance = directive["stance"]
        played = (state.get("time") or {}).get("game_seconds") or 0
        if stance == "defend" and played > 600:
            stance = "balanced"  # never turtle forever

        # 1. Deploy the MCV.
        if not owned["fact"]:
            if legal.get("deploy") and self._fresh("deploy", 5):
                orders.append({"cmd": "deploy", "units": [_id(legal["deploy"][0])]})
            return orders

        # 2. Place finished buildings right away.
        for q in obs.get("production", []):
            if q.get("ready") and str(q.get("queue")).lower() in ("building", "defense"):
                item = str(q["item"]).lower()
                if self._fresh("place:" + item, 3):
                    orders.append({"cmd": "place", "item": item})

        # 3. Next building in the plan.
        rax = "barr" if ("barr" in avail or owned["barr"]) else "tent"
        if not queued.get("building") and eco["cash"] >= 150:
            item = self._next_building(eco, avail, owned, rax)
            if item and self._fresh("build:" + item, 4):
                orders.append({"cmd": "build", "item": item})

        # 4. A couple of defenses once the war factory exists.
        if not queued.get("defense") and owned["weap"] and eco["cash"] > 1500:
            if sum(owned[d] for d in ("ftur", "pbox", "gun")) < 2:
                for d in ("ftur", "pbox"):
                    if d in avail and self._fresh("build:" + d, 6):
                        orders.append({"cmd": "build", "item": d})
                        break

        # 5. Infantry.
        if owned[rax] and not queued.get("infantry") and eco["cash"] > 250:
            if self._fresh("train:inf", 4):
                orders.append({"cmd": "train", "item": "e1", "count": 5})

        # 6. Vehicles: a second harvester first, then the best tank we can build.
        harvesters = eco.get("harvesters", 0)
        harvesters = len(harvesters) if isinstance(harvesters, list) else int(harvesters or 0)
        if owned["weap"] and not queued.get("vehicle"):
            if owned["proc"] and harvesters < 2 and "harv" in avail and eco["cash"] > 1100:
                if self._fresh("train:harv", 8):
                    orders.append({"cmd": "train", "item": "harv", "count": 1})
            elif eco["cash"] > 700 and self._fresh("train:veh", 4):
                for t in ("3tnk", "2tnk", "1tnk", "ftrk", "jeep"):
                    if t in avail:
                        orders.append({"cmd": "train", "item": t, "count": 2})
                        break

        # 7. Idle harvesters go back to work.
        idle_h = [
            u["id"] for u in obs.get("units", [])
            if u.get("type") == "harv" and u.get("idle") and not u.get("wreck")
        ]
        if idle_h and self._fresh("harvest", 5):
            orders.append({"cmd": "harvest", "units": idle_h[:50]})

        # 8. Repair damaged buildings (up to 3).
        for b in (legal.get("repair") or [])[:3]:
            if self._fresh("repair:%s" % _id(b), 10):
                orders.append({"cmd": "repair", "building": _id(b)})

        # 9. Attack waves.
        wave = directive["wave_size"] + {"attack": -4, "balanced": 0, "econ": 4, "defend": 99}[stance]
        combat = legal.get("combat_units") or []
        if legal.get("attack_now") and len(combat) >= max(6, wave) and self._fresh("attack", 8):
            orders.append(legal["attack_now"])

        return orders

    def note(self):
        d = self.strategist.directive
        return ("%s: %s" % (d["stance"], d["note"]))[:400]


_policies = {}


def redalert_action(state):
    """Return play_action arguments for the docs' sample loop, or None to wait."""
    key = (state.get("session_id"), state.get("your_seat"))  # two seats may share a session
    policy = _policies.setdefault(key, RedAlertPolicy())
    orders = policy.decide(state)
    if not orders:
        return None
    return {
        "session_id": state["session_id"],
        "state_version": state["state_version"],
        "action": {"type": "orders", "orders": orders[:20]},
        "reasoning_summary": policy.note(),
    }
