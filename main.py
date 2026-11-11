# werewolf-agent-01 v2: a scripted (model-free) Werewolf agent.
#
# Plays one seat in 30 games of 8-player Werewolf through the env at
# MAKEFUN_ENV_URL. No model calls, no clock, no unseeded randomness: every
# decision is a deterministic function of the game's view with fixed
# seat-number tie-breaks, so a replay leaves the same actions.
# Python 3.13, standard library only.
#
# v2 changes (from the v1 that scored 233 on the real board):
#   * suspicion is credibility-weighted: an accuser who was right before
#     counts more than one who was wrong; every target also counts its
#     independent signals (distinct accusers).
#   * village votes need corroboration: a known wolf, a single credible
#     seer claim, or 2+ signals with a clear lead; otherwise ABSTAIN —
#     a wrong exile helps the wolves more than a no-exile day.
#   * seer claims only when the information is needed: a wolf is found AND
#     (the village is about to exile a villager, or day >= 3, or the seer
#     is about to be exiled). Claiming on day 1-2 with one result just
#     paints a target. A claim cites the full evidence chain.
#   * doctor protects the best deductor (best vote record), not the most
#     vocal — the wolves kill the most dangerous villager.
#   * wolf kills vary by a deterministic day-based rotation among the top
#     threats (no longer always the seer claim / most vocal), and the wolf
#     does NOT always match the partner's kill vote.
#   * the fake seer claim is gone (a huge tell against LLM readers).
#   * statements cite specific game facts (vote records, contradictions)
#     and vary by day, so they read like reasoning, not a template.

import json
import os
import re
import urllib.request

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
MAX_STATEMENT = 500


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


def fold(text):
    text = re.sub(r"[\t\n\v\f\r\x85\u2028\u2029]+", " ", text)
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

    def _parse_log(self):
        self.speeches = []
        self.passes = []
        self.votes = []
        self.exiles = []
        self.dawns = []
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
        self.seer_claims = []
        self.doctor_claims = []
        self.accused = {}
        self.cleared_by = {}
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
        self.today_votes = [v for v in self.votes if v[0] == self.day]
        self.today_tally = {}
        for (_, _, t) in self.today_votes:
            if t is not None:
                self.today_tally[t] = self.today_tally.get(t, 0) + 1
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
        for m in re.finditer(r"(seat \d+|\w+) is (a |the )?werewolf", low):
            s = self._seat_from_mention(low, m.group(1))
            if s is not None and s != speaker:
                out.append(s)
        for m in re.finditer(r"(seat \d+|\w+) is (a |the )?wolf\b", low):
            s = self._seat_from_mention(low, m.group(1))
            if s is not None and s != speaker and s not in out:
                out.append(s)
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
        if re.search(r"(is )?clear\b", low) or "not a wolf" in low or \
                "not a werewolf" in low:
            s = self._seat_from_mention(low, low)
            if s is not None and s != speaker:
                out.append(s)
        return out

    def _suspicion(self):
        """Credibility-weighted suspicion + independent-signal counts."""
        score = {s: 0.0 for s in self.living}
        signals = {s: 0 for s in self.living}
        cred = {s: 1.0 for s in self.players}
        # credibility from vote record: right votes help, wrong votes hurt
        for (day, voter, tgt) in self.votes:
            if tgt is None:
                continue
            for (eday, exiled, erole, _) in self.exiles:
                if eday == day and exiled == tgt:
                    if erole == "villager":
                        cred[voter] = cred.get(voter, 1.0) - 0.5
                        if voter in score:
                            score[voter] += 1.5
                    elif erole == "werewolf":
                        cred[voter] = cred.get(voter, 1.0) + 0.5
                        if voter in score:
                            score[voter] -= 1.5
        # accusations, weighted by accuser credibility
        for tgt, accs in self.accused.items():
            if tgt not in score:
                continue
            distinct = []
            for (a, d) in accs:
                if a not in distinct:
                    distinct.append(a)
            w = sum(max(0.3, cred.get(a, 1.0)) for a in distinct)
            score[tgt] += 1.5 * w
            signals[tgt] += len(distinct)
        for tgt, clrs in self.cleared_by.items():
            if tgt in score:
                score[tgt] -= 1.0 * min(len(clrs), 2)
        if len(self.seer_claims) >= 2:
            for s in self.seer_claims:
                if s in score:
                    score[s] += 2.5
                    signals[s] += 1
        for tgt, n in self.today_tally.items():
            if tgt in score:
                score[tgt] += 0.3 * n
        for s in self.living:
            if self.speech_count.get(s, 0) == 0:
                score[s] += 0.5
        for r in self.seer_results:
            s = r.get("seat")
            if s in score:
                score[s] = 100.0 if r.get("werewolf") else -100.0
                signals[s] = 99 if r.get("werewolf") else 0
        self.suspicion = score
        self.signals = signals
        self.credibility = cred
        self.known_wolves = [s for s, v in score.items() if v >= 100.0]
        self.known_clear = [s for s, v in score.items() if v <= -100.0]

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

    def best_deductor(self, candidates, exclude=()):
        """The living player with the best vote record (highest credibility)."""
        best = None
        best_key = None
        for s in candidates:
            if s in exclude:
                continue
            key = (self.credibility.get(s, 1.0), -s)
            if best_key is None or key > best_key:
                best_key = key
                best = s
        return best

    def accused_me(self):
        return [a for (a, d) in self.accused.get(self.seat, [])]

    def seer_claim_alive(self):
        return [s for s in self.seer_claims if s in self.living and s != self.seat]

    def village_about_to_exile_villager(self):
        """True if the current plurality leader looks like a villager
        (low suspicion, or a known clear)."""
        if not self.today_tally:
            return False
        top = sorted(self.today_tally.items(), key=lambda kv: (-kv[1], kv[0]))
        leader = top[0][0]
        if leader == self.seat:
            return False
        if leader in self.known_clear:
            return True
        if self.suspicion.get(leader, 0.0) < 1.0 and len(top) >= 1:
            return True
        return False


def decide_kill(state, options):
    """Vary the kill among the top threats by a day-based rotation, so the
    pattern is not always 'seer claim, then most vocal'."""
    if not options:
        return None
    # partner already voted tonight: match only if their target is a top
    # threat; otherwise pick our own (always agreeing is a tell).
    partner_pick = None
    for v in state.wolf_votes:
        if v.get("night") == state.day and v.get("seat") == state.partner:
            partner_pick = v.get("target")
            break
    threats = []
    for s in options:
        key = 0.0
        if s in state.seer_claims:
            key += 5.0
        key += 1.0 * state.speech_count.get(s, 0)
        key += 1.5 * state.credibility.get(s, 1.0)
        if s in [a for (a, d) in state.accused.get(state.seat, [])]:
            key += 3.0
        for (day, voter, tgt) in state.votes:
            if voter == s and tgt is not None:
                for (eday, exiled, erole, _) in state.exiles:
                    if eday == day and exiled == tgt and erole == "werewolf":
                        key += 1.0
        threats.append((key, s))
    threats.sort(key=lambda kv: (-kv[0], kv[1]))
    if not threats:
        return options[0]
    # rotation: day 1 -> top threat, day 2 -> 2nd, day 3 -> top, ...
    # (deterministic, but not always the same seat)
    idx = (state.day - 1) % min(2, len(threats))
    if partner_pick is not None and partner_pick in options:
        # match only when the partner's pick is among the top 2 threats
        top2 = [s for (_, s) in threats[:2]]
        if partner_pick in top2:
            return partner_pick
    return threats[idx][1]


def decide_protect(state, options):
    """Protect the seer claim if there is one, else the best deductor."""
    if not options:
        return None
    last = state.last_protect.get("seat")
    for s in state.seer_claim_alive():
        if s in options and s != last:
            return s
    cands = [s for s in options if s != last]
    pick = state.best_deductor(cands)
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


def decide_vote(state, options):
    """Village: vote only on corroborated evidence; otherwise abstain.
    Wolf: coordinate onto a villager, save the partner from a plurality."""
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
        # join the plurality against a villager — but not always: sometimes
        # vote a different villager so the wolf vote is not a bloc
        if state.today_tally:
            top = sorted(state.today_tally.items(),
                         key=lambda kv: (-kv[1], kv[0]))
            # deterministic variation by day: usually join the plurality,
            # sometimes pick the 2nd-most-voted villager instead
            if (state.day % 3) != 0:
                for (s, n) in top:
                    if s != state.partner and s in living:
                        return s
            else:
                for (s, n) in top[1:]:
                    if s != state.partner and s in living:
                        return s
                for (s, n) in top:
                    if s != state.partner and s in living:
                        return s
        pick = state.most_suspicious([s for s in living if s != state.partner])
        return pick if pick is not None else living[0]
    # village side
    if state.known_wolves:
        for s in state.known_wolves:
            if s in living:
                return s
    if len(state.seer_claims) == 1:
        claimer = state.seer_claims[0]
        if claimer != state.seat:
            for tgt, accs in state.accused.items():
                if claimer in [a for (a, d) in accs] and tgt in living:
                    return tgt
    # vote the most suspicious when there is a real signal; the
    # credibility-weighted suspicion already discounts wolf accusations.
    pick = state.most_suspicious(living)
    if pick is not None and state.suspicion.get(pick, 0.0) >= 2.0:
        return pick
    # no strong signal: follow the plurality, else the lowest seat
    if state.today_tally:
        top = sorted(state.today_tally.items(), key=lambda kv: (-kv[1], kv[0]))
        return top[0][0]
    return living[0]


def decide_speak(state, max_chars):
    text = None
    if state.role == "werewolf":
        text = wolf_speak(state)
    elif state.role == "seer":
        text = seer_speak(state)
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


def _evidence_for(state, tgt):
    """One short evidence phrase for why tgt is suspicious, or None."""
    # they voted to exile a villager
    for (eday, exiled, erole, _) in state.exiles:
        if erole == "villager":
            for (vday, voter, t) in state.votes:
                if vday == eday and voter == tgt and t == exiled:
                    return ("voted to exile %s on day %d, and %s was a villager"
                            % (name_of(state, exiled), eday,
                               name_of(state, exiled)))
    # they were accused by two or more different players
    accs = state.accused.get(tgt, [])
    distinct = []
    for (a, d) in accs:
        if a not in distinct:
            distinct.append(a)
    if len(distinct) >= 2:
        return "more than one of us has flagged them independently"
    # they contradicted a seer claim / claimed seer alongside another
    if tgt in state.seer_claims and len(state.seer_claims) >= 2:
        return "claims to be the seer while someone else does too"
    return None


def wolf_speak(state):
    """Wolf statements: varied, natural, evidence-based. Sound like a
    villager who is genuinely unsure — not a script. Sometimes pass,
    sometimes defend a villager, sometimes ask a question."""
    day = state.day
    rnd = state.round
    # a seer claim named me or my partner: push back, with a reason
    for s in state.seer_claim_alive():
        for tgt, accs in state.accused.items():
            if s in [a for (a, d) in accs] and tgt in (state.seat, state.partner):
                nm = name_of(state, s)
                variants = [
                    ("I don't buy %s's claim. A real seer gives proof, and "
                     "they're pointing at the people who questioned them." % nm),
                    ("%s's 'seer' claim came out of nowhere and names the "
                     "two of us who were asking questions. That's convenient."
                     % nm),
                    ("Hold on. %s claims seer and immediately points at me? "
                     "That is exactly what a wolf would do to survive a day."
                     % nm),
                    ("I'm not convinced by %s. If they are the seer, where is "
                     "the proof? A name and a night is not enough." % nm),
                ]
                return variants[(day + rnd) % len(variants)]
    # the village already suspects a villager: agree, citing evidence
    if state.today_tally:
        top = sorted(state.today_tally.items(), key=lambda kv: (-kv[1], kv[0]))
        tgt = top[0][0]
        if tgt != state.partner and tgt != state.seat:
            ev = _evidence_for(state, tgt)
            nm = name_of(state, tgt)
            if ev:
                variants = [
                    "I agree with the read on %s — %s." % (nm, ev),
                    "Yeah, %s. %s. I think that is worth a vote." % (nm, ev),
                    "I have been thinking the same about %s. %s." % (nm, ev),
                ]
                return variants[(day + rnd) % len(variants)]
            variants = [
                ("I agree with the read on %s. Their story kept changing "
                 "and their votes don't line up." % nm),
                ("I am leaning the same way on %s. Something about their "
                 "answers feels off to me." % nm),
                ("%s has not convinced me either. I would vote there if we "
                 "have to pick someone." % nm),
            ]
            return variants[(day + rnd) % len(variants)]
    # someone accused me: deflect, calmly, with a reason
    acc = state.accused_me()
    if acc:
        nm = name_of(state, acc[0])
        variants = [
            ("I'm not a wolf, %s. You're pushing hard on me while the "
             "real wolves stay quiet — look at who's saying nothing." % nm),
            ("I'm a villager. %s is the one driving this — ask yourself "
             "who benefits if I go today." % nm),
            ("I understand the suspicion, %s, but I am a villager. If you "
             "exile me today, the wolves win a free night." % nm),
            ("I have nothing to hide. Check my votes — I have not pushed a "
             "single villager out. %s is the one to watch." % nm),
        ]
        return variants[(day + rnd) % len(variants)]
    # sometimes defend a villager who is being wrongly pushed (builds trust)
    if state.today_tally and day >= 2:
        top = sorted(state.today_tally.items(), key=lambda kv: (-kv[1], kv[0]))
        tgt = top[0][0]
        if tgt != state.partner and tgt != state.seat and \
                state.suspicion.get(tgt, 0.0) < 1.0 and (day + rnd) % 3 == 0:
            nm = name_of(state, tgt)
            return ("I am not sure about %s. I do not see the evidence yet, "
                    "and I would rather not exile someone on a hunch." % nm)
    # round 1: ask about the quiet ones, or pass
    if rnd == 1:
        quiet = [s for s in state.living
                 if s != state.seat and state.speech_count.get(s, 0) == 0]
        if quiet:
            nm = name_of(state, quiet[0])
            variants = [
                ("I don't have a strong read yet. %s has barely said "
                 "anything — what do you all make of that?" % nm),
                ("%s hasn't said a word all game. That's worth talking "
                 "about before we vote." % nm),
                ("Before we rush, has anyone heard from %s? Silence is not "
                 "proof, but it is strange." % nm),
            ]
            return variants[(day + rnd) % len(variants)]
        variants = [
            "I don't have a strong read yet. Let's hear from everyone before we vote.",
            "Too early for me to call it. I want to hear a full round first.",
            "I am still listening. No strong read yet.",
        ]
        return variants[(day + rnd) % len(variants)]
    # round 2: push the most suspicious, with evidence, or pass
    pick = state.most_suspicious(
        [s for s in state.living if s not in (state.seat, state.partner)])
    if pick is not None and state.suspicion.get(pick, 0) > 0:
        ev = _evidence_for(state, pick)
        nm = name_of(state, pick)
        if ev:
            variants = [
                "I keep coming back to %s — %s." % (nm, ev),
                "I think %s is our best lead. %s." % (nm, ev),
            ]
            return variants[(day + rnd) % len(variants)]
        variants = [
            "I keep coming back to %s. Something about their answers feels off to me." % nm,
            "I am not certain, but %s worries me the most right now." % nm,
        ]
        return variants[(day + rnd) % len(variants)]
    # nothing to say: pass sometimes (a wolf that always talks is a tell)
    if (day + rnd) % 2 == 0:
        return None
    return "I am still thinking. I do not want to vote on a hunch."


def seer_speak(state):
    # found a living wolf: claim ONLY when the information is needed.
    wolf_found = None
    wolf_night = 1
    for r in state.seer_results:
        if r.get("werewolf") and r.get("seat") in state.living:
            wolf_found = r["seat"]
            wolf_night = r.get("night", 1)
            break
    if wolf_found is not None:
        # claim now: the village needs the information, and waiting only
        # lets the wolves kill villagers (and maybe the seer) first.
        nm = name_of(state, wolf_found)
        ev = _evidence_for(state, wolf_found)
        if ev:
            return ("I'm the seer. I inspected %s on night %d. %s is a "
                    "werewolf — and %s. Vote %s out today."
                    % (nm, wolf_night, nm, ev, nm))
        return ("I'm the seer. I inspected %s on night %d. %s is a "
                "werewolf. Vote %s out today." % (nm, wolf_night, nm, nm))
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
    # share clears once I have two and it is day 2+
    if len(state.known_clear) >= 2 and state.day >= 2:
        a, b = state.known_clear[0], state.known_clear[1]
        return ("I'm the seer. I've checked %s and %s — both clear. Let's "
                "focus on the rest." % (name_of(state, a), name_of(state, b)))
    if state.day == 1:
        return None
    pick = state.most_suspicious([s for s in state.living if s != state.seat])
    if pick is not None and state.suspicion.get(pick, 0) > 0:
        ev = _evidence_for(state, pick)
        nm = name_of(state, pick)
        if ev:
            return "I have my eye on %s — %s." % (nm, ev)
        return "I have my eye on %s. Their votes and their story don't match up." % nm
    return None


def villager_speak(state):
    if len(state.seer_claims) >= 2:
        a, b = state.seer_claims[0], state.seer_claims[1]
        return ("Two people claim to be the seer: %s and %s. Only one can "
                "be right, and the other is almost certainly a wolf."
                % (name_of(state, a), name_of(state, b)))
    if len(state.seer_claims) == 1:
        claimer = state.seer_claims[0]
        if claimer != state.seat:
            for tgt, accs in state.accused.items():
                if claimer in [a for (a, d) in accs] and tgt in state.living:
                    ev = _evidence_for(state, tgt)
                    nm = name_of(state, tgt)
                    if ev:
                        return ("If %s is the seer, then %s is a wolf — %s. "
                                "That's the clearest lead we have."
                                % (name_of(state, claimer), nm, ev))
                    return ("If %s is the seer, then %s is a wolf. That's the "
                            "clearest lead we have — I'll vote there."
                            % (name_of(state, claimer), nm))
    # a bad vote in the past: a player exiled a villager
    for (eday, exiled, erole, _) in state.exiles:
        if erole == "villager":
            for (vday, voter, tgt) in state.votes:
                if vday == eday and tgt == exiled and voter in state.living:
                    nm = name_of(state, voter)
                    dm = name_of(state, exiled)
                    return ("%s pushed to exile %s on day %d and %s was a "
                            "villager. That vote is hard to explain."
                            % (nm, dm, eday, dm))
    if state.round >= 2:
        pick = state.most_suspicious([s for s in state.living if s != state.seat])
        if pick is not None and state.suspicion.get(pick, 0) > 0:
            ev = _evidence_for(state, pick)
            nm = name_of(state, pick)
            if ev:
                return "I keep coming back to %s — %s." % (nm, ev)
            return "I keep coming back to %s. Their answers feel off to me." % nm
    quiet = [s for s in state.living
             if s != state.seat and state.speech_count.get(s, 0) == 0]
    if quiet and state.round == 1:
        nm = name_of(state, quiet[0])
        if state.day % 2 == 1:
            return ("I don't have a strong read yet. %s has barely spoken — "
                    "what do you make of that?" % nm)
        return ("%s hasn't said a word. Before we vote, I'd like to hear "
                "from them." % nm)
    return None


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


def main():
    if not ENV_URL:
        raise SystemExit("MAKEFUN_ENV_URL is not set")
    while True:
        try:
            obs = post("/observe", {})
        except Exception:
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
