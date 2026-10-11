# AltruAgent entry (self-hosted)

Self-hosted agent for the AltruAgent AI Agent Gaming Tournament.

## How it runs

```
pip install -r requirements.txt
# PowerShell, current session only (never write the key into a file):
$env:ALTRUAGENT_OFFICIAL_AGENT_KEY = "eak_live_..."
python agent.py
```

`agent.py` signs in with the Official Agent Key, polls for assigned games every 10 s, takes each seat, keeps its lease, and plays through the game server's MCP interface. Run one copy only.

## Components

- `agent.py`: connection and play loop, based on the platform's published sample agent.
- `redalert_policy.py`: Red Alert policy. Rule-based code issues all orders (build order, unit training, attack waves). An optional LLM "strategist" runs in a background thread every ~25 s and only sets a stance (econ, balanced, attack, defend) and an attack wave size. It never issues orders and never blocks the rules.
- Pokémon and Werewolf: currently placeholder random legal moves from the sample.

## Models, services, and prompts

- LLM use is optional and off unless `LLM_BASE_URL` and `LLM_MODEL` are set (any OpenAI-compatible endpoint, such as a local Ollama server or OpenAI), with `LLM_API_KEY` if needed.
- The only prompt is the `SYSTEM` string in `redalert_policy.py`. The model receives a JSON summary of the agent's own player-view observation and returns a small JSON directive that the code validates and clamps.
- No web search, no spectator data, and no information beyond the agent's own player view is used. Match memory is not carried between matches.

## Dependencies

See `requirements.txt`.

## Secrets

No credentials are stored in this repository. The Official Agent Key and any LLM key are read from environment variables.
