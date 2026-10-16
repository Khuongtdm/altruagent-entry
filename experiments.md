# Experiments — Red Alert agent (rule-based, params-only tuning)

Method per `coach_prompt.md`: read the match logs, diagnose each loss in one line, count causes,
propose at most three parameter changes, write `params_b.json`, run A/B with seats swapped, keep A
unless B wins clearly over >= 6 finished matches. Only parameter files are edited.

Data note: the agent writes `final` records only when the platform calls `log_result`, so most logs
here have no `your_return`. Wins/losses below are therefore read from the log itself: a seat that
loses its construction yard (`lost_fact`) and is left with no buildings has lost; the winner is the
other seat of the same match. Every conclusion states its sample size.

---

## Round 0 — baseline read-out (no change proposed)

Matches behind it: 6 sessions, 10 seat-logs, 5 decided wins / 5 decided losses.
`644b0063` and `ab366819` are single-seat logs (their partner seat was never captured), so they
contribute a seat each, not a match each.

### Loss diagnosis (one line each)

| seat | faction | line |
|---|---|---|
| 1a221251_Multi1 | soviet | 1 harvester all game; never reached the tank gate; yard lost 547 s. |
| 3d860262_Multi0 | soviet | 1 harvester all game, 2nd only at 500 s; 0 tanks; no attack ever launched; yard lost 549 s. |
| 3dd1ce95_Multi0 | soviet | 1 harvester all game; 0 tanks; army 26 held to 370 s, then wiped; yard lost 536 s. |
| 644b0063_Multi0 | allied | built 5 tanks but attacked alone at 349 s into a larger enemy army (attack_wiped at 619 s), then lost 27 buildings. |
| 6e47359c_Multi0 | soviet | 1 harvester all game; 0 tanks; yard lost 520 s. |

### Cause counts (5 losses, 5 seats)

- **Cash gates strangle the war factory.** The war factory completes at gs 110 in **10/10** seats.
  After that the three branches that would use it (extra refinery 1200, harvester 1000+100 reserve,
  tanks 1500) all need more cash than the single harvester can bank before an enemy push arrives.
  Cycles (5 s each) in which *every other condition of the branch already held* and only the cash
  gate blocked it: harvester **351 LOST vs 50 WIN**, tank **261 LOST vs 193 WIN**, refinery #2
  **236 LOST vs 62 WIN**. Five of five losses and five of five wins sit behind this same gate, so
  the gate alone does not decide the match — but the losses are exactly the seats that never got
  past it.
- **Never left the one-harvester trap: 4/5 losses.** 2nd harvester arrived at gs 140–145 in all five
  wins; in four of five losses it arrived at 450/500 s or never (h=1 at 300 s and 400 s, and h=1 at
  the moment the yard died). The 2nd harvester was blocked *only* by `harvester_min_cash` (1000)
  plus the 100 reserve in 85 cycles per losing seat against 48 in winners; with the gate at 600 the
  same orders would have fired at gs 110–145 instead of 450–500.
- **No armour at all: 4/5 losses** (0 tanks ever built; winners field 4–6). Tank orders fired at
  funds 1490–1681 (gate 1500+100 was the binding constraint); at gate 900 the first tank would have
  come at gs 170–225 instead of 330–385.
- **Second refinery late or never: 4/5 losses** (proc stayed at 1 all game in the four one-harvester
  losses; the winners reached proc 2 at 310–335 s). At `second_proc_cash` 800 the 2nd refinery would
  have been ordered at gs 115–170.
- **Not a cause (outcome only):** "lost many units" and "lost the yard" — 75 buildings lost across
  the 5 losing seats versus 0 across the 5 winners, i.e. the whole loss, not a lever.
- **Not a cause:** idle money. Max spendable funds after the war factory: losers 780–1681, winners
  2041–2430. Losers were *poor*, not hoarding; stall% in the losing seats is 0–3%.
- **Not a cause at this sample size:** defenses (3 flame towers/pillboxes were up in the losing
  seats), refused orders (24 total, all `attack_move` `not_owned`/`unknown_actor` — a bookkeeping
  artefact of re-issuing orders to dead units, not a lost battle).
- **Same-code control:** `3d860262` is one match, same params, same code, decided by faction only —
  Multi0 (Soviet, `barr`) lost, Multi1 (Allied, `tent`) won. The Soviet seat's 2nd harvester needed
  a free vehicle slot; the Allied seat's harvester order fired at gs 110 (harv 0.10 already in the
  vehicle queue) and 2nd harvester was out by 145. 2 seats, so faction is a hypothesis, not a finding.

### Round 0 changes

None. Baseline is recorded as `params_a.json` = the shipped defaults (`params_econ.json`), verified
identical key by key.

---

## Round 1 — loosen the three cash gates (A = params_a.json, B = params_b.json)

Diff (three parameters, all inside the PARAMS.md ranges):

| parameter | A | B | evidence | predicted effect | metric |
|---|---|---|---|---|---|
| `harvester_min_cash` | 1000 | 600 | 85 cycles/seat in losses blocked by this gate alone vs 48 in wins; 2nd harvester at 450–500 s or never in 4/5 losses vs 140–145 s in 5/5 wins | 2nd harvester out at gs 110–145; income doubles in the 110–350 s window | `harvesters` at 200/300 s; gs of the 2nd harvester |
| `second_proc_cash` | 1200 | 800 | proc stuck at 1 for the whole game in 4/5 losses (winners reached proc 2 at 310–335 s); gate blocked 236 cycles in losses vs 62 in wins | 2nd refinery ordered at gs 115–170; a 3rd harvester with it | `bld.proc` at 300 s; harvesters at 300 s |
| `tank_min_cash` | 1500 | 900 | 0 tanks ever in 4/5 losses; tank orders fired at funds 1490–1681; gate blocked 261 cycles in losses vs 193 in wins | first tank at gs 170–225 instead of 330–385 | `units.2tnk/3tnk` at 300 s and 400 s |

Kept as A: `combat_target` 30, `floor_3` 26, `surplus_cash` 2000, `use_tanks` 1 — the army freeze at
the floor is equally common in winners (65 blocked cycles in losses vs 82 in wins), so it is not
evidence-backed and was not touched. Two parameters that look wrong but are *not* supported were
also left alone: `harvesters_target` 4 (the 2nd harvester is the whole gap; 4 is only reached later)
and `combat_batch` (infantry was not the bottleneck — 26 infantry were alive at 300 s in every seat).

A/B command (Windows cmd, one agent process, both seats mine):

```
set RA_LOG_DIR=logs_round1
set RA_PARAMS_0=params_a.json
set RA_PARAMS_1=params_b.json
python agent.py
```

Then create several Red Alert Testing matches with both seats "Mine (self-hosted)". In the next
batch swap the two files between the seats (`RA_PARAMS_0=params_b.json`, `RA_PARAMS_1=params_a.json`)
so neither side keeps the seat advantage.

Decision rule: B must win clearly more often than A over >= 6 finished matches in total with seats
swapped; otherwise keep A. Result: **pending** (no `logs_round1` yet).

### Falsifiers for round 1

If B is not better, the gate theory is wrong for this opponent pool and the next lever is elsewhere.
Concretely, B has failed if: (a) `harvesters` at 300 s is still 1 in the B seats, (b) first tank is
still after gs 330 in the B seats, or (c) the construction yard still dies around gs 500–550 in B.
If B does fire the orders but still loses the same way, the bottleneck is defence/attack timing, not
the economy, and round 2 should look at `defend_radius` / `floor_*` / `all_in_cooldown_s`.
