"""Device ownership and capture contamination (WO-026).

Two labs once shared emulator-5554: one lab's `logcat -c` erased the other's launch marks
and its Bold-text toggles recreated the other's app, silently invalidating three captures.
Nothing noticed. A run now claims a serial, and a capture that carries a foreign launch
mark fails scoring instead of being read as a result.
"""

import fcntl
import json
import os
import re
import subprocess
import time

HARNESS = os.path.dirname(os.path.abspath(__file__))
# (WO-027) ONE claims directory for every checkout on this machine.  It was HARNESS/.claims, so a
# session in a .claude/worktrees checkout had its own and could not see this one's: on 2026-09-24
# it drove the Simulator a sweep had claimed, and two captures went CONTAMINATED.  A device is a
# machine-wide thing, and so is its claim.  LAB_CLAIMS_DIR overrides it (tests).
CLAIMS = os.environ.get("LAB_CLAIMS_DIR") or os.path.join(
    os.path.expanduser("~"), "Library", "Caches", "transistorsoft-lab", "claims")


def _list(argv):
    """A device listing that cannot hang the harness (WO-027): a wedged adb server answered
    nothing for as long as anyone waited on 2026-09-23."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=20).stdout
    except subprocess.TimeoutExpired:
        raise SystemExit("`%s` did not return within 20 s — restart the adb server "
                         "(`adb kill-server`) or the Simulator" % " ".join(argv))


def attached():
    out = _list(["adb", "devices"])
    return [l.split("\t")[0] for l in out.splitlines()[1:]
            if l.strip() and l.endswith("\tdevice")]


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _claim_path(serial):
    return os.path.join(CLAIMS, serial + ".json")


def claim(serial, owner):
    """Take the serial, or raise if another live run holds it."""
    os.makedirs(CLAIMS, exist_ok=True)
    path = _claim_path(serial)
    if os.path.exists(path):
        with open(path) as f:
            held = json.load(f)
        if held.get("pid") != os.getpid() and _alive(held.get("pid")):
            raise SystemExit(
                "%s is claimed by pid %s (%s, from %s) since %s.\n"
                "Use --serial to pick another device, or clear %s if that run is gone."
                % (serial, held.get("pid"), held.get("owner"), held.get("checkout", "?"),
                   held.get("since"), path))
    with open(path, "w") as f:
        json.dump({"pid": os.getpid(), "owner": owner, "checkout": os.path.dirname(os.path.dirname(HARNESS)),
                   "since": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    return serial


def release(serial):
    path = _claim_path(serial)
    if os.path.exists(path):
        with open(path) as f:
            held = json.load(f)
        if held.get("pid") == os.getpid():
            os.remove(path)


def lock_sweep(owner):
    """(WO-027, sweep lock) Take the machine for one sweep, or raise naming the sweep that has it.  Returns
    the lock's fd: the sweep keeps it open and hands it to every build and run it starts (sweep.capped).

    On 2026-09-26 two boards started 21 s apart from one checkout: their builds rebuilt the same apps at
    once, their runs hit each other's device claims, and one uninstalled an app the other was running.
    Both boards were void.

    The lock is an flock on `sweep.json` beside the device claims, not the file.  The kernel holds it while
    any process with the fd lives and drops it when the last one exits, so nothing clears it by pid: a
    killed sweep's lock is free at once, a reused pid means nothing, and a build or run that outlives its
    sweep (each runs in its own session, so a ^C or a kill of the sweep leaves it going) still holds it.
    The file only names the holder for a refusal ({pid, owner = the command line, checkout, since}, as a
    device claim) and is never removed: then the next sweep would lock a new file beside the held one."""
    os.makedirs(CLAIMS, exist_ok=True)
    path = _claim_path("sweep")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        for _ in range(20):          # a sweep that has only just taken it may not have written itself yet
            try:
                with open(path) as f:
                    held = json.load(f)
            except (OSError, ValueError):
                held = None
            if isinstance(held, dict) and _alive(held.get("pid")):
                break
            time.sleep(0.05)
        held = held if isinstance(held, dict) else {}
        gone = "" if _alive(held.get("pid")) else " (gone; a build or run it started holds the lock)"
        raise SystemExit(
            "a sweep is already running: pid %s%s, from %s, since %s:\n    %s\n"
            "Wait for it, or stop it: `lsof %s` lists every process that holds the lock."
            % (held.get("pid", "?"), gone, held.get("checkout", "?"), held.get("since", "?"), held.get("owner", "?"),
               path))
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps({"pid": os.getpid(), "owner": owner, "checkout": os.path.dirname(os.path.dirname(HARNESS)),
                             "since": time.strftime("%Y-%m-%d %H:%M:%S")}).encode())
    return fd


def booted_simulators():
    """UDIDs of booted iOS Simulators (WO-027) — the only iOS devices a lab ever drives."""
    out = _list(["xcrun", "simctl", "list", "devices", "booted", "-j"])
    try:
        runtimes = json.loads(out).get("devices", {})
    except ValueError:
        return []
    # iPhones only: an iPad reports platform "iPadOS" and would fail the identity check for
    # the lab's reasons.
    return [d["udid"] for rt, devs in runtimes.items() if "iOS" in rt
            for d in devs if d.get("state") == "Booted"
            and "iPhone" in d.get("deviceTypeIdentifier", "")]


def pick(requested, owner, os_name="android"):
    """The requested serial, else $LAB_SERIAL, else the only emulator (or booted Simulator)."""
    if requested:
        return claim(requested, owner)
    # One variable per OS, so an exported Android serial can never be handed to simctl.
    env = os.environ.get("LAB_UDID" if os_name == "ios" else "LAB_SERIAL")
    if env:
        return claim(env, owner)
    if os_name == "ios":
        emulators = booted_simulators()
        if not emulators:
            raise SystemExit("no iOS Simulator is booted; boot one or pass --serial <udid>")
    else:
        emulators = [s for s in attached() if s.startswith("emulator-")]
    if not emulators:
        raise SystemExit("no emulator attached; pass --serial (physical devices are never "
                         "picked automatically)")
    free = [s for s in emulators if not os.path.exists(_claim_path(s))]
    if not free:
        raise SystemExit("every attached emulator is claimed: %s" % ", ".join(emulators))
    if len(free) > 1:
        raise SystemExit("several free emulators (%s); pass --serial" % ", ".join(free))
    return claim(free[0], owner)


LAUNCH_MARK = re.compile(r"LAB point=launch label=(\S+)")


def contamination(lines, label, os_name="android"):
    """Launch marks in this capture that belong to another run, and processes that are not this
    run's app — the contamination signal.

    Lines are normalized first: logcat renders a native LAB line as `I/LAB( pid): point=…`,
    so the tag and the message are not adjacent in the raw text.
    """
    import labkit
    # (WO-027) And a lab APP this run never launched: one left running on the device logs its
    # ticks and events into the same stream, with no launch mark to give it away (2026-09-24: all
    # 14 flutter-ios steps of that day's board).  Every process that writes `app=` lines must have
    # booted this step's driver — except one whose every line is `app=error`: the app's own driver
    # failing before its boot line, which scores as the FAIL it is rather than as a stray.  (Tier C,
    # C6.0 review) And `app=window`: the MainActivity's own report, 2 s after each resume, native and
    # logged whether the driver boots or not — a stuck splash is the app's FAIL (driverBoot `-`), not
    # another lab app.  The first step evicts every other lab app (run.py), so a foreign app on screen
    # with no driver running is not left to this check.
    own = labkit.app_pids(lines)
    written, boots = {}, []
    for l in lines:
        pid, n = labkit.line_pid(l), labkit.normalize(l)
        if pid and labkit.TAG + " app=" in n:
            written.setdefault(pid, []).append(n)
            if " app=boot " in n and pid not in boots:
                boots.append(pid)
    stray = sorted((p for p, ls in written.items()
                    if p not in own and not all(" app=error" in l or " app=window " in l for l in ls)), key=int)
    lines = [labkit.normalize(l) for l in lines]
    marks = {m.group(1) for l in lines for m in [LAUNCH_MARK.search(l)] if m}
    foreign = marks - {label}
    # Old labs used their own tag; a capture carrying one is from a different lab entirely.
    foreign |= {"(pre-WO-026 lab)"} if any("RECREATE_LAB point=launch" in l for l in lines) else set()
    out = sorted(foreign)
    if stray and label not in marks:
        # The head of the log is gone — this run's own launch mark with it — so a process whose
        # boot line is missing may be the app itself: Android's `logcat -d` is a ring buffer.
        out += ["log head lost: pid %s logs app= lines with no app=boot, and this run's launch mark "
                "is gone too" % ", ".join(stray)]
    else:
        out += ["foreign lab process pid %s" % p for p in stray]
    if os_name == "ios":
        # One process per iOS step (no trigger relaunches it): a second boot is another lab app.
        out += ["lab process pid %s booted mid-step" % p for p in boots[1:]]
    return out
