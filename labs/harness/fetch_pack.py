"""The fetch pack (WO-002): every place background-fetch differs from the vendored bg-forge harness, in ONE module.

The harness beside this file is bg-forge's, vendored byte-identical (labs/VENDORED.md, D1: commit 49d38d47f).  It
carries background-geolocation names at known seams.  Nothing here edits a vendored file: each override below
rebinds a name in a vendored module after that module is imported, and each names the vendored line it stands in
for (`SEAM`) — the hook bg-forge should grow in Phase B, so this module shrinks to data.

How it is made active in EVERY Python process the harness starts:
  * `./labkit` exports PYTHONPATH=labs/harness/pack and FETCH_LABKIT_PACK=1.  labs/harness/pack/sitecustomize.py
    (chaining whatever sitecustomize the interpreter ships, Homebrew's included) calls activate().
  * activate() installs a post-import hook: when a vendored module (`labkit`, `backends`, `run`, …) is imported from
    labs/harness, its patches run right after its body does.
  * A vendored SCRIPT run as __main__ (`run.py`, which sweep.py starts as `sys.executable -u run.py` at
    sweep.py:644; `lab_setup.py`, which re-execs itself) has its own globals patched the moment its main() is
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
import threading
import time

HARNESS = os.path.dirname(os.path.abspath(__file__))
LABS = os.path.dirname(HARNESS)
UPSTREAM = "bg-forge 49d38d47f"
ENV_FLAG = "FETCH_LABKIT_PACK"
ENV_REPORT = "FETCH_PACK_REPORT"       # a file: every patched module appends one JSON line (the selftest's proof)

# ---------------------------------------------------------------------------------------------------- fetch's data

SDK_TAG = "TSBackgroundFetch"                       # Android: BackgroundFetch.java:23 `TAG = "TSBackgroundFetch"`
# iOS: the SDK NSLogs with no os_log subsystem; every line opens `[TSBackgroundFetch …`, `[TSBGTask …` or
# `[TSBGAppRefreshSubscriber …` (TSBackgroundFetch.m:13, TSBGTask.m:13/273/293, TSBGAppRefreshSubscriber.m:14).
IOS_SDK_PREFIX = "[TSB"
NATIVE_PROJECT = "TSBackgroundFetch"                # PROJECT:TSBackgroundFetch-<build> (apple-generic versioning)
ANDROID_ARTIFACT = "tsbackgroundfetch"              # labs/m2/com/transistorsoft/tsbackgroundfetch/<label>
XCFRAMEWORK = "TSBackgroundFetch.xcframework"       # labs/xcframeworks/<label>/TSBackgroundFetch.xcframework
FETCH_JOB_ID = 999                                  # BackgroundFetchConfig.java:265
# FetchJobService.java:24-31 drops a run < 1000 ms after the job's `scheduled_at`; :38-51 drops a repeat of the same
# taskId within 5000 ms.  A `job:` action keeps outside both, with margin.
JOB_SETTLE_S = 1.5
JOB_REPEAT_S = 5.5
JOBSCHEDULER = "jobscheduler.txt"                   # the capture's `dumpsys jobscheduler <pkg>`, at the step's end
FETCH_ACTIONS = ("job", "force_stop")
# The first slice's suite (WO-002 Log, 2026-11-05).  A name with no scenario file yet is refused by sweep as usual.
SUITE = ["termination-calibration", "api-smoke", "events-fetch-android"]
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
# pack keeps a second copy of.  The run_host copy is approved on this condition (WO-002 Log, 2026-11-05) until bg-forge
# grows an ACTION_HANDLERS seam and the copy is deleted.
PINNED = {
    "run.run_host": "a96c663497be2bdb49ba0bcfd0f9f4228ea9b76b30a9df42e21f7311c51ad253",
    "backends.Simctl.native_resolved": "d096d885420974749ff7c464745e85ad4a3e8663e2e1a0a0c31d454eb7b81c8a",
    "backends.Simctl.permissions": "21c189bfb25c46c7f4926b616cf7bae886c6f8cb4b91f6cc2807ead03c4692d2",
    "backends.Adb.sdk_db": "a409db941314b6e45fbb33e34026893c2efbff300af1451e770b9e42deb39e44",
    "backends.Simctl.sdk_db": "38ece09ef32f44b212005f479491f7629bd5f9c58dae672b00df07dc63d1abd4",
    "labkit.Capture.sdk": "08d8b8ca25c8476e4fa88ead814737b21f57ea5281757b2669dcf675f8500e7f",
    "lab_setup.ensure_native": "8b5090f3b1ad21e13f555ba1e17d96301df76578ac2d27b49651737b6e5bdba9",
    "lab_setup.published": "bd7ac84931e63616421294e982e568b5658aaea5f602df89328e29146afd2cff",
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
    # The tap's predicate is a format string around SDK_SUBSYSTEM (backends.py:1289-1292), and fetch has no
    # subsystem: the value closes the subsystem clause and opens fetch's own.  The predicate the Simulator gets:
    #   (eventMessage CONTAINS "LAB ") OR (subsystem == "com.transistorsoft.fetchlab.none")
    #   OR (eventMessage BEGINSWITH "[TSB") OR (eventMessage CONTAINS "LICENSE VALIDATION FAILURE")
    _rebind(m, "SDK_SUBSYSTEM", 'com.transistorsoft.fetchlab.none") OR (eventMessage BEGINSWITH "%s' % IOS_SDK_PREFIX)
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

    original_end_log = m.Adb.end_log

    def end_log(self, plat):
        """The vendored log dump, then the host's read of the app's jobs: `dumpsys jobscheduler <pkg>` into the
        capture as jobscheduler.txt (the step's dir, which the run_step wrapper below hands the device)."""
        text = original_end_log(self, plat)
        out = getattr(self, "_fetch_capture_out", None)
        if out:
            r = labkit.device_call(["adb", "-s", self.serial, "shell", "dumpsys", "jobscheduler", plat["package"]],
                                   timeout=30)
            with open(os.path.join(out, JOBSCHEDULER), "w") as f:
                f.write("# adb -s %s shell dumpsys jobscheduler %s  (exit %d, read %s)\n%s%s"
                        % (self.serial, plat["package"], r.returncode, time.strftime("%Y-%m-%d %H:%M:%S"),
                           r.stdout, r.stderr))
        return text
    _rebind(m.Adb, "end_log", end_log)

    # The fetch host actions' device primitives (dispatched by the run.run_host wrapper below).
    def job(self, plat, value, expect="present"):
        """`cmd jobscheduler run -f <pkg> <jobId>`, outside the SDK's guards: a repeat of the same job on this device
        waits out JOB_REPEAT_S from the last one (FetchJobService.java:38-51); JOB_SETTLE_S after scheduling is the
        action's `delay_s`, held at load.  Returns the mark's text after `action=`.  What the step expects of the
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
        waited = 0.0
        if key in fired and time.time() - fired[key] < JOB_REPEAT_S:
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
        return "job %d%s%s%s said=%s" % (jid, " expect=absent" if expect == "absent" else "",
                                         " waited_ms=%d" % (waited * 1000) if waited else "",
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

    for name in ("job", "force_stop"):
        def refuse(self, *a, name=name):
            raise SystemExit("the Simulator has no `%s` — labkit refuses `actions:` on a scenario that does not "
                             "excuse ios" % name)
        _extend(m.Simctl, name, refuse)
    return ["SDK_TAG", "SDK_SUBSYSTEM", "to_logcat", "Adb.sdk_db", "Simctl.sdk_db", "PERMISSIONS",
            "FLAG_PERMISSIONS", "PRIVACY", "SDK_ALERT_TITLES", "Simctl.permissions", "Simctl.native_resolved",
            "Adb.end_log", "Adb.job", "Adb.force_stop", "Simctl.job", "Simctl.force_stop"]


seam("backends.SDK_TAG", "data", "backends.py:40", "fetch's logcat tag, TSBackgroundFetch")
seam("backends.SDK_SUBSYSTEM", "data", "backends.py:39 + :1289-1292",
     "fetch has no os_log subsystem; the value is spliced into the tap predicate to add `eventMessage BEGINSWITH "
     "\"[TSB\"` — seam: an IOS_LOG_PREDICATE constant")
seam("backends.to_logcat", "wrap", "backends.py:992-1021 (:1015)",
     "retags fetch's NSLog lines (`[TSB…`) to SDK_TAG — seam: an sdk_line(ev) predicate")
seam("backends.Adb.sdk_db / Simctl.sdk_db", "replace", "backends.py:633-635, :1394-1400; run.py:1211",
     "no SDK database: b'' (run.py still writes an empty transistor_log.db) — seam: SDK_DB = None skips the pull")
seam("backends.PERMISSIONS / FLAG_PERMISSIONS / PRIVACY / SDK_ALERT_TITLES", "data",
     "backends.py:732, :954, :751, :806", "no location/motion permissions, no SDK alert")
seam("backends.Simctl.permissions", "replace", "backends.py:1094-1102 (:1095 hardcodes location-always, motion)",
     "grants nothing — seam: an IOS_GRANTS constant")
seam("backends.Simctl.native_resolved", "replace", "backends.py:1458-1490 (:1485 hardcodes TSLocationManager)",
     "reads PROJECT:TSBackgroundFetch-<build> — seam: a NATIVE_PROJECT constant")
seam("backends.Adb.end_log", "wrap", "backends.py:629-631; run.py:1170",
     "adds the host's `dumpsys jobscheduler <pkg>` read as jobscheduler.txt — seam: a capture_extras(dev, plat, out) "
     "hook")
seam("backends.Adb.job / Adb.force_stop / Simctl.job / Simctl.force_stop", "extend", "backends.py:238 (beside Adb.kill)",
     "the fetch host actions' device primitives; the Simulator's two refuse (actions are Android's)")

# ---------------------------------------------------------------------------------------------------- labkit


@patches("labkit")
def _labkit(m):
    _check_pinned("labkit", m)
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

    def sdk(self, like):
        """The SDK's own lines in the window, on both OSes from the log (fetch has no database): Android's logcat
        tag TSBackgroundFetch, and the iOS tap's lines retagged there (backends.to_logcat above), from the app under
        test's own processes only (own_pids).  Logcat is device-wide, and bg-forge's lab apps link TSBackgroundFetch
        too: the database the vendored method read was the app's own, so it was scoped by construction.  `like` is
        the vendored callers' SQL LIKE pattern (`%needle%`)."""
        rx, own = like_regex(like), _own_pids(m, self)
        return [self.norm[l] for l in self.window
                if SDK_LINE.match(l) and m.line_pid(l) in own and rx.search(self.norm[l])]
    _rebind(m.Capture, "sdk", sdk)

    # Every step that expects anything also expects (WO-002 review):
    #   * configureOutcome ok — its configure() resolved: a rejected or hung configure leaves start/stop/status acting
    #     on nothing, and api-smoke would still read green.  A step that makes none (the launch extra noConfigure)
    #     says `configureOutcome: "-"`; a `process: live` step is given "-" (expectations below: the RN driver
    #     configures once per JS context, and run.py clears the log at every step).
    #   * foreignLines 0 — no SDK, headless or task line from a process that is not the app's (own_pids): logcat is
    #     device-wide, bg-forge's lab apps link TSBackgroundFetch, and the vendored headless metrics read every pid.
    # Both read the whole log and are complete once the driver's configure() has settled (evidence_state below), so
    # they are early metrics: a step that may end on its evidence still does.
    _rebind(m, "IMPLICIT", dict(m.IMPLICIT, configureOutcome="ok", foreignLines=0))
    _rebind(m, "EARLY_METRICS", m.EARLY_METRICS | {"configureOutcome", "foreignLines"})

    original_evidence_state = m.evidence_state

    def evidence_state(lines, step, os_name):
        """The vendored live read, and not complete while a configure() the driver made has not settled, or (a fresh
        process) before the driver has logged its boot configure decision — so configureOutcome is final when the
        step ends on its evidence."""
        complete, alive = original_evidence_state(lines, step, os_name)
        if complete and not configure_settled(m, lines, step):
            return False, alive
        return complete, alive
    _rebind(m, "evidence_state", evidence_state)

    original_expectations = m.expectations

    def expectations(step, platform_name, os_name=None):
        """The vendored expectations; a `process: live` step that does not state configureOutcome expects '-'."""
        merged = original_expectations(step, platform_name, os_name)
        stated = set(step.get("expect") or {}) | set((step.get("platform_expect") or {}).get(platform_name) or {})
        if step.get("process") == "live" and "configureOutcome" in merged and "configureOutcome" not in stated:
            merged["configureOutcome"] = "-"
        return merged
    _rebind(m, "expectations", expectations)

    original_validate_moves = m._validate_moves

    def _validate_moves(where, step, excused=()):
        """The vendored check, with fetch's actions in it: each `job:` / `force_stop:` item is checked here, then
        handed to the vendored check as a `kill: true` stand-in, so its `after:`, `delay_s`, the window budget and
        the iOS excuse are held by vendored code; the resolved item gets its own key back."""
        acts = step.get("actions")
        if not isinstance(acts, list) or not any(isinstance(a, dict) and set(a) & set(FETCH_ACTIONS) for a in acts):
            return original_validate_moves(where, step, excused)
        own, stand, seen = {}, [], []
        for i, a in enumerate(acts):
            kinds = [k for k in FETCH_ACTIONS if isinstance(a, dict) and k in a]
            if not kinds:
                stand.append(a)
                continue
            at = "%s actions %d" % (where, i)
            does = kinds + [k for k in m.ACTION_DOES if k in a]
            if len(does) != 1:
                raise SystemExit("%s: an action does exactly one of %s — not %s"
                                 % (at, ", ".join(m.ACTION_DOES + FETCH_ACTIONS), " and ".join(does)))
            kind, value = kinds[0], a[kinds[0]]
            delay = a.get("delay_s", 0)
            expect = a.get("expect")
            extra = 0
            if "expect" in a and (kind != "job" or expect not in ("present", "absent")):
                raise SystemExit("%s: `expect:` belongs to a `job:` and is `present` (it runs: the default) or "
                                 "`absent` (JobScheduler holds no such job) — not %r on a `%s:`" % (at, expect, kind))
            if kind == "job":
                if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and value)):
                    raise SystemExit("%s: `job:` is the jobId JobScheduler holds (`%d`, the fetch job) or a "
                                     "scheduleTask taskId (its String.hashCode) — not %r" % (at, FETCH_JOB_ID, value))
                if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < JOB_SETTLE_S:
                    raise SystemExit("%s: a `job:` waits `delay_s: %g` or more after its conditions — "
                                     "FetchJobService ignores a run < 1 s after the job was scheduled "
                                     "(FetchJobService.java:24-31), and a run it ignores reads as an SDK defect"
                                     % (at, JOB_SETTLE_S))
                if job_id(value) in seen:          # the host waits out the SDK's duplicate guard: budget for it
                    extra = JOB_REPEAT_S
                seen.append(job_id(value))
            elif value is not True:
                raise SystemExit("%s: `force_stop:` is true, an `am force-stop` of the app — not %r" % (at, value))
            own[i] = (kind, value, delay, expect)
            s = {k: v for k, v in a.items() if k not in (kind, "expect")}
            s["kill"] = True
            if extra:
                s["delay_s"] = delay + extra
            stand.append(s)
        step["actions"] = stand
        original_validate_moves(where, step, excused)
        resolved = []
        for i, item in enumerate(step["actions"]):
            if i in own:
                kind, value, delay, expect = own[i]
                item = {k: v for k, v in item.items() if k != "kill"}
                item[kind] = value
                if expect is not None:
                    item["expect"] = expect
                if "delay_s" in acts[i]:
                    item["delay_s"] = delay
                else:
                    item.pop("delay_s", None)
            resolved.append(item)
        step["actions"] = resolved
    _rebind(m, "_validate_moves", _validate_moves)

    original_validate_hooks = m._validate_hooks

    def _validate_hooks(where, step, excused=()):
        """The vendored check, with fetch's constants: a probe of a property is held to `function|object` at `$`
        (labkit.py:1165-1177: bg-geo's properties are namespaces and objects), but fetch's STATUS_* / NETWORK_TYPE_*
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

    _fetch_metrics(m)
    return ["CALLBACK_ARG", "FGS_PROMOTED", "Capture.sdk", "IMPLICIT", "EARLY_METRICS", "evidence_state",
            "expectations", "_validate_moves", "_validate_hooks", "METRICS(+fetch)"]


seam("labkit.CALLBACK_ARG", "data", "labkit.py:185",
     "the vendored pattern plus onEvent/onTimeout/task — seam: find callbacks by type (`=>`), as :1421 does")
seam("labkit.FGS_PROMOTED", "data", "labkit.py:4428",
     "the vendored pattern under fetch's tag: fetch starts no foreground service, so it never matches")
seam("labkit.IOS_GRACE_S", "none", "labkit.py:4413, ios_grace :4444",
     "not overridden: its keys are geo (addGeofence, watchPosition), no fetch hook names them, so 'default' (2 s)")
seam("labkit.Capture.sdk", "replace", "labkit.py:3193-3215",
     "reads TSBackgroundFetch lines from the log on both OSes, never transistor_log.db — seam: SDK log adapter")
seam("labkit._validate_moves", "wrap", "labkit.py:1565-1567 (ACTION_DOES), :1582-1688",
     "admits `job:` and `force_stop:` — seam: an ACTION_VALIDATORS table")
seam("labkit._validate_hooks", "wrap", "labkit.py:1165-1177 (a property probe's `$` is function|object)",
     "a probe of a constant (STATUS_*, NETWORK_TYPE_*: numbers) may assert its type and value — seam: "
     "`allowed |= wire_of(member)` for a property whose return has a `wire`")
seam("labkit.headless_names", "data", "labkit.py:3383-3386, :3411 (import time)",
     "reads vocabulary enums.Event: fetch's vocabulary carries [fetch, timeout] (a lab convention) — seam: a pack "
     "hook for headless names; `locationerror` stays appended")
seam("labkit.METRICS", "extend", "labkit.py:3221-3227 (the registry: no override)",
     "fetch's metrics: SDK-line counts, fetchTally, jobIds, jobsRegistered, configureOutcome")
seam("labkit.IMPLICIT / EARLY_METRICS", "data", "labkit.py:4316, :4339 (IMPLICIT read at :4746 with setdefault)",
     "adds `configureOutcome: ok` and `foreignLines: 0` to every step that expects anything, both early (whole-log) "
     "metrics — seam: a pack hook for implicit expectations")
seam("labkit.evidence_state", "wrap", "labkit.py:4604",
     "not complete while a configure() the driver made is unsettled — seam: an evidence hook per product")
seam("labkit.expectations", "wrap", "labkit.py:4732",
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


def _fetch_metrics(m):
    for name, (like, where) in SDK_COUNTS.items():
        def count(c, like=like):
            return len(c.sdk(like))
        count.__name__ = name
        count.__doc__ = "(WO-002) SDK lines `%s` in the window (Android, %s)." % (like.strip("%"), where)
        _extend(m.METRICS, name, count)

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

    for fn in (fetchTally, jobIds, jobsRegistered, configureOutcome, jobsAtForceStop, bootBroadcasts, foreignLines):
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


def _fetch_kind(item):
    return next((k for k in FETCH_ACTIONS if isinstance(item, dict) and k in item), None)


@patches("run")
def _run(m):
    import labkit
    _check_pinned("run", m)
    original_run_host = m.run_host

    def run_host(dev, step, t_launch, deadline, stop, host, server=None, plat=None):
        """The vendored runner for every move and action it knows, untouched; fetch's `job:` / `force_stop:` items
        on a companion thread with the same rule — each acts once the log shows its `after:` (labkit.after_unmet over
        the driver's own lines and the host's marks) plus `delay_s`, and is marked `host i=<i> action=…` at its own
        index, which actionsDone scores.  The vendored runner is handed the step with each fetch item's `after:`
        replaced by a condition no log ever shows, so it keeps every index and never sends one.

        A PACK-SIDE COPY of run.run_host's wait-then-act loop (run.py:543-580), for fetch's kinds only: the
        vendored dispatch is a closed if/elif whose last branch treats any other key as `power_save`
        (run.py:581-601).  The upstream seam (an ACTION_HANDLERS table) deletes this copy."""
        actions = step.get("actions") or []
        mine = {i: a for i, a in enumerate(actions) if _fetch_kind(a)}
        if not mine:
            return original_run_host(dev, step, t_launch, deadline, stop, host, server, plat)
        never = [{"point": "fetch-pack-owned-%d" % os.getpid()}]
        shadow = dict(step, actions=[dict(a, after=never) if i in mine else a for i, a in enumerate(actions)])
        others = bool(step.get("moves")) or len(mine) < len(actions)
        companion = threading.Thread(target=_fetch_runner, args=(labkit, dev, step, mine, t_launch, deadline, stop,
                                                                  host, plat), daemon=True)
        companion.start()
        if others:
            original_run_host(dev, shadow, t_launch, deadline, stop, host, server, plat)
        companion.join()
    run_host.vendored = original_run_host
    _rebind(m, "run_host", run_host)

    original_run_step = m.run_step

    def run_step(dev, plat, scen, var, step, args, timings=None):
        """The vendored step, with the device told its capture directory (Adb.end_log above writes jobscheduler.txt
        there)."""
        dev._fetch_capture_out = labkit.capture_dir(plat["name"], scen["name"], var["name"], step["name"], dev.os)
        try:
            return original_run_step(dev, plat, scen, var, step, args, timings)
        finally:
            dev._fetch_capture_out = None
    _rebind(m, "run_step", run_step)

    original_assert_native = m.assert_native

    def assert_native(dev, plat, var):
        """The vendored check, its message naming fetch's SDK."""
        try:
            return original_assert_native(dev, plat, var)
        except SystemExit as e:
            if isinstance(e.code, str):
                raise SystemExit(e.code.replace("TSLocationManager", NATIVE_PROJECT)
                                 .replace("publish_native_ios.sh", "the fetch iOS pin (WO-002 phase 2)"))
            raise
    _rebind(m, "assert_native", assert_native)
    return ["run_host", "run_step", "assert_native"]


def _fetch_runner(labkit, dev, step, mine, t_launch, deadline, stop, host, plat):
    state = {i: {"held": None, "next": None} for i in mine}
    try:
        while not stop.is_set() and time.time() < deadline and state:
            lines, norm = dev.peek(), None
            now = time.time()
            for i, item in sorted(mine.items()):
                st = state.get(i)
                if st is None:
                    continue
                if st["held"] is None:
                    if norm is None:
                        own = labkit.app_pids(lines)
                        norm = [labkit.normalize(l) for l in lines if labkit.TAG in l and (
                            labkit.line_pid(l) in own or " point=" in labkit.normalize(l))]
                    if labkit.after_unmet(item["after"], norm, step) is not None:
                        continue
                    st["held"], st["next"] = now, now + item.get("delay_s", 0)
                    host["held_s"]["action%d" % i] = round(now - t_launch, 3)
                if now < st["next"]:
                    continue
                began = time.time()
                kind = _fetch_kind(item)
                what = (dev.job(plat, item["job"], item.get("expect", "present")) if kind == "job"
                        else dev.force_stop(plat))
                done = time.time()
                dev.mark("host i=%d action=%s sent_ms=%d done_ms=%d mark_ms=%d"
                         % (i, what, began * 1000, done * 1000, time.time() * 1000))
                host["sent_s"]["action%d" % i] = round(began - t_launch, 3)
                del state[i]
            stop.wait(labkit.POLL_S)
    except SystemExit as e:
        host["error"] = host["error"] or str(e.code if isinstance(e.code, str) else e)
    except Exception as e:
        host["error"] = host["error"] or "%s: %s" % (type(e).__name__, e)


seam("run.run_host", "wrap", "run.py:531-611 (dispatch :581-601)",
     "fetch's `job:` and `force_stop:` on a companion runner (a copy of the wait-then-act loop) — seam: an "
     "ACTION_HANDLERS table keyed by action name")
seam("run.run_step", "wrap", "run.py:614-616", "tells the device its capture dir, for jobscheduler.txt")
seam("run.assert_native", "wrap", "run.py:293-308 (:307 wording)", "names TSBackgroundFetch")
seam("run.run_step eviction", "none", "run.py:708-714",
     "not overridden: evicts this lab's platforms() only; prefix eviction of every Transistor lab app is a bg-forge "
     "work order (WO-002 'Lab devices')")
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


@patches("inventory")
def _inventory(m):
    _rebind(m, "ANDROID_ARTIFACTS", (ANDROID_ARTIFACT,))
    return ["ANDROID_ARTIFACTS"]


seam("inventory.ANDROID_ARTIFACTS", "data", "inventory.py:30", "tsbackgroundfetch")

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
    # tsbackgroundfetch/build.gradle reads every property through findProperty (:103-120): nothing is required at
    # configuration time, so no placeholder (signing is made optional by lab-publish.init.gradle).
    _rebind(m, "GRADLE_PLACEHOLDERS", {})

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

    def published(label, os_name):
        if os_name == "android":
            return (os.path.isdir(os.path.join(m.LABS, "m2", "com", "transistorsoft", ANDROID_ARTIFACT, label))
                    and os.path.isfile(os.path.join(m.LABS, "m2", ".provenance", label + ".txt")))
        return (os.path.isdir(os.path.join(m.LABS, "xcframeworks", label, XCFRAMEWORK))
                and os.path.isfile(os.path.join(m.LABS, "xcframeworks", ".provenance", label + ".txt")))
    _rebind(m, "published", published)

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
        except (SystemExit, FileNotFoundError) as e:     # labkit.variant() opens the file bare (labkit.py:2875)
            why = (e.code if isinstance(e, SystemExit) and isinstance(e.code, str) else
                   "no labs/harness/variants/%s.yaml" % var_name if isinstance(e, FileNotFoundError) else str(e))
            r.line(False, "variant %s" % var_name, why[:90],
                   "write labs/harness/variants/%s.yaml naming a published label (WO-002 phase 2, native pinning)"
                   % var_name)
            return None
    _rebind(m, "check_repo", check_repo)

    original_line = m.Report.line

    def line(self, ok, name, detail, fix=None, tool=False):
        """The vendored report line; check_repo's Capacitor detail names bg-geo's plugin path (lab_setup.py:249)."""
        if detail == "capacitor/background-geolocation/node_modules":
            detail = "capacitor/background-fetch/node_modules"
        return original_line(self, ok, name, detail, fix, tool)
    _rebind(m.Report, "line", line)

    def ensure_native(var, oses, logs):
        m.step("4  native SDK builds for %s (%s)" % (var["name"], var["native"]))
        src, label = var.get("native_source") or {}, var["native"]
        for os_name in oses:
            if published(label, os_name):
                print("ok: %s %s is published" % (os_name, label))
                continue
            if not src.get(os_name):
                raise SystemExit("setup stopped: %s is not published for %s, and variant %s records no native_source.%s"
                                 " to build it from" % (label, os_name, var["name"], os_name))
            wt = m.native_checkout("native/background-fetch", src[os_name], "native")
            if os_name == "android":
                print("(a few minutes: Gradle builds tsbackgroundfetch and publishes it to labs/m2)")
                m.sh([os.path.join(m.HARNESS, "publish_native_fetch.sh"), label, wt],
                     log=os.path.join(logs, "native-android.log"))
            else:
                raise SystemExit("setup stopped: the iOS pin for fetch (xcframeworks/%s/%s) is not built yet — WO-002 "
                                 "phase 2" % (label, XCFRAMEWORK))
    _rebind(m, "ensure_native", ensure_native)
    return ["SUBMODULES", "GRADLE_PLACEHOLDERS", "capacitor_ready", "ensure_capacitor", "published",
            "swift_api_published", "check_repo", "Report.line", "ensure_native"]


seam("lab_setup.SUBMODULES / GRADLE_PLACEHOLDERS", "data", "lab_setup.py:42-45, :52-56", "fetch's submodules; none")
seam("lab_setup.capacitor_ready / ensure_capacitor", "replace", "lab_setup.py:212-215, :368-373 (:371 prints the bg-geo path)",
     "capacitor/background-fetch and its types dependency — seam: a CAPACITOR_PLUGIN path")
seam("lab_setup.published / swift_api_published", "replace", "lab_setup.py:225-239 (:229 TSLocationManager)",
     "tsbackgroundfetch / TSBackgroundFetch.xcframework — seam: ANDROID_ARTIFACT / XCFRAMEWORK constants")
seam("lab_setup.check_repo", "wrap", "lab_setup.py:242-265 (labkit.py:2875 opens the variant bare)",
     "a variant that does not load is a MISSING line, not a traceback")
seam("lab_setup.Report.line", "wrap", "lab_setup.py:249 (the Capacitor line's detail text)",
     "names capacitor/background-fetch — seam: the CAPACITOR_PLUGIN path above")
seam("lab_setup.ensure_native", "replace", "lab_setup.py:403-437",
     "native/background-fetch checkout, publish_native_fetch.sh — seam: a native-build table per OS")
seam("lab_setup.check_access", "none", "lab_setup.py:326-339",
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
        if spec is None or not spec.origin or os.path.dirname(os.path.abspath(spec.origin)) != HARNESS:
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
        if os.path.dirname(os.path.abspath(getattr(mod, "__file__", "") or "")) == HARNESS:
            _apply(name, mod, False)
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
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
