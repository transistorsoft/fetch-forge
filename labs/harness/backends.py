"""Device backends (WO-027): the primitives a step needs, on Android over adb and on the iOS
Simulator over simctl.

One class per OS, the same methods, so run.py never asks which OS it is on.  Everything an
Android-only scenario needs (Configuration triggers, dumpsys, run-as) lives on Adb and is a
no-op on Simctl — the scenarios that need it never list an iOS platform.

The log is the one place the two differ in KIND rather than in spelling: logcat is a buffer
the harness clears and dumps, while os_log is a stream the harness must be listening to
before the launch.  Simctl rewrites what it heard into logcat's `-v time` line shape, so
labkit's parser, marks and every metric stay one vocabulary.  Marks go through the device's
own log on both OSes — `adb shell log -t LAB` and `simctl spawn … syslog -s` — so their
timestamps come from the same clock as the app's lines.

The SDK's own log is mirrored into os_log line by line (subsystem
com.transistorsoft.BackgroundGeolocation); the tap keeps those under the tag
TSLocationManager, and Capture.sdk() reads them there.  The SQLite log database is pulled
for parity but is nearly empty for a short run — the iOS logger flushes it on a 60 s timer
and `simctl terminate` never flushes.
"""

import base64
import datetime
import glob
import html
import json
import mmap
import os
import plistlib
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

import devices
import labkit

SDK_SUBSYSTEM = "com.transistorsoft.BackgroundGeolocation"
SDK_TAG = "TSLocationManager"


class Adb:
    os = "android"

    def __init__(self, serial):
        self.serial = serial
        self._stream = None
        self._stream_path = None
        self._pending = None             # (action, Tap, before-dump): a tap no re-dump could confirm
        self._fresh = None               # (Tier C, C7.5) the dump that confirmed the last tap, for the next action

    def _adb(self, *args, **kw):
        return labkit.adb(self.serial, *args, **kw)

    MIN_FREE_MB = 512     # a 160 MB debug APK is staged and then installed; below this installs fail

    def preflight(self):
        """(WO-027) Refuse a device that cannot take a run, before anything is built or installed,
        and name it in the capture.  Two of 2026-09-23's lost hours: an emulator that had died, and
        one whose /data was 95 % full, so every install failed `not enough space`.  And the Android
        half of that night's sweep changed AVD under the same serial with nothing in run.json to
        say so — hence `info`."""
        prop = lambda k: self._adb("shell", "getprop", k, timeout=15).strip()
        if prop("sys.boot_completed") != "1":
            raise SystemExit("%s is not booted (sys.boot_completed != 1) — start it from Android "
                             "Studio's Device Manager" % self.serial)
        free_mb = self._free_mb()
        if free_mb is not None and free_mb < self.MIN_FREE_MB:
            raise SystemExit("%s has %d MB free on /data; the lab needs %d — uninstall apps or wipe "
                             "the AVD" % (self.serial, free_mb, self.MIN_FREE_MB))
        # (WO-027) The screen: a pressed power button ran two captures with the display off on
        # 2026-09-23 — the app launched behind a dark screen, a condition the lab never measures.
        awake = "mWakefulness=Awake" in self._adb("shell", "dumpsys", "power", timeout=15)
        if not awake:
            self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=15)
        self._adb("shell", "wm", "dismiss-keyguard", timeout=15)
        avd = self._adb("emu", "avd", "name", timeout=15).splitlines()
        # (WO-027) The foreground-service start deadline this device enforces: null is AOSP's default
        # (labkit.fgs_deadline_s reads it by API level).  The end-of-step grace is sized from it.
        fgs_ms = self._adb("shell", "device_config", "get", "activity_manager",
                           "service_start_foreground_timeout_ms", timeout=15).strip()
        # (Tier C, C8.0 review) Google Play services' version: Location Accuracy and its consent dialog are its, and it
        # updates itself (Lab1 went from 26.34.36 to 26.36.35 on 2026-10-02 while Lab2 stayed), so a capture says which.
        gms = re.search(r"^package:com\.google\.android\.gms versionCode:(\d+)", self._adb(
            "shell", "pm", "list", "packages", "--show-versioncode", "com.google.android.gms", timeout=15), re.M)
        self.info = {"avd": avd[0].strip() if avd and avd[0].strip() != "KO" else None,
                     "model": prop("ro.product.model"), "os": "Android " + prop("ro.build.version.release"),
                     "sdk": prop("ro.build.version.sdk"), "data_free_mb": free_mb,
                     "display": "awake" if awake else "woken",
                     "fgs_timeout_ms": int(fgs_ms) if fgs_ms.isdigit() else None,
                     "play_services": int(gms.group(1)) if gms else None}
        return self.info

    def terminate(self, plat, timeout=labkit.DEVICE_TIMEOUT):
        self._adb("shell", "am", "force-stop", plat["package"], timeout=timeout)

    def installed(self, prefixes):
        """(WO-191) The installed packages under any of `prefixes`: what a first step evicts is read from the device."""
        ids = [l[len("package:"):].strip() for l in self._adb("shell", "pm", "list", "packages", timeout=15).splitlines()
               if l.startswith("package:")]
        if not ids:                      # every device lists `android`: none read is a failed read, not a clean device
            raise SystemExit("`pm list packages` on %s listed nothing — the other lab apps cannot be evicted; nothing "
                             "from this step is scored" % self.serial)
        return sorted(p for p in ids if p.startswith(tuple(prefixes)))

    def evict(self, package):
        """(WO-027, C5.2) Another lab app, out of this step's way: force-stopped.  A stopped package is sent nothing
        until it is launched again, so its geofences cannot wake it mid-step (INFERRED: no Android cell of sweep
        20260928-144003 logged another lab app).  (WO-191) By package id: it may be the other lab's.  A force-stop
        also cancels a fetch app's job (MEASURED, fetch-forge WO-002: job 999 gone after `am force-stop`)."""
        self.terminate({"package": package})

    def alive(self, plat):
        """(Tier C, C6.0) The pid of plat's main process, or None when it is not running — the precondition of a
        `process: live` step (run.py).  `pidof` matches the process name exactly, so `:remote` processes are not it."""
        r = labkit.device_call(["adb", "-s", self.serial, "shell", "pidof", plat["package"]])
        pids = r.stdout.split()
        return pids[0] if r.returncode == 0 and pids else None

    def reset_app(self, plat, artifact):
        """(WO-027) Back to a fresh install: `pm clear` drops the app's data — the SDK's persisted
        state (enabled, config, the location table) included — and its runtime grants, which
        permissions() re-applies right after.  The APK stays installed; `artifact` is unused here
        and taken for symmetry with the Simulator, where a fresh state IS a reinstall."""
        self._adb("shell", "pm", "clear", plat["package"])

    # (Tier C, C7.2) Each Configuration setting a trigger changes: how to read it, the value that is
    # neutral, and how to put it back.  (Tier C, C6.1) And the master location switch `actions: location` turns off,
    # which recreates nothing but must be on for any step: `is-location-enabled` prints `true` (MEASURED on API 36).
    NEUTRAL = (("settings get secure font_weight_adjustment", "0",
                ("shell", "settings", "put", "secure", "font_weight_adjustment", "0")),
               ("settings get system font_scale", "1.0",
                ("shell", "settings", "put", "system", "font_scale", "1.0")),
               ("cmd uimode night", "Night mode: no",
                ("shell", "cmd", "uimode", "night", "no")),
               ("cmd location is-location-enabled", "true",
                ("shell", "cmd", "location", "set-location-enabled", "true")))

    # (Tier C, C5.0) What `actions: network` (and C5.3's power save) change, read in the same shell call.  Their neutral
    # is not a constant: it is what the emulator read at `lab_devices.py up` (network_default), never a forced on.
    NETWORK = (("settings get global wifi_on", "wifi"), ("settings get global mobile_data", "data"),
               ("settings get global low_power", "powerSave"))

    def neutral(self):
        """Put every Configuration setting a trigger changes back to neutral.  (Tier C, C7.2) Returns
        whether it changed anything: a revert recreates what is on screen, and run.py lets that
        settle before a launch only then — the fixed 2 s it slept on every step, 134 s a lane.  One
        read for all three; only a setting that is not exactly neutral is put, and a row that reads
        `null` (never set, or deleted) counts as not neutral.  A read that fails puts all three.

        (Tier C, C5.0) And wifi, mobile data and battery saver, read against the defaults `lab_devices.py up` recorded
        for this AVD: `self.network_state` says what was read, what differs and whether it was put back, for run.json.
        A difference is printed as a LAB DEFECT, and put back where NEUTRAL_WRITES_NETWORK (on since C5.1's pilot showed
        `svc` works on API 36); with it off, the step runs as the device is."""
        # `settings put … 0`, never `settings delete`: deleting the row does not reach the live
        # Configuration, so the next `put 300` is a silent no-op and nothing is recreated.
        reads = [read for read, _, _ in self.NEUTRAL] + [read for read, _ in self.NETWORK] + [self.FOCUS]
        now = self._adb("shell", "; ".join(reads)).strip().splitlines()
        changed = False
        for i, (_, want, put) in enumerate(self.NEUTRAL):
            if len(now) != len(reads) or now[i].strip() != want:
                self._adb(*put)
                changed = True
        default, self.network_state = self.network_default(), None
        if default is not None and len(now) == len(reads):
            got = {name: now[len(self.NEUTRAL) + k].strip() for k, (_, name) in enumerate(self.NETWORK)}
            differs = sorted(name for name in default if got.get(name) != default[name])
            self.network_state = {"read": got, "default": default, "differs": differs,
                                  "put": bool(differs) and NEUTRAL_WRITES_NETWORK}
            if differs:
                print("[neutral] LAB DEFECT: %s differ from the defaults `lab_devices.py up` recorded (%s) — %s"
                      % (", ".join("%s=%s" % (n, got.get(n)) for n in differs),
                         ", ".join("%s=%s" % (n, default[n]) for n in differs),
                         "put back" if NEUTRAL_WRITES_NETWORK else "not put back (backends.NEUTRAL_WRITES_NETWORK off)"))
            if differs and NEUTRAL_WRITES_NETWORK:
                on = lambda name: default[name] == "1"
                self.network(**{name: on(name) for name in ("wifi", "data") if name in differs})
                if "powerSave" in differs:
                    self._adb("shell", "cmd", "power", "set-mode", "1" if on("powerSave") else "0")
                changed = True
        # (Tier C, C6.1 pilot) Play Services' location-off warning, left focused by a step that turned location off.
        if len(now) == len(reads) and self.LOCATION_WARNING in now[-1]:
            print("[neutral] LAB DEFECT: Play Services' location-off warning was left on screen — dismissed")
            self.dismiss_location_warning(now[-1])
        return changed

    def network_default(self):
        """(Tier C, C5.0) The wifi / data / powerSave readings `lab_devices.py up` recorded for this AVD, or None (a
        device `up` did not start, or started before C5.0)."""
        avd = (getattr(self, "info", None) or {}).get("avd")
        if not avd:
            return None
        try:
            with open(network_default_path(avd)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def move(self, lat, lon):
        """(Tier C, C5.0) A `moves:` hop: the emulator's GNSS to (lat, lon) — `geo fix` takes LONGITUDE first, as
        begin_log's.  Refused unless adb exits 0 with no `KO` (the console's refusal): a hop that did not happen would
        score its step against a fix that never moved.  What `geo fix` prints on success is NOT CHECKED."""
        r = labkit.device_call(["adb", "-s", self.serial, "emu", "geo", "fix", str(lon), str(lat)])
        if r.returncode != 0 or "KO" in r.stdout:
            raise SystemExit("move failed (adb exit %d): %s" % (r.returncode, (r.stdout + r.stderr).strip() or "no output"))

    def network(self, wifi=None, data=None):
        """(Tier C, C5.0) `actions: network`: wifi and mobile data on (True) or off (False) through `svc`, NOT CHECKED on
        API 36 until C5.1's one-cell pilot.  Offs first, data before wifi, then ons, wifi before data: so the
        wifi -> cellular failover can never report a connection between them (the C5 surface analysis, §4.2)."""
        order = [(k, v) for k, v in (("data", data), ("wifi", wifi)) if v is False] + \
                [(k, v) for k, v in (("wifi", wifi), ("data", data)) if v is True]
        for kind, on in order:
            r = labkit.device_call(["adb", "-s", self.serial, "shell", "svc", kind, "enable" if on else "disable"])
            if r.returncode != 0:
                raise SystemExit("svc %s %s failed (exit %d): %s" % (kind, "enable" if on else "disable",
                                                                     r.returncode, (r.stdout + r.stderr).strip()))

    # (Tier C, C6.1 pilot) Play Services answers the master location switch going off with its own "No location
    # access" window (com.google.android.gms/.location.settings.LocationOffWarningActivity), which stays focused after
    # the switch is back on: MEASURED on emulator-5580 after headless-android-causes' pilot.  Read with one line of
    # `dumpsys window` (never empty, so neutral()'s batched read keeps its line count).
    LOCATION_WARNING = "LocationOffWarningActivity"
    FOCUS = "dumpsys window | grep -m1 mCurrentFocus || echo none"

    def location(self, on):
        """(Tier C, C6.1) `actions: location`: the master location switch, set (`cmd location set-location-enabled`,
        present on API 36) and read back (`is-location-enabled`, which prints `true` or `false`).  Refused unless the
        read-back is what was set: a switch that did not flip would score its step against a providerchange that never
        happened.  neutral() puts it back on.  (C6.1 pilot) Turned on, Play Services' location-off warning is dismissed;
        returns what the action's mark adds (`dismissed=…`, or nothing)."""
        want = "true" if on else "false"
        self._switch(("cmd", "location", "set-location-enabled", want), ("cmd", "location", "is-location-enabled"), want)
        return "dismissed=%s" % self.LOCATION_WARNING if on and self.dismiss_location_warning() else ""

    def dismiss_location_warning(self, focus=None):
        """(Tier C, C6.1 pilot) BACK while Play Services' location-off warning has focus (`focus`, one FOCUS line, read
        here when not given); whether it had.  Still focused after the BACK is printed as a LAB DEFECT, not refused: the
        step's evidence is not on screen.  That BACK closes it is NOT CHECKED until the next run on a device."""
        if self.LOCATION_WARNING not in (self._adb("shell", self.FOCUS) if focus is None else focus):
            return False
        self._adb("shell", "input", "keyevent", "KEYCODE_BACK")
        if self.LOCATION_WARNING in self._adb("shell", self.FOCUS):
            print("[location] LAB DEFECT: Play Services' location-off warning is still focused after BACK")
        return True

    def kill(self, plat):
        """(WO-114) `actions: kill`: SIGKILL plat's main process, as Android's low-memory killer does.  The process
        gets no callback, and ActivityManager sees a death: not a crash, and not the force-stop that `terminate()` is,
        which drops the app's service records.  What it restarts afterwards is the SDK's to decide (the value its
        services return from onStartCommand).  `run-as`, so debug builds only, which every lab app is.  Refused when
        nothing is running, when the call fails, or when that pid is still alive 2 s later (a restarted process has
        another); returns what the action's mark adds, `pid=<n>`."""
        pid = self.alive(plat)
        if pid is None:
            raise SystemExit("kill: %s has no running process" % plat["package"])
        r = labkit.device_call(["adb", "-s", self.serial, "shell", "run-as", plat["package"], "kill", "-9", pid])
        if r.returncode != 0:
            raise SystemExit("kill: run-as %s kill -9 %s failed (exit %d): %s"
                             % (plat["package"], pid, r.returncode, (r.stdout + r.stderr).strip() or "no output"))
        deadline = time.time() + 2
        while self.alive(plat) == pid:
            if time.time() >= deadline:
                raise SystemExit("kill: pid %s of %s is still running 2 s after SIGKILL" % (pid, plat["package"]))
            time.sleep(0.1)
        return "pid=%s" % pid

    def power_save(self, on):
        """(Tier C, C6.1) `actions: power_save`: battery saver, set (`cmd power set-mode`, the call neutral() puts it
        back with) and read back (`settings get global low_power`, the row neutral() reads)."""
        want = "1" if on else "0"
        self._switch(("cmd", "power", "set-mode", want), ("settings", "get", "global", "low_power"), want)

    NOTIFICATION_WAIT_S = 6

    def notification_tap(self, package, button):
        """(Tier C, C5.3; item 3b) `actions: notification_tap`: open the notification shade, tap the app's own
        notification button `<package>:id/<button>`, and close the shade again, whatever happened.  Returns None once
        it has tapped, or why it could not when NOTIFICATION_WAIT_S ran out: `absent` (no such button in the shade)
        or `no-area` (there, with empty bounds: nothing to tap).  A foreground-service notification can be up to 10 s
        late, so the action waits on the driver's own evidence first (`after:`) and this only for the shade to draw.

        (C5.3 review) A miss is returned, not raised: whether the button is drawn is the SDK's doing (its layout and
        each action id are looked up by name, and a failed lookup is logged and carried past,
        ForegroundNotification.java:214-222, :313-328), so the step is scored and its log kept."""
        node = re.compile(r'<node [^>]*resource-id="%s:id/%s"[^>]*>' % (re.escape(package or ""), re.escape(button)))
        self._adb("shell", "cmd", "statusbar", "expand-notifications")
        try:
            deadline, missed = time.time() + self.NOTIFICATION_WAIT_S, "absent"
            while True:
                found = node.search(self._dump() or "")
                bounds = found and re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', found.group(0))
                if bounds:
                    x1, y1, x2, y2 = map(int, bounds.groups())
                    if x2 > x1 and y2 > y1:
                        self._adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2), timeout=15)
                        return None
                    missed = "no-area"
                if time.time() >= deadline:
                    return missed
                time.sleep(0.5)
        finally:
            self._adb("shell", "cmd", "statusbar", "collapse")

    def _switch(self, put, read, want):
        """(Tier C, C6.1) One adb call that sets, then reads back until the read is `want`, for up to 2 s (whether
        either setting is written before its command returns is NOT CHECKED); refused on a failed call or a read that
        never gets there."""
        def call(argv):
            r = labkit.device_call(["adb", "-s", self.serial, "shell"] + list(argv))
            if r.returncode != 0:
                raise SystemExit("%s failed (exit %d): %s" % (" ".join(argv), r.returncode,
                                                              (r.stdout + r.stderr).strip() or "no output"))
            return r.stdout.strip()
        call(put)
        deadline = time.time() + 2
        while True:
            got = call(read)
            if got == want:
                return
            if time.time() >= deadline:
                raise SystemExit("%s reads %r 2 s after %s, not %r" % (" ".join(read), got, " ".join(put), want))
            time.sleep(0.2)

    def permissions(self, plat, revoke):
        for p in PERMISSIONS:
            self._adb("shell", "pm", "grant", plat["package"], "android.permission." + p)
        for p in revoke:
            self._adb("shell", "pm", "revoke", plat["package"], "android.permission." + p)

    def prompt(self, plat, names):
        """Put these permissions back to NEVER ASKED, so the next request shows the OS's first-ask
        dialog.  `pm revoke` alone is not that: a permission the user denied, or revoked in
        Settings, is not never-asked.  Android keeps its USER_SET flag through a revoke (USER_FIXED
        too, after a second denial), so the next dialog is a second strike whose deny is the
        permanent one, or there is no dialog at all.  (WO-027, permissions) So the flags go as well:
        `pm revoke` plus `pm clear-permission-flags … user-set user-fixed` was measured to restore
        NEVER_ASKED (labs/findings/motion-rationale-test.md, row 11).  A first step's `pm clear`
        may already have cleared them (not measured); this makes sure either way."""
        for p in names:
            self._adb("shell", "pm", "revoke", plat["package"], "android.permission." + p)
            self._adb("shell", "pm", "clear-permission-flags", plat["package"],
                      "android.permission." + p, "user-set", "user-fixed")

    # (Tier C, C8.0) Google's Location Accuracy consent dialog, by its Activity in a FOCUS line.
    CONSENT = "LocationSettingsCheckerActivity"

    def dismiss_stale_dialog(self, plat):
        """(WO-027) The iOS twin sweeps an orphaned alert here.  A permission dialog belongs to
        GrantPermissionsActivity, which lives in the requesting app's TASK and finishes with it, and
        the SDK's rationale is the app's own window — `force-stop` takes both.

        (Tier C, C8.0) Not Google's Location Accuracy consent dialog: it is Play services' own
        Activity, and it outlives the app that raised it — still focused 8 s after `am force-stop`
        (MEASURED 2026-10-02, emulator-5582).  Left there, the next launch could come up under it, and
        the host would answer a window nothing is waiting on.  BACK while it has focus closes it
        (MEASURED the same day, by hand: focus went to the launcher), as dismiss_location_warning does
        Play services' other window.  One FOCUS read a step (20-60 ms); returns the window swept, for
        `swept=`, or None.  (C8.0 review) Still focused after the BACK, the step is refused: it would
        launch under a window nothing waits on."""
        if self.CONSENT not in self._adb("shell", self.FOCUS):
            return None
        self._adb("shell", "input", "keyevent", "KEYCODE_BACK")
        if self.CONSENT in self._adb("shell", self.FOCUS):
            raise SystemExit("Play services' Location Accuracy consent dialog was left up by an earlier step and is "
                             "still focused after BACK; nothing from this step is scored")
        print("[dialog] swept a stale Location Accuracy consent dialog (BACK)")
        return self.CONSENT

    def answer_dialog(self, action, plat):
        """Answer the dialog this action names, if it is up.  Returns a Tap (the button label
        tapped, as a str) or None.  `uiautomator dump` is the only way to READ it: the permission
        controller is another process, so the app cannot see it and a blind tap at fixed
        coordinates would "succeed" against a dialog that was never there.

        Its windows, one mechanism (WO-027):
          * `allow`/`deny`/`always` are the OS's — the grant dialog, or the app-permission page it
            opens for ACCESS_BACKGROUND_LOCATION, both drawn by the permission controller.
          * `rationale` is the SDK's own Dialog, inside the app, which the permission-controller
            guard would reject forever; its label match IS its guard.
          * `cancel` is that same Dialog's other button.  "Cancel" is anybody's word, so its guard
            is the rationale's positive button in the same dump (RATIONALE_POSITIVE).  The Dialog
            is setCancelable(false): Back cannot close it, only this can.
          * `back` is not a button at all: the background grant is delivered only when that page
            is LEFT (onRequestPermissionsResult), so the flow hangs without a Back press.  Sent
            only while the page is still up, so a missed step cannot look like a completed one.
          * (Tier C, C8.0) `turn-on`/`no-thanks` are Play services' window, Google's Location
            Accuracy consent dialog: guarded by Play services' package on the dump (_play_services),
            which the permission-controller guard would reject forever.  `approximate` declines the
            OS's precise-location upgrade, under the permission-controller guard like `deny`.

        (WO-027, permissions) A tap is not an answer until the window has gone.  Without that
        check, one closing window could be tapped twice, and the after-sequence watcher would
        count it as an undeclared one.  So the host re-dumps and applies android_answered: its
        rule differs by action, because the app-permission page stays up after `always` until
        `back` leaves it.

        (WO-027, permissions) A tap whose re-dumps could not be read at all is neither answered
        nor swallowed, so it is kept pending.  The next call for the same action judges it
        against the first dump it reads, before tapping anything: if the window has gone, it
        returns that same Tap, with its original tapped_ms.  Without this, a delivered tap whose
        window closed while uiautomator failed found nothing left to tap on the next poll, and
        the step went red as `missed=`.  Any other action, and the next step (begin_log), drop it.

        (Tier C, C7.5) The re-dump follows the tap at once, with no sleep.  What makes that safe is
        uiautomator itself: a dump reads the screen only after 1 s without accessibility events
        (its snapshot came >= 1.009 s after UiAutomation connected in all 671 dumps on record), so a
        dump started at the tap reads it >= ~1.1 s later and after the transition has settled.
        Tapped windows left within 0.05 s, the app-permission page 0.27-0.55 s after Back, and each
        next window was up within 0.22 s (all 143 recorded taps, 2026-09-23..25).  A re-dump that
        still shows the window keeps the tap pending too, as an unreadable one does, so the next
        call confirms it or, the window still up about 3 s after the tap, taps again.  That also
        widens, from about 2 s to about 5 s, the time in which a swallowed tap is credited if its
        window leaves for some other reason; no scenario is known to do both.  And the dump that
        confirmed a tap is the next call's first look, once: the proof the window left is the same
        read that finds the next window.  When that read does not show this action's window, the
        same call reads the screen afresh, so a late window costs no more than it did with the
        sleep.  Only answer_dialog uses it; visible_dialog always reads the screen itself."""
        if action == "keep":
            raise SystemExit("`dialog: keep` is iOS-only — tapping Android's \"Allow only while "
                             "using the app\" while ACCESS_BACKGROUND_LOCATION is held revokes it "
                             "and kills the running app (measured 2026-09-25)")
        pending, self._pending = self._pending, None
        if pending and pending[0] != action:
            pending = None
        fresh, self._fresh = self._fresh, None
        xml = fresh if fresh is not None else self._dump()
        # A reused dump may predate this window: then read the screen afresh, once, in this call.
        again = (lambda: self.answer_dialog(action, plat)) if fresh is not None else (lambda: None)
        if xml is None:
            self._pending = pending          # could not look: still unconfirmed
            return None
        if pending and android_answered(action, pending[1], pending[2], xml):
            self._fresh = xml
            return pending[1]
        system = _system(xml)
        if action == "back":
            if not system:
                return again()
            self._adb("shell", "input", "keyevent", "KEYCODE_BACK")
            tapped = Tap("KEYCODE_BACK", int(time.time() * 1000))
        else:
            if action == "cancel":
                shown = {_canon(text) for text in dump_labels(xml)}
                if not any(_canon(label) in shown for label in RATIONALE_POSITIVE):
                    return again()
            elif action in PLAY_SERVICES_ACTIONS:
                # (Tier C, C8.0) Google's consent dialog is Play services' window, not the permission controller's.
                if not _play_services(xml):
                    return again()
            elif action != "rationale" and not system:
                return again()
            tapped = None
            for label in DIALOG_BUTTONS[action]:
                m = _label_re(label).search(xml)
                if not m:
                    continue
                x1, y1, x2, y2 = (int(g) for g in m.groups()[1:])
                self._adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                tapped = Tap(label, int(time.time() * 1000))
                break
            if tapped is None:
                return again()
        after = self._dump()
        if after is None:                # one retry: a dump that failed is not a window still up
            after = self._dump()
        if android_answered(action, tapped, xml, after):
            self._fresh = after
            return tapped
        self._pending = (action, tapped, xml)
        return None

    def _dump(self):
        """One `uiautomator dump` of the active window, or None when it could not be read.  The
        file is removed first: a dump that fails leaves the previous one in place, and a stale
        file would show a window that has already gone."""
        xml = self._adb("shell", "rm -f /sdcard/lab-dialog.xml; "
                        "uiautomator dump /sdcard/lab-dialog.xml >/dev/null 2>&1; "
                        "cat /sdcard/lab-dialog.xml 2>/dev/null")
        return xml if "<hierarchy" in xml else None

    def visible_dialog(self, plat):
        """(WO-027, permissions) Which known dialog is up now, by its buttons: dialog_signature
        over one dump, with no tap.  Returns None when no known dialog is up.  Raises
        RuntimeError when the screen could not be read: for an absence assertion, "could not
        look" must not count as "nothing there"."""
        xml = self._dump()
        if xml is None:
            raise RuntimeError("uiautomator dump could not be read")
        return dialog_signature(dump_labels(xml))

    # (Tier C, C7.7, Chris 2026-09-26) Google Location Accuracy is Play services' consent, with no setting to
    # write: it is turned on or off on Play services' own screen, by its switch.  The lab's default is on, as on
    # emulator-5554 (off, the SDK's high-accuracy check raises Google's consent dialog over the app); a scenario
    # may ask for off to test that dialog.  It persists in the AVD's /data.
    ACCURACY_ACTION = "com.google.android.gms.location.settings.LOCATION_ACCURACY"

    def location_accuracy(self):
        """Is Google Location Accuracy on — `dumpsys location`'s network provider enabled?  (Tier C, C8.0 review) None
        when the read shows no network provider block at all — an adb call that failed (labkit.adb returns its stdout
        whatever the exit) or output cut short: "could not read" is not "off", now that run.set_accuracy reads it
        before every Android launch."""
        out = self._adb("shell", "dumpsys", "location", timeout=30)
        if "network provider:" not in out:
            return None
        # The network provider's own block: from its heading to the next provider's
        block = out.split("network provider:", 1)[1].split(" provider:", 1)[0]
        return re.search(r"\benabled=true\b", block) is not None

    def set_location_accuracy(self, on):
        """Turn Google Location Accuracy `on` (True) or off, the way a person does, then back to the launcher.
        None once `dumpsys location` agrees, else why not."""
        if self.location_accuracy() == on:
            return None
        self._adb("shell", "am", "start", "-W", "-a", self.ACCURACY_ACTION, timeout=30)
        seen = tapped = False
        try:
            for _ in range(10):
                node = re.search(r'<node [^>]*resource-id="android:id/switch_widget"[^>]*>', self._dump() or "")
                if node:
                    seen = True
                    if not tapped and ('checked="true"' in node.group(0)) != on:
                        x1, y1, x2, y2 = map(int, re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                                                            node.group(0)).groups())
                        self._adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2), timeout=15)
                        tapped = True
                if seen and self.location_accuracy() == on:
                    return None
                time.sleep(1)
            state = "on" if on else "off"
            return ("not %s 10 s after tapping its switch" % state if tapped else
                    "its switch reads %s, yet the network provider does not" % state if seen else
                    "no switch on %s" % self.ACCURACY_ACTION)
        finally:
            self._adb("shell", "input", "keyevent", "KEYCODE_HOME", timeout=15)

    def permission_flags(self, plat):
        """(WO-027, permissions) The OS's own record of the permission state a step starts from,
        read from `dumpsys package`: granted or not, plus the USER_SET/USER_FIXED strike flags
        that decide whether the next request shows a dialog (permission_flags_summary)."""
        return permission_flags_summary(self._adb("shell", "dumpsys", "package", plat["package"],
                                                  timeout=30))

    def begin_log(self):
        self._pending = None             # a tap left unconfirmed belongs to the previous step
        self._fresh = None               # and so does the dump that confirmed its last one
        self._adb("logcat", "-c")
        self._adb("emu", "geo", "fix", str(labkit.STANDING_FIX[1]), str(labkit.STANDING_FIX[0]))   # longitude first
        # (WO-027) A live copy of the log, READ only to decide when a step may end on its evidence
        # (run.py).  One stream, not `logcat -d` every second — polling would add load, and a loaded
        # machine manufactures defects (README).  The capture itself still comes from end_log.
        self._stop_stream()
        self._stream_path = os.path.join(labkit.HARNESS, ".claims", "%s-%d.logcat" % (self.serial, os.getpid()))
        os.makedirs(os.path.dirname(self._stream_path), exist_ok=True)
        self._stream = subprocess.Popen(["adb", "-s", self.serial, "logcat", "-v", "time"],
                                        stdout=open(self._stream_path, "w"), stderr=subprocess.DEVNULL,
                                        stdin=subprocess.DEVNULL)

    def peek(self):
        """The log so far, from the live stream (raw logcat lines)."""
        if not self._stream_path or not os.path.exists(self._stream_path):
            return []
        with open(self._stream_path, errors="replace") as f:
            text = f.read()
        # adb writes in chunks that ignore line ends: an unfinished last line (a torn
        # `event-arm … outcome=` reads as a non-ok arm) must never count toward "complete".
        lines = text.splitlines()
        return lines if text.endswith("\n") else lines[:-1]

    def _stop_stream(self):
        if self._stream is not None and self._stream.poll() is None:
            self._stream.terminate()
            try:
                self._stream.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._stream.kill()
                self._stream.wait()
        self._stream = None
        if self._stream_path and os.path.exists(self._stream_path):
            os.remove(self._stream_path)
        self._stream_path = None

    def mark(self, text):
        labkit.mark(self.serial, text)

    def launch(self, plat, component, extras):
        """`extras` is {key: str|bool|int}; typed here because `am start` wants a flag per type."""
        flat = []
        for key, value in extras.items():
            flag = {bool: "--ez", int: "--ei"}.get(type(value), "--es")
            flat += [flag, key, str(value).lower() if isinstance(value, bool) else str(value)]
        argv = ["am", "start", "-W", "-n", "%s/%s" % (plat["package"], component), *flat]
        # (Tier C, C4.4 pilot) adb sends a shell command as ONE request, "shell,v2,raw:" + the argv joined by
        # spaces, and past labkit.ADB_COMMAND_LIMIT it exits 1 with `error: closed` on stderr and nothing runs.
        # Refused here, by size, before anything is sent; scenario() holds a step's extras under it at load.
        size = len(("shell,v2,raw:" + " ".join(argv)).encode())
        if size > labkit.ADB_COMMAND_LIMIT:
            raise SystemExit("launch failed: the `am start` command is %d B, over the %d B adb sends in one "
                             "request — split the step" % (size, labkit.ADB_COMMAND_LIMIT))
        # A launch that did not happen is never scored.  This read stdout for "Error" alone, and adb's own
        # failure is on stderr: the pilot's 72,460 B step ran 25 s against no app and read batch-incomplete.
        # `-W` prints `Status:` once the launch is done (ok on a cold, warm or delivered-to-top start).
        r = labkit.device_call(["adb", "-s", self.serial, "shell", *argv])
        if r.returncode != 0 or "Error" in r.stdout or "Status:" not in r.stdout:
            raise SystemExit("launch failed (adb exit %d): %s"
                             % (r.returncode, (r.stdout + r.stderr).strip() or "no output"))

    def fire(self, trigger, plat, component, on=True):
        """`on=False` puts the setting back, which recreates the Activity a second time — that
        is how a scenario asks for more than one recreation (trigger_repeat)."""
        if trigger == "bold":
            self._adb("shell", "settings", "put", "secure", "font_weight_adjustment",
                      "300" if on else "0")
        elif trigger == "fontscale":
            self._adb("shell", "settings", "put", "system", "font_scale", "1.3" if on else "1.0")
        elif trigger == "night":
            self._adb("shell", "cmd", "uimode", "night", "yes" if on else "no")
        elif trigger == "finish":
            self._adb("shell", "am", "start", "-n", "%s/%s" % (plat["package"], component),
                      "--ez", "finishNow", "true")
        elif trigger == "none":
            pass
        else:
            raise SystemExit("unknown trigger %r" % trigger)

    def snapshot(self, out):
        # (WO-027) Close the notification shade first: if it is open — a leftover from a previous
        # session, or a notification the SDK posted — the screenshot is of the shade, not the app.
        self._adb("shell", "cmd", "statusbar", "collapse")
        time.sleep(0.5)
        with open(os.path.join(out, "top-activity.txt"), "w") as f:
            f.write(self._adb("shell", "dumpsys", "activity", "activities"))
            f.write(self._adb("shell", "dumpsys", "window", "windows"))
        with open(os.path.join(out, "after-trigger.png"), "wb") as f:
            f.write(labkit.device_call(["adb", "-s", self.serial, "exec-out", "screencap", "-p"],
                                       timeout=30, text=False).stdout)

    def end_log(self, plat):
        self._stop_stream()
        return self._adb("logcat", "-d", "-v", "time")

    def sdk_db(self, plat):
        return labkit.device_call(["adb", "-s", self.serial, "exec-out", "run-as", plat["package"],
                                   "cat", "databases/transistor_log.db"], text=False).stdout

    def push_verdict(self, plat, text):
        """(WO-027) The host's verdict for the device view (labkit.verdict_view), into the app's own
        files dir — `run-as`, so debug builds only, which every lab app is.  `None` clears it.
        Written to a temporary name and renamed, so the driver's read (every 0.25 s since Tier C, C7.6)
        never sees half a file.
        Base64 on the command line: a verdict is a few KB, and the payload needs no shell quoting."""
        if text is None:
            cmd = "rm -f files/lab-verdict.json"
        else:
            cmd = ("mkdir -p files && echo %s | base64 -d > files/.lab-verdict.tmp && "
                   "mv files/.lab-verdict.tmp files/lab-verdict.json" % base64.b64encode(text.encode()).decode())
        # check=True: a push that failed (run-as refused, a full disk) raises, so run.py's show()
        # prints it and the live pusher retries — adb passes the device's exit status through.
        self._adb("shell", "run-as %s sh -c '%s'" % (plat["package"], cmd), timeout=15, check=True)

    def fgs_all_foreground(self, plat, started):
        """(WO-027) The framework's word, not the SDK's: has every foreground service the app started
        reached the foreground?  The grace ends on the SDK's promotion log line, which comes from the
        code under test — so the step also asks ActivityManager each time the grace runs out before
        the cap.  Android 16 prints `* ServiceRecord{… <pkg>/<Service> …}` per record,
        `isForeground=true` once it is, and `fgRequired=true` while its startForeground() is still
        owed (AOSP ServiceRecord.dump).  Only records at risk are judged: `started`, the classes the
        log shows started as foreground services, and any record still owing its promotion — a bound
        JobService (WorkManager's SystemJobService, alive for api-smoke's background task) has no
        deadline to miss."""
        out = self._adb("shell", "dumpsys", "activity", "services", plat["package"], timeout=15, check=True)
        records = re.split(r"\n\s*\* ServiceRecord\{", "\n" + out)[1:]
        for r in records:
            head = r.split("\n", 1)[0]
            if " %s/" % plat["package"] not in head:
                continue
            cls = head.split(" %s/" % plat["package"], 1)[1].split()[0].split("}")[0].rsplit(".", 1)[-1]
            if "fgRequired=true" in r or (cls in started and "isForeground=true" not in r):
                return False
        return True

    def _free_mb(self):
        rows = self._adb("shell", "df", "-k", "/data", timeout=15).strip().splitlines()
        return int(rows[-1].split()[3]) // 1024 if len(rows) > 1 else None

    def _last_update(self, plat):
        """The installed package's lastUpdateTime, or None when it is not installed."""
        m = re.search(r"lastUpdateTime=(.+)", self._adb("shell", "dumpsys", "package", plat["package"],
                                                         timeout=30))
        return m.group(1).strip() if m else None

    # Android refuses an install that would leave /data under its low-storage reserve.
    INSTALL_HEADROOM_MB = 350

    def install(self, plat, artifact):
        """(WO-027) Install — unless the device already has THIS build.  Every run re-installed the
        identical APK, and `install -r` of Flutter's 179 MB debug build needs room for two copies:
        on a 5.8 GB AVD with ~620 MB free it failed four times on 2026-09-24, as a bare "Performing
        Streamed Install" because the reason went to stderr.  Skipped only when the source stamp
        run.py wrote for this artifact AND the device's lastUpdateTime both match what the last
        install here recorded — anything else that touched the app (another session, the sweep's
        uninstall) changes the time, and it installs.  `pm clear` still resets the app's state at
        a scenario's first step (reset_app); a reinstall added nothing to that."""
        record = os.path.join(devices.CLAIMS, "installed-%s-%s.json" % (self.serial, plat["package"]))
        stamp_file = artifact + ".lab-stamp"
        stamp = open(stamp_file).read().strip() if os.path.exists(stamp_file) else None
        updated = self._last_update(plat)
        try:
            with open(record) as f:
                held = json.load(f)
        except (OSError, ValueError):
            held = {}
        if stamp and updated and held.get("stamp") == stamp and held.get("updated") == updated:
            print("[install] skipped: %s already has this build (installed %s)" % (plat["package"], updated))
            return
        apk_mb = os.path.getsize(artifact) // (1024 * 1024)
        need = (2 if updated else 1) * apk_mb + self.INSTALL_HEADROOM_MB     # replacing keeps two copies
        free = self._free_mb()
        if free is not None and free < need:
            raise SystemExit("%s has %d MB free on /data; installing this %d MB APK needs %d%s — "
                             "uninstall another lab app or grow the AVD's /data"
                             % (self.serial, free, apk_mb, need,
                                " (two copies while it replaces the installed one)" if updated else ""))
        r = labkit.device_call(["adb", "-s", self.serial, "install", "-r", "-g", artifact], timeout=300)
        if "Success" not in r.stdout:
            # adb prints the reason — INSTALL_FAILED_INSUFFICIENT_STORAGE and the like — on stderr.
            reason = r.stderr.strip() or r.stdout.strip()
            code = re.search(r"Failure \[[^\]]*\]", reason)
            raise SystemExit("install failed: %s" % (code.group(0) if code else " ".join(reason.split())[-240:]))
        os.makedirs(devices.CLAIMS, exist_ok=True)
        with open(record, "w") as f:
            json.dump({"stamp": stamp, "updated": self._last_update(plat), "artifact": artifact}, f)

    def native_resolved(self, plat, artifact):
        return None                      # asserted at build time from the plugin's println

    def close(self):
        self._stop_stream()


PERMISSIONS = ["ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION",
               "ACTIVITY_RECOGNITION", "POST_NOTIFICATIONS"]

# The Android permission names a scenario revokes, in simctl's privacy vocabulary.  Only the
# revokes whose meaning is the same: `revoke location-always` denies location ENTIRELY, which is
# not Android's ACCESS_BACKGROUND_LOCATION (when-in-use kept) — that one refuses until a
# downgrade to when-in-use is measured on a device.
# (Tier C, C5.0) Whether Adb.neutral() puts wifi, data and battery saver back to their recorded defaults (the Tier C
# design, decision 6: "neutral() adopts svc").  (C5.1) On since its pilot: `svc` took both Lab AVDs off and back on
# (sweeps 20260928-094825 and -095012), and with it off one red step left Lab2 offline for the next two.
NEUTRAL_WRITES_NETWORK = True


def network_default_path(avd):
    """(Tier C, C5.0) Where `lab_devices.py up` records an AVD's wifi / data / powerSave readings: beside the machine's
    claims, since the AVD is the machine's, not a checkout's."""
    return os.path.join(os.path.dirname(devices.CLAIMS), "neutral-%s.json" % avd)


PRIVACY = {"ACCESS_FINE_LOCATION": "location-always", "ACCESS_COARSE_LOCATION": "location-always",
           "ACTIVITY_RECOGNITION": "motion"}

# (WO-027) The buttons a `dialog:` step answers, most permissive first.  A permission dialog is the
# OS's, not the app's: its wording is the platform's and changes between versions, so the answer is
# a RANKED list of labels rather than one string — the first that is actually on screen is tapped,
# and the capture records WHICH, because "we tapped something" is not evidence.
DIALOG_BUTTONS = {
    "allow": ["Allow all the time", "Allow While Using App", "While using the app",
              "Allow Once", "Only this time", "Allow", "OK"],
    "deny": ["Don’t Allow", "Don't Allow", "Deny", "Don’t allow", "Don't allow"],
    # (WO-027) The SDK's OWN dialog, not the OS's: on Android 11+ the core shows
    # `backgroundPermissionRationale` before it may ask for ACCESS_BACKGROUND_LOCATION, drawn in
    # the app's process (TSBackgroundPermissionRationale: `new Dialog(...)`, setCancelable(false)).
    # Its default positive text renders WITH the quotes the template puts there.
    "rationale": ['Change to "Allow all the time"', "Change to Allow all the time",
                  "Allow all the time"],
    # The upgrade itself: a RadioButton on Android's app-permission page (the OS opens it instead
    # of a second dialog), an alert button on iOS.  Never "while using" — that is the thing this
    # action exists to upgrade away from.
    "always": ["Allow all the time", "Change to Always Allow", "Always Allow"],
    # (WO-027, permissions) Declining the iOS upgrade alert, keeping When In Use.  iOS only:
    # Android's nearest button, "Allow only while using the app" on the app-permission page, is
    # left out on purpose, and labkit refuses `keep` there.  Tapping it while
    # ACCESS_BACKGROUND_LOCATION is held revokes background and kills the running app.
    "keep": ["Keep Only While Using"],
    # (WO-027, permissions) The SDK's OWN window on both OSes: the Android rationale's negative
    # button (TSBackgroundPermissionRationale DEFAULT_NEGATIVE_ACTION), and the iOS Settings
    # alert's (TSGeolocationConfig.m:35).  The word is generic, so it is never tapped on its label
    # alone; see the guards in each answer_dialog.
    "cancel": ["Cancel"],
    # (Tier C, C8.0) Google's Location Accuracy consent dialog: Play services' LocationSettingsCheckerActivity, which
    # the SDK's settings check raises (startResolutionForResult) while Location Accuracy is off — "For a better
    # experience, your device will need to use Location Accuracy".  MEASURED 2026-10-02 on emulator-5582 (API 36,
    # Play services 26.34.36): `android:id/button1` "Turn on", `android:id/button2` "No thanks".  Android only.
    "turn-on": ["Turn on"],
    "no-thanks": ["No thanks"],
    # (Tier C, C8.0) Declining Android's precise-location upgrade: with COARSE held, a request for FINE shows "Change
    # <app>’s location access from approximate to precise?" — "Change to precise location", "Only this time", "Keep
    # approximate location" (permission_deny_button), MEASURED 2026-10-02 on emulator-5582 (API 36).  No `deny` label
    # is on it.  Android only.
    "approximate": ["Keep approximate location"],
}

# (WO-027, permissions) The rationale's positive button (TSBackgroundPermissionRationale
# DEFAULT_OK_BUTTON, `Change to "{backgroundPermissionOptionLabel}"`) is what vouches for its
# "Cancel".  The bare "Allow all the time" is left out: it is also the radio on the OS's
# app-permission page, and must never vouch for a Cancel.
RATIONALE_POSITIVE = [l for l in DIALOG_BUTTONS["rationale"] if l not in DIALOG_BUTTONS["always"]]

# (WO-027, permissions) The titles of the SDK's iOS Settings alert (TSGeolocationConfig.m:32-33,
# the defaults; a scenario that sets `locationAuthorizationAlert` would have to pass its own).
# It is a UIAlertController in the app's own process, so the display-name owner guard (built for
# system alerts that outlive their app) does not apply to it, and its text never names the app.
# A title on screen is what makes its "Cancel" and "Settings" the SDK's.
SDK_ALERT_TITLES = ["Location services are off", "Background location is not enabled"]

# (WO-027) A label as it appears INSIDE a uiautomator dump's attribute.  The rationale's default
# button carries literal double quotes — `Change to "Allow all the time"` — and uiautomator does
# NOT escape them: it SWAPS THE DELIMITER, writing `text='Change to "Allow all the time"'`.
# Measured 2026-09-23, after a regex anchored on `text="` polled a dialog that was plainly on
# screen for 34 s and reported "no rationale dialog appeared".  So: entities for the three
# characters XML always escapes, the quote left alone, and the delimiter matched either way.
def xml_text(label):
    return label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Either delimiter, then anywhere in the same node, its bounds: [x1,y1][x2,y2].
def _label_re(label):
    return re.compile(r'text=(["\'])%s\1[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
                      % re.escape(xml_text(label)), re.I)


class Tap(str):
    """(WO-027, permissions) What answer_dialog returns when it answered: the label tapped, a str
    exactly as before, so `button.replace(" ", "_")` still works.  It also carries `tapped_ms`:
    host epoch milliseconds (time.time()) taken when the tap command RETURNED, so the tap
    happened at or before it.  It is taken BEFORE the gone-check, which is what puts the host's
    mark after the tap: on iOS the 0.6 s wait and one read, about 1.1 s (review of D4); on Android
    (Tier C, C7.5) one uiautomator dump with no wait, typically ~2 s and longer when the dump waits
    on a busy screen.  A tap whose re-dumps could not be read, or on Android still showed the
    window, is confirmed on a later poll (answer_dialog's pending tap), and its mark lands later
    still, with the same tapped_ms."""

    def __new__(cls, label, tapped_ms):
        tap = str.__new__(cls, label)
        tap.tapped_ms = tapped_ms
        return tap


def _canon(text):
    """A label as compared: case, runs of whitespace and the apostrophe's two spellings folded."""
    return " ".join(str(text or "").replace("’", "'").lower().split())


# (WO-027, permissions) Every button a window the lab knows can show: every DIALOG_BUTTONS label
# (the motion prompts' buttons are among them: Android's "Allow"/"Don’t allow", iOS's
# "Don’t Allow"/"OK"), plus the iOS Settings alert's "Settings".  The GENERIC ones could be
# anybody's, so they never make a window alone.  They count only beside a specific button, or
# ("Settings" + "Cancel") under an SDK_ALERT_TITLES title.
KNOWN_BUTTONS = {_canon(l) for labels in DIALOG_BUTTONS.values() for l in labels} | {"settings"}
GENERIC_BUTTONS = {"allow", "ok", "cancel", "settings"}


def dump_labels(dump):
    """Every node's `text` in a uiautomator dump, entities decoded.  Either quote delimiter:
    uiautomator swaps it rather than escaping a quote (see xml_text)."""
    return [html.unescape(m.group(2))
            for m in re.finditer(r"""(?:^|\s)text=(["'])(.*?)\1""", dump or "", re.S) if m.group(2)]


def dialog_signature(labels):
    """(WO-027, permissions) Which known dialog these on-screen labels show, or None.  A window is
    recognised by its BUTTONS, never by the app's name or a package string.  The frontmost app's
    own tree is in every iOS dump.  On Android, a permanent denial launches
    GrantPermissionsActivity, which finishes about 113 ms later and never shows a button
    (FINDINGS.md row 4).  The signature is the known buttons it shows, sorted and joined with
    "|", with spaces as `_` and no quotes, so it can go into a mark as it is.  An example:
    `dont_allow|only_this_time|while_using_the_app`."""
    shown = {_canon(l) for l in labels}
    buttons = shown & KNOWN_BUTTONS
    titled = any(_canon(t) in text for text in shown for t in SDK_ALERT_TITLES)
    if not (buttons - GENERIC_BUTTONS) and not (titled and {"settings", "cancel"} <= buttons):
        return None
    return "|".join(sorted({re.sub(r"['\"“”]", "", b).replace(" ", "_") for b in buttons}))


def window_gone(label, before, after):
    """(WO-027, permissions) Has the window a tap on `label` answered gone?  `before` is the
    labels the tap was chosen from.  `after` is a re-read, or None when it could not be read,
    which is not "gone".  The window has gone when the label has, or when a DIFFERENT known
    window now shows it.  That second case matters for the motion dialog: with ACTIVITY_RECOGNITION
    un-granted, a bridge that drops requestPermission's argument brings it up right after the
    location dialog, with the same "Don’t allow".  Judged on the label alone, `deny` would be
    reported unanswered and would then tap the motion dialog too, hiding the undeclared window
    the scenario exists to catch."""
    if after is None:
        return False
    if _canon(label) not in {_canon(l) for l in after}:
        return True
    return dialog_signature(after) != dialog_signature(before)


def _system(dump):
    """Is this dump the permission controller's (the OS's dialog or its app-permission page)?"""
    return "com.android.permissioncontroller" in dump or "GrantPermissions" in dump


# (Tier C, C8.0) The actions that answer Google's Location Accuracy consent dialog, and its guard: Play services'
# package on the dump's nodes (MEASURED: every node of the dialog is com.google.android.gms's).
PLAY_SERVICES_ACTIONS = ("turn-on", "no-thanks")


def _play_services(dump):
    """Is this dump Google Play services' window (the Location Accuracy consent dialog)?"""
    return re.search(r"""package=["']com\.google\.android\.gms["']""", dump or "") is not None


def radio_checked(dump, label):
    """(WO-027, permissions) Does the radio whose text is `label` read checked="true" on Android's
    app-permission page?  True or False only when the row is unambiguous: the nearest ancestor
    of the text node (up to four levels) that holds exactly one checkable node.  None when it
    cannot tell: an unreadable dump, no such text, a plain button (the API 29 dialog), or a row
    that reaches past its own radio to the "Use precise location" switch or a sibling radio."""
    try:
        root = ET.fromstring(dump or "")
    except ET.ParseError:
        return None
    parent = {child: node for node in root.iter() for child in node}
    for node in root.iter("node"):
        if _canon(node.get("text")) != _canon(label):
            continue
        row = node
        for _ in range(4):
            checkable = [n for n in row.iter("node") if n.get("checkable") == "true"]
            if len(checkable) == 1:
                return checkable[0].get("checked") == "true"
            if len(checkable) > 1 or row not in parent:
                return None
            row = parent[row]
        return None
    return None


def android_answered(action, label, before, after):
    """(WO-027, permissions) H7: did the Android tap answer its window?  `before` is the dump the
    tap was chosen from, `after` a re-dump (None when unreadable).  One rule per action:
      * allow / deny / rationale / cancel: the window has gone (window_gone).
      * always: accepted unless the tapped radio positively reads unchecked (radio_checked is
        False: the tap was swallowed, and a second tap on a radio is harmless).  The page stays
        up until `back`, so "still up" is expected here and never counts against it.
      * back: the permission controller's window has gone.
    Never "is a permission-controller window up" except for back: that alone would never
    confirm `always`.  And `allow` is never re-run against the page, because its first label,
    "Allow all the time", would grant BACKGROUND there."""
    if action == "back":
        return after is not None and not _system(after)
    if action == "always":
        return radio_checked(after, label) is not False
    return window_gone(label, dump_labels(before), None if after is None else dump_labels(after))


# (WO-027, permissions) The permissions whose OS state a step's permission-flags snapshot records.
FLAG_PERMISSIONS = ["ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION",
                    "ACTIVITY_RECOGNITION"]
# Flags with no bearing on whether the next request asks: usage-indicator sensitivity, and the
# install-time exemptions of a restricted permission, which ACCESS_BACKGROUND_LOCATION is.
_QUIET_FLAGS = re.compile(r"^(USER_SENSITIVE_WHEN_(GRANTED|DENIED)|APPLY_RESTRICTION|RESTRICTION_\w+)$")


def permission_flags_summary(dumpsys):
    """(WO-027, permissions) `dumpsys package <pkg>`'s runtime-permission lines, for example
    `android.permission.ACCESS_FINE_LOCATION: granted=false, flags=[ USER_SET|USER_FIXED]`
    (FINDINGS.md's ground truth), cut down to one mark-safe string with no spaces, quotes or
    `=`.  One `NAME:granted|denied:FLAGS` per FLAG_PERMISSIONS entry, joined with ",".  FLAGS is
    "|"-joined with _QUIET_FLAGS dropped, or "-" when none is left, and a permission the dump
    does not list reads `NAME:?`.  An example:
    `ACCESS_FINE_LOCATION:denied:USER_SET,ACCESS_COARSE_LOCATION:denied:USER_SET,
    ACCESS_BACKGROUND_LOCATION:denied:-,ACTIVITY_RECOGNITION:denied:-` (one line).  None when
    the dump lists none of them, e.g. the package is not installed.  The first user's section wins."""
    found = {}
    for m in re.finditer(r"^\s*android\.permission\.(\w+): granted=(true|false)(?:, flags=\[([^\]]*)\])?",
                         dumpsys or "", re.M):
        found.setdefault(m.group(1), (m.group(2) == "true", (m.group(3) or "").replace("|", " ").split()))
    if not any(p in found for p in FLAG_PERMISSIONS):
        return None
    parts = []
    for p in FLAG_PERMISSIONS:
        if p not in found:
            parts.append("%s:?" % p)
            continue
        granted, flags = found[p]
        flags = [f for f in flags if not _QUIET_FLAGS.match(f)]
        parts.append("%s:%s:%s" % (p, "granted" if granted else "denied", "|".join(flags) or "-"))
    return ",".join(parts)


# `log stream --style ndjson`: one JSON object per event — "timestamp": "2026-09-21 22:18:01.069579-0400",
# "processImagePath", "processID", "subsystem", "eventMessage".  Never the compact style: it pads
# single-letter level codes to two columns and folds multi-line messages, and a regex over it
# silently dropped every Error/Fault line.
def to_logcat(ndjson):
    """os_log events in logcat `-v time` shape, so one parser reads both.

    A `LAB …` message (the shim's NSLog, or a mark the harness sent through syslog) becomes a
    line under the LAB tag, exactly as `Log.i("LAB", …)` renders; the SDK's mirrored lines go
    under TSLocationManager; anything else keeps its process name as the tag.  The Flutter
    engine prefixes Dart prints with "flutter: " — stripped, so `LAB app=` lands where the
    Android drivers put it."""
    out = []
    for line in ndjson.splitlines():
        if not line.startswith("{"):
            continue                     # the "Filtering the log data…" header
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("eventType") not in (None, "logEvent"):
            continue
        stamp = ev.get("timestamp", "")[5:23]          # "09-21 22:18:01.069"
        pid = ev.get("processID", 0)
        msg = " ".join(str(ev.get("eventMessage", "")).splitlines())
        if msg.startswith("flutter: "):
            msg = msg[9:]
        if ev.get("subsystem") == SDK_SUBSYSTEM:
            out.append("%s I/%s(%5s): %s" % (stamp, SDK_TAG, pid, msg))
        elif msg.startswith("LAB "):
            out.append("%s I/LAB     (%5s): %s" % (stamp, pid, msg[4:]))
        else:
            proc = os.path.basename(ev.get("processImagePath") or "?")
            out.append("%s I/%s(%5s): %s" % (stamp, proc, pid, msg))
    return "\n".join(out) + ("\n" if out else "")


REPORTS = os.path.expanduser("~/Library/Logs/DiagnosticReports")


def crash_reports(bundle, since, pid=None):
    """`LAB point=crash …` lines for every crash report the Simulator filed for `bundle` after
    `since`, stamped with the report's own capture time.  A Swift trap leaves nothing in the
    app's os_log; the .ips under ~/Library/Logs/DiagnosticReports is the durable evidence.

    (WO-027) With `pid`, only THIS process's reports: ReportCrash files a report up to ~10.5 s
    after the crash (measured), so matching by file time alone charged a late report to the NEXT
    step — the innocent scenario red, the guilty one green.  The report body names the pid."""
    out = []
    for path in glob.glob(os.path.join(REPORTS, "*.ips")):
        if os.path.getmtime(path) < since:
            continue
        try:
            with open(path, errors="replace") as f:
                head = json.loads(f.readline())
                body = json.loads(f.read()) if pid else {}
        except (OSError, ValueError):
            continue
        if head.get("bundleID") != bundle:
            continue
        if pid and body.get("pid") not in (None, pid):
            continue
        # "2026-09-21 13:04:31.00 -0400" -> "09-21 13:04:31.000"
        m = re.match(r"\d{4}-(\d\d-\d\d \d\d:\d\d:\d\d)\.(\d+)", head.get("timestamp", ""))
        stamp = "%s.%s" % (m.group(1), (m.group(2) + "000")[:3]) if m else "01-01 00:00:00.000"
        out.append("%s I/LAB     (    0): point=crash report=%s" % (stamp, os.path.basename(path)))
    return "\n".join(out) + ("\n" if out else "")


class Simctl:
    os = "ios"

    def __init__(self, udid):
        self.serial = udid
        self._tap = None
        self._tap_path = None
        self._app_pid = None
        self._pending = None             # (action, Tap, labels before): as Adb's
        self._containers = {}            # (Tier C, C7.1) (bundle, kind) -> path, until a reinstall
        self.privacy = []                # (Tier C, C5.0) this step's `simctl privacy` calls and return codes (_privacy)

    def _run(self, *args, check=False, timeout=labkit.DEVICE_TIMEOUT):
        r = labkit.device_call(["xcrun", "simctl", *args], timeout=timeout)
        if check and r.returncode != 0:
            raise SystemExit("simctl %s failed: %s" % (" ".join(args), r.stderr.strip()))
        return r.stdout

    def terminate(self, plat, timeout=labkit.DEVICE_TIMEOUT):
        self._run("terminate", self.serial, plat["package"], timeout=timeout)   # exit 1 when not running: fine

    def installed(self, prefixes):
        """(WO-191) The installed bundle ids under any of `prefixes`.  `simctl listapps` prints an old-style plist,
        which plistlib cannot read; plutil converts it."""
        listing = self._run("listapps", self.serial, check=True)
        try:
            apps = json.loads(subprocess.run(["plutil", "-convert", "json", "-o", "-", "-"], input=listing,
                                             capture_output=True, text=True, timeout=20).stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            apps = None
        if not isinstance(apps, dict) or not apps:   # every Simulator lists its system apps: none read is a failed read
            raise SystemExit("`simctl listapps %s` could not be read — the other lab apps cannot be evicted; nothing "
                             "from this step is scored" % self.serial)
        return sorted(p for p in apps if p.startswith(tuple(prefixes)))

    def evict(self, package):
        """(WO-027, C5.2) Another lab app, out of this step's way: uninstalled, since a terminated one is not.  Killed,
        nothing called its stop(), so its CoreLocation regions stay registered, and iOS relaunches it in the background
        when the location crosses one: a scenario's move, or the standing fix put back at a step's start.  Sweep
        20260928-144003: six iOS cells refused as contaminated, the react-native, capacitor and flutter apps booting
        0.6-1.6 s after the move, logging didExitRegion / didEnterRegion lab-fence-dwell.  Nothing is lost: every
        scenario starts from a fresh install of its own app, from the build on disk (reset_app).  Refused if it is
        still installed, since it could still wake.  (WO-191) By bundle id: it may be the other lab's, which
        reinstalls its own app the same way."""
        self._run("uninstall", self.serial, package)
        if self.container({"package": package}, "app"):
            raise SystemExit("simctl uninstall %s left it installed on %s — its geofences could still wake it mid-step; "
                             "nothing from this step is scored" % (package, self.serial))

    def neutral(self):
        return False                     # no Configuration triggers on iOS: nothing to settle

    def permissions(self, plat, revoke):
        for service in ("location-always", "motion"):
            self._privacy("grant", service, plat["package"])
        unmapped = [p for p in revoke if p not in PRIVACY]
        if unmapped:
            # A revoke the scenario asked for and the Simulator cannot do changes the experiment.
            raise SystemExit("simctl has no privacy service for %s" % ", ".join(unmapped))
        for service in {PRIVACY[p] for p in revoke}:
            self._privacy("revoke", service, plat["package"])

    def prompt(self, plat, names):
        """Put these permissions back to NOT DETERMINED so the next request shows the alert.

        `simctl privacy reset` — "Reset access, prompting on next use" — not `revoke`, which means
        DENIED: a denied service never prompts again, so a scenario that revoked would be testing
        the refusal path while believing it tested the dialog."""
        for service in {PRIVACY[p] for p in names if p in PRIVACY}:
            self._privacy("reset", service, plat["package"])

    def _privacy(self, verb, service, bundle):
        """(Tier C, C5.0) One `simctl privacy` call, its return code kept for run.json (`privacy`).  A failed grant was
        silent (the Tier C design's P8), and every iOS step grants motion.  (C5.1) And refused: the step would run
        without the permission state it declares, so nothing from it is scored (exit 2).  Turned on after the C5.1
        pilot (sweep 20260928-092341) read all 48 calls 0 on four iOS cells, the one board the design asked for first."""
        r = labkit.device_call(["xcrun", "simctl", "privacy", self.serial, verb, service, bundle])
        self.privacy.append({"call": "%s %s" % (verb, service), "rc": r.returncode,
                             "stderr": r.stderr.strip()[:200] or None})
        if r.returncode != 0:
            raise SystemExit("simctl privacy %s %s %s failed (exit %d): %s — the step would run without the permission "
                             "state it declares; nothing from it is scored" % (verb, service, bundle, r.returncode,
                                                                                r.stderr.strip() or "no output"))

    def move(self, lat, lon):
        """(Tier C, C5.0) A `moves:` hop: one fix at (lat, lon), as begin_log's standing fix; refused unless simctl
        exits 0.  Whether CoreLocation's region monitoring sees it on the Simulator is NOT CHECKED (C5.2's pilot)."""
        self._run("location", self.serial, "set", "%s,%s" % (lat, lon), check=True)

    def network(self, wifi=None, data=None):
        raise SystemExit("the Simulator has no network of its own — labkit refuses `actions:` on a scenario that does "
                         "not excuse ios")

    def _display_name(self, plat):
        """The name the OS puts in its own alert — `Allow “CDV Lab” to use your location?` — read
        from the INSTALLED bundle, so it is the app under test's own word for itself."""
        cache = getattr(self, "_names", None)
        if cache is None:
            cache = self._names = {}
        if plat["package"] not in cache:
            name = None
            try:
                app = self.container(plat, "app")
                if app:
                    with open(os.path.join(app, "Info.plist"), "rb") as f:
                        info = plistlib.load(f)
                    name = info.get("CFBundleDisplayName") or info.get("CFBundleName")
            except Exception:
                name = None
            cache[plat["package"]] = name
        return cache[plat["package"]]

    def answer_dialog(self, action, plat):
        """Answer a system permission alert, if one is up.  Returns a Tap (the button label
        tapped, as a str) or None.  simctl cannot tap — `idb` is the only scriptable route to a
        Simulator's UI, and it is an OPTIONAL dependency: a scenario that never answers a dialog
        never needs it, and one that does says so here rather than skipping the iOS cell in
        silence."""
        if shutil.which("idb") is None:
            raise SystemExit(
                "this scenario answers a system dialog, which needs `idb` on iOS — simctl has no "
                "tap.  Install it with `brew install facebook/fb/idb` (and `pip install fb-idb`), "
                "or run this scenario with --os android.")
        described = labkit.device_call(["idb", "ui", "describe-all", "--udid", self.serial], timeout=30)
        if described.returncode != 0:
            raise SystemExit("idb could not read the Simulator's UI: %s" % described.stderr.strip())
        # `describe-all` prints ONE JSON ARRAY of elements, each with an AXLabel and a frame —
        # measured, not assumed: a line-by-line reader finds nothing and reports "no dialog",
        # which is the failure that looks like success.
        try:
            nodes = json.loads(described.stdout)
        except ValueError:
            return None
        if action in labkit.ANDROID_ONLY_ACTIONS:      # (Tier C, C8.0) the Play services and upgrade actions too
            raise SystemExit("`dialog: %s` is Android-only — %s" % (action, labkit.ANDROID_ONLY_ACTIONS[action]))
        labels = [str(node.get("AXLabel") or "") for node in nodes]
        # (WO-027, permissions) A tap whose re-read failed is judged on this read, before any
        # guard or tap: gone means it answered, at its own tapped_ms (as Adb.answer_dialog).
        pending, self._pending = self._pending, None
        if pending and pending[0] == action and window_gone(pending[1], pending[2], labels):
            return pending[1]
        if action == "cancel":
            # (WO-027, permissions) The SDK's own Settings alert, whose "Cancel" is guarded by
            # the alert's title (SDK_ALERT_TITLES), not by the owner guard below.  It lives in the
            # app's process, so it cannot outlive the app, and its text never names the app.
            if not any(_canon(t) in _canon(text) for text in labels for t in SDK_ALERT_TITLES):
                return None
        else:
            # (WO-027) OWNER GUARD.  An alert outlives the app that raised it — `simctl uninstall`
            # leaves it standing — and idb will happily tap a dead window, which marks the capture
            # `dialogAnswered=allow` for something nothing was listening to.  Measured 2026-09-23:
            # flutter's tap landed 0.4 s BEFORE its own app booted and the run still read "allow".
            # The alert must name the app under test before it is touched.
            name = self._display_name(plat)
            if name and not any(name in text for text in labels):
                if not getattr(self, "_guard_noted", False):
                    self._guard_noted = True
                    print("[dialog] nothing on screen names %r yet" % name)
                return None
        for label in DIALOG_BUTTONS[action]:
            for node in nodes:
                if str(node.get("AXLabel", "")).strip().lower() != label.lower():
                    continue
                frame = node.get("frame") or {}
                x = frame.get("x", 0) + frame.get("width", 0) / 2
                y = frame.get("y", 0) + frame.get("height", 0) / 2
                labkit.device_call(["idb", "ui", "tap", "--udid", self.serial, str(int(x)), str(int(y))], timeout=30)
                tapped = Tap(label, int(time.time() * 1000))
                # (WO-027) A tap is not an answer until the window is GONE.  An alert that is
                # still animating in reports a frame idb will happily tap, and the tap is
                # swallowed: measured 2026-09-23, three iOS runs marked `dialogAnswered=allow`
                # 0.65-0.8 s after the request, no CLAuthorization delegate followed, and the SDK
                # sat out its own 30 s timeout.  The passing runs had tapped ~1.9 s in.  So the
                # host checks, and a tap that changed nothing is not reported as an answer — the
                # watcher simply polls again.  (WO-027, permissions) "Gone" is window_gone, the
                # rule Android uses too: another alert showing the same label counts as a new window.
                time.sleep(0.6)
                after = self._labels()
                if not window_gone(label, labels, after):
                    if after is None:
                        self._pending = (action, tapped, labels)
                    return None
                return tapped
        return None

    def _labels(self):
        """Every AXLabel on screen now, or None when idb could not read it.  A caller asking
        whether an alert has GONE must treat None as still up: "could not look" is not "gone"."""
        described = labkit.device_call(["idb", "ui", "describe-all", "--udid", self.serial], timeout=30)
        if described.returncode != 0:
            return None
        try:
            nodes = json.loads(described.stdout)
        except ValueError:
            return None
        return [str(node.get("AXLabel") or "") for node in nodes]

    def visible_dialog(self, plat):
        """(WO-027, permissions) Which known dialog is up now, by its buttons: dialog_signature
        over one describe-all, with no tap.  Returns None when no known dialog is up.  No owner
        guard: a window is recognised by its buttons, never by the app's name, which the app's
        own tree may carry on every read.  Raises RuntimeError when the screen could not be
        read: for an absence assertion, "could not look" must not count as "nothing there"."""
        if shutil.which("idb") is None:
            raise SystemExit(
                "this step watches for system dialogs, which needs `idb` on iOS — simctl cannot "
                "read the screen.  Install it with `brew install facebook/fb/idb` (and `pip "
                "install fb-idb`), or run this scenario with --os android.")
        labels = self._labels()
        if labels is None:
            raise RuntimeError("idb could not read the Simulator's UI")
        return dialog_signature(labels)

    def permission_flags(self, plat):
        """(WO-027, permissions) Android's snapshot has no iOS counterpart: simctl cannot read
        TCC's state back.  The most an iOS capture can say is which privacy call was made."""
        return None

    def _tap_text(self):
        with open(self._tap_path, errors="replace") as f:
            return f.read()

    def close(self):
        """Tear the tap down whatever happened — a run that raised between begin_log and
        end_log would otherwise leave `log stream` writing into the next run's file."""
        if self._tap is not None and self._tap.poll() is None:
            self._tap.terminate()
            self._tap.wait()
        self._tap = None
        if self._tap_path and os.path.exists(self._tap_path):
            os.remove(self._tap_path)

    def begin_log(self):
        """Start listening BEFORE the launch: os_log is a stream, not a buffer to dump later.
        `simctl launch --stdout` was measured at 0 bytes for a scene-based launch; this is the
        channel (WO-027, Platform parity)."""
        self.close()
        self._pending = None             # a tap left unconfirmed belongs to the previous step
        self._since = time.time()
        # Per-process file name: an orphaned tap from a dead run must never share an inode.
        self._tap_path = os.path.join(labkit.HARNESS, ".claims", "%s-%d.oslog" % (self.serial, os.getpid()))
        os.makedirs(os.path.dirname(self._tap_path), exist_ok=True)
        # (WO-027) The same standing fix `adb emu geo fix` gives the emulator (Adb.begin_log), so
        # getCurrentPosition can resolve on both OSes instead of erroring on iOS alone.  A static
        # location only: driving a route is WO-003's successor, not this.
        self._run("location", self.serial, "set", "%s,%s" % labkit.STANDING_FIX)
        self._tap = subprocess.Popen(
            ["xcrun", "simctl", "spawn", self.serial, "log", "stream", "--style", "ndjson",
             "--level", "debug", "--predicate",
             '(eventMessage CONTAINS "LAB ") OR (subsystem == "%s") OR '
             '(eventMessage CONTAINS "LICENSE VALIDATION FAILURE")' % SDK_SUBSYSTEM],
            stdout=open(self._tap_path, "w"), stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        # The stream announces itself ("Filtering the log data using …") once attached; a mark
        # posted before that is lost, and the run would die at the trigger mark for no reason.
        for _ in range(50):
            if os.path.getsize(self._tap_path) > 0:
                break
            time.sleep(0.1)
        else:
            raise SystemExit("log stream on %s never attached" % self.serial)

    def preflight(self):
        """(WO-027) The Simulator must be booted, and the capture names which one it was."""
        try:
            runtimes = json.loads(self._run("list", "devices", "-j", timeout=20)).get("devices", {})
        except ValueError:
            runtimes = {}
        for runtime, devs in runtimes.items():
            for d in devs:
                if d.get("udid") == self.serial:
                    if d.get("state") != "Booted":
                        raise SystemExit("Simulator %s (%s) is %s, not Booted" % (d.get("name"), self.serial, d.get("state")))
                    ver = runtime.rsplit(".", 1)[-1].replace("iOS-", "iOS ").replace("-", ".")
                    self.info = {"model": d.get("name"), "os": ver}
                    return self.info
        raise SystemExit("no Simulator %s in `simctl list devices`" % self.serial)

    def peek(self):
        """The log so far, from the tap file `log stream` is still writing (a torn last line is
        skipped by to_logcat)."""
        if not self._tap_path or not os.path.exists(self._tap_path):
            return []
        return to_logcat(self._tap_text()).splitlines()

    def mark(self, text):
        # The device's own log, like `adb shell log -t LAB`: same clock as the app's lines.
        self._run("spawn", self.serial, "syslog", "-s", "-l", "notice",
                  "%s point=%s" % (labkit.TAG, text))

    def launch(self, plat, component, extras):
        """The shim reads argv as `--key=value`; `component` (an Activity) has no iOS meaning."""
        argv = ["--%s=%s" % (k, str(v).lower() if isinstance(v, bool) else v)
                for k, v in extras.items()]
        out = self._run("launch", "--terminate-running-process", self.serial, plat["package"], *argv)
        m = re.search(r":\s*(\d+)\s*$", out.strip())     # "<bundle>: <pid>" on success
        if not m:
            raise SystemExit("launch failed: %s" % out.strip())
        self._app_pid = int(m.group(1))            # crash reports are matched to THIS process

    def fire(self, trigger, plat, component, on=True):
        if trigger != "none":
            raise SystemExit("iOS has no %r trigger — the manifest's ios.supports.triggers must "
                             "keep this scenario off the platform" % trigger)

    def snapshot(self, out):
        self._run("io", self.serial, "screenshot", os.path.join(out, "after-trigger.png"))
        with open(os.path.join(out, "top-activity.txt"), "w") as f:
            f.write("")                  # no Activity stack on iOS; topActivity reads '-'

    def end_log(self, plat):
        # The capture mark travels syslogd -> logd -> the stream -> this file; cutting the tap
        # the instant after posting it drops the mark, and the run dies at Capture._mark.
        for _ in range(100):
            if "point=capture" in self._tap_text():
                break
            time.sleep(0.1)
        self._tap.terminate()
        self._tap.wait()
        text = self._tap_text()
        self.close()
        reports = crash_reports(plat["package"], self._since, self._app_pid)
        if not reports and self._app_pid and not self._alive(plat["package"]):
            # (WO-027) The app is gone and no report yet: ReportCrash may still be writing it —
            # up to ~10.5 s after the crash, measured.  Wait for THIS pid's report rather than
            # score a crash as a pass (or hand it to the next step).
            for _ in range(15):
                time.sleep(1)
                reports = crash_reports(plat["package"], self._since, self._app_pid)
                if reports:
                    break
        return to_logcat(text) + reports

    def _alive(self, bundle):
        """Is the app's process still running?  launchctl lists a live app as
        `<pid>  <status>  UIKitApplication:<bundle>[…]`, and a dead one with `-` or not at all."""
        for line in self._run("spawn", self.serial, "launchctl", "list", timeout=20).splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and ("UIKitApplication:%s[" % bundle) in parts[2]:
                return parts[0].strip().isdigit()
        return False

    def container(self, plat, kind="data"):
        """(Tier C, C7.1) Remembered until the app is (re)installed: each lookup is a `simctl` process
        (≈0.17 s), and a step asked 6–7 times — the live-verdict pusher alone every second it had a
        new view.  install() and reset_app() forget it, and a remembered path that no longer exists
        (the app reinstalled from outside) is looked up again."""
        key = (plat["package"], kind)
        path = self._containers.get(key)
        if not path or not os.path.isdir(path):
            path = self._containers[key] = self._run("get_app_container", self.serial, plat["package"], kind).strip()
        return path

    def sdk_db(self, plat):
        path = os.path.join(self.container(plat), "Library", "Application Support",
                            "TSLocationManager", "transistor_log.db")
        if not os.path.exists(path):
            return b""
        with open(path, "rb") as f:
            return f.read()

    def push_verdict(self, plat, text):
        """(WO-027) As Adb.push_verdict: the app's data container is a directory on this Mac, so the
        host writes it directly — `Library/lab-verdict.json`, which the shim reads through
        NSHomeDirectory().  Renamed into place, never written in place."""
        path = os.path.join(self.container(plat), "Library", "lab-verdict.json")
        if text is None:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path + ".tmp", "w") as f:
            f.write(text)
        os.replace(path + ".tmp", path)

    def install(self, plat, artifact):
        self._containers.clear()         # (Tier C, C7.1) a new install, new containers
        self._run("install", self.serial, artifact, check=True, timeout=180)

    def dismiss_stale_dialog(self, plat):
        """(WO-027) Clear an alert whose owner is gone.  Called once the app under test has been
        terminated, so anything still on screen is by definition dead — and a dead alert is not
        harmless: the next step's watcher finds it at t=0, taps it, and marks an answer for a
        window no one was listening to.  Best effort: without idb there is no tap on a Simulator,
        and a scenario that actually needs one says so in answer_dialog."""
        if shutil.which("idb") is None:
            return None
        described = labkit.device_call(["idb", "ui", "describe-all", "--udid", self.serial], timeout=30)
        if described.returncode != 0:
            return None
        try:
            nodes = json.loads(described.stdout)
        except ValueError:
            return None
        # Least permissive first: whatever this answers is already inert, and if the guess about
        # "inert" is ever wrong, the harmless mistake is the one that grants nothing.  (WO-027,
        # permissions) `keep` before `allow`: a stale upgrade alert is declined, never accepted.
        # Not `cancel`: the SDK's Settings alert dies with the app, and "Cancel" alone is anybody's.
        for label in DIALOG_BUTTONS["deny"] + DIALOG_BUTTONS["keep"] + DIALOG_BUTTONS["allow"]:
            for node in nodes:
                if str(node.get("AXLabel", "")).strip().lower() != label.lower():
                    continue
                frame = node.get("frame") or {}
                x = frame.get("x", 0) + frame.get("width", 0) / 2
                y = frame.get("y", 0) + frame.get("height", 0) / 2
                labkit.device_call(["idb", "ui", "tap", "--udid", self.serial, str(int(x)), str(int(y))], timeout=30)
                print("[dialog] swept a stale alert (%s)" % label)
                return label
        return None

    def reset_app(self, plat, artifact):
        """(WO-027) Back to a fresh install, literally: uninstall drops the data container and
        the app's TCC grants (permissions() re-applies them right after).  Not a data-container
        wipe — cfprefsd caches NSUserDefaults and would hand the deleted values straight back."""
        self._containers.clear()         # (Tier C, C7.1) a new install, new containers
        self._run("uninstall", self.serial, plat["package"])
        self._run("install", self.serial, artifact, check=True, timeout=180)

    def native_resolved(self, plat, artifact):
        """The version the INSTALLED app links, read out of its binary: CocoaPods links the
        static TSLocationManager archive into the executable (there is no
        Frameworks/TSLocationManager.framework on this path — that was the SPM spike), and the
        linker keeps the framework's own `PROJECT:TSLocationManager-<build>` string.  Never
        None: an iOS capture that cannot name its SDK is evidence about nothing (WO-027).
        The executable is whatever the app's Info.plist names (Flutter's `Runner`, React
        Native's `BGGeoDemo`); a Debug build keeps the code in `<exe>.debug.dylib`."""
        app = self.container(plat, "app")
        if not app:
            raise SystemExit("%s is not installed on %s — run with --build" % (plat["package"], self.serial))
        try:
            with open(os.path.join(app, "Info.plist"), "rb") as f:
                exe = plistlib.load(f).get("CFBundleExecutable")
        except (OSError, plistlib.InvalidFileException):
            exe = None
        if not exe:
            raise SystemExit("the installed %s has no readable CFBundleExecutable — cannot tell "
                             "which SDK this capture would exercise" % plat["package"])
        binaries = [os.path.join(app, exe + ".debug.dylib"), os.path.join(app, exe)]
        for binary in binaries:
            if not os.path.exists(binary):
                continue
            # (WO-027) In-process, not `strings -`: 1.76 s -> 0.01 s on React Native's 35 MB
            # debug dylib.  The token is contiguous printable ASCII, so the raw bytes hold it
            # exactly as `strings` printed it.
            with open(binary, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                m = re.search(rb"PROJECT:TSLocationManager-(\d+)", mm)
                build = m.group(1).decode() if m else None     # read before the map closes
            if build:
                return build
        raise SystemExit("the installed %s carries no PROJECT:TSLocationManager-<build> string — "
                         "cannot tell which SDK this capture would exercise" % plat["package"])


def backend(os_name, serial):
    return {"android": Adb, "ios": Simctl}[os_name](serial)
