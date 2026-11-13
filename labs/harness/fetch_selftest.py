#!/usr/bin/env python3
"""The fetch lab's offline selftest (WO-002): no device, no build.  `./labkit selftest` runs it.

What it holds:
  * the pack is active here AND in a subprocess started the way sweep.py starts run.py (sweep.capped, sweep.py:647),
    and one level further down; refused (exit 2, lab_product.py: bg-forge WO-191) where FETCH_LABKIT_PACK is not set,
    where the activation is not installed, or where the active pack is another checkout's;
  * labs/harness/vocabulary.yaml against types/background-fetch (names, signatures, interfaces, closed types) and the
    constants' values against docforge/db;
  * the vendored loader on the fetch vocabulary: configs, arguments, and a scenario with fetch's events, hooks and
    host actions (and the refusals that keep a scenario honest);
  * the SDK log adapter (Android logcat tag, the iOS tap's retag and its predicate), the fetch metrics, the host's
    jobscheduler read, actionsDone over a `job` miss;
  * the host actions' runner and device primitives against a fake device.

bg-forge's own selftest (labkit._selftest, 727 checks) is bg-geo data and is not run: labs/VENDORED.md.
Prints `ok <check>` per check, FAIL lines, and a verdict; exit 1 on any failure.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HARNESS = os.path.dirname(os.path.abspath(__file__))
LABS = os.path.dirname(HARNESS)
REPO = os.path.dirname(LABS)
sys.path.insert(0, HARNESS)

import fetch_pack                                     # noqa: E402  (activated by sitecustomize under ./labkit)
os.environ.setdefault("FETCH_LABKIT_PACK", "1")       # and here when the selftest is run on its own: lab_product.py
fetch_pack.activate()                                 # asks for both (the subprocess checks set their own env)
import backends                                       # noqa: E402
import devices                                        # noqa: E402
import labkit                                         # noqa: E402
import run                                            # noqa: E402
import sweep                                          # noqa: E402

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("ok %s" % name) if ok else ("FAIL %s\n     got:  %r\n     want: %r" % (name, got, want)))
    return ok


def note(text):
    print("-- %s" % text)


def refused(fn):
    try:
        fn()
    except SystemExit as e:
        return str(e.code if isinstance(e.code, str) else e)
    return None


# ---------------------------------------------------------------------------------------------------- activation

def test_activation(tmp):
    check("pack active in this process", fetch_pack.active(), True)
    check("backends.SDK_TAG", backends.SDK_TAG, "TSBackgroundFetch")
    # (bg-forge WO-191) labkit's seams are hooks labkit applies itself, from lab_product.py: no post-import patch.
    check("labkit loaded lab_product.py, and SDK_LOG, EVIDENCE and CAPTURE_EXTRAS are the pack's",
          (labkit.PRODUCT.__name__, labkit.SDK_LOG.__module__, labkit.EVIDENCE.__module__,
           labkit.CAPTURE_EXTRAS.__module__), ("lab_product", "fetch_pack", "fetch_pack", "fetch_pack"))
    check("job and force_stop (Android), fire, background and terminate (iOS, WO-002 phase 2a) are labkit action "
          "kinds, run by the vendored run_host (no companion runner)",
          (list(labkit.ACTION_HANDLERS)[-5:], [labkit.ACTION_HANDLERS[k].oses for k in ("fire", "background", "terminate")],
           run.run_host.__module__ in ("run", "__main__")),
          (["job", "force_stop", "fire", "background", "terminate"], [("ios",)] * 3, True))
    check("sweep.SUITE is fetch's", sweep.SUITE, fetch_pack.SUITE)
    check("sweep.scenario_list('suite')", sweep.scenario_list("suite"), fetch_pack.SUITE)

    # (1) The way sweep.py starts a run (sweep.py:647 via sweep.capped, :247): the vendored function, real argv.
    report = os.path.join(tmp, "report.jsonl")
    env = dict(os.environ, FETCH_PACK_REPORT=report, FETCH_LABKIT_PACK="1",
               PYTHONPATH=os.pathsep.join([os.path.join(HARNESS, "pack")] +
                                          [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]))
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        rc = sweep.capped([sys.executable, "-u", os.path.join(HARNESS, "run.py"), "--help"], 120,
                          os.path.join(tmp, "run-help.log"))
        # (2) One level further: a process that starts run.py itself, as lab_setup's subprocesses do.
        nested = subprocess.run([sys.executable, "-c", "import subprocess, sys; sys.exit(subprocess.run("
                                 "[sys.executable, %r, '--help'], stdout=subprocess.DEVNULL).returncode)"
                                 % os.path.join(HARNESS, "run.py")], timeout=120).returncode
        # (3) Refused without the flag (bg-forge WO-191): labkit loads lab_product.py, which will not give a process
        # fetch's labkit seams beside bg-geo's backends, run and sweep.  Exit 2, the harness's "could not be judged".
        bare = dict(env)
        bare.pop("FETCH_LABKIT_PACK")
        refusal = subprocess.run([sys.executable, os.path.join(HARNESS, "run.py"), "--help"], env=bare,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=120)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    rows = [json.loads(l) for l in open(report)] if os.path.exists(report) else []
    pids = sorted({r["pid"] for r in rows if r["pid"] != os.getpid()})
    check("run.py --help via sweep.capped exits 0", rc, 0)
    check("nested run.py --help exits 0", nested, 0)
    check("bare run.py --help (FETCH_LABKIT_PACK unset) exits 2: lab_product.py did not load, and says why",
          (refusal.returncode, "fetch pack is not active" in refusal.stderr and "did not load" in refusal.stderr),
          (2, True))
    # Each half of lab_product's guard: the flag without the activation (fetch_pack imported by hand, never activated),
    # and an activation without the flag (its subprocesses would not inherit it).
    halves = []
    for flag, code in (("1", "import fetch_pack, labkit"), (None, "import fetch_pack; fetch_pack.activate(); import labkit")):
        env_ = {k: v for k, v in os.environ.items() if k not in ("FETCH_LABKIT_PACK", "PYTHONPATH")}
        if flag:
            env_["FETCH_LABKIT_PACK"] = flag
        r = subprocess.run([sys.executable, "-B", "-c", "import sys; sys.path.insert(0, %r); %s" % (HARNESS, code)],
                           env=env_, capture_output=True, text=True, timeout=120)
        halves.append((r.returncode, "fetch pack is not active" in r.stderr))
    check("lab_product refuses the flag without the activation, and the activation without the flag",
          halves, [(2, True), (2, True)])
    # Paths (review): the pack's import hook and lab_product compare REAL paths.  Through a symlink to labs/harness
    # (macOS's /tmp is one), Python runs the script from its realpath; the hook must still patch backends and run.
    # And a pack activated from another checkout (a shell that ran `./labkit env` there) is refused here: its hook
    # patches only its own harness, so this one's backends would stay bg-geo's.
    link = os.path.join(tmp, "harness-link")
    os.symlink(HARNESS, link)
    linked_report = os.path.join(tmp, "linked.jsonl")
    r = subprocess.run([sys.executable, os.path.join(link, "run.py"), "--help"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True, timeout=120,     # the pack loaded through the link too, as
                       env=dict(env, FETCH_PACK_REPORT=linked_report,      # `./labkit env` in a symlinked checkout does
                                PYTHONPATH=os.path.join(link, "pack")))
    linked = sorted({json.loads(l)["module"] for l in open(linked_report)}) if os.path.exists(linked_report) else []
    other = os.path.join(tmp, "other", "harness")
    os.makedirs(other)
    for name in os.listdir(HARNESS):
        if os.path.isfile(os.path.join(HARNESS, name)):
            shutil.copy2(os.path.join(HARNESS, name), other)
    r2 = subprocess.run([sys.executable, os.path.join(other, "run.py"), "--help"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, text=True, timeout=120, env=env)
    check("through a symlinked harness path run.py is patched whole (backends, devices, labkit, run); a checkout given another "
          "checkout's active pack is refused",
          (r.returncode, linked, r2.returncode, "another checkout's" in r2.stderr),
          (0, ["backends", "devices", "labkit", "run"], 2, True))
    check("two pack-activated child processes reported (sweep.capped's, the nested one's)", len(pids), 2)
    for pid in pids[:2]:
        mine = {r["module"]: r for r in rows if r["pid"] == pid}
        check("child %d: run.py patched as __main__ (assert_native)" % pids.index(pid),
              (mine.get("run") or {}).get("as_main"), True)
        check("child %d: labkit registered (lab_product.py) and backends patched on import" % pids.index(pid),
              sorted(k for k in mine if k in ("labkit", "backends")), ["backends", "labkit"])
    check("no report from the child run without FETCH_LABKIT_PACK", len(pids), 2)


# ---------------------------------------------------------------------------------------------------- vocabulary

def _strip_comments(src):
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", src, flags=re.S))


def _split_top(text, sep=","):
    out, depth, cur = [], 0, ""
    for i, ch in enumerate(text):
        if ch in "(<[{":
            depth += 1
        elif ch in ")]}" or (ch == ">" and text[i - 1:i] != "="):
            depth -= 1
        if ch == sep and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return [p for p in out + [cur.strip()] if p]


def types_surface(root):
    """The fetch API as types/background-fetch spells it: {api: {...}, interfaces: {...}, unions: {...}}."""
    src = lambda name: " ".join(_strip_comments(open(os.path.join(root, "src", name)).read()).split())
    bf, itf, ty = src("BackgroundFetch.ts"), src("interfaces.ts"), src("types.ts")
    api = {}
    for name, ts in re.findall(r"static (\w+): (\w+);", bf):
        api[name] = {"property": True, "returns": ts}
    for m in re.finditer(r"static (\w+)\(", bf):
        depth, i = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(bf[i], 0)
            i += 1
        params = _split_top(bf[m.end():i - 1])
        ret = bf[i:].split(";", 1)[0].lstrip(": ").strip()
        ret = re.sub(r"^Promise<(.*)>$", r"\1", ret)
        api[m.group(1)] = {"args": [" ".join(p.split()) for p in params], "returns": ret}
    interfaces = {}
    for m in re.finditer(r"export interface (\w+)(?: extends (\w+))? \{(.*?)\}", itf):
        fields = {}
        for f in m.group(3).split(";"):
            if ":" in f:
                k, v = f.split(":", 1)
                fields[k.strip()] = v.strip()
        interfaces[m.group(1)] = (m.group(2), fields)
    flat = {}
    for name, (base, fields) in interfaces.items():
        flat[name] = dict(interfaces[base][1] if base else {}, **fields)
    unions = {n: [int(v) for v in vals.split("|")] for n, vals in re.findall(r"export type (\w+) = ([\d |]+);", ty)}
    return {"api": api, "interfaces": flat, "unions": unions}


def types_doc_values(root):
    """(WO-002 Tier 1) The constants' values as the types package DOCUMENTS them — where the NETWORK_TYPE transposition
    shipped (9c296a8, fixed a day later in 7be684d): each static's JSDoc `Value: \\`N\\`` (src/BackgroundFetch.ts) and
    the `| \\`N\\` | [[BackgroundFetch.<NAME>]] |` rows of src/types.ts.  No runtime reads either, so types_surface's
    `0 | 1 | 2 | 3 | 4` union cannot see a swap."""
    bf = open(os.path.join(root, "src", "BackgroundFetch.ts")).read()
    ty = open(os.path.join(root, "src", "types.ts")).read()
    jsdoc = {name: int(v) for v, name in re.findall(r"Value: `(\d+)`\s*\*/\s*static (\w+):", bf)}
    table = {name: int(v) for v, name in re.findall(r"\|\s*`(\d+)`\s*\|\s*\[\[BackgroundFetch\.(\w+)\]\]", ty)}
    return {"jsdoc": jsdoc, "table": table}


def test_vocabulary():
    vocab = labkit.vocabulary()
    root = os.path.join(REPO, "types", "background-fetch")
    if not os.path.isfile(os.path.join(root, "src", "BackgroundFetch.ts")):
        note("NOT CHECKED: types/background-fetch is not checked out (git submodule update --init types/background-fetch)")
    else:
        t = types_surface(root)
        check("vocabulary api names == the types' statics", sorted(vocab["api"]), sorted(t["api"]))
        for name, want in sorted(t["api"].items()):
            have = vocab["api"].get(name) or {}
            got = ({"property": True, "returns": have.get("returns")} if have.get("property") else
                   {"args": have.get("args") or [], "returns": have.get("returns")})
            check("vocabulary api %s as the types declare it" % name, got, want)
        for name in ("BackgroundFetchConfig", "TaskConfig", "HeadlessEvent"):
            check("vocabulary types.interfaces.%s == interfaces.ts (AbstractConfig flattened)" % name,
                  vocab["types"]["interfaces"][name], t["interfaces"][name])
        check("vocabulary config == BackgroundFetchConfig's fields",
              {k: v for k, v in vocab["config"].items()},
              {k.rstrip("?"): v for k, v in t["interfaces"]["BackgroundFetchConfig"].items()})
        for name, values in t["unions"].items():
            check("vocabulary enums.%s values == types.ts" % name, vocab["enums"][name]["values"], values)
        # (WO-002 Tier 1, api-constants' offline half) The documented values, where the transposition shipped.
        doc = types_doc_values(root)
        members = {k: v for entry in vocab["enums"].values() for k, v in (entry.get("members") or {}).items()}
        check("constants: every static's JSDoc `Value: N` in types/background-fetch == the vocabulary (9c296a8 documented "
              "CELLULAR 2, UNMETERED 3, NOT_ROAMING 4)", doc["jsdoc"], members)
        check("constants: the BackgroundFetchStatus and NetworkType tables in types.ts == the vocabulary",
              doc["table"], members)
    db = os.path.join(REPO, "docforge", "db")
    if os.path.isdir(db):
        consts = {}
        for f in os.listdir(db):
            text = open(os.path.join(db, f), errors="replace").read()
            m = re.search(r'ts: "static (\w+): (\w+) = (\d+);"', text)
            if m:
                consts[m.group(1)] = (m.group(2), int(m.group(3)))
        members = {k: (e, v) for e, entry in vocab["enums"].items() if e != "Event"
                   for k, v in (entry.get("members") or {}).items()}
        check("enum members (constant -> value) == docforge/db signatures.ts", members, consts)
        fin = open(os.path.join(db, "BackgroundFetch.finish.yaml")).read()
        note("known divergence: finish is `finish(taskId: string)` in the types, %s in docforge/db"
             % ("`taskId?`" if "finish(taskId?: string)" in fin else "now matching"))
    for name, member in sorted(vocab["api"].items()):
        labkit.wire_of(member)                        # refuses a vocabulary entry labkit cannot type
    check("wire of configure / start / status", [sorted(labkit.wire_of(vocab["api"][n])) for n in
                                                ("configure", "start", "status")], [["number"]] * 3)
    check("configure emits a string (the taskId)", labkit.wire_of(vocab["api"]["configure"], "emits"), {"string"})
    check("headless names: the pack's HEADLESS_NAMES (WO-191), and only their metrics generated",
          (labkit.headless_names(), "Event" in vocab["enums"],
           sorted(k for k in labkit.METRICS if k.startswith(("headlessEvents.", "headlessFired.")))),
          (["fetch", "timeout"], False,
           ["headlessEvents.fetch", "headlessEvents.timeout", "headlessFired.fetch", "headlessFired.timeout"]))
    cfg = {"minimumFetchInterval": 15, "stopOnTerminate": False, "enableHeadless": True, "requiredNetworkType": 2}
    check("a fetch config conforms", labkit._validate_config("t", cfg, vocab["config"]), None)
    check("a misspelt config key refuses", "is not in the SDK vocabulary" in (refused(
        lambda: labkit._validate_config("t", {"minimumFetchIntervall": 15}, vocab["config"])) or ""), True)
    check("requiredNetworkType 7 is no NetworkType", "none of 0, 1, 2, 3, 4" in (labkit._validate_config(
        "t", {"requiredNetworkType": 7}, vocab["config"]) or ""), True)
    check("a TaskConfig conforms", labkit._conform({"taskId": "com.lab.t", "delay": 5000}, "TaskConfig", "a"), None)
    check("a TaskConfig without delay does not", "leaves out `delay`" in (labkit._conform(
        {"taskId": "com.lab.t"}, "TaskConfig", "a") or ""), True)


# ---------------------------------------------------------------------------------------------------- scenarios

SCENARIO = """
description: fetch selftest (never run on a device)
not_applicable: {ios: "2026-11-05 the host actions are Android's"}
steps:
  - name: one
    settle: 5
    after: 40
    events:
      - {subscribe: configure, args: {minimumFetchInterval: 15, stopOnTerminate: false, enableHeadless: true}, errors: true, returns: {"$": {type: string}}}
    hooks:
      - {api: scheduleTask, args: {taskId: com.transistorsoft.fetchlab.task, delay: 5000}, returns: {"$": {type: boolean}}}
      - {api: status, returns: {"$": {type: number}}}
      - {api: STATUS_AVAILABLE, probe: true, returns: {"$": {equals: 2}}}
      - {api: stop, args: com.transistorsoft.fetchlab.task, returns: {"$": {type: boolean}}}
    actions:
      - {after: {armed: configure@0}, delay_s: 2, job: 999}
      - {after: {point: host}, kill: true}
      - {after: {point: host}, delay_s: 2, job: 999, expect: absent}
      - {after: {point: host}, force_stop: true}
      - {after: {api: "scheduleTask#0"}, delay_s: 2, job: com.transistorsoft.fetchlab.task}
"""


# (bg-forge WO-191) The repeat budget on its own: two `job: 999` in one step, the second waiting out the SDK's 5 s
# duplicate guard (JOB_REPEAT_S).  settle + after = 20 s holds two jobs 2 s after configure arms (12 + 2 + 4 = 18) but not
# a repeat's 7.5 s (23.5).  The two items are written alike — equal, but not one object — so an equality lookup would
# read the second as the first and drop its budget.
REPEAT = """
description: fetch selftest, the repeat budget (never run on a device)
not_applicable: {ios: "2026-11-05 the host actions are Android's"}
steps:
  - name: one
    settle: 5
    after: 15
    events:
      - {subscribe: configure, args: {minimumFetchInterval: 15, stopOnTerminate: false, enableHeadless: true}, errors: true, returns: {"$": {type: string}}}
    actions:
      - {after: {armed: configure@0}, delay_s: 2, job: 999}
      - {after: {armed: configure@0}, delay_s: 2, job: 999}
"""


# (WO-002 Tier 1) `unspaced:`: the same two jobs, the second fired inside the SDK's guard on purpose.  It costs no repeat
# budget (20 s holds it: 12 + 2 + 4 = 18), and only loads with a date, after an earlier run of the same job, and where
# the step states duplicateJobs.
UNSPACED = """
description: fetch selftest, unspaced (never run on a device)
not_applicable: {ios: "2026-11-09 the host actions are Android's"}
steps:
  - name: one
    settle: 5
    after: 15
    events:
      - {subscribe: configure, args: {minimumFetchInterval: 15, stopOnTerminate: false, enableHeadless: true}, errors: true, returns: {"$": {type: string}}}
    actions:
      - {after: {armed: configure@0}, delay_s: 2, job: 999}
      - {after: {armed: configure@0}, delay_s: 2, job: 999, unspaced: "2026-11-09 inside the guard, on purpose"}
    expect: {duplicateJobs: 1}
"""


def load_scenario(tmp, text, name="selftest-fetch"):
    os.makedirs(os.path.join(tmp, "scenarios"), exist_ok=True)
    with open(os.path.join(tmp, "scenarios", name + ".yaml"), "w") as f:
        f.write(text)
    saved = labkit.HARNESS
    labkit.HARNESS = tmp
    try:
        return labkit.scenario(name)
    finally:
        labkit.HARNESS = saved


def test_scenarios(tmp):
    sc = load_scenario(tmp, SCENARIO)
    acts = sc["steps"][0]["actions"]
    check("a fetch scenario loads: actions resolved, fetch keys restored",
          [(next(k for k in ("job", "kill", "force_stop") if k in a), a.get("delay_s"), a["after"]) for a in acts],
          [("job", 2, [{"armed": "configure@0"}]), ("kill", None, [{"point": "host"}]),
           ("job", 2, [{"point": "host"}]), ("force_stop", None, [{"point": "host"}]),
           ("job", 2, [{"api": "scheduleTask#0"}])])
    check("no stand-in left behind", [sorted(a) for a in acts if "kill" in a and len(set(a) & {"job", "force_stop"})], [])
    check("a job's `expect: absent` is restored, and only where it was written", [a.get("expect") for a in acts],
          [None, None, "absent", None, None])
    check("configure armed through events (callbacks after one value)", sc["steps"][0]["events"][0]["subscribe"],
          "configure")
    bad = lambda body, where="selftest-bad": refused(lambda: load_scenario(tmp, SCENARIO.replace(
        "      - {after: {armed: configure@0}, delay_s: 2, job: 999}", body), where))
    check("job inside the 1 s scheduling guard refuses",
          "delay_s: 1.5" in (bad("      - {after: {armed: configure@0}, delay_s: 1, job: 999}") or ""), True)
    check("job: true refuses", "is the jobId" in (bad("      - {after: {armed: configure@0}, delay_s: 2, job: true}") or ""),
          True)
    check("job and kill in one action refuses", "exactly one of" in (bad(
        "      - {after: {armed: configure@0}, delay_s: 2, job: 999, kill: true}") or ""), True)
    check("expect: maybe refuses", "`present`" in (bad(
        "      - {after: {armed: configure@0}, delay_s: 2, job: 999, expect: maybe}") or ""), True)
    check("expect on a force_stop refuses", "belongs to a `job:`" in (bad(
        "      - {after: {armed: configure@0}, force_stop: true, expect: absent}") or ""), True)
    check("force_stop: false refuses", "is true" in (bad(
        "      - {after: {armed: configure@0}, force_stop: false}") or ""), True)
    check("a job whose after: names nothing in the step refuses", "no such api hook" in (bad(
        "      - {after: {api: \"start#0\"}, delay_s: 2, job: 999}") or ""), True)
    check("actions without ios excused refuse (vendored rule)", "excuses ios" in (refused(lambda: load_scenario(
        tmp, SCENARIO.replace('not_applicable: {ios: "2026-11-05 the host actions are Android\'s"}\n', ""),
        "selftest-noios")) or ""), True)
    check("a too-short window still refuses (vendored budget, repeat guard included)", "raise them" in (refused(
        lambda: load_scenario(tmp, SCENARIO.replace("after: 40", "after: 12"), "selftest-short")) or ""), True)
    repeated = refused(lambda: load_scenario(tmp, REPEAT, "selftest-repeat")) or ""
    check("a repeated job budgets the duplicate guard (an equal item found by identity, not by equality): 20 s is short "
          "for two job 999s and enough for job 999 and job 7",
          ("need 7.5 s" in repeated and "raise them" in repeated,
           refused(lambda: load_scenario(tmp, "job: 7}".join(REPEAT.rsplit("job: 999}", 1)), "selftest-norepeat"))),
          (True, None))                                     # this selftest's refused(): None for one that loads
    unspaced = lambda old, new, name: refused(lambda: load_scenario(tmp, UNSPACED.replace(old, new), name)) or ""
    check("unspaced: a dated repeat in a step that states duplicateJobs loads, with no repeat budget (20 s holds it)",
          refused(lambda: load_scenario(tmp, UNSPACED, "selftest-unspaced")), None)
    check("unspaced: refused without a date, as `expect: absent`, with no earlier run of the job, and without a "
          "duplicateJobs count; never on another kind of action",
          ["dated reason" in unspaced('unspaced: "2026-11-09 ', 'unspaced: "', "selftest-u1"),
           "`expect: absent`" in unspaced("job: 999, unspaced", "job: 999, expect: absent, unspaced", "selftest-u2"),
           "no earlier action fires job 999" in unspaced("      - {after: {armed: configure@0}, delay_s: 2, job: 999}\n",
                                                         "", "selftest-u3"),
           "states how many runs" in unspaced("{duplicateJobs: 1}", "{duplicateJobs: any}", "selftest-u4"),
           "states how many runs" in unspaced("    expect: {duplicateJobs: 1}\n", "", "selftest-u5"),
           "another kind of action" in unspaced("job: 999, unspaced", "force_stop: true, unspaced", "selftest-u6")],
          [True] * 6)
    check("configure as an api hook refuses (two required parameters)", "required parameters" in (refused(
        lambda: load_scenario(tmp, SCENARIO.replace("      - {api: status, ",
                                                    "      - {api: configure, args: {minimumFetchInterval: 15}, "),
                              "selftest-hook")) or ""), True)
    vendored = labkit.CALLBACK_ARG
    labkit.CALLBACK_ARG = re.compile(r"(^cb$|callback)", re.I)
    try:
        why = refused(lambda: load_scenario(tmp, SCENARIO, "selftest-vendored-cb"))
    finally:
        labkit.CALLBACK_ARG = vendored
    check("without the pack's CALLBACK_ARG, configure's onEvent reads as a value (why the seam exists)",
          "takes 2 values before its callback" in (why or ""), True)
    check("STATUS_AVAILABLE equals 5 refuses (not a BackgroundFetchStatus)", refused(lambda: load_scenario(
        tmp, SCENARIO.replace('equals: 2}', 'equals: 5}'), "selftest-status")) is not None, True)


# ---------------------------------------------------------------------------------------------------- captures

def L(t, level, tag, pid, msg):
    return "11-05 12:00:%06.3f %s/%s(%5d): %s" % (t, level, tag, pid, msg)


def test_capture(tmp):
    out = os.path.join(tmp, "captures", "react-native", "s", "FIX", "run")
    os.makedirs(out)
    pkg = "com.transistorsoft.fetchlab.reactnative"
    lines = [
        L(1.0, "I", "LAB     ", 100, "point=launch label=x"),
        L(1.05, "I", "ActivityManager", 669, "Start proc 200:%s/u0a190 for next-top-activity {%s/.Main}" % (pkg, pkg)),
        L(1.06, "I", "LAB     ", 200, "point=appCreate pid=200 process=%s headless=- tasks=-" % pkg),
        L(1.1, "I", "ReactNativeJS", 200, "LAB app=boot n=1 args={}"),                        # the driver: pid 200
        L(1.15, "I", "ReactNativeJS", 200, "LAB app=config json={}"),
        L(1.2, "I", "ReactNativeJS", 200, "LAB app=configure source=boot outcome=ok status=2 ms=18"),
        L(1.3, "D", "TSBackgroundFetch", 200, "BootReceiver: android.intent.action.BOOT_COMPLETED"),
        L(1.4, "I", "ReactNativeJS", 400, "LAB app=boot n=1 args={}"),                        # untied driver pid
        L(1.5, "D", "TSBackgroundFetch", 200, "- Background Fetch event received: early"),     # before the window
        L(2.0, "I", "LAB     ", 100, "point=trigger none"),
        L(3.0, "D", "TSBackgroundFetch", 200, "- configure"),
        L(3.1, "D", "TSBackgroundFetch", 200, "- registerTask: react-native-background-fetch (jobId: 999)"),
        L(3.2, "D", "TSBackgroundFetch", 200, "- registerTask: com.lab.task (jobId: -1234)"),
        L(3.3, "D", "TSBackgroundFetch", 300, "- registerTask: foreign (jobId: 999)"),          # another lab app
        L(3.4, "D", "TSBackgroundFetch", 300, "- Background Fetch event received: foreign"),
        L(3.5, "I", "LAB     ", 301, "point=appCreate pid=301 process=com.other.lab headless=-"),
        L(3.6, "D", "TSBackgroundFetch", 301, "- Background Fetch event received: other-lab"),
        L(3.7, "I", "ReactNativeJS", 302, "LAB point=headless event=fetch taskId=x timeout=false"),  # foreign headless
        L(5.9, "I", "LAB     ", 201, "point=appCreate pid=201 process=%s headless=- tasks=-" % pkg),  # headless
        L(5.95, "I", "ActivityManager", 669, "Start proc 202:%s/u0a190 for service {%s/x}" % (pkg, pkg)),
        L(4.0, "I", "LAB     ", 100, "point=host i=0 action=job 999 sent_ms=1 done_ms=2 mark_ms=3"),
        L(4.1, "D", "TSBackgroundFetch", 200, "- Background Fetch event received: react-native-background-fetch"),
        L(4.2, "D", "TSBackgroundFetch", 200, "- finish: react-native-background-fetch"),
        L(4.3, "D", "TSBackgroundFetch", 200, "- jobFinished"),
        L(5.0, "D", "TSBackgroundFetch", 200, "- Caught duplicate Job react-native-background-fetch: [IGNORED]"),
        L(6.0, "D", "TSBackgroundFetch", 202, "[BGTask] timeout: com.lab.task"),           # Start proc only
        L(6.1, "D", "TSBackgroundFetch", 201, "- Background Fetch event received: com.lab.task"),  # appCreate
        L(6.2, "D", "TSLocationManager", 200, "- Background Fetch event received: not-ours"),
        L(7.0, "I", "LAB     ", 100, "point=host i=1 action=job 999 missed=no-job sent_ms=1 done_ms=2 mark_ms=3"),
        L(8.0, "I", "LAB     ", 100, "point=capture"),
        L(9.0, "D", "TSBackgroundFetch", 200, "- jobFinished"),                                # after the window
    ]
    with open(os.path.join(out, "logcat.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    actions = [{"after": [{"point": "trigger"}], "delay_s": 2, "job": 999},
               {"after": [{"point": "host"}], "delay_s": 2, "job": 999}]
    with open(os.path.join(out, "run.json"), "w") as f:
        json.dump({"os": "android", "actions": actions, "year": 2026}, f)
    with open(os.path.join(out, fetch_pack.JOBSCHEDULER), "w") as f:
        f.write("# adb -s emulator-5580 shell dumpsys jobscheduler %s  (exit 0, read 2026-11-05)\n"
                "Settings:\n  min_exp_backoff_time=10000\n"
                "Registered 3 jobs:\n"
                "  JOB #u0a190/999: 3c1b6f4 %s/com.transistorsoft.tsbackgroundfetch.FetchJobService\n"
                "    u0a190 tag=*job*/%s/com.transistorsoft.tsbackgroundfetch.FetchJobService\n"
                "  JOB #u0a190/-1234: 9d1 %s/com.transistorsoft.tsbackgroundfetch.FetchJobService\n"
                "  JOB #u0a55/7: 11 com.other/com.other.Svc\n"
                "Pending queue:\n  JOB #u0a190/555: 1 %s/x\n" % (pkg, pkg, pkg, pkg, pkg))
    c = labkit.Capture(out)
    got = {name: labkit.METRICS[name](c) for name in fetch_pack.SDK_COUNTS}
    check("SDK-line metrics count TSBackgroundFetch lines in the window only",
          got, {"fetchReceived": 2, "fetchFinished": 1, "fetchTimeouts": 1, "jobFinished": 1, "jobStopped": 0,
                "duplicateJobs": 1, "stoppingOnTerminate": 0, "headlessTaskErrors": 0})
    check("fetchTally", labkit.METRICS["fetchTally"](c), "com.lab.task=1,react-native-background-fetch=1")
    check("jobIds: the package's registered jobs, not the pending queue", labkit.METRICS["jobIds"](c), "-1234,999")
    check("actionsDone over a job the app did not hold", labkit.METRICS["actionsDone"](c), "action1=missed(no-job)")
    check("Capture.sdk takes the vendored callers' LIKE patterns", c.sdk("%duplicate Job %"),
          ["- Caught duplicate Job react-native-background-fetch: [IGNORED]"])
    check("Capture.sdk: the app's own processes only (driver, appCreate, Start proc) — never another lab app's",
          c.sdk("%event received: %"), ["- Background Fetch event received: react-native-background-fetch",
                                         "- Background Fetch event received: com.lab.task"])
    check("own pids: driver, the package's appCreate and Start proc; not another package's, not an untied app=boot",
          sorted(fetch_pack._own_pids(labkit, c)), ["200", "201", "202"])
    check("foreignLines: SDK and headless lines of processes that are not the app's (whole log)",
          labkit.METRICS["foreignLines"](c), 4)
    check("foreignLines is implicit 0 and early", (labkit.IMPLICIT.get("foreignLines"),
                                                   "foreignLines" in labkit.EARLY_METRICS), (0, True))
    check("bootBroadcasts: the app's BootReceiver lines, whole log", labkit.METRICS["bootBroadcasts"](c),
          "BOOT_COMPLETED=1")
    check("jobsAtForceStop without a force_stop", labkit.METRICS["jobsAtForceStop"](c), "no-force-stop")
    check("jobsRegistered: the app's registerTask jobIds in the window", labkit.METRICS["jobsRegistered"](c),
          "-1234,999")
    with open(os.path.join(out, "run.json"), "w") as f:
        json.dump({"os": "android", "actions": actions, "year": 2026, "process": {"live": True, "pid": 400}}, f)
    check("own pids on a `process: live` step: also the untied driver pid (its process predates the step's log)",
          sorted(fetch_pack._own_pids(labkit, labkit.Capture(out))), ["200", "201", "202", "400"])
    with open(os.path.join(out, "run.json"), "w") as f:
        json.dump({"os": "android", "actions": actions, "year": 2026}, f)
    check("configureOutcome: every configure() made resolved", labkit.METRICS["configureOutcome"](c), "ok")
    check("configureOutcome is implicit `ok` (setdefault: a step may restate it)", labkit.IMPLICIT.get(
        "configureOutcome"), "ok")
    # (agent S, 2026-11-05) '-' means "read, and none": a failed or section-less read is `unread`, never '-'.
    with open(os.path.join(out, fetch_pack.JOBSCHEDULER), "w") as f:
        f.write("# adb -s emulator-5580 shell dumpsys jobscheduler %s  (exit 0, read 2026-11-05)\n"
                "Registered 1 job:\n  JOB #u0a55/7: 11 com.other/com.other.Svc\n" % pkg)
    check("jobIds: a read listing none of the package's jobs", labkit.METRICS["jobIds"](labkit.Capture(out)), "-")
    with open(os.path.join(out, fetch_pack.JOBSCHEDULER), "w") as f:
        f.write("# adb -s emulator-5580 shell dumpsys jobscheduler %s  (exit 255, read 2026-11-05)\n"
                "error: device offline\n" % pkg)
    check("jobIds: a failed read is unread, not none", labkit.METRICS["jobIds"](labkit.Capture(out)), "unread")
    with open(os.path.join(out, fetch_pack.JOBSCHEDULER), "w") as f:
        f.write("# adb -s emulator-5580 shell dumpsys jobscheduler %s  (exit 0, read 2026-11-05)\nSettings:\n" % pkg)
    check("jobIds: a read with no Registered section is unread", labkit.METRICS["jobIds"](labkit.Capture(out)),
          "unread")
    os.remove(os.path.join(out, fetch_pack.JOBSCHEDULER))
    check("jobIds without a read (iOS: no jobscheduler.txt)", labkit.METRICS["jobIds"](labkit.Capture(out)), "no-read")
    # configureOutcome over a rejected configure, and over none made
    def outcome(*cfg):
        body = []
        for line in lines:
            if "app=configure " in line or "app=config json=" in line:
                continue
            body.append(line)
            if "app=boot" in line and labkit.line_pid(line) == "200":
                body += [L(1.2, "I", "ReactNativeJS", 200, x) for x in cfg]
        with open(os.path.join(out, "logcat.txt"), "w") as f:
            f.write("\n".join(body) + "\n")
        return labkit.METRICS["configureOutcome"](labkit.Capture(out))
    check("configureOutcome: a rejected configure names its source", outcome(
        "LAB app=configure source=boot outcome=skipped reason=event-arm",
        "LAB app=configure source=event-arm outcome=rejected ms=3 message=x"), "event-arm=rejected")
    check("configureOutcome: none made is '-' (skipped is not one)", outcome(
        "LAB app=configure source=boot outcome=skipped reason=noConfigure"), "-")
    check("configureOutcome: a configure() that never settled is never ok", outcome(
        "LAB app=config json={}", "LAB app=configure source=boot outcome=ok status=2 ms=1", "LAB app=config json={}"),
        "unsettled=1")
    # actionsDone over `expect: absent`: a job JobScheduler no longer holds is the step's expectation met
    with open(os.path.join(out, "logcat.txt"), "w") as f:
        f.write("\n".join([l for l in lines if "point=launch" in l or "point=trigger" in l] + [
            L(4.0, "I", "LAB     ", 100, "point=host i=0 action=force_stop pid=200 held=999 sent_ms=1 done_ms=2 "
                                         "mark_ms=3"),
            L(7.0, "I", "LAB     ", 100, "point=host i=1 action=job 999 expect=absent said=Could_not_find_job_999 "
                                         "sent_ms=1 done_ms=2 mark_ms=3"),
            L(8.0, "I", "LAB     ", 100, "point=capture")]) + "\n")
    with open(os.path.join(out, "run.json"), "w") as f:
        json.dump({"os": "android", "year": 2026, "actions": [
            {"after": [{"point": "trigger"}], "force_stop": True},
            {"after": [{"point": "host"}], "delay_s": 3, "job": 999, "expect": "absent"}]}, f)
    check("actionsDone: `expect: absent` met is ok", labkit.METRICS["actionsDone"](labkit.Capture(out)), "ok")
    check("jobsAtForceStop: the force_stop mark's held=", labkit.METRICS["jobsAtForceStop"](labkit.Capture(out)), "999")
    check("Adb.sdk_db pulls nothing", backends.Adb("emulator-0").sdk_db({"package": pkg}), b"")


def LT(t, level, tag, pid, msg):
    """A logcat line `t` seconds after 12:00:00 (minutes allowed: the timeout is 60 s)."""
    return "11-05 12:%02d:%06.3f %s/%s(%5d): %s" % (int(t // 60), t % 60, level, tag, pid, msg)


def test_tier1(tmp):
    """(WO-002 Tier 1) The metrics the react-native Android Tier 1 scenarios read, on one synthetic capture: the app's
    foreground process (200), its headless one (201), another lab app's (300, never counted), lines before and after
    the window, and a jobscheduler.txt whose `Job history:` spans the device's life."""
    out = os.path.join(tmp, "captures", "react-native", "t1", "FIX", "run")
    os.makedirs(out)
    pkg = "com.transistorsoft.fetchlab.react"
    S = lambda t, pid, msg, level="D": LT(t, level, "TSBackgroundFetch", pid, msg)
    J = lambda t, pid, msg: LT(t, "I", "ReactNativeJS", pid, msg)
    lines = [
        LT(0.5, "D", "TSBackgroundFetch", 200, "[HeadlessTask] start taskId: 0"),               # before the window
        LT(1.0, "I", "LAB     ", 100, "point=launch label=x"),
        LT(1.05, "I", "LAB     ", 200, "point=appCreate pid=200 process=%s headless=- tasks=-" % pkg),
        J(1.1, 200, "LAB app=boot n=1 args={}"),
        S(1.2, 200, "☯️  onCreate"),
        S(1.3, 200, "☯️  onStart"),
        LT(2.0, "I", "LAB     ", 100, "point=trigger none"),
        J(2.0, 200, "LAB app=event=fetch n=1 taskId=react-native-background-fetch"),
        J(2.0, 200, "LAB app=event id=configure@0 n=1"),                       # a declaration's, not the driver's
        J(2.1, 200, "LAB app=finish taskId=react-native-background-fetch timeout=false after_ms=- outcome=withheld"),
        S(2.5, 200, "- Background Fetch event received: com.lab.t"),                # an earlier receipt: not the one
        S(3.0, 200, "- Background Fetch event received: com.lab.t"),
        J(3.1, 200, "LAB app=event=fetch n=2 taskId=com.lab.t"),
        J(3.2, 200, "LAB app=finish taskId=com.lab.t timeout=false after_ms=0 outcome=ok"),
        S(62.0, 200, "- Background Fetch event received: com.lab.other"),            # another task's: never paired
        S(63.004, 200, "[BGTask] timeout: com.lab.t"),
        J(63.01, 200, "LAB app=event=timeout n=1 taskId=com.lab.t"),
        J(63.02, 200, "LAB app=finish taskId=com.lab.t timeout=true after_ms=0 outcome=ok"),
        S(64.0, 200, "- cancel taskId=react-native-background-fetch, jobId=999", "I"),
        S(64.1, 200, "- cancel taskId=react-native-background-fetch, jobId=999", "I"),
        S(64.2, 200, "- cancel taskId=com.lab.t, jobId=-1234", "I"),
        S(65.0, 200, "☯️  HeadlessMode? true"),
        LT(69.0, "I", "LAB     ", 201, "point=appCreate pid=201 process=%s headless=- tasks=x" % pkg),
        S(69.5, 201, "☯️  HeadlessMode? true"),
        S(70.0, 201, "- Background Fetch event received: react-native-background-fetch"),
        S(70.01, 201, "[HeadlessTask] initializing ReactContext"),
        S(70.05, 201, "- Background Fetch event received: com.lab.other"),           # another task's: never paired
        S(70.06, 201, "[HeadlessTask] start taskId: 1"),
        J(70.071, 201, "LAB point=headless event=fetch taskId=react-native-background-fetch timeout=false"),
        S(71.0, 201, "[HeadlessTask] end taskId: 1"),
        LT(71.5, "W", "ReactNativeJS", 201, "No task registered for key BackgroundFetch"),
        S(80.0, 201, "- Background Fetch event received: react-native-background-fetch"),
        S(80.005, 201, "[HeadlessTask] start taskId: 2"),
        J(80.02, 201, "LAB point=headless event=fetch taskId=react-native-background-fetch timeout=false"),
        S(80.5, 201, "[HeadlessTask] end taskId: 2"),
        S(81.0, 201, "[HeadlessTask] Reflection error ReactHost: x"),
        S(81.1, 201, "[HeadlessTask] reflection error addReactInstanceEventListener: x", "E"),
        S(81.2, 201, "[HeadlessTask] task attempted to run in the foreground.  Task ignored.", "E"),
        S(81.3, 201, "[HeadlessTask] Failed invoke HeadlessTask: x"),
        J(81.4, 201, "LAB point=headless event=timeout taskId=react-native-background-fetch timeout=true"),  # no hand-over
        LT(82.0, "I", "LAB     ", 300, "point=appCreate pid=300 process=com.other.lab headless=-"),
        S(82.1, 300, "[HeadlessTask] start taskId: 9"),
        LT(82.2, "W", "ReactNativeJS", 300, "No task registered for key BackgroundFetch"),
        J(82.3, 300, "LAB app=event=fetch n=1 taskId=x"),
        LT(90.0, "I", "LAB     ", 100, "point=capture"),
        S(91.0, 201, "[HeadlessTask] start taskId: 3"),                                   # after the window
    ]
    with open(os.path.join(out, "logcat.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    t0 = 1794171579.0
    meta = {"os": "android", "year": 2026, "measure_from": "launch",
            "timings": {"t0": t0, "phases": {"launch_mark": 1.0, "trigger": 2.0, "capture_extras": 90.5}}}
    history = ("Registered 1 job:\n  JOB #u0a213/999: 3c1 %(p)s/svc.FetchJobService\n"
               "  Job history:\n"
               "     -2h26m50s884ms  STOP-P: #u0a213/999 %(p)s/svc.FetchJobService onStartJob returned false\n"
               "        -2m09s691ms  STOP-P: #u0a213/999 %(p)s/svc.FetchJobService cancelled while waiting for bind\n"
               "          -89s500ms START-P: #u0a213/999 %(p)s/svc.FetchJobService\n"
               "          -89s480ms  STOP-P: #u0a213/999 %(p)s/svc.FetchJobService onStartJob returned false\n"
               "          -40s000ms  STOP-P: #u0a213/999 %(p)s/svc.FetchJobService app called jobFinished\n"
               "          -10s000ms  STOP-P: #u0a99/7 com.other/svc.Job app called jobFinished\n"
               "             -884ms   STOP: #u0a213/-1234 %(p)s/svc.FetchJobService app called jobFinished\n"
               "  Pending queue:\n"
               "     -1s000ms  STOP-P: #u0a213/999 %(p)s/svc.FetchJobService not a history row\n" % {"p": pkg})

    def capture(head_extra=", host_ms=%d" % ((t0 + 91.0) * 1000), meta_=meta, exit_=0):
        with open(os.path.join(out, "run.json"), "w") as f:
            json.dump(meta_, f)
        with open(os.path.join(out, fetch_pack.JOBSCHEDULER), "w") as f:
            f.write("# adb -s emulator-5580 shell dumpsys jobscheduler %s  (exit %d, read 2026-11-05 12:01:31%s)\n%s"
                    % (pkg, exit_, head_extra, history))
        return labkit.Capture(out)
    c = capture()
    names = ("rnHeadlessInit", "rnHeadlessStarts", "rnHeadlessEnds", "rnHeadlessErrors", "headlessNoTask",
             "driverEvents", "driverFinishes", "appProcesses", "fetchTimeoutSkewMs", "headlessLatencyMs",
             "lifecycleTally", "jobsCancelled", "jobStops")
    check("Tier 1 metrics: the app's own processes, in the window",
          {n: labkit.METRICS[n](c) for n in names},
          {"rnHeadlessInit": 1, "rnHeadlessStarts": 2, "rnHeadlessEnds": 2, "rnHeadlessErrors": 4,
           "headlessNoTask": 1, "driverEvents": "fetch=2,timeout=1",
           "driverFinishes": "fetch:ok=1,fetch:withheld=1,timeout:ok=1", "appProcesses": 2,
           "fetchTimeoutSkewMs": 4, "headlessLatencyMs": 71, "lifecycleTally": "headless:true=2,onCreate=1,onStart=1",
           "jobsCancelled": "-1234,999",
           "jobStops": "-1234:app_called_jobFinished=1,999:app_called_jobFinished=1,999:onStartJob_returned_false=1"})
    with open(os.path.join(out, "logcat.txt"), "w") as f:      # a timeout 59.5 s early: |skew|, not skew
        f.write("\n".join(lines[:-2] + [S(84.0, 200, "- Background Fetch event received: com.lab.u"),
                                        S(84.5, 200, "[BGTask] timeout: com.lab.u")] + lines[-2:]) + "\n")
    check("fetchTimeoutSkewMs: an early timeout counts as far as a late one (the largest |skew|)",
          labkit.METRICS["fetchTimeoutSkewMs"](labkit.Capture(out)), 59500)
    with open(os.path.join(out, "logcat.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    check("jobStops: placed by run.json's capture_extras when the header has no host_ms (a capture from before it)",
          labkit.METRICS["jobStops"](capture("")), labkit.METRICS["jobStops"](c))
    check("jobStops: from the trigger mark when the step measures from it (the 89 s-old run falls before it)",
          labkit.METRICS["jobStops"](capture(meta_=dict(meta, measure_from="trigger"))),
          "-1234:app_called_jobFinished=1,999:app_called_jobFinished=1")
    check("jobStops: the header's host_ms wins over run.json's capture_extras (a dump 1.5 s after that phase puts the "
          "89 s-old run after the trigger mark)",
          labkit.METRICS["jobStops"](capture(", host_ms=%d" % ((t0 + 92.0) * 1000), dict(meta, measure_from="trigger"))),
          "-1234:app_called_jobFinished=1,999:app_called_jobFinished=1,999:onStartJob_returned_false=1")
    check("jobStops: no-epoch without timings, unread after a failed read, no-read without a read",
          (labkit.METRICS["jobStops"](capture("", {"os": "android", "year": 2026, "measure_from": "launch"})),
           labkit.METRICS["jobStops"](capture(exit_=255)),
           (os.remove(os.path.join(out, fetch_pack.JOBSCHEDULER)), labkit.METRICS["jobStops"](labkit.Capture(out)))[1]),
          ("no-epoch", "unread", "no-read"))
    check("history_age_ms: days, hours, zero-padded minutes, seconds, ms; not a bare dash",
          [fetch_pack.history_age_ms(a) for a in ("-2h26m50s884ms", "-2m09s691ms", "-40s597ms", "-884ms", "-1d2h", "-")],
          [8810884, 129691, 40597, 884, 93600000, None])
    check("every Tier 1 metric is registered, and the RN HeadlessTask counts beside the SDK's",
          (sorted(n for n in names if n not in labkit.METRICS), sorted(fetch_pack.RN_HEADLESS_COUNTS)),
          ([], ["rnHeadlessEnds", "rnHeadlessInit", "rnHeadlessStarts"]))


def test_restart(tmp):
    """(WO-007) restartOrder on synthetic captures: the host's `point=onDestroy` probe line of the finishing Activity,
    its `current=` against its `activity=`, the app's own processes only, inside the window."""
    pkg = "com.transistorsoft.fetchlab.capacitor"
    A, B, C = "MainActivity@aaa1", "MainActivity@bbb2", "MainActivity@ccc3"
    I = lambda t, pid, msg: LT(t, "I", "LAB     ", pid, msg)
    destroy = lambda t, act, current, finishing="true", pid=200: I(
        t, pid, "point=onDestroy activity=%s isChangingConfigurations=%s isFinishing=%s headless=false "
                "background=false adapter=true tasks=x current=%s"
        % (act, "false" if finishing == "true" else "true", finishing, current))

    def order(name, body, before=()):
        out = os.path.join(tmp, "captures", "capacitor", "restart-" + name, "FIX", "run")
        os.makedirs(out)
        lines = list(before) + [
            I(1.0, 100, "point=launch label=x"),
            I(1.05, 200, "point=appCreate pid=200 process=%s headless=- tasks=-" % pkg),
            I(1.1, 300, "point=appCreate pid=300 process=com.other.lab headless=-"),
            I(2.0, 100, "point=trigger finish"),
        ] + list(body) + [I(40.0, 100, "point=capture")]
        with open(os.path.join(out, "logcat.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
        with open(os.path.join(out, "run.json"), "w") as f:
            json.dump({"os": "android", "year": 2026, "measure_from": "launch"}, f)
        with open(os.path.join(out, fetch_pack.JOBSCHEDULER), "w") as f:
            f.write("# adb -s emulator-5580 shell dumpsys jobscheduler %s  (exit 0, read 2026-11-05 12:00:41)\n" % pkg)
        return labkit.METRICS["restartOrder"](labkit.Capture(out))

    restart = [I(2.1, 200, "point=finishNow activity=%s restart=true" % A),
               I(2.3, 200, "point=onCreate activity=%s pid=200 creates=2 recreated=false headless=false current=%s"
                 % (B, A))]
    check("restartOrder: the new Activity resumed before the old one's destroy",
          order("resumed", restart + [destroy(2.4, A, B), I(2.41, 200, "point=onDestroy:afterSuper activity=%s "
                                                                            "headless=false current=%s" % (A, B))]),
          "resumed-first")
    check("restartOrder: a plain finish (current= is the Activity itself), and a null current=",
          (order("plain", [I(2.1, 200, "point=finishNow activity=%s" % A), destroy(3.0, A, A)]),
           order("null", [I(2.1, 200, "point=finishNow activity=%s restart=true" % A), destroy(3.0, A, "null")])),
          ("destroyed-first", "destroyed-first"))
    check("restartOrder: a finishing Activity other than the one finishNow named is passed over",
          order("named", restart + [destroy(2.35, C, B), destroy(2.4, A, A)]), "destroyed-first")
    check("restartOrder: `-` for an afterSuper line alone, a recreation, another app's process, an early line",
          (order("after", [I(2.4, 200, "point=onDestroy:afterSuper activity=%s isFinishing=true headless=false "
                                       "current=%s" % (A, B))]),                   # only the needle tells it apart
           order("recreate", [destroy(2.4, A, B, finishing="false")]),
           order("foreign", [destroy(2.4, A, B, pid=300)]),
           order("early", [], before=[destroy(0.5, A, B)])),
          ("-", "-", "-", "-"))
    check("restartOrder: with no finishNow line (an old host log), the first finishing destroy is read",
          order("unnamed", [destroy(2.4, A, B)]), "resumed-first")
    check("restartOrder: `no-probe` when the probe failed (no current=)",
          order("probe", [I(2.4, 200, "point=onDestroy activity=%s isChangingConfigurations=false isFinishing=true "
                                      "probeError=NullPointerException" % A)]), "no-probe")
    check("restartOrder: registered, neither implicit nor early; the scenario is in the suite",
          ("restartOrder" in labkit.METRICS, "restartOrder" in labkit.IMPLICIT, "restartOrder" in labkit.EARLY_METRICS,
           "restart-foreground-android" in fetch_pack.SUITE),
          (True, False, False, True))


def test_ios_adapter():
    ev = lambda msg, proc="/x/Runner.app/Runner", sub="": json.dumps({
        "eventType": "logEvent", "timestamp": "2026-11-05 12:00:01.000000-0500", "processID": 77,
        "processImagePath": proc, "subsystem": sub, "eventMessage": msg})
    text = "\n".join(["Filtering the log data using ...", ev("LAB app=boot x=1"),
                      ev("[TSBackgroundFetch scheduleBGAppRefresh] com.transistorsoft.fetch"),
                      ev("[TSBGTask save]: <TSBGTask>"), ev("unrelated line"),
                      ev("Finished <SBBackgroundFetchTask: app<com.transistorsoft.fetchlab.react((null))>; seq: 3> "
                         "with result: RetrievedNewData", proc="/x/CoreServices/SpringBoard.app/SpringBoard")])
    got = backends.to_logcat(text).splitlines()
    check("iOS tap: SDK NSLog lines retagged TSBackgroundFetch, LAB kept, others by process",
          [re.sub(r"^\S+ \S+ ", "", l) for l in got],
          ["I/LAB     (   77): app=boot x=1",
           "I/TSBackgroundFetch(   77): [TSBackgroundFetch scheduleBGAppRefresh] com.transistorsoft.fetch",
           "I/TSBackgroundFetch(   77): [TSBGTask save]: <TSBGTask>", "I/Runner(   77): unrelated line",
           "I/SpringBoard(   77): Finished <SBBackgroundFetchTask: app<com.transistorsoft.fetchlab.react((null))>; "
           "seq: 3> with result: RetrievedNewData"])
    src = open(os.path.join(HARNESS, "backends.py")).read()
    fmt = re.search(r"'\(eventMessage CONTAINS \"LAB \"\) OR \(subsystem == \"%s\"\) OR '\s*"
                    r"'\(eventMessage CONTAINS \"LICENSE VALIDATION FAILURE\"\)' % SDK_SUBSYSTEM", src)
    check("the vendored tap predicate is still a format around SDK_SUBSYSTEM (backends.py:1291)", fmt is not None,
          True)
    predicate = ('(eventMessage CONTAINS "LAB ") OR (subsystem == "%s") OR '
                 '(eventMessage CONTAINS "LICENSE VALIDATION FAILURE")' % backends.SDK_SUBSYSTEM)
    check("the predicate the Simulator's log stream gets", predicate,
          '(eventMessage CONTAINS "LAB ") OR (subsystem == "com.transistorsoft.fetchlab.none") OR '
          '(eventMessage BEGINSWITH "[TSB") OR (process == "SpringBoard" AND eventMessage CONTAINS '
          '"SBBackgroundFetchTask") OR (subsystem == "com.apple.BackgroundTasks") OR '
          '(eventMessage CONTAINS "LICENSE VALIDATION FAILURE")')
    if shutil.which("log"):
        r = subprocess.run(["log", "show", "--last", "1s", "--style", "ndjson", "--predicate", predicate],
                           capture_output=True, text=True, timeout=60)
        check("macOS `log show` accepts the predicate (host syntax check; the Simulator not run)", r.returncode, 0)
    else:
        note("NOT CHECKED: no `log` on this host for the predicate syntax")


def test_ios_phase2a(tmp):
    """(WO-002 phase 2a) The iOS actions' device primitives, and the metrics an iOS capture is scored by."""
    calls = []

    def device_call(argv, timeout=60, text=True):
        calls.append(argv)
        code, err = answer[0]
        return subprocess.CompletedProcess(argv, code, "", err)
    saved = labkit.device_call
    labkit.device_call = device_call
    outcomes = []
    try:
        dev, plat = backends.Simctl("UDID-1"), {"package": "com.transistorsoft.fetchlab.react"}
        for answer in ([(0, "")], [(121, "Notification has no user visible content\n")], [(121, "other\n")],
                       [(1, "No devices are booted.\n")]):
            try:
                outcomes.append(dev.push_fetch(plat))
            except SystemExit as e:
                outcomes.append("refused exit=" + re.search(r"exited (\d+)", str(e)).group(1))
        answer = [(0, "")]
        background = dev.to_background(plat)
        n = len(calls)
        fired = [dev.fire_task(plat, "task", "com.transistorsoft.fetchlab.task")]
        answer = [(1, "notifyutil: bad name\n")]
        try:
            fired.append(dev.fire_task(plat, "task-timeout", "com.transistorsoft.fetchlab.task"))
        except SystemExit as e:
            fired.append("refused exit=" + re.search(r"exited (\d+)", str(e)).group(1))
        fired.append(calls[n])
        del calls[n:]                                   # the push_fetch check below reads calls[-1]: to_background's
    finally:
        labkit.device_call = saved
    check("(ios-task-sim) Simctl.fire_task posts the Darwin notification `<bundle>/<kind>/<taskId>` LabModule observes "
          "(`simctl spawn <udid> notifyutil -p`); a failed post refuses",
          fired, ["fire task taskId=com.transistorsoft.fetchlab.task", "refused exit=1",
                  ["xcrun", "simctl", "spawn", "UDID-1", "notifyutil", "-p",
                   "com.transistorsoft.fetchlab.react/task/com.transistorsoft.fetchlab.task"]])
    verdicts = []
    for item in ({"fire": "fetch"}, {"fire": "task", "taskId": "com.transistorsoft.fetchlab.task"},
                 {"fire": "task-timeout", "taskId": "com.transistorsoft.fetchlab.task"}, {"fire": "task"},
                 {"fire": "fetch", "taskId": "x"}, {"fire": "processing", "taskId": "x"},
                 {"fire": "task", "taskId": "a b"}):
        try:
            verdicts.append(fetch_pack._fire_ok("at", item, {}))
        except SystemExit as e:
            verdicts.append(str(e).split(": ", 1)[1].split(" ")[0])
    check("(ios-task-sim) `fire:` is fetch, or task / task-timeout with a `taskId:` (an identifier); a taskId on a "
          "fetch, a task without one, another kind and a malformed id refuse",
          verdicts, [0, 0, 0, "`fire:", "`taskId:`", "`fire:`", "`taskId:`"])
    check("Simctl.push_fetch: `simctl push <udid> <bundle> <payload>`; exit 0, and 121 only with simctl's "
          "no-visible-content message, are a fired fetch; anything else refuses",
          ([a[:5] for a in calls[:1]], outcomes, background, calls[-1]),
          ([["xcrun", "simctl", "push", "UDID-1", "com.transistorsoft.fetchlab.react"]],
           ["fire fetch push_exit=0", "fire fetch push_exit=121",
            "refused exit=121", "refused exit=1"],
           "background via=com.apple.Preferences", ["xcrun", "simctl", "launch", "UDID-1", "com.apple.Preferences"]))

    def capture(os_name):
        out = os.path.join(tmp, "captures", "react-native", "ios-%s" % os_name, "FIX", "run")
        os.makedirs(out)
        sb = "SBBackgroundFetchTask: app<%s((null))>; seq: %d>"
        lines = [L(1.0, "I", "LAB     ", 100, "point=launch label=x"),
                 L(1.1, "I", "LAB     ", 500, "app=boot n=1 args={}"),
                 L(2.0, "I", "LAB     ", 100, "point=trigger none"),
                 L(2.5, "I", "TSBackgroundFetch", 500, "[TSBackgroundFetch scheduleTask] ERROR:  Failed to submit task "
                   "request: Error Domain=BGTaskSchedulerErrorDomain Code=1 \"(null)\""),
                 L(2.6, "I", "LAB     ", 500, "app=tick n=1 boot=1 status=2 headless=- tasks=t1 pendingFetch=false"),
                 L(2.7, "I", "LAB     ", 500, "point=fire kind=task taskId=t1 found=true ran=true"),
                 L(2.71, "I", "LAB     ", 500, "app=event=fetch n=1 taskId=t1"),
                 L(2.8, "I", "LAB     ", 500, "point=fire kind=task taskId=t1 found=false ran=false"),
                 L(2.9, "I", "LAB     ", 500, "point=fire kind=task-timeout taskId=t2 found=true ran=false"),
                 L(3.0, "I", "SpringBoard", 50, "Executing <" + sb % ("com.transistorsoft.fetchlab.react", 3)),
                 L(3.01, "I", "LAB     ", 500, "point=legacyFetch state=background remaining=30.0"),
                 L(3.02, "I", "TSBackgroundFetch", 500, "[TSBackgroundFetch performFetchWithCompletionHandler]"),
                 L(4.0, "I", "TSBackgroundFetch", 500, "[TSBackgroundFetch finish] react-native-background-fetch (1 of 1)"),
                 L(4.01, "I", "LAB     ", 500, "point=legacyFetchComplete result=newData state=background"),
                 L(4.05, "I", "SpringBoard", 50, "Finished <" + sb % ("com.transistorsoft.fetchlab.react", 3)
                   + " with result: RetrievedNewData"),
                 L(4.06, "I", "SpringBoard", 50, "Finished <" + sb % ("com.transistorsoft.labs.react", 4)
                   + " with result: NoNewDataAvailable"),                                  # bg-forge's lab app
                 L(4.1, "I", "TSBackgroundFetch", 500, "[TSBackgroundFetch finish] x Called without a task to finish.  "
                   "Ignoring."),                                                           # not a finish
                 L(29.01, "I", "LAB     ", 500, "app=event=timeout n=1 taskId=react-native-background-fetch"),
                 L(30.0, "I", "LAB     ", 500, "app=tick n=9 boot=1 status=2 headless=- tasks=- pendingFetch=false"),
                 L(35.0, "I", "LAB     ", 100, "point=capture")]
        with open(os.path.join(out, "logcat.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
        with open(os.path.join(out, "run.json"), "w") as f:
            json.dump({"os": os_name, "actions": [], "year": 2026}, f)
        return labkit.Capture(out)
    names = ("fetchReceived", "fetchFinished", "jobFinished", "legacyFetchStates", "legacyResults", "springboardResults",
             "legacyTimeoutAfterMs")
    ios, android = capture("ios"), capture("android")
    check("iOS metrics: fetchReceived/fetchFinished count the iOS SDK's lines (a finish without its subscriber is "
          "not one), Android's own counts read '-', the legacy fetch, its result and SpringBoard's record are "
          "tallied (a non-fetch lab app's SpringBoard line is not), the timeout's ms after the delivery; on Android the "
          "iOS lines count nothing",
          ([labkit.METRICS[n](ios) for n in names], [labkit.METRICS[n](android) for n in names[:3]]),
          ([1, 1, "-", "background=1", "newData=1", "RetrievedNewData=1", 26000], [0, 0, 0]))
    tasks = ("taskFires", "taskSubmitErrors", "driverTally", "sdkTasks")
    check("(ios-task-sim) the custom-task metrics: the hook's fires by outcome (ran, missing, noCallback), why the "
          "SDK could not submit, the driver's deliveries by taskId, and the last tick's task list",
          [labkit.METRICS[n](ios) for n in tasks],
          ["task-timeout:noCallback=1,task:missing=1,task:ran=1", "BGTaskSchedulerErrorDomain:1=1",
           "fetch:t1=1,timeout:react-native-background-fetch=1", "-"])


def test_ios_registration(tmp):
    """(ios-registration) bgRegistrations and sdkLaunchCalls on a synthetic iOS capture: BGTaskScheduler's lines placed
    per process against its own launch lines, the SDK's look-alike NSLog and another app's lines not counted."""
    fetch, task = "com.transistorsoft.fetch", "com.transistorsoft.fetchlab.task"
    reg = lambda t, pid, ident: L(t, "I", "FetchLab", pid, "registerForTaskWithIdentifier: %s" % ident)
    fw = lambda t, pid, msg, proc="FetchLab": L(t, "I", proc, pid, msg)
    lab = lambda t, pid, msg: L(t, "I", "LAB     ", pid, msg)

    def capture(os_name):
        out = os.path.join(tmp, "captures", "react-native", "reg-%s" % os_name, "FIX", "run")
        os.makedirs(out)
        lines = [lab(1.0, 100, "point=launch label=x"), lab(1.05, 100, "point=trigger none"),
                 # the delegate's call: both registrations before the framework's `Application finished launching`
                 lab(1.10, 500, "point=didFinishLaunching argv=[] state=background sdkLaunchCall=delegate"),
                 lab(1.11, 500, "point=sdkLaunchCall at=delegate"), reg(1.12, 500, fetch),
                 L(1.13, "I", "TSBackgroundFetch", 500, "[TSBackgroundFetch registerForTaskWithIdentifier: %s" % task),
                 reg(1.14, 500, task), lab(1.15, 500, "point=didFinishLaunchingReturn"),
                 fw(1.16, 500, "Application finished launching"), fw(1.20, 500, "Handling app launch"),
                 lab(1.30, 500, "app=boot n=1 args={}"),
                 # +load's: after it, before `Handling app launch`; then a registration after it, and a rejected one
                 lab(2.10, 600, "point=didFinishLaunching argv=[] state=background sdkLaunchCall=scene"),
                 lab(2.11, 600, "point=didFinishLaunchingReturn"), fw(2.12, 600, "Application finished launching"),
                 reg(2.13, 600, fetch), reg(2.14, 600, task), lab(2.15, 600, "point=sdkLaunchCall at=scene"),
                 fw(2.20, 600, "Handling app launch"), reg(2.21, 600, task),
                 fw(2.22, 600, "Registration rejected; zzz is not advertised in the application's Info.plist"),
                 # another app's process: no LAB launch line, not counted
                 fw(3.0, 700, "Application finished launching", "MobileCal"), fw(3.1, 700, "Handling app launch",
                                                                                 "MobileCal"),
                 L(3.2, "I", "MobileCal", 700, "registerForTaskWithIdentifier: %s" % fetch),
                 # an app process that logged neither framework line
                 lab(4.0, 800, "point=didFinishLaunching argv=[] state=background sdkLaunchCall=none"),
                 reg(4.1, 800, fetch),
                 lab(9.0, 100, "point=capture")]
        with open(os.path.join(out, "logcat.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
        with open(os.path.join(out, "run.json"), "w") as f:
            json.dump({"os": os_name, "actions": [], "year": 2026}, f)
        return labkit.Capture(out)
    ios = capture("ios")
    check("(ios-registration) bgRegistrations: launching (before the framework's `Application finished launching`), "
          "finished (before its `Handling app launch`), late (after it), unplaced (neither line), rejected; the SDK's "
          "own NSLog and another app's lines are not registrations",
          labkit.METRICS["bgRegistrations"](ios),
          "%s:finished=1,%s:launching=1,%s:unplaced=1,%s:finished=1,%s:late=1,%s:launching=1,zzz:rejected=1"
          % (fetch, fetch, fetch, task, task, task))
    check("(ios-registration) sdkLaunchCalls: the host's calls by where it made them",
          labkit.METRICS["sdkLaunchCalls"](ios), "delegate=1,scene=1")


def test_ios_smoke(tmp):
    """(api-smoke-ios) bgSubmits, bgCancels and sdkSubscribers on a synthetic iOS capture: BGTaskScheduler's own lines
    in the app's processes, by request kind; the SDK's look-alike NSLogs and another app's lines not counted."""
    fetch, task = "com.transistorsoft.fetch", "com.transistorsoft.fetchlab.task"
    fw = lambda t, pid, msg, proc="FetchLab": L(t, "I", proc, pid, msg)
    lab = lambda t, pid, msg: L(t, "I", "LAB     ", pid, msg)
    tick = "app=tick n=%d boot=1 status=2 headless=- configured=true subscribers=%s tasks=- pendingFetch=false"

    def capture(name, os_name, ticks=True):
        out = os.path.join(tmp, "captures", "react-native", "api-smoke-ios-%s" % name, "FIX", "run")
        os.makedirs(out)
        lines = [lab(1.0, 100, "point=launch label=x"), lab(1.05, 100, "point=trigger none"),
                 lab(1.1, 500, "point=didFinishLaunching argv=[] state=background sdkLaunchCall=delegate"),
                 L(1.2, "I", "TSBackgroundFetch", 500, "[TSBackgroundFetch scheduleBGAppRefresh] %s" % fetch),
                 fw(1.21, 500, "submitTaskRequest: <BGAppRefreshTaskRequest: %s, earliestBeginDate: 2026-11-11 "
                               "16:59:36 +0000>" % fetch),
                 fw(1.3, 500, "submitTaskRequest: <BGProcessingTaskRequest: %s, earliestBeginDate: 2026-11-11 "
                               "16:54:36 +0000, requiresExternalPower=0, requiresNetworkConnectivity=0>" % task),
                 fw(1.4, 500, "cancelTaskRequestWithIdentifier: %s" % task),
                 fw(1.5, 500, "cancelTaskRequestWithIdentifier: %s" % fetch),
                 # another app's process: no LAB launch line, not counted
                 fw(1.6, 700, "submitTaskRequest: <BGAppRefreshTaskRequest: com.apple.x, earliestBeginDate: (null)>",
                    "MobileCal"),
                 fw(1.7, 700, "cancelTaskRequestWithIdentifier: com.apple.x", "MobileCal")]
        if ticks:
            lines += [lab(2.0, 500, tick % (1, "react-native-background-fetch")), lab(3.0, 500, tick % (2, "-"))]
        lines.append(lab(9.0, 100, "point=capture"))
        with open(os.path.join(out, "logcat.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
        with open(os.path.join(out, "run.json"), "w") as f:
            json.dump({"os": os_name, "actions": [], "year": 2026}, f)
        return labkit.Capture(out)
    names, ios = ("bgSubmits", "bgCancels", "sdkSubscribers"), capture("ios", "ios")
    check("(api-smoke-ios) bgSubmits by request kind, bgCancels, and the last tick's subscribers; another app's "
          "requests are not the app's, and a capture with no probe reads no-probe",
          ([labkit.METRICS[n](ios) for n in names],
           labkit.METRICS["sdkSubscribers"](capture("noprobe", "ios", ticks=False))),
          (["%s:appRefresh=1,%s:processing=1" % (fetch, task), "%s=1,%s=1" % (fetch, task), "-"], "no-probe"))


def test_relaunch():
    """(WO-002 phase 2a) The contamination wrap: on iOS a boot after the step's own `terminate` is the app relaunched."""
    def lines(terminate, boots=2):
        out = [L(1.0, "I", "LAB     ", 100, "point=launch label=lbl"), L(1.1, "I", "LAB     ", 500, "app=boot n=1 args={}")]
        if terminate:
            out.append(L(2.0, "I", "LAB     ", 101, "point=host i=0 action=terminate sent_ms=1 done_ms=2 mark_ms=2"))
        out += [L(3.0 + k, "I", "LAB     ", 600 + k, "app=boot n=1 args={}") for k in range(boots - 1)]
        return out
    got = [devices.contamination(lines(True), "lbl", "ios"), devices.contamination(lines(False), "lbl", "ios"),
           devices.contamination(lines(True, boots=3), "lbl", "ios")]
    check("(WO-002 phase 2a) iOS contamination: a boot after the step's own `terminate` is the relaunched app; "
          "without one, or a second boot after one terminate, it is still another lab app",
          got, [[], ["lab process pid 600 booted mid-step"], ["lab process pid 601 booted mid-step"]])


# ---------------------------------------------------------------------------------------------------- host actions

class FakeDev:
    os = "android"

    def __init__(self):
        self.serial, self.lines, self.marks, self.calls, self.lock = "emulator-0", [], [], [], threading.Lock()
        self.t = 0.0

    def add(self, msg, tag="LAB     ", pid=100):
        with self.lock:
            self.t += 0.001
            self.lines.append(L(10 + self.t, "I", tag, pid, msg))

    def peek(self):
        with self.lock:
            return list(self.lines)

    def mark(self, text):
        self.marks.append(text)
        self.add("point=" + text, pid=999)

    def job(self, plat, value, expect="present", unspaced=None):
        self.calls.append(("job", value) if expect == "present" else ("job", value, expect))
        return "job %d" % fetch_pack.job_id(value)

    def force_stop(self, plat):
        self.calls.append(("force_stop",))
        return "force_stop pid=4242"

    def kill(self, plat):
        self.calls.append(("kill",))
        return "pid=4242"


def test_runner():
    dev, host, stop = FakeDev(), {"error": None, "held_s": {}, "sent_s": {}}, threading.Event()
    step = {"moves": None, "actions": [
        {"after": [{"point": "go"}], "delay_s": 0.2, "job": 999},
        {"after": [{"point": "go"}], "kill": True},
        {"after": [{"point": "host"}], "force_stop": True},
        {"after": [{"point": "never-shown"}], "job": "com.lab.task"},
        {"after": [{"point": "go"}], "delay_s": 0.1, "job": 7, "expect": "absent"}]}
    t0 = time.time()
    th = threading.Thread(target=run.run_host, args=(dev, step, t0, t0 + 6, stop, host, None, {"package": "p"}))
    th.start()
    dev.add("app=boot x=1")
    dev.add("point=go")
    deadline = time.time() + 5
    while time.time() < deadline and len(dev.calls) < 4:
        time.sleep(0.05)
    stop.set()
    th.join(10)
    check("runner: the vendored kill and fetch's jobs and force_stop each ran once, `expect` passed through",
          sorted(dev.calls, key=repr), sorted([("job", 999), ("kill",), ("force_stop",), ("job", 7, "absent")], key=repr))
    check("runner: marks keep each action's own index",
          sorted(re.sub(r" sent_ms=.*", "", m) for m in dev.marks if m.startswith("host")),
          ["host i=0 action=job 999", "host i=1 action=kill pid=4242", "host i=2 action=force_stop pid=4242",
           "host i=4 action=job 7"])
    check("runner: the job waited its delay_s after `go`", host["sent_s"]["action0"] - host["held_s"]["action0"] >= 0.2,
          True)
    check("runner: an action whose condition never came is not sent", "action3" in host["sent_s"], False)
    check("runner: no error", host["error"], None)
    check("runner: one runner for every kind, the vendored run_host (ACTION_HANDLERS)",
          run.run_host.__module__ in ("run", "__main__"), True)


def test_primitives():
    answers = []

    def device_call(argv, timeout=60, text=True):
        answers.append(argv)
        said = {"999": "Running job [FORCED]\n", "7": "", "8": "", "9": "Running job [FORCED]\n"}.get(argv[-1], "")
        err = {"7": "Could not find job 7 in package p / user 0\n",
               "8": "Package not found: p / user 0\n"}.get(argv[-1], "")
        return subprocess.CompletedProcess(argv, 0 if said else 255, said, err)
    saved, saved_repeat = labkit.device_call, fetch_pack.JOB_REPEAT_S
    labkit.device_call, fetch_pack.JOB_REPEAT_S = device_call, 0.3
    try:
        dev, plat = backends.Adb("emulator-0"), {"package": "p"}
        first = dev.job(plat, 999)
        second = dev.job(plat, 999)
        missed = dev.job(plat, 7)
        gone = dev.job(plat, 7, "absent")
        still = dev.job(plat, 9, "absent")
        absent = refused(lambda: dev.job(plat, 8))
        # (WO-002 Tier 1) `unspaced`: right after a run of the same job, with a repeat wait that would take 30 s.
        fetch_pack.JOB_REPEAT_S = 30
        t = time.time()
        inside = dev.job(plat, 999, "present", "2026-11-09 on purpose")
        inside_s = time.time() - t
        fresh = backends.Adb("emulator-1").job(plat, 999, "present", "2026-11-09 the first here")
        # The action kind's runner hands `unspaced` to the device (run_host calls it so): no wait, its mark.
        handled = labkit.ACTION_HANDLERS["job"].run(dev, {"job": 999, "unspaced": "2026-11-09 via the handler"},
                                                     plat, None)
    finally:
        labkit.device_call, fetch_pack.JOB_REPEAT_S = saved, saved_repeat
    check("Adb.job: the command", answers[0], ["adb", "-s", "emulator-0", "shell", "cmd", "jobscheduler", "run", "-f",
                                              "p", "999"])
    check("Adb.job: forced run, JobScheduler's answer in the mark", first, "job 999 said=Running_job_FORCED")
    check("Adb.job: a repeat waits out the duplicate guard", bool(re.fullmatch(
        r"job 999 waited_ms=\d+ said=Running_job_FORCED", second)), True)
    check("Adb.job: no such job is a scored miss", missed,
          "job 7 missed=no-job said=Could_not_find_job_7_in_package_p_user_0")
    check("Adb.job expect=absent: no such job is the expectation met (a repeat of job 7: it waited)", bool(
        re.fullmatch(r"job 7 expect=absent waited_ms=\d+ said=Could_not_find_job_7_in_package_p_user_0", gone)), True)
    check("Adb.job expect=absent: a job that runs is a scored miss", still,
          "job 9 expect=absent missed=present said=Running_job_FORCED")
    check("Adb.job: no such package refuses", "is not installed" in (absent or ""), True)
    check("Adb.job unspaced: no repeat wait (30 s here), the ms since the job last ran in the mark; '-' for a first run",
          (inside_s < 5, bool(re.fullmatch(r"job 999 unspaced since_ms=\d+ said=Running_job_FORCED", inside)), fresh),
          (True, True, "job 999 unspaced since_ms=- said=Running_job_FORCED"))
    check("the `job` action kind passes `unspaced` through to Adb.job (the runner's handler, not only the primitive)",
          bool(re.fullmatch(r"job 999 unspaced since_ms=\d+ said=Running_job_FORCED", handled)), True)
    dumps = []

    def dump_call(argv, timeout=60, text=True):
        dumps.append(argv)
        return subprocess.CompletedProcess(argv, 0, "Registered 2 jobs:\n  JOB #u0a1/999: 1 p/x.FetchJobService\n"
                                                    "  JOB #u0a9/7: 2 q/y\n", "")
    labkit.device_call = dump_call
    try:
        dev, pids = backends.Adb("emulator-0"), iter(["4242", None])
        dev.alive = lambda plat: next(pids, None)
        dev.terminate = lambda plat: dumps.append("terminate")
        stopped = dev.force_stop({"package": "p"})
    finally:
        labkit.device_call = saved
    check("Adb.force_stop: reads the jobs held, then force-stops", (stopped, dumps[0][-3:], dumps[1]),
          ("force_stop pid=4242 held=999", ["dumpsys", "jobscheduler", "p"], "terminate"))
    check("START_PROC: a `<pkg>:<sub>` process", fetch_pack.START_PROC.search(
        "Start proc 77:com.x.app:remote/u0a1 for service").groups(), ("77", "com.x.app:remote"))
    check("java_hash: 'hello'", fetch_pack.java_hash("hello"), 99162322)
    check("java_hash: 'polygenelubricants' (Integer.MIN_VALUE)", fetch_pack.java_hash("polygenelubricants"),
          -2147483648)
    check("java_hash: ''", fetch_pack.java_hash(""), 0)
    check("job_id: 999 and a taskId", [fetch_pack.job_id(999), fetch_pack.job_id("hello")], [999, 99162322])


def test_seams():
    rows = fetch_pack.OVERRIDES
    check("every override names a vendored file:line", [o["symbol"] for o in rows
                                                       if not re.search(r"\w+\.(py|js):\d+", o["seam"])], [])
    check("every patched module's patch is registered (labkit registers itself: lab_product.py; inventory reads "
          "labkit.NATIVE)", sorted(fetch_pack.PATCHES), ["backends", "devices", "lab_setup", "run", "sweep"])
    import types
    # A re-vendor that renames a seam: the pack refuses instead of writing a dead attribute (WO-002 review).
    renamed = types.ModuleType("sweep")
    renamed.LISTS = {}
    why = None
    try:
        fetch_pack.PATCHES["sweep"](renamed)
    except fetch_pack.PackError as e:
        why = str(e)
    check("a vanished seam stops the patch (sweep without SUITE)", "has no `SUITE`" in (why or ""), True)
    # Vendored logic the pack copies: pinned by source sha256 (Simctl.native_resolved, its re-implementation).
    resolved = backends.Simctl.native_resolved.vendored
    vendored = types.SimpleNamespace(Simctl=types.SimpleNamespace(native_resolved=resolved))
    check("PINNED backends.Simctl.native_resolved matches the vendored source", fetch_pack.source_sha256(resolved),
          fetch_pack.PINNED["backends.Simctl.native_resolved"])
    check("WO-191 unpinned the four copies its hooks replaced",
          sorted(q for q in ("run.run_host", "labkit.Capture.sdk", "lab_setup.published", "lab_setup.ensure_native")
                 if q in fetch_pack.PINNED), [])
    saved = dict(fetch_pack.PINNED)
    fetch_pack.PINNED["backends.Simctl.native_resolved"] = "0" * 64
    try:
        fetch_pack._check_pinned("backends", vendored)
        why = None
    except fetch_pack.PackError as e:
        why = str(e)
    finally:
        fetch_pack.PINNED.clear()
        fetch_pack.PINNED.update(saved)
    check("a changed pinned function stops the patch", "changed" in (why or "") and "PINNED" in (why or ""), True)
    r = subprocess.run([sys.executable, "-B", "-c", "import sys, types; sys.path.insert(0, %r); import fetch_pack; "
                        "fetch_pack._apply('sweep', types.ModuleType('sweep'), False)" % HARNESS],
                       capture_output=True, text=True, timeout=60,
                       env={k: v for k, v in os.environ.items() if k not in ("FETCH_LABKIT_PACK", "PYTHONPATH")})
    check("activation over a vanished seam exits 2, saying why", (r.returncode, "refusing to run" in r.stderr), (2, True))
    # A stand-in `run` with no assert_native: importing the real one now loads labkit, whose lab_product.py refuses a
    # process without the pack (exit 2 too, but for another reason than the one under test).
    r = subprocess.run([sys.executable, "-B", "-c", "import sys, types; sys.path.insert(0, %r); import fetch_pack; "
                        "fetch_pack._apply('run', types.ModuleType('run'), False)" % HARNESS],
                       capture_output=True, text=True, timeout=60,
                       env={k: v for k, v in os.environ.items() if k not in ("FETCH_LABKIT_PACK", "PYTHONPATH")})
    check("a wrapped seam that vanished (a bare read: AttributeError) also exits 2, saying why",
          (r.returncode, "patching vendored run failed (AttributeError" in r.stderr), (2, True))
    taken = types.SimpleNamespace(job=1)
    for obj, name in ((taken, "job"), ({"jobIds": 1}, "jobIds")):
        try:
            fetch_pack._extend(obj, name, 2)
            why = None
        except fetch_pack.PackError as e:
            why = str(e)
        check("_extend refuses a name the vendored side already has (%s)" % name, "already has" in (why or ""), True)


def test_hooks(tmp):
    """(bg-forge WO-191) The hooks register() sets that no capture or scenario test reaches: CAPTURE_EXTRAS, and NATIVE
    through lab_setup (published(), ensure_native's slots).  Every command is recorded, none run."""
    import contextlib
    import types
    import lab_setup
    calls, out_a, out_i = [], os.path.join(tmp, "extras-a"), os.path.join(tmp, "extras-i")
    os.makedirs(out_a)
    os.makedirs(out_i)
    real_call = labkit.device_call
    try:
        labkit.device_call = lambda argv, **kw: (calls.append(argv),
                                                 subprocess.CompletedProcess(argv, 0, "Registered 1 jobs:\n", ""))[1]
        labkit.CAPTURE_EXTRAS(types.SimpleNamespace(os="android", serial="emulator-0"), {"package": "com.lab.p"}, out_a)
        labkit.CAPTURE_EXTRAS(types.SimpleNamespace(os="ios", serial="UDID"), {"package": "com.lab.p"}, out_i)
    finally:
        labkit.device_call = real_call
    head = open(os.path.join(out_a, fetch_pack.JOBSCHEDULER)).readline()
    check("CAPTURE_EXTRAS: Android's `dumpsys jobscheduler <pkg>` into jobscheduler.txt, whose header names the package "
          "own_pids reads and the host clock jobStops places the history by; nothing on iOS",
          (calls, head.startswith("# adb -s emulator-0 shell dumpsys jobscheduler com.lab.p  (exit 0"),
           bool(re.search(r", host_ms=\d{13}\)$", head.strip())),
           fetch_pack._package_of(types.SimpleNamespace(path=out_a)), os.listdir(out_i)),
          ([["adb", "-s", "emulator-0", "shell", "dumpsys", "jobscheduler", "com.lab.p"]], True, True, "com.lab.p", []))

    rec, runs = [], []
    saved = (lab_setup.sh, lab_setup.native_checkout, lab_setup.LABS)
    try:
        lab_setup.sh = lambda cmd, cwd=None, env=None, log=None: rec.append(
            [os.path.basename(cmd[0])] + cmd[1:] + (["LANG=" + env["LANG"]] if env and "LANG" in env else []) +
            ([os.path.basename(log)] if log else []))
        lab_setup.native_checkout = lambda sub, sha, name: (rec.append(["checkout", sub, sha[:1], name]), "/wt")[1]
        lab_setup.LABS = os.path.join(tmp, "labs")
        for rel in (("m2", "com", "transistorsoft", "tsbackgroundfetch", "L"), ("m2", ".provenance"),
                    ("xcframeworks", "L", "TSBackgroundFetch.xcframework"), ("xcframeworks", ".provenance")):
            os.makedirs(os.path.join(lab_setup.LABS, *rel), exist_ok=True)
        for store in ("m2", "xcframeworks"):
            open(os.path.join(lab_setup.LABS, store, ".provenance", "L.txt"), "w").close()
        pub = (lab_setup.published("L", "android"), lab_setup.published("L", "ios"), lab_setup.published("M", "android"))
        for label, src, oses in (("M", {"android": "a" * 40}, ["android"]), ("L", {}, ["android", "ios"]),
                                 ("M", {"ios": "b" * 40}, ["ios"]), ("M", {}, ["android"])):
            del rec[:]
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    lab_setup.ensure_native({"name": "V", "native": label, "native_source": src}, oses, "/logs")
                end = "ok"
            except SystemExit as e:
                end = str(e)
            runs.append((list(rec), [l for l in out.getvalue().splitlines() if l.startswith("ok:")], end))
    finally:
        lab_setup.sh, lab_setup.native_checkout, lab_setup.LABS = saved
    # The pack's check_repo wrapper reports a variant that does not load as a MISSING line; labkit's own exit 2 (its
    # product did not load) must pass through it, never read as a missing variant (review).
    outcomes, saved_lv = [], lab_setup.load_variant
    try:
        for exc in (SystemExit(2), SystemExit("variant FIX: no such label")):
            lab_setup.load_variant = lambda name, exc=exc: (_ for _ in ()).throw(exc)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    outcomes.append(("returned", lab_setup.check_repo(lab_setup.Report(), ["android"], "FIX")))
            except SystemExit as e:
                outcomes.append(("raised", e.code))
    finally:
        lab_setup.load_variant = saved_lv
    check("doctor's check_repo: labkit's exit 2 passes through, a variant that does not load is a MISSING line",
          outcomes, [("raised", 2), ("returned", None)])
    import inventory
    check("NATIVE: published() reads tsbackgroundfetch and TSBackgroundFetch.xcframework, and inventory prunes it",
          (pub, inventory.ANDROID_ARTIFACTS), ((True, True, False), ("tsbackgroundfetch",)))
    check("NATIVE: an unpublished Android label is built by fetch's slot (native/background-fetch as `native`, "
          "publish_native_fetch.sh); a published one needs nothing; an unpublished iOS label is built by fetch's iOS "
          "slot from the same worktree (publish_native_fetch_ios.sh, LANG set for xcodebuild); no native_source is the "
          "shared refusal",
          runs,
          [([["checkout", "native/background-fetch", "a", "native"], ["publish_native_fetch.sh", "M", "/wt",
                                                                      "native-android.log"]], [], "ok"),
           ([], ["ok: android L is published", "ok: ios L is published"], "ok"),
           ([["checkout", "native/background-fetch", "b", "native"], ["publish_native_fetch_ios.sh", "M", "/wt",
                                                                      "LANG=en_US.UTF-8", "native-ios.log"]], [], "ok"),
           ([], [], "setup stopped: M is not published for android, and variant V records no native_source.android to "
                    "build it from")])


def test_shim(tmp):
    """The pack's sitecustomize shim chains the interpreter's own sitecustomize and never another fetch pack's.  Two
    packs on PYTHONPATH (another checkout's, left by `./labkit env` there, behind the one `./labkit` puts first) or one
    reached through a symlink used to chain each other until RecursionError: every harness process exited 2."""
    base = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "FETCH_LABKIT_PACK", "FETCH_PACK_REPORT")}
    own = subprocess.run([sys.executable, "-c", "import sitecustomize; print(sitecustomize.__file__)"], env=base,
                         capture_output=True, text=True, timeout=60)
    interpreters = os.path.realpath(own.stdout.strip()) if own.returncode == 0 and own.stdout.strip() else None
    other = os.path.join(tmp, "shim-other", "harness")
    os.makedirs(other)
    shutil.copytree(os.path.join(HARNESS, "pack"), os.path.join(other, "pack"))
    shutil.copy2(os.path.join(HARNESS, "fetch_pack.py"), other)
    link = os.path.join(tmp, "shim-link")
    os.symlink(HARNESS, link)
    probe = ("import os, sys, sitecustomize as s; c = getattr(s, '_chained', None); "
             "print(os.path.realpath(sys.modules['fetch_pack'].HARNESS)); print(os.path.realpath(c.__file__) if c else None)")
    got = []
    for path in ([os.path.join(HARNESS, "pack"), os.path.join(other, "pack")],
                 [os.path.join(link, "pack"), os.path.join(HARNESS, "pack")]):
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
                           env=dict(base, FETCH_LABKIT_PACK="1", PYTHONPATH=os.pathsep.join(path)))
        got.append((r.returncode, r.stdout.split("\n")[:2] if r.returncode == 0 else r.stderr[-300:]))
    check("the pack's shim: two packs on PYTHONPATH, or one spelled through a symlink, load the first one's pack and "
          "chain exactly the interpreter's own sitecustomize (no RecursionError)",
          got, [(0, [os.path.realpath(HARNESS), str(interpreters)])] * 2)


def test_live():
    """configureOutcome's live half (evidence_state) and the expectations a `process: live` step gets."""
    step = {"hooks": [{"api": "status"}], "expect": {"crashes": 0}}
    base = [L(1, "I", "ReactNativeJS", 200, "LAB app=boot n=1 args={}"), L(2, "I", "ReactNativeJS", 200,
                                                                         "LAB app=api-done n=1")]
    cfg = lambda *x: [L(1.5, "I", "ReactNativeJS", 200, m) for m in x]
    check("evidence: not complete before the driver's boot configure decision",
          labkit.evidence_state(base, step, "android")[0], False)
    check("evidence: not complete while a configure() is unsettled", labkit.evidence_state(
        base + cfg("LAB app=config json={}", "LAB app=configure source=boot outcome=skipped reason=event-arm"),
        step, "android")[0], False)
    check("evidence: complete once it settled", labkit.evidence_state(
        base + cfg("LAB app=config json={}", "LAB app=configure source=boot outcome=ok status=2"), step, "android")[0],
        True)
    check("evidence: a `process: live` step needs no configure line", labkit.evidence_state(
        base, dict(step, process="live"), "android")[0], True)
    check("a hooks-only step still ends on its evidence with the pack's implicit metrics",
          labkit.end_policy(step, "react-native"), "evidence")
    check("expectations: configureOutcome ok implied", labkit.expectations(step, "react-native").get(
        "configureOutcome"), "ok")
    check("expectations: a `process: live` step expects configureOutcome '-'", labkit.expectations(
        dict(step, process="live"), "react-native").get("configureOutcome"), "-")
    check("expectations: a stated configureOutcome wins on a live step", labkit.expectations(
        dict(step, process="live", expect={"configureOutcome": "ok"}), "react-native").get("configureOutcome"), "ok")
    check("CALLBACK_ARG keeps the vendored names and adds fetch's",
          [bool(labkit.CALLBACK_ARG.search(n)) for n in ("cb", "errorCallback", "onEvent", "task", "config")],
          [True, True, True, True, False])


def test_shipped():
    """Every scenario and variant this lab ships loads through the vendored loader, with the pack."""
    for path in sorted(os.listdir(os.path.join(HARNESS, "scenarios"))):
        if path.endswith(".yaml"):
            name = path[:-5]
            check("scenario %s loads" % name, refused(lambda: labkit.scenario(name)), None)
    for path in sorted(os.listdir(os.path.join(HARNESS, "variants"))):
        if path.endswith(".yaml"):
            name = path[:-5]
            try:
                labkit.variant(name)
                why = None
            except (SystemExit, FileNotFoundError) as e:
                why = str(e)
            check("variant %s loads" % name, why, None)
    # (WO-002) Every platform this lab ships registers with its board column, board name and hosts; a new host's
    # lab.yaml that loses one shows up here, not as a board with a missing or misplaced column.
    for name, column, short, hosts in (("react-native", 2, "rn", ["android", "ios"]),
                                       ("capacitor", 3, "cap", ["android"])):
        check("platform %s column" % name, labkit.platform(name).get("column"), column)
        check("platform %s board name" % name, labkit.board_name(name), short)
        check("platform %s hosts" % name, labkit.hosts(name), hosts)
    check("board order", labkit.board_order(), ["react-native", "capacitor"])


def main():
    sys.stdout.reconfigure(line_buffering=True)
    tmp = tempfile.mkdtemp(prefix="fetch-selftest-")
    try:
        for test in (test_seams, test_vocabulary, lambda: test_scenarios(tmp), lambda: test_capture(tmp),
                     lambda: test_tier1(tmp), lambda: test_restart(tmp),
                     test_ios_adapter, lambda: test_ios_phase2a(tmp),
                     lambda: test_ios_registration(tmp), lambda: test_ios_smoke(tmp), test_relaunch, test_primitives, test_runner, lambda: test_hooks(tmp), lambda: test_shim(tmp),
                     test_live, test_shipped,
                     lambda: test_activation(tmp)):
            try:
                test()
            except (SystemExit, Exception) as e:           # a refusal or a traceback is a failed check, not an exit
                check("%s ran to its end" % getattr(test, "__name__", "test"), "%s: %s" % (type(e).__name__, e), None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = RESULTS.count(False)
    print("fetch selftest: %s (%d checks%s)" % ("PASS" if not failed else "FAIL", len(RESULTS),
                                                ", %d failed" % failed if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
