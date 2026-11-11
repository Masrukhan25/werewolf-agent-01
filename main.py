# werewolf-agent-01: a scripted (model-free) Werewolf agent.
#
# Plays one seat in 30 games of 8-player Werewolf through the env at
# MAKEFUN_ENV_URL. No model calls, no clock, no unseeded randomness: every
# decision is a deterministic function of the game's view (log, private
# record, options) with fixed seat-number tie-breaks, so a replay leaves the
# same actions. Python 3.13, standard library only.
#
# Strategy in short:
#   * track the public log: claims (seer/doctor), accusations, votes, deaths
#     and revealed roles; score every living seat's suspicion from them.
#   * WOLF: agree kills with the partner (match their vote, or pick the
#     biggest threat: a seer claim first, then the most vocal accuser);
#     blend in by day, steer votes onto villagers, save the partner from a
#     plurality by forcing a tie.
#   * SEER: inspect the most suspicious first; claim clearly with the night
#     and seat when a wolf is found; share clears to steer the village.
#   * DOCTOR: protect the seer claim when there is one, else the most
#     useful villager; never self, never the same seat twice in a row.
#   * VILLAGER: vote the most suspicious; call out contradictions (two
#     seer claims means one is a wolf) and bad votes with evidence.

import json
import os
import re
import urllib.request

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
MAX_STATEMENT = 500

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def post(path, body, timeout=65):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ENV_URL + path, data=data,
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def act(game, decision, **kw):
    body = {"game": game, "decision": decision}
    body.update(kw)
    try:
        post("/act", body)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def fold(text):
    """Fold a statement the env's way and cut it to the limit."""
    text = re.sub(r"[\t\n\v\f\r\x85\u2028\u2029]+", " ", text)
    # drop remaining Cc controls (U+0000-U+001F, U+007F-U+009F)
    text = "".join(ch for ch in text
                   if not (ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F))
    text = text.strip()
    chars = list(text)
    if len(chars) > MAX_STATEMENT:
        text = "".join(chars[:MAX_STATEMENT]).strip()
    return text


def name_of(state, seat):
    p = state.players.get(seat)
    return p["name"] if p else "seat %d" % seat


# --------------------------------------------------------------------------
# Game state derived from one view
# --------------------------------------------------------------------------

class State:
    def __init__(self, view):
        self.view = view
        self.day = view.get("day", 1)
        self.phase = view.get("phase", "")
        you = view.get("you") or {}
        self.seat = you.get("seat")
        self.name = you.get("name", "?")
        self.role = you.get("role", "villager")
        self.alive = bool(you.get("alive"))
        partner = you.get("partner") or {}
        self.partner = partner.get("seat") if partner else None
        self.players = {}
        for p in view.get("players") or []:
            self.players[p["seat"]] = p
        self.name_to_seat = {p["name"]: p["seat"] for p in self.players.values()}
        self.log = view.get("log") or []
        self.seer_results = view.get("seerResults") or []
        self.last_protect = view.get("lastProtect") or {}
        self.wolf_votes = view.get("wolfVotes") or []
        self.outcome = view.get("outcome")
        self.living = [s for s in sorted(self.players)
                       if self.players[s].get("alive")]
        self.dead = [s for s in sorted(self.players)
                     if not self.players[s].get("alive")]
        self.revealed = {s: self.players[s].get("role") for s in self.dead}
        self._parse_log()
        self._suspicion()

    # -- log parsing ----------------------------------------------------
    def _parse_log(self):
        self.speeches = []      # (day, seat, round, text)
        self.passes = []        # (day, seat, round)
        self.votes = []         # (day, seat, target)
        self.exiles = []        # (day, seat, role, tally)
        self.dawns = []         # (day, died, role)
        for e in self.log:
            k = e.get("kind")
            if k == "speech":
                self.speeches.append((e.get("day"), e.get("seat"),
                                      e.get("round"), e.get("text") or ""))
            elif k == "pass":
                self.passes.append((e.get("day"), e.get("seat"), e.get("round")))
            elif k == "vote":
                self.votes.append((e.get("day"), e.get("seat"), e.get("target")))
            elif k == "exile":
                self.exiles.append((e.get("day"), e.get("seat"),
                                    e.get("role"), e.get("tally") or []))
            elif k == "dawn":
                self.dawns.append((e.get("day"), e.get("died"), e.get("role")))
        # claims and accusations from speeches
        self.seer_claims = []   # seats that claimed seer
        self.doctor_claims = []  # seats that claimed doctor
        self.accused = {}       # target seat -> [(accuser, day)]
        self.cleared_by = {}    # target seat -> [speaker]
        self.speech_count = {}
        for (day, seat, rnd, text) in self.speeches:
            if seat is None:
                continue
            self.speech_count[seat] = self.speech_count.get(seat, 0) + 1
            low = text.lower()
            if self._claims_role(low, "seer"):
                if seat not in self.seer_claims:
                    self.seer_claims.append(seat)
            if self._claims_role(low, "doctor"):
                if seat not in self.doctor_claims:
                    self.doctor_claims.append(seat)
            for tgt in self._mentioned_wolves(low, seat):
                self.accused.setdefault(tgt, []).append((seat, day))
            for tgt in self._mentioned_clear(low, seat):
                self.cleared_by.setdefault(tgt, []).append(seat)
        # today's votes so far (for bandwagon reading)
        self.today_votes = [v for v in self.votes if v[0] == self.day]
        self.today_tally = {}
        for (_, _, t) in self.today_votes:
            if t is not None:
                self.today_tally[t] = self.today_tally.get(t, 0) + 1
        # current debate round: speeches+passes today so far
        n_today = len([s for s in self.speeches if s[0] == self.day]) + \
            len([p for p in self.passes if p[0] == self.day])
        n_living = max(1, len(self.living))
        self.round = n_today // n_living + 1

    def _claims_role(self, low, role):
        if role not in low:
            return False
        if re.search(r"\bi['’]m (the |a )?" + role + r"\b", low):
            return True
        if re.search(r"\bi am (the |a )?" + role + r"\b", low):
            return True
        if role == "seer" and re.search(
                r"\bi (inspected|checked) (seat \d+|\w+)", low):
            return True
        if role == "doctor" and re.search(r"\bi protected (seat \d+|\w+)", low):
            return True
        return False

    def _seat_from_mention(self, low, text):
        """Resolve a name or 'seat N' mention to a seat number, or None."""
        m = re.search(r"seat (\d+)", low)
        if m:
            s = int(m.group(1))
            if s in self.players:
                return s
        for name, s in self.name_to_seat.items():
            if re.search(r"\b" + re.escape(name.lower()) + r"\b", low):
                return s
        return None

    def _mentioned_wolves(self, low, speaker):
        out = []
        if "wolf" not in low and "werewolf" not in low:
            return out
        # "X is a wolf/werewolf" or "seat N is a wolf"
        for m in re.finditer(r"(seat \d+|\w+) is (a |the )?werewolf", low):
            s = self._seat_from_mention(low, m.group(1))
            if s is not None and s != speaker:
                out.append(s)
        for m in re.finditer(r"(seat \d+|\w+) is (a |the )?wolf\b", low):
            s = self._seat_from_mention(low, m.group(1))
            if s is not None and s != speaker:
                out.append(s)
        # "vote X, they're a wolf" / "X is the wolf"
        if re.search(r"vote (for )?(seat \d+|\w+)", low) and \
                re.search(r"wolf", low):
            s = self._seat_from_mention(low, low)
            if s is not None and s != speaker and s not in out:
                out.append(s)
        return out

    def _mentioned_clear(self, low, speaker):
        out = []
        if "clear" not in low and "not a wolf" not in low and \
                "not a werewolf" not in low:
            return out
        for m in re.finditer(r"(seat \d+|\w+) is (not a |not the )?(wolf|werewolf|clear)", low):
            pass
        if re.search(r"(is )?clear\b", low) or "not a wolf" in low or \
                "not a werewolf" in low:
            s = self._seat_from_mention(low, low)
            if s is not None and s != speaker:
                out.append(s)
        return out

    # -- suspicion ------------------------------------------------------
    def _suspicion(self):
        """Suspicion score per living seat (higher = more likely a wolf)."""
        score = {s: 0.0 for s in self.living}
        for tgt, accs in self.accused.items():
            if tgt in score:
                score[tgt] += 2.0 * min(len(accs), 3)
        for tgt, clrs in self.cleared_by.items():
            if tgt in score:
                score[tgt] -= 1.0 * min(len(clrs), 2)
        # two seer claims: both are suspicious (one is a wolf)
        if len(self.seer_claims) >= 2:
            for s in self.seer_claims:
                if s in score:
                    score[s] += 2.0
        # vote record: pushing a villager's exile is bad; a wolf's is good
        for (day, voter, tgt) in self.votes:
            if tgt is None:
                continue
            for (eday, exiled, erole, _) in self.exiles:
                if eday == day and exiled == tgt:
                    if erole == "villager" and voter in score:
                        score[voter] += 2.0
                    elif erole == "werewolf" and voter in score:
                        score[voter] -= 2.0
        # votes received today: the table already suspects them a little
        for tgt, n in self.today_tally.items():
            if tgt in score:
                score[tgt] += 0.5 * n
        # quiet players are slightly suspicious; very vocal ones less so
        for s in self.living:
            if self.speech_count.get(s, 0) == 0:
                score[s] += 0.5
        # my own seer results dominate
        for r in self.seer_results:
            s = r.get("seat")
            if s in score:
                score[s] = 100.0 if r.get("werewolf") else -100.0
        self.suspicion = score
        # known wolves / known clears (seer only)
        self.known_wolves = [s for s, v in score.items() if v >= 100.0]
        self.known_clear = [s for s, v in score.items() if v <= -100.0]

    # -- helpers --------------------------------------------------------
    def most_suspicious(self, candidates, exclude=()):
        best = None
        best_key = None
        for s in candidates:
            if s in exclude:
                continue
            key = (self.suspicion.get(s, 0.0), -s)
            if best_key is None or key > best_key:
                best_key = key
                best = s
        return best

    def most_vocal(self, candidates, exclude=()):
        best = None
        best_key = None
        for s in candidates:
            if s in exclude:
                continue
            key = (self.speech_count.get(s, 0), -s)
            if best_key is None or key > best_key:
                best_key = key
                best = s
        return best

    def accused_me(self):
        return [a for (a, d) in self.accused.get(self.seat, [])]

    def seer_claim_alive(self):
        return [s for s in self.seer_claims if s in self.living and s != self.seat]


# --------------------------------------------------------------------------
# Night decisions
# --------------------------------------------------------------------------

def decide_kill(state, options):
    """Coordinate with the partner: match their pick, or pick the threat."""
    # partner already voted tonight?
    for v in state.wolf_votes:
        if v.get("night") == state.day and v.get("seat") == state.partner:
            tgt = v.get("target")
            if tgt in options:
                return tgt
    # threat score over living non-wolves
    best = None
    best_key = None
    for s in options:
        key = 0.0
        if s in state.seer_claims:
            key += 5.0
        key += 2.0 * state.speech_count.get(s, 0)
        if s in [a for (a, d) in state.accused.get(state.seat, [])]:
            key += 3.0
        # a player who voted a wolf out is dangerous
        for (day, voter, tgt) in state.votes:
            if voter == s and tgt is not None:
                for (eday, exiled, erole, _) in state.exiles:
                    if eday == day and exiled == tgt and erole == "werewolf":
                        key += 1.0
        key = (key, -s)
        if best_key is None or key > best_key:
            best_key = key
            best = s
    return best if best is not None else (options[0] if options else None)


def decide_protect(state, options):
    """Protect the seer claim if there is one, else the most useful villager."""
    if not options:
        return None
    last = state.last_protect.get("seat")
    for s in state.seer_claim_alive():
        if s in options and s != last:
            return s
    # most vocal living non-self player (not last protected)
    cands = [s for s in options if s != last]
    pick = state.most_vocal(cands)
    if pick is not None:
        return pick
    return options[0]


def decide_inspect(state, options):
    """Inspect the most suspicious un-inspected living player."""
    if not options:
        return None
    done = {r.get("seat") for r in state.seer_results}
    cands = [s for s in options if s not in done]
    if not cands:
        cands = list(options)
    pick = state.most_suspicious(cands)
    return pick if pick is not None else cands[0]


# --------------------------------------------------------------------------
# Day decisions
# --------------------------------------------------------------------------

def decide_vote(state, options):
    """Vote the most suspicious; as a wolf, join the bandwagon or save partner."""
    if not options:
        return None
    living = [s for s in options]
    if state.role == "werewolf":
        # save the partner: if they lead the tally, tie it with a villager
        if state.partner in state.today_tally:
            lead = max(state.today_tally.values())
            if state.today_tally.get(state.partner, 0) >= lead and lead >= 1:
                others = sorted(state.today_tally.items(),
                                key=lambda kv: (-kv[1], kv[0]))
                for (s, n) in others:
                    if s != state.partner and s in living:
                        return s
        # join the plurality against a villager
        if state.today_tally:
            top = sorted(state.today_tally.items(),
                         key=lambda kv: (-kv[1], kv[0]))
            for (s, n) in top:
                if s != state.partner and s in living:
                    return s
        # else vote the villager the table already suspects most
        pick = state.most_suspicious([s for s in living if s != state.partner])
        return pick if pick is not None else living[0]
    # village side
    if state.known_wolves:
        for s in state.known_wolves:
            if s in living:
                return s
    # a single credible seer claim naming a wolf: follow it
    if len(state.seer_claims) == 1:
        claimer = state.seer_claims[0]
        if claimer != state.seat:
            for tgt, accs in state.accused.items():
                if claimer in [a for (a, d) in accs] and tgt in living:
                    return tgt
    pick = state.most_suspicious(living)
    if pick is not None and state.suspicion.get(pick, 0) > 0:
        return pick
    # no signal: follow the plurality, else the lowest seat
    if state.today_tally:
        top = sorted(state.today_tally.items(), key=lambda kv: (-kv[1], kv[0]))
        return top[0][0]
    return living[0]


def decide_speak(state, max_chars):
    """One short, in-character statement, or a pass."""
    text = None
    if state.role == "werewolf":
        text = wolf_speak(state)
    elif state.role == "seer":
        text = seer_speak(state)
    elif state.role == "doctor":
        text = villager_speak(state)
    else:
        text = villager_speak(state)
    if text is None:
        return None
    text = fold(text)
    if not text:
        return None
    chars = list(text)
    if len(chars) > (max_chars or MAX_STATEMENT):
        text = "".join(chars[:max_chars or MAX_STATEMENT]).strip()
    return text or None


def wolf_speak(state):
    # a seer claim named me or my partner: push back on the claim
    for s in state.seer_claim_alive():
        for tgt, accs in state.accused.items():
            if s in [a for (a, d) in accs] and tgt in (state.seat, state.partner):
                nm = name_of(state, s)
                return ("I don't buy %s's claim. A real seer gives proof, and "
                        "they're pointing at the people who questioned them."
                        % nm)
    # the village already suspects a villager: agree and add a little
    if state.today_tally:
        top = sorted(state.today_tally.items(), key=lambda kv: (-kv[1], kv[0]))
        tgt = top[0][0]
        if tgt != state.partner and tgt != state.seat:
            nm = name_of(state, tgt)
            return ("I agree with the read on %s. Their story kept changing "
                    "and their votes don't line up." % nm)
    # someone accused me: deflect onto the accuser, calmly
    acc = state.accused_me()
    if acc:
        nm = name_of(state, acc[0])
        return ("I'm not a wolf, %s. You're pushing hard on me while the "
                "real wolves stay quiet — look at who's saying nothing." % nm)
    # late game, no seer claim, partner or I under pressure: fake seer claim
    if state.day >= 3 and len(state.living) <= 5 and not state.seer_claims:
        cands = [s for s in state.living
                 if s not in (state.seat, state.partner)]
        if cands:
            tgt = state.most_vocal(cands) or cands[0]
            nm = name_of(state, tgt)
            return ("I'm the seer. I inspected %s last night and they're a "
                    "werewolf. Vote them out today." % nm)
    # otherwise blend in: ask a question, or note the quiet ones
    if state.round == 1:
        quiet = [s for s in state.living
                 if s != state.seat and state.speech_count.get(s, 0) == 0]
        if quiet:
            nm = name_of(state, quiet[0])
            return ("I don't have a strong read yet. %s has barely said "
                    "anything — what do you all make of that?" % nm)
        return "I don't have a strong read yet. Let's hear from everyone before we vote."
    pick = state.most_suspicious(
        [s for s in state.living if s not in (state.seat, state.partner)])
    if pick is not None and state.suspicion.get(pick, 0) > 0:
        nm = name_of(state, pick)
        return "I keep coming back to %s. Something about their answers feels off to me." % nm
    return None


def seer_speak(state):
    # found a living wolf: claim and name them with the night
    for r in state.seer_results:
        if r.get("werewolf") and r.get("seat") in state.living:
            nm = name_of(state, r["seat"])
            return ("I'm the seer. I inspected %s on night %d and they're a "
                    "werewolf. Vote %s out today." % (nm, r.get("night", 1), nm))
    # someone else claims seer and contradicts my clears: call it out
    if state.seer_claims:
        for s in state.seer_claims:
            if s == state.seat:
                continue
            for tgt, accs in state.accused.items():
                if s in [a for (a, d) in accs] and tgt in state.known_clear:
                    nm = name_of(state, s)
                    cm = name_of(state, tgt)
                    return ("%s claims to be the seer, but I checked %s and "
                            "they're clear. Only one of us is telling the "
                            "truth." % (nm, cm))
    # share clears once I have two, to steer the village
    if len(state.known_clear) >= 2 and state.day >= 2:
        a, b = state.known_clear[0], state.known_clear[1]
        return ("I'm the seer. I've checked %s and %s — both clear. Let's "
                "focus on the rest." % (name_of(state, a), name_of(state, b)))
    # day 1 with no wolf found: stay quiet
    if state.day == 1:
        return None
    # otherwise push the most suspicious
    pick = state.most_suspicious([s for s in state.living if s != state.seat])
    if pick is not None and state.suspicion.get(pick, 0) > 0:
        nm = name_of(state, pick)
        return "I have my eye on %s. Their votes and their story don't match up." % nm
    return None


def villager_speak(state):
    # two seer claims: one is a wolf — say so
    if len(state.seer_claims) >= 2:
        a, b = state.seer_claims[0], state.seer_claims[1]
        return ("Two people claim to be the seer: %s and %s. Only one can "
                "be right, and the other is almost certainly a wolf."
                % (name_of(state, a), name_of(state, b)))
    # a seer claim named a wolf: back it
    if len(state.seer_claims) == 1:
        claimer = state.seer_claims[0]
        if claimer != state.seat:
            for tgt, accs in state.accused.items():
                if claimer in [a for (a, d) in accs] and tgt in state.living:
                    nm = name_of(state, tgt)
                    return ("If %s is the seer, then %s is a wolf. That's the "
                            "clearest lead we have — I'll vote there."
                            % (name_of(state, claimer), nm))
    # a bad vote yesterday: a player exiled a villager
    for (eday, exiled, erole, _) in state.exiles:
        if erole == "villager":
            for (vday, voter, tgt) in state.votes:
                if vday == eday and tgt == exiled and voter in state.living:
                    nm = name_of(state, voter)
                    dm = name_of(state, exiled)
                    return ("%s pushed to exile %s yesterday and %s was a "
                            "villager. That vote is hard to explain."
                            % (nm, dm, dm))
    # round 2: push the most suspicious
    if state.round >= 2:
        pick = state.most_suspicious([s for s in state.living if s != state.seat])
        if pick is not None and state.suspicion.get(pick, 0) > 0:
            nm = name_of(state, pick)
            return "I keep coming back to %s. Their answers feel off to me." % nm
    # round 1: ask about the quiet ones, or pass
    quiet = [s for s in state.living
             if s != state.seat and state.speech_count.get(s, 0) == 0]
    if quiet and state.round == 1:
        nm = name_of(state, quiet[0])
        return ("I don't have a strong read yet. %s has barely spoken — "
                "what do you make of that?" % nm)
    return None


# --------------------------------------------------------------------------
# Decision dispatch
# --------------------------------------------------------------------------

def decide(pending):
    game = pending.get("game")
    kind = pending.get("decision")
    view = pending.get("view") or {}
    options = pending.get("options") or []
    try:
        state = State(view)
    except Exception:
        state = None
    if state is None or not state.alive:
        # dead or unreadable view: pass / abstain so nothing stalls
        if kind == "speak":
            return {"game": game, "decision": "speak", "pass": True}
        return {"game": game, "decision": kind, "target": None}
    try:
        if kind == "kill":
            tgt = decide_kill(state, options)
            return {"game": game, "decision": "kill",
                    "target": tgt if tgt in options else None}
        if kind == "protect":
            tgt = decide_protect(state, options)
            return {"game": game, "decision": "protect",
                    "target": tgt if tgt in options else None}
        if kind == "inspect":
            tgt = decide_inspect(state, options)
            return {"game": game, "decision": "inspect",
                    "target": tgt if tgt in options else None}
        if kind == "vote":
            tgt = decide_vote(state, options)
            return {"game": game, "decision": "vote",
                    "target": tgt if tgt in options else None}
        if kind == "speak":
            text = decide_speak(state, pending.get("max_chars") or MAX_STATEMENT)
            if text:
                return {"game": game, "decision": "speak", "text": text}
            return {"game": game, "decision": "speak", "pass": True}
    except Exception:
        pass
    if kind == "speak":
        return {"game": game, "decision": "speak", "pass": True}
    return {"game": game, "decision": kind, "target": None}


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    if not ENV_URL:
        raise SystemExit("MAKEFUN_ENV_URL is not set")
    while True:
        try:
            obs = post("/observe", {})
        except Exception:
            # env unreachable: retry once after a short pause is not allowed
            # (no clock); just retry the long-poll immediately.
            continue
        if not isinstance(obs, dict):
            continue
        if obs.get("done"):
            break
        pending = obs.get("pending") or []
        if not pending:
            continue
        for p in pending:
            try:
                body = decide(p)
            except Exception:
                kind = p.get("decision")
                if kind == "speak":
                    body = {"game": p.get("game"), "decision": "speak",
                            "pass": True}
                else:
                    body = {"game": p.get("game"), "decision": kind,
                            "target": None}
            act(p.get("game"), body["decision"],
                **{k: v for k, v in body.items()
                   if k not in ("game", "decision")})


if __name__ == "__main__":
    main()
