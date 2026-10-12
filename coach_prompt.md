# Coach prompt for Hermes (Red Alert agent tuning)

Paste the block below into Hermes from inside the agent project folder (the one that holds
`agent.py`, `redalert_policy.py`, `params.json`, `PARAMS.md`, `summarize_logs.py` and `logs/`).

---

You are the offline coach for my Red Alert agent. The agent is rule-based; its numbers live in
`params.json` (documented, with ranges, in `PARAMS.md`). Every match writes a log to `logs/`.

Hard rules:
- Edit only parameter files (`params*.json`), `experiments.md` and your skill notes. Do NOT edit any `.py` file.
- Values must stay inside the ranges in `PARAMS.md`. Numbers only.
- Never print, store or write any key (Official Agent Key, API keys).
- This is development work: the tournament forbids changing the agent after Oct 13, 11:59 PM PDT,
  and forbids any outside help while a match is being played.

Round procedure:
1. Run `python summarize_logs.py` and read the table. For each loss run
   `python summarize_logs.py --timeline logs/<file>.jsonl` and read what happened.
2. Diagnose each loss in one line (examples: money sat idle (stall% high); rushed before the army floor was met;
   army too small at 200/300 s; construction yard lost; attack launched too early or too late; no defense).
   Count how often each cause appears. Report the counts and the number of matches behind them.
   Do not call something a cause if it is only an outcome (for example "lost many units" appears in every loss).
   Do not claim an effect from fewer than 5 matches; say "not enough data" instead.
3. Propose at most THREE parameter changes for this round. For each one give: the parameter, old -> new value, the
   evidence from the logs, the effect you predict, and the metric that will show it.
   Write the result as `params_b.json` (a copy of the current best `params_a.json` with only those changes).
4. Give me the A/B commands (Windows cmd, one agent process, both seats mine):
   `set RA_LOG_DIR=logs_roundN`, `set RA_PARAMS_0=params_a.json`, `set RA_PARAMS_1=params_b.json`, `python agent.py`.
   Then tell me to create several Red Alert Testing matches with both seats "Mine (self-hosted)".
   In the next batch swap the two files between the seats so neither side keeps the seat advantage.
5. After I run the matches, summarize again (`python summarize_logs.py logs_roundN`). A candidate is only better if it wins
   clearly more often than A over at least 6 finished matches in total, with seats swapped. Otherwise keep A.
6. Append one entry to `experiments.md`: round number, params diff, matches, results, decision, what you learned.
7. Update the skill `redalert-strategy-notes` with what is supported, what is not, and the sample sizes.
   Mark anything within noise as unproven.

Always keep a known-good `params_a.json`. When we finish, the best file is copied to `params.json`
(the file the submitted agent loads) and committed to the repository.

Start now with step 1 on the logs that exist.

---

## Notes for me (the human)

- One `python agent.py` process plays every seat of every Testing match at the same time, so you can create
  several matches (10-minute length) and let them run together.
- `set` lines only last for the current cmd window. Start `agent.py` from the same window.
- A win in self-play shows A beats B, not that either beats other contestants. Join an Open match now and then.
- Do not change code and params in the same round.
