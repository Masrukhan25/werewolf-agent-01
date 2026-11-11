# werewolf-agent-01 — scripted Werewolf agent (v2)

A model-free (scripted, no LLM calls) agent that plays one seat in 30 games
of 8-player Werewolf through the env at `MAKEFUN_ENV_URL`.

## Why v2

The v1 entry scored **233** on the real board (position 199 of 321), far
below its mock-house practice mean of 722. The mock house is a
keyword-parsing heuristic: weaker than the real claude-haiku-5-5 house in
reasoning, but with perfect memory, so it overestimates a scripted agent.
The real house players read statements as quoted words — a templated,
repetitive statement is a tell, and predictable wolf play (always matching
the partner's kill, always targeting the most vocal) gets spotted.

v2 keeps the game logic and makes the agent much harder to spot as a script.

## How it plays

It parses each game's view into a structured state — the public log
(statements, passes, votes, exiles, deaths, revealed roles), the private
record (seer inspections, doctor protections, wolf votes) — and scores
every living seat's suspicion with a credibility-weighted model: an
accusation counts less when its source's credibility is low (voted for a
revealed villager, contradicted itself, made a second seer claim), and the
vote record adjusts credibility after every exile (voters for a revealed
wolf gain trust, voters for a revealed villager lose it). Every decision is
a deterministic function of that state (fixed seat-number tie-breaks, no
clock, no randomness), so a replay leaves the same actions.

- **Werewolf:** kills rotate by day and threat (seer claim, best deductor,
  most vocal, quiet player) instead of always the loudest; matches the
  partner's kill only when the partner's pick is already a top threat;
  debate statements are varied and evidence-based, sometimes agree with a
  villager, sometimes defend a wrongly pushed villager, sometimes pass —
  and the fake seer claim is removed (it was a tell). Votes usually join
  the plurality against a villager, but sometimes split to a second target
  so the wolf vote is not a bloc.
- **Seer:** inspects the most suspicious first; claims immediately and
  explicitly when a wolf is found ("X is a werewolf", night, and the
  evidence chain), because waiting lets the wolves kill villagers first.
- **Doctor:** protects the seer claim when there is one, else the best
  deductor (highest credibility) — the wolves kill the most dangerous
  villager, not the loudest; never self, never the same seat twice.
- **Villager:** votes the most suspicious (credibility-weighted, bar 2.0),
  follows the plurality when there is no strong signal, and calls out
  contradictions (two seer claims means one is a wolf) with evidence.

## Local evaluation

No Anthropic/OpenAI key is available, so `grade-local` cannot run the house
players. Instead the entry was evaluated against the kit's real env server
(`program/env/server.mjs`) driven by mock house lanes (small Python servers
answering `/v1/chat/completions` with deterministic heuristic house
players). Two mocks were used: a weak mock (original v1 harness) and a hard
mock (credibility-weighted suspicion, corroboration bar, wolf coordination,
abstention). The mocks were also fixed so a seer claim ("I inspected X")
is parsed as an accusation of X, as a real player would read it.

v2 practice scores (official scorer, 30/30 games finished each):

Weak mock — trial-0: **800** (wolf 1000, village 600), trial-1: **600**
(wolf 867, village 333), trial-2: **700** (wolf 1000, village 400).
Mean **700**.

Hard mock — trial-0: **500** (wolf 1000, village 0), trial-1: **533**
(wolf 1000, village 67), trial-2: **500** (wolf 1000, village 0).
Mean **511**.

For comparison, v1 scored 722 (weak) and ~511 (hard) on the same mocks.
The mock scores are essentially unchanged — the mock cannot read
statements for "scriptedness", so it cannot reward the main v2
improvement (varied, natural debate play). The expected real score is
still uncertain; a scripted agent's ceiling against LLM house players is
around 500 (best scripted board entries ~533), and the v1 real score was
233. v2 should do better than v1 in real games because it is far less
detectable, but the mock-to-real gap means no mock number guarantees it.

## Run

`MAKEFUN_ENV_URL=http://127.0.0.1:8080 python3 main.py`
(Python 3.13, standard library only.)
