#!/usr/bin/env python3
"""The lab's own devices (Tier C, C7.7): two Android emulators and two iOS Simulators, each pair
named `Lab1` and `Lab2`, so a sweep can run one lane per device.

    ./labkit up                  # create what is missing, start it, wait until ready (lab_devices.py up)
    ./labkit status              # the same table; changes nothing
    ./labkit down --os android   # stop the Lab devices, and only those

Everything goes by NAME.  Nothing that is not called Lab1 or Lab2 is ever started, stopped, created or
changed — not Pixel_9_API_36 (emulator-5554), not Pixel_9_API_36_lab2, not any other AVD or Simulator.
Reading is another matter: finding the Lab devices means listing every emulator and Simulator.

Android.  A missing AVD is written from `avd/lab.config.ini` (avdmanager has no pixel_9 device on this
machine), and an existing one must match it.  Each has a fixed console port, so its serial never changes:
Lab1 is emulator-5580, Lab2 emulator-5582.  A Lab AVD running on any other port is reported, and `up`
refuses rather than start a second copy.  Ready means, in order: adb lists it as `device` and it answers
to its own name, sys.boot_completed is 1, it has a device window (`avd/windows.swift`; on any Space, or
minimized), `backends.Adb.preflight()` passes (boot, /data room, display awake), Google Location Accuracy
is on (`up` only: it turns it on), and its display draws: a raw screencap is not one flat colour.  (Tier C,
C8.0) resolve() no longer checks Location Accuracy: run.py sets each step's on a Lab emulator, so a scenario can
ask for it off and the next step, or the scenario's own end, puts it back.

iOS.  A missing Simulator is created as an iPhone 17 Pro on iOS 27.2, and ready means `simctl bootstatus`
finished on that device type and runtime.

`resolve(os_name)` is sweep.py's default device list: the Lab devices that are ready now and that no live
run has claimed, Lab1 first.  It starts nothing.
"""
import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time

import backends
import devices
import labkit

NAMES = ("Lab1", "Lab2")
# (Tier C, C7.7) Fixed console ports: a serial that never changes, clear of Chris's 5554 and of the ports an
# emulator started without -port takes next (5556, 5558, …), and inside the range adb scans (5554–5584).
PORTS = {"Lab1": 5580, "Lab2": 5582}
SDK = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.join(
    os.path.expanduser("~"), "Library", "Android", "sdk")
AVD_HOME = os.path.join(os.path.expanduser("~"), ".android", "avd")
TEMPLATE = os.path.join(labkit.HARNESS, "avd", "lab.config.ini")
WINDOWS = os.path.join(labkit.HARNESS, "avd", "windows.swift")
# The machine-wide lab cache the claims live in: an emulator outlives this process, and so does its output.
CACHE = os.path.dirname(devices.CLAIMS)
DEVICE_TYPE = "iPhone 17 Pro"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-2"
# (Tier C, C7.7) What makes an existing AVD one of the lab's, beside its name: the template's values for these.
# Both AVDs the same device and image, so no cell's result depends on which one ran it.
HARDWARE = ("AvdId", "image.sysdir.1", "tag.id", "abi.type", "hw.device.name", "hw.lcd.width", "hw.lcd.height",
            "hw.lcd.density", "hw.ramSize", "disk.dataPartition.size")
POLL_S = 2


class Final(str):
    """A why that more time cannot change: poll() returns it at once."""


def poll(check, deadline):
    """Call check() — None when satisfied, else why not — every POLL_S until it is satisfied (None) or
    `deadline` has passed (the last why), or check() says why not with a Final.  A deadline already past
    checks once.  A device call past its own timeout is a why, not an error."""
    while True:
        try:
            why = check()
        except SystemExit as e:
            why = str(e)
        if why is None or isinstance(why, Final) or time.time() >= deadline:
            return why
        time.sleep(POLL_S)


def adb_devices():
    """serial -> adb's state for it (`device`, `offline`, …), for every emulator adb lists."""
    out = labkit.device_call(["adb", "devices"], timeout=20).stdout
    return dict(l.strip().split("\t", 1) for l in out.splitlines()[1:] if l.startswith("emulator-") and "\t" in l)


def avd_name(serial):
    """The AVD an emulator says it is, over its console; None when it does not answer."""
    try:
        return labkit.adb(serial, "emu", "avd", "name", timeout=15).splitlines()[0].strip()
    except (SystemExit, IndexError):
        return None


def entries():
    """The names in AVD_HOME as written.  (Tier C, C7.7) This disk ignores case, so os.path.exists says a
    Lab1.ini is there when only another AVD's lab1.ini is."""
    try:
        return os.listdir(AVD_HOME)
    except OSError:
        return []


def template(name):
    """avd/lab.config.ini's lines for the AVD `name`: comments out, {name} filled in."""
    with open(TEMPLATE) as f:
        return [l.replace("{name}", name) for l in f.read().splitlines() if l and not l.startswith("#")]


def avd_problem(name):
    """Why the AVD `name` on disk is not a Lab AVD; None when it is.  (Tier C, C7.7) Checked before an
    existing one is launched, used or reported: a hand-copied .ini can name another AVD's disk (the lab2
    clone was made by copying files), and the emulator would boot that disk as Lab1 and answer to Lab1."""
    avd = os.path.join(AVD_HOME, name + ".avd")
    try:
        with open(os.path.join(AVD_HOME, name + ".ini")) as f:
            ini = dict(l.strip().split("=", 1) for l in f if "=" in l)
        # The emulator opens `path`, else `path.rel` under ~/.android: both, when given, must be this AVD's
        if (not ini.get("path") and not ini.get("path.rel") or ini.get("path.rel") not in (None, "avd/%s.avd" % name)
                or ini.get("path") and os.path.realpath(ini["path"]) != os.path.realpath(avd)):
            return "%s.ini points at %s, not %s" % (name, ini.get("path") or ini.get("path.rel"), avd)
        if name + ".avd" not in entries():
            return "no %s.avd in %s" % (name, AVD_HOME)
        with open(os.path.join(avd, "config.ini")) as f:
            have = dict(l.strip().split("=", 1) for l in f if "=" in l)
    except OSError as e:
        return "%s is unreadable: %s" % (name, e)
    want = dict(l.split("=", 1) for l in template(name))
    off = ["%s=%s (the lab's is %s)" % (k, have.get(k), want[k]) for k in HARDWARE if have.get(k) != want[k]]
    return "not a Lab AVD: %s" % ", ".join(off) if off else None


def android_view(name, listed=None):
    """One Lab AVD as it stands, from reads only: whether it exists, its emulator's pid, the serial it
    answers on, adb's state for that serial — and, when it is not running, which AVD holds its port.
    `listed` is adb's listing when the caller has one: {} reads ps alone."""
    port = PORTS[name]
    v = {"name": name, "serial": "emulator-%d" % port, "pid": None, "elsewhere": False, "taken": None,
         "exists": name + ".ini" in entries()}
    listed = adb_devices() if listed is None else listed
    for line in labkit.device_call(["ps", "-Ao", "pid=,command="], timeout=20).stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        # The binary's path, which may hold spaces, ends at the first option: a -flag, or `@Lab1`
        exe = re.split(r" (?=[-@])", cmd, maxsplit=1)[0]
        if "qemu-system" not in os.path.basename(exe):
            continue
        args = cmd[len(exe):].split()
        opt = lambda flag: args[args.index(flag) + 1] if flag in args[:-1] else None
        # (Tier C, C7.7) `emulator @Lab1` is `-avd Lab1`: missed, `down` said done and `up` started a second copy
        avd, at = opt("-avd") or next((a[1:] for a in args if a.startswith("@")), None), opt("-port")
        if avd == name:
            v["pid"] = int(pid)
            if at != str(port):
                # Started some other way — Android Studio passes no -port — so find its serial by asking each
                # emulator adb lists which AVD it is.
                v["elsewhere"] = True
                v["serial"] = "emulator-" + at if at else next((s for s in listed if avd_name(s) == name), None)
        elif at == str(port):
            v["taken"] = avd
    if v["pid"] is None and v["serial"] in listed:
        v["taken"] = v["taken"] or avd_name(v["serial"]) or "an emulator that does not say which AVD it is"
    v["adb"] = listed.get(v["serial"])
    return v


def windows(pids):
    """pid -> [(width, height, on screen)] for each non-empty layer-0 window those processes own."""
    r = labkit.device_call(["swift", WINDOWS] + [str(p) for p in pids], timeout=120)
    if r.returncode:
        raise SystemExit("windows.swift failed: %s" % r.stderr.strip()[-300:])
    out = {}
    for line in r.stdout.splitlines():
        pid, size, on = line.split()
        w, h = size.split("x")
        out.setdefault(int(pid), []).append((int(w), int(h), on == "1"))
    return out


def screens(wins):
    """(Tier C, C7.7) The device's own window among a qemu process's: portrait, and wider than any bar.  The
    emulator also owns a 61x515 toolbar, a 500x500 window and 30-px strips (Chris's, 2026-09-26); none of them is
    the screen, so one of them on screen must not pass for a drawn device."""
    return [(w, h, on) for w, h, on in wins if h > w >= 200]


def ready_android(v, deadline, launched=None, setup=False):
    """None when this Lab AVD, running on its own port, is ready; else the step that failed and why.
    Each step is polled until `deadline`, in order.  `launched`: when `up` started its emulator.  `setup`:
    `up` may change the device (Location Accuracy, which only `up` checks); resolve() only looks."""
    serial, name = v["serial"], v["name"]

    def listed():
        state = adb_devices().get(serial)
        if state == "device":
            return None
        why = "adb lists %s as %s" % (serial, state) if state else "adb does not list %s" % serial
        # (Tier C, C7.7) An emulator that exited at launch — a second copy its AVD's lock refused, a system image
        # gone — is not coming back: say so now, not at the deadline.  Its launcher has 10 s to start qemu.
        if launched and time.time() > launched + 10 and android_view(name)["pid"] is None:
            return Final("its emulator exited, and " + why)
        return why

    why = poll(listed, deadline)
    if why:
        return "no device: " + why

    # (Tier C, C7.7) Before preflight wakes and unlocks it: the serial must be this AVD, never an emulator
    # of Chris's that took the port.  A console slow to answer (four devices booting) is asked again; one
    # that names another AVD is refused at once.
    def identity():
        got = avd_name(serial)
        why = "%s answers as AVD %s, not %s" % (serial, got, name)
        return None if got == name else why if got is None else Final(why)

    why = poll(identity, deadline)
    if why:
        return "identity: " + why

    def booted():
        got = labkit.adb(serial, "shell", "getprop", "sys.boot_completed", timeout=15).strip()
        return None if got == "1" else "sys.boot_completed is %s" % (got or "unset")

    why = poll(booted, deadline)
    if why:
        return "boot not completed: " + why
    pid = android_view(name)["pid"]

    # (Tier C, C7.7, Chris's option C) The device window may be on another Space or minimized: macOS reports
    # neither as on screen, and a healthy Lab emulator there must not drop out of a sweep.  Whether the
    # display actually draws is asked of the device itself, after preflight has woken it (blank()).
    def drawn():
        if pid is None:
            return "its emulator process is gone"
        wins = windows([pid]).get(pid, [])
        if screens(wins):
            return None
        return "pid %d has no device window%s — booted but never drawn, as on 2026-09-23" % (
            pid, " (only %s)" % ", ".join("%dx%d%s" % (w, h, "" if on else " off screen") for w, h, on in wins)
            if wins else "")

    why = poll(drawn, deadline)
    if why:
        return "window not drawn: " + why
    try:
        backends.Adb(serial).preflight()
    except SystemExit as e:
        return "preflight refused: %s" % e
    # (Tier C, C8.0; the genesis analysis §2.3 d) Only `up` sets the lab's default.  resolve() no longer leaves an
    # emulator with Location Accuracy off out of a sweep: run.py sets each step's (run.set_accuracy), so a scenario can
    # ask for it off, and the next step puts it back.  `status` still shows it.
    if setup:
        why = accuracy_on(serial)
        if why:
            return "location accuracy: " + why
    why = poll(lambda: blank(serial), deadline)
    return "not rendering: " + why if why else None


def accuracy_off(serial):
    """(Tier C, C7.7) Why this emulator's location is not the lab's default, or None: Google Location Accuracy
    on (backends.Adb.location_accuracy) and assisted GPS on, as on emulator-5554.  Reads only."""
    on = backends.Adb(serial).location_accuracy()
    if on is None:      # (Tier C, C8.0 review) no network provider block: unread, not off
        return "Google Location Accuracy could not be read (dumpsys location shows no network provider)"
    if not on:
        return "Google Location Accuracy is off (the network provider is disabled)"
    if labkit.adb(serial, "shell", "settings", "get", "global", "assisted_gps_enabled", timeout=15).strip() != "1":
        return "assisted GPS is off"
    return None


def accuracy_on(serial):
    """(Tier C, C7.7) Set the lab's default: assisted GPS on, and Google Location Accuracy on through Play
    services' own switch (backends.Adb.set_location_accuracy).  None when both are on, else why not."""
    labkit.adb(serial, "shell", "settings", "put", "global", "assisted_gps_enabled", "1", timeout=15)
    return backends.Adb(serial).set_location_accuracy(True)


def blank(serial):
    """(Tier C, C7.7) Why this emulator's display is not drawing, or None.  A raw screencap is width, height,
    format and (API 29+) a dataspace, 4 bytes each, then RGBA_8888; one that is a single colour is a display
    that renders nothing, where a booted, woken device shows at least its launcher.  Every 997th pixel is
    compared (a prime stride, so no row pattern hides a difference): about 2,600 of 2.6 M."""
    raw = labkit.device_call(["adb", "-s", serial, "exec-out", "screencap"], timeout=30, text=False).stdout
    w, h = int.from_bytes(raw[0:4], "little"), int.from_bytes(raw[4:8], "little")
    head = len(raw) - w * h * 4
    if len(raw) < 12 or head not in (12, 16):
        return "screencap returned %d bytes, not a %dx%d RGBA image" % (len(raw), w, h)
    px = raw[head:]
    if len({px[i:i + 4] for i in range(0, len(px), 4 * 997)}) > 1:
        return None
    return "its %dx%d screen is one colour (#%s)" % (w, h, px[:4].hex())


# (WO-027, 2026-10-07) The Lab images carry the Play Store, and it updates the image's Google apps by itself, with no
# account signed in.  MEASURED 2026-10-07 on both emulators:
#   * Play services, which the SDK's location, geofencing and activity code runs on, moved under the lab: 25.07.34 in
#     the image, 26.34.36 on Lab2 from 2026-09-26, 26.36.35 on Lab1 from 2026-10-01.  The two emulators differed for
#     six days of boards whose cells either could take.
#   * 23 system apps ran from updated copies on /data, 14 of them updated in one evening (2026-10-06), and /data
#     was down to 729 MB free on Lab1 and 1,069 MB on Lab2 of 5.9 GB.  A run refuses below 512 MB.
# So `up` turns the Play Store app off (`pm disable-user`), which stops the updates; Play services stays, and nothing
# in the lab uses the Store itself.  With it off on both, hello-world, location-timestamp, events-geofence,
# permission-request and location-accuracy-off's six steps passed on each emulator, and `events` read its standing
# red.  Play services was made the same on both by installing Lab1's own APKs on Lab2, and sixteen unrelated apps'
# updates were reverted (2,263 and 2,163 MB free after).  labs/README.md, "Play Store and Play services", has the
# commands, and how to move the lab to a newer Play services on purpose.
PLAY_STORE, PLAY_SERVICES = "com.android.vending", "com.google.android.gms"


def play_store(serial, adb=None):
    """(WO-027, Play Store) The Play Store app on this emulator: "on", "off", or None where the image has none or
    the package list was not read.  `pm list packages` matches by substring, so the line itself is looked for."""
    adb = adb or labkit.adb
    for flag, state in (("-d", "off"), ("-e", "on")):
        if "package:" + PLAY_STORE in adb(serial, "shell", "pm", "list", "packages", flag, PLAY_STORE,
                                          timeout=15).split():
            return state
    return None


def play_services(serial, adb=None):
    """(WO-027, Play Store) Play services' versionCode on this emulator (263635035 is 26.36.35), or None."""
    m = re.search(r"^package:%s versionCode:(\d+)" % re.escape(PLAY_SERVICES), (adb or labkit.adb)(
        serial, "shell", "pm", "list", "packages", "--show-versioncode", PLAY_SERVICES, timeout=15), re.M)
    return int(m.group(1)) if m else None


def store_off(serial, adb=None):
    """(WO-027, Play Store) Turn the Play Store app off where it is on.  Returns what `up` adds to its line: nothing
    when it was off already or the image has none; a failure is said and does not fail the `up`."""
    adb = adb or labkit.adb
    try:
        if play_store(serial, adb) != "on":
            return ""
        said = adb(serial, "shell", "pm", "disable-user", "--user", "0", PLAY_STORE, timeout=30).strip()
        if play_store(serial, adb) == "off":
            return ", Play Store turned off"
    except SystemExit as e:
        said = str(e)
    return ", Play Store NOT turned off (%s)" % (said.splitlines()[-1][:100] if said else "pm said nothing")


# (WO-027, 2026-10-07) macOS rests an app whose window is hidden or covered (App Nap), and a resting emulator launches
# apps at a third of its pace and starves on a busy host (sweep.py, PRIORITY).  The emulator has no bundle id, so its
# preferences domain is its executable's name, and NSAppSleepDisabled there opts it out from its next start.
# MEASURED 2026-10-07: with it set and the emulators restarted, both stayed at normal priority through seven minutes
# with their windows hidden (200 readings, 2 s apart) and launched a lab app in 1.8 s; before, Lab1 was at
# priority 4 within five minutes of being hidden, and launched it in 4.4 to 5.2 s.
NAP_DOMAIN, NAP_KEY = "qemu-system-aarch64", "NSAppSleepDisabled"


def keep_awake(call=None, platform=sys.platform):
    """(WO-027, PRIORITY) Opt the emulator out of App Nap before one is started, where it is not opted out already:
    a preference of the user's, kept.  Returns what `up` adds to its line: nothing when it was set, or off macOS."""
    if platform != "darwin":
        return ""
    call = call or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    try:
        if call(["defaults", "read", NAP_DOMAIN, NAP_KEY]).stdout.strip() == "1":
            return ""
        wrote = call(["defaults", "write", NAP_DOMAIN, NAP_KEY, "-bool", "YES"])
        why = None if wrote.returncode == 0 else wrote.stderr.strip() or "defaults exit %d" % wrote.returncode
    except (OSError, subprocess.SubprocessError) as e:
        why = str(e)
    return ", App Nap opt-out NOT set (%s)" % why if why else ", App Nap opt-out set (`defaults write %s %s`)" % (
        NAP_DOMAIN, NAP_KEY)


def up_android(name, timeout):
    """(serial, ready, note) once the Lab AVD `name` is ready or `timeout` has passed."""
    t0 = time.time()
    v = android_view(name)
    why = v["exists"] and avd_problem(name)
    if why:
        return v["serial"] or "?", False, why
    if v["elsewhere"]:
        return v["serial"] or "?", False, "running, not on its port %d — `down` it, then `up` again" % PORTS[name]
    note, launched = "already running", None
    if v["pid"] is not None:
        # (Tier C, C7.7) A run owns its device (WO-026): its preflight below would wake and unlock it mid-run
        holder = claimed(v["serial"])
        if holder:
            return v["serial"], False, "claimed by %s — in use, so not checked" % holder
    else:
        if v["taken"]:
            return v["serial"], False, "its port %d is taken by %s" % (PORTS[name], v["taken"])
        note = "launched"
        if not v["exists"]:
            # (Tier C, C7.7) Written as the Pixel_9_API_36_lab2 clone was: the .ini and config.ini, nothing
            # else — the emulator makes the disk images on first boot.  An AVD that exists is never
            # rewritten, and neither is a leftover <name>.avd directory, nor another AVD's lab1.ini on
            # this case-blind disk.  Created exclusively: of two `up`s at once, one creates it.
            config = template(name)
            keys = dict(l.split("=", 1) for l in config)
            for key in ("image.sysdir.1", "skin.path"):
                if not os.path.isdir(os.path.join(SDK, keys[key])):
                    return v["serial"], False, "cannot create %s: no %s under the SDK %s (%s)" % (
                        name, keys[key], SDK, key)
            found = sorted(e for e in entries() if e.lower() in (name.lower() + ".ini", name.lower() + ".avd"))
            if found:
                return v["serial"], False, "cannot create %s: %s in %s with no %s.ini — not overwriting it" % (
                    name, " and ".join(found), AVD_HOME, name)
            avd = os.path.join(AVD_HOME, name + ".avd")
            try:
                os.makedirs(avd)
                with open(os.path.join(avd, "config.ini"), "w") as f:
                    f.write("\n".join(config) + "\n")
                with open(os.path.join(AVD_HOME, name + ".ini"), "x") as f:
                    f.write("avd.ini.encoding=UTF-8\npath=%s\npath.rel=avd/%s.avd\ntarget=%s\n"
                            % (avd, name, keys["target"]))
            except OSError as e:
                return v["serial"], False, "cannot create %s: %s — another `up` creating it at once?" % (name, e)
            note = "created, launched"
        log = os.path.join(CACHE, "emulator-%s.log" % name)
        os.makedirs(CACHE, exist_ok=True)
        # -port: the fixed serial.  -no-snapshot: always a cold boot from the AVD's own disk, and no RAM
        # snapshot written at shutdown — `down` is quick, and neither device ever resumes a saved session
        # (a Quick Boot snapshot, saved if the AVD is ever run from Android Studio, would bring back that
        # session's apps and screens).  Its own session, stdin closed, output to the cache: it outlives us.
        argv = [os.path.join(SDK, "emulator", "emulator"), "-avd", name, "-port", str(PORTS[name]), "-no-snapshot"]
        note += keep_awake()             # (WO-027, PRIORITY) before the start: read once, when the process begins
        # Appended under a header: a second `up` must not truncate the log of an emulator already running
        with open(log, "a") as out:
            out.write("\n== %s lab_devices.py up: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(argv)))
            out.flush()
            try:
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            except OSError as e:
                return v["serial"], False, "cannot start %s: %s — set ANDROID_HOME to the SDK" % (argv[0], e)
        launched = time.time()
        note += ", log %s" % log.replace(os.path.expanduser("~"), "~", 1)
    why = ready_android(v, t0 + timeout, launched, setup=True)
    # (Tier C, C7.7) `up` never stops what it started: a failed one leaves a 7.5 GB emulator for `down`
    if why and android_view(name, {})["pid"] is not None:
        note += "; still running — `down` stops it"
    if why is None:
        note += record_network(name, v["serial"]) + store_off(v["serial"])      # (WO-027, Play Store)
    return v["serial"], why is None, why + " (%s)" % note if why else note


def record_network(name, serial):
    """(Tier C, C5.0) Record this AVD's wifi / mobile data / battery saver readings, the defaults backends.Adb.neutral()
    holds each step to (`actions: network` changes them) — once, where none is recorded.  (C5.0 review) Never again on a
    later `up`, cold or not: `svc wifi|data disable` persists in Settings.Global on the AVD's own disk, which a
    -no-snapshot boot keeps, so a step that died between its network actions would come back offline and be recorded as
    the default.  To re-record, delete the file (the note `up` prints names it).  The note, or ""."""
    path = backends.network_default_path(name)
    if os.path.exists(path):
        return ""
    reads = backends.Adb.NETWORK
    now = labkit.adb(serial, "shell", "; ".join(read for read, _ in reads), timeout=15).strip().splitlines()
    if len(now) != len(reads):
        return "; network defaults NOT recorded (%d readings)" % len(now)
    got = {key: now[k].strip() for k, (_, key) in enumerate(reads)}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(got, f)
    os.replace(path + ".tmp", path)
    return "; network defaults %s recorded in %s" % (" ".join("%s=%s" % kv for kv in sorted(got.items())),
                                                     path.replace(os.path.expanduser("~"), "~", 1))


def simulators():
    """`simctl list -j`: devices, device types and runtimes, one listing."""
    try:
        return json.loads(labkit.device_call(["xcrun", "simctl", "list", "-j"], timeout=30).stdout)
    except ValueError:
        raise SystemExit("`xcrun simctl list -j` did not return JSON")


def named(listing, name):
    """Every Simulator called `name`, each with its runtime."""
    return [dict(d, runtime=rt) for rt, devs in listing.get("devices", {}).items() for d in devs
            if d.get("name") == name]


def ios_problem(sims, listing):
    """Why these Simulators, all of one Lab name, are not one lab device; None when they are."""
    if len(sims) > 1:
        return "%d Simulators are named %s (%s) — rename all but one" % (
            len(sims), sims[0]["name"], ", ".join(s["udid"] for s in sims))
    want = next((t.get("identifier") for t in listing.get("devicetypes", []) if t.get("name") == DEVICE_TYPE), None)
    sim = sims[0]
    # (Tier C, C7.7) Both Simulators the same device and runtime, so no cell's result depends on which one ran it.
    if sim["runtime"] != RUNTIME or sim.get("deviceTypeIdentifier") != want:
        return "%s is %s on %s; a Lab Simulator is %s on %s" % (
            sim["name"], sim.get("deviceTypeIdentifier"), sim["runtime"], DEVICE_TYPE, RUNTIME)
    return None


def up_ios(name, timeout):
    """(UDID, ready, note) once the Lab Simulator `name` has booted or `timeout` has passed."""
    t0 = time.time()
    listing = simulators()
    sims = named(listing, name)
    if not sims:
        types = {t.get("name") for t in listing.get("devicetypes", [])}
        runtimes = {r.get("identifier") for r in listing.get("runtimes", []) if r.get("isAvailable")}
        missing = [what for what, ok in (("no %s device type" % DEVICE_TYPE, DEVICE_TYPE in types),
                                         ("no available runtime %s" % RUNTIME, RUNTIME in runtimes)) if not ok]
        if missing:
            return "-", False, "cannot create %s: %s" % (name, "; ".join(missing))
        r = labkit.device_call(["xcrun", "simctl", "create", name, DEVICE_TYPE, RUNTIME], timeout=60)
        if r.returncode:
            return "-", False, "simctl create failed: %s" % r.stderr.strip()
        udid, note = r.stdout.strip(), "created, booted"
    else:
        why = ios_problem(sims, listing)
        if why:
            return sims[0]["udid"], False, why
        udid, note = sims[0]["udid"], "already booted" if sims[0].get("state") == "Booted" else "booted"
    # bootstatus -b boots a Simulator that is shut down, then waits until its boot has finished.
    try:
        r = labkit.device_call(["xcrun", "simctl", "bootstatus", udid, "-b"],
                               timeout=max(1, round(t0 + timeout - time.time())))
    except SystemExit:
        why = "did not finish within %d s" % timeout
    else:
        why = "exited %d: %s" % (r.returncode, (r.stderr or r.stdout).strip()[-200:]) if r.returncode else None
    if why:
        return udid, False, "boot not completed: bootstatus %s%s" % (
            why, " (created)" if note.startswith("created") else "")
    return udid, True, note


def status_android(name):
    v = android_view(name)
    if not v["exists"]:
        return v["serial"], "absent"
    problem = avd_problem(name)                  # (Tier C, C7.7) what `up` and resolve() would refuse, first
    if problem:
        return v["serial"] or "?", "%s, %s" % ("stopped" if v["pid"] is None else "running", problem)
    if v["pid"] is None:
        return v["serial"], "stopped" + (", port %d taken by %s" % (PORTS[name], v["taken"]) if v["taken"] else "")
    state = "running on another port" if v["elsewhere"] else "running"
    if v["adb"] != "device":
        return v["serial"] or "?", "%s, adb: %s" % (state, v["adb"] or "not listed")
    try:
        boot = labkit.adb(v["serial"], "shell", "getprop", "sys.boot_completed", timeout=15).strip()
        device = screens(windows([v["pid"]]).get(v["pid"], []))
        free = backends.Adb(v["serial"])._free_mb()
        why = accuracy_off(v["serial"]) if boot == "1" else ""
        accuracy = "?" if boot != "1" else "on" if why is None else "unread" if "could not be read" in why else "OFF"
        # (WO-027, Play Store) what `up` turns off, said loudly while it is on; and what the SDK runs on
        store, gms = (play_store(v["serial"]), play_services(v["serial"])) if boot == "1" else (None, None)
    except SystemExit as e:
        return v["serial"], "%s, unreadable: %s" % (state, e)
    window = "on screen" if any(on for _, _, on in device) else "off screen" if device else "none"
    return v["serial"], "%s, %s, window %s, /data free %s MB, location accuracy %s%s%s" % (
        state, "booted" if boot == "1" else "booting", window, free, accuracy,
        "" if gms is None else ", Play services %d" % gms,
        "" if store is None else ", Play Store %s" % ("off" if store == "off" else "ON"))


def status_ios(name, listing):
    sims = named(listing, name)
    if not sims:
        return "-", "absent"
    return sims[0]["udid"], ios_problem(sims, listing) or sims[0].get("state")


def claimed(serial):
    """Who holds this device's claim (devices.claim), when that run is still alive."""
    try:
        with open(devices._claim_path(serial)) as f:
            held = json.load(f)
    except (OSError, ValueError):
        return None
    return "pid %s (%s)" % (held.get("pid"), held.get("owner")) if devices._alive(held.get("pid")) else None


def down_android(name):
    try:
        v, wedged = android_view(name), None
    except SystemExit:
        # (Tier C, C7.7) adb itself not answering, as on 2026-09-23: ps still finds the Lab emulator
        v, wedged = android_view(name, {}), "adb does not answer"
    if v["pid"] is None:
        return v["serial"], True, "not running"
    serial = v["serial"]
    # (Tier C, C7.7) A run owns its device (WO-026): never pull one from under it.
    holder = serial and claimed(serial)
    if holder:
        return serial, False, "claimed by %s — not stopped" % holder
    # `emu kill` stops whatever answers on that port: it must be this AVD.
    got = avd_name(serial) if v["adb"] == "device" else None
    if got == name:
        labkit.adb(serial, "emu", "kill", timeout=15)
        # Gone from both lists: an `up` right after must not find adb still holding the port.
        why = poll(lambda: None if android_view(name)["pid"] is None and serial not in adb_devices()
                   else "still running", time.time() + 120)
        return serial, why is None, "stopped" if why is None else "%s 120 s after `emu kill`" % why
    if got is not None:
        return serial, False, "%s answers as AVD %s, not %s — not stopped" % (serial, got, name)
    # (Tier C, C7.7) No console to ask: adb does not answer, does not list it or lists it offline, or its
    # console is silent — the states a failed `up` leaves.  ps found the process by its own command line,
    # `-avd <name>` or `@<name>`, which no other AVD's has, so stop that process: SIGTERM, then SIGKILL.
    why_not = wedged or ("adb does not list it" if v["adb"] is None else "adb lists it %s" % v["adb"]
                         if v["adb"] != "device" else "its console does not answer")
    gone = lambda: None if android_view(name, {})["pid"] is None else "still running"
    for sig, grace in (("TERM", 30), ("KILL", 10)):
        labkit.device_call(["kill", "-" + sig, str(v["pid"])], timeout=15)
        if poll(gone, time.time() + grace) is None:
            return serial or "?", True, "stopped pid %d with SIG%s (%s)" % (v["pid"], sig, why_not)
    return serial or "?", False, "pid %d still running after SIGKILL (%s)" % (v["pid"], why_not)


def down_ios(sim):
    if sim.get("state") == "Shutdown":
        return True, "not running"
    holder = claimed(sim["udid"])
    if holder:
        return False, "claimed by %s — not stopped" % holder
    r = labkit.device_call(["xcrun", "simctl", "shutdown", sim["udid"]], timeout=60)
    return r.returncode == 0, "stopped" if r.returncode == 0 else "simctl shutdown: %s" % r.stderr.strip()


def resolve(os_name):
    """The serial (Android) or UDID (iOS) of each Lab device that is up and ready now, and not claimed
    by a live run, in Lab1, Lab2 order: sweep.py's default devices.  Starts nothing.  A Lab device left
    out is named on stderr, so a sweep that runs on fewer devices than there are says why."""
    listing = simulators() if os_name == "ios" else None
    out = []
    for name in NAMES:
        if os_name == "android":
            v = android_view(name)
            serial = v["serial"]
            why = "absent" if not v["exists"] else avd_problem(name) or (
                "stopped" if v["pid"] is None
                else "running, not on its port %d" % PORTS[name] if v["elsewhere"] else None)
        else:
            sims = named(listing, name)
            serial = sims[0]["udid"] if sims else None
            why = "absent" if not sims else ios_problem(sims, listing) or (
                None if sims[0].get("state") == "Booted" else sims[0].get("state"))
        # (Tier C, C7.7) A device a live run holds is not dealt to a second sweep, and the preflight below
        # must not wake or unlock it under that run.
        holder = None if why else claimed(serial)
        if holder:
            why = "claimed by %s" % holder
        elif not why and os_name == "android":
            why = ready_android(v, 0)
        elif not why:
            # simctl says Booted from the first moment of a boot: ready is bootstatus finished (without -b
            # it boots nothing)
            try:
                r = labkit.device_call(["xcrun", "simctl", "bootstatus", serial], timeout=10)
                why = "bootstatus exited %d" % r.returncode if r.returncode else None
            except SystemExit:
                why = "still booting: bootstatus did not finish within 10 s"
        if why:
            print("lab_devices: %s %s left out — %s" % (os_name, name, why), file=sys.stderr)
        else:
            out.append(serial)
    return out


def row(os_name, name, serial, state, detail=""):
    print(("%-7s %-4s %-36s %-9s %s" % (os_name, name, serial, state, detail)).rstrip(), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["up", "status", "down"])
    ap.add_argument("--os", default="android,ios", help="comma list: android, ios")
    ap.add_argument("--timeout", type=int, default=300, help="`up`: seconds each device has to become ready")
    args = ap.parse_args()
    oses = [o for o in args.os.split(",") if o]
    if not oses or set(oses) - {"android", "ios"}:
        raise SystemExit("--os takes android, ios or both")
    jobs = [(o, n) for o in ("android", "ios") if o in oses for n in NAMES]

    if args.command == "status":
        listing = simulators() if "ios" in oses else None
        for o, n in jobs:
            row(o, n, *(status_android(n) if o == "android" else status_ios(n, listing)))
        return 0

    if args.command == "down":
        ok = True
        for o, n in jobs:
            if o == "android":
                try:
                    serial, done, detail = down_android(n)
                except SystemExit as e:
                    serial, done, detail = "?", False, str(e)
                row(o, n, serial, "down" if done else "NOT DOWN", detail)
                ok &= done
                continue
            sims = named(simulators(), n)
            if not sims:
                row(o, n, "-", "down", "absent")
            for sim in sims:                 # every Simulator called Lab<n>: all of them are the lab's
                done, detail = down_ios(sim)
                row(o, n, sim["udid"], "down" if done else "NOT DOWN", detail)
                ok &= done
        return 0 if ok else 1

    def up(o, n):
        t0 = time.time()
        try:
            serial, ready, note = (up_android if o == "android" else up_ios)(n, args.timeout)
        except SystemExit as e:              # a listing or device call past its timeout
            serial, ready, note = "?", False, str(e)
        return serial, ready, "%4ds  %s" % (time.time() - t0, note)

    # Every device at once, so the boots overlap; a line prints once it and those above it are done.
    ok = True
    with concurrent.futures.ThreadPoolExecutor(len(jobs)) as pool:
        for (o, n), (serial, ready, detail) in zip(jobs, pool.map(lambda j: up(*j), jobs)):
            row(o, n, serial, "ready" if ready else "NOT READY", detail)
            ok &= ready
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
