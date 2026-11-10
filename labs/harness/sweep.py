#!/usr/bin/env python3
"""One command for a device sweep (WO-027).

    ./labkit sweep                          # the WO-027 suite, 4 bridges x 2 OSes (python3 labs/harness/sweep.py)
    ./labkit sweep --platforms flutter --os ios --scenarios events,api-smoke
    ./labkit sweep --build stale            # development: rebuild only stale cells

Two phases, in the order the lab's rules force:

1. BUILDS, strictly one at a time — two builds at once load the machine (README: "A loaded machine
   manufactures defects"), and the two OS halves of one platform share app/ (Cordova's iOS
   `plugin rm/add` once pulled the Java sources from under the Android javac).  Android before iOS
   within a platform.  `--build all` (the default) is for validation; `stale` rebuilds only cells
   whose stamp no longer matches, for iteration; `none` trusts the installed apps.
2. LANES, one per device, in parallel — nothing is building by then.  Each lane runs its queue
   through run.py, one scenario at a time, each capped by `--timeout` and killed with its whole
   process group when it overruns.  (Tier C, C7.7) An OS has one device or two (`--android`,
   `--ios`; by default the Lab devices `lab_devices.py` has up).  (WO-106) Its devices share one
   queue, in board order, and whichever is free takes the next cell: one cell, one device at a time —
   captures carry no serial, and one cell on two devices at once would write the same capture
   directory.  Different cells of one platform on the two devices write different ones.

The board is honest about what happened, which the hand-written sweep scripts were not: a refusal
exited 1 like a FAIL and printed the previous capture's `=> PASS` beside it.  Here a scenario the
platform does not run is `n/a` before anything is spawned; a run counts as PASS or FAIL only when it
wrote a fresh capture for every step in THIS sweep; anything else is ERROR with the run's last line
(a refusal, a dead device) or TIMEOUT.  Every result names the native snapshot, the plugin commit and
the device it ran on.

(Tier C, C7.0) The sweep keeps its own record beside the cell logs in `.claims/sweep-<stamp>/`:
`stdout.log`, every line printed here, and `board.json` — the builds, lanes and runs with their
epochs, and each run's step `end`, `timings` and `dialog_observer` copied out of its run.json.
Captures carry no sweep id and the next sweep overwrites them; board.json is what keeps a board
measurable after that (`summarize.py --board <stamp>`).

(WO-027) A loaded machine manufactures reds, and a load figure alone does not say what loaded it.  A run
that ends above LOAD_SUSPECT is followed by a `LOAD` line naming the processes using the most CPU at that
moment, kept in board.json as the run's `busy`; the board ends with a `load:` line, the peak, and when
any run ended above the line, how many and which processes were on top across them.

(WO-027, 2026-10-05) A host out of memory manufactures them too, and load does not show it.  Before anything is
built or run, and again after the builds, the sweep reads the host's memory and times a system-app launch on each
Android emulator (PREFLIGHT, below): it prints a MEMORY and a PROBE line, keeps both in board.json, and refuses to
start when macOS reports critical memory pressure or an emulator is slow to launch.  `--no-preflight` starts anyway.

(WO-027, 2026-10-07) macOS also slows an emulator on its own account: one whose window is hidden or covered runs in
the background, at a third of its pace on an idle host and starved on a busy one.  The preflight reads each Android
emulator's thread priorities and refuses while one is there (PRIORITY, below); a run that ends with its emulator
there is followed by a PRIORITY line, and the board ends with how many did.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

import devices as claims                     # (WO-027, sweep lock) main()'s `devices` is its serials
import labkit

SUITE = ["api-smoke", "api-config", "api-datastore", "api-geofences", "events", "events-geofence",
         "events-schedule", "hello-world", "permission-request", "permission-request-always",
         "watch-position",
         # (WO-029) the three bridges' Location timestamp; Cordova excused in writing (wont-fix)
         "location-timestamp",
         # (Tier C, C2) the iOS-only State attributes, typed, and WO-060's partial alert; Android excused in
         # writing.  The Android-only ones are typed by api-roundtrip-android's baselines (C3.2 retired api-state-android)
         "api-state-ios",
         # (Tier C, C3) every Config leaf set, read back and restored by reset(); one-OS keys excused in writing
         "api-roundtrip", "api-roundtrip-android", "api-roundtrip-ios",
         # (Tier C, flutter#1715) no Activity window touched off the main thread; the self-test proves the probe
         "decor-race-1715",
         # (Tier C, C4.1; WO-059) the two desiredAccuracy probes, null and "high": -1 and filter.policy 2 on all 8.
         # (Tier C, C4.5) With the rulings: every invalid value invalid.yaml rules with no `pending:` reads its ruling
         # on every cell; the one-OS values in the two companions, each excused in writing on the other OS
         "api-config-values", "api-config-values-android", "api-config-values-ios",
         # (Tier C, C4.1; WO-067) the Location family typed leaf by leaf; iOS's null floor is `number | null`
         "api-types",
         # (WO-039) the one-commit iOS reset's standing guard.  Its arm step reads false-red if
         # it straddles 00:00 (see the scenario's description) — don't start a sweep at 23:58.
         "ready-omitted-schedule",
         # (WO-066) a token-only setConfig survives a relaunch with reset:false; red on iOS before 4.7.1-master-b60a026
         "token-survives-launch",
         # (Tier C, C5.1) the 17 properties of the TypeScript types, member by member, on the three JavaScript bridges;
         # green on all six at its pilot (sweep 20260928-092341); Flutter excused in writing (its Dart surface is R's)
         "api-properties-typescript",
         # (Tier C, R; addendum A1) Flutter's enums through their Dart spellings, generated from docforge/db: green on
         # both Flutter cells on its first board, 20261005-074711, which was its condition for SUITE (Tier C's closure)
         "api-properties-dart",
         # (Tier C, C8.0; decision 27 B) requestPermission()'s no-argument form in the four cases permission-request
         # and permission-request-always do not reach: green on all eight on its first full board, 20261002-085423
         "permission-request-forms",
         # (Tier C, C5.2) a real geofence EXIT (a 500 m move) and DWELL, green on all eight on board 20260928-145837,
         # 22-31 s a cell.  In that board's order: the dwell was measured green behind the exit's displaced fused fix
         "events-geofence-exit", "events-geofence-dwell",
         # (Tier C, C5.3) Subscription.remove() beside a never-removed twin, and a rejected fix's onLocationFilter with
         # Flutter's row since R: each green on all eight on board 20261005-111238 (26 and 34 s a cell)
         "events-remove", "events-location-filter",
         # (Tier C, C5.1) removeListeners() silences every listener armed before it and leaves an active watch
         # running.  Opt-in while it read red on React Native (WO-096) and on Capacitor's watch row (WO-097): green
         # on all eight bridge pairs and on the native pair on board 20261010-144634, its condition for SUITE
         "events-remove-all"]
# (Tier C, C5.0) Every other scenario is on exactly one named list (the rest-of-Tier-C design, decision 1 A), so
# `--scenarios GUARDS` runs a set by its name and no scenario is left off every list (the selftest holds it).
#   GUARDS       the lifecycle guards: recreation, runtime reset, terminate, the queued HTTP flush
#   PERMISSIONS  the permission cases that are not SUITE's two permission-request scenarios
#   LEGACY       the unlisted legacy scenarios
#   OPTIN        the conformance scenarios kept out of SUITE: C4's opt-in board, which measures, and C5's until each
#                reads green (predicted red on a known defect, or awaiting its pilot) — not one of the design's three:
#                decision 1 A's rule, that no scenario is left off a list, needs a fourth.  (C5.1: named MEASURE in
#                C5.0; renamed once C5's asserting scenarios joined it.)
LISTS = {
    "GUARDS": ["recreate-bold", "recreate-bold-no-stop-on-terminate", "recreate-dark-mode", "recreate-fontscale",
               "recreate-surviving-engine", "recreate-twice", "recreate-watch", "runtime-reset", "runtime-reset-motion",
               "runtime-reset-slc", "terminate-finish", "launch-http-flush",
               # (Tier C, C6.0) Android headless (H2) and genesis case B2 (G1)
               "headless-android", "recreate-surviving-engine-no-stop-on-terminate",
               # (Tier C, C6.1) the headless causes (H5)
               "headless-android-causes",
               # (WO-114; react-native #2669) what a tracking app uploads after Android kills its process: no record
               # twice.  Green on the four Android cells on board 20261005-115027, the first on a FIX that carries the
               # fix (4.7.1-master-20bde3a); red before it, which the PRE-WO-114 arm keeps measurable
               "headless-android-kill"],
    "PERMISSIONS": ["permission-request-location", "permission-request-location-always", "permission-motion",
                    "permission-when-in-use",
                    # (Tier C, C8.0) Location Accuracy OFF and the permission cases (decision 1 A).  OFF puts the
                    # setting back on when its steps are done (run.restore_accuracy), wherever it runs in the queue.
                    # The no-argument forms joined SUITE after board 20261002-085423 (decision 27 B);
                    # permission-location-error and temporary-full-accuracy-android are predicted red (WO-095;
                    # decision 20's two defects)
                    "location-accuracy-off", "permission-request-motion-android",
                    "permission-request-motion-ios", "permission-location-error", "temporary-full-accuracy-android",
                    # (Tier C, C8.1) the two failure callbacks after a denial, counted and never judged (decision 21).
                    # Predicted red on the two Flutter cells, its watch step: Dart's watchPosition has no failure callback
                    "events-errors"],
    "LEGACY": ["scheduler-survives-launch", "significant-changes", "reset-no-args"],
    "OPTIN": ["api-args", "api-config-invalid", "api-config-invalid-android", "api-config-invalid-ios", "api-errors",
              "api-forbidden", "api-types-control",
              # (WO-143 stage 4) the native platform's enum surface, predicted red on the members whose value is not
              # the types' (DesiredAccuracy's Android priorities, AuthorizationStrategy's case); a measurement
              "api-properties-kotlin", "api-properties-swift",
              "connectivity-android",
              # (Tier C, C5.2) events-geofence-exit and -dwell joined SUITE after board 20260928-145837, and
              # events-location-filter after 20261005-111238, green on all eight once R gave Flutter its
              # onLocationFilter row (design.md §3); geofence-exit-far-android stays a measurement until F1
              "geofence-exit-far-android",
              # (WO-107) predicted red on the four bridges' masters, which build addGeofences' polygons on the calling
              # thread; joins SUITE once WO-107 is merged and it reads green on all eight
              "geofences-bulk-add",
              # (RN #2677) predicted red on slow-config wherever the main thread asks for the SDK while another thread
              # builds it: React Native's module until it builds TSConfig first (5.8.1, master), Flutter's first channel
              # call, Capacitor's load() behind BootReceiver; joins SUITE once it reads green.  Its two natural launches
              # assert nothing timing decides: a measurement
              "startup-config-wait-2677",
              # (Tier C, C5.3) what needed the rebuild.  events-remove joined SUITE after board 20261005-111238.
              # events-http joins after a green board (design.md §3): it is red on Capacitor and Cordova Android, a
              # successful authorization event with no `error`.  The rest stay here: two wait out a heartbeat timer,
              # one needs the host's hand on the device, and the logger and device-settings calls are measurements
              "events-http", "events-heartbeat-android", "events-heartbeat-ios",
              "events-android-host", "api-logger", "api-device-settings-android"],
}
# (WO-106) Runs in a row that end in ERROR before a lane stops taking cells.  Board 20261001-233522: a crash-looping
# emulator ERRORed every cell after it in 1 s, and on a shared queue those are cells the healthy device could run.
STOP_AFTER = 3
# (WO-027) A run that ends above this 1-minute load is followed by what was using the CPU.  The lab's own work ends
# its runs at 3-35 (boards 20261002-115024 and -130440); the board whose reds were starvation ran at 40-60
# (20261001-215439), and 20261003-124300 peaked at 575 with nothing kept to say what did it.
LOAD_SUSPECT = 40
BUSY_KEPT, BUSY_SAID, BUSY_NAMED = 8, 5, 6    # processes kept per such run; named on its LOAD line; on the board's
# (WO-027, 2026-10-05) PREFLIGHT.  Sweep 20261005-113336 was stopped after 56 cells: the Mac was out of memory (63 of
# 64 GB used, 28.7 GiB of it compressed, 6.2 GB of swap), and both Lab emulators crawled.  App launches took 10 to 41 s
# and five Android cells were lost, at a load of 30 to 43 with the CPU half idle.  What the sweep refuses on:
#   * macOS's own verdict, `kern.memorystatus_vm_pressure_level`, at critical (4).  Its warning level (2) is printed
#     and not refused: the lab ran normally at it the same afternoon, with 27.0 GiB compressed and no swapping.  No
#     figure of ours stands in for the kernel's: the compressed share of RAM read 45 % in the lost sweep and 42 % in
#     that healthy hour, and free memory 0.1 GB in both.  The level in the lost sweep itself was NOT READ.
#   * the symptom, MEASURED: a cold launch of the Settings app on each Android emulator.  0.21 to 0.28 s on a healthy
#     Lab emulator, 1.05 s on one under that warning; in the lost sweep a launch took 10 s and more.  Refused above
#     PROBE_SLOW_MS, or when it does not finish in PROBE_TIMEOUT_S.  It catches a slow emulator whatever slowed it.
# The Simulators are not probed: they ran at their usual pace through the lost sweep.
# (WO-027, 2026-10-07) PRIORITY.  macOS puts an app to rest when its window is hidden, minimized or covered (App Nap),
# and an emulator with a window is such an app: every thread of its process drops to priority 4, the background band.
# MEASURED 2026-10-07 on an idle host (load 4):
#   * Lab1's window covered: its 127 threads all at 4; it launched a lab app in 4.4 to 5.2 s on 21 runs in a row
#     (five minutes of use, no recovery) and Settings in 0.65 to 0.8 s.  Lab2's threads were at 31 and 46, and it
#     launched the same app in 1.7 s every time and Settings in 0.2 s.
#   * Lab1's window brought to the front (Chris): its threads at 31 within the minute, the app launched in 1.9 to
#     2.4 s and Settings in 0.28 s, as on Lab2.
# What it costs on a busy host is INFERRED from the sweeps' own records, in which no priority was read.  Nine of the
# 22 sweeps from 2026-10-02 to 2026-10-06 began with an emulator slow to launch (`am start -W` in 4.9 to 31 s, where
# 1.0 to 3.9 s is healthy) and five lost cells to it (20261005-074711, -113336, 20261006-054130, -062812, -130933),
# at host loads of 26 to 70 at which the other emulator ran at its usual pace.  The slowness ended after 4 to 7
# minutes, from one cell to the next, which a window coming into view explains and nothing else on record does.
# The cure is at the emulator's start: `./labkit up` opts it out of App Nap (lab_devices.keep_awake), after which
# both Lab emulators stayed at normal priority through seven minutes with their windows hidden.  An emulator started
# before that, or by another hand, is not covered.
# So the preflight reads each emulator's threads (`ps -M`) and refuses to start while most of them are in the
# background band, naming the cure.  A window can be covered later too (20261006-062812: emulator-5582 lost two cells
# after nine healthy ones), so each Android run reads them again as it ends: a PRIORITY line follows a run whose
# emulator was there, board.json marks it (`background`), and the board's last lines count them.  The Settings probe
# does not stand in for this: it read 0.6 to 0.9 s on a resting emulator, under its 3 s line.
BACKGROUND_PRI = 4
PRESSURE = {1: "normal", 2: "warning", 4: "critical"}
PROBE_SLOW_MS, PROBE_TIMEOUT_S = 3000, 30
PROBE = ("am force-stop com.android.settings; am start -W -n com.android.settings/.Settings; "
         "am force-stop com.android.settings; input keyevent 3")
RUN = os.path.join(labkit.HARNESS, "run.py")
REPO = os.path.dirname(labkit.LABS)
_print = threading.Lock()
_dir = None          # (Tier C, C7.0) this sweep's .claims directory, once main() has made it
_board = {}          # (Tier C, C7.0) board.json, written only by keep()
_lock = None         # (WO-027, sweep lock) the sweep lock's fd, once main() holds it


def say(line):
    with _print:
        print(line, flush=True)
        if _dir:
            try:
                with open(os.path.join(_dir, "stdout.log"), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except (OSError, ValueError):
                pass                 # the terminal still has it


def keep(section=None, entry=None):
    """(Tier C, C7.0) Append `entry` to board.json's `section`, then re-write the whole file: under
    the print lock, and atomically (a temp file, then os.replace), so a sweep killed at any point
    leaves every build and run that finished and a reader never sees half a file.  Instrumentation
    only: a failed write is printed, never fatal to the lane.  A value JSON has no type for (a YAML
    date in an n/a reason) is written as its text, so one entry can never stop every later write."""
    with _print:
        if section:
            _board[section].append(entry)
        path = os.path.join(_dir, "board.json")
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(_board, f, indent=1, default=str)
            os.replace(path + ".tmp", path)
        except (OSError, TypeError, ValueError) as e:
            print("[board] board.json not written: %s" % e, file=sys.stderr)


def capped(argv, timeout, log_path):
    """Run argv in its own process group; past `timeout` the WHOLE group is killed (run.py's
    logcat stream and log tap with it).  Returns the exit code, or "timeout".  (WO-027, sweep lock) The
    child holds the sweep lock too: in its own session it outlives a sweep that is ^C'd or killed, and
    the next sweep must wait for it."""
    with open(log_path, "w") as log:
        p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             cwd=REPO, start_new_session=True, pass_fds=() if _lock is None else (_lock,))
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            return "timeout"


def busiest(text=None, top=BUSY_KEPT):
    """(WO-027) The processes using the most CPU now, busiest first, as [{pid, pcpu, etime, comm}]: `ps -r`'s own
    order.  `comm` is the executable's path and never its arguments: another tool's command line can carry a token,
    and this goes into board.json and stdout.log.  Instrumentation only: a ps that fails or stalls gives [], never an
    error to the lane.  `text` is ps's output, for the selftest."""
    if text is None:
        try:
            text = subprocess.run(["ps", "-A", "-r", "-o", "pid=", "-o", "pcpu=", "-o", "etime=", "-o", "comm="],
                                  capture_output=True, text=True, timeout=5, env=dict(os.environ, LC_ALL="C")).stdout
        except (OSError, subprocess.SubprocessError):
            return []
    out = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            out.append({"pid": int(parts[0]), "pcpu": float(parts[1].replace(",", ".")), "etime": parts[2],
                        "comm": parts[3].strip()})
        except ValueError:
            continue
        if len(out) == top:
            break
    return out


def busy_name(comm):
    """(WO-027) A process's name on a line: its executable's file name.  An app inside a Simulator gets that device's
    first 8 characters before it, as the RESULT lines name the lab's own: another session's simulator shows as one."""
    sim = re.search(r"/CoreSimulator/Devices/([0-9A-Fa-f]{8})", comm)
    base = os.path.basename(comm.rstrip("/")) or comm
    return "%s/%s" % (sim.group(1), base) if sim else base


def sample_load(loadavg=os.getloadavg, top=busiest):
    """(WO-027) The host's 1-minute load as a run ends, and the busiest processes when it is above LOAD_SUSPECT
    (None at or under it: a quiet run asks ps nothing).  A sampler that raises gives []: the run's result is kept."""
    load = loadavg()[0]
    if load <= LOAD_SUSPECT:
        return load, None
    try:
        return load, top()
    except Exception:
        return load, []


def load_note(os_name, device, load, busy):
    """(WO-027) The LOAD line under a RESULT that ended above LOAD_SUSPECT: the load, and the busiest processes."""
    return "LOAD    %-7s %-13s load=%.1f busiest: %s" % (
        os_name, device, load,
        ", ".join("%s %.0f%%" % (busy_name(b["comm"]), b["pcpu"]) for b in busy[:BUSY_SAID]) or "ps gave nothing")


def load_lines(runs):
    """(WO-027) The board's last lines, from board.json's runs.  Always the highest load a run ended at.  When any run
    ended above LOAD_SUSPECT: how many, the peak's run and time, and the processes on top across those runs, by the
    CPU they held in total, each with the runs it was seen in and its highest figure."""
    ran = [r for r in runs if isinstance(r.get("load"), (int, float))]
    if not ran:
        return []
    peak = max(ran, key=lambda r: r["load"])
    over = [r for r in ran if r["load"] > LOAD_SUSPECT]
    if not over:
        return ["load: at most %.1f" % peak["load"]]
    lines = ["load: %d of %d runs ended above %d; peak %.1f at %s (%s %s %s)" % (
        len(over), len(ran), LOAD_SUSPECT, peak["load"],
        time.strftime("%H:%M:%S", time.localtime(peak["end"])) if isinstance(peak.get("end"), (int, float)) else "?",
        peak.get("os"), peak.get("plat"), peak.get("scenario"))]
    seen = {}                                 # name -> [total pcpu, runs it was seen in, highest pcpu]
    for r in over:
        names = set()
        for b in r.get("busy") or []:
            name = busy_name(b["comm"])
            row = seen.setdefault(name, [0.0, 0, 0.0])
            row[0] += b["pcpu"]
            row[2] = max(row[2], b["pcpu"])
            names.add(name)
        for name in names:
            seen[name][1] += 1
    top = sorted(seen.items(), key=lambda kv: (-kv[1][0], kv[0]))[:BUSY_NAMED]
    lines.append("  busiest then: %s" % (", ".join("%s in %d (to %.0f%%)" % (name, n, high)
                                                    for name, (_, n, high) in top) or "nothing sampled"))
    return lines


def _run_text(argv, timeout):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=dict(os.environ, LC_ALL="C")).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def host_memory(sysctl=None, vm_stat=None):
    """(WO-027, PREFLIGHT) The host's memory now: {pressure, compressed_gb, ram_gb, swap_used_mb}, each None when it
    could not be read (another OS, a command that failed): a reading that is missing refuses nothing.  `pressure` is
    macOS's level, 1 normal, 2 warning, 4 critical.  `sysctl` and `vm_stat` are the commands' output, for the
    selftest."""
    if sysctl is None:
        sysctl = _run_text(["sysctl", "kern.memorystatus_vm_pressure_level", "hw.memsize", "vm.swapusage"], 5)
    if vm_stat is None:
        vm_stat = _run_text(["vm_stat"], 5)
    number = lambda pattern, text: (lambda m: float(m.group(1)) if m else None)(re.search(pattern, text))
    level, ram = number(r"memorystatus_vm_pressure_level:\s*(\d+)", sysctl), number(r"hw\.memsize:\s*(\d+)", sysctl)
    page, held = number(r"page size of (\d+) bytes", vm_stat), number(r"Pages occupied by compressor:\s+(\d+)", vm_stat)
    swap = number(r"vm\.swapusage:.*?used = ([\d.]+)M", sysctl)
    return {"pressure": int(level) if level in PRESSURE else None,
            "compressed_gb": None if page is None or held is None else round(page * held / 2 ** 30, 1),
            "ram_gb": None if ram is None else round(ram / 2 ** 30, 1),
            "swap_used_mb": None if swap is None else round(swap)}


def memory_line(mem):
    """(WO-027, PREFLIGHT) The MEMORY line: macOS's verdict, and the figures beside it."""
    figures = []
    if mem.get("compressed_gb") is not None and mem.get("ram_gb"):
        figures.append("%.1f of %.0f GiB compressed (%.0f%%)" % (mem["compressed_gb"], mem["ram_gb"],
                                                                100 * mem["compressed_gb"] / mem["ram_gb"]))
    if mem.get("swap_used_mb") is not None:
        figures.append("swap %d MB used" % mem["swap_used_mb"])
    return "MEMORY  pressure %s%s" % (PRESSURE.get(mem.get("pressure"), "not read"),
                                     ", " + ", ".join(figures) if figures else "")


def launch_probe(serial, shell=None):
    """(WO-027, PREFLIGHT) How long this emulator takes to cold-launch the Settings app, in ms (ActivityManager's own
    TotalTime), or None when the launch did not report one within PROBE_TIMEOUT_S.  Settings is stopped before and
    after, and the device left on its home screen.  `shell` is the command's output, for the selftest."""
    if shell is None:
        shell = _run_text(["adb", "-s", serial, "shell", PROBE], PROBE_TIMEOUT_S)
    m = re.search(r"TotalTime:\s*(\d+)", shell)
    return int(m.group(1)) if m else None


def emulator_pid(serial, listing=None):
    """(WO-027, PRIORITY) The pid of the emulator behind `emulator-<port>`: the qemu process started with `-port
    <port>`, as lab_devices.py starts the Lab emulators.  None for any other serial, or when no such process runs."""
    m = re.match(r"emulator-(\d+)$", serial)
    if not m:
        return None
    if listing is None:
        listing = _run_text(["ps", "-axo", "pid=,command="], 10)
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if "qemu-system" in command and re.search(r"\s-port\s+%s(\s|$)" % m.group(1), command):
            return int(pid)
    return None


def emulator_priority(serial, listing=None, threads=None):
    """(WO-027, PRIORITY) How macOS schedules an emulator's process: {pid, threads, background}, the number of its
    threads and how many of them are at BACKGROUND_PRI or below.  None when the process or its threads were not
    read.  `listing` and `threads` are `ps -axo pid=,command=` and `ps -M -p <pid>`, for the selftest."""
    pid = emulator_pid(serial, listing)
    if pid is None:
        return None
    if threads is None:
        threads = _run_text(["ps", "-M", "-p", str(pid)], 10)
    # One line a thread: ... %CPU STAT PRI STIME UTIME [COMMAND], the priority with its policy's letter (31T).
    pris = re.findall(r"(?m)\s[A-Z][A-Za-z+<>]*\s+(\d+)[A-Z]?\s+\d+:\d\d\.\d\d\s+\d+:\d\d\.\d\d", threads)
    if not pris:
        return None
    return {"pid": pid, "threads": len(pris), "background": sum(1 for p in pris if int(p) <= BACKGROUND_PRI)}


def resting(reading):
    """(WO-027, PRIORITY) macOS has the emulator in the background: most of its threads are in that band.  Measured:
    all 127 of a resting emulator's, and 0 to 2 of a working one's."""
    return bool(reading) and reading["background"] * 2 > reading["threads"]


def priority_line(serial, reading):
    """(WO-027, PRIORITY) The line for an emulator macOS has in the background."""
    return "PRIORITY android %-13s in the background: %d of %d threads at priority %d or below" % (
        serial, reading["background"], reading["threads"], BACKGROUND_PRI)


def priority_lines(runs):
    """(WO-027, PRIORITY) The board's line on it, from board.json's runs: how many ended with their emulator in the
    background, and on which emulator."""
    by = {}
    for r in runs:
        if r.get("background"):
            by[r.get("serial")] = by.get(r.get("serial"), 0) + 1
    n = sum(by.values())
    return ["priority: %d run%s ended with the emulator in the background (%s): its window was hidden or covered" % (
        n, "" if n == 1 else "s", ", ".join("%s %d" % kv for kv in sorted(by.items())))] if by else []


def preflight(serials, memory=host_memory, probe=launch_probe, priority=emulator_priority):
    """(WO-027, PREFLIGHT) What the host and the Android emulators say before a board runs on them: the reading
    ({memory, probes, priority}), the lines to print, and why the sweep should not start (None when it may)."""
    mem = memory()
    rest = {s: priority(s) for s in serials}      # (WO-027, PRIORITY) before the probe, which is read against it
    probes = {s: probe(s) for s in serials}
    lines = [memory_line(mem)] + ["PROBE   android %-13s %s" % (
        s, "Settings launched in %d ms" % ms if ms is not None else "Settings did not launch in %d s" % PROBE_TIMEOUT_S)
        for s, ms in probes.items()] + [priority_line(s, r) for s, r in rest.items() if resting(r)]
    why = []
    if mem.get("pressure") == 4:
        why.append("macOS reports critical memory pressure")
    for s, r in rest.items():
        if resting(r):
            why.append("macOS runs %s in the background, where it launches apps at a third of its pace and starves "
                       "on a busy host: its window is hidden, minimized or covered.  Bring it into view, or restart "
                       "it (`./labkit down`, `./labkit up`): `up` opts the emulator out of that" % s)
    for s, ms in probes.items():
        if ms is None:
            why.append("%s did not launch Settings in %d s" % (s, PROBE_TIMEOUT_S))
        elif ms > PROBE_SLOW_MS:
            why.append("%s took %.1f s to launch Settings (a healthy Lab emulator takes 0.2 to 0.3 s)" % (s, ms / 1000))
    return {"memory": mem, "probes": probes, "priority": rest}, lines, "; ".join(why) or None


def memory_holders(text=None, top=6):
    """(WO-027, PREFLIGHT) The processes holding the most memory, for a refusal: `top`'s own names and figures.
    Said only when the sweep refuses; a top that fails gives nothing."""
    if text is None:
        text = _run_text(["top", "-l", "1", "-o", "mem", "-n", str(top), "-stats", "command,mem"], 15)
    rows = [l.rsplit(None, 1) for l in text.splitlines()]
    rows = [(r[0].strip(), r[1]) for r in rows if len(r) == 2 and re.match(r"^\d+[KMG][+-]?$", r[1])]
    return ", ".join("%s %s" % (name, mem.rstrip("+-")) for name, mem in rows[:top])


def refusal(why, when, holders=memory_holders):
    """(WO-027, PREFLIGHT) The sweep's last words when it will not start, or will not go on past its builds."""
    held = holders()
    return ("sweep not started%s: %s.\n%sFree memory (idle Gradle daemons: `pkill -f GradleDaemon`; an IDE or a "
            "browser you are not using), restart the lab devices (`./labkit down`, then `./labkit up`), and start "
            "again; or pass --no-preflight to run on the host as it is."
            % (when, why, "Holding the most memory: %s.\n" % held if held else ""))


def memory_lines(runs):
    """(WO-027, PREFLIGHT) The board's line on memory, from board.json's runs: the highest pressure a run ended at,
    and how many ended above normal."""
    seen = [r["pressure"] for r in runs if r.get("pressure") in PRESSURE]
    if not seen:
        return []
    above = sum(1 for p in seen if p > 1)
    return ["memory: pressure at most %s%s" % (PRESSURE[max(seen)],
                                               "; %d of %d runs ended above normal" % (above, len(seen)) if above else "")]


def last_line(path):
    try:
        lines = [l.strip() for l in open(path, errors="replace") if l.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


def outcome(plat, os_name, scen_name, variant, started, rc, log_path, serial=None):
    """What this run actually produced — never the previous capture's verdict.  (Tier C, C7.7) Nor
    another process's: run.py's exit 2 means THIS run could not be judged, whatever captures are on
    disk, and a capture whose run.json names another serial (another sweep, or a hand run of the same
    cell, fresh within this run's window) is not this run's."""
    steps = labkit.scenario(scen_name)["steps"]
    if rc == 2:
        return {"status": "ERROR", "detail": last_line(log_path)[:160], "steps": len(steps)}
    verdicts, reds, ends, meta = [], [], [], {}
    for st in steps:
        d = labkit.capture_dir(plat, scen_name, variant, st["name"], os_name)
        rj, sm = os.path.join(d, "run.json"), os.path.join(d, "summary.txt")
        if not (os.path.exists(rj) and os.path.getmtime(rj) >= started and os.path.exists(sm)
                and os.path.getmtime(sm) >= started):
            status = "TIMEOUT" if rc == "timeout" else "ERROR"
            return {"status": status, "detail": last_line(log_path)[:160], "steps": len(steps)}
        # (Tier C, C7.1) Only a step already checked fresh above is read: reading every step's run.json
        # while checking the first raised FileNotFoundError for a later step never captured, which
        # killed the whole OS lane (found by C7.0's seam test).
        meta = json.load(open(rj))
        if serial is not None and meta.get("serial") not in (None, serial):
            return {"status": "ERROR", "steps": len(steps),
                    "detail": "%s: capture written by %s, not %s" % (st["name"], meta["serial"], serial)}
        text = open(sm).read()
        verdicts.append("=> PASS" in text)
        reds += ["%s: %s" % (st["name"], " ".join(l.split()[:2]))
                 for l in text.splitlines() if "MISMATCH" in l]
        ends.append((meta.get("end") or {}).get("ended", "?"))
    return {"status": "PASS" if all(verdicts) else "FAIL", "reds": reds,
            "native": meta.get("native"),
            # (WO-143) `none`: run.json records a null plugin for a platform that has none (run.plugin_path).
            # `?` stays what it was, a capture that does not say.
            "plugin": "none" if "plugin" in meta and meta["plugin"] is None else
                      "%s%s" % ((meta.get("plugin_source") or {}).get("commit", "?")[:7],
                                "+dirty" if (meta.get("plugin_source") or {}).get("dirty") == "yes" else ""),
            "device": (meta.get("device") or {}).get("avd") or (meta.get("device") or {}).get("model"),
            "end": ends}


class Queue:
    """(WO-106) One OS's cells in board order, shared by its devices: whichever is free takes the next.  A cell
    is taken once, so it never runs on two devices at once.  The last lane to leave is handed what no lane took."""

    def __init__(self, cells, lanes):
        self._cells, self._lanes, self._lock = list(cells), lanes, threading.Lock()

    def take(self):
        with self._lock:
            return self._cells.pop(0) if self._cells else None

    def leave(self):
        with self._lock:
            self._lanes -= 1
            if self._lanes:
                return []
            rest, self._cells = self._cells, []
            return rest


def drain(queue, run_cell, fail, uninstall):
    """(WO-106) One device's lane over its OS's queue, until the queue is empty or STOP_AFTER runs in a row end
    in ERROR.  `run_cell(plat, scen)` returns (result, ran): `ran` is False for an n/a cell or a failed build,
    which neither count toward the row nor break it.  `fail(plat, scen, detail, took)` records an ERROR no run
    produced; `took` is whether this device had taken the cell.  `uninstall(plat)` runs when the next cell is
    another platform's, and for the last one at the end.  Returns why the lane stopped early, or None."""
    last, row, cell, why = None, 0, None, None
    try:
        while True:
            cell = queue.take()
            if cell is None:
                break
            if last not in (None, cell[0]):
                uninstall(last)
            last = cell[0]
            res, ran = run_cell(*cell)
            cell = None
            if ran:
                row = row + 1 if res["status"] == "ERROR" else 0
                if row == STOP_AFTER:
                    why = "stopped: %d consecutive errors: %s" % (row, res.get("detail", ""))
                    break
        if last is not None and why is None:
            uninstall(last)
    except BaseException as e:
        # (Tier C, C7.7) A thread drops its SystemExit without a word — a device call past its timeout, as
        # the 60 s uninstall on a wedged emulator — and the sweep then exited 0 with this device's cells
        # blank.  (WO-106) Only the cell it had taken is this lane's; the rest stay for the other device.
        why = "lane died: %s" % (e if isinstance(e, SystemExit) else "%s: %s" % (type(e).__name__, e))
        if cell is not None:
            fail(*cell, why, True)
    finally:
        for plat, scen in queue.leave():
            fail(plat, scen, "no device left", False)
    return why


def lane(os_name, serial, queue, args, results, log_dir):
    t0 = time.time()
    # (Tier C, C7.0) main() made this lane's span before either lane started: neither adds a key
    # while the other is writing board.json.  (Tier C, C7.7) The OS's span is its devices' first
    # start and last end, set under the print lock: two lanes of one OS may end at once.
    span = _board["lanes"][os_name]
    dev = span["devices"][serial]
    with _print:
        dev["start"] = round(t0, 3)
        span["start"] = min(d["start"] for d in span["devices"].values() if d["start"] is not None)

    def run_cell(plat, scen):
        key = (plat, os_name, scen)
        if results.get((plat, os_name, "__build__")) == "FAILED":
            results[key] = {"status": "ERROR", "detail": "build failed"}
            keep("runs", {"plat": plat, "os": os_name, "serial": serial, "scenario": scen, **results[key]})
            return results[key], False
        try:
            view = labkit.platform(plat, os_name)
            # (WO-192, D1) A view that does not build the variant's channel: n/a, as an OS a scenario excuses is.
            reason = (labkit.channel_refusal(view, labkit.variant(args.variant))
                      or labkit.applicability(labkit.scenario(scen), view))
        except SystemExit as e:
            reason = str(e)
        if reason:
            results[key] = {"status": "n/a", "detail": reason}
            keep("runs", {"plat": plat, "os": os_name, "serial": serial, "scenario": scen, **results[key]})
            return results[key], False
        with _print:                     # (WO-106) board.json: the platforms this device actually ran
            if plat not in dev["platforms"]:
                dev["platforms"].append(plat)
        started = time.time()
        log = os.path.join(log_dir, "%s-%s-%s.log" % (plat, os_name, scen))
        rc = capped([sys.executable, "-u", RUN, "--platform", plat, "--scenario", scen, "--os", os_name,
                     "--serial", serial, "--variant", args.variant, "--nonfinal", args.nonfinal,
                     "--post-settle", str(args.post_settle)], args.timeout, log)
        res = outcome(plat, os_name, scen, args.variant, started, rc, log, serial)
        ended = time.time()
        res["secs"] = round(ended - started)
        results[key] = res
        load, busy = sample_load()
        mem = host_memory()                  # (WO-027, PREFLIGHT) as the run ends, for board.json and the last line
        rest = emulator_priority(serial) if os_name == "android" else None     # (WO-027, PRIORITY) and this
        # (Tier C, C7.7) The device: an emulator serial, or a Simulator UDID's first 8 characters
        say("RESULT %-7s %-13s %-13s %-26s %-7s %4ss  %s" % (
            os_name, serial if os_name == "android" else serial[:8], plat, scen, res["status"], res["secs"],
            "native=%s plugin=%s device=%s end=%s load=%.1f" % (res["native"], res["plugin"], res["device"],
                                                                 ",".join(res["end"]), load)
            if res["status"] in ("PASS", "FAIL") else res.get("detail", "")))
        if busy is not None:             # (WO-027) whatever the status: an ERROR's RESULT line carries no load
            say(load_note(os_name, serial if os_name == "android" else serial[:8], load, busy))
        if resting(rest):                # (WO-027, PRIORITY) read a red against this before anything else
            say(priority_line(serial, rest))
        # (Tier C, C7.0) Each step's `end`, `timings` and `dialog_observer`, copied out of the
        # run.json THIS run wrote (an older one is a previous sweep's), before the next sweep
        # overwrites it.  The run's `end` in board.json is its exit epoch; the per-step `ended`
        # the RESULT line prints is each step's end.ended, and `declared_steps` how many the
        # scenario has, captured or not.
        steps, declared = [], labkit.scenario(scen)["steps"]
        for st in declared:
            rj = os.path.join(labkit.capture_dir(plat, scen, args.variant, st["name"], os_name), "run.json")
            try:
                if os.path.getmtime(rj) >= started:
                    meta = json.load(open(rj))
                    if meta.get("serial") in (None, serial):     # (Tier C, C7.7) as outcome() reads them
                        steps.append({"step": st["name"], "end": meta.get("end"),
                                      "timings": meta.get("timings"),
                                      "dialog_observer": meta.get("dialog_observer")})
            except (OSError, ValueError):
                pass             # none written, or cut short by the kill
        keep("runs", {"plat": plat, "os": os_name, "serial": serial, "scenario": scen, **res,
                      "start": round(started, 3), "end": round(ended, 3), "load": round(load, 2),
                      **({} if busy is None else {"busy": busy}),
                      **{k: mem[k] for k in ("pressure", "compressed_gb") if mem.get(k) is not None},
                      **({"background": [rest["background"], rest["threads"]]} if resting(rest) else {}),
                      "declared_steps": len(declared), "steps": steps})
        return res, True

    def fail(plat, scen, detail, took):
        results[(plat, os_name, scen)] = {"status": "ERROR", "detail": detail}
        keep("runs", {"plat": plat, "os": os_name, "serial": serial if took else None, "scenario": scen,
                      "status": "ERROR", "detail": detail})

    def uninstall(plat):
        # The emulator's /data is small (2026-09-23: 95 % full, every install failing): never leave
        # four lab apps installed at once.
        if os_name == "android" and args.uninstall:
            labkit.adb(serial, "uninstall", labkit.platform(plat)["package"], timeout=60)

    why = drain(queue, run_cell, fail, uninstall)
    if why:
        say("LANE    %-7s %-13s %s" % (os_name, serial if os_name == "android" else serial[:8], why))
    results[(serial, "__lane__")] = (t0, time.time())
    with _print:
        dev["end"] = round(time.time(), 3)
        if all(d["end"] is not None for d in span["devices"].values()):
            span["end"] = max(d["end"] for d in span["devices"].values())
    keep()


def build_pass(cells, args, results, log_dir):
    t0 = time.time()
    import run as runmod
    var = labkit.variant(args.variant)
    for plat, os_name in cells:
        view = labkit.platform(plat, os_name)
        if args.build == "none":
            continue
        # (WO-192, D1) Nothing to build for a view that does not build the variant's channel: its cells are n/a.
        why = labkit.channel_refusal(view, var)
        if why:
            say("BUILD   %-7s %-13s n/a: %s" % (os_name, plat, why))
            keep("builds", {"plat": plat, "os": os_name, "status": "n/a", "start": None, "end": None})
            continue
        if args.build == "stale":
            path = runmod.stamp_path(view)
            if os.path.exists(path) and open(path).read().strip() == runmod.source_stamp(view, var):
                say("BUILD   %-7s %-13s current, skipped" % (os_name, plat))
                keep("builds", {"plat": plat, "os": os_name, "status": "current", "start": None, "end": None})
                continue
        started = time.time()
        log = os.path.join(log_dir, "build-%s-%s.log" % (plat, os_name))
        rc = capped([sys.executable, "-u", RUN, "--build-only", "--platform", plat, "--os", os_name,
                     "--variant", args.variant], args.build_timeout, log)
        ok = rc == 0
        results[(plat, os_name, "__build__")] = "ok" if ok else "FAILED"
        say("BUILD   %-7s %-13s %-7s %4ss  %s" % (os_name, plat, "ok" if ok else "FAILED",
                                                round(time.time() - started), "" if ok else last_line(log)[:140]))
        keep("builds", {"plat": plat, "os": os_name, "status": "ok" if ok else "FAILED",
                        "start": round(started, 3), "end": round(time.time(), 3)})
    results[("__build_secs__",)] = round(time.time() - t0)


def board(results, cells, scenarios, t_total, devices):
    # (Tier C, C7.0) say(), not print(): the board and its wall line land in stdout.log too
    # (WO-143) Each manifest's `short`: three letters, so `-ios` fits the column after it.
    cols = ["%s%s" % (labkit.board_name(p), "" if o == "android" else "-ios") for p, o in cells]
    say("\n%-26s %s" % ("scenario", " ".join("%-8s" % c for c in cols)))
    counts = {}
    for scen in scenarios:
        row = []
        for p, o in cells:
            st = results.get((p, o, scen), {}).get("status", "-")
            counts[st] = counts.get(st, 0) + 1
            row.append("%-8s" % {"n/a": "·", "TIMEOUT": "TIME"}.get(st, st))
        say("%-26s %s" % (scen, " ".join(row)))
    say("\n" + ", ".join("%s %d" % kv for kv in sorted(counts.items())))
    for (p, o, scen), res in sorted((k, v) for k, v in results.items() if len(k) == 3 and isinstance(v, dict)):
        if res.get("status") == "FAIL":
            say("  RED    %-7s %-13s %-26s %s" % (o, p, scen, "; ".join(res["reds"])[:150]))
        elif res.get("status") in ("ERROR", "TIMEOUT"):
            say("  %-6s %-7s %-13s %-26s %s" % (res["status"], o, p, scen, res.get("detail", "")[:150]))
    # (Tier C, C7.7) An OS's lane is its span, first device start to last device end; with two
    # devices, each device's lane follows it.
    lanes = []
    for o in ("android", "ios"):
        spans = [(s, results[(s, "__lane__")]) for s in devices.get(o, []) if (s, "__lane__") in results]
        lanes.append("%s lane %ss%s" % (
            o, round(max(e for _, (_, e) in spans) - min(b for _, (b, _) in spans)) if spans else 0,
            " (%s)" % ", ".join("%s %ss" % (s if o == "android" else s[:8], round(e - b)) for s, (b, e) in spans)
            if len(devices.get(o, [])) > 1 else ""))
    say("\nwall %ds: builds %ss, %s" % (t_total, results.get(("__build_secs__",), 0), ", ".join(lanes)))
    for line in load_lines(_board.get("runs") or []):     # (WO-027) read a red against this before the scenario
        say(line)
    for line in memory_lines(_board.get("runs") or []):   # (WO-027, PREFLIGHT) and against this
        say(line)
    for line in priority_lines(_board.get("runs") or []):  # (WO-027, PRIORITY) and this
        say(line)
    return not any(r.get("status") in ("FAIL", "ERROR", "TIMEOUT") for k, r in results.items()
                   if len(k) == 3 and isinstance(r, dict))


def scenario_list(text):
    """(Tier C, C5.0) `--scenarios`: a comma list of scenario names and list names (`suite`, or one of LISTS), expanded
    in order, each scenario once.  `suite` alone stays the default."""
    out = []
    for name in (s for s in text.split(",") if s):
        for s in SUITE if name == "suite" else LISTS.get(name, [name]):
            if s not in out:
                out.append(s)
    return out


def refuse_unusable(os_name, found):
    """An OS's device list, named or resolved: emulators only on Android, each once, one or two."""
    phone = next((s for s in found if os_name == "android" and not s.startswith("emulator-")), None)
    if phone:
        raise SystemExit("--android %s is not an emulator: the lab never installs to a phone" % phone)
    if len(set(found)) < len(found):
        raise SystemExit("--%s %s names a device twice" % (os_name, ",".join(found)))
    if len(found) not in (1, 2):
        raise SystemExit("--%s %s: %d devices, and a sweep runs on 1 or 2"
                         % (os_name, ",".join(found), len(found)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # (WO-143) Every platform that has a lab, in board order: the manifests' `column` (labkit.board_order).
    known = labkit.board_order()
    ap.add_argument("--platforms", default=",".join(known))
    ap.add_argument("--os", default="android,ios")
    ap.add_argument("--scenarios", default="suite",
                    help="comma list of scenarios and list names: `suite` (the WO-027 scenarios), %s" % ", ".join(LISTS))
    ap.add_argument("--variant", default="FIX")
    ap.add_argument("--android", help="EMULATOR serials, comma-separated — never a phone "
                                      "(default: the Lab AVDs that are up, lab_devices.py)")
    ap.add_argument("--ios", help="booted Simulator UDIDs, comma-separated (default: the Lab simulators that are up)")
    # (Tier C, C7.7) The one-vs-two-device A/B without typing serials and UDIDs.
    ap.add_argument("--devices", type=int, choices=[1, 2],
                    help="use the first N Lab devices that are up on each OS, Lab1 first (default: all of them)")
    ap.add_argument("--build", choices=["all", "stale", "none"], default="all")
    # (Tier C, C7.3) Passed to every run: the non-final-step A/B arms (run.py --help).
    ap.add_argument("--nonfinal", choices=["hold", "end"], default=labkit.NONFINAL)
    ap.add_argument("--post-settle", type=int, choices=[0, 3], default=labkit.POST_SETTLE)
    ap.add_argument("--timeout", type=int, default=300, help="seconds per run before its group is killed")
    ap.add_argument("--build-timeout", type=int, default=900)
    ap.add_argument("--no-uninstall", dest="uninstall", action="store_false")
    ap.add_argument("--no-preflight", dest="preflight", action="store_false",
                    help="start even when the host's memory or an emulator's launch time says not to (both are "
                         "still read and printed)")
    args = ap.parse_args()

    platforms = [p for p in args.platforms.split(",") if p]
    oses = [o for o in args.os.split(",") if o]
    scenarios = scenario_list(args.scenarios)
    for s in scenarios:
        labkit.scenario(s)                    # an unknown name fails now, not twenty minutes in
    unknown = [p for p in platforms if p not in known]
    if unknown:
        raise SystemExit("unknown platform(s) %s: a sweep deals only %s" % (", ".join(unknown), ", ".join(known)))
    # (WO-027, sweep lock) And the OS and the variant, before the lock: a mistyped OS ran no cell and exited 0,
    # and a bad variant died in the build pass.
    if not set(oses) <= {"android", "ios"}:
        raise SystemExit("--os %s: a sweep runs android and ios only" % args.os)
    # (WO-143) The same exit 0 with nothing run: a platform asked for on an OS it has no host on.
    if not labkit.lab_cells(platforms, oses):
        raise SystemExit("--platforms %s --os %s: no cell — %s no lab host there (its lab.yaml has no such half)"
                         % (args.platforms, args.os, "it has" if len(platforms) == 1 else "they have"))
    var = labkit.variant(args.variant)
    if var.get("retired"):
        raise SystemExit("variant %s is retired: %s" % (var["name"], var["retired"]))
    if args.devices and (args.android or args.ios):
        raise SystemExit("--devices picks from the Lab devices that are up; name devices with --android/--ios "
                         "instead, not both")
    named = {o: [s for s in (getattr(args, o) or "").split(",") if s] for o in ("android", "ios") if o in oses}
    for os_name, found in named.items():
        if found:
            refuse_unusable(os_name, found)

    # (WO-027, sweep lock) One sweep per machine (devices.lock_sweep), taken once the arguments are checked,
    # so --help and a bad one never reach it, and before anything asks a device, builds or writes the
    # sweep's record.  Nothing here releases it: the kernel does, once this process and every build and
    # run it started have exited.  After a ^C in join() below the lanes run on, and so does the lock.
    global _lock
    _lock = claims.lock_sweep(" ".join(sys.argv))
    devices = {}
    for os_name, found in named.items():
        if not found:
            import lab_devices
            found = lab_devices.resolve(os_name)
            if args.devices and len(found) < args.devices:
                raise SystemExit("--devices %d: %d Lab device(s) up for %s — run `./labkit up`"
                                 % (args.devices, len(found), os_name))
            found = found[:args.devices]
            if not found:
                raise SystemExit("no Lab device up for %s — run `./labkit up` "
                                 "(or pass --android/--ios)" % os_name)
            refuse_unusable(os_name, found)
        devices[os_name] = found
    cells = labkit.lab_cells(platforms, oses)        # (WO-143) only where the platform has a host

    # (WO-027, PREFLIGHT) Before the sweep's record exists: a refused sweep leaves nothing behind, as the refusals
    # above leave nothing.
    before, pre_lines, why = preflight(devices.get("android", []))
    if why and args.preflight:
        for line in pre_lines:
            print(line, flush=True)
        raise SystemExit(refusal(why, ""))

    # (Tier C, C7.7) Two sweeps started in one second must not share a record: board.json, its temp file
    # and the cell logs would overwrite each other.  (WO-027, sweep lock) They can no longer run at once,
    # but one can end inside the second it began (--build none, and every cell n/a or every device idle)
    # and the next start in that same second.
    second, n = time.strftime("%Y%m%d-%H%M%S"), 1
    while True:
        stamp = second if n == 1 else "%s-%d" % (second, n)
        log_dir = os.path.join(labkit.HARNESS, ".claims", "sweep-" + stamp)
        try:
            os.makedirs(log_dir)
            break
        except FileExistsError:
            n += 1
    global _dir
    _dir = log_dir                            # (Tier C, C7.0) say() tees into stdout.log from here
    say("SWEEP %s: %d scenarios x %d cells, variant %s, build=%s, logs %s, load %.1f" % (
        stamp, len(scenarios), len(cells), args.variant, args.build, os.path.relpath(log_dir, REPO),
        os.getloadavg()[0]))

    for line in pre_lines:
        say(line)
    if why:
        say("PREFLIGHT ignored (--no-preflight): %s" % why)

    t0 = time.time()
    _board.update(sweep=stamp, argv=sys.argv, variant=args.variant, scenarios=scenarios, cells=cells,
                  serials=devices, t0=round(t0, 3), builds=[], lanes={}, runs=[], wall=None,
                  preflight={"start": before})
    keep()
    results = {}
    build_pass(cells, args, results, log_dir)
    # (WO-027, PREFLIGHT) Again once the builds are done, when any ran: eight app builds are what took the host from
    # full to out of memory on 2026-10-05.  The apps stay built, so the next start skips them.
    if any(b.get("status") != "current" for b in _board["builds"]):
        after, post_lines, why = preflight(devices.get("android", []))
        _board["preflight"]["after_builds"] = after
        for line in post_lines:
            say(line)
        keep()
        if why and args.preflight:
            raise SystemExit(refusal(why, " after its builds"))
        if why:
            say("PREFLIGHT ignored (--no-preflight): %s" % why)
    threads = []
    for os_name, serials in devices.items():
        # (WO-106) One queue per OS, in the order one device would run it: platforms in --platforms order, then
        # scenarios.  Each device's `platforms` in board.json is what it actually ran.
        queue = [(p, s) for p in platforms for s in scenarios]
        if not queue:
            continue
        queue = Queue(queue, len(serials))
        span = _board["lanes"].setdefault(os_name, {"start": None, "end": None, "devices": {}})
        for serial in serials:
            span["devices"][serial] = {"start": None, "end": None, "platforms": []}
            threads.append(threading.Thread(target=lane, args=(os_name, serial, queue, args, results, log_dir)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    _board["wall"] = round(wall, 3)
    keep()
    return 0 if board(results, cells, scenarios, round(wall), devices) else 1


if __name__ == "__main__":
    sys.exit(main())
