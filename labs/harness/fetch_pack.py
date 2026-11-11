"""The fetch pack (WO-002): every place background-fetch differs from the vendored bg-forge harness, in ONE module.

The harness beside this file is bg-forge's, vendored byte-identical (labs/VENDORED.md, D1: commit 23f448c42).  It
carries background-geolocation names at known seams.  Nothing here edits a vendored file, and each override names the
vendored line it stands in for (`SEAM`).

labkit.py's seams are the product hooks bg-forge grew for this pack (bg-forge WO-191: ACTION_HANDLERS, SDK_LOG,
EVIDENCE, CAPTURE_EXTRAS, HEADLESS_NAMES, NATIVE, …).  labkit loads labs/harness/lab_product.py itself, at the end of
its own definitions, and lab_product.py calls register() below: so labkit gets fetch's seams in every process that
imports it, with no post-import patch.  The other vendored modules (backends, run, sweep, lab_setup) have no such hook
until Phase B: their overrides rebind names after the module is imported, by the activation below — and since
register() runs while backends.py is still loading, lab_product.py refuses a process without that activation (exit 2):
it would mix fetch's labkit seams with bg-geo's backends, run and sweep.

How the activation is made active in EVERY Python process the harness starts:
  * `./labkit` exports PYTHONPATH=labs/harness/pack and FETCH_LABKIT_PACK=1.  labs/harness/pack/sitecustomize.py
    (chaining whatever sitecustomize the interpreter ships, Homebrew's included) calls activate().
  * activate() installs a post-import hook: when a vendored module (`backends`, `run`, …) is imported from
    labs/harness, its patches run right after its body does.
  * A vendored SCRIPT run as __main__ (`run.py`, which sweep.py starts as `sys.executable -u run.py` at
    sweep.py:647; `lab_setup.py`, which re-execs itself) has its own globals patched the moment its main() is
    entered (a one-shot sys.setprofile hook), so its main() and everything it calls see the pack.  `lab_devices.py`
    has no patch of its own: it sees the pack through the modules it imports.
  * Subprocesses inherit the environment, so the chain holds however deep the harness spawns.
`python3 labs/harness/fetch_pack.py --seams` prints the table; `./labkit selftest` proves the subprocess chain.

Usage outside ./labkit:  eval "$(./labkit env)"  — then `python3 labs/harness/run.py …` runs with the pack.
"""

import collections
import hashlib
import importlib.abc
import importlib.machinery
import inspect
import json
import os
import re
import sys
import time

HARNESS = os.path.dirname(os.path.realpath(__file__))   # real paths throughout: Python gives a script its realpath
LABS = os.path.dirname(HARNESS)
UPSTREAM = "bg-forge 23f448c42"
ENV_FLAG = "FETCH_LABKIT_PACK"
ENV_REPORT = "FETCH_PACK_REPORT"       # a file: every patched module appends one JSON line (the selftest's proof)

# ---------------------------------------------------------------------------------------------------- fetch's data

SDK_TAG = "TSBackgroundFetch"                       # Android: BackgroundFetch.java:23 `TAG = "TSBackgroundFetch"`
# iOS: the SDK NSLogs with no os_log subsystem; every line opens `[TSBackgroundFetch …`, `[TSBGTask …` or
# `[TSBGAppRefreshSubscriber …` (TSBackgroundFetch.m:13, TSBGTask.m:13/273/293, TSBGAppRefreshSubscriber.m:14).
IOS_SDK_PREFIX = "[TSB"
# (WO-002 phase 2a) SpringBoard's background-fetch task (`Executing` / `Finished <SBBackgroundFetchTask: …>`), and
# simctl push's exit when the payload has nothing for a notification to show (it has already sent the fetch).
IOS_FETCH_TASK = "SBBackgroundFetchTask"
PUSH_NO_CONTENT_EXIT = 121
# (ios-task-sim) `fire:` kinds, and the Darwin notification the custom-task kinds post: `<bundle>/<kind>/<taskId>`, the
# names the lab app's LabModule observes (LabObserveFires).
FIRE_KINDS = ("fetch", "task", "task-timeout")
DARWIN_FIRE = "%s/%s/%s"
NATIVE_PROJECT = "TSBackgroundFetch"                # PROJECT:TSBackgroundFetch-<build> (apple-generic versioning)
ANDROID_ARTIFACT = "tsbackgroundfetch"              # labs/m2/com/transistorsoft/tsbackgroundfetch/<label>
XCFRAMEWORK = "TSBackgroundFetch.xcframework"       # labs/xcframeworks/<label>/TSBackgroundFetch.xcframework
FETCH_JOB_ID = 999                                  # BackgroundFetchConfig.java:265
# FetchJobService.java:24-31 drops a run < 1000 ms after the job's `scheduled_at`; :38-51 drops a repeat of the same
# taskId within 5000 ms.  A `job:` action keeps outside both, with margin.
JOB_SETTLE_S = 1.5
JOB_REPEAT_S = 5.5
# (WO-002 Tier 1) A job a step fires inside the SDK's 5 s guard ON PURPOSE (events-duplicate) carries a dated reason,
# as bg-forge's `invalid:` does: `unspaced: "2026-11-09 <why>"`.  Only where the step states how many runs are dropped.
UNSPACED = re.compile(r"^\d{4}-\d\d-\d\d \S")
BGTASK_TIMEOUT_MS = 60000                           # BGTask.java:28 MAX_TIME, posted in the BGTask constructor (:85)
JOBSCHEDULER = "jobscheduler.txt"                   # the capture's `dumpsys jobscheduler <pkg>`, at the step's end
FETCH_ACTIONS = ("job", "force_stop")
# The names a fetch headless task is handed, by the lab's convention: a fetch lab driver writes `event=fetch` for a
# HeadlessEvent whose `timeout` is false and `event=timeout` for one whose `timeout` is true (fetch's HeadlessEvent is
# {taskId, timeout}, with no name).  labkit.HEADLESS_NAMES (WO-191) generates a headlessEvents.<name> metric per name.
HEADLESS_NAMES = ("fetch", "timeout")
# The first slice's suite (WO-002 Log, 2026-11-05), then Tier 1 on react-native Android (2026-11-09).  A name with no
# scenario file yet is refused by sweep as usual.
SUITE = ["termination-calibration", "api-smoke", "events-fetch-android",
         "api-constants", "events-initial-ignored", "events-duplicate", "events-task-android", "events-timeout-sdk",
         "stop-on-terminate", "headless-kill", "headless-activity-destroyed", "recreate-foreground-android",
         "ios-fetch-sim", "ios-timeout-sim", "ios-task-sim"]      # (WO-002 phase 2a) iOS's, n/a on Android
LISTS = {}

OVERRIDES = []                                      # (symbol, kind, seam, why) — the Phase B cut list


def seam(symbol, kind, where, why):
    OVERRIDES.append({"symbol": symbol, "kind": kind, "seam": where, "why": why})


class PackError(Exception):
    """A seam the pack rebinds is gone, or vendored logic the pack copies has changed: the pack refuses (exit 2)."""


def _rebind(obj, name, value):
    """Rebind a vendored name, refused when the vendored module no longer has it: a re-vendor that renames or drops
    a seam must stop the pack, never leave it writing a dead attribute while bg-geo's value runs on.  (Adding a
    name — Adb.job, a METRICS entry — is plain setattr / item assignment, the declared 'extend' kind.)"""
    if not hasattr(obj, name):
        raise PackError("vendored %s has no `%s` — the seam moved since %s; re-check fetch_pack.py against it"
                        % (getattr(obj, "__name__", type(obj).__name__), name, UPSTREAM))
    setattr(obj, name, value)


def _extend(obj, name, value):
    """Add a name the vendored module does not have (Adb.job, a METRICS entry) — refused when it already has one: a
    re-vendor that grows the same name must never be shadowed silently by the pack's."""
    taken = name in obj if isinstance(obj, dict) else hasattr(obj, name)
    if taken:
        raise PackError("vendored %s already has `%s` — bg-forge grew a name the pack adds; reconcile fetch_pack.py "
                        "with it" % (getattr(obj, "__name__", type(obj).__name__), name))
    if isinstance(obj, dict):
        obj[name] = value
    else:
        setattr(obj, name, value)


# Vendored functions the pack COPIES or RE-IMPLEMENTS rather than wraps, each with the sha256 of its source
# (inspect.getsource) at UPSTREAM.  A re-vendor that changes one stops the pack (exit 2) until its fetch copy here is
# reviewed against the new source and the hash updated: vendor_check pins the files' bytes, this pins the logic the
# pack keeps a second copy of.  (WO-191 deleted four: run.run_host's copy for ACTION_HANDLERS, labkit.Capture.sdk's
# for SDK_LOG, lab_setup.published and ensure_native's for NATIVE.)
PINNED = {
    "backends.Simctl.native_resolved": "d096d885420974749ff7c464745e85ad4a3e8663e2e1a0a0c31d454eb7b81c8a",
    "backends.Simctl.permissions": "21c189bfb25c46c7f4926b616cf7bae886c6f8cb4b91f6cc2807ead03c4692d2",
    "backends.Adb.sdk_db": "a409db941314b6e45fbb33e34026893c2efbff300af1451e770b9e42deb39e44",
    "backends.Simctl.sdk_db": "38ece09ef32f44b212005f479491f7629bd5f9c58dae672b00df07dc63d1abd4",
    "lab_setup.swift_api_published": "cad25bdcdacdfae1d47e8e7052e0d8a1a050327668656c308f332725f285ce0a",
    "lab_setup.capacitor_ready": "4fa7054ffe195d899aa0e05bf6ff6dd6f1fc5f8721b9be4515d586913a4001de",
    "lab_setup.ensure_capacitor": "f5dcaef7a00928af12c5f9a5cd68bd10c8cd72774a4cb25a223da804c6cd99d1",
}


def source_sha256(obj):
    return hashlib.sha256(inspect.getsource(obj).encode()).hexdigest()


def _check_pinned(module_name, m):
    """Before a module is patched: each of its PINNED functions still has the source the pack's copy was made from."""
    for qual, want in PINNED.items():
        mod, _, path = qual.partition(".")
        if mod != module_name:
            continue
        obj = m
        for part in path.split("."):
            if not hasattr(obj, part):
                raise PackError("vendored %s is gone — the pack re-implements it; re-check fetch_pack.py against %s"
                                % (qual, UPSTREAM))
            obj = getattr(obj, part)
        got = source_sha256(obj)
        if got != want:
            raise PackError("vendored %s changed (source sha256 %s…, pinned %s…): the pack keeps a copy of it — "
                            "review that copy against the new source, then update fetch_pack.PINNED"
                            % (qual, got[:12], want[:12]))


PATCHES = {}


def patches(module_name):
    def register(fn):
        PATCHES[module_name] = fn
        return fn
    return register


def java_hash(s):
    """Java's String.hashCode(), as a signed int: a custom task's jobId (BackgroundFetchConfig.java getJobId())."""
    h = 0
    for unit in s.encode("utf-16-be").hex(" ", 2).split():   # UTF-16 code units, as Java hashes them
        h = (31 * h + int(unit, 16)) & 0xFFFFFFFF
    return h - (1 << 32) if h & 0x80000000 else h


def job_id(value):
    """`job: 999` (a jobId) or `job: "<taskId>"` (that task's jobId, its String.hashCode)."""
    return value if isinstance(value, int) else java_hash(value)


def like_regex(like):
    """An SQL LIKE pattern (what Capture.sdk() is handed, `%needle%`) as a regex: `%` any run, the rest literal."""
    body = ".*".join(re.escape(p) for p in like.split("%"))
    return re.compile(("" if like.startswith("%") else "^") + body + ("" if like.endswith("%") else "$"), re.S)


SDK_LINE = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d\d\d\s+[VDIWEF]/%s\s*\(" % re.escape(SDK_TAG))


def _report(module, as_main, symbols):
    path = os.environ.get(ENV_REPORT)
    if not path:
        return
    with open(path, "a") as f:
        f.write(json.dumps({"pid": os.getpid(), "module": module, "as_main": as_main, "patched": symbols,
                            "argv0": os.path.basename(sys.argv[0]) if sys.argv else None,
                            "executable": sys.executable}) + "\n")


# ---------------------------------------------------------------------------------------------------- backends

@patches("backends")
def _backends(m):
    import labkit
    _check_pinned("backends", m)
    _rebind(m, "SDK_TAG", SDK_TAG)
    # The tap's predicate is a format string around SDK_SUBSYSTEM (backends.py:1314-1317), and fetch has no
    # subsystem: the value closes the subsystem clause and opens fetch's own.  The predicate the Simulator gets:
    #   (eventMessage CONTAINS "LAB ") OR (subsystem == "com.transistorsoft.fetchlab.none")
    #   OR (eventMessage BEGINSWITH "[TSB")
    #   OR (process == "SpringBoard" AND eventMessage CONTAINS "SBBackgroundFetchTask")
    #   OR (eventMessage CONTAINS "LICENSE VALIDATION FAILURE")
    # (WO-002 phase 2a) SpringBoard's lines are the OS's own record of a background fetch it ran and the result the
    # app's completion handler gave it (`Finished <SBBackgroundFetchTask: app<bundle…>; seq: n> with result: …`,
    # MEASURED 2026-11-10); they render as I/SpringBoard(pid) (backends.py:1029-1031) and springboardResults reads them.
    _rebind(m, "SDK_SUBSYSTEM", 'com.transistorsoft.fetchlab.none") OR (eventMessage BEGINSWITH "%s") OR '
                                '(process == "SpringBoard" AND eventMessage CONTAINS "%s' % (IOS_SDK_PREFIX, IOS_FETCH_TASK))
    original_to_logcat = m.to_logcat

    def to_logcat(ndjson):
        """The vendored rewrite, then fetch's SDK lines (no subsystem: tagged by process name there) under SDK_TAG."""
        out = []
        for line in original_to_logcat(ndjson).splitlines():
            hit = re.match(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d\d\d) I/[^(]+\((\s*\d+)\): (\[TSB.*)$", line)
            out.append("%s I/%s(%s): %s" % (hit.group(1), SDK_TAG, hit.group(2), hit.group(3)) if hit else line)
        return "\n".join(out) + ("\n" if out else "")
    _rebind(m, "to_logcat", to_logcat)

    # No SDK database: fetch logs only to logcat / os_log.  run.py still writes the (empty) transistor_log.db.
    _rebind(m.Adb, "sdk_db", lambda self, plat: b"")
    _rebind(m.Simctl, "sdk_db", lambda self, plat: b"")

    # A fetch app holds no runtime permission the SDK needs, and the SDK raises no dialog of its own.
    _rebind(m, "PERMISSIONS", [])
    _rebind(m, "FLAG_PERMISSIONS", [])
    _rebind(m, "PRIVACY", {})
    _rebind(m, "SDK_ALERT_TITLES", [])

    def simctl_permissions(self, plat, revoke):
        """Grant nothing (bg-geo grants location-always and motion); a revoke simctl cannot map still refuses."""
        unmapped = [p for p in revoke if p not in m.PRIVACY]
        if unmapped:
            raise SystemExit("simctl has no privacy service for %s" % ", ".join(unmapped))
        for service in {m.PRIVACY[p] for p in revoke}:
            self._privacy("revoke", service, plat["package"])
    _rebind(m.Simctl, "permissions", simctl_permissions)

    original_native_resolved = m.Simctl.native_resolved

    def native_resolved(self, plat, artifact):
        """The installed app's `PROJECT:TSBackgroundFetch-<build>`, as the vendored method reads TSLocationManager's:
        the same binaries (`<exe>.debug.dylib`, then `<exe>`), in-process, never None."""
        import mmap
        import plistlib
        app = self.container(plat, "app")
        if not app:
            raise SystemExit("%s is not installed on %s — run with --build" % (plat["package"], self.serial))
        try:
            with open(os.path.join(app, "Info.plist"), "rb") as f:
                exe = plistlib.load(f).get("CFBundleExecutable")
        except (OSError, plistlib.InvalidFileException):
            exe = None
        if not exe:
            raise SystemExit("the installed %s has no readable CFBundleExecutable — cannot tell which SDK this "
                             "capture would exercise" % plat["package"])
        token = re.compile(rb"PROJECT:" + NATIVE_PROJECT.encode() + rb"-(\d+)")
        for binary in (os.path.join(app, exe + ".debug.dylib"), os.path.join(app, exe)):
            if os.path.exists(binary) and os.path.getsize(binary):
                with open(binary, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    hit = token.search(mm)
                    build = hit.group(1).decode() if hit else None
                if build:
                    return build
        raise SystemExit("the installed %s carries no PROJECT:%s-<build> string — cannot tell which SDK this "
                         "capture would exercise" % (plat["package"], NATIVE_PROJECT))
    native_resolved.vendored = original_native_resolved
    _rebind(m.Simctl, "native_resolved", native_resolved)

    # The fetch host actions' device primitives, run by labkit.ACTION_HANDLERS' `job` and `force_stop` (register()).
    def job(self, plat, value, expect="present", unspaced=None):
        """`cmd jobscheduler run -f <pkg> <jobId>`, outside the SDK's guards: a repeat of the same job on this device
        waits out JOB_REPEAT_S from the last one (FetchJobService.java:38-51); JOB_SETTLE_S after the action's own
        conditions is its `delay_s`, held at load.  (WO-002 Tier 1) An `unspaced:` job does not wait: it is fired
        inside the duplicate guard on purpose, and the mark carries `unspaced since_ms=<ms since the same job last
        ran here>` (events-duplicate).  Returns the mark's text after `action=`.  What the step expects of the
        job is the action's `expect:` — `present` (the default: it runs) or `absent` (JobScheduler holds no such job,
        termination-calibration's force-stop step).  The other answer is a scored miss (`missed=no-job`,
        `missed=present`; actionsDone), not a refused step: whether the job is there is the SDK's doing
        (stopOnTerminate, stop(), a force-stop).  JobScheduler's answer rides in the mark as `said=` (non-word runs as
        `_`, 60 characters).  Read on the lab image (Android 16, 2026-11-05): `Running job [FORCED]` and `Could not
        find job 999 in package com.transistorsoft.fetchla…` (the `said=` of events-fetch-android and
        termination-calibration's force-stop step)."""
        jid = job_id(value)
        fired = self.__dict__.setdefault("_fetch_jobs_fired", {})
        key = (plat["package"], jid)
        waited, since = 0.0, None
        if unspaced:
            since = int((time.time() - fired[key]) * 1000) if key in fired else None
        elif key in fired and time.time() - fired[key] < JOB_REPEAT_S:
            waited = JOB_REPEAT_S - (time.time() - fired[key])
            time.sleep(waited)
        r = labkit.device_call(["adb", "-s", self.serial, "shell", "cmd", "jobscheduler", "run", "-f",
                                plat["package"], str(jid)], timeout=30)
        fired[key] = time.time()
        said = " ".join((r.stdout + " " + r.stderr).split())
        if "Running job" in said:
            missed = None if expect == "present" else "present"
        elif "Could not find job" in said:
            missed = None if expect == "absent" else "no-job"
        elif "Package not found" in said:
            raise SystemExit("job: %s is not installed on %s (%s)" % (plat["package"], self.serial, said[:160]))
        else:
            raise SystemExit("job: `cmd jobscheduler run -f %s %d` answered %r (exit %d)"
                             % (plat["package"], jid, said[:200], r.returncode))
        return "job %d%s%s%s%s said=%s" % (jid, " expect=absent" if expect == "absent" else "",
                                           " waited_ms=%d" % (waited * 1000) if waited else "",
                                           " unspaced since_ms=%s" % ("-" if since is None else since) if unspaced
                                           else "",
                                           " missed=%s" % missed if missed else "",
                                         re.sub(r"\W+", "_", said).strip("_")[:60] or "-")
    _extend(m.Adb, "job", job)

    def force_stop(self, plat):
        """`am force-stop <pkg>` (the vendored terminate()), then wait until no process is left; `pid=none` when
        none was running.  Refused when the process outlives 2 s.  First the host reads `dumpsys jobscheduler <pkg>`,
        and the mark carries the jobs JobScheduler held just before (`held=999`, '-' for none, `unread` when the read
        failed): the JobScheduler side of what a later `job: …, expect: absent` finds gone (jobsAtForceStop) — the
        SDK's `- registerTask:` line is logged before it calls JobScheduler.schedule() and ignores its result
        (BackgroundFetch.java:259-261, BGTask.java:169), so it proves only that the SDK asked."""
        r = labkit.device_call(["adb", "-s", self.serial, "shell", "dumpsys", "jobscheduler", plat["package"]],
                               timeout=30)
        if r.returncode != 0 or not re.search(r"^\s*Registered \d+ jobs?:", r.stdout or "", re.M):
            held = "unread"
        else:
            held = ",".join(map(str, sorted(scheduled_jobs(r.stdout, plat["package"])))) or "-"
        pid = self.alive(plat)
        self.terminate(plat)
        deadline = time.time() + 2
        while self.alive(plat) is not None:
            if time.time() >= deadline:
                raise SystemExit("force_stop: %s still has a process 2 s after am force-stop" % plat["package"])
            time.sleep(0.1)
        return "force_stop pid=%s held=%s" % (pid or "none", held)
    _extend(m.Adb, "force_stop", force_stop)

    # (WO-002 phase 2a, D3) The iOS host actions' device primitives, run by labkit.ACTION_HANDLERS' `fire`,
    # `background` and `terminate` (register()).
    def push_fetch(self, plat):
        """`xcrun simctl push <udid> <bundle>` with {"aps":{"content-available":1}}: SpringBoard sends the app UIKit's
        legacy background fetch, in the foreground, suspended or not running (MEASURED 2026-11-10, WO-002 Log).  simctl
        then fails to post the notification itself (exit 121, `Notification has no user visible content`) AFTER the
        fetch has gone; that one exit is success, any other is not."""
        path = os.path.join(labkit.HARNESS, ".claims", "push-%d-%d.json" % (os.getpid(), time.time_ns()))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"aps": {"content-available": 1}}, f)
        try:
            r = labkit.device_call(["xcrun", "simctl", "push", self.serial, plat["package"], path], timeout=30)
        finally:
            os.unlink(path)
        said = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode == 0 or (r.returncode == PUSH_NO_CONTENT_EXIT and "no user visible content" in said):
            return "fire fetch push_exit=%d" % r.returncode
        raise SystemExit("simctl push %s exited %d: %s" % (plat["package"], r.returncode, said[-300:]))
    _extend(m.Simctl, "push_fetch", push_fetch)

    def fire_task(self, plat, kind, task_id):
        """(ios-task-sim) The custom-task hook: a Darwin notification, `<bundle>/<kind>/<taskId>`, which the lab app's
        LabModule observes for each permitted task identifier and answers with the TSBGTask's `execute` (`task`) or
        `onTimeout` (`task-timeout`), logging `LAB point=fire kind= taskId= found= ran=`.  notifyutil cannot know
        whether anyone observed it: the app's line is the evidence (taskFires).  A suspended app gets it only at its
        next resume (the D3 spike), so the step keeps the app in the foreground."""
        name = DARWIN_FIRE % (plat["package"], kind, task_id)
        r = labkit.device_call(["xcrun", "simctl", "spawn", self.serial, "notifyutil", "-p", name], timeout=30)
        if r.returncode != 0:
            raise SystemExit("notifyutil -p %s exited %d: %s" % (name, r.returncode,
                                                                 ((r.stdout or "") + (r.stderr or "")).strip()[-300:]))
        return "fire %s taskId=%s" % (kind, task_id)
    _extend(m.Simctl, "fire_task", fire_task)

    def to_background(self, plat):
        """The app to the background, as a user leaving it would: another app (Settings) launched over it.  The
        Simulator suspends a backgrounded app within 1 s (MEASURED 2026-11-10); a pushed fetch then resumes it."""
        r = labkit.device_call(["xcrun", "simctl", "launch", self.serial, "com.apple.Preferences"], timeout=30)
        if r.returncode != 0:
            raise SystemExit("simctl launch com.apple.Preferences exited %d: %s" % (r.returncode, (r.stderr or "")[-300:]))
        return "background via=com.apple.Preferences"
    _extend(m.Simctl, "to_background", to_background)

    for name in ("job", "force_stop"):
        def refuse(self, *a, name=name):
            raise SystemExit("the Simulator has no `%s` — labkit refuses `actions:` on a scenario that does not "
                             "excuse ios" % name)
        _extend(m.Simctl, name, refuse)
    return ["SDK_TAG", "SDK_SUBSYSTEM", "to_logcat", "Adb.sdk_db", "Simctl.sdk_db", "PERMISSIONS",
            "FLAG_PERMISSIONS", "PRIVACY", "SDK_ALERT_TITLES", "Simctl.permissions", "Simctl.native_resolved",
            "Adb.job", "Adb.force_stop", "Simctl.job", "Simctl.force_stop", "Simctl.push_fetch", "Simctl.fire_task",
            "Simctl.to_background"]


seam("backends.SDK_TAG", "data", "backends.py:40", "fetch's logcat tag, TSBackgroundFetch")
seam("backends.SDK_SUBSYSTEM", "data", "backends.py:39 + :1314-1317",
     "fetch has no os_log subsystem; the value is spliced into the tap predicate to add `eventMessage BEGINSWITH "
     "\"[TSB\"` — seam: an IOS_LOG_PREDICATE constant")
seam("backends.to_logcat", "wrap", "backends.py:1002-1031 (:1025)",
     "retags fetch's NSLog lines (`[TSB…`) to SDK_TAG — seam: an sdk_line(ev) predicate")
seam("backends.Adb.sdk_db / Simctl.sdk_db", "replace", "backends.py:643-645, :1419-1425; run.py:1218",
     "no SDK database: b'' (run.py still writes an empty transistor_log.db) — seam: SDK_DB = None skips the pull")
seam("backends.PERMISSIONS / FLAG_PERMISSIONS / PRIVACY / SDK_ALERT_TITLES", "data",
     "backends.py:742, :964, :761, :816", "no location/motion permissions, no SDK alert")
seam("backends.Simctl.permissions", "replace", "backends.py:1119-1127 (:1120 hardcodes location-always, motion)",
     "grants nothing — seam: an IOS_GRANTS constant")
seam("backends.Simctl.native_resolved", "replace", "backends.py:1483-1515 (:1510 hardcodes TSLocationManager)",
     "reads PROJECT:TSBackgroundFetch-<build> — seam: a NATIVE_PROJECT constant")
seam("backends.Adb.job / Adb.force_stop / Simctl.job / Simctl.force_stop", "extend", "backends.py:248 (beside Adb.kill)",
     "the fetch host actions' device primitives; the Simulator's two refuse (actions are Android's)")

# ---------------------------------------------------------------------------------------------------- labkit


def register(lk):
    """(bg-forge WO-191, D3) fetch's seams in labkit.py, applied by labkit itself: lab_product.py, which labkit loads at
    the end of its own definitions, calls this with the module (`lk`) — once per labkit module a process holds — and
    labkit exits 2 if it raises.  Everything here changes only `lk`: backends.py may still be loading (WO-191 Part 3)."""
    symbols = _register(lk)
    _report("labkit", lk.__name__ == "__main__", symbols)
    return symbols


def _register(m):
    # configure(config, onEvent, onTimeout?) and status(callback?): the types name callbacks onEvent/onTimeout/task,
    # which the vendored name test (`cb` or `…callback…`) does not count — so configure would read as taking two
    # values before its callback.  :1421 already finds callbacks by TYPE; this widens the name test to fetch's names.
    # Built FROM the vendored pattern, so a re-vendor that changes it carries through (only `.search` reads it).
    vendored_cb = m.CALLBACK_ARG
    _rebind(m, "CALLBACK_ARG", re.compile(r"(?:%s)|^on[A-Z]\w*$|^task$" % vendored_cb.pattern, vendored_cb.flags))
    # No foreground service: the vendored promotion pattern under fetch's tag, a line fetch never logs, so any FGS
    # start holds the step to the cap.  Built from the vendored pattern; refused if it no longer opens with the tag.
    vendored_fgs = m.FGS_PROMOTED
    if not vendored_fgs.pattern.startswith("TSLocationManager"):
        raise PackError("vendored labkit.FGS_PROMOTED no longer opens with `TSLocationManager` (%r) — re-check the "
                        "pack's retag against %s" % (vendored_fgs.pattern[:60], UPSTREAM))
    _rebind(m, "FGS_PROMOTED", re.compile(re.escape(SDK_TAG) + vendored_fgs.pattern[len("TSLocationManager"):],
                                          vendored_fgs.flags))

    # The SDK's own lines (bg-forge WO-191's SDK_LOG): on both OSes from the log, never transistor_log.db (fetch has
    # no database) — Android's logcat tag TSBackgroundFetch, and the iOS tap's lines retagged there (backends.to_logcat
    # above), from the app under test's own processes only (own_pids).  Logcat is device-wide, and bg-forge's lab apps
    # link TSBackgroundFetch too: the database bg-geo's reader reads was the app's own, so it was scoped by
    # construction.  `like` is the callers' SQL LIKE pattern (`%needle%`).
    def sdk_log(c, like):
        rx, own = like_regex(like), _own_pids(m, c)
        return [c.norm[l] for l in c.window if SDK_LINE.match(l) and m.line_pid(l) in own and rx.search(c.norm[l])]
    _rebind(m, "SDK_LOG", sdk_log)

    # Every step that expects anything also expects (WO-002 review):
    #   * configureOutcome ok — its configure() resolved: a rejected or hung configure leaves start/stop/status acting
    #     on nothing, and api-smoke would still read green.  A step that makes none (the launch extra noConfigure)
    #     says `configureOutcome: "-"`; a `process: live` step is given "-" (expectations below: the RN driver
    #     configures once per JS context, and run.py clears the log at every step).
    #   * foreignLines 0 — no SDK, headless or task line from a process that is not the app's (own_pids): logcat is
    #     device-wide, bg-forge's lab apps link TSBackgroundFetch, and the vendored headless metrics read every pid.
    # Both read the whole log and are complete once the driver's configure() has settled (EVIDENCE below), so they are
    # early metrics: a step that may end on its evidence still does.  Added in place (WO-191: register's hook).
    _extend(m.IMPLICIT, "configureOutcome", "ok")
    _extend(m.IMPLICIT, "foreignLines", 0)
    for name in ("configureOutcome", "foreignLines"):
        if name in m.EARLY_METRICS:
            raise PackError("vendored labkit.EARLY_METRICS already has `%s` — reconcile fetch_pack.py with it" % name)
        m.EARLY_METRICS.add(name)

    # Not complete while a configure() the driver made has not settled, or (a fresh process) before the driver has
    # logged its boot configure decision — so configureOutcome is final when the step ends on its evidence.
    _rebind(m, "EVIDENCE", lambda lines, step, os_name: configure_settled(m, lines, step))

    original_expectations = m.expectations

    def expectations(step, platform_name, os_name=None):
        """The vendored expectations; a `process: live` step that does not state configureOutcome expects '-'."""
        merged = original_expectations(step, platform_name, os_name)
        stated = set(step.get("expect") or {}) | set((step.get("platform_expect") or {}).get(platform_name) or {})
        if step.get("process") == "live" and "configureOutcome" in merged and "configureOutcome" not in stated:
            merged["configureOutcome"] = "-"
        return merged
    _rebind(m, "expectations", expectations)

    original_validate_hooks = m._validate_hooks

    def _validate_hooks(where, step, excused=()):
        """The vendored check, with fetch's constants: a probe of a property is held to `function|object` at `$`
        (labkit.py:1270-1282: bg-geo's properties are namespaces and objects), but fetch's STATUS_* / NETWORK_TYPE_*
        are numbers on the default export, and the driver's probe reports one as it is (labapi.js:378-383).  A probe
        of a property whose vocabulary entry carries `wire:` (a constant) may assert `$` by `type` (its wire) and
        `equals` (one of its type's values) — checked here; the vendored check sees `function|object` in its place,
        and the hook gets its own `$` back."""
        hooks = step.get("hooks")
        held = {}
        for i, h in enumerate(hooks or []):
            member = m.api_member(h.get("api")) if isinstance(h, dict) and h.get("probe") else None
            returns = h.get("returns") if member else None
            if not (member and member.get("property") and member.get("wire") and isinstance(returns, dict)
                    and isinstance(returns.get("$"), dict)):
                continue
            checks, at = returns["$"], "%s hook %d (%s) $" % (where, i, h["api"])
            wire = m.wire_of(member)
            values = m.ts_values(str(member.get("returns")))
            asked = set(str(checks.get("type", "")).split("|")) - {""} | m._implied(checks)
            if set(checks) - {"type", "equals"} or asked - wire or ("equals" in checks and values is not None
                                                                   and checks["equals"] not in values):
                raise SystemExit("%s: %s is a constant the types declare `%s` (wire %s, values %s) — assert its "
                                 "`type` and one of those values only" % (at, h["api"], member.get("returns"),
                                                                          "|".join(sorted(wire)), sorted(values or [])))
            held[i] = checks
            hooks[i] = dict(h, returns=dict(returns, **{"$": {"type": "function|object"}}))
        original_validate_hooks(where, step, excused)
        for i, checks in held.items():
            h = step["hooks"][i]
            step["hooks"][i] = dict(h, returns=dict(h["returns"], **{"$": checks}))
    _rebind(m, "_validate_hooks", _validate_hooks)

    # The names a headless task is handed (HEADLESS_NAMES above): labkit generates headlessEvents.<name> from them
    # after register(), so they are fetch's, not the vocabulary's.
    _rebind(m, "HEADLESS_NAMES", lambda: list(HEADLESS_NAMES))

    # After the step's log is on disk: the host's read of the app's jobs, `dumpsys jobscheduler <pkg>`, into the capture
    # as jobscheduler.txt (jobIds, jobsAtForceStop and own_pids read it).  Android's: the Simulator has no JobScheduler.
    def capture_extras(dev, plat, out):
        if dev.os != "android":
            return
        r = m.device_call(["adb", "-s", dev.serial, "shell", "dumpsys", "jobscheduler", plat["package"]], timeout=30)
        # (WO-002 Tier 1) host_ms: the host's clock when the dump came back — `Job history:` ages are relative to the
        # dump, and jobStops places them against run.json's launch mark (the same host clock).
        host_ms = int(time.time() * 1000)
        with open(os.path.join(out, JOBSCHEDULER), "w") as f:
            f.write("# adb -s %s shell dumpsys jobscheduler %s  (exit %d, read %s, host_ms=%d)\n%s%s"
                    % (dev.serial, plat["package"], r.returncode, time.strftime("%Y-%m-%d %H:%M:%S"), host_ms,
                       r.stdout, r.stderr))
    _rebind(m, "CAPTURE_EXTRAS", capture_extras)

    # fetch's host actions, as two kinds of labkit's own (ACTION_HANDLERS): validated and run by the vendored
    # _validate_moves and run_host, scored by actionsDone.  `expect:` is the job's (both declare it, so an `expect:`
    # on a force_stop is refused in fetch's own words, below).  Android's only (the default `oses`).
    _extend(m.ACTION_HANDLERS, "job", m.ActionHandler(_job_ok, lambda dev, item, plat, server: dev.job(
        plat, item["job"], item.get("expect", "present"), item.get("unspaced")), keys=("expect", "unspaced")))
    _extend(m.ACTION_HANDLERS, "force_stop", m.ActionHandler(
        _force_stop_ok, lambda dev, item, plat, server: dev.force_stop(plat), keys=("expect",)))
    # (WO-002 phase 2a, D3) iOS's: a fetch fired through SpringBoard (`simctl push`) or a custom task through the lab
    # app's hook (a Darwin notification, ios-task-sim), the app sent to the background, and the app terminated (so a
    # later `fire` launches it in the background).  A scenario using one excuses android.
    _extend(m.ACTION_HANDLERS, "fire", m.ActionHandler(
        _fire_ok, lambda dev, item, plat, server: dev.push_fetch(plat) if item["fire"] == "fetch"
        else dev.fire_task(plat, item["fire"], item["taskId"]), oses=("ios",), keys=("taskId",)))
    _extend(m.ACTION_HANDLERS, "background", m.ActionHandler(
        _flag_ok("background"), lambda dev, item, plat, server: dev.to_background(plat), oses=("ios",)))
    _extend(m.ACTION_HANDLERS, "terminate", m.ActionHandler(
        _flag_ok("terminate"), lambda dev, item, plat, server: (dev.terminate(plat), "terminate")[1], oses=("ios",)))

    # The native SDK a label is built from and published as: tsbackgroundfetch in labs/m2 and TSBackgroundFetch's
    # xcframework, both OSes from one checkout (native/background-fetch).  Android's slot is fetch's own, so its publish is
    # publish_native_fetch.sh (the label checked, the POM and AAR verified, the provenance written only after them;
    # bg-forge's publish_native.sh is not vendored), into a worktree named `native` as before; iOS's stops at its pin.
    for os_name, values in (("android", {"submodule": "native/background-fetch", "build_root": "android",
                                         "gradle_task": ":tsbackgroundfetch:publishToMavenLocal",
                                         "version_property": "TS_BACKGROUND_FETCH_VERSION_NAME",
                                         "artifacts": (ANDROID_ARTIFACT,), "build": _native_android}),
                            ("ios", {"artifact": XCFRAMEWORK, "build": _native_ios})):
        for key, value in values.items():
            if key not in m.NATIVE[os_name]:
                raise PackError("vendored labkit.NATIVE[%r] has no `%s` — the seam moved since %s"
                                % (os_name, key, UPSTREAM))
            m.NATIVE[os_name][key] = value

    _fetch_metrics(m)
    return ["CALLBACK_ARG", "FGS_PROMOTED", "SDK_LOG", "IMPLICIT", "EARLY_METRICS", "EVIDENCE", "expectations",
            "_validate_hooks", "HEADLESS_NAMES", "CAPTURE_EXTRAS", "ACTION_HANDLERS(+job, force_stop)", "NATIVE",
            "METRICS(+fetch)"]


def _expect_ok(at, item, kind):
    expect = item.get("expect")
    if "expect" in item and (kind != "job" or expect not in ("present", "absent")):
        raise SystemExit("%s: `expect:` belongs to a `job:` and is `present` (it runs: the default) or "
                         "`absent` (JobScheduler holds no such job) — not %r on a `%s:`" % (at, expect, kind))


def _job_ok(at, item, step):
    """A `job:` action (ACTION_HANDLERS' validate): its value, its `delay_s` outside the SDK's scheduling guard, and the
    seconds it can take beyond that — JOB_REPEAT_S when an earlier action of the step fires the same job, since the
    host then waits out the SDK's duplicate guard (Adb.job).  `item` is step["actions"][i] itself (WO-191): found by
    identity, never by equality, or an equal repeat would read as the first."""
    _expect_ok(at, item, "job")
    value, delay = item["job"], item.get("delay_s", 0)
    if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and value)):
        raise SystemExit("%s: `job:` is the jobId JobScheduler holds (`%d`, the fetch job) or a "
                         "scheduleTask taskId (its String.hashCode) — not %r" % (at, FETCH_JOB_ID, value))
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < JOB_SETTLE_S:
        raise SystemExit("%s: a `job:` waits `delay_s: %g` or more after its conditions — "
                         "FetchJobService ignores a run < 1 s after the job was scheduled "
                         "(FetchJobService.java:24-31), and a run it ignores reads as an SDK defect" % (at, JOB_SETTLE_S))
    actions = step.get("actions") or []
    i = next(j for j, a in enumerate(actions) if a is item)
    earlier = [job_id(a["job"]) for a in actions[:i] if isinstance(a, dict) and "job" in a]
    if "unspaced" in item:
        # (WO-002 Tier 1) Firing inside the SDK's guards is a shortcut WO-002 rejects for a delivery — the drop reads
        # as an SDK defect — and a measurement when the step asserts the drop.  So: dated, a repeat of a job fired
        # earlier in the step, never `expect: absent`, and only where the step's expect states duplicateJobs.
        unspaced, dup = item["unspaced"], (step.get("expect") or {}).get("duplicateJobs")
        if not (isinstance(unspaced, str) and UNSPACED.match(unspaced)):
            raise SystemExit("%s: `unspaced:` is a dated reason, \"YYYY-MM-DD <why>\": the host fires this job without "
                             "waiting out the SDK's 5 s duplicate guard, on purpose — not %r" % (at, unspaced))
        if item.get("expect") == "absent":
            raise SystemExit("%s: an `unspaced:` job is fired for the SDK to deliver or drop — `expect: absent` is a "
                             "job JobScheduler no longer holds, which no guard is about" % at)
        if job_id(value) not in earlier:
            raise SystemExit("%s: `unspaced:` waives the wait after an earlier run of the same job in this step, and "
                             "no earlier action fires job %d" % (at, job_id(value)))
        if isinstance(dup, bool) or not isinstance(dup, int):
            raise SystemExit("%s: a step with an `unspaced:` job states how many runs the SDK drops — "
                             "`duplicateJobs: <n>` in its expect, not %r (WO-002, \"Not an acceptable shortcut\")"
                             % (at, dup))
        return 0
    return JOB_REPEAT_S if job_id(value) in earlier else 0


def _fire_ok(at, item, step):
    """(WO-002 phase 2a) `fire: fetch`: the one kind of event SpringBoard sends on the Simulator.  A custom task has no
    such route (BGTaskScheduler is unavailable there), so `fire: task` and `fire: task-timeout` take a `taskId:` and go
    through the lab app's hook (Simctl.fire_task)."""
    if item["fire"] not in FIRE_KINDS:
        raise SystemExit("%s: `fire:` is `fetch` (a SpringBoard background fetch through `simctl push`), or `task` / "
                         "`task-timeout` with a `taskId:` (the lab app's custom-task hook) — not %r" % (at, item["fire"]))
    if item["fire"] == "fetch" and "taskId" in item:
        raise SystemExit("%s: `taskId:` goes with `fire: task` and `fire: task-timeout`, not `fire: fetch`" % at)
    if item["fire"] != "fetch" and "taskId" not in item:
        raise SystemExit("%s: `fire: %s` needs a `taskId:`" % (at, item["fire"]))
    if item["fire"] != "fetch" and not (isinstance(item["taskId"], str) and re.match(r"^[\w.-]+$", item["taskId"])):
        raise SystemExit("%s: `taskId:` is a task identifier (letters, digits, `.`, `_`, `-`), not %r"
                         % (at, item["taskId"]))
    return 0


def _flag_ok(kind):
    def ok(at, item, step):
        if item[kind] is not True:
            raise SystemExit("%s: `%s:` is true, not %r" % (at, kind, item[kind]))
        return 0
    ok.__name__ = "_%s_ok" % kind
    return ok


def _force_stop_ok(at, item, step):
    _expect_ok(at, item, "force_stop")
    if item["force_stop"] is not True:
        raise SystemExit("%s: `force_stop:` is true, an `am force-stop` of the app — not %r" % (at, item["force_stop"]))
    return 0


def _native_android(var, done, logs, setup):
    """labkit.NATIVE["android"]["build"], lab_setup.ensure_native's slot (WO-191): nothing for a published label; else
    native/background-fetch at its native_source, built and published by publish_native_fetch.sh."""
    if done:
        return
    wt = setup.native_checkout("native/background-fetch", var["native_source"]["android"], "native")
    print("(a few minutes: Gradle builds tsbackgroundfetch and publishes it to labs/m2)")
    setup.sh([os.path.join(setup.HARNESS, "publish_native_fetch.sh"), var["native"], wt],
             log=os.path.join(logs, "native-android.log"))


def _native_ios(var, done, logs, setup):
    """labkit.NATIVE["ios"]["build"], lab_setup.ensure_native's slot (WO-191): nothing for a published label; else
    native/background-fetch at its native_source, its Simulator slice built and published into labs/xcframeworks by
    publish_native_fetch_ios.sh (WO-002 phase 2).  The same `native` worktree as Android's slot: one label is one
    native commit on both OSes, each OS's provenance recording its own version (D6)."""
    if done:
        return
    wt = setup.native_checkout("native/background-fetch", var["native_source"]["ios"], "native")
    print("(a minute or two: Xcode archives TSBackgroundFetch's simulator slice into labs/xcframeworks)")
    setup.sh([os.path.join(setup.HARNESS, "publish_native_fetch_ios.sh"), var["native"], wt],
             env=dict(os.environ, LANG="en_US.UTF-8"), log=os.path.join(logs, "native-ios.log"))


seam("labkit.CALLBACK_ARG", "data", "labkit.py:187",
     "the vendored pattern plus onEvent/onTimeout/task — seam: find callbacks by type (`=>`), as :1526 does")
seam("labkit.FGS_PROMOTED", "data", "labkit.py:4660",
     "the vendored pattern under fetch's tag: fetch starts no foreground service, so it never matches")
seam("labkit.IOS_GRACE_S", "none", "labkit.py:4645, ios_grace :4676",
     "not overridden: its keys are geo (addGeofence, watchPosition), no fetch hook names them, so 'default' (2 s)")
seam("labkit.SDK_LOG", "hook", "labkit.py:3412-3438",
     "reads TSBackgroundFetch lines from the log on both OSes, never transistor_log.db")
seam("labkit.ACTION_HANDLERS", "hook", "labkit.py:1773",
     "`job:` (with `expect:` and, Tier 1, a dated `unspaced:`) and `force_stop:`, two action kinds of labkit's own")
seam("labkit._validate_hooks", "wrap", "labkit.py:1270-1282 (a property probe's `$` is function|object)",
     "a probe of a constant (STATUS_*, NETWORK_TYPE_*: numbers) may assert its type and value — seam: "
     "`allowed |= wire_of(member)` for a property whose return has a `wire`")
seam("labkit.HEADLESS_NAMES", "hook", "labkit.py:3613-3618",
     "fetch and timeout (HEADLESS_NAMES): the vocabulary carries no Event enum any more")
seam("labkit.METRICS", "extend", "labkit.py:3445-3451 (the registry: no override)",
     "fetch's metrics: SDK-line counts, fetchTally, jobIds, jobsRegistered, configureOutcome; (Tier 1) jobStops, "
     "jobsCancelled, React Native HeadlessTask counts, driverEvents/driverTally/driverFinishes, appProcesses, timing "
     "metrics; (phase 2a) the iOS legacy-fetch and custom-task metrics")
seam("labkit.IMPLICIT / EARLY_METRICS", "hook", "labkit.py:4548, :4571",
     "adds `configureOutcome: ok` and `foreignLines: 0` to every step that expects anything, both early (whole-log) "
     "metrics, in place")
seam("labkit.EVIDENCE", "hook", "labkit.py:4916",
     "not complete while a configure() the driver made is unsettled")
seam("labkit.CAPTURE_EXTRAS", "hook", "run.py:610-623 (end_capture_log)",
     "the host's `dumpsys jobscheduler <pkg>` read as jobscheduler.txt (Android), its header carrying host_ms")
seam("labkit.NATIVE", "hook", "labkit.py:275; lab_setup.py:236-242, :416-431; inventory.py:30",
     "tsbackgroundfetch / TSBackgroundFetch.xcframework; fetch's own slots, publish_native_fetch.sh (Android) and "
     "publish_native_fetch_ios.sh (iOS)")
seam("labkit.expectations", "wrap", "labkit.py:4968",
     "a `process: live` step expects configureOutcome '-' (the driver configures once per JS context)")

# ---------------------------------------------------------------------------------------------------- metrics

# Android SDK lines (native/background-fetch 5de5242, the FIX label; unchanged from 71718f9).  Counted in the window,
# as every vendored metric is, from the app under test's own processes (Capture.sdk).
SDK_COUNTS = {
    "fetchReceived": ("%- Background Fetch event received: %", "BackgroundFetch.java:225"),
    "fetchFinished": ("%- finish: %", "BackgroundFetch.java:196"),
    "fetchTimeouts": ("%[BGTask] timeout: %", "BGTask.java:203"),
    "jobFinished": ("%- jobFinished%", "FetchJobService.java:54"),
    "jobStopped": ("%- onStopJob%", "FetchJobService.java:65"),
    "duplicateJobs": ("%- Caught duplicate Job %", "FetchJobService.java:41"),
    "stoppingOnTerminate": ("%- Stopping on terminate%", "BackgroundFetch.java:277"),
    "headlessTaskErrors": ("%Headless task error: %", "BGTask.java:219, BackgroundFetch.java:283"),
}
# (WO-002 phase 2a) The same two on iOS (native/background-fetch 5de5242): the legacy fetch, the Simulator's path
# (BGTaskScheduler is unavailable there), and a finish that found its subscriber (`(n of m)`; "Called without a task"
# and "Failed to find Fetch subscriber" are not finishes).  The SDK logs no line for a timeout on that path.
IOS_SDK_COUNTS = {
    "fetchReceived": ("%[TSBackgroundFetch performFetchWithCompletionHandler]%", "TSBackgroundFetch.m:180"),
    "fetchFinished": ("%[TSBackgroundFetch finish] % of %", "TSBackgroundFetch.m:366"),
}
# (WO-002 Tier 1) React Native's HeadlessTask (react-native/background-fetch android HeadlessTask.java), which logs under
# the SDK's tag: the cold path builds a ReactContext (bridgeless: 4.2.6), each JS task starts and ends (4.2.7's names),
# and its errors.  Another bridge logs none of these: its scenarios state them per platform (platform_expect).
RN_HEADLESS_COUNTS = {
    "rnHeadlessInit": ("%[HeadlessTask] initializing ReactContext%", "HeadlessTask.java:127"),
    "rnHeadlessStarts": ("%[HeadlessTask] start taskId: %", "HeadlessTask.java:76"),
    "rnHeadlessEnds": ("%[HeadlessTask] end taskId: %", "HeadlessTask.java:80"),
}
RN_HEADLESS_ERRORS = ("%[HeadlessTask] Failed invoke HeadlessTask%",              # HeadlessTask.java:44
                      "%[HeadlessTask] task attempted to run in the foreground%",  # :86
                      "%[HeadlessTask] Reflection error%",                         # :106, :119
                      "%[HeadlessTask] reflection error%")                         # :140, :151
# React Native's own warning when a headless task starts before the bundle registered it: RN 4.4.2's cold-start race
# (61695d4), AppRegistryImpl.js:309.  Tag ReactNativeJS, not the SDK's.
RN_NO_TASK = "No task registered for key BackgroundFetch"
# `dumpsys jobscheduler` `Job history:` rows (AOSP JobPackageTracker; read on the lab image, Android 16):
#   `     -40s555ms  STOP-P: #u0a213/999 <pkg>/<service> onStartJob returned false`.  START rows carry no reason.
HISTORY_ROW = re.compile(r"^\s+(-[0-9dhms]+)\s+(START|STOP)(?:-P)?: #\S+?/(-?\d+) ([\w.]+)/\S+(?: (.*))?$")
HISTORY_AGE = re.compile(r"^-(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m(?!s))?(?:(\d+)s)?(?:(\d+)ms)?$")


def history_age_ms(text):
    """A `Job history:` age (`-2h26m50s884ms`, `-2m09s691ms`, `-40s597ms`, `-884ms`) in ms, or None."""
    hit = HISTORY_AGE.match(text)
    if not hit or text == "-":
        return None
    d, h, mi, s, ms = (int(x or 0) for x in hit.groups())
    return (((d * 24 + h) * 60 + mi) * 60 + s) * 1000 + ms


def tally(keys):
    return ",".join("%s=%d" % (k, keys.count(k)) for k in sorted(set(keys))) or "-"


def scheduled_jobs(text, package):
    """The job ids `dumpsys jobscheduler <pkg>` lists under `Registered N jobs:` for the package, in order.  Each
    registered job opens `  JOB #u0a<uid>/<id>: <hash> <pkg>/<service>` (AOSP JobStore dump; read on the lab image,
    Android 16, 2026-11-05: `Registered 288 jobs:` / `  JOB #u0a212/999: 3aa55a2 <pkg>/…FetchJobService`)."""
    ids, inside = [], False
    for line in (text or "").splitlines():
        if re.match(r"^\s*Registered \d+ jobs?:", line):
            inside = True
            continue
        if inside and line and not line[0].isspace():
            inside = False
        if inside:
            hit = re.match(r"^\s+JOB #\S+?/(-?\d+): \S+ (\S+?)/", line)
            if hit and hit.group(2) == package:
                ids.append(int(hit.group(1)))
    return ids


def _on_ios(c):
    return (c.meta or {}).get("os") == "ios"


def _fetch_metrics(m):
    for name, (like, where) in list(SDK_COUNTS.items()) + list(RN_HEADLESS_COUNTS.items()):
        def count(c, like=like, name=name):
            # (WO-002 phase 2a) iOS logs its own words for the two it shares; Android's others read '-' there.
            if _on_ios(c):
                ios = IOS_SDK_COUNTS.get(name)
                return len(c.sdk(ios[0])) if ios else "-"
            return len(c.sdk(like))
        count.__name__ = name
        count.__doc__ = "(WO-002) SDK lines `%s` in the window (Android, %s)%s." % (
            like.strip("%"), where, "; on iOS `%s` (%s)" % (IOS_SDK_COUNTS[name][0].strip("%"), IOS_SDK_COUNTS[name][1])
            if name in IOS_SDK_COUNTS else "; '-' on iOS")
        _extend(m.METRICS, name, count)

    def tally(values):
        return ",".join("%s=%d" % (v, values.count(v)) for v in sorted(set(values))) or "-"

    def legacyFetchStates(c):
        """(WO-002 phase 2a) iOS: the legacy background fetches UIKit handed the app, by its state then
        (AppDelegate.swift's `LAB point=legacyFetch state=…`): `background=1`; '-' for none, and on Android."""
        return tally([h.group(1) for h in (re.search(r"point=legacyFetch state=(\w+)", l) for l in c.window) if h])
    _extend(m.METRICS, "legacyFetchStates", legacyFetchStates)

    def legacyResults(c):
        """(WO-002 phase 2a) iOS: what the SDK passed to UIKit's completion handler (`LAB point=legacyFetchComplete
        result=newData|noData|failed`): `newData=1`; '-' for none, and on Android."""
        return tally([h.group(1) for h in (re.search(r"point=legacyFetchComplete result=(\w+)", l) for l in c.window)
                      if h])
    _extend(m.METRICS, "legacyResults", legacyResults)

    def springboardResults(c):
        """(WO-002 phase 2a) iOS: SpringBoard's own record of the fetches it ran for a fetch lab app and what their
        completion said (`Finished <SBBackgroundFetchTask: app<…>; seq: n> with result: …`): `RetrievedNewData=1`;
        '-' for none, and on Android."""
        return tally([h.group(1) for h in (re.search(r"%s: app<com\.transistorsoft\.fetchlab\.[^(>]+.*with result: (\w+)"
                                                     % IOS_FETCH_TASK, l) for l in c.window) if h])
    _extend(m.METRICS, "springboardResults", springboardResults)

    def legacyTimeoutAfterMs(c):
        """(WO-002 phase 2a) iOS: ms from the legacy fetch's delivery (`point=legacyFetch`) to the driver's first
        `app=event=timeout`, the SDK's UIBackgroundTask expiring (TSBackgroundFetch.m:186-195; the SDK logs no line of
        its own there); '-' without both."""
        stamp = re.compile(r"^\d\d-\d\d (\d\d):(\d\d):(\d\d\.\d{3}) ")
        def ms(line):
            h = stamp.match(line)
            return int(round((int(h.group(1)) * 3600 + int(h.group(2)) * 60 + float(h.group(3))) * 1000)) if h else None
        fetched = next((ms(l) for l in c.window if "point=legacyFetch state=" in l), None)
        timed = next((ms(l) for l in c.window if " app=event=timeout " in l), None)
        return timed - fetched if fetched is not None and timed is not None else "-"
    _extend(m.METRICS, "legacyTimeoutAfterMs", legacyTimeoutAfterMs)

    def taskFires(c):
        """(ios-task-sim) iOS: what the lab app's custom-task hook did with each `fire: task|task-timeout` (LabModule's
        `LAB point=fire kind=<k> taskId=<id> found=<b> ran=<b>`), `<kind>:<ran|noCallback|missing>=n`: ran (the task's
        execute / onTimeout called its callback), noCallback (the task is listed with no callback), missing (no such
        task, e.g. finish() destroyed it); '-' for none, and on Android."""
        out = []
        for l in c.window:
            if " point=fire kind=" in l:
                found, ran = re.search(r"\bfound=(\w+)", l), re.search(r"\bran=(\w+)", l)
                out.append("%s:%s" % (re.search(r"\bkind=(\S+)", l).group(1),
                                      "missing" if not found or found.group(1) != "true"
                                      else "ran" if ran and ran.group(1) == "true" else "noCallback"))
        return tally(out)
    _extend(m.METRICS, "taskFires", taskFires)

    def taskSubmitErrors(c):
        """(ios-task-sim) iOS: why the SDK could not submit a custom task (`[TSBackgroundFetch scheduleTask] ERROR:
        Failed to submit task request: Error Domain=<d> Code=<n> …`, TSBackgroundFetch.m:253-255), `<domain>:<code>=n`:
        `BGTaskSchedulerErrorDomain:1` is the Simulator's (BGTaskSchedulerErrorCodeUnavailable); `TSBackgroundFetch:0`
        would be a task identifier the app's Info.plist does not permit (TSBGTask.m:155-157).  '-' for none, and on
        Android."""
        return tally(["%s:%s" % h.groups() for h in (re.search(r"Error Domain=(\S+) Code=(-?\d+)", l)
                                                     for l in c.sdk("%Failed to submit task request:%")) if h])
    _extend(m.METRICS, "taskSubmitErrors", taskSubmitErrors)

    def sdkTasks(c):
        """(ios-task-sim) iOS: the custom tasks the SDK lists at the step's end (TSBGTask's list, the probe's `tasks=`
        in the last tick), sorted: '-' for none — a non-periodic task is gone after its finish (TSBGTask.m:250-260).
        `no-probe` where no tick carries the field (Android's probe has none)."""
        return c.last_state("tasks", "no-probe")
    _extend(m.METRICS, "sdkTasks", sdkTasks)

    def fetchTally(c):
        """(WO-002) Fetch events the SDK received, `taskId=n,…` sorted ('-' for none): recorded, for the taskIds a step
        does not assert."""
        ids = [line.split("event received: ", 1)[1].strip() for line in c.sdk(SDK_COUNTS["fetchReceived"][0])]
        return ",".join("%s=%d" % (t, ids.count(t)) for t in sorted(set(ids))) or "-"

    def jobIds(c):
        """(WO-002) The job ids the app holds at the step's end, from the host's `dumpsys jobscheduler <pkg>` read
        (jobscheduler.txt): `999`, `999,-1234`, '-' for none, `no-read` where the capture has no read (iOS), and
        `unread` where the read failed (exit not 0) or carries no `Registered N jobs:` section — so '-' always means
        "read, and the app holds none" (termination-calibration's force-stop step asserts it)."""
        path = os.path.join(c.path, JOBSCHEDULER)
        if not os.path.exists(path):
            return "no-read"
        text = open(path, errors="replace").read()
        head = re.search(r"\(exit (-?\d+),", text.split("\n", 1)[0])
        if (head and head.group(1) != "0") or not re.search(r"^\s*Registered \d+ jobs?:", text, re.M):
            return "unread"
        package = (c.meta or {}).get("package") or _package_of(c)
        ids = scheduled_jobs(text, package)
        return ",".join(map(str, sorted(ids))) or "-"
    def jobsRegistered(c):
        """(WO-002 review) The jobIds the SDK ASKED JobScheduler to schedule in the window, from its
        `- registerTask: <taskId> (jobId: N)` lines, sorted, '-' for none.  The line is logged before
        BGTask.schedule() and schedule()'s result is discarded (BackgroundFetch.java:259-261, BGTask.java:169), so it
        does not prove JobScheduler holds the job: jobIds (step end) and jobsAtForceStop (the force_stop mark) read
        JobScheduler itself."""
        ids = {int(hit.group(1)) for hit in (re.search(r"\(jobId: (-?\d+)\)", line)
                                              for line in c.sdk("%- registerTask: %")) if hit}
        return ",".join(map(str, sorted(ids))) or "-"

    def configureOutcome(c):
        """(WO-002 review) Did every configure() the driver made resolve?  From the driver's own lines in the whole log
        (configure runs at boot, before a trigger): `app=config json=` before each call, `app=configure
        source=<boot|event-arm|…> outcome=<ok|rejected|skipped>` when it settles (or is skipped).  `ok` when each one
        made resolved, '-' when none was made (`skipped` is not one), else `<source>=<outcome>,…` for the ones that
        did not and `unsettled=<n>` for calls that never settled (counted per process).  Implicit `ok`."""
        own = _own_pids(m, c)
        rows = [(m.line_pid(l), c.norm[l]) for l in c.lines if m.line_pid(l) in own]
        calls = collections.Counter(p for p, n in rows if m.TAG + " app=config json=" in n)
        made = [(p, c.field(n, "source"), c.field(n, "outcome")) for p, n in rows if m.TAG + " app=configure " in n]
        settled = collections.Counter(p for p, _, out in made if out in ("ok", "rejected"))
        unsettled = sum(max(0, calls[p] - settled[p]) for p in calls)
        bad = ["%s=%s" % (src, out) for _, src, out in made if out not in ("ok", "skipped")]
        if unsettled:
            bad.append("unsettled=%d" % unsettled)
        if bad:
            return ",".join(bad)
        return "ok" if any(out == "ok" for _, _, out in made) else "-"

    def jobsAtForceStop(c):
        """(WO-002 review) The jobs JobScheduler held just before the step's last `force_stop:` (its mark's `held=`,
        the host's `dumpsys jobscheduler <pkg>` read then): `999`, '-' for none, `unread`, `no-force-stop`."""
        marks = [n for n in (c.norm[l] for l in c.lines) if m.TAG + " point=host " in n and " action=force_stop " in n]
        return c.field(marks[-1], "held", "?") if marks else "no-force-stop"

    def bootBroadcasts(c):
        """(WO-002 review) The broadcasts the SDK's BootReceiver logged in the whole log, `<ACTION>=n,…` ('-' none).
        On the lab's Android 16 every launch after an `am force-stop` of an app already launched delivers
        BOOT_COMPLETED (the app leaving the stopped state) — run.py force-stops before every step — and the SDK's
        onBoot clears a startOnBoot:false config off the UI thread around configure() (BackgroundFetch.java:101-121;
        a race, SUSPECTED, not reproduced).  Recorded, so a step that goes red can be read against it."""
        own = _own_pids(m, c)
        acts = [hit.group(1).rsplit(".", 1)[-1] for hit in (re.search(r"BootReceiver: (\S+)", c.norm[l])
                for l in c.lines if SDK_LINE.match(l) and m.line_pid(l) in own) if hit]
        return ",".join("%s=%d" % (a, acts.count(a)) for a in sorted(set(acts))) or "-"

    def foreignLines(c):
        """(WO-002 review) SDK lines (tag TSBackgroundFetch) and the drivers' `point=headless` / `point=task-` lines
        in the whole log from a process that is not the app's (own_pids): another lab app's fetch, which the
        vendored headless metrics (not scoped by process) would count as this app's, and devices.contamination
        (which reads `app=` lines and launch marks) does not see.  Implicit 0."""
        own = _own_pids(m, c)
        return sum(1 for l in c.lines if m.line_pid(l) not in own and (
            SDK_LINE.match(l) or m.TAG + " point=headless" in c.norm[l] or m.TAG + " point=task-" in c.norm[l]))

    # ---- (WO-002 Tier 1, react-native Android) -------------------------------------------------------------------

    def own_window(c):
        """The window's lines from the app's own processes, with their normalized text."""
        own = _own_pids(m, c)
        return [(l, c.norm[l]) for l in c.window if m.line_pid(l) in own]

    def jobStops(c):
        """(WO-002 Tier 1) JobScheduler's own word on how each run of the app's jobs ended, inside the step: the
        `Job history:` STOP rows of jobscheduler.txt, `<jobId>:<reason>=n,…` (non-word runs as `_`), sorted by jobId
        then reason; '-' for none.  The SDK logs nothing when it ignores a run < 1 s after scheduling
        (FetchJobService.java:26-31) — JobScheduler records `onStartJob returned false` — and a delivered run reads
        `app called jobFinished`.  A row's age is relative to the dump; it is placed on the host clock from the
        header's `host_ms` (else run.json's capture_extras phase) and kept from the step's measure_from mark (launch
        or trigger, run.json timings) on: the history spans the device's life.  `no-read` / `unread` as jobIds;
        `no-epoch` for a capture that cannot place a row (written before this metric, or another measure_from)."""
        path = os.path.join(c.path, JOBSCHEDULER)
        if not os.path.exists(path):
            return "no-read"
        text = open(path, errors="replace").read()
        head = text.split("\n", 1)[0]
        code = re.search(r"\(exit (-?\d+),", head)
        if code and code.group(1) != "0":
            return "unread"
        timings = (c.meta or {}).get("timings") or {}
        phases, t0 = timings.get("phases") or {}, timings.get("t0")
        stamp = re.search(r"\bhost_ms=(\d+)", head)
        dump = (int(stamp.group(1)) if stamp else
                (t0 + phases["capture_extras"]) * 1000 if t0 is not None and "capture_extras" in phases else None)
        mark = {"launch": "launch_mark", "trigger": "trigger"}.get((c.meta or {}).get("measure_from", "trigger"))
        if dump is None or t0 is None or phases.get(mark) is None:
            return "no-epoch"
        start, package, rows, inside = (t0 + phases[mark]) * 1000, _package_of(c), [], False
        for line in text.splitlines():
            if re.match(r"^\s*Job history:", line):
                inside = True
                continue
            if inside and line.strip() and not HISTORY_ROW.match(line) and not line.startswith("     "):
                inside = False
            hit = HISTORY_ROW.match(line) if inside else None
            if not hit or hit.group(2) != "STOP" or hit.group(4) != package:
                continue
            age = history_age_ms(hit.group(1))
            if age is not None and dump - age >= start:
                rows.append((int(hit.group(3)), re.sub(r"\W+", "_", hit.group(5) or "-").strip("_") or "-"))
        return ",".join("%d:%s=%d" % (j, r, rows.count((j, r))) for j, r in sorted(set(rows))) or "-"

    def jobsCancelled(c):
        """(WO-002 Tier 1) The jobIds the SDK cancelled in the window, from `- cancel taskId=<t>, jobId=<n>`
        (BGTask.java:268: stop(), stopOnTerminate's lazy stop, a reschedule), sorted, each once; '-' for none."""
        ids = {int(hit.group(1)) for hit in (re.search(r"\bjobId=(-?\d+)", line)
                                              for line in c.sdk("%- cancel taskId=%")) if hit}
        return ",".join(map(str, sorted(ids))) or "-"

    def rnHeadlessErrors(c):
        """(WO-002 Tier 1) React Native HeadlessTask's error lines in the window (HeadlessTask.java:44, :86, :106,
        :119, :140, :151): a failed invoke, a task refused in the foreground, the bridgeless reflection errors."""
        return sum(len(c.sdk(like)) for like in RN_HEADLESS_ERRORS)

    def headlessNoTask(c):
        """(WO-002 Tier 1) React Native's `No task registered for key BackgroundFetch` from the app's processes in
        the window: a headless task started before the JS bundle registered it (RN 4.4.2's cold-start race)."""
        return sum(1 for _, n in own_window(c) if RN_NO_TASK in n)

    def driverEvents(c):
        """(WO-002 Tier 1) The foreground driver's deliveries from configure()'s callbacks, `fetch=n,timeout=n`
        ('-' none): its `app=event=<fetch|timeout> n=<k> taskId=<id>` lines (index.js onDelivery).  A headless
        delivery is never one (headlessEvents counts those)."""
        return tally([hit.group(1) for hit in (re.search(m.TAG + r" app=event=(fetch|timeout) ", n)
                                               for _, n in own_window(c)) if hit])

    def driverTally(c):
        """(ios-task-sim) The foreground driver's deliveries by kind and taskId, `<fetch|timeout>:<taskId>=n` ('-'
        none): which task each `app=event=… taskId=<id>` line was for.  fetchTally is the Android SDK's view; this one
        reads the driver, so it holds on both OSes."""
        return tally(["%s:%s" % (hit.group(1), c.field(n, "taskId")) for hit, n in
                      ((re.search(m.TAG + r" app=event=(fetch|timeout) ", n), n) for _, n in own_window(c)) if hit])

    def driverFinishes(c):
        """(WO-002 Tier 1) How the foreground driver finished each delivery, `<fetch|timeout>:<outcome>=n` ('-'
        none): its `app=finish taskId=… timeout=<bool> … outcome=<ok|withheld|threw>` lines (index.js onDelivery;
        the knobs withholdFinish / finishOnTimeout decide)."""
        return tally(["%s:%s" % ("timeout" if c.field(n, "timeout") == "true" else "fetch", c.field(n, "outcome"))
                      for _, n in own_window(c) if m.TAG + " app=finish " in n])

    def appProcesses(c):
        """(WO-002 Tier 1) How many processes of the app started in the window: its `point=appCreate` lines
        (MainApplication), one per process.  A kill then a job reads 2; a job into the process the finished
        Activity left reads 1."""
        return len({m.line_pid(l) for l, n in own_window(c) if m.TAG + " point=appCreate " in n})

    def fetchTimeoutSkewMs(c):
        """(WO-002 Tier 1) How far the SDK's timeout landed from BGTASK_TIMEOUT_MS after the delivery it times out:
        the largest |`[BGTask] timeout: <t>` − `- Background Fetch event received: <t>` − 60000| ms in the window,
        each timeout against the latest receipt of its taskId before it; '-' for none.  The timer is posted just
        before the receipt is logged (BGTask.java:85, FetchJobService.java:57-58), so a sound run reads a few ms."""
        received, skews = {}, []
        for l, n in own_window(c):
            if not SDK_LINE.match(l):
                continue
            got = re.search(r"- Background Fetch event received: (\S+)", n)
            if got:
                received[got.group(1)] = c._ts(l)
                continue
            out = re.search(r"\[BGTask\] timeout: (\S+)", n)
            if out and received.get(out.group(1)) is not None:
                skews.append(int(round(abs(c._ts(l) - received[out.group(1)] - BGTASK_TIMEOUT_MS))))
        return max(skews) if skews else "-"

    def headlessLatencyMs(c):
        """(WO-002 Tier 1) The slowest hand-over to a headless task: ms from the SDK's `- Background Fetch event
        received: <t>` to the driver's `point=headless event=fetch … taskId=<t>`, each headless line against the
        latest receipt of its taskId before it; '-' for none.  A headless `event=timeout` is not a hand-over of a
        receipt (BGTask.onTimeout fires it 60 s later): headlessEvents.timeout and fetchTimeoutSkewMs read those.
        RN 4.4.2 (61695d4) replaced a postDelayed(500) on the cold path with runOnJSQueueThread: MEASURED 69-75 ms
        since (termination-calibration, both Lab AVDs)."""
        received, waits = {}, []
        for l, n in own_window(c):
            got = re.search(r"- Background Fetch event received: (\S+)", n) if SDK_LINE.match(l) else None
            if got:
                received[got.group(1)] = c._ts(l)
            elif m.TAG + " point=headless event=fetch " in n and received.get(c.field(n, "taskId")) is not None:
                waits.append(int(round(c._ts(l) - received[c.field(n, "taskId")])))
        return max(waits) if waits else "-"

    def lifecycleTally(c):
        """(WO-002 Tier 1) The SDK's LifecycleManager lines in the window (LifecycleManager.java: `☯️  onStart`,
        `onResume`, …, `☯️  HeadlessMode? <bool>` as `headless:<bool>`), tallied: why the SDK thought the app
        headless, or not, around a recreation (recreate-foreground-android)."""
        keys = []
        for line in c.sdk("%☯️%"):
            hit = re.search(r"☯️\s+(HeadlessMode\? (\w+)|(on\w+))", line)
            if hit:
                keys.append("headless:%s" % hit.group(2) if hit.group(2) else hit.group(3))
        return tally(keys)

    for fn in (fetchTally, jobIds, jobsRegistered, configureOutcome, jobsAtForceStop, bootBroadcasts, foreignLines,
               jobStops, jobsCancelled, rnHeadlessErrors, headlessNoTask, driverEvents, driverTally, driverFinishes,
               appProcesses, fetchTimeoutSkewMs, headlessLatencyMs, lifecycleTally):
        _extend(m.METRICS, fn.__name__, fn)


START_PROC = re.compile(r"\bStart proc (\d+):([\w.]+(?::[\w.]+)?)/")


def configure_settled(lk, lines, step):
    """The live read's half of configureOutcome (labkit.evidence_state above): the driver's configure() calls
    (`app=config json=`) have all settled (`app=configure … outcome=ok|rejected`), and — unless the step keeps the
    process (`process: live`: the driver configures once per JS context, and the log is cleared per step) — the
    driver has logged its boot decision (`app=configure source=…`, `skipped` included)."""
    lines = [l for l in lines if lk.TAG in l]
    own = lk.app_pids(lines)
    mine = [lk.normalize(l) for l in lines if lk.line_pid(l) in own]
    calls = sum(1 for n in mine if lk.TAG + " app=config json=" in n)
    made = [n for n in mine if lk.TAG + " app=configure " in n]
    settled = sum(1 for n in made if re.search(r"\boutcome=(ok|rejected)\b", n))
    return calls <= settled and (bool(made) or step.get("process") == "live")


def _own_pids(lk, c):
    """The processes of the app under test in a capture.  Where the capture's package is known (Android:
    jobscheduler.txt's header): every process tied to it — a `LAB point=appCreate … process=<pkg>[:sub]` line (the
    app's MainApplication, a headless start included) or an ActivityManager `Start proc <pid>:<pkg>[:sub]/` — plus,
    on a `process: live` step only (its process started before the step's log), the driver's `app=boot` pids that
    no line ties to another package.  Where it is not (iOS): the driver's `app=boot` pids and every appCreate pid.
    Logcat is device-wide; another lab app's lines are not this step's (foreignLines counts them)."""
    own = c.__dict__.get("_fetch_own_pids")
    if own is not None:
        return own
    package = _package_of(c)
    ours = lambda proc: package is None or proc == package or proc.startswith(package + ":")
    tied, other = set(), set()
    for line in c.lines:
        n = c.norm[line]
        if lk.TAG + " point=appCreate " in n:
            (tied if ours(c.field(n, "process", "")) else other).add(lk.line_pid(line))
            continue
        hit = START_PROC.search(line)
        if hit and package is not None:
            (tied if ours(hit.group(2)) else other).add(hit.group(1))
    boot = set(lk.app_pids(c.lines)) - other
    live = isinstance((c.meta or {}).get("process"), dict) and c.meta["process"].get("live")
    own = (tied | boot) if package is None or live else tied
    own.discard(None)
    c.__dict__["_fetch_own_pids"] = own
    return own


def _package_of(c):
    """The capture's app package, from its jobscheduler.txt header (run.json does not record it)."""
    try:
        head = open(os.path.join(c.path, JOBSCHEDULER), errors="replace").readline()
    except OSError:
        return None
    hit = re.search(r"dumpsys jobscheduler (\S+)", head)
    return hit.group(1) if hit else None

# ---------------------------------------------------------------------------------------------------- run


@patches("devices")
def _devices(m):
    original_contamination = m.contamination

    def contamination(lines, label, os_name="android", package=None):
        """The vendored check, which on iOS counts any second `app=boot` in a step as another lab's app ("no trigger
        relaunches it", devices.py:225-228).  (WO-002 phase 2a) fetch's `terminate` action does: SpringBoard then
        launches the app in the background for the next `fire` (ios-fetch-sim's cold step; MEASURED 2026-11-10,
        pid 30305 after `action=terminate`).  A boot that follows one of this step's own `action=terminate` host
        marks is that relaunch, one boot per terminate; every other finding stands."""
        import labkit
        out = original_contamination(lines, label, os_name, package)
        if os_name != "ios" or not out:
            return out
        relaunched, pending = set(), 0
        for line in lines:
            n = labkit.normalize(line)
            if "point=host " in n and " action=terminate" in n:
                pending += 1
            elif pending and labkit.TAG + " app=boot " in n:
                relaunched.add(labkit.line_pid(line))
                pending -= 1
        return [o for o in out if o not in {"lab process pid %s booted mid-step" % pid for pid in relaunched}]
    _rebind(m, "contamination", contamination)
    return ["contamination"]


seam("devices.contamination", "wrap", "devices.py:225-228 (iOS: a second boot is another lab app)",
     "an iOS boot after the step's own `terminate` action is the app relaunched by SpringBoard, not another lab app")


@patches("run")
def _run(m):
    _check_pinned("run", m)
    original_assert_native = m.assert_native

    def assert_native(dev, plat, var):
        """The vendored check, its message naming fetch's SDK."""
        try:
            return original_assert_native(dev, plat, var)
        except SystemExit as e:
            if isinstance(e.code, str):
                raise SystemExit(e.code.replace("TSLocationManager", NATIVE_PROJECT)
                                 .replace("publish_native_ios.sh", "publish_native_fetch_ios.sh"))
            raise
    _rebind(m, "assert_native", assert_native)
    return ["assert_native"]


seam("run.assert_native", "wrap", "run.py:302-317 (:316 wording)", "names TSBackgroundFetch")
seam("run.run_step eviction", "none", "run.py:719-725",
     "not overridden: bg-forge WO-191 evicts every installed com.transistorsoft.labs.* and .fetchlab.* app but the one "
     "under test (and force-stops every other Transistor app on Android), and flags a foreign headless process")
seam("_shared/labapi.js, labevents.js screen titles", "none", "labapi.js:96, labevents.js:53,96",
     "not overridden (vendored JS, read on the device): the driver's screen says 'BackgroundGeolocation api calls' / "
     "'`on*` events' — cosmetic; seam: a title the driver passes in")

# ---------------------------------------------------------------------------------------------------- sweep, inventory


@patches("sweep")
def _sweep(m):
    _rebind(m, "SUITE", list(SUITE))
    _rebind(m, "LISTS", {k: list(v) for k, v in LISTS.items()})
    return ["SUITE", "LISTS"]


seam("sweep.SUITE / LISTS", "data", "sweep.py:64-105, :115-164", "fetch's scenario lists")


# inventory.ANDROID_ARTIFACTS reads labkit.NATIVE (bg-forge WO-191): tsbackgroundfetch, set by register().

# ---------------------------------------------------------------------------------------------------- lab_setup


@patches("lab_setup")
def _lab_setup(m):
    _check_pinned("lab_setup", m)
    # The submodules the lab builds from: the native SDK (both OSes from one checkout), the types (the vocabulary's
    # source, which the fetch selftest reads), and the four bridges the platform apps build against.
    _rebind(m, "SUBMODULES", {"android": [], "ios": [],
                               "both": ["native/background-fetch", "types/background-fetch",
                                        "react-native/background-fetch", "cordova/background-fetch",
                                        "capacitor/background-fetch", "flutter/background_fetch"]})
    # (WO-191) GRADLE_PLACEHOLDERS is not rebound: only lab_setup's default Android slot reads it, and fetch's slot
    # (labkit.NATIVE, register()) runs publish_native_fetch.sh, whose build reads every property through findProperty
    # (tsbackgroundfetch/build.gradle :103-120), so it needs none.

    def capacitor_ready():
        plugin = os.path.join(m.REPO, "capacitor", "background-fetch")
        return all(os.path.isfile(os.path.join(plugin, "node_modules", dep, "package.json"))
                   for dep in ("@capacitor/core", "@transistorsoft/background-fetch-types"))
    _rebind(m, "capacitor_ready", capacitor_ready)

    def ensure_capacitor():
        m.step("3  Capacitor plugin deps")
        if capacitor_ready():
            print("ok: capacitor/background-fetch/node_modules")
            return
        m.sh(["npm", "ci", "--no-audit", "--no-fund"], cwd=os.path.join(m.REPO, "capacitor", "background-fetch"))
    _rebind(m, "ensure_capacitor", ensure_capacitor)

    def swift_api_published(label):
        """Fetch's Swift API (native/background-fetch ios/TSBackgroundFetch/SwiftInterface) beside the label, when
        the iOS pin copies it; the native-ios platform is not in the first slice."""
        root = os.path.join(m.LABS, "xcframeworks", label)
        return os.path.isfile(os.path.join(root, "Package.swift")) and bool(
            [p for p, _, files in os.walk(root) if "BackgroundFetch.swift" in files])
    _rebind(m, "swift_api_published", swift_api_published)

    original_check_repo = m.check_repo

    def check_repo(r, oses, var_name):
        """The vendored checks; a variant that does not load yet (no labs/harness/variants/<name>.yaml before the
        native pin lands) is a MISSING line, not a traceback."""
        try:
            return original_check_repo(r, oses, var_name)
        except (SystemExit, FileNotFoundError) as e:     # labkit.variant() opens the file bare (labkit.py:3062)
            if isinstance(e, SystemExit) and not isinstance(e.code, str):
                raise                    # labkit's own exit 2 (its product did not load): never a "missing variant"
            why = (e.code if isinstance(e, SystemExit) and isinstance(e.code, str) else
                   "no labs/harness/variants/%s.yaml" % var_name if isinstance(e, FileNotFoundError) else str(e))
            r.line(False, "variant %s" % var_name, why[:90],
                   "write labs/harness/variants/%s.yaml naming a published label (WO-002 phase 2, native pinning)"
                   % var_name)
            return None
    _rebind(m, "check_repo", check_repo)

    original_line = m.Report.line

    def line(self, ok, name, detail, fix=None, tool=False):
        """The vendored report line; check_repo's Capacitor detail names bg-geo's plugin path (lab_setup.py:260)."""
        if detail == "capacitor/background-geolocation/node_modules":
            detail = "capacitor/background-fetch/node_modules"
        return original_line(self, ok, name, detail, fix, tool)
    _rebind(m.Report, "line", line)

    return ["SUBMODULES", "capacitor_ready", "ensure_capacitor", "swift_api_published", "check_repo", "Report.line"]


seam("lab_setup.SUBMODULES", "data", "lab_setup.py:44-47",
     "fetch's submodules (doctor and check_access read it before PyYAML exists: not a labkit hook, WO-191 D4 option A)")
seam("lab_setup.capacitor_ready / ensure_capacitor", "replace", "lab_setup.py:214-217, :381-386 (:384 prints the bg-geo path)",
     "capacitor/background-fetch and its types dependency — seam: a CAPACITOR_PLUGIN path")
seam("lab_setup.swift_api_published", "replace", "lab_setup.py:244-250",
     "fetch's Swift API (BackgroundFetch.swift) beside the label — waits for the iOS pin (WO-191 D4)")
seam("lab_setup.check_repo", "wrap", "lab_setup.py:253-276 (labkit.py:3062 opens the variant bare)",
     "a variant that does not load is a MISSING line, not a traceback")
seam("lab_setup.Report.line", "wrap", "lab_setup.py:260 (the Capacitor line's detail text)",
     "names capacitor/background-fetch — seam: the CAPACITOR_PLUGIN path above")
seam("lab_setup.check_access", "none", "lab_setup.py:339-352",
     "not overridden: its private list (native/android, native/ios) is never missing here, so it checks nothing")

# ---------------------------------------------------------------------------------------------------- activation

SCRIPTS = {"run.py": "run", "sweep.py": "sweep", "lab_setup.py": "lab_setup", "lab_devices.py": "lab_devices",
           "summarize.py": "summarize", "inventory.py": "inventory"}


def _apply(name, module, as_main):
    """Patch one vendored module.  Any failure while patching — a seam that moved, a pinned function that changed
    (PackError), a renamed name a wrapper reads — stops the process with exit 2, the harness's "could not be judged":
    never a run with half fetch's seams and half bg-geo's."""
    try:
        symbols = PATCHES[name](module)
    except PackError as e:
        why = str(e)
    except Exception as e:            # a bare read of a renamed seam (AttributeError), getsource on a non-function, …
        why = "patching vendored %s failed (%s: %s) — the seam moved since %s" % (name, type(e).__name__, e, UPSTREAM)
    else:
        _report(name, as_main, symbols)
        return
    sys.stderr.write("fetch pack: %s — refusing to run the harness (labs/README.md, \"The pack\")\n" % why)
    sys.stderr.flush()
    os._exit(2)


class _Loader(importlib.abc.Loader):
    def __init__(self, inner, name):
        self.inner, self.name = inner, name

    def create_module(self, spec):
        return self.inner.create_module(spec)

    def exec_module(self, module):
        self.inner.exec_module(module)
        _apply(self.name, module, False)

    def __getattr__(self, attr):          # get_source / get_code / is_package: tracebacks and pkgutil
        return getattr(self.inner, attr)


class _Finder(importlib.abc.MetaPathFinder):
    """Patches a vendored module of labs/harness right after its body runs; anything else is left alone."""

    def find_spec(self, name, path=None, target=None):
        if name not in PATCHES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not spec.origin or os.path.dirname(os.path.realpath(spec.origin)) != HARNESS:
            return None
        spec.loader = _Loader(spec.loader, name)
        return spec


def _patch_main(frame, event, arg):
    """One-shot: the first call of `main` in __main__ (a vendored script) patches that script's globals first."""
    if event != "call" or frame.f_code.co_name != "main" or frame.f_globals.get("__name__") != "__main__":
        return
    sys.setprofile(None)
    name = SCRIPTS.get(os.path.basename(frame.f_globals.get("__file__") or ""))
    main = sys.modules.get("__main__")
    if name and name in PATCHES and main is not None:
        _apply(name, main, True)


def activate():
    """Install the post-import hook and, for a vendored script run as __main__, the main() hook.  Idempotent."""
    if any(isinstance(f, _Finder) for f in sys.meta_path):
        return
    # sys.path is left alone: a vendored script has labs/harness as sys.path[0] already, and any other process the
    # harness starts (pip in the venv, say) never imports a vendored module, so the hook never fires there.
    sys.meta_path.insert(0, _Finder())
    for name in [n for n in PATCHES if n in sys.modules]:    # imported before activation: patch now
        mod = sys.modules[name]
        if os.path.dirname(os.path.realpath(getattr(mod, "__file__", "") or "")) == HARNESS:
            _apply(name, mod, False)
    argv0 = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if os.path.dirname(argv0) == HARNESS and os.path.basename(argv0) in SCRIPTS:
        sys.setprofile(_patch_main)


def active():
    return any(isinstance(f, _Finder) for f in sys.meta_path)


def seams_table():
    rows = ["%-48s %-8s %-40s %s" % ("override", "kind", "vendored seam (%s)" % UPSTREAM, "why")]
    for o in OVERRIDES:
        rows.append("%-48s %-8s %-40s %s" % (o["symbol"], o["kind"], o["seam"], o["why"]))
    rows += ["", "pinned (copied or re-implemented by the pack; source sha256 at %s, checked at import):" % UPSTREAM]
    rows += ["  %-46s %s" % (q, h) for q, h in PINNED.items()]
    return "\n".join(rows)


if __name__ == "__main__":
    if sys.argv[1:] == ["--seams"]:
        print(seams_table())
    elif sys.argv[1:] == ["--json"]:
        print(json.dumps(OVERRIDES, indent=1))
    else:
        sys.exit("usage: fetch_pack.py --seams | --json")
