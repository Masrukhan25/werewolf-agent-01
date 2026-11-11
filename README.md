# werewolf-agent-01 — scripted Werewolf agent

A model-free (scripted, no LLM calls) agent that plays one seat in 30 games
of 8-player Werewolf through the env at `MAKEFUN_ENV_URL`.

## How it plays

It parses each game's view into a structured state — the public log
(statements, passes, votes, exiles, deaths, revealed roles), the private
record (seer inspections, doctor protections, wolf votes) — and scores
every living seat's suspicion from claims, accusations and the vote record.
Every decision is a deterministic function of that state (fixed seat-number
tie-breaks, no clock, no randomness), so a replay leaves the same actions.

- **Werewolf:** agrees kills with the partner (matches their vote, or picks
  the biggest threat: a seer claim first, then the most vocal accuser);
  blends in by day, steers votes onto villagers, and may fake a seer claim
  late when no seer has claimed.
- **Seer:** inspects the most suspicious first; claims clearly with the
  night and seat when a wolf is found; shares clears to steer the village.
- **Doctor:** protects the seer claim when there is one, else the most
  useful villager; never self, never the same seat twice in a row.
- **Villager:** votes the most suspicious; calls out contradictions (two
  seer claims means one is a wolf) and bad votes with evidence.

## Local evaluation

No Anthropic/OpenAI key is available, so `grade-local` cannot run the house
players. Instead the entry was evaluated against the kit's real env server
(`program/env/server.mjs`) driven by a mock house lane (a small Python
server answering `/v1/chat/completions` with deterministic heuristic house
players). Practice scores (official scorer, 30/30 games finished each):

- trial-0: **767** (wolf 1000, village 533; seer 600, doctor 600, villager 400)
- trial-1: **700** (wolf 1000, village 400)
- trial-2: **700** (wolf 1000, village 400)

Mean **722**. The mock house is a keyword-parsing heuristic, weaker than
the real claude-haiku-5-5 house in reasoning but with perfect memory, so
these scores are optimistic; the wolf side (15/15 on every trial) is the
strongest, the village side the weakest.

## Run

`MAKEFUN_ENV_URL=http://127.0.0.1:8080 python3 main.py`
(Python 3.13, standard library only.)
