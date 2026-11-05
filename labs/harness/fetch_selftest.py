#!/usr/bin/env python3
"""The fetch lab's offline selftest (WO-002): no device, no build.  `./labkit selftest` runs it.

What it holds:
  * the pack is active here AND in a subprocess started the way sweep.py starts run.py (sweep.capped, sweep.py:644),
    and one level further down; it is inert where FETCH_LABKIT_PACK is not set;
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
fetch_pack.activate()                                 # and here when the selftest is run on its own
import backends                                       # noqa: E402
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
    check("labkit.Capture.sdk is the pack's", labkit.Capture.sdk.__module__, "fetch_pack")
    check("run.run_host is the pack's wrapper", getattr(run.run_host, "vendored", None) is not None, True)
    check("sweep.SUITE is fetch's", sweep.SUITE, fetch_pack.SUITE)
    check("sweep.scenario_list('suite')", sweep.scenario_list("suite"), fetch_pack.SUITE)

    # (1) The way sweep.py starts a run (sweep.py:644 via sweep.capped, :247): the vendored function, real argv.
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
        # (3) Inert without the flag: the same start, FETCH_LABKIT_PACK unset.
        bare = dict(env)
        bare.pop("FETCH_LABKIT_PACK")
        inert = subprocess.run([sys.executable, os.path.join(HARNESS, "run.py"), "--help"], env=bare,
                               stdout=subprocess.DEVNULL, timeout=120).returncode
    finally:
        os.environ.clear()
        os.environ.update(saved)
    rows = [json.loads(l) for l in open(report)] if os.path.exists(report) else []
    pids = sorted({r["pid"] for r in rows if r["pid"] != os.getpid()})
    check("run.py --help via sweep.capped exits 0", rc, 0)
    check("nested run.py --help exits 0", nested, 0)
    check("bare run.py --help exits 0", inert, 0)
    check("two pack-activated child processes reported (sweep.capped's, the nested one's)", len(pids), 2)
    for pid in pids[:2]:
        mine = {r["module"]: r for r in rows if r["pid"] == pid}
        check("child %d: run.py patched as __main__ (run_host, run_step, assert_native)" % pids.index(pid),
              (mine.get("run") or {}).get("as_main"), True)
        check("child %d: labkit and backends patched on import" % pids.index(pid),
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
    check("headless names (vocabulary enums.Event + labkit's locationerror)", labkit.headless_names(),
          ["fetch", "timeout", "locationerror"])
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


def test_ios_adapter():
    ev = lambda msg, proc="/x/Runner.app/Runner", sub="": json.dumps({
        "eventType": "logEvent", "timestamp": "2026-11-05 12:00:01.000000-0500", "processID": 77,
        "processImagePath": proc, "subsystem": sub, "eventMessage": msg})
    text = "\n".join(["Filtering the log data using ...", ev("LAB app=boot x=1"),
                      ev("[TSBackgroundFetch scheduleBGAppRefresh] com.transistorsoft.fetch"),
                      ev("[TSBGTask save]: <TSBGTask>"), ev("unrelated line")])
    got = backends.to_logcat(text).splitlines()
    check("iOS tap: SDK NSLog lines retagged TSBackgroundFetch, LAB kept, others by process",
          [re.sub(r"^\S+ \S+ ", "", l) for l in got],
          ["I/LAB     (   77): app=boot x=1",
           "I/TSBackgroundFetch(   77): [TSBackgroundFetch scheduleBGAppRefresh] com.transistorsoft.fetch",
           "I/TSBackgroundFetch(   77): [TSBGTask save]: <TSBGTask>", "I/Runner(   77): unrelated line"])
    src = open(os.path.join(HARNESS, "backends.py")).read()
    fmt = re.search(r"'\(eventMessage CONTAINS \"LAB \"\) OR \(subsystem == \"%s\"\) OR '\s*"
                    r"'\(eventMessage CONTAINS \"LICENSE VALIDATION FAILURE\"\)' % SDK_SUBSYSTEM", src)
    check("the vendored tap predicate is still a format around SDK_SUBSYSTEM (backends.py:1291)", fmt is not None,
          True)
    predicate = ('(eventMessage CONTAINS "LAB ") OR (subsystem == "%s") OR '
                 '(eventMessage CONTAINS "LICENSE VALIDATION FAILURE")' % backends.SDK_SUBSYSTEM)
    check("the predicate the Simulator's log stream gets", predicate,
          '(eventMessage CONTAINS "LAB ") OR (subsystem == "com.transistorsoft.fetchlab.none") OR '
          '(eventMessage BEGINSWITH "[TSB") OR (eventMessage CONTAINS "LICENSE VALIDATION FAILURE")')
    if shutil.which("log"):
        r = subprocess.run(["log", "show", "--last", "1s", "--style", "ndjson", "--predicate", predicate],
                           capture_output=True, text=True, timeout=60)
        check("macOS `log show` accepts the predicate (host syntax check; the Simulator not run)", r.returncode, 0)
    else:
        note("NOT CHECKED: no `log` on this host for the predicate syntax")


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

    def job(self, plat, value, expect="present"):
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
    check("runner: a step with no fetch action goes straight to the vendored runner",
          run.run_host.vendored.__module__ in ("run", "__main__"), True)


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
    check("every patched module's patch is registered", sorted(fetch_pack.PATCHES),
          ["backends", "inventory", "lab_setup", "labkit", "run", "sweep"])
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
    # Vendored logic the pack copies: pinned by source sha256 (the run_host copy's approval condition).
    vendored = types.SimpleNamespace(run_host=run.run_host.vendored)
    check("PINNED run.run_host matches the vendored source", fetch_pack.source_sha256(run.run_host.vendored),
          fetch_pack.PINNED["run.run_host"])
    saved = dict(fetch_pack.PINNED)
    fetch_pack.PINNED["run.run_host"] = "0" * 64
    try:
        fetch_pack._check_pinned("run", vendored)
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
    r = subprocess.run([sys.executable, "-B", "-c", "import sys; sys.path.insert(0, %r); import run, fetch_pack; "
                        "del run.run_step; fetch_pack._apply('run', run, False)" % HARNESS],
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


def main():
    sys.stdout.reconfigure(line_buffering=True)
    tmp = tempfile.mkdtemp(prefix="fetch-selftest-")
    try:
        for test in (test_seams, test_vocabulary, lambda: test_scenarios(tmp), lambda: test_capture(tmp),
                     test_ios_adapter, test_primitives, test_runner, test_live, test_shipped,
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
