# Red Alert policy parameters

Edit `params.json` (numbers only). Values outside the range are clamped; unknown names are ignored.

| name | default | min | max | what it does |
|---|---|---|---|---|
| `power_margin` | 30 | 0 | 200 | build a power plant when power_drained + margin > power_provided |
| `power_margin_2` | 60 | 0 | 300 | earlier-warning margin for an extra power plant |
| `cash_reserve` | 100 | 0 | 1000 | funds kept unspent when deciding army and harvester purchases |
| `first_proc_min_cash` | 1300 | 0 | 4000 | funds needed before the first refinery |
| `second_proc_cash` | 1200 | 0 | 4000 | funds needed before an extra refinery |
| `war_factory_min_cash` | 1200 | 0 | 4000 | funds needed before a war factory |
| `extra_power_min_cash` | 400 | 0 | 2000 | funds needed before an extra power plant |
| `target_powr` | 4 | 1 | 10 | power plants to build |
| `target_proc` | 3 | 1 | 5 | refineries to build (extra ones wait for 2 harvesters) |
| `target_rax` | 2 | 1 | 5 | barracks (tent or barr) to build |
| `target_weap` | 2 | 1 | 4 | war factories to build |
| `target_fix` | 1 | 0 | 2 | service depots (unlock heavy tanks) |
| `target_defenses` | 3 | 0 | 10 | defensive structures (flame tower, pillbox, turret, tesla coil) |
| `minimum_economic_harvesters` | 2 | 1 | 4 | harvesters to get before optional spending |
| `harvesters_target` | 4 | 1 | 8 | harvesters to build in total |
| `harvester_min_cash` | 1000 | 300 | 3000 | funds needed before training a harvester |
| `combat_target` | 30 | 6 | 80 | living combat units that trigger the main attack |
| `combat_batch` | 3 | 1 | 10 | infantry per order once past the defense floor |
| `combat_min_cash` | 700 | 100 | 3000 | funds needed before extra infantry past the floor |
| `surplus_cash` | 2000 | 500 | 10000 | above this, always keep training army (never sit on money) |
| `floor_t1` | 100 | 30 | 400 | game second where floor_0 ends |
| `floor_t2` | 170 | 60 | 600 | game second where floor_1 ends |
| `floor_t3` | 230 | 90 | 900 | game second where floor_2 ends |
| `floor_0` | 5 | 0 | 40 | minimum army before floor_t1 |
| `floor_1` | 10 | 0 | 40 | minimum army before floor_t2 |
| `floor_2` | 18 | 0 | 60 | minimum army before floor_t3 |
| `floor_3` | 26 | 0 | 80 | minimum army after floor_t3 |
| `use_tanks` | 1 | 0 | 1 | 1 = also train tanks from the war factory |
| `tank_min_cash` | 1500 | 500 | 4000 | funds needed before training tanks |
| `tank_count` | 2 | 1 | 5 | tanks per order |
| `rally_distance` | 14 | 0 | 40 | cells in front of the base toward the enemy where troops gather (0 = off) |
| `defend_radius` | 28 | 5 | 80 | enemy within this many cells of base triggers a defensive response |
| `reinforce_every_s` | 15 | 5 | 120 | seconds between reinforcement waves during an attack |
| `reinforce_min` | 8 | 1 | 40 | idle units at home needed to send a reinforcement wave |
| `all_in_cooldown_s` | 25 | 5 | 200 | seconds between main attack launches |
| `tiebreak_window_s` | 150 | 0 | 600 | last seconds: hold the army if ahead, push if behind (0 = off) |
| `repair_min_cash` | 900 | 0 | 3000 | funds needed before repairing a building |
| `log_every_s` | 5 | 1 | 30 | game seconds between log snapshots |
