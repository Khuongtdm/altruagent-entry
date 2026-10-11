"""AltruAgent self-hosted agent: the platform's sample loop, with Red Alert played by
redalert_policy.py (rules + optional LLM strategist). Pokemon and Werewolf still use the
sample's random legal moves until you write policies for them.

    pip install requests
    $env:ALTRUAGENT_OFFICIAL_AGENT_KEY = "eak_live_..."     # PowerShell, this session only
    python agent.py

Run only ONE copy of this program per key. Never commit or print the key.
"""

import os
import random
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

import requests

from redalert_policy import redalert_action

CONTROL_PLANE = "https://api.altruagent-game.com"
RETRY_LATER = {"GAME_NOT_STARTED", "NOT_YOUR_TURN", "STALE_STATE", "RUNTIME_TEMPORARILY_UNAVAILABLE"}
# One id for this running program, kept for its whole life: the server uses it
# to know that a seat's lease belongs to you. 16-256 characters.
EXECUTION_ID = "agent-" + secrets.token_hex(16)


def seconds_left(iso):
    expires = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (expires - datetime.now(timezone.utc)).total_seconds()


class SignInRefused(Exception):
    """The key was refused: wrong, rotated or revoked (or the agent is AWS-hosted)."""


class ControlPlane:
    """Your agent's session with the control plane, from the Official Agent Key.

    One per thread: a requests.Session must not be shared between threads.
    """

    def __init__(self, key):
        self.key = key
        self.http = requests.Session()
        self.session = None

    def authenticate(self):
        delay = 2
        while True:
            try:
                r = self.http.post(
                    CONTROL_PLANE + "/tournament/agent/authenticate",
                    json={"official_agent_key": self.key},
                    timeout=30,
                )
            except requests.RequestException as e:
                r, problem = None, str(e)
            if r is not None and r.status_code == 200:
                self.session = r.json()  # {"access_token", "expires_at"}
                return
            if r is not None and r.status_code in (400, 401, 403):
                raise SignInRefused(f"Sign-in refused ({r.status_code}): {r.text}")
            # Rate limited (429), a server error or no answer: wait and try again.
            print(f"Sign-in failed ({r.status_code if r is not None else problem}); retrying in {delay} s")
            time.sleep(delay)
            delay = min(delay * 2, 60)

    def request(self, method, path, **kwargs):
        if self.session is None or seconds_left(self.session["expires_at"]) < 60:
            self.authenticate()
        for attempt in (1, 2):
            r = self.http.request(
                method,
                CONTROL_PLANE + path,
                headers={"Authorization": "Bearer " + self.session["access_token"]},
                timeout=30,
                **kwargs,
            )
            if r.status_code == 401 and attempt == 1:
                self.authenticate()  # the session expired: sign in again, retry once
                continue
            return r

    def assignments(self):
        r = self.request("GET", "/tournament/agent/assignments")
        r.raise_for_status()
        return r.json()["assignments"]

    def grant(self, seat_id):
        """Take a seat: returns a SeatGrant, or None if another copy holds it."""
        r = self.request(
            "POST", f"/tournament/agent/assignments/{seat_id}/grant", json={"execution_id": EXECUTION_ID}
        )
        if r.status_code in (404, 409):  # gone, not ready yet, or another copy holds it
            return None
        r.raise_for_status()
        return r.json()

    def renew(self, seat_id):
        r = self.request(
            "POST", f"/tournament/agent/assignments/{seat_id}/lease/renew", json={"execution_id": EXECUTION_ID}
        )
        return r.status_code == 200


def keep_lease(key, seat_id, stop):
    """Renew the seat's lease every 10 s (it lapses after 30 s without one)."""
    control = ControlPlane(key)  # its own connection: this runs in a thread
    while not stop.wait(10):
        try:
            if not control.renew(seat_id):
                return  # the game has ended, or another copy holds the seat
        except requests.RequestException as e:
            print(f"Lease renewal failed, retrying: {e}")
        except SignInRefused:
            return


class Seat:
    """One granted seat: its credentials and its MCP connection."""

    def __init__(self, control, assignment, grant):
        self.control = control
        self.seat_id = assignment["seat_id"]
        self.grant = grant
        self.http = requests.Session()  # one connection, reused for every call
        self.request_id = 0

    def regrant(self):
        fresh = self.control.grant(self.seat_id)
        if fresh is None:  # ends this seat's thread only
            raise RuntimeError("The seat can't be taken again: the game ended, or another copy holds it.")
        self.grant = fresh

    def call(self, tool, **arguments):
        """Call one MCP tool; returns its result object (which may hold "error")."""
        if seconds_left(self.grant["expires_at"]) < 120:  # renew before the token expires
            self.regrant()
        for attempt in (1, 2):
            self.request_id += 1
            r = self.http.post(
                self.grant["gameapi_server_url"] + "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
                headers={
                    "Authorization": "Bearer " + self.grant["access_token"],
                    "Accept": "application/json, text/event-stream",
                },
                timeout=60,
            )
            if r.status_code == 401 and attempt == 1:
                self.regrant()  # the token expired: get a fresh grant, then retry once
                continue
            r.raise_for_status()
            reply = r.json()
            if "error" in reply:  # a JSON-RPC protocol error, not a game error
                raise RuntimeError(reply["error"])
            return reply["result"]["structuredContent"]


def fill_pokemon_template(state, template):
    """Pokemon VGC team preview and doubles turns: the one legal action is a
    template to fill in, not a finished move (see /skill/pokemon, sections 6-7).
    Placeholder: random choices. Replace with a real policy."""
    base = {"session_id": state["session_id"], "state_version": state["state_version"]}
    if template["type"] == "select_lineup":
        bring = random.sample(template["roster"], 4)  # 4 of your 6; the first 2 lead
        return {**base, "action": {"type": "select_lineup", "bring": bring, "leads": bring[:2]}}
    action, taken = {"type": "doubles_turn"}, set()
    for slot in template["slots"]:
        moves = [o for o in slot["options"] if o["type"] == "move"]
        # Two slots may not switch to the same Pokemon (with one reserve left,
        # both list it: one switches, the other passes).
        switches = [o for o in slot["options"] if o["type"] == "switch" and o["species"] not in taken]
        if moves:
            move = random.choice(moves)
            targets = move.get("targets") or [0]
            foes = [t for t in targets if t > 0]  # 1 / 2: the opponent's Pokemon
            pick = {"type": "move", "move_id": move["move_id"], "target": random.choice(foes or targets)}
        elif switches:
            pick = {"type": "switch", "species": random.choice(switches)["species"]}
            taken.add(pick["species"])
        else:
            pick = {"type": "pass"}
        action[f"slot_{slot['slot']}"] = pick
    return {**base, "action": action}


def choose_action(state):
    """Your policy. Return the arguments for play_action, or None to wait."""
    if state.get("game_type") == "red_alert":
        try:
            return redalert_action(state)
        except Exception:  # a bug in the policy must not kill the seat's thread
            traceback.print_exc()
            return None
    legal = state.get("legal_actions")
    if not legal:
        return None
    actions = legal.get("actions") or []
    if not actions:
        return None
    template = (actions[0].get("input") or {}).get("action") or {}
    if template.get("type") in ("select_lineup", "doubles_turn"):
        return fill_pokemon_template(state, template)
    # Werewolf, and Pokemon draft picks: every legal action carries ready-made arguments.
    return random.choice(actions)["input"]


def play(seat):
    """Plays one seat's game. Returns True once the game is over (or gone)."""
    grant = seat.grant
    sid = grant["game_session_id"]
    name = f"Seat {grant['seat_position'] + 1} of {grant['seat_count']}"
    print(f"{name}, {grant['game_type']}, session {sid}")
    state = seat.call("get_game_state", session_id=sid)  # this call marks the seat present
    last_seq = -1
    while True:
        if "error" in state:
            if state["error"] == "SESSION_NOT_FOUND":
                print(f"{name}: the game is over and gone")
                return True
            if state["error"] not in RETRY_LATER:
                print(f"{name}: game error: {state}")
                return False  # tried again on a later poll while the seat is still listed
            time.sleep(1)
            state = seat.call("get_game_state", session_id=sid)
            continue
        if state["is_terminal"]:
            print(f"{name}: result:", seat.call("get_result", session_id=sid))
            return True
        for message in state.get("new_messages") or []:
            last_seq = max(last_seq, message["seq"])
        if state.get("waiting_for_players"):
            print(f"{name}: waiting for seats", state["waiting_for_players"], "to connect...")
        elif (
            state.get("phase") == "messaging"
            and not state.get("eliminated")
            and state.get("your_position") not in (state.get("terminated_messaging") or [])
        ):
            # Werewolf discussion: say nothing and end our part of the window.
            seat.call("send_message", session_id=sid, message_type="terminate", recipients=[])
        else:
            arguments = choose_action(state)
            if arguments is not None:
                reply = seat.call("play_action", **arguments)
                if "error" not in reply:
                    state = reply.get("state") or seat.call("get_game_state", session_id=sid)
                    continue
                refused = reply["error"]
                state = seat.call("get_game_state", session_id=sid)
                if refused in RETRY_LATER or "error" in state:
                    continue  # nothing was applied: decide again on the fresh state
                # Refused (for example INVALID_ACTION): keep the seat and wait for
                # the game to move on, rather than sending the same move again.
                print(f"{name}: move refused: {reply}")
        state = seat.call(
            "wait_for_update",
            session_id=sid,
            since_version=state["state_version"],
            since_message_seq=last_seq,
            since_is_current_actor=state["is_current_actor"],
            since_phase=state["phase"],
        )


def run_seat(key, assignment, finished):
    """One seat, in its own thread: take it, keep its lease, play it to the end."""
    seat_id = assignment["seat_id"]
    control = ControlPlane(key)  # its own connections: this runs in a thread
    try:
        grant = control.grant(seat_id)
    except (requests.RequestException, SignInRefused) as e:
        print(f"Could not take seat {seat_id}, trying again soon: {e}")
        return
    if grant is None:
        return  # not ready yet, over, or another copy holds it: tried again on the next poll
    stop = threading.Event()
    threading.Thread(target=keep_lease, args=(key, seat_id, stop), daemon=True).start()
    try:
        if play(Seat(control, assignment, grant)):
            finished.add(seat_id)  # over: never taken again
    except Exception as e:  # this seat only; it is tried again on a later poll
        print(f"Seat {seat_id} stopped: {e}")
    finally:
        stop.set()


def main():
    key = os.environ.get("ALTRUAGENT_OFFICIAL_AGENT_KEY")
    if not key:
        sys.exit("Set ALTRUAGENT_OFFICIAL_AGENT_KEY to your Official Agent Key (eak_live_...).")
    control = ControlPlane(key)
    try:
        control.authenticate()
    except SignInRefused as e:
        sys.exit(str(e))
    print("Signed in. Waiting for games...")
    workers = {}  # seat_id -> the thread playing it
    finished = set()  # seats whose game is over
    while True:
        try:
            assignments = control.assignments()
        except SignInRefused as e:
            sys.exit(str(e))  # the key was rotated or revoked
        except requests.RequestException as e:
            print(f"Could not list games, retrying: {e}")
            time.sleep(10)
            continue
        listed = {a["seat_id"] for a in assignments}
        for assignment in assignments:
            seat_id = assignment["seat_id"]
            worker = workers.get(seat_id)
            if seat_id in finished or (worker is not None and worker.is_alive()):
                continue
            # Every seat at once: a game only starts when all its seats have
            # connected, and one key may hold several of them.
            worker = threading.Thread(target=run_seat, args=(key, assignment, finished), daemon=True)
            worker.start()
            workers[seat_id] = worker
        for seat_id in list(workers):
            if seat_id not in listed and not workers[seat_id].is_alive():
                del workers[seat_id]
        finished &= listed
        time.sleep(10)  # check for new games every 10 s


if __name__ == "__main__":
    main()
