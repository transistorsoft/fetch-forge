#!/usr/bin/env python3
"""Re-score captures without re-running them (WO-026).

    summarize.py captures/capacitor/recreate-bold/FIX/run
    summarize.py --all                 # every capture under labs/captures
    summarize.py --metrics             # what a scenario's expect: block may assert
    summarize.py --coverage            # which SDK members the api scenarios assert (WO-027)
    summarize.py --coverage --leaves   # every State attribute: declared type, scope, set/typed/valued/round-tripped (Tier C)
                                       #   then every payload leaf, per type: typed/valued (Tier C, C4.0)
    summarize.py --coverage --args     # per member, the argument forms asked: conforming, settles-only, invalid (C4.0)
    summarize.py --replay-end [GRACE]  # would ending on evidence have changed a verdict? (WO-027)
                                       #   no GRACE: the per-step rule (labkit.Grace); a number: fixed
    summarize.py --replay-end --root labs/baselines/2026-09-23-4.7.0-release   # against a baseline
    summarize.py --replay-end --allow-cut          # also captures that already ended on evidence
    summarize.py --trace [paths]       # per step: what caused what, from the app's own lines (WO-027)
    summarize.py --trace-diff labs/baselines/<ref> [--root <new>]   # SAME / ORDER-CHANGED / SUMMARY-CHANGED
    summarize.py --board 20260925-140034           # where a sweep's lane seconds went, per phase (Tier C, C7.0)
    summarize.py --board <stamp> --vs <stamp>      # two boards side by side, and the difference
    summarize.py --divergence [VARIANT]            # the invalid-value matrix: leaf x kind x cell (Tier C, C4.4)
    summarize.py --pilot [paths]                   # per invalid_values capture: carried, or the log short? (C4.4)

Scoring is identical to a live run: the same metrics over the same trigger..capture window.
"""

import argparse
import base64
import glob
import re
import json
import os
import shutil
import statistics
import sys
import tempfile

import devices
import labkit


def score_one(path):
    run = os.path.join(path, "run.json")
    cap = labkit.Capture(path)
    meta = cap.meta
    label = meta.get("label", "?")
    print("== %s" % os.path.relpath(path, labkit.LABS))
    foreign = devices.contamination(cap.lines, label, meta.get("os", "android"), meta.get("package"))   # (WO-191)
    if foreign:
        print("   CONTAMINATED by %s => FAIL" % ", ".join(foreign))
        return False
    if not meta.get("scenario"):
        print("   (no run.json — showing metrics only)")
        for name, fn in sorted(labkit.METRICS.items()):
            print("   %-32s %s" % (name, fn(cap)))
        return True
    # (Tier C, C4.4) A capture of a scenario since retired (null-args' leftover logcat, C4.3) answers a spec that is gone:
    # skipped and said so, never a traceback that stops --all at the first one.
    if not labkit.scenario_exists(meta["scenario"]):
        print("   RETIRED: scenario %s has no labs/harness/scenarios/%s.yaml — a leftover capture, skipped"
              % (meta["scenario"], meta["scenario"]))
        return True
    try:
        scen = labkit.scenario(meta["scenario"])
    except SystemExit as e:
        # (Tier C, C4.4 review) Its file is there but refuses to load — invalid.yaml or roundtrip.yaml refusing the whole
        # table once the types gain a leaf: named and counted a failure, never a stop for every capture after it.
        print("   UNLOADABLE: %s — skipped" % e)
        return False
    step = next((s for s in scen["steps"] if s["name"] == meta.get("step")), None)
    if step is None and meta.get("step") is not None:
        # (Tier C, C4.4 review) A step since renamed or retired: never scored against the scenario's first step.
        print("   RETIRED: step %s is no longer in scenario %s — a leftover capture, skipped" % (meta["step"], scen["name"]))
        return True
    step = step or scen["steps"][0]           # a capture from before run.json named its step
    expect = labkit.expectations(step, meta["platform"], meta.get("os", "android"))
    if not expect:
        print("   (no expectations)")
        return True
    var = labkit.variant(meta["variant"])
    return labkit.report(labkit.score(cap, expect),
                         inverted=labkit.must_fail(var, scen["name"], step["name"]))


def replay_end(paths, grace_s, allow_cut=False):
    """(WO-027) Would ending each step on its evidence have changed a single metric?  For every
    FULL-WINDOW FIX capture whose step qualifies (labkit.end_policy), find where the host would have
    ended it — run.py's rule: evidence complete, a grace, then a tick first logged AFTER the grace
    and newer than the newest tick at its end — cut the log there, re-score, and compare every row.
    `grace_s` None replays the per-step rule (labkit.Grace, and run.py's second read at the liveness
    tick where the driver ran its per-tick fix — run.json `driver.tickGcp`, on when absent); a number
    replays a fixed grace of that many seconds.

    Limits, stated so nobody over-reads a clean result: it needs full-window captures (a run given an
    explicit --settle/--after keeps the window; a capture that already ended on evidence is counted
    `already-cut` and not compared — unless `allow_cut`, when it is replayed against the log it has and
    a replayed end past that log's end is `undecidable`); the dumpsys check cannot be replayed and is
    taken as agreeing; and
    it can say nothing about `crashes` while no capture on disk contains an app crash — the grace is
    sized from the SDK's own timers (Android's foreground-service deadline, iOS's watchPosition error
    timer and geofence evaluation), not from this."""
    tally = {"same": 0, "changed": 0, "full-window": 0, "already-cut": 0, "not-eligible": 0}
    if allow_cut:
        tally.update({"cut-same": 0, "cut-changed": 0, "undecidable": 0})
    saved, saved_cut, rules, assumed = [], [], {}, 0
    for path in paths:
        cap = labkit.Capture(path)
        meta = cap.meta
        if not meta.get("scenario") or meta.get("variant") != "FIX":
            tally["not-eligible"] += 1
            continue
        if not labkit.scenario_exists(meta["scenario"]):       # (Tier C, C4.4) as score_one: a retired scenario's
            tally["retired"] = tally.get("retired", 0) + 1     # leftover capture has no step to replay
            continue
        try:
            scen = labkit.scenario(meta["scenario"])
        except SystemExit:                                    # (Tier C, C4.4 review) as score_one: its file refuses
            tally["unloadable"] = tally.get("unloadable", 0) + 1
            continue
        step = next((st for st in scen["steps"] if st["name"] == meta.get("step")), None)
        if step is None and meta.get("step") is not None:     # (Tier C, C4.4 review) a step since renamed or retired
            tally["retired"] = tally.get("retired", 0) + 1
            continue
        step = step or scen["steps"][0]
        # (Tier C, C7.3) Today's rule: a non-final step ends on proof too, unless labkit.NONFINAL says hold.
        final_step = step is scen["steps"][-1] or labkit.NONFINAL == "end"
        # (WO-027) The step as it RAN: run.json keeps its hooks and events as written.  A hook added to
        # the YAML since would leave an old capture's evidence forever short of its count — and the
        # capture would drop, silently, into "full-window, identical by construction".
        if isinstance(meta.get("hooks"), list):
            step = dict(step, hooks=meta["hooks"], events=meta.get("events") or [])
        if "watch" in (meta.get("driver") or {}):
            step = dict(step, watch=meta["driver"]["watch"])
        if labkit.end_policy(step, meta["platform"]) != "evidence":
            tally["not-eligible"] += 1
            continue
        cut_already = (meta.get("end") or {}).get("ended") == "evidence"
        if cut_already and not allow_cut:
            tally["already-cut"] += 1          # not a full-window baseline any more
            continue
        # (WO-027) A capture the harness fails as contaminated is no evidence about the rule either.
        if devices.contamination(cap.lines, meta.get("label", "?"), meta.get("os", "android"), meta.get("package")):
            tally["contaminated"] = tally.get("contaminated", 0) + 1
            continue
        t_e = t_end = None
        k_g = 0                                # the app's ticks counted at the end of the grace
        seen = []
        if grace_s is None:
            # (WO-027) The per-step rule, decided by the SAME labkit.Grace run.py uses, fed the log one
            # second at a time — no look-ahead: a decision only ever sees the lines before it.  The
            # dumpsys check cannot be replayed; it is taken as agreeing.
            try:
                pkg = labkit.platform(meta["platform"], meta.get("os", "android"))["package"]
            except (KeyError, SystemExit):
                pkg = None
            device = meta.get("device")
            if not device and meta.get("os", "android") == "android":
                # A capture from before run.json kept the device: the lab emulator's (API 36, the 30 s
                # default) — not fgs_deadline_s's conservative 10 s, which would replay a 7 s floor
                # that no live run on that emulator applies.
                device, assumed = {"sdk": 36}, assumed + 1
            grace = labkit.Grace(step, meta.get("os", "android"), pkg, final_step=final_step,
                                 deadline_s=labkit.fgs_deadline_s(device))
            lines_ts = [(cap._ts(l), l) for l in cap.lines]
            t0 = next(t for t, _ in lines_ts if t is not None)
            t_last = max(t for t, _ in lines_ts if t is not None)
            i, g_alive, confirming, read_at = 0, None, False, -1
            now = (t0 // 1000) * 1000.0
            while now < (t_last // 1000 + 2) * 1000:
                while i < len(lines_ts) and (lines_ts[i][0] is None or lines_ts[i][0] <= now):
                    seen.append(lines_ts[i][1])
                    i += 1
                if i != read_at:                       # re-read only what a new line can change
                    ok, alive = labkit.evidence_state(seen, step, meta.get("os", "android"))
                    read_at = i
                t_e = (t_e or now) if ok else None
                end_at = grace.update(seen, now / 1000.0, t_e / 1000.0 if t_e else None)
                if end_at is None or now / 1000.0 < end_at:
                    g_alive, confirming = None, False
                elif g_alive is None:
                    g_alive = alive
                elif alive > g_alive:
                    # (WO-027, Tier B) Whether the driver ran its per-tick fix is what the capture
                    # recorded, never today's YAML: a capture from before B2 ran it everywhere.
                    if (grace.fixed is None and not grace.held and not confirming
                            and (meta.get("driver") or {}).get("tickGcp", True)):
                        confirming = True              # run.py reads the log once more, 0.5 s later
                        now += 500
                        continue
                    t_end = now
                    break
                now += labkit.POLL_S * 1000              # run.py's poll
            rules[grace.record()["rule"]] = rules.get(grace.record()["rule"], 0) + 1
        for line in ([] if grace_s is None else cap.lines):
            seen.append(line)
            norm = labkit.normalize(line)
            if labkit.TAG + " app=" not in norm and "point=dialog action=" not in norm:
                continue
            t = cap._ts(line)
            if t is None:
                continue
            is_tick = " app=tick " in norm or " app=beat " in norm
            if t_e is None:
                ok, _ = labkit.evidence_state(seen, step, meta.get("os", "android"))
                if ok:
                    t_e = t
            elif is_tick:
                _, alive = labkit.evidence_state(seen, step, meta.get("os", "android"))
                if t <= t_e + grace_s * 1000:
                    k_g = alive
                elif alive > k_g:                  # another lab app's tick does not count
                    t_end = t
                    break
        cap_t = cap._mark("point=capture", last=True)
        if t_end is None or t_end >= cap_t:
            # identical by construction: the window is not cut — or, for a capture that already
            # ended on evidence, the rule would have ended it later than the log it has
            tally["undecidable" if cut_already else "full-window"] += 1
            continue
        kept = [l for l in cap.lines if cap._ts(l) is None or cap._ts(l) <= t_end]
        kept = [l for l in kept if "point=trigger" not in l and "point=capture" not in l]
        stamp = next(labkit.TS.match(l).group(0) for l in reversed(kept) if labkit.TS.match(l))
        kept += ["%s I/LAB     ( 1): point=trigger %s" % (stamp, step["trigger"]),
                 "%s I/LAB     ( 1): point=capture" % stamp]
        tmp = tempfile.mkdtemp(prefix="replay-end-")
        try:
            with open(os.path.join(tmp, "logcat.txt"), "w") as f:
                f.write("\n".join(kept) + "\n")
            shutil.copy(os.path.join(path, "run.json"), tmp)
            short = labkit.Capture(tmp)
        finally:
            shutil.rmtree(tmp)
        expect = labkit.expectations(step, meta["platform"], meta.get("os", "android"))
        full = [(k, a) for k, a, _, _ in labkit.score(cap, expect)]
        if any(a == "spec-changed-since-capture" for _, a in full):
            # The YAML has moved on: this capture answers nothing now, so "same" would be vacuous.
            tally["spec-changed"] = tally.get("spec-changed", 0) + 1
            continue
        cut = [(k, a) for k, a, _, _ in labkit.score(short, expect)]
        (saved_cut if cut_already else saved).append((cap_t - t_end) / 1000.0)
        if full == cut:
            tally["cut-same" if cut_already else "same"] += 1
        else:
            tally["cut-changed" if cut_already else "changed"] += 1
            print("CHANGED  %s" % os.path.relpath(path, labkit.LABS))
            for (k, a), (_, b) in zip(full, cut):
                if a != b:
                    print("   %-20s full=%s  cut=%s" % (k, a, b))
    print("replay-end grace=%s: %s; window saved on the cut captures: %.0f s (mean %.1f s)%s%s%s"
          % ("per-step rule" if grace_s is None else "%ss" % grace_s,
             ", ".join("%s %d" % kv for kv in tally.items()), sum(saved),
             sum(saved) / len(saved) if saved else 0,
             "; on captures already cut live: %.0f s more (mean %.1f s)" % (sum(saved_cut), sum(saved_cut) / len(saved_cut))
             if saved_cut else "",
             "; rules: " + ", ".join("%s %d" % kv for kv in sorted(rules.items())) if rules else "",
             "; %d Android capture(s) with no device record replayed at the lab emulator's 30 s deadline"
             % assumed if assumed else ""))
    return tally["changed"] == 0 and tally.get("cut-changed", 0) == 0


# The SDK's line as it raises the OS location dialog: Android, iOS.  (WO-045) Android says "Requesting
# Background permission" when the request includes background location, as requestPermission's does.
DIALOG_RAISE = re.compile(r"LocationAuthorization: Requesting (?:Background )?permission|requestInitialAuthorization\]")
JITTER_MS = 50


def trace(path):
    """(WO-027) What caused what in one step, read from the app's OWN lines (labkit.app_pids): the
    order its api hooks completed; where each event's first delivery landed — `boot` (before any
    hook completed) or after which hook, with the gaps to that hook and to the next; which driver
    line preceded the SDK raising the OS permission dialog; and the dialog actions.  A speed change
    that moves a cause — the boot fix no longer raising the dialog, an event no longer riding the
    batch — shows up here even when every metric row stays the same."""
    cap = labkit.Capture(path)
    own = labkit.app_pids(cap.lines)
    foreign = devices.contamination(cap.lines, cap.meta.get("label", "?"), cap.meta.get("os", "android"),
                                    cap.meta.get("package"))   # (WO-191)
    api, events, dialog, actions, last_api, last_app = [], {}, None, [], None, None
    # (Tier C, C5.0) The host's moves and actions are causes too, from the moment each send began, on the device's clock
    # (labkit.sent_on_device): the mark itself lands after the send returns, so a delivery can precede it in the log.
    host = sorted((labkit.sent_on_device(l, n, cap.year), "move%s.%s" % (cap.field(n, "i"), cap.field(n, "hop").split("/")[0])
                   if " point=move " in n else "action%s" % cap.field(n, "i"))
                  for l, n in ((l, cap.norm[l]) for l in cap.lines)
                  if re.search(r"%s point=(move|host) i=\d+ .*\bsent_ms=\d+" % labkit.TAG, n) and cap._ts(l))
    for line in cap.lines:
        t, norm = cap._ts(line), cap.norm[line]
        if labkit.TAG + " point=dialog action=" in norm:
            actions.append(cap.field(norm, "action"))
            continue
        if dialog is None and labkit.line_pid(line) in own and DIALOG_RAISE.search(line):
            dialog = {"after": last_app and last_app[1], "t": t,
                      "gap_ms": round(t - last_app[0]) if last_app and t else None}
            continue
        if labkit.line_pid(line) not in own or labkit.TAG + " app=" not in norm:
            continue
        m = re.search(r"app=([\w-]+)(=\S+)?", norm)
        kind, ident = m.group(1) + (m.group(2) or ""), cap.field(norm, "id", None)
        if kind == "api" and ident:
            api.append((ident, t))
            last_api = (t, ident)
        elif kind == "event" and ident and ident not in events:
            cause = max([c for c in (last_api, max((h for h in host if t and h[0] <= t), default=None)) if c],
                        key=lambda c: c[0], default=None)
            events[ident] = {"after": cause[1] if cause else "boot", "t": t,
                             "gap_ms": round(t - cause[0]) if cause else None}
        if kind not in ("ret", "beat"):      # a beat is a heartbeat, never a cause
            last_app = (t, kind + (" " + ident if ident else ""))
    for ev in events.values():
        nxt = next((ta for _, ta in api if ta > ev["t"]), None)
        ev["next_ms"] = round(nxt - ev["t"]) if nxt else None
    return {"api": api, "events": events, "dialog": dialog, "actions": actions, "own": own,
            "contaminated": foreign, "host": host}


def _crossed(t_r, t_n, hooks_r, hooks_n):
    """The hook a point moved across between two runs — before it in one, after it in the other —
    unless it lies within JITTER_MS of that hook in both; else None."""
    for h in sorted(set(hooks_r) & set(hooks_n), key=lambda h: hooks_r[h] or 0):   # the first one crossed
        if None in (t_r, t_n, hooks_r[h], hooks_n[h]):
            continue
        if (hooks_r[h] > t_r) != (hooks_n[h] > t_n) and \
                (abs(hooks_r[h] - t_r) >= JITTER_MS or abs(hooks_n[h] - t_n) >= JITTER_MS):
            return h
    return None


def _trace_changes(ref, new):
    """How two traces of the same step differ in cause and order, jitter (JITTER_MS) forgiven:
    every comparison is pairwise against the hooks both runs completed, like the api order."""
    out = []
    t_ref, t_new = dict(ref["api"]), dict(new["api"])
    common = [i for i, _ in new["api"] if i in t_ref]
    for a in range(len(common)):
        for b in range(a + 1, len(common)):
            x, y = common[a], common[b]
            if (t_ref[x] > t_ref[y]) != (t_new[x] > t_new[y]) and \
                    (abs(t_ref[x] - t_ref[y]) >= JITTER_MS or abs(t_new[x] - t_new[y]) >= JITTER_MS):
                out.append("api order: %s / %s" % (x, y))
    if {i for i, _ in ref["api"]} != {i for i, _ in new["api"]}:
        out.append("api hooks completed: %s -> %s" % (sorted(t_ref), sorted(t_new)))
    # (C5.0 review) An event or the dialog moving across a host send is a changed cause too.
    c_ref = dict(t_ref, **{h: t for t, h in ref.get("host") or []})
    c_new = dict(t_new, **{h: t for t, h in new.get("host") or []})
    for ident in sorted(set(ref["events"]) | set(new["events"])):
        r, n = ref["events"].get(ident), new["events"].get(ident)
        if (r is None) != (n is None):
            out.append("event %s: %s" % (ident, "no longer delivered" if n is None else "newly delivered"))
        elif r is not None:
            h = _crossed(r["t"], n["t"], c_ref, c_new)
            if h:
                out.append("event %s first delivered after %s -> after %s (moved across %s)"
                           % (ident, r["after"], n["after"], h))
    rd, nd = ref["dialog"], new["dialog"]
    if (rd is None) != (nd is None):
        out.append("dialog %s" % ("no longer raised" if nd is None else "newly raised"))
    elif rd is not None:
        h = _crossed(rd["t"], nd["t"], c_ref, c_new)
        if h:
            out.append("dialog raised after %s -> after %s (moved across %s)" % (rd["after"], nd["after"], h))
    if ref["actions"] != new["actions"]:
        out.append("dialog actions %s -> %s" % (ref["actions"], new["actions"]))
    if [h for _, h in ref.get("host") or []] != [h for _, h in new.get("host") or []]:      # (Tier C, C5.0)
        out.append("host sends %s -> %s" % ([h for _, h in ref.get("host") or []], [h for _, h in new.get("host") or []]))
    return out


def trace_diff(ref_root, new_root):
    """(WO-027) Every step captured under both roots: SUMMARY-CHANGED when summary.txt differs,
    ORDER-CHANGED when a cause or an order moved (trace), else SAME."""
    rel = lambda root: {os.path.relpath(os.path.dirname(p), root) for p in
                        glob.glob(os.path.join(root, "*", "*", "*", "*", "logcat.txt"))}
    both = sorted(rel(ref_root) & rel(new_root))
    if not both:
        print("trace-diff: no step captured under both %s and %s" % (ref_root, new_root), file=sys.stderr)
        sys.exit(2)
    tally = {"SAME": 0, "ORDER-CHANGED": 0, "SUMMARY-CHANGED": 0, "CONTAMINATED": 0, "NO-TRACE": 0}
    for r in both:
        a, b = os.path.join(ref_root, r), os.path.join(new_root, r)
        ta, tb = trace(a), trace(b)
        # A contaminated step is never SAME: its summary.txt may predate the check, and its trace
        # hides the stray it was contaminated by.
        if ta["contaminated"] or tb["contaminated"]:
            tally["CONTAMINATED"] += 1
            print("CONTAMINATED     %s  (%s)" % (r, "; ".join(ta["contaminated"] + tb["contaminated"])))
            continue
        if not ta["own"] or not tb["own"]:
            tally["NO-TRACE"] += 1                 # no app=boot: nothing of the app's to compare
            print("NO-TRACE         %s" % r)
            continue
        read = lambda d: open(os.path.join(d, "summary.txt")).read() if os.path.exists(os.path.join(d, "summary.txt")) else None
        if read(a) != read(b):
            tally["SUMMARY-CHANGED"] += 1
            print("SUMMARY-CHANGED  %s" % r)
            continue
        changes = _trace_changes(ta, tb)
        if changes:
            tally["ORDER-CHANGED"] += 1
            print("ORDER-CHANGED    %s" % r)
            for c in changes:
                print("   " + c)
        else:
            tally["SAME"] += 1
    print("trace-diff %s -> %s: %s (%d only under one root)"
          % (os.path.relpath(ref_root), os.path.relpath(new_root), ", ".join("%s %d" % kv for kv in tally.items()),
             len(rel(ref_root) ^ rel(new_root))))
    return tally["SAME"] == len(both)


def called_member(vocab, hook):
    """(Tier C, R3) The member of a property's interface a dotted hook CALLS, as the vocabulary's `members:` names
    it (`logger.getLog` is Logger.getLog), or None: an undotted hook, a probe, a name the types do not declare."""
    head, dot, tail = str(hook.get("api")).partition(".")
    if not dot or hook.get("probe") or labkit.api_member(hook["api"]) is None:
        return None
    return "%s.%s" % ((vocab.get("api") or {})[head]["returns"], tail)


def coverage():
    """The SDK vocabulary against every scenario's `api` hooks: what is asserted, what is only
    probed, and what nothing asks about.  Without this, "the conformance suite passes" means
    only "the methods someone wrote a hook for pass" (WO-027)."""
    invoked, probed, subscribed, configured = {}, {}, {}, set()
    armed, nested = {}, {}                  # (Tier C, C5.0) arm_only subscriptions; members reached below a property
    called = {}                             # (Tier C, R3) ... and those a dotted hook CALLS (`logger.getLog`)
    headless = {}                           # (Tier C, C6.0) headlessEvents.<name> / headlessFired.<name> -> scenarios
    vocab = labkit.vocabulary()
    config_vocab = vocab.get("config") or {}

    def keys(table, decl, prefix=""):
        # Recurse only where the VOCABULARY declares a group: `http.headers` is declared as an
        # opaque record, so a scenario's {X-Lab: "1"} counts as `http.headers`, not a leaf below it.
        for k, v in (table or {}).items():
            if isinstance(v, dict) and isinstance(decl.get(k), dict):
                keys(v, decl[k], prefix + k + ".")
            else:
                configured.add(prefix + k)
    for path in sorted(glob.glob(os.path.join(labkit.HARNESS, "scenarios", "*.yaml"))):
        scen = labkit.scenario(os.path.basename(path)[:-5])
        for step in scen["steps"]:
            keys(step.get("config"), config_vocab)
            # (Tier C, C6.0) The headless names a step asserts — delivered to the task, or fired by the SDK.
            for block in [step.get("expect") or {}] + list((step.get("platform_expect") or {}).values()):
                for key, want in (block or {}).items():
                    if key.startswith(("headlessEvents.", "headlessFired.")) and want != "any":
                        headless.setdefault(key, set()).add(scen["name"])
            for hook in step["hooks"]:
                if "api" in hook:
                    (probed if hook.get("probe") else invoked).setdefault(hook["api"], set()).add(scen["name"])
                    # (Tier C, C5.0) A probe of `logger` / `deviceSettings` asking `$.<member>` reaches that member.
                    owner = ((vocab.get("api") or {}).get(hook["api"]) or {}).get("returns")
                    for path in (hook.get("returns") or {}) if hook.get("probe") and owner in (vocab.get("members") or {}) \
                            else ():
                        if str(path).count(".") == 1:
                            nested.setdefault("%s.%s" % (owner, str(path)[2:]), set()).add(scen["name"])
                    if called_member(vocab, hook):
                        called.setdefault(called_member(vocab, hook), set()).add(scen["name"])
                    member = (vocab.get("api") or {}).get(hook["api"]) or {}
                    declared = member.get("args") or []
                    # (Tier C, C4.0) Not one carrying `invalid:`: a key sent a value the types forbid is not exercised.
                    if declared and labkit.param(declared[0])[0] == "config" and isinstance(hook.get("args"), dict) \
                            and "invalid" not in hook:
                        keys(hook["args"], config_vocab)        # a Config passed as an argument
            # (WO-027) An `events:` declaration covers a member a hook cannot reach: the 15 `on*`
            # and watchPosition take a callback, so they are subscribed, never invoked.
            for decl in step.get("events") or []:
                # (Tier C, C5.0) An `arm_only` declaration asserts the subscription, not a delivery: counted apart.
                (armed if "arm_only" in decl else subscribed).setdefault(decl["subscribe"], set()).add(scen["name"])
    api = vocab.get("api") or {}
    methods = [n for n, m in api.items() if not n.startswith("on") and not (m or {}).get("property")]
    props = [n for n, m in api.items() if (m or {}).get("property")]
    events = [n for n in api if n.startswith("on")]
    # A member reached ONLY through a subscription is covered, and by a different mechanism — say
    # so, rather than counting it as uncovered or silently folding it into `invoked`.
    covered = set(invoked) | set(probed) | set(subscribed)
    print("== methods: %d invoked, %d probed only, %d subscribed, %d uncovered of %d; "
          "properties: %d probed of %d; events: %d subscribed of %d%s"
          % (len(set(methods) & set(invoked)), len(set(methods) & set(probed) - set(invoked)),
             len(set(methods) & set(subscribed) - set(invoked) - set(probed)),
             len(set(methods) - covered), len(methods),
             len(set(props) & set(probed)), len(props),
             len(set(events) & set(subscribed)), len(events),
             # (Tier C, C5.0) and those only armed, apart: the subscription asserted, no delivery
             ", %d armed only" % len(set(events) & set(armed) - set(subscribed)) if set(armed) - set(subscribed) else ""))
    for name in sorted(methods) + sorted(props) + sorted(events):
        how = ("invoked  " if name in invoked else "probed   " if name in probed
               else "subscribed" if name in subscribed else "arm-only " if name in armed else "-        ")
        by = ", ".join(sorted(invoked.get(name, set()) | probed.get(name, set())
                              | subscribed.get(name, set()) | armed.get(name, set())))
        print("   %-9s %-40s %s" % (how, name, by))
    # (Tier C, C5.0) The members of what `logger` and `deviceSettings` return (vocabulary `members:`): probed where a
    # probe of the property asks `$.<member>` (C5.1's `declared: members`); invoked by a dotted hook after the rebuild.
    owned = sorted("%s.%s" % (owner, name) for owner, table in (vocab.get("members") or {}).items() for name in table or {})
    # (Tier C, R3) A member a dotted hook calls is invoked; one only read below a probe of its property, probed.
    print("== members of properties: %d invoked, %d probed only of %d"
          % (len(set(owned) & set(called)), len(set(owned) & set(nested) - set(called)), len(owned)))
    for name in owned:
        print("   %-9s %-40s %s" % ("invoked  " if name in called else "probed   " if name in nested else "-        ",
                                    name, ", ".join(sorted(called.get(name, set()) | nested.get(name, set())))))
    total = []
    def count(table, prefix=""):
        for k, v in (table or {}).items():
            if isinstance(v, dict):
                count(v, prefix + k + ".")
            else:
                total.append(prefix + k)
    count(vocab.get("config"))
    print("== config keys exercised by a scenario: %d of %d" % (len(configured & set(total)), len(total)))
    for key in sorted(configured & set(total)):
        print("   %s" % key)
    # (Tier C, C6.0) What reaches the headless task (Android), per name: asserted delivered (the task's own line),
    # asserted fired (the SDK's), or both — a count of 0 is an assertion too.
    names = labkit.headless_names()
    delivered = {n for n in names if "headlessEvents." + n in headless}
    fired = {n for n in names if "headlessFired." + n in headless}
    print("== headless names (Android): %d asserted delivered, %d asserted fired, of %d"
          % (len(delivered), len(fired), len(names)))
    for name in names:
        how = ("both     " if name in delivered and name in fired else "delivered" if name in delivered
               else "fired    " if name in fired else "-        ")
        by = headless.get("headlessEvents." + name, set()) | headless.get("headlessFired." + name, set())
        print("   %-9s %-40s %s" % (how, name, ", ".join(sorted(by))))


def coverage_leaves():
    """(Tier C, C0) Every attribute a State carries — Config's compound keys minus State's Omit list,
    plus State's own keys — with its declared type, wire type and OS scope, and which scenarios SET it
    (config: or a Config argument), TYPE it (`type:` or an implied type) or VALUE it (equals, gte …).
    The mechanical answer to "is each attribute tested for its datatype?".  (Tier C, C3) `same:` and
    `differs:` value nothing: they compare with an earlier reading.  ROUND-TRIPPED: in one step, a hook
    sets the attribute through a Config argument, a hook asks for a value it set (`equals`, on the
    attribute or a level down), and a later reset() that does not set it asks for that same path back:
    `same:` a hook that read it before the set, `equals` another value, or, a level down, `absent`.
    A Record's or a list's own type proves nothing, and neither does the value set asked for again.
    (Tier C, C4.0) Then the same for every payload a member returns or emits (the vocabulary's `types:`):
    each leaf once, under the type that declares it — `Coords.latitude` however a path reaches it — and
    which scenarios type it (a `declared:` expansion counts) or value it; `optional`/`under` value nothing.
    (Tier C, C4.2) Nor does `record: true`, which reads a value and judges none — on State or a payload."""
    vocab = labkit.vocabulary()
    api = vocab.get("api") or {}
    scope = {k: os_ for os_ in ("android", "ios") for k in ((vocab.get("scope") or {}).get(os_) or [])}
    leaves = {}
    def walk(node, prefix=""):
        for k, v in node.items():
            walk(v, prefix + k + ".") if isinstance(v, dict) else leaves.__setitem__(prefix + k, v)
    walk(labkit.state_tree())
    setby, typed, valued, tripped = {}, {}, {}, {}
    # (Tier C, C4.0) Every leaf a declared return or emit reaches, walked from each root as _declare expands
    # it: an interface's fields, an inline object's, an array's canonical `.0`.  `reach` maps (root, path) to
    # the leaf — `Type.field`, `?` as the vocabulary spells it — so a scenario's path is counted where it lands.
    boxes = (vocab.get("types") or {}).get("interfaces") or {}
    fields, reach, ptyped, pvalued = {}, {}, {}, {}
    def expand(root, ts, leaf, path):
        element = None if isinstance(ts, dict) else labkit._array_element(ts)
        name = leaf if isinstance(ts, dict) else next((p for p in labkit._alternatives(element or ts) if p in boxes),
                                                      None)
        box = ts if isinstance(ts, dict) else boxes.get(name)
        if box is None:
            if leaf:
                fields[leaf], reach[(root, path)] = ts, leaf
            return
        for field, sub in box.items():
            if field != "[key: string]":
                expand(root, sub, "%s.%s" % (name.rstrip("?"), field),
                       path + (".0" if element else "") + "." + field.rstrip("?"))
    # (R3) and every payload a dotted member returns (DeviceSettingsRequest), which only a dotted call reaches
    owned = [m for table in (vocab.get("members") or {}).values() for m in (table or {}).values()]
    for ts in sorted({str((m or {}).get(k)) for m in list(api.values()) + owned for k in ("returns", "emits")}):
        expand(ts, ts, None, "$")
    def configured(table, prefix=""):
        for k, v in (table or {}).items():
            if isinstance(v, dict) and (prefix + k) not in leaves:
                yield from configured(v, prefix + k + ".")
            elif prefix + k in leaves:
                yield prefix + k
    def owner(path):
        """The attribute a path asks about: itself, or the one it is a level down in (`.c3`, `.length`)."""
        leaf = str(path)[2:]
        while leaf not in leaves and "." in leaf:
            leaf = leaf.rsplit(".", 1)[0]
        return leaf if leaf in leaves else None
    for path in sorted(glob.glob(os.path.join(labkit.HARNESS, "scenarios", "*.yaml"))):
        scen = labkit.scenario(os.path.basename(path)[:-5])
        for step in scen["steps"]:
            for leaf in configured(step.get("config")):
                setby.setdefault(leaf, set()).add(scen["name"])
            asks = [("%s#%d" % (h["api"], i), h, labkit.api_member(h["api"]) or {}, "returns")       # (R3) dotted too
                    for i, h in enumerate(step["hooks"]) if "api" in h]
            asks += [("%s@%d" % (d["subscribe"], i), d, api.get(d["subscribe"]) or {}, "emits")
                     for i, d in enumerate(step.get("events") or [])]
            since = {}              # (Tier C, C3) leaf -> (the hook that set it, {path: the value asked for there})
            for ident, h, member, key in asks:
                sets = set()
                # (Tier C, C4.0) A set carrying `invalid:` sets nothing: the key was sent a value the types forbid (WO-059's
                # `desiredAccuracy: null`), and what the State reads back after it is valued, never round-tripped.
                if key == "returns" and labkit.param((member.get("args") or [""])[0])[0] == "config" \
                        and isinstance(h.get("args"), dict) and "invalid" not in h:
                    sets = set(configured(h["args"]))
                    for leaf in sets:
                        setby.setdefault(leaf, set()).add(scen["name"])
                        since[leaf] = (int(ident.split("#")[1]), {})
                # (Tier C, C4.0) A payload path lands on a leaf (`.N` read as the canonical `.0`) or on nothing: a
                # container, `.length`, `$err`, a key below a Record.  A probe reports typeOf(member), no payload.
                for p, c in (h.get("returns") or {}).items() if not h.get("probe") else ():
                    leaf = reach.get((str(member.get(key)), re.sub(r"\.\d+(?=\.|$)", ".0", str(p))))
                    if leaf and ("type" in (c or {}) or labkit._implied(c or {})):
                        ptyped.setdefault(leaf, set()).add("%s:%s" % (scen["name"], ident))
                    if leaf and set(c or {}) - {"type", "absent", labkit.RECORD} - set(labkit.REFS) - set(labkit.QUALIFIERS) \
                            - _whole_in(c or {}, p, str(member.get(key))):
                        pvalued.setdefault(leaf, set()).add("%s:%s" % (scen["name"], ident))
                if str(member.get(key)) != "State":
                    continue
                for p, checks in (h.get("returns") or {}).items():
                    who, c = "%s:%s" % (scen["name"], ident), checks or {}
                    mine = owner(p) if key == "returns" else None
                    if mine in since and h["api"] == "reset" and mine not in sets:   # a restore, not the check
                        at, asked = since[mine]
                        back = (_before(c.get("same"), at) or "equals" in c and c["equals"] != asked.get(str(p))
                                or c.get("absent") and str(p)[2:] != mine)
                        if str(p) in asked and back:
                            tripped.setdefault(mine, set()).add(who)
                    elif mine in since and "equals" in c:
                        since[mine][1][str(p)] = c["equals"]
                    leaf = str(p)[2:]
                    if leaf not in leaves:
                        continue
                    if "type" in c or labkit._implied(c):
                        typed.setdefault(leaf, set()).add(who)
                    if set(c) - {"type", "absent", labkit.RECORD} - set(labkit.REFS) - set(labkit.QUALIFIERS) \
                            - _whole_in(c, p, None):
                        valued.setdefault(leaf, set()).add(who)
    n = len(leaves)
    print("== State attributes: %d (%d both OSes, %d Android-only, %d iOS-only) — set %d, typed %d, valued %d, "
          "round-tripped %d, never asserted %d"
          % (n, n - len(scope), sum(v == "android" for v in scope.values()), sum(v == "ios" for v in scope.values()),
             len(setby), len(typed), len(valued), len(tripped), len(set(leaves) - set(typed) - set(valued))))
    aw, dw = max(map(len, leaves)), max(len(str(t)) for t in leaves.values())
    row = "   %-*s %-*s %-13s %-8s %-4s %-5s %-13s %s"
    print(row % (aw, "attribute", dw, "declared", "wire", "scope", "set", "typed", "round-tripped", "by"))
    for leaf in sorted(leaves):
        print(row % (
            aw, leaf, dw, leaves[leaf], "|".join(sorted(labkit.ts_wire(leaves[leaf]))), scope.get(leaf, "both"),
            "yes" if leaf in setby else "-", "value" if leaf in valued else "type" if leaf in typed else "-",
            "yes" if leaf in tripped else "-", ", ".join(sorted(typed.get(leaf, set()) | valued.get(leaf, set())))[:90]))
    owners = sorted({leaf.split(".")[0] for leaf in fields})
    print("== payload leaves (Tier C, C4.0): %d in %d types — typed %d, valued %d, never asserted %d"
          % (len(fields), len(owners), len(ptyped), len(pvalued), len(set(fields) - set(ptyped) - set(pvalued))))
    aw, dw = max(map(len, fields)), max(len(str(t)) for t in fields.values())
    print("   %-*s %6s %6s %6s" % (aw, "type", "leaves", "typed", "valued"))
    for name in owners:
        mine = {leaf for leaf in fields if leaf.split(".")[0] == name}
        print("   %-*s %6d %6d %6d" % (aw, name, len(mine), len(mine & set(ptyped)), len(mine & set(pvalued))))
    row = "   %-*s %-*s %-13s %-5s %s"
    print(row % (aw, "leaf", dw, "declared", "wire", "typed", "by"))
    omitted = (vocab.get("types") or {}).get("omit") or {}
    def wire(leaf):
        """`-` for a leaf `omit:` dates because no wire carries its declared type (lastSeenAt, a `Date`)."""
        try:
            return "|".join(sorted(labkit.ts_wire(fields[leaf])))
        except SystemExit:
            if leaf.rstrip("?") not in omitted:
                raise
            return "-"
    for leaf in sorted(fields):
        print(row % (aw, leaf, dw, fields[leaf], wire(leaf),
                     "value" if leaf in pvalued else "type" if leaf in ptyped else "-",
                     ", ".join(sorted(ptyped.get(leaf, set()) | pvalued.get(leaf, set())))[:90]))


def _whole_in(checks, path, root):
    """(Tier C, C5.1) {"in"} where a path's `in:` lists every value its closed type has — what `declared:` generates, a
    type check (S2) — so it values nothing; set() where it is narrower, or there is none."""
    if "in" not in checks:
        return set()
    leaf = labkit._state_leaf(str(path), None if root in (None, "State") else root)
    return {"in"} if isinstance(leaf, str) and labkit.ts_values(leaf) == set(checks["in"]) else set()


def coverage_args():
    """(Tier C, C4.0) Per member that takes an argument, the forms the scenarios ask of it.  A conforming form
    reads by its shape — an object by its keys, an array by its elements', a closed type's value as itself,
    any other scalar by its wire type, `()` for none — and a hook marked `settles_only:` is counted apart, as
    the lab cannot see that argument take effect.  A form carrying `invalid:` reads by the kind of departure
    labkit found (_conform's reason): omitted (a required argument or field), null, wrong type, out of range
    (a value a closed type does not list) or unknown (a key the type does not declare), and where."""
    api = dict(labkit.vocabulary().get("api") or {})
    # (Tier C, R3; R's review) And the members a dotted hook calls: `logger.getLog`, under the name a hook writes.
    for prop, owner in list(api.items()):
        if (owner or {}).get("property"):
            for name, m in ((labkit.vocabulary().get("members") or {}).get(str(owner.get("returns"))) or {}).items():
                api["%s.%s" % (prop, name)] = m
    takes = {n: labkit.param(m["args"][0]) for n, m in api.items() if (m or {}).get("args")
             and not m.get("property") and not labkit.CALLBACK_ARG.search(labkit.param(m["args"][0])[0])}
    forms = {}                      # (member, class, form) -> {scenario}

    def shape(value, ts):
        if isinstance(value, dict):
            return "{%s}" % ",".join(sorted(value))
        if isinstance(value, list):
            return "[%s]" % "|".join(sorted({shape(v, labkit._array_element(ts) or "") for v in value}))
        return (json.dumps(value) if labkit.ts_values(ts) is not None else "null" if value is None else
                "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "string")
    for path in sorted(glob.glob(os.path.join(labkit.HARNESS, "scenarios", "*.yaml"))):
        scen = labkit.scenario(os.path.basename(path)[:-5])
        for step in scen["steps"]:
            asks = [(h["api"], h) for h in step["hooks"] if h.get("api") in takes and not h.get("probe")]
            asks += [(d["subscribe"], d) for d in step.get("events") or [] if d["subscribe"] in takes and "args" in d]
            for name, h in asks:
                ts = takes[name][2]
                if "invalid" not in h:
                    form = shape(h["args"], ts) if "args" in h else "()"
                elif "args" not in h:
                    form = "omitted ()"             # labkit loads it only where the argument is required
                else:
                    # labkit loaded it only because the call departs from the types, so the same _conform names
                    # the departure, and where: `args.geolocation.desiredAccuracy is null — …`.
                    why = labkit._conform(h["args"], ts, "args")
                    kind = ("unknown" if "declares no such field" in why else "null" if " is null — " in why
                            else "out of range" if " is none of " in why else "omitted" if " leaves out `" in why
                            else "wrong type")
                    form = "%s %s" % (kind, re.split(r"[: ]", why)[0] + ("." + why.split("`")[1] if kind == "omitted"
                                                                         else ""))
                cls = "invalid" if "invalid" in h else "settles" if "settles_only" in h else "asked"
                forms.setdefault((name, cls, form), set()).add(scen["name"])
    classes = ("asked", "settles", "invalid")
    print("== argument forms (Tier C, C4.0): %d members take an argument, %d asked — %d conforming forms, "
          "%d settles-only, %d invalid"
          % (len(takes), len({name for name, _, _ in forms}), *(sum(c == cls for _, c, _ in forms) for cls in classes)))
    mw, fw = max(map(len, takes)), max([len(f) for _, _, f in forms] + [4])
    print("   %-*s %-8s %-*s %s" % (mw, "member", "class", fw, "form", "by"))
    for name in sorted(takes):
        rows = sorted(((cls, form) for n, cls, form in forms if n == name), key=lambda r: (classes.index(r[0]), r[1]))
        for cls, form in rows or [("-", "")]:
            print(("   %-*s %-8s %-*s %s" % (mw, name, cls, fw, form,
                                          ", ".join(sorted(forms.get((name, cls, form), ()))))).rstrip())


# (Tier C, C4.4) The matrix's columns: native.md's eight cells, the four Android bridges then the same four on iOS.
DIV_CELLS = [(p, o) for o in ("android", "ios") for p in ("capacitor", "cordova", "flutter", "react-native")]
DIV_NAMES = {"capacitor": "cap", "cordova": "cor", "flutter": "fl", "react-native": "rn"}
# A reading longer than this is cut for its column, `…` marking the cut — never `~`, which is the driver's or the
# transport's (a ret with trunc=1, or one cut before its parent=).  10.100000381469727 fits.  (Tier C, C4.4 review)
# `≠` compares the readings themselves, never the clipped text.
DIV_CLIP = 20


def _div_same(ret, other):
    """(Tier C, C4.4) Whether two readings are alike: the type, then a scalar's value — numerically, as `same:` compares
    them (Flutter prints 5 on one OS and 5.0 on the other); a container or null by its type."""
    if ret is None or other is None or ret["type"] != other["type"]:
        return False
    if ret["type"] == "number":
        try:
            return float(ret["value"]) == float(other["value"])
        except (TypeError, ValueError):
            return False
    return ret["type"] not in ("string", "boolean") or ret["value"] == other["value"]


def _div_sent(ret, sent):
    """(Tier C, C4.4) Whether a reading is the value a set sent, as JSON typed it (an empty {} or [] by its type)."""
    kind = ("null" if sent is None else "boolean" if isinstance(sent, bool) else "number"
            if isinstance(sent, (int, float)) else "object" if isinstance(sent, dict) else "array"
            if isinstance(sent, list) else "string")
    return ret is not None and _div_same(ret, {"type": kind, "value": json.dumps(sent) if kind != "string" else sent})


def _div_reading(ret):
    """(Tier C, C4.4 review) A reading as `≠` compares it, unclipped: (type, value) — a number as a float, so Flutter's
    -1.0 is -1 —, or (type,) for a container or null."""
    if ret["type"] == "number":
        try:
            return "number", float(ret["value"])
        except (TypeError, ValueError):
            return "number", ret["value"]
    return (ret["type"], ret["value"]) if ret["type"] in ("string", "boolean") else (ret["type"],)


def _div_cell(ret, sent=labkit._ABSENT, prime=None, base=None, outcome=None):
    """(Tier C, C4.4) One cell of the matrix, as (shown, reading).  Shown: what the driver reported — a number as a
    number (-1.0 reads -1), a string quoted, a container or null by its type — tagged with the first earlier reading it
    equals: [=] the value sent, [=i] the value sent but for case (a string: iOS canonicalises strategy, method and the
    enums), [p] the prime's, [b] the baseline's.  Equalities, stated; never a verdict.  With nothing reported: `rej`
    where the call's `api id=` line says the bridge rejected the set, the driver's own outcome where it names another
    (`unexpressible`, `absent`), and `?` where it logged none — a death, a hang, a lost line.  The reading is what `≠`
    compares (_div_reading); None for `?`."""
    if ret is None:
        if outcome == "rejected":
            return "rej", ("rej",)
        if outcome not in (None, "ok"):
            return outcome[:DIV_CLIP], ("outcome", outcome)
        return "?", None
    value = ret["value"]
    if ret["type"] == "number":
        try:
            value = "%d" % float(value) if float(value).is_integer() else value
        except (TypeError, ValueError, OverflowError):
            pass
    shown = (json.dumps(value) if ret["type"] == "string" else value if ret["type"] in ("number", "boolean")
             else ret["type"])
    shown = shown if len(shown) <= DIV_CLIP else shown[:DIV_CLIP - 1] + "…"      # a column stays readable
    asked = sent is not labkit._ABSENT
    tag = ("=" if asked and _div_sent(ret, sent) else
           "=i" if asked and isinstance(sent, str) and ret["type"] == "string" and ret["value"].lower() == sent.lower()
           else "p" if _div_same(ret, prime) else "b" if _div_same(ret, base) else "")
    return shown + ("[%s]" % tag if tag else "") + ("~" if ret.get("trunc") else ""), _div_reading(ret)


def divergence(variant_name="FIX", root=os.path.join(labkit.LABS, "captures")):
    """(Tier C, C4.4) The invalid-value matrix the C4 plan's §5 C4.4 hands to Chris: per generated set — its leaf, its
    kind, the value it sent — what each of the eight cells STORED, then every other `record: true` reading of those
    scenarios (a set's coupled leaf under its set; the Option B probes).  Read from the captures alone — run.json's
    hooks (their `sets:` and `kind:`), the `api id=` outcome lines and the ret lines — so a scenario edited or retired
    since reads as it ran.  A cell is tagged with the first earlier reading of its own step it equals (_div_cell): [=]
    the value sent, [=i] the same but for case, [p] the prime's, [b] the baseline's; a string reads quoted, so "5" is
    never 5; `rej` is a set the bridge rejected.  `≠` marks a row whose cells do not all read alike, compared unclipped.
    A contaminated capture is left out and named.  Nothing is judged: the rulings are decisions 5-10's (C4.5)."""
    caps = []
    for run in sorted(glob.glob(os.path.join(root, "*", "*", variant_name, "*", "run.json"))):
        try:
            with open(run) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        caps.append((os.path.dirname(run), meta))
    scenarios = sorted({m.get("scenario") for _, m in caps if any("kind" in h for h in m.get("hooks") or [])})
    if not scenarios:
        print("no capture under %s/*/*/%s carries an invalid_values set (Tier C, C4.4)" % (root, variant_name),
              file=sys.stderr)
        return 1
    leaves = labkit.config_leaves()
    rows, extra, skipped = {}, {}, []     # key -> {cell: (shown, reading)}; rows also -> (rank, subrows)
    for path, meta in caps:
        if meta.get("scenario") not in scenarios:
            continue
        cell = (meta.get("platform"), meta.get("os", "android"))
        cap = labkit.Capture(path)
        if devices.contamination(cap.lines, meta.get("label", "?"), cell[1], meta.get("package")):   # (WO-191)
            skipped.append(os.path.relpath(path, labkit.LABS))
            continue
        hooks, rets, calls = meta.get("hooks") or [], labkit._rets(cap), {}
        for line in cap.app("api id=", window=False):         # (Tier C, C4.4 review) a rejection reads `rej`, not `?`
            calls.setdefault(cap.field(line, "id"), cap.field(line, "outcome"))
        base_id = next(("getState#%d" % i for i, h in enumerate(hooks) if h.get("api") == "getState"), None)
        for i, h in enumerate(hooks):
            ident = "%s#%d" % (h.get("api"), i)
            recorded = [p for p, c in (h.get("returns") or {}).items() if (c or {}).get(labkit.RECORD)]
            if "kind" in h:
                path_ = "$." + h["sets"]
                prime = hooks[i - 1] if i and hooks[i - 1].get("sets") == h["sets"] and "kind" not in hooks[i - 1] else None
                node = h.get("args")
                for seg in h["sets"].split("."):
                    node = node.get(seg, labkit._ABSENT) if isinstance(node, dict) else labkit._ABSENT
                sent = node
                key = (meta["scenario"], h["sets"], h["kind"], json.dumps(sent))
                row = rows.setdefault(key, {"rank": (leaves.index(h["sets"]) if h["sets"] in leaves else len(leaves),
                                                     labkit.IV_KINDS.index(h["kind"]) if h["kind"] in labkit.IV_KINDS
                                                     else 99, i), "cells": {}, "subs": {}})
                row["cells"][cell] = _div_cell(rets.get((ident, path_)), sent,
                                               rets.get(("setConfig#%d" % (i - 1), path_)) if prime else None,
                                               rets.get((base_id, path_)) if base_id else None, calls.get(ident))
                for p in recorded:
                    if p != path_:
                        row["subs"].setdefault(p, {})[cell] = _div_cell(
                            rets.get((ident, p)), base=rets.get((base_id, p)) if base_id else None,
                            outcome=calls.get(ident))
            elif recorded:
                for p in recorded:
                    key = (meta["scenario"], meta.get("step"), i, ident, json.dumps(h.get("args")) if "args" in h else "",
                           p)
                    extra.setdefault(key, {})[cell] = _div_cell(rets.get((ident, p)), outcome=calls.get(ident))

    def line(mark, label, cells, width):
        return ("  %s %-*s %s" % (mark, width, label, "  ".join(
            "%-*s" % (w, cells[c][0] if c in cells else ".") for c, w in zip(DIV_CELLS, widths)))).rstrip()

    def mark(cells):
        readings = [reading for _, reading in cells.values()]
        return "≠" if None in readings or len(set(readings)) > 1 else " "
    heads = ["%s-%s" % (DIV_NAMES.get(p, p), "A" if o == "android" else "i") for p, o in DIV_CELLS]
    every = [r["cells"] for r in rows.values()] + [s for r in rows.values() for s in r["subs"].values()] + \
        list(extra.values())
    widths = [max([len(h)] + [len(c[cell][0]) for c in every if cell in c]) for h, cell in zip(heads, DIV_CELLS)]
    diverge = sum(mark(r["cells"]) != " " for r in rows.values())
    print("== invalid values (Tier C, C4.4), variant %s: %d sets in %s, %d of them read differently across the cells"
          % (variant_name, len(rows), ", ".join(scenarios), diverge))
    print("   a cell is what its State read, tagged with the first earlier reading of its step it equals: [=] the value "
          "sent, [=i] the value sent but for case, [p] the prime's, [b] the baseline's — equalities, not verdicts")
    print("   rej the bridge rejected the set (the next set's neighbours then read unreported-ref); threw it threw as it "
          "was called (R5); ? no reading and no "
          "outcome (a death, a hang, a lost line); … clipped here; ~ cut by the driver or the transport; . no capture")
    for scen in scenarios:
        mine = sorted(((k, r) for k, r in rows.items() if k[0] == scen), key=lambda kr: kr[1]["rank"])
        width = max([len("%s %s %s" % k[1:]) for k, _ in mine] + [40])
        print("\n   %s" % scen)
        print(line(" ", "leaf  kind  sent", {c: (h, None) for c, h in zip(DIV_CELLS, heads)}, width))
        for (s_, leaf, kind, sent), r in mine:
            print(line(mark(r["cells"]), "%s %s %s" % (leaf, kind, sent), r["cells"], width))
            for p, cells in sorted(r["subs"].items()):
                print(line(mark(cells), "  + %s" % p, cells, width))
        others = sorted((k, v) for k, v in extra.items() if k[0] == scen)
        if others:
            print("   ... and every other recorded reading of %s" % scen)
            owidth = max(len("%s %s %s %s" % (k[1], k[3], k[4], k[5])) for k, _ in others)
            for (s_, step, _, ident, args, p), cells in others:
                print(line(mark(cells), "%s %s %s %s" % (step, ident, args, p), cells, owidth))
    if skipped:
        print("\n   contaminated, left out: %s" % ", ".join(skipped))
    return 0


# (Tier C, C4.4 review) The pilot's readings, README §5: the transport's evidence kept apart from the log's.
PILOT_READINGS = ("CARRIED", "LOG SHORT", "NOT CARRIED", "DIED", "HEAD LOST", "CONTAMINATED")


def enum_scan(root):
    """(Tier C, C5.1, harness item 13) Every `in:` the current YAML asks — generated on each closed leaf of a `declared:`
    expansion (S2), or written — held against the value each FIX capture already reported at that path, before any
    board.  A re-score cannot do it: the comparators come from run.json, and a hook whose checks changed reads
    `spec-changed-since-capture`.  A hook is matched by its index and member, an event by its index and member; a path
    the capture never reported, or reported with another wire type (a type red, the type check's), is not a reading
    of the value set.  Prints each red, then the tally; exit 1 on any red."""
    reds, held, skipped, unreadable = [], 0, [], []
    for run in sorted(glob.glob(os.path.join(root, "*", "*", "FIX", "*", "run.json"))):
        path = os.path.dirname(run)
        try:
            meta = json.load(open(run))
        except (OSError, ValueError):
            unreadable.append(os.path.relpath(path, root))
            continue
        if not meta.get("scenario") or not labkit.scenario_exists(meta["scenario"]):
            continue
        try:
            steps = labkit.scenario(meta["scenario"])["steps"]
        except SystemExit:
            skipped.append(os.path.relpath(path, root))
            continue
        step = next((s for s in steps if s["name"] == meta.get("step")), None)
        asked = [("%s#%d" % (h["api"], i), h, (meta.get("hooks") or [])[i:i + 1]) for i, h in enumerate(step["hooks"])
                 if "api" in h] if step else []
        asked += [("%s@%d" % (d["subscribe"], i), d, (meta.get("events") or [])[i:i + 1])
                  for i, d in enumerate(step["events"])] if step else []
        asked = [(ident, entry) for ident, entry, then in asked if then and (then[0].get("api") or then[0].get("subscribe"))
                 == (entry.get("api") or entry.get("subscribe")) and any("in" in (c or {}) for c in
                                                                        (entry.get("returns") or {}).values())]
        if not asked:
            continue
        # (C5.1 review) logcat.txt is gitignored (labs/.gitignore): a worktree or a clone has run.json alone, and a scan
        # that skipped them would read clean.  Named, and the scan fails.
        try:
            rets = labkit._rets(labkit.Capture(path))
        except (OSError, SystemExit) as e:
            unreadable.append("%s (%s)" % (os.path.relpath(path, root), type(e).__name__))
            continue
        for ident, entry in asked:
            mine = labkit._delivery(rets, entry, ident)          # (Tier C, R4) a declaration's `delivery`'s lines
            for p, c in entry["returns"].items():
                ret = mine.get((ident, str(p)))
                kinds = {"number" if isinstance(v, (int, float)) else "string" for v in c.get("in") or []}
                if "in" not in c or ret is None or ret["type"] not in kinds:
                    continue
                held += 1
                why = labkit._check(ret, {"in": c["in"]})
                if why:
                    reds.append("%s  %s.%s=%s" % (os.path.relpath(path, root), ident, p, why))
    for red in reds:
        print("   RED  %s" % red)
    print("== in-scan: %d readings held to their value sets, %d red%s%s" % (
        held, len(reds), "; %d capture(s) whose scenario no longer loads: %s" % (len(skipped), ", ".join(skipped))
        if skipped else "", "; %d capture(s) UNREADABLE — no logcat.txt (it is gitignored: point --root at the checkout "
                            "that ran them): %s" % (len(unreadable), ", ".join(unreadable[:5]) + (" …" if len(unreadable) > 5
                                                                                             else "")) if unreadable else ""))
    return 1 if reds or unreadable else 0


def pilot_one(path):
    """(Tier C, C4.4 review) What one capture of an `invalid_values:` step says about CARRYING its hooks, kept apart from
    what it says about the LOG that recorded the answers — a lost line read as a transport failure points at the wrong
    fix (IV_STEP_LIMIT instead of log capacity), and the reverse would call an unproven size proven.  The whole log, as
    apiAssertions reads it.  Returns (reading, facts); the first that holds:
        CONTAMINATED  another run's lines are in the log (devices.contamination), as score_one fails it
        HEAD LOST     the log's head is gone, this run's launch mark with it: Android's `logcat -d` ring buffer
                      overflowed (`adb logcat -g`) — log capacity, not the transport
        DIED          the app crashed (`crashes`): `stopped` names the first hook with no `api id=` line — read the report
        NOT CARRIED   no crash, and `api-done n=` is missing or short of the hooks sent: the transport, or a hang
        LOG SHORT     carried (`api-done n=` equals the hooks sent), yet an `api id=` or a ret line is missing: log
                      capacity — the Android ring buffer, the iOS stream — never IV_STEP_LIMIT
        CARRIED       every hook carried, and every call and path it asked logged: the transport holds at this size
    The facts: hooksB64 (wire_hook over run.json's hooks, as launch_payloads builds it), the hooks sent, the driver's
    `api-done n=`, calls and rets logged against those asked, and the first hook with no `api id=` line."""
    with open(os.path.join(path, "run.json")) as f:
        meta = json.load(f)
    with open(os.path.join(path, "logcat.txt"), errors="replace") as f:
        cap = labkit.Capture.live(f.read().splitlines(), meta)
    hooks = meta.get("hooks") or []
    sent = [(i, h) for i, h in enumerate(hooks) if "api" in h]
    done = cap.app("api-done ", window=False)
    ran = cap.field(done[-1], "n") if done else "-"
    calls = {cap.field(l, "id") for l in cap.app("api id=", window=False)}
    rets = labkit._rets(cap)
    asked = ["%s#%d" % (h["api"], i) for i, h in sent]
    paths = {("%s#%d" % (h["api"], i), str(p)) for i, h in sent for p in (h.get("returns") or {})}
    facts = {"hooksB64": len(base64.b64encode(json.dumps([labkit.wire_hook(h) for h in hooks], allow_nan=False).encode())),
             "hooks": len(sent), "api-done": ran, "calls": "%d/%d" % (len(calls & set(asked)), len(asked)),
             "rets": "%d/%d" % (len(paths & set(rets)), len(paths)),
             "stopped": next((a for a in asked if a not in calls), "-")}
    foreign = devices.contamination(cap.lines, meta.get("label", "?"), meta.get("os", "android"),
                                    meta.get("package"))   # (WO-191)
    head = [f for f in foreign if f.startswith("log head lost")]
    reading = ("CONTAMINATED" if foreign and not head else "HEAD LOST" if head else
               "DIED" if labkit.METRICS["crashes"](cap) else "NOT CARRIED" if ran != str(len(sent)) else
               "LOG SHORT" if set(asked) - calls or paths - set(rets) else "CARRIED")
    return reading, facts


def pilot(paths):
    """(Tier C, C4.4 review) `--pilot`: pilot_one per capture, one line each; 0 when every one reads CARRIED."""
    print("== invalid-value pilot (Tier C, C4.4): did the transport carry each step, apart from whether the log kept "
          "its answers (README §5)")
    readings = []
    for path in paths:
        try:
            reading, facts = pilot_one(path)
        except (OSError, ValueError) as e:
            reading, facts = "UNREADABLE", {"why": str(e)}
        readings.append(reading)
        print("   %-12s %s" % (reading, os.path.relpath(path, labkit.LABS)))
        print("                hooksB64 %s B  hooks %s  api-done %s  calls %s  rets %s  stopped %s"
              % tuple(facts.get(k, "-") for k in ("hooksB64", "hooks", "api-done", "calls", "rets", "stopped"))
              if "why" not in facts else "                %s" % facts["why"])
    return 0 if readings and all(r == "CARRIED" for r in readings) else 1


def _before(ref, index):
    """(Tier C, C3) A `same:` reference to a hook that ran before hook `index` — a reading from before the
    set, so matching it is a restore, not a value merely held."""
    m = labkit.REF_ID.match(ref) if isinstance(ref, str) else None
    return bool(m) and int(m.group(2)) < index


# (Tier C, C7.0) A lane's seconds in the order they pass: each step's six phases, t0 to its last
# boundary, then what lies between steps and between runs.  Contiguous, so the rows sum to the lane.
BOARD_ROWS = [("setup", "t0 -> launch"),
              ("window", "launch -> evidence (-> end, a step that ran its window)"),
              ("hold", "evidence -> end"),
              ("capture", "end -> run_json"),
              ("verdict", "run_json -> settled/snapshot"),
              ("teardown", "-> terminated_post"),
              ("run head", "run start -> first step: spawn, claim, preflight, install"),
              ("between steps", "a step's last phase -> the next step's t0"),
              ("run tail", "last step -> run end: run.py's exit, outcome()"),
              ("untimed", "an ERROR/TIMEOUT run past its last step, or steps with no phases"),
              ("between runs", "lane start -> run -> run -> lane end: RESULT, board.json, uninstall")]


def load_board(ref):
    """(Tier C, C7.0) A sweep's board.json, named by its stamp (20260925-140034), its directory or
    the file itself.  A sweep from before C7.0 has none, and is refused rather than guessed at."""
    path = ref
    if not os.path.exists(path):
        path = os.path.join(labkit.HARNESS, ".claims", ref if ref.startswith("sweep-") else "sweep-" + ref)
    if os.path.isdir(path):
        if not os.path.exists(os.path.join(path, "board.json")):
            print("%s has no board.json: a sweep from before Tier C C7.0 recorded no build, lane or step "
                  "timings, only cell logs, so it has no phase budget" % path, file=sys.stderr)
            sys.exit(2)
        path = os.path.join(path, "board.json")
    if not os.path.exists(path):
        print("no sweep %s (a stamp under %s, a sweep directory, or a board.json)"
              % (ref, os.path.join(labkit.HARNESS, ".claims")), file=sys.stderr)
        sys.exit(2)
    return json.load(open(path))


def lane_budget(board, os_name, serial=None):
    """(Tier C, C7.0) Where one OS lane's seconds went, from board.json alone: {row: [seconds, one
    per step, gap or run]} over BOARD_ROWS, the lane's span, every timed step with its phases, each
    boundary's seconds since the one before it (with each dialog window, and the device-log stamps
    against the reads that saw them), and the device calls summed.  A sweep killed before its lane
    ended runs to its last finished run.

    (Tier C, C7.7) An OS with two devices: each device's lane is budgeted on its own (`between runs`
    is a gap in ONE device's queue), then pooled — rows, steps, intervals and calls over all the
    OS's runs — so `lane` is device-seconds, the device lanes summed, and `span` the OS's first start
    to its last end, its share of the wall.  `devices`: each device's lane, platforms and end."""
    span = (board.get("lanes") or {}).get(os_name) or {}
    devices = span.get("devices") or {}
    if serial is None and len(devices) > 1:
        each = {s: lane_budget(board, os_name, s) for s in devices}
        parts = [p for p in each.values() if p]
        if not parts:
            return None
        pooled = {"rows": {name: [x for p in parts for x in p["rows"][name]] for name, _ in BOARD_ROWS},
                  "lane": sum(p["lane"] for p in parts), "runs": [r for p in parts for r in p["runs"]],
                  "steps": [st for p in parts for st in p["steps"]], "calls": {}, "intervals": {},
                  "untimed": sum(p["untimed"] for p in parts), "partial": sum(p["partial"] for p in parts),
                  "killed": span.get("end") is None,
                  # (Tier C, C7.7) A device that finished no timed run (its builds failed, it died first, its
                  # group was all n/a) is still one of the OS's devices: listed, with no lane
                  "devices": {s: each[s]["devices"][s] if each[s] else
                              {"lane": 0.0, "none": True, "killed": d.get("end") is None,
                               "platforms": d.get("platforms") or []} for s, d in devices.items()}}
        for p in parts:
            for name, v in p["intervals"].items():
                pooled["intervals"].setdefault(name, []).extend(v)
            for method, c in p["calls"].items():
                pooled["calls"][method] = [x + y for x, y in zip(pooled["calls"].get(method, [0, 0.0, 0, 0.0]), c)]
        start = span["start"] if span.get("start") is not None else min(r["start"] for r in pooled["runs"])
        end = span["end"] if span.get("end") is not None else max(   # a device that did end counts
            [r["end"] for r in pooled["runs"]] + [d["end"] for d in devices.values() if d.get("end") is not None])
        pooled["span"] = end - start
        return pooled
    if serial is not None:
        span = devices[serial]
    runs = sorted((r for r in board.get("runs") or [] if r.get("os") == os_name and r.get("start") is not None
                   and (serial is None or r.get("serial") == serial)), key=lambda r: r["start"])
    if not runs:
        return None
    start = span["start"] if span.get("start") is not None else runs[0]["start"]
    end = span["end"] if span.get("end") is not None else runs[-1]["end"]
    rows = {name: [] for name, _ in BOARD_ROWS}
    steps, calls, untimed, partial, seq, extra = [], {}, 0, 0, {}, {}
    prev = start
    for r in runs:
        rows["between runs"].append(r["start"] - prev)
        at = r["start"]
        timed = sorted((s for s in r.get("steps") or []
                        if (s.get("timings") or {}).get("phases") and s["timings"].get("t0") is not None),
                       key=lambda s: s["timings"]["t0"])
        untimed += len(r.get("steps") or []) - len(timed)
        for i, s in enumerate(timed):
            t, ph = s["timings"], s["timings"]["phases"]
            rows["between steps" if i else "run head"].append(t["t0"] - at)
            # Each phase ends at the first boundary named that the step passed, else where the one
            # before it ended: whatever a step skipped, its six phases sum to its last boundary.  A
            # step that ran its whole window has no hold, even where its evidence came (no liveness
            # line after the grace).  One whose run.json re-write failed stops at db_pulled.
            ended, grace = (s.get("end") or {}).get("ended"), (s.get("end") or {}).get("grace") or {}
            partial += "run_json" not in ph
            last, phases = 0.0, {}
            for name, b in (("setup", ph.get("launch")),
                            ("window", ph.get("evidence") if ended == "evidence" else ph.get("end")),
                            ("hold", ph.get("end")), ("capture", ph.get("run_json", ph.get("db_pulled"))),
                            ("verdict", ph.get("snapshot", ph.get("settled"))), ("teardown", max(ph.values()))):
                b = last if b is None else b
                phases[name], last = b - last, b
                rows[name].append(phases[name])
            at = t["t0"] + last
            steps.append(dict(phases, plat=r["plat"], scenario=r["scenario"], step=s["step"],
                              rule=grace.get("rule"), grace_s=grace.get("grace_s")))
            before, passed = 0.0, sorted(ph.items(), key=lambda kv: kv[1])
            for i, (name, b) in enumerate(passed):
                seq.setdefault(name, []).append((i / max(1, len(passed) - 1), b - before))
                before = b
            lp = t.get("log_phases") or {}
            pairs = [("log: launch -> app=boot", ph.get("launch"), lp.get("boot")),
                     ("log: evidence line -> its read", lp.get("evidence"), ph.get("evidence")),
                     ("log: grace_end -> beat line", ph.get("grace_end"), lp.get("beat")),
                     ("log: beat line -> its read", lp.get("beat"), ph.get("alive"))]
            for d in t.get("dialogs") or []:
                pairs += [("dialog: launch -> found", 0.0, d.get("found_s")),
                          ("dialog: found -> tapped", d.get("found_s"), d.get("tapped_s")),
                          ("dialog: tapped -> done", d.get("tapped_s"), d.get("done_s"))]
            for name, a, b in pairs:
                extra.setdefault(name, [])
                if a is not None and b is not None:
                    extra[name].append(b - a)
            for method, c in (t.get("calls") or {}).items():
                calls.setdefault(method, [0, 0.0, 0, 0.0])
                for k, key in enumerate(("n", "s", "bg_n", "bg_s")):
                    calls[method][k] += c.get(key, 0)
        rows["run tail" if timed and r.get("status") in ("PASS", "FAIL") else "untimed"].append(r["end"] - at)
        prev = r["end"]
    rows["between runs"].append(end - prev)
    # Boundaries in the order they fall (the median of each one's place in its step, 0 first, 1 last:
    # a window step and an evidence step pass different ones), then the dialog windows and log stamps.
    intervals = {name: [dt for _, dt in v]
                 for name, v in sorted(seq.items(), key=lambda kv: statistics.median(at for at, _ in kv[1]))}
    intervals.update((name, v) for name, v in extra.items() if v)
    return {"rows": rows, "lane": end - start, "span": end - start, "runs": runs, "steps": steps, "calls": calls,
            "intervals": intervals, "untimed": untimed, "partial": partial, "killed": span.get("end") is None,
            "devices": {serial or next(iter(devices), None) or (board.get("serials") or {}).get(os_name):
                        {"lane": end - start, "killed": span.get("end") is None,
                         "platforms": span.get("platforms") or list(dict.fromkeys(r["plat"] for r in runs))}}}


def board_report(ref, vs=None):
    """(Tier C, C7.0) Per OS: the lane, its phase budget (sum, % of the lane, per-instance median),
    each boundary's interval, the 15 steps held longest after their evidence and the device calls;
    with `vs`, two boards side by side and the difference (vs - ref) per phase, per interval, per
    lane and per call.  (Tier C, C7.7) An OS with two devices on either board is reported by its
    span, then each device's lane, and its tables are device-seconds (lane_budget): one device
    against two shows the saving in the span and the contention in the per-phase and per-call growth."""
    boards = [load_board(ref)] + ([load_board(vs)] if vs else [])
    for bd in boards:
        statuses = {}
        for r in bd.get("runs") or []:
            statuses[r.get("status")] = statuses.get(r.get("status"), 0) + 1
        built = [b for b in bd.get("builds") or [] if b.get("start") is not None]
        print("board %s: variant %s, %d scenarios x %d cells, wall %s, builds %.1f s (%d built); %s"
              % (bd.get("sweep"), bd.get("variant"), len(bd.get("scenarios") or []), len(bd.get("cells") or []),
                 "%.1f s" % bd["wall"] if bd.get("wall") is not None else "none (killed before its board)",
                 sum(b["end"] - b["start"] for b in built), len(built),
                 ", ".join("%s %d" % kv for kv in sorted(statuses.items()))))
    oses = [o for o in ("android", "ios") if any(o in (bd.get("lanes") or {}) or
                                                  any(r.get("os") == o and r.get("start") for r in bd.get("runs") or [])
                                                  for bd in boards)]
    med = lambda v: statistics.median(v) if v else 0.0
    for os_name in oses:
        budgets = [lane_budget(bd, os_name) for bd in boards]
        if not vs:
            b = budgets[0]
            if b is None:
                print("\n== %s: no run finished" % os_name)
                continue
            multi = len(b["devices"]) > 1
            dead = " (no lane end: %s, runs to its last finished run)" % (
                "its lane thread died" if boards[0].get("wall") is not None else "killed")
            print("\n== %s %s %.1f s%s: %d runs, %d steps timed%s%s"
                  % (os_name, "span" if multi else "lane", b["span"] if multi else b["lane"],
                     "" if not b["killed"] else dead, len(b["runs"]), len(b["steps"]),
                     "; %d step(s) with no phases (a run.py from before C7.0)" % b["untimed"] if b["untimed"] else "",
                     "; %d step(s) whose run.json re-write failed (capture ends at db_pulled, and judge to "
                     "teardown fall in the gap after)" % b["partial"] if b["partial"] else ""))
            if multi:
                # (Tier C, C7.7) The span is what the wall pays; the rows are device-seconds, every device's lane
                for serial, d in b["devices"].items():
                    print("   device %s %s: %s%s"
                          % (serial, "no run finished" if d.get("none") else "lane %.1f s" % d["lane"],
                             ", ".join(d["platforms"]), "" if not d["killed"] else dead))
                print("   rows below: device-seconds, over all %d devices' runs" % len(b["devices"]))
            print("   %-14s %8s %6s %8s %4s" % ("phase", "s", "%", "median", "n"))
            for name, what in BOARD_ROWS:
                v = b["rows"][name]
                print("   %-14s %8.1f %6.1f %8.2f %4d  %s"
                      % (name, sum(v), 100.0 * sum(v) / b["lane"] if b["lane"] else 0, med(v), len(v), what))
            print("   %-14s %8.1f %6.1f" % ("device lanes" if multi else "lane", b["lane"], 100.0))
            print("   %-32s %4s %8s %8s   (each boundary: s since the one before it)" % ("interval", "n", "s", "median"))
            for name, v in b["intervals"].items():
                print("   %-32s %4d %8.1f %8.3f" % (name, len(v), sum(v), med(v)))
            print("   top 15 steps by hold:")
            for st in sorted(b["steps"], key=lambda st: -st["hold"])[:15]:
                print("   %8.2f s  window %6.2f s  %-13s %s/%s  (%s%s)"
                      % (st["hold"], st["window"], st["plat"], st["scenario"], st["step"], st["rule"] or "-",
                         "" if st["grace_s"] is None else " %g s" % st["grace_s"]))
            print("   calls %-26s %6s %9s %6s %9s   (bg: off the step's thread)" % ("", "n", "s", "bg n", "bg s"))
            for method, (n, secs, bg_n, bg_s) in sorted(b["calls"].items(), key=lambda kv: -kv[1][1]):
                print("         %-26s %6d %9.3f %6d %9.3f" % (method, n, secs, bg_n, bg_s))
            continue
        a, b = budgets
        if a is None or b is None:
            print("\n== %s: %s" % (os_name, "no run finished on either board" if a is b else
                                    "only %s has a run" % boards[a is None].get("sweep")))
            continue
        multi = len(a["devices"]) > 1 or len(b["devices"]) > 1
        if multi:
            # (Tier C, C7.7) The span is the saving; the rows, device-seconds, show the contention
            print("\n== %s span: %.1f s (A, %s, %d device%s) -> %.1f s (B, %s, %d device%s), %+.1f s"
                  % (os_name, a["span"], boards[0].get("sweep"), len(a["devices"]), "s" * (len(a["devices"]) > 1),
                     b["span"], boards[1].get("sweep"), len(b["devices"]), "s" * (len(b["devices"]) > 1),
                     b["span"] - a["span"]))
            for tag, x in (("A", a), ("B", b)):
                print("   %s: %s" % (tag, "; ".join("%s %s (%s)" % (
                    s, "no run finished" if d.get("none") else "lane %.1f s" % d["lane"], ", ".join(d["platforms"]))
                    for s, d in x["devices"].items())))
            print("   rows below: device-seconds, over each board's devices")
        else:
            print("\n== %s lane: %.1f s (A, %s) -> %.1f s (B, %s), %+.1f s"
                  % (os_name, a["lane"], boards[0].get("sweep"), b["lane"], boards[1].get("sweep"),
                     b["lane"] - a["lane"]))
        print("   %-14s %8s %6s %8s  %8s %6s %8s  %8s %8s"
              % ("phase", "A s", "%", "median", "B s", "%", "median", "diff s", "median"))
        for name, _ in BOARD_ROWS + [("lane", None)]:
            va = [d["lane"] for d in a["devices"].values()] if name == "lane" else a["rows"][name]
            vb = [d["lane"] for d in b["devices"].values()] if name == "lane" else b["rows"][name]
            print("   %-14s %8.1f %6.1f %8.2f  %8.1f %6.1f %8.2f  %+8.1f %+8.2f"
                  % ("device lanes" if multi and name == "lane" else name,
                     sum(va), 100.0 * sum(va) / a["lane"] if a["lane"] else 0, med(va),
                     sum(vb), 100.0 * sum(vb) / b["lane"] if b["lane"] else 0, med(vb),
                     sum(vb) - sum(va), med(vb) - med(va)))
        print("   %-32s %4s %8s  %4s %8s  %8s %8s" % ("interval", "A n", "median", "B n", "median", "diff", "diff s"))
        for name in list(a["intervals"]) + [n for n in b["intervals"] if n not in a["intervals"]]:
            va, vb = a["intervals"].get(name, []), b["intervals"].get(name, [])
            print("   %-32s %4d %8.3f  %4d %8.3f  %+8.3f %+8.1f"
                  % (name, len(va), med(va), len(vb), med(vb), med(vb) - med(va), sum(vb) - sum(va)))
        print("   %-26s %6s %9s  %6s %9s  %6s %9s" % ("calls", "A n", "s", "B n", "s", "diff n", "diff s"))
        none = [0, 0.0, 0, 0.0]
        for method in sorted(set(a["calls"]) | set(b["calls"]),
                             key=lambda m: -max(a["calls"].get(m, none)[1], b["calls"].get(m, none)[1])):
            ca, cb = a["calls"].get(method, none), b["calls"].get(method, none)
            print("   %-26s %6d %9.3f  %6d %9.3f  %+6d %+9.3f"
                  % (method, ca[0], ca[1], cb[0], cb[1], cb[0] - ca[0], cb[1] - ca[1]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--all", action="store_true")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--metrics", action="store_true", help="list the metric vocabulary")
    mode.add_argument("--coverage", action="store_true", help="SDK members the api scenarios assert")
    ap.add_argument("--leaves", action="store_true",
                    help="with --coverage: every compound Config/State attribute, and which scenarios set, type or value it; "
                         "then every payload leaf (Tier C, C4.0)")
    ap.add_argument("--args", action="store_true",
                    help="with --coverage: per member, the argument forms the scenarios ask (Tier C, C4.0)")
    mode.add_argument("--replay-end", nargs="?", const="rule", metavar="GRACE",
                    help="replay end-on-evidence over every capture on disk and compare verdicts: the "
                         "per-step rule (labkit.Grace; the default), or a fixed GRACE in seconds")
    ap.add_argument("--allow-cut", action="store_true",
                    help="--replay-end also replays captures that already ended on evidence")
    mode.add_argument("--trace", action="store_true", help="per step: what caused what (app's own lines)")
    mode.add_argument("--trace-diff", metavar="REF_ROOT",
                    help="compare every step under REF_ROOT with --root: SAME / ORDER-CHANGED / SUMMARY-CHANGED")
    mode.add_argument("--board", metavar="SWEEP",
                      help="a sweep's phase budget per OS lane, from its board.json: a stamp, its directory "
                           "or the file (Tier C, C7.0)")
    ap.add_argument("--vs", metavar="SWEEP", help="with --board: a second board, side by side")
    mode.add_argument("--divergence", nargs="?", const="FIX", metavar="VARIANT",
                      help="the invalid-value matrix, leaf x kind x cell, from the captures of VARIANT (FIX) under "
                           "--root (Tier C, C4.4)")
    mode.add_argument("--in-scan", action="store_true",
                      help="every FIX capture's reported values against the current YAML's `in:` sets, before any board "
                           "(Tier C, C5.1); needs the captures' logcat.txt, which only the checkout that ran them has")
    mode.add_argument("--pilot", action="store_true",
                      help="per invalid_values capture (the paths, or every FIX one under --root): did the transport "
                           "carry the step, apart from whether the log kept its answers (Tier C, C4.4)")
    ap.add_argument("--root", default=os.path.join(labkit.LABS, "captures"),
                    help="where --all / --replay-end / --trace look (e.g. a labs/baselines/<set>)")
    a = ap.parse_args()
    if a.allow_cut and a.replay_end is None:
        ap.error("--allow-cut only means something with --replay-end")
    if (a.leaves or a.args) and not a.coverage:
        ap.error("--leaves and --args only mean something with --coverage")
    if a.leaves and a.args:
        ap.error("--leaves and --args are two views of --coverage: ask for one")
    if a.trace_diff and (a.paths or a.all):
        ap.error("--trace-diff compares two roots: REF_ROOT and --root, not paths")
    if a.vs and not a.board:
        ap.error("--vs only means something with --board")
    if a.board:
        return board_report(a.board, a.vs)
    if a.divergence:
        if a.paths or a.all:
            ap.error("--divergence reads every capture of its variant under --root, not paths")
        return divergence(a.divergence, a.root)
    if a.in_scan:
        return enum_scan(a.root)
    if a.pilot:
        if a.all:
            ap.error("--pilot reads the paths given, or every FIX invalid_values capture under --root")
        paths = a.paths or [os.path.dirname(run) for run in sorted(glob.glob(os.path.join(a.root, "*", "*", "FIX", "*",
                                                                                            "run.json")))
                            if any("kind" in h for h in (json.load(open(run)).get("hooks") or []))]
        if not paths:
            print("no invalid_values capture under %s/*/*/FIX (Tier C, C4.4)" % a.root, file=sys.stderr)
            return 2
        return pilot(paths)
    if a.coverage:
        coverage_leaves() if a.leaves else coverage_args() if a.args else coverage()
        return 0
    if a.metrics:
        for name, fn in sorted(labkit.METRICS.items()):
            print("%-32s %s" % (name, (fn.__doc__ or "").strip().splitlines()[0]
                                if fn.__doc__ else ""))
        return 0
    paths = a.paths
    if a.all:
        paths = sorted(os.path.dirname(p) for p in
                       glob.glob(os.path.join(a.root, "*", "*", "*", "*", "logcat.txt")))
    if a.trace_diff:
        return 0 if trace_diff(a.trace_diff, a.root) else 1
    if a.replay_end is not None or a.trace:
        paths = paths or sorted(os.path.dirname(p) for p in
                                glob.glob(os.path.join(a.root, "*", "*", "*", "*", "logcat.txt")))
    if (a.replay_end is not None or a.trace) and not paths:
        print("no capture under %s" % a.root, file=sys.stderr)
        return 2
    if a.trace:
        for p in paths:
            t = trace(p)
            print("== %s" % os.path.relpath(p, labkit.LABS))
            if t["contaminated"]:
                print("   CONTAMINATED  %s" % "; ".join(t["contaminated"]))
            print("   api     %s" % " ".join(i for i, _ in t["api"]))
            if t["host"]:                                   # (Tier C, C5.0) the host's moves and actions, in send order
                print("   host    %s" % " ".join(h for _, h in t["host"]))
            for ident, ev in sorted(t["events"].items(), key=lambda kv: kv[1]["t"]):
                print("   event   %-24s first after %s (+%s ms; next hook +%s ms)"
                      % (ident, ev["after"], ev["gap_ms"], ev["next_ms"]))
            if t["dialog"]:
                print("   dialog  raised after %s (+%s ms); actions %s"
                      % (t["dialog"]["after"], t["dialog"]["gap_ms"], ",".join(t["actions"]) or "-"))
        return 0
    if a.replay_end is not None:
        return 0 if replay_end(paths, None if a.replay_end == "rule" else float(a.replay_end),
                               allow_cut=a.allow_cut) else 1
    if not paths:
        ap.error("give a capture directory, or --all")
    # (WO-027) A list, not a generator: all() stopped at the first red block, and a conformance
    # capture is red by design until its bridge is fixed — every capture after it went unscored.
    results = [score_one(p) for p in paths]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
