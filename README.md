# werewolf-agent-01 — scripted Werewolf agent (v3)

A model-free (scripted, no LLM calls) agent that plays one seat in 30 games
of 8-player Werewolf through the env at `MAKEFUN_ENV_URL`.

## Why v3

The v2 entry scored **433** on the real board (position 27 of 415 scored),
up from v1's 233 — but the target is 500+. Diagnosing v2 on the hard mock
revealed the bottleneck was entirely the village side: wolf 15/15 in every
trial (1000), village 1/45 (22). The village could not coordinate an exile.

Two causes were found. First, the mock house (and any keyword reader) only
parses accusations from the exact phrase "X is a werewolf" plus a "seat N"
mention in the same speech to follow a seer claim — and v2's statements
said things like "I keep coming back to X" without the explicit phrase or
the seat number, so its accusations were invisible to the vote-following
logic. Second, v2 had no voting-pattern signal: wolves (almost) never vote
for their partner and vote together for the same villager, which is a
detectable tell once tracked.

v3 fixes both: every accusation uses the canonical phrase "NAME is a
werewolf (seat N)" with a "Vote seat N" call, and suspicion now includes a
voting-pattern partner signal (pairs that avoid each other across 2+ days,
and pairs that vote for the same target on 2+ days). The village vote bar
is lowered from 2.0 to 1.5 so the agent votes its top suspect instead of
following a possibly wolf-driven plurality.

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
Mean **511** (village 22, vote accuracy ~503).

v3 practice scores (hard mock, official scorer, 30/30 games each):

Hard mock — trial-0: **600** (wolf 1000, village 200, vote acc. 732),
trial-1: **567** (wolf 1000, village 133, vote acc. 688), trial-2:
**667** (wolf 1000, village 333, vote acc. 780). Mean **611** (village
222, vote accuracy 733) — +100 over v2, village 10x better. The wolf side
stays perfect (15/15 every trial); the gain is entirely the village
learning to coordinate: explicit accusations the house can parse, plus the
voting-pattern partner signal.

Real-score expectation: the hard mock overestimates (v2: mock 511 → real
433, gap 78). Applying the same gap, v3 real ≈ 530; applying the mock/real
ratio (0.85), ≈ 520. Either way above the 500 target and above the
20th-place prize cutoff (433). The plain-villager role is still the weakest
(0 in trials 0–1, 200 in trial 2); seer and doctor improved most.

## Run

`MAKEFUN_ENV_URL=http://127.0.0.1:8080 python3 main.py`
(Python 3.13, standard library only.)
