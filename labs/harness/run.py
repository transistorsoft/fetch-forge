#!/usr/bin/env python3
"""Run one scenario against one platform app and score it (WO-026).

    run.py --platform capacitor --scenario recreate-bold --variant FIX
    run.py --platform flutter   --scenario runtime-reset  --variant BASE --build
    run.py --platform capacitor --scenario launch-http-flush --variant FIX   # ordered pair
    run.py --platform flutter   --scenario api-datastore --os ios --build     # (WO-027)

One step: terminate -> neutral settings -> grant, then revoke (skipped on `permissions: keep`)
-> start the log -> launch with the scenario's extras -> settle -> trigger -> capture (+10 s
screenshot and, on Android, dumpsys) -> pull the log and the SDK log database -> revert the
trigger -> score.  A scenario's first step, and any step marked `fresh: true`, starts from a
fresh install.

`--os` picks the device backend (backends.py): adb for android, simctl for ios.  A platform
manifest carries its iOS half under `ios:`; run.py sees one merged view either way.

The run prints PASS/FAIL per expectation. A mutation variant inverts that: it must break at
least one expectation of every step it claims to break — every step of a scenario its
must_fail names, or the one step a `<scenario>/<step>` entry names (labkit.must_fail).
"""

import argparse
import hashlib
import http.server
import io
import json
import os
import re
import subprocess
import threading
import sys
import time

import backends
import devices
import lab_devices                  # (Tier C, C8.0 review) NAMES: the AVDs whose Location Accuracy the lab may set
import labkit


def build(plat, var):
    web = plat.get("web")
    if web:
        # By hand, never `npx cap sync`: sync regenerates the plugin wiring and would swap
        # the plugin under test for the published one.
        src, dest = (os.path.join(labkit.LABS, web[k]) for k in ("src", "dest"))
        os.makedirs(dest, exist_ok=True)
        for name in os.listdir(src):
            subprocess.run(["cp", os.path.join(src, name), os.path.join(dest, name)], check=True)
        print("[web] %s -> %s" % (web["src"], web["dest"]))
    cmd = [expand(part).format(native=var.get("native", ""), plugin=plugin_path(plat, var) or "")
           for part in plat["build"]["cmd"]]
    cwd = os.path.join(labkit.LABS, plat["build"]["cwd"])
    env = dict(os.environ, **{k: expand(str(v)) for k, v in (plat["build"].get("env") or {}).items()})
    # (WO-027) The build script picks its OS branch from LAB_OS: an `export LAB_OS=ios` left in the
    # operator's shell would otherwise send an Android --build down the iOS branch, exit 0, and
    # have the stale APK stamped as fresh.  The platform view's own OS always wins.
    env["LAB_OS"] = plat.get("os", "android")
    # (WO-027) The stamp goes BEFORE the build, not only when the script fails: a build that is
    # killed — a timeout, ^C — would otherwise leave the previous stamp vouching for a half-built
    # artifact.  It is written back only after a clean exit.  Known limit: a lab source edited
    # DURING the build is stamped as built.  Comparing stamps before and after cannot catch it —
    # the builds rewrite hashed files themselves (Capacitor's public/bg.js from the plugin, the
    # pubspec.lock / package-lock.json that pub get and npm install touch), so it would refuse
    # exactly the builds that matter.
    if os.path.exists(stamp_path(plat)):
        os.remove(stamp_path(plat))
    print("[build] %s (cwd %s)" % (" ".join(cmd), cwd))
    r = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise SystemExit("build failed")
    with open(stamp_path(plat), "w") as f:
        f.write(source_stamp(plat, var))



def native_source(var, os_name="android"):
    """The native commit the variant's snapshot was built from, if publish_native.sh recorded
    it. A capture that cannot name its native source is evidence about nothing in particular."""
    store = {"android": "m2", "ios": "xcframeworks"}[os_name]
    path = os.path.join(labkit.LABS, store, ".provenance", "%s.txt" % var.get("native"))
    if not os.path.exists(path):
        return None
    fields = dict(l.split(":", 1) for l in open(path).read().splitlines() if ":" in l)
    return {k.strip(): v.strip() for k, v in fields.items()
            if k.strip() in ("branch", "commit", "dirty", "marketing", "build")}

def expand(value):
    """Expand {labs} / {repo} in a manifest path.

    Manifests are committed, so they may not carry a machine path: one pinned to a git
    worktree stops existing when the worktree does, and one pinned to a session scratchpad
    stops existing when the session does — in both cases the labs simply do not build for
    anyone else."""
    if not isinstance(value, str):
        return value
    # Substitution, not str.format: a build command carries {native} and {plugin} too, and
    # format() would raise KeyError on them here.
    return (value.replace("{labs}", labkit.LABS)
                 .replace("{repo}", os.path.dirname(labkit.LABS)))


def plugin_path(plat, var):
    """A variant names a checkout ('worktree', or a path); the platform knows where that is.
    (WO-143) None for a platform with no plugin: its manifest names neither `plugins` nor `default_checkout`, because
    it is built from the native label alone, and a variant's `plugin:` is a choice it does not have.  Every
    platform had one until then, and a manifest without one died on `git -C worktree`: after a clean build, in the
    stamp, and again when run.json was written, with the step already run."""
    if not plat.get("plugins") and not plat.get("default_checkout"):
        return None
    choice = var.get("plugin", "worktree")
    path = expand(plat.get("plugins", {}).get(choice, choice))
    if path != "default" and os.path.isabs(path) and not os.path.exists(path):
        raise SystemExit(
            "%s: plugin checkout %r for variant %r does not exist at %s.\n"
            "Place it there (labs/checkouts/ is gitignored) or pick another variant."
            % (plat["name"], choice, var.get("name"), path))
    return path


def plugin_checkout(plat, var):
    """(WO-027) The git checkout the variant's plugin is BUILT from, by its top level.  `default`
    means the app's own declared dependency, which the manifest names as `default_checkout`;
    Capacitor's manifest names the checkout's android/ dir, hence --show-toplevel.  (WO-143) None where the
    platform has no plugin (plugin_path)."""
    path = plugin_path(plat, var)
    if path is None:
        return None
    if path == "default":
        path = expand(plat.get("default_checkout", ""))
        if not path:
            raise SystemExit("%s: plugin 'default' but lab.yaml names no default_checkout"
                             % plat["name"])
    r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("%s: plugin %s is not a git checkout — a capture could not name the "
                         "plugin it ran" % (plat["name"], path))
    return r.stdout.strip()


# (WO-027) Watchman's sync cookies, `.watchman-cookie-<host>-<pid>-<n>`: created and deleted within
# milliseconds, about once a minute, in every root watchman watches — the React Native lab app (its
# .watchmanconfig) and a plugin checkout a Metro is watching.  Hashed, one landed inside a stamp
# check and refused a current APK as stale (2026-09-24, S4, react-native/android api-geofences).
VOLATILE_PREFIXES = (".watchman-cookie-",)


def plugin_state(checkout):
    """HEAD, and everything the working tree adds to it: the tracked diff and the untracked,
    non-ignored files by name.  Gitignored output (node_modules, build/) is excluded by git."""
    git = lambda *a: subprocess.run(["git", "-C", checkout] + list(a), capture_output=True,
                                    check=True).stdout
    head = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "HEAD", "--binary")
    untracked = sorted(n for n in git("ls-files", "-o", "--exclude-standard", "-z").decode()
                       .split("\0") if n and not os.path.basename(n).startswith(VOLATILE_PREFIXES))
    return head, diff, untracked


def plugin_source(plat, var):
    """What run.json records about the plugin, beside native_source: None where the platform has none (WO-143)."""
    checkout = plugin_checkout(plat, var)
    if checkout is None:
        return None
    head, diff, untracked = plugin_state(checkout)
    return {"checkout": os.path.relpath(checkout, os.path.dirname(labkit.LABS)),
            "commit": head, "dirty": "yes" if (diff or untracked) else "no"}


SKIP_DIRS = {"build", ".dart_tool", ".gradle", "node_modules", ".idea", "captures", ".claims",
             "Pods", ".symlinks", "ephemeral", "xcuserdata", "DerivedData",   # (WO-027) iOS volatiles
             # (WO-027) Cordova's generated platforms/ and plugins/: `cordova plugin add` rewrites
             # the tree of EVERY installed platform on every build, so hashing them made each OS's
             # build invalidate the other OS's stamp for good — an alternation cost a full rebuild.
             # They are a function of what IS hashed: app/{www,native,config.xml,lab-plugin,
             # package.json}, build.sh, and the variant's plugin checkout (in the stamp's preamble).
             "platforms", "plugins",
             # (WO-027) The other three bridges' generated iOS host, app/ios/ — gitignored, made by
             # build.sh from native-ios/ (which IS hashed) — for the same reason: an iOS build
             # rewrote it and every Android stamp went stale, measured on all three the day the
             # native snapshot was bumped.  No lab app has a tracked file under an `ios` dir.
             "ios",
             # (WO-027) React Native's CMake/NDK output, app/android/app/.cxx — gitignored and generated
             # from inputs that ARE hashed.  293 of the RN stamp's files lived there and a Gradle build
             # rewrites some, so an Android build could invalidate the iOS stamp (and back): the same
             # cross-OS churn as `ios` above, which only tonight's build order avoided.
             ".cxx",
             # (WO-143) The Kotlin Gradle plugin's session directory, beside settings.gradle: it holds a
             # `sessions/*.salive` file while a compiler daemon lives, and a build that is killed leaves one behind,
             # which would refuse a current APK as stale.  Empty at rest in the Flutter and React Native labs, so no
             # stamp of theirs moves; the native lab's project is all Kotlin.
             ".kotlin"}


def source_stamp(plat, var):
    """What the installed APK must correspond to: this variant, and the app's sources by
    CONTENT (mtimes are useless — the harness rewrites the web assets every run)."""
    # The app sources and the build script only: the manifest's prose and FINDINGS.md do not
    # change what is installed, and hashing them would invalidate a perfectly good APK.
    base = os.path.join(labkit.LABS, "platforms", plat["name"])
    # (WO-143) `build.sources` where a platform's two OSes are two apps that share no source (native: Kotlin and
    # Swift): each OS's stamp is its own app's.  Absent, as on the four bridges, it is all of app/.
    roots = [os.path.join(base, rel) for rel in (plat.get("build") or {}).get("sources") or ["app"]]
    build_sh = os.path.join(base, "build.sh")
    if os.path.exists(build_sh):
        roots.append(build_sh)
    # (WO-027) Sources the builds pull in from OUTSIDE app/ (labapi.js): unhashed, an edit there
    # would leave a stale APK whose stamp still matches.
    shared = os.path.join(labkit.LABS, "platforms", "_shared")
    if os.path.isdir(shared):
        roots.append(shared)
    h = hashlib.sha256()
    h.update(("%s|%s|" % (var.get("native"), plugin_path(plat, var))).encode())
    # (WO-027) The plugin checkout by CONTENT, not by path: the path above is the string
    # 'default' for Flutter and React Native, so a plugin commit changed nothing here — four
    # release merges on 2026-09-23 left every installed app stamped fresh, and a run scored an
    # app built from the previous plugin commit.  HEAD, the tracked diff, and untracked files.
    # (WO-143) Where there is one: a platform with no plugin is its label, hashed below, and its own sources.
    checkout = plugin_checkout(plat, var)
    if checkout is not None:
        head, diff, untracked = plugin_state(checkout)
        h.update(("%s|" % head).encode())
        h.update(diff)
        for name in untracked:
            h.update(name.encode())
            try:
                with open(os.path.join(checkout, name), "rb") as f:
                    h.update(f.read())
            except OSError:
                pass
    # (WO-027) The label's snapshot, not just its name: publish_native*.sh overwrites an existing
    # label and rewrites .provenance/<label>.txt (commit, sha, when) while the built app still
    # links the old snapshot — and the install-time guard cannot tell two builds of one
    # marketing version apart.  This OS's store only: stamps are per-artifact.
    store = {"android": "m2", "ios": "xcframeworks"}[plat.get("os", "android")]
    prov = os.path.join(labkit.LABS, store, ".provenance", "%s.txt" % var.get("native"))
    if os.path.exists(prov):
        with open(prov, "rb") as f:
            h.update(f.read())
    # NB: never wrap os.walk in sorted() — that consumes the whole tree before the pruning
    # below takes effect, and the hash would then include build/ (the APK hashing itself).
    paths = []
    for root in roots:
        if os.path.isfile(root):
            paths.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            paths += [os.path.join(dirpath, n) for n in filenames if not n.startswith(VOLATILE_PREFIXES)]
    for path in sorted(paths):
        h.update(os.path.relpath(path, base).encode())
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            pass
    return h.hexdigest()


def artifact(plat):
    """The installable the build produces: an APK on Android, a .app bundle on iOS."""
    return os.path.join(labkit.LABS, plat["build"].get("apk") or plat["build"]["app"])


def stamp_path(plat):
    # Beside the artifact, so an Android build can never satisfy an iOS run or vice versa.
    return artifact(plat) + ".lab-stamp"


def check_stamp(plat, var):
    apk = artifact(plat)
    if not os.path.exists(apk):
        raise SystemExit("no build at %s — run with --build" % apk)
    # A failed build leaves the previous APK in place, and a variant switch changes nothing
    # on disk: scoring either would report a result for code that is not under test.
    want = source_stamp(plat, var)
    have = open(stamp_path(plat)).read().strip() if os.path.exists(stamp_path(plat)) else None
    if have != want:
        raise SystemExit(
            "the installed APK was not built from these sources for variant %s — run with "
            "--build (a failed build leaves the previous APK behind)" % var["name"])
    return apk


def install(dev, plat, var):
    apk = check_stamp(plat, var)
    print("[install] %s" % apk)
    dev.install(plat, apk)


def assert_native(dev, plat, var):
    """(WO-027) The app about to be LAUNCHED must link the SDK its label's provenance describes;
    a capture that cannot prove which SDK it exercised is evidence about nothing.  Read from the
    device before the first launch — so --no-install cannot skip it: on iOS right after the first
    step's reinstall (Tier C, C7.1), elsewhere before any step.  Adb reports None (asserted at
    build time from the plugin's println); Simctl never does."""
    resolved = dev.native_resolved(plat, artifact(plat))
    if resolved is None:
        return
    source = native_source(var, dev.os) or {}
    if not source.get("build"):
        raise SystemExit("no provenance for label %r on %s — publish it with harness/publish_native_ios.sh"
                         % (var.get("native"), dev.os))
    if resolved != source["build"]:
        raise SystemExit("the installed app links TSLocationManager build %s, but the provenance "
                         "for %r says %s — rebuild with --build" % (resolved, var.get("native"), source["build"]))


# (WO-027) How long the driver gets to draw the final verdict before the screenshot and before the
# app is put down.  (Tier C, C7.6) The driver polls the pushed file every 0.25 s (it was once a
# second, and this 1.5 s), plus a frame.  A drawn-acknowledgement was considered and not built: the
# device log is already closed when the final verdict is pushed, and an ack file would need native
# code in all 8 app halves.
VERDICT_SETTLE_S = 0.5

# (WO-027, permissions) How many looks may fail after the dialog observer's last successful one, for
# the step to score `undeclaredDialogs` (stop_watcher).  Counted, not timed: a failed `uiautomator
# dump` can take several seconds, so a wall-clock limit refused a step for one slow failure.  A look
# in progress at the stop is joined, so it is counted either way.
LOOK_FAILS_ALLOWED = 1


def set_accuracy(dev, step, label):
    """(Tier C, C8.0; decision 22 B) Google Location Accuracy as the step declares it (default on), set before EVERY
    Android launch: a read is one `dumpsys location` (23-32 ms), a flip about 2.3 s (both MEASURED 2026-10-02).  So the
    step after an OFF one starts on, and an emulator someone left off is turned on rather than left out of the sweep
    (resolve() no longer checks it).  Play services' switch takes assisted GPS with it, off and on (MEASURED), so
    nothing else is set.  A flip that does not take is refused (unscored), as a launch that did not happen is: the step
    would run on a setting it never asked for.  The Home press after a flip lands before the launch.

    (C8.0 review) A read that shows no network provider (adb failed, output cut short) is read once more, then
    refused: "could not read" is never "off".  The value is read again right before the launch, flipped or not.  Only
    a Lab emulator is flipped (lab_devices.NAMES, the AVD preflight read): Chris accepted Google's consent on those, and
    nothing that is not called Lab1 or Lab2 is changed (lab_devices).  Elsewhere a step that wants it off is refused,
    and one that wants it on runs as the device is, said in a LAB DEFECT line and the record.  Returns what run.json
    records, {want, was, now} (the first read and the last), or None on a backend without the setting (iOS)."""
    if not hasattr(dev, "set_location_accuracy"):
        return None
    want = step["locationAccuracy"]

    def read():
        on = dev.location_accuracy()
        return dev.location_accuracy() if on is None else on
    was = read()
    if was is None:
        raise SystemExit("%s: Google Location Accuracy could not be read (dumpsys location shows no network provider, "
                         "twice); nothing from this step is scored" % label)
    if was != want:
        avd = (getattr(dev, "info", None) or {}).get("avd")
        if avd not in lab_devices.NAMES:
            if not want:
                raise SystemExit("%s: the step wants Google Location Accuracy off, and %s is not a Lab emulator (AVD %s): "
                                 "the lab changes it only on Lab1 and Lab2; nothing from this step is scored"
                                 % (label, getattr(dev, "serial", "?"), avd))
            print("[accuracy] LAB DEFECT: Google Location Accuracy is off on %s, which is not a Lab emulator (AVD %s), "
                  "so it is left off; the step wants it on" % (getattr(dev, "serial", "?"), avd))
            return {"want": want, "was": was, "now": was}
        why = dev.set_location_accuracy(want)
        if why:
            raise SystemExit("%s: Google Location Accuracy could not be turned %s (%s); nothing from this step is scored"
                             % (label, "on" if want else "off", why))
    now = read()
    if now != want:
        raise SystemExit("%s: Google Location Accuracy reads %s right before the launch, not %s; nothing from this step "
                         "is scored" % (label, {True: "on", False: "off", None: "nothing"}[now], "on" if want else "off"))
    return {"want": want, "was": was, "now": now}


def restore_accuracy(dev, scen):
    """(Tier C, C8.0 review) A scenario that turned Google Location Accuracy off puts it back on when its steps are
    done, on a Lab emulator: the next step on the device would, but under WO-106's shared queue the next cell may run
    on the other device, a lane's last cell has no next one, and a run by hand none either.  Best effort: a restore
    that fails is a LAB DEFECT line, never a verdict, and the next step anywhere sets its own.  Returns whether it
    tried."""
    if (not hasattr(dev, "set_location_accuracy") or all(s["locationAccuracy"] for s in scen["steps"])
            or (getattr(dev, "info", None) or {}).get("avd") not in lab_devices.NAMES):
        return False
    try:
        why = dev.set_location_accuracy(True)
    except SystemExit as e:                  # a device call past its timeout: still not a verdict
        why = str(e)
    print("[accuracy] %s" % ("on again after the scenario" if why is None
                             else "LAB DEFECT: not on again after the scenario: %s" % why))
    return True


def mark_value(text):
    """(WO-027, permissions) A label as ONE mark field.  Capture.field reads a value up to the first
    space, so whitespace becomes `_`, the encoding the dialog marks always used.  A straight
    apostrophe ("Don't allow") becomes ’: Adb.mark single-quotes the whole mark for the device
    shell, and one ' ends that quote, so the mark is lost and the step goes red for the lab's
    reason.  A double quote is left alone: `button=Change_to_"Allow_all_the_time"` has always
    landed intact."""
    return re.sub(r"\s+", "_", str(text).strip()).replace("'", "\u2019")


def judge(out, label, plat, scen, var, step):
    """(WO-027) A step's verdict, computed ONCE: main() reports it and writes summary.txt from it,
    and the device view is shown the same rows before the screenshot (final_view)."""
    cap = labkit.Capture(out)
    j = {"cap": cap, "foreign": devices.contamination(cap.lines, label, cap.meta.get("os", "android"),
                                                      plat["package"]), "rows": [],   # (WO-191) by package too
         "expect": None, "inverted": labkit.must_fail(var, scen["name"], step["name"]), "ok": None}
    if j["foreign"]:
        j["ok"] = False
        return j
    j["expect"] = labkit.expectations(step, plat["name"], plat.get("os", "android"))
    if j["expect"]:
        j["rows"] = labkit.score(cap, j["expect"])
        j["ok"] = labkit.report(j["rows"], inverted=j["inverted"], stream=io.StringIO())
    return j


def final_view(j, step):
    """What the device shows once the step is scored: every row final, plus the metric table.
    (Tier C, C7.6) `final: true`, which no live push carries, also makes the driver select its Test
    tab before drawing, so the screenshot shows the verdict whichever tab was open."""
    view = labkit.verdict_view(j["cap"], step, final=True)
    view.update(final=True, verdict=None if j["ok"] is None else ("PASS" if j["ok"] else "FAIL"),
                inverted=j["inverted"], contaminated=j["foreign"] or None,
                metrics=[[k, str(a), str(e), bool(ok)] for k, a, e, ok in j["rows"]])
    return view


class LabHttp:
    """(Tier C, C6.1) The step's own HTTP server, for a step whose config or hooks name `{lab_http}`
    (labkit.LAB_HTTP): on the host's loopback for the step's life (run_step closes it before the capture mark), on one
    port per device for the whole scenario (`dev.lab_http_port`: launch-http-flush's queued records must meet the
    same URL on the next launch, or the new URL's own flush would hide the one it tests).  Two routes, scripted as a
    real backend behaves:
      * POST /locations: when the step configures an accessToken (`access_token`), 200 while the request carries the
        token this server last issued (the scenario's own, then `lab-access-<n>`), else 401 — the SDK's authorization
        path (HttpService.java:440-456): it refreshes, fires `authorization`, and flushes again.  With none configured,
        200.  A step's `lab_http: {locations: <status>}` answers every upload with that status instead.
      * POST /refresh: 200 with a new access token when the body carries the refresh token (the scenario's
        refreshPayload, `{refreshToken}` substituted by the SDK), else 401.
    `revoke()` (`actions: server: revoke`) invalidates the issued token without telling the device, so the SDK's next
    upload is refused; (C5.3) `lab_http: {revoked: true}` starts the server that way.  Every answer is marked into the
    device's log after it is sent — path, status, the bearer token it carried and the `location` records it posted —
    which labkit's labHttp metrics read; none after close().
    (WO-114) `killed()` (run_host, after an `actions: kill`) counts the host's kills of the app.  The server remembers
    every record it accepted by its `timestamp`, and how many kills had happened by then; a record accepted again
    after a later kill is one the SDK uploaded twice across a process death.  From the first kill on, a mark also
    carries `kills=<n> replayed=<n>`: the kills so far, and how many of this request's records were such repeats."""
    ACCESS, REFRESH = labkit.LAB_HTTP_ACCESS, labkit.LAB_HTTP_REFRESH

    def __init__(self, dev, access_token=None, locations=None, revoked=False):
        self.dev, self.lock, self.closed, self.n = dev, threading.Lock(), False, 0
        self.issued, self.valid, self.token_required, self.locations = 1, access_token, access_token is not None, locations
        if revoked:                 # (Tier C, C5.3) `lab_http: {revoked: true}`: refused from the first upload
            self.valid = None
        self.kills, self.seen = 0, {}            # (WO-114) kills so far; timestamp -> kills when first accepted
        lab = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                lab.answer(self)

            do_GET = do_PUT = do_POST

            def log_message(self, *args):        # the device's log is the record, not the host's stderr
                pass
        try:        # allow_reuse_address (HTTPServer's): the previous step's closed server leaves the port in TIME_WAIT
            self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", getattr(dev, "lab_http_port", 0)), Handler)
        except OSError as e:
            raise SystemExit("the lab server could not bind 127.0.0.1:%s (%s) — the step did not run as declared"
                             % (getattr(dev, "lab_http_port", 0), e))
        self.httpd.daemon_threads = True
        self.port = dev.lab_http_port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def answer(self, req):
        got = time.time()
        size = int(req.headers.get("Content-Length") or 0)
        body = req.rfile.read(size) if size > 0 else b""
        route = req.path.split("?", 1)[0]
        auth = req.headers.get("Authorization") or ""
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else auth or "-"
        records, replayed = 0, 0
        with self.lock:
            if req.command == "POST" and route == "/locations":
                status = self.locations or (401 if self.token_required and token != self.valid else 200)
                reply = {} if status == 200 else {"error": "access token not issued by this server"}
                try:
                    root = json.loads(body.decode()).get("location")
                    records = len(root) if isinstance(root, list) else 1 if isinstance(root, dict) else 0
                    if status == 200:            # (WO-114) accepted before an earlier kill, and now again
                        for rec in root if isinstance(root, list) else [root]:
                            stamp = rec.get("timestamp") if isinstance(rec, dict) else None
                            if stamp is not None and self.seen.setdefault(str(stamp), self.kills) < self.kills:
                                replayed += 1
                except (ValueError, AttributeError, UnicodeDecodeError):
                    pass
            elif req.command == "POST" and route == "/refresh" and self.REFRESH.encode() in body:
                self.issued += 1
                self.valid = self.ACCESS % self.issued
                status, reply = 200, {"access_token": self.valid, "refresh_token": self.REFRESH}
            elif req.command == "POST" and route == "/refresh":
                status, reply = 401, {"error": "refresh token not issued by this server"}
            else:
                status, reply = 404, {"error": "no such route"}
            data = json.dumps(reply).encode()
            req.send_response(status)
            req.send_header("Content-Type", "application/json")
            req.send_header("Content-Length", str(len(data)))
            req.end_headers()
            req.wfile.write(data)
            if not self.closed:
                since_kill = " kills=%d replayed=%d" % (self.kills, replayed) if self.kills else ""
                self.dev.mark("server i=%d method=%s path=%s status=%d auth=%s records=%d%s got_ms=%d mark_ms=%d"
                              % (self.n, req.command, route, status, mark_value(token) or "-", records, since_kill,
                                 got * 1000, time.time() * 1000))
                self.n += 1

    def revoke(self):
        with self.lock:
            self.valid = None

    def killed(self):
        with self.lock:
            self.kills += 1

    def close(self):
        with self.lock:              # an answer being marked finishes first; none is marked after
            self.closed = True
        self.httpd.shutdown()
        self.httpd.server_close()


def run_host(dev, step, t_launch, deadline, stop, host, server=None, plat=None):
    """(Tier C, C5.0) The host's `moves:` and `actions:` for one step, on its own thread (run_step): each acts once the
    log shows every one of its `after:` conditions (labkit.after_unmet, over the driver's own lines and the host's
    marks), plus its `delay_s`, a move's hops `every_s` apart.  Each send is marked into the device's log after it
    returns, carrying `sent_ms`, when it began (the host clock), as a dialog mark carries `tapped_ms`.  (C5.3 review)
    A move a declaration counts deliveries after (labkit.move_anchors) is marked BEFORE its first hop as well,
    `move-begin i=<i>`: that mark's stamp is the anchor, never later than the send (labkit._delivered_after).  Only
    those moves: the mark takes 0.1 to 0.3 s, and no other step's sends move by it.  Until `stop` or `deadline`;
    `host` gathers when each one's conditions held and each send began (s from launch), and the error that stopped
    it, which run_step's stop_runner turns into an unscored step."""
    items = [("moves", i, m) for i, m in enumerate(step.get("moves") or [])] + \
        [("actions", i, a) for i, a in enumerate(step.get("actions") or [])]
    plan, anchored = labkit.move_plan(step), labkit.move_anchors(step)
    state = {(key, i): {"held": None, "next": None, "hop": 1} for key, i, _ in items}
    try:
        while not stop.is_set() and time.time() < deadline and state:
            lines, norm = dev.peek(), None
            now = time.time()
            for key, i, item in items:
                st = state.get((key, i))
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
                    host["held_s"]["%s%d" % (key[:-1], i)] = round(now - t_launch, 3)
                if now < st["next"]:
                    continue
                began = time.time()
                if key == "moves":
                    h, n = st["hop"], item.get("hops", 1)
                    lat, lon = plan[i][h - 1]
                    if h == 1 and i in anchored:
                        dev.mark("move-begin i=%d" % i)
                        began = time.time()          # `sent_ms` stays the send's own start
                    dev.move(lat, lon)
                    # (C5.0 review) `mark_ms`, just before the mark: labkit.sent_on_device puts the send on the device's
                    # clock from the mark's own stamp, since the emulators run 0.4 to 2.6 s off the host
                    dev.mark("move i=%d hop=%d/%d to=%s,%s sent_ms=%d mark_ms=%d"
                             % (i, h, n, lat, lon, began * 1000, time.time() * 1000))
                    st["hop"], st["next"] = h + 1, began + item.get("every_s", 0)
                    host["sent_s"]["move%d.%d" % (i, h)] = round(began - t_launch, 3)
                    if h < n:
                        continue
                else:
                    # (WO-191) The kind's handler (labkit.ACTION_HANDLERS), a product's among them.  Exactly one, as
                    # _validate_moves checked at load: anything else is refused here, never sent as another kind.
                    kinds = [k for k in labkit.ACTION_HANDLERS if k in item]
                    if len(kinds) != 1:
                        raise SystemExit("action %d does %s — not exactly one kind labkit.ACTION_HANDLERS knows"
                                         % (i, " and ".join(kinds) or "nothing"))
                    what = labkit.ACTION_HANDLERS[kinds[0]].run(dev, item, plat, server)
                    done = time.time()
                    dev.mark("host i=%d action=%s sent_ms=%d done_ms=%d mark_ms=%d"
                             % (i, what, began * 1000, done * 1000, time.time() * 1000))
                    host["sent_s"]["action%d" % i] = round(began - t_launch, 3)
                del state[(key, i)]
            stop.wait(labkit.POLL_S)
    except SystemExit as e:           # a thread's SystemExit is swallowed silently: kept for stop_runner()
        host["error"] = str(e.code if isinstance(e.code, str) else e)
    except Exception as e:
        host["error"] = "%s: %s" % (type(e).__name__, e)


def end_capture_log(dev, plat, out, phases):
    """The step's log, ended and written into the capture as logcat.txt; then (WO-191) the product's own reads into
    the capture (labkit.CAPTURE_EXTRAS: fetch's `dumpsys jobscheduler <pkg>`), after the log is safe on disk, so an
    extra that fails costs no log.  Its own function so the selftest can drive it: run_step needs a device."""
    log_text = dev.end_log(plat)
    phases["log_ended"] = time.time()
    with open(os.path.join(out, "logcat.txt"), "w") as f:
        f.write(log_text)
    phases["log_written"] = time.time()
    if labkit.CAPTURE_EXTRAS is not None:
        labkit.CAPTURE_EXTRAS(dev, plat, out)
        phases["capture_extras"] = time.time()
    return log_text


def run_step(dev, plat, scen, var, step, args, timings=None):
    label = "%s.%s.%s.%s" % (plat["name"], scen["name"], var["name"], step["name"])
    out = labkit.capture_dir(plat["name"], scen["name"], var["name"], step["name"], dev.os)
    os.makedirs(out, exist_ok=True)

    t_step = time.time()
    # (Tier C, C7.0) Where the step's seconds go, for run.json `timings`: each boundary it passes
    # (host epoch here, seconds from t0 there), and every backend call it makes.  Calls come from any
    # thread (the dialog watcher and the live-verdict pusher run on their own) and include a backend's
    # calls on itself (Simctl.sdk_db -> container -> _run), so each method is shadowed by a counting
    # instance attribute until the step's finally.  Seconds are inclusive: a nested call is in its
    # caller's total too.  `bg_n`/`bg_s` are the share made off this step's own thread, which overlaps
    # it rather than adding to it.  A step that raises before its teardown is through leaves them on,
    # and main() ends the run.
    phases, calls, calls_lock, shadowed = {}, {}, threading.Lock(), {}
    step_thread = threading.current_thread()

    def counted(name, fn):
        def call(*a, **k):
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                dt = time.perf_counter() - t
                bg = threading.current_thread() is not step_thread
                with calls_lock:
                    c = calls.setdefault(name, [0, 0.0, 0, 0.0])
                    c[0] += 1
                    c[1] += dt
                    if bg:
                        c[2] += 1
                        c[3] += dt
        return call
    for name in dir(dev):
        fn = getattr(dev, name)
        if not name.startswith("__") and callable(fn):
            shadowed[name] = vars(dev).get(name)       # None: the class's own method shows through again
            setattr(dev, name, counted(name, fn))

    def measured():
        """(Tier C, C7.0) timings.phases, in seconds from t0 in the order passed, and timings.calls."""
        with calls_lock:
            table = {name: {"n": n, "s": round(s, 3), "bg_n": bn, "bg_s": round(bs, 3)}
                     for name, (n, s, bn, bs) in sorted(calls.items())}
        return {"phases": {name: round(t - t_step, 3) for name, t in sorted(phases.items(), key=lambda kv: kv[1])},
                "calls": table}

    # (WO-027) A picture from an earlier run must not pass as this one's: an evidence step takes
    # its screenshot only after it is scored, so a step that cannot be scored leaves none.
    # summary.txt too: a step that goes unscored (contaminated) must not leave the last run's verdict
    # beside this run's log for anything that reads it.
    for stale in ("after-trigger.png", "top-activity.txt", "summary.txt"):
        if os.path.exists(os.path.join(out, stale)):
            os.remove(os.path.join(out, stale))
    # (Tier C, C6.0; decision 15 A) `process: live` launches into the process the previous step's finish() left running
    # (that step skipped its own force-stop, below): not stopped here, and a process that is gone, or is not the one
    # that ran the previous step's driver (labkit.live_refusal), is refused (unscored), never cold-launched in its
    # place.  labkit.scenario holds the key to Android, a later step after a finish, `permissions: keep`, and no
    # reinstall or revoke.
    live_pid = None
    if step.get("process") == "live":
        live_pid = dev.alive(plat)
        before = scen["steps"][scen["steps"].index(step) - 1]
        why = labkit.live_refusal(labkit.capture_dir(plat["name"], scen["name"], var["name"], before["name"], dev.os),
                                  live_pid)
        if why:
            raise SystemExit("%s: `process: live`, but %s %s; nothing from this step is scored"
                             % (label, plat["package"], why))
        phases["live_checked"] = time.time()
    else:
        dev.terminate(plat)
        phases["terminated"] = time.time()
    # (WO-027) A scenario starts from a fresh install.  Only force-stopping between scenarios let
    # the SDK's persisted state leak from one to the next: `permission-motion` leaves the SDK
    # enabled, and `permission-request` run after it found ready() resuming tracking and raising
    # the OS dialog itself before its own requestPermission hook could — a PASS or FAIL decided
    # by run order.  First step only: a multi-step scenario carries its state between its OWN
    # steps on purpose (recreate-*, terminate-finish, launch-http-flush).
    # (WO-027, permissions) Unless a step says `fresh: true`: never-asked again after an earlier
    # answer.  `prompt:` alone cannot give that mid-scenario: an Android permission the user denied
    # is not never-asked, and a fresh install is the only reset measured to re-arm the iOS upgrade
    # alert.  Stopping the other lab apps stays first-step only.
    keep = step.get("permissions", "grant") == "keep"
    fresh = bool(step.get("fresh"))
    # (WO-027, permissions) Whether this step STARTS from a fresh install, which the first step always
    # does whatever it declares.  The provenance below records this, not the key.
    reset = step is scen["steps"][0] or fresh
    actions = list((step["dialog"] or {}).get(dev.os) or [])   # the declared sequence, this OS
    evicted = foreign_apps = None        # (WO-191) first step only: run.json `evicted` / `foreignTransistorApps`
    if step is scen["steps"][0]:
        # (WO-027) And no OTHER lab app is left running: one left on the Simulator for two hours
        # ticked through every flutter-ios step of the 2026-09-24 board and decided when each one
        # ended (how it was left is not known).  Ours to stop: the device is claimed.
        # (WO-027, C5.2) Nor able to be woken mid-step: on iOS a stopped one is relaunched by its own
        # geofences when the location moves, so there evict uninstalls it (Simctl.evict).
        # (WO-191) Every lab's, read from the device: the background-fetch lab shares it (labkit.evictions).
        evicted, foreign_apps = labkit.evictions(dev.installed((labkit.VENDOR_PACKAGE_PREFIX,)), plat["package"],
                                                 dev.os)
        for package in evicted:
            dev.evict(package)
        phases["others_stopped"] = time.time()
    if reset:
        dev.reset_app(plat, artifact(plat))
        if dev.os == "ios" and step is scen["steps"][0]:
            assert_native(dev, plat, var)          # (Tier C, C7.1) the app this reinstall put there
        phases["reset"] = time.time()
    # (WO-027) The app is down, so any alert still on screen belongs to nothing — and the watcher
    # below would tap it at t=0 and mark an answer for a dead window.
    # (WO-027, permissions) What the sweep tapped is provenance (the launch mark, run.json).  On a
    # `keep` step it is a lab defect: the tap may have answered a permission (a motion alert raised
    # after the previous step's observer stopped), so the state carried in is no longer the user's.
    swept = dev.dismiss_stale_dialog(plat)
    phases["swept"] = time.time()
    if swept is not None and keep:
        raise SystemExit("%s: the host swept a stale alert (%s) before this `permissions: keep` step — "
                         "that tap may have answered a permission, so the state it carries is no longer "
                         "what the user answered; nothing from this step is scored" % (label, swept))
    reverted = dev.neutral()
    network = getattr(dev, "network_state", None)       # (Tier C, C5.0) what neutral() read, before this step
    phases["neutral_pre"] = time.time()
    accuracy = set_accuracy(dev, step, label)
    if accuracy is not None:
        phases["accuracy"] = time.time()
    if getattr(dev, "privacy", None) is not None:        # (Tier C, C5.0) this step's `simctl privacy` calls only
        dev.privacy.clear()
    # (WO-027, permissions) `permissions: keep` carries what the user actually answered on the
    # previous step (a WhenInUse, a denial, a declined upgrade) into this launch.  Every other step
    # re-grants everything first, which overwrites that answer before the app can see it.  labkit
    # refuses `keep` with a `prompt:` or `revoke:`, on a first step, and with `fresh: true`.
    if not keep:
        dev.permissions(plat, step["revoke"])
        # (WO-027) `prompt:` is not `revoke:`: it puts a permission back to "never answered", which
        # is the only state that produces the OS dialog.  On iOS a revoke means DENIED and never
        # prompts again, so a scenario that conflated them would test the refusal path believing it
        # tested the dialog.
        if step["prompt"]:
            dev.prompt(plat, step["prompt"])
        phases["permissions"] = time.time()
    # (WO-027, permissions) The OS's own record of the state this request starts from, where a
    # step's meaning depends on it: Android's `dumpsys package` grant and USER_SET/USER_FIXED flags
    # decide whether the next denial is the first or the permanent one.  Simctl's returns None:
    # TCC's state cannot be read back.  The hasattr keeps a backend without the method working.  A
    # read that fails is recorded as failed rather than left out: a missing snapshot looks like a
    # step that never needed one.
    flags = None
    # Only on a step whose meaning depends on the permission state (declared `fresh`, not step 0's
    # reinstall: every scenario's first step would pay a dumpsys, and risk its timeout, for nothing).
    if (step["prompt"] or fresh or keep) and hasattr(dev, "permission_flags"):
        try:
            flags = dev.permission_flags(plat)
        except (Exception, SystemExit) as e:    # device_call's timeout is a SystemExit: record it
            flags = {"error": str(e)}
            print("[permissions] flags snapshot failed: %s" % e)
        # Adb's summary is None when dumpsys lists none of the permissions (the package is not
        # installed), which is a finding, not "no snapshot".  Simctl's None is the latter.
        if flags is None and dev.os == "android":
            flags = {"error": "no runtime-permission lines in dumpsys"}
        phases["flags"] = time.time()
    # (Tier C, C7.2) A Configuration revert recreates what is on screen: it settles before a launch
    # (292fbd2's recreate lab).  Only when neutral() changed something — nothing to settle otherwise.
    if reverted:
        time.sleep(2)
    phases["sleep_end"] = time.time()
    dev.begin_log()
    phases["log_begun"] = time.time()
    # (Tier C, C6.1) The step's lab server, once the log is begun (its marks go there), and the step as SENT: the
    # placeholder replaced by the address this device reaches it at.  run.json records the config sent, which
    # configDropped compares with the driver's echo; the hooks as written, which apiAssertions scores against.
    server, sent = None, step
    if labkit.uses_lab_http(step):
        server = LabHttp(dev, (step["config"].get("authorization") or {}).get("accessToken"),
                         (step.get("lab_http") or {}).get("locations"), bool((step.get("lab_http") or {}).get("revoked")))
        sent = labkit.with_lab_http(step, "http://%s:%d" % (labkit.LAB_HTTP_HOST[dev.os], server.port))
        phases["server_started"] = time.time()

    # (Tier C, C4.0) configB64, hooksB64 and eventsB64 come from labkit.launch_payloads — the device is told what
    # to call or subscribe to and which paths to report, never what is expected back — so what travels is
    # exactly what scenario() sized against LAUNCH_LIMIT at load.
    extras = {**labkit.launch_payloads(sent),
              # (WO-027) The recipe's own name, for the header the driver renders: a screenshot
              # should say what it is a screenshot OF.
              "scenario": scen["name"],
              # (Tier C) And the native snapshot it runs on, the header's second line.
              "nativeLabel": var["native"],
              # (WO-027) Which view the driver renders — api, events or title (Tier C, C7.6: the
              # State table is retired).
              "view": step["view"],
              "stopOnTerminate": step.get("stopOnTerminate", True),
              "watch": step.get("watch", False),
              "noStart": step.get("noStart", False),
              # (Tier C, C7.6) No `bootGcp` any more: the drivers' boot getCurrentPosition is gone.
              "persistGcp": step.get("persistGcp", False)}
    # `activity` is not an extra: it picks which Activity variant the scenario launches.
    platform_knobs = dict(step.get("extras") or {})
    activity = platform_knobs.pop("activity", plat.get("activity", ""))
    if activity and "." not in activity:         # `am start -n pkg/Name` wants pkg-relative
        activity = "." + activity
    extras.update(platform_knobs)

    # (WO-027, permissions) The launch mark says what permission state the step started from: a
    # multi-step capture must show whether its request met never-asked, a carried answer or a
    # fresh grant.  `fresh=` is whether it started from a fresh install (true on every first step);
    # `dialog=` is this OS's declared sequence; `swept=` is the stale alert the host tapped, `-` for
    # none; `flags=` is present only where the snapshot above was taken.
    t_mark = time.time()                   # (Tier C, C7.0) the host side of clock_offset_s
    # (Tier C, C6.0) and `process=live pid=` on a step that launched into a live process, the pid alive() read.
    # (Tier C, C8.0) and on Android `accuracy=on|off`, Google Location Accuracy as read right before the launch.
    dev.mark("launch label=%s trigger=%s permissions=%s fresh=%s prompt=%s revoke=%s dialog=%s swept=%s config=%s%s%s%s"
             % (label, step["trigger"], "keep" if keep else "grant", "true" if reset else "false",
                ",".join(step["prompt"]) or "-", ",".join(step["revoke"]) or "-",
                ",".join(actions) or "-", "-" if swept is None else mark_value(swept),
                json.dumps(sent["config"], separators=(",", ":")),
                "" if flags is None else " flags=" + mark_value(
                    flags if isinstance(flags, str) else json.dumps(flags, separators=(",", ":"), sort_keys=True)),
                "" if live_pid is None else " process=live pid=%s" % live_pid,
                "" if accuracy is None else " accuracy=%s" % ("on" if accuracy["now"] else "off")))
    phases["launch_mark"] = time.time()
    # (WO-027) The device view colours what the HOST has judged (labkit.verdict_view), pushed into
    # the app's storage; the driver only draws it.  A push is display, never evidence: one that
    # fails is printed and the run goes on.  The previous step's verdict is cleared first.
    def show(view):
        try:
            dev.push_verdict(plat, None if view is None else json.dumps(view, separators=(",", ":")))
            return True
        except (Exception, SystemExit) as e:
            print("[verdict] push failed: %s" % e)
            return False
    if not show(None):                     # a verdict left on screen would be another step's
        show(None)
    phases["verdict_cleared"] = time.time()
    # (Tier C, C6.0 review) And the same process still, at the launch: the calls since the check above take seconds.
    if live_pid is not None and dev.alive(plat) != live_pid:
        raise SystemExit("%s: `process: live`, but %s's process %s is gone or replaced at the launch; nothing from this "
                         "step is scored" % (label, plat["package"], live_pid))
    dev.launch(plat, activity, extras)
    t_launch = phases["launch"] = time.time()
    stop_live, pusher = threading.Event(), None
    if step["events"] or any("api" in h for h in step["hooks"]):
        def push_live():
            meta, last = {"hooks": step["hooks"], "events": step["events"]}, None
            while not stop_live.wait(1):
                try:
                    view = labkit.verdict_view(labkit.Capture.live(dev.peek(), meta), step)
                except Exception as e:                  # display only, like show()
                    print("[verdict] live read failed: %s" % e)
                    continue
                text = json.dumps(view, sort_keys=True)
                if text != last and show(view):   # a failed push is retried next second
                    last = text
        pusher = threading.Thread(target=push_live, daemon=True)
        pusher.start()

    settle = args.settle if args.settle is not None else step["settle"]
    after = args.after if args.after is not None else step["after"]

    # (Tier C, C5.0) `moves:` and `actions:` (run_host).  A thread, not the evidence loop: a window step only sleeps, and
    # C6's `trigger: finish` step needs a cause after the app is gone.  Stopped and joined before the capture mark, like
    # the dialog watcher.
    acts = bool(step.get("moves") or step.get("actions"))
    host = {"error": None, "held_s": {}, "sent_s": {}}
    stop_host, runner = threading.Event(), None
    if acts:
        runner = threading.Thread(target=run_host, args=(dev, step, t_launch, t_launch + settle + after, stop_host, host,
                                                         server, plat), daemon=True)
        runner.start()

    def stop_runner():
        """(Tier C, C5.0) Stopped and joined before the capture mark, so no host mark lands after the step it belongs
        to; a send that failed leaves the step unscored (exit 2).  (C6.1) The lab server closes here too, after the
        runner that may revoke on it: no answer is marked after the capture mark."""
        if runner is not None:
            stop_host.set()
            runner.join()
            phases["runner_joined"] = time.time()
        if server is not None:
            server.close()
            phases["server_closed"] = time.time()
        if runner is None:
            return
        if host["error"]:
            raise SystemExit("%s: a host move or action failed (%s) — the step did not run as declared; nothing from "
                             "it is scored" % (label, host["error"]))

    # (WO-027) A system permission dialog is the OS's window: the driver cannot see it and the SDK
    # cannot dismiss it, so the HOST watches for one while the step runs.  Polling, not a schedule:
    # the dialog appears whenever the SDK gets around to asking, and a fixed moment would either
    # tap nothing or tap before it is up.  Whatever is tapped is marked into the device's own log,
    # so the capture carries the button label rather than the host's word for it.
    # (WO-027) The actions are ORDERED and each waits for its own window: an Always grant is a
    # sequence (OS dialog, the SDK's rationale, the upgrade, Back), and answering only the first
    # leaves the SDK blocked on one nobody will touch.  A missing window ends the sequence rather
    # than skipping ahead — `dialogAnswered` then names how far the host actually got, and a
    # `point=dialog missed=<action>` mark says which window never came.
    # (WO-027, permissions) Then the host keeps LOOKING, and touches nothing, until the step ends.
    # It starts once the declared sequence is through, or at launch when none is declared.  Each NEW
    # window a dump recognises by its buttons is marked `point=dialog seen=<its buttons>`, which
    # `undeclaredDialogs` counts.  Otherwise "no dialog appeared" cannot be asserted: a window that
    # blocks nothing the step calls leaves no other trace, such as the SDK's Settings alert or a
    # motion prompt raised after the location answer.  It runs on any step that declares a
    # sequence for this OS, and on any step that asserts `undeclaredDialogs`.
    observe = "undeclaredDialogs" in labkit.expectations(step, plat["name"], dev.os)
    watcher = None
    watcher_done = []                  # when the declared sequence finished, for the full-window fallback
    seq_done, stop_watch = threading.Event(), threading.Event()
    watch = {"from_s": None, "looks": 0, "failed": 0, "failed_since_ok": 0, "seen": [], "error": None}
    last_ok = []                       # (began, ended) of the latest look that read the screen
    # (Tier C, C7.0) Per declared action, in s from launch: the start of the attempt that found its
    # window, the tap (the Tap's tapped_ms), and answer_dialog's return, after the backend's gone-check.
    # A tap the backend kept pending (its re-reads failed, or on Android still showed the window) is
    # confirmed by a LATER attempt: the window was found by the last one that began before the tap.
    # (Tier C, C7.5) On Android an attempt's first look can be the dump that confirmed the previous
    # action, so the window may have been read one dump (typically ~2 s) before the attempt began.
    dialogs = [{"action": a, "attempts": 0, "found_s": None, "tapped_s": None, "done_s": None}
               for a in actions]
    if actions or observe:
        def watch_for_dialog(deadline):
            try:
                for action, row in zip(actions, dialogs):
                    button, began = None, []
                    while time.time() < deadline and not stop_watch.is_set():
                        began.append(time.time())
                        row["attempts"] += 1
                        try:
                            button = dev.answer_dialog(action, plat)
                        except SystemExit:
                            raise
                        except Exception as e:          # a dump can race the dialog's own animation
                            button = None
                            print("[dialog] read failed: %s" % e)
                        if button:
                            tapped = getattr(button, "tapped_ms", None)
                            found = max([b for b in began if tapped is None or b <= tapped / 1000] or began[-1:])
                            row.update(found_s=round(found - t_launch, 3), done_s=round(time.time() - t_launch, 3))
                            break
                        stop_watch.wait(1)
                    if not button:
                        print("[dialog] no %s dialog appeared" % action)
                        dev.mark("dialog missed=%s" % action)
                        break
                    # (WO-027, permissions) `tapped_ms=`: when the tap itself returned, in host epoch
                    # ms (Capture._ts's clock).  The mark lands a dump (~2 s) or more later, after the
                    # backend's gone-check, so order against the app's own lines is judged by this field, never
                    # by the mark's timestamp.
                    tapped = getattr(button, "tapped_ms", None)
                    row["tapped_s"] = None if tapped is None else round(tapped / 1000 - t_launch, 3)
                    dev.mark("dialog action=%s button=%s%s" % (action, mark_value(button),
                                                               "" if tapped is None else " tapped_ms=%d" % tapped))
                    print("[dialog] %s -> %s" % (action, button))
                watcher_done.append(time.time())
                seq_done.set()
                watch["from_s"] = round(time.time() - t_launch, 1)
                last = None
                while not stop_watch.is_set():
                    began = time.time()
                    try:
                        seen = dev.visible_dialog(plat)          # one dump, no tap
                    except SystemExit:
                        raise
                    except Exception as e:
                        watch["failed"] += 1
                        watch["failed_since_ok"] += 1
                        print("[dialog] look failed: %s" % e)
                    else:
                        watch["looks"] += 1
                        watch["failed_since_ok"] = 0
                        last_ok[:] = [began, time.time()]
                        # Once per window: a new signature, or one back from None.  A failed look
                        # leaves `last` alone, because "could not look" is not "gone".
                        if seen and seen != last:
                            dev.mark("dialog seen=%s" % mark_value(seen))
                            watch["seen"].append(seen)
                            print("[dialog] seen, not declared: %s" % seen)
                        last = seen
                    stop_watch.wait(1)
            except SystemExit as e:
                # A thread's SystemExit is swallowed without a word (threading.excepthook), so it is
                # kept for stop_watcher() to report.
                watch["error"] = str(e.code if isinstance(e.code, str) else e)
            except Exception as e:
                watch["error"] = "%s: %s" % (type(e).__name__, e)
            finally:
                if not watcher_done:
                    watcher_done.append(time.time())
                seq_done.set()
        watcher = threading.Thread(target=watch_for_dialog, args=(time.time() + settle + after,),
                                   daemon=True)
        watcher.start()

    def stop_watcher(evidence_t=None):
        """(WO-027, permissions) Stopped and JOINED before the capture mark, so no `seen=` lands after
        the step it describes.  The join needs no timeout of its own: every device call the thread
        can be inside carries one.  An absence the host could not watch for is not an absence.  So a
        step that asserts `undeclaredDialogs` is not scored (exit 2, as a dead device is) if the
        watcher died, or unless its LAST successful look began after `evidence_t` and at most
        LOOK_FAILS_ALLOWED looks failed after it.  `evidence_t` is the host's read that found the evidence complete
        (api-done, every declared answer); a window step, or one whose evidence never completed,
        has none, and freshness alone applies.  Every look follows the declared answers anyway: the
        observer starts once they are through.  Before, one look at launch vouched for the whole
        step however many failed after it."""
        if watcher is None:
            return
        t_stop = time.time()
        stop_watch.set()
        watcher.join()
        phases["watcher_joined"] = time.time()
        # Seconds from launch, for run.json: the last good look, the stop, the evidence it had to follow.
        watch.update(last_look_s=[round(t - t_launch, 1) for t in last_ok] or None,
                     stop_s=round(t_stop - t_launch, 1),
                     evidence_s=None if evidence_t is None else round(evidence_t - t_launch, 1))
        if watch["error"]:
            print("[dialog] watcher stopped: %s" % watch["error"])
        if not observe:
            return
        if watch["error"]:
            why = watch["error"]
        elif not last_ok:
            why = "no look completed after the declared dialogs"
        elif evidence_t is not None and last_ok[0] <= evidence_t:
            why = ("no look began after the evidence at %.1f s; the last good one began at %.1f s"
                   % (evidence_t - t_launch, last_ok[0] - t_launch))
        elif watch["failed_since_ok"] > LOOK_FAILS_ALLOWED:
            why = ("%d looks failed after the last good one, which ended %.1f s before the step stopped"
                   % (watch["failed_since_ok"], t_stop - last_ok[1]))
        else:
            return
        raise SystemExit("%s: the host could not watch the screen through this step (%s) — "
                         "`undeclaredDialogs` would vouch for windows nobody looked for; nothing "
                         "from this step is scored" % (label, why))

    # (WO-027) End on evidence.  A step whose every metric reads the whole log (labkit.end_policy)
    # ends once all of its evidence is in — the api batch done, each event at its `min`, each
    # dialog answered — plus a grace for a late crash (labkit.Grace, per step), AND a tick newer than
    # the one the evidence arrived on: proof the app is still alive, not merely quiet.  The host decides from
    # its own read of the log; the device is never told what counts as done.  The liveness tick is
    # one FIRST SEEN after the grace — a tick seen during it proves nothing about its end, and ticks
    # queued behind a batch flush within milliseconds of the evidence — and "complete" is re-read at
    # the moment of ending.  Missing evidence — the case the window exists for — falls back to
    # exactly the old window, dialog watcher included.  An explicit --settle/--after asks for that
    # window, so it keeps it.  summarize.py --replay-end models this rule over captures on disk.
    policy = "window" if (args.settle is not None or args.after is not None) else \
        labkit.end_policy(step, plat["name"], inverted=labkit.must_fail(var, scen["name"], step["name"]))
    end = {"policy": policy, "ended": "window"}
    ev_read = beat_read = None                       # (Tier C, C7.0) the reads log_phases replays, below
    if policy == "evidence":
        triggered, ev_t, grace_alive, confirming = False, None, None, False
        read_n, grace_n = 0, None
        parse_s = []                                 # (Tier B, B3) what each poll's read of the log costs
        deadline = t_launch + settle + after
        # (WO-027) How long past the evidence: labkit.Grace — proof on Android (every foreground
        # service promoted, and ActivityManager agrees), a per-step constant on iOS, the old 12 s
        # wherever proof is missing.
        # (Tier C, C7.3) `--nonfinal end` ends a non-final step on the same proof a final step gets —
        # the A/B arm labkit's END_GRACE_S comment asks for before the 12 s hold is lifted.
        nonfinal = getattr(args, "nonfinal", labkit.NONFINAL)
        grace = labkit.Grace(step, dev.os, plat["package"],
                             final_step=step is scen["steps"][-1] or nonfinal == "end",
                             deadline_s=labkit.fgs_deadline_s(getattr(dev, "info", None)))
        while time.time() < deadline:
            if not triggered and time.time() >= t_launch + settle:
                dev.mark("trigger %s" % step["trigger"])
                phases["trigger"] = time.time()
                dev.fire(step["trigger"], plat, activity)
                triggered = True
            t_read = time.time()
            lines, now = dev.peek(), time.time()
            ok, alive = labkit.evidence_state(lines, step, dev.os)
            if not ok:
                ev_t = None                          # a read that regressed restarts the clock
            elif ev_t is None:
                ev_t, ev_read = now, (lines, read_n)
            read_n = len(lines)
            grace_end = grace.update(lines, now, ev_t)
            parse_s.append(time.time() - t_read)
            if grace_end is None or now < grace_end:
                grace_alive, confirming = None, False  # the liveness tick must come after the grace
                phases.pop("grace_end", None)
            elif grace_alive is None:
                if grace.fixed is None and not grace.held and now < ev_t + labkit.END_GRACE_S:
                    # Ending early on Android: ActivityManager must agree with the SDK's log.
                    try:
                        foreground = dev.fgs_all_foreground(plat, grace.started)
                    except (Exception, SystemExit) as e:
                        foreground = False
                        print("[end] dumpsys failed: %s" % e)
                    if not foreground:
                        grace.hold("dumpsys: a service is not in the foreground")
                if grace.held is None or now >= ev_t + labkit.END_GRACE_S:
                    grace_alive = alive              # the liveness lines counted once the grace ran out
                    phases["grace_end"], grace_n = now, len(lines)
            elif alive > grace_alive:                # a beat or tick born after the grace: alive through it
                if grace.fixed is None and not grace.held and not confirming and labkit.tick_gcp(step):
                    # The tick that proves liveness also starts a LocationRequestService every other
                    # time, logged up to 494 ms after it (the measured maximum): read the log once
                    # more before ending, so that start is paired (or holds the step) instead of
                    # slipping past unexamined.  (WO-027, Tier B) Only where the driver runs its
                    # per-tick fix (labkit.tick_gcp): without it the tick starts no service.
                    confirming = True
                    time.sleep(0.5)
                    continue
                end.update(ended="evidence", evidence_s=round(ev_t - t_launch, 1),
                           end_s=round(time.time() - t_launch, 1))
                phases["alive"], beat_read = now, (lines, grace_n)
                break
            time.sleep(labkit.POLL_S)
        if ev_t is not None:
            phases["evidence"] = ev_t
        end["grace"] = grace.record()
        if parse_s:
            end.update(poll_s=labkit.POLL_S, parse_ms=round(1000 * sum(parse_s) / len(parse_s), 1),
                       parse_ms_max=round(1000 * max(parse_s), 1))
        if not triggered:              # ended inside settle: the mark still precedes capture
            dev.mark("trigger %s" % step["trigger"])
            phases["trigger"] = time.time()
            dev.fire(step["trigger"], plat, activity)
        if end["ended"] != "evidence" and actions:
            # The old window: the host watched `after` seconds past the watcher's last tap.  The
            # SEQUENCE's end, not the thread's: the thread now keeps looking until stop_watcher().
            seq_done.wait(timeout=max(0, after))
            if watcher_done:
                time.sleep(max(0, watcher_done[0] + after - time.time()))
        phases["end"] = time.time()
        print("[end] %s" % ("evidence complete at %.1f s, ended at %.1f s" % (end["evidence_s"], end["end_s"])
                            if end["ended"] == "evidence" else "full window (%s)" % ("evidence incomplete"
                            if ev_t is None else "no liveness line after the grace")))
        stop_watcher(ev_t)
        stop_runner()
        dev.mark("capture")                # (WO-027) its screenshot waits for the verdict, below
        phases["capture_mark"] = time.time()
    else:
        time.sleep(settle)
        phases["settle_end"] = time.time()
        dev.mark("trigger %s" % step["trigger"])
        phases["trigger"] = time.time()
        dev.fire(step["trigger"], plat, activity)
        for i in range(1, step.get("trigger_repeat", 1)):
            time.sleep(5)                  # (WO-027, GUARDS timing) the first recreation booted within 1.7 s
            dev.mark("trigger %s (%d)" % (step["trigger"], i + 1))
            dev.fire(step["trigger"], plat, activity, on=(i % 2 == 0))

        if actions:
            seq_done.wait(timeout=max(0, after))
        time.sleep(min(10, after))
        dev.snapshot(out)
        time.sleep(max(0, after - 10))
        phases["end"] = time.time()
        stop_watcher()
        stop_runner()
        dev.mark("capture")
        phases["capture_mark"] = time.time()

    stop_live.set()                        # peek() ends with the log
    if pusher is not None:
        pusher.join(timeout=10)
        if not pusher.is_alive():
            phases["pusher_joined"] = time.time()
    log_text = end_capture_log(dev, plat, out, phases)
    # (WO-027, Tier B) Did the driver keep the schedule the step declared — its anchor, a slot per
    # `at_s` and none late, the per-tick fix the host expects?  A LAB defect, not a verdict: printed
    # and recorded in run.json, never scored.
    live = labkit.Capture.live(log_text.splitlines(), {})
    schedule = labkit.schedule_report(live, step)
    for defect in schedule:
        print("[schedule] LAB DEFECT: %s" % defect)
    # (Tier C, C7.0) The emulator's clock against the host's, read from the log already pulled: the
    # launch mark's own stamp minus the host time at the middle of its call, good to half that call's
    # round trip.  The Simulator logs on this Mac's clock, so iOS records null, as does a log that
    # lost the mark.
    clock_offset = None
    if dev.os == "android":
        stamps = [live._ts(l) for l in live.lines if "point=launch label=%s " % label in l]
        if stamps and stamps[0] is not None:
            clock_offset = round(stamps[0] / 1000 - (t_mark + phases["launch_mark"]) / 2, 3)
    # (Tier C, C7.0) What the host's reads only bound, from the device's own stamps, on the phases'
    # timeline (s from t0, less clock_offset_s): `boot`, the driver's first app=boot; `evidence`, the
    # line that completed it, replayed over the read that found it; `beat`, the first liveness line
    # that read had not seen at the grace, in the read that ended the step.
    log_phases = {}
    if dev.os == "ios" or clock_offset is not None:
        def at(line):
            ms = live._ts(line)
            return None if ms is None else round(ms / 1000 - (clock_offset or 0) - t_step, 3)
        log_phases["boot"] = next((at(l) for l in live.lines if labkit.TAG + " app=boot " in live.norm[l]), None)
        if ev_read is not None:
            read, since = ev_read
            log_phases["evidence"] = next((at(l) for k, l in enumerate(read[since:], since + 1) if labkit.TAG in l
                                           and labkit.evidence_state(read[:k], step, dev.os)[0]), None)
        if beat_read is not None:
            read, since = beat_read
            own = labkit.app_pids(read)
            log_phases["beat"] = next((at(l) for l in read[since:] if labkit.line_pid(l) in own and any(
                labkit.TAG + " app=%s " % kind in labkit.normalize(l) for kind in ("tick", "beat"))), None)
        log_phases = {name: t for name, t in log_phases.items() if t is not None}
    with open(os.path.join(out, "transistor_log.db"), "wb") as f:
        f.write(dev.sdk_db(plat))
    phases["db_pulled"] = time.time()
    with open(os.path.join(out, "run.json"), "w") as f:
        json.dump({"label": label, "platform": plat["name"], "os": dev.os,
                   # (WO-191) the app under test, which summarize hands contamination: a re-score needs it too
                   "package": plat["package"],
                   "scenario": scen["name"],
                   "variant": var["name"], "step": step["name"], "serial": dev.serial,
                   # (WO-191) a first step's: what it stopped (Android) or uninstalled (iOS), and the non-lab Transistor
                   # apps it found (iOS leaves them installed, D1), so a board shows a dirty device.  null: a later step
                   "evicted": evicted, "foreignTransistorApps": foreign_apps,
                   "native": var.get("native"), "native_source": native_source(var, dev.os),
                   # (WO-027) what the installed artifact actually embeds, where readable
                   "native_resolved": dev.native_resolved(plat, artifact(plat)),
                   "plugin": plugin_path(plat, var),
                   # (WO-027) and which commit of it: `plugin` alone read "default"
                   "plugin_source": plugin_source(plat, var),
                   # (WO-027) which device, not just which serial: one serial served two AVDs on
                   # 2026-09-23, and nothing in the captures could tell them apart
                   "device": getattr(dev, "info", None),
                   "activity": activity,
                   # (Tier C, C6.0) the live process a `process: live` step launched into (null: a fresh process)
                   "process": None if live_pid is None else {"live": True, "pid": live_pid},
                   "settle": settle, "after": after,
                   # (WO-027) how the step ended: its policy, and whether it ended on evidence
                   "end": end,
                   # (WO-027) wall clock per phase, so "how long does a test take on this bridge"
                   # is answerable from the capture.  step_s covers terminate..capture, which is
                   # settle + after plus whatever the bridge itself cost.
                   # (Tier C, C7.0) Beside it: t0, the host epoch at the step's start; `phases`, each
                   # boundary passed in s from t0; `calls`, {method: {n, s, bg_n, bg_s}} per backend
                   # method; the declared `dialogs`; `clock_offset_s` and the mark round trip that
                   # bounds it; `log_phases`, device stamps on the same timeline.  Phases and calls
                   # from here on are added at the step's end.
                   "timings": dict(timings or {}, step_s=round(time.time() - t_step, 1), t0=round(t_step, 3),
                                   **measured(), dialogs=dialogs, clock_offset_s=clock_offset,
                                   clock_offset_rtt_s=None if clock_offset is None
                                   else round(phases["launch_mark"] - t_mark, 3),
                                   log_phases=log_phases),
                   "measure_from": step.get("measure_from", "trigger"),
                   # (Tier C, C7.3) which A/B arm ran: the non-final hold and Android's post-step pause
                   "arm": {"nonfinal": getattr(args, "nonfinal", labkit.NONFINAL),
                           "post_settle": getattr(args, "post_settle", labkit.POST_SETTLE)},
                   # (WO-027, permissions) the permission state the step STARTED from, as the launch
                   # mark says it: re-granted or carried (`keep`), a fresh install or not (every first
                   # step is; `fresh_declared` is the step's own key), what was put back to never-asked
                   # or revoked, this OS's declared dialog sequence, the stale alert the host swept
                   # (null for none), and the OS's own flags where the backend reads them (null where
                   # it took none).
                   "permissions": "keep" if keep else "grant", "fresh": reset, "fresh_declared": fresh,
                   "prompt": step["prompt"], "revoke": step["revoke"], "dialog": actions,
                   "swept": swept, "permission_flags": flags,
                   # (WO-027, permissions) what the dialog watcher saw after the declared sequence:
                   # when it started looking, how many looks completed or failed, each new window, and
                   # what stop_watcher judged its watch by (s from launch).
                   "dialog_observer": dict(watch) if watcher is not None else None,
                   # (Tier C, C5.0) the host's moves and actions as resolved (labkit._validate_moves), which movesSent
                   # and actionsDone score, and when each one's conditions held and each send began (s from launch);
                   # what Adb.neutral() read of wifi, data and battery saver before the step, against the defaults
                   # `lab_devices.py up` recorded (null: nothing recorded); each `simctl privacy` call's return code.
                   "moves": step.get("moves"), "actions": step.get("actions"),
                   "host": host if acts else None,
                   "network": network, "privacy": list(getattr(dev, "privacy", None) or []) or None,
                   # (Tier C, C8.0) Google Location Accuracy: what the step asked for, the device's first read and its read
                   # right before the launch (set_accuracy); null on iOS, which has none.
                   "location_accuracy": accuracy,
                   "config": sent.get("config") or {},
                   # (Tier C, C6.1) the lab server's address as sent, null for a step that starts none, and its script
                   "lab_http": None if server is None else "http://%s:%d" % (labkit.LAB_HTTP_HOST[dev.os], server.port),
                   "lab_http_script": step.get("lab_http"),
                   # (WO-027) what the driver was told beyond hooks and events — in the capture
                   # only as the `app=boot args=` line, which the iOS log cuts at ~1 KB
                   # (WO-027, Tier B) and tickGcp, which no extra carries: the driver derives it, and
                   # the replay (summarize.py) reads it back from here rather than from today's YAML.
                   # (Tier C, C7.6) No bootGcp.  A capture from before carries it, for schedule_report
                   # to read that capture's anchor against; the live check above hands it none.
                   "driver": dict({k: extras[k] for k in ("stopOnTerminate", "watch", "noStart", "persistGcp")},
                                  tickGcp=labkit.tick_gcp(step)),
                   # (WO-027, Tier B) labkit.schedule_report's LAB defects; [] when the schedule held.
                   "schedule": schedule,
                   # (WO-027) the events as WRITTEN, for the same reason as the hooks below.
                   "events": step["events"],
                   # (WO-027) the hooks as WRITTEN, not as wired: apiAssertions scores against
                   # this, so a harness bug that drops a hook is itself caught.
                   "hooks": step["hooks"],
                   "year": time.localtime().tm_year,
                   "when": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=1)
    phases["run_json"] = time.time()

    # (WO-027) Scored here, once, and shown before the screenshot: the capture's picture carries
    # the verdict summary.txt records.  A window step already took its picture mid-window (a
    # trigger's effect is what it shows); an evidence step takes it now.
    # judge() can raise (a capture that lost its trigger or capture mark cannot be scored), and the
    # app must still be put down: cleanup is a `finally`.  A display failure never aborts the run.
    try:
        j = judge(out, label, plat, scen, var, step)
        phases["judged"] = time.time()
        try:
            view = final_view(j, step)
        except Exception as e:
            print("[verdict] final view failed: %s" % e)
            view = None
        if view is not None and show(view):
            phases["verdict_pushed"] = time.time()
        # Every step, not only an evidence step: a window step's app was put down the moment its
        # verdict was pushed, before the driver's next poll, so its screen never left `scoring…`
        # unless a settle happened to follow (Android's 3 s after a Configuration trigger).
        time.sleep(VERDICT_SETTLE_S)
        phases["settled"] = time.time()
        if policy == "evidence":
            dev.snapshot(out)
            phases["snapshot"] = time.time()
    finally:
        dev.neutral()
        phases["neutral_post"] = time.time()
        # (WO-027) The 3 s let a Bold/fontScale/night revert settle before force-stop (292fbd2),
        # and gave a following step of the same scenario a settled device.  Nothing else needs
        # it: every capture file is written above, and on iOS a late crash report is matched to
        # its own process now (backends.crash_reports), not to whichever step began after its
        # file time.
        # (Tier C, C7.3) `--post-settle 0` drops the pause after a non-final step (the A/B's third arm,
        # varied apart from the hold); after a Configuration trigger it is always 3 s.
        post_settle = 3 if step["trigger"] in ("bold", "fontscale", "night") else \
            getattr(args, "post_settle", labkit.POST_SETTLE) if step is not scen["steps"][-1] else 0
        if dev.os == "android" and post_settle:
            time.sleep(post_settle)
            phases["post_sleep"] = time.time()
        # (Tier C, C6.0) Looking ahead: a next step that says `process: live` relaunches into this step's process, so
        # it is left running.  A run that stops before that step still force-stops it (main's finally).
        if labkit.keeps_process(scen, step):
            phases["kept_live"] = time.time()
        else:
            dev.terminate(plat)
            phases["terminated_post"] = time.time()
        # (Tier C, C7.0) The backend's own methods back, then this step's later phases and calls
        # added to run.json in place.  Best effort: a failure is printed and the step's outcome stands.
        for name, fn in shadowed.items():
            if fn is None:
                delattr(dev, name)
            else:
                setattr(dev, name, fn)
        try:
            path = os.path.join(out, "run.json")
            with open(path) as f:
                record = json.load(f)
            record["timings"].update(measured())
            with open(path + ".tmp", "w") as f:
                json.dump(record, f, indent=1)
            os.replace(path + ".tmp", path)
        except Exception as e:
            print("[timings] run.json kept its first write: %s" % e)
    return out, label, j


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--scenario")
    ap.add_argument("--variant", default="FIX")
    ap.add_argument("--os", default="android", choices=["android", "ios"])
    ap.add_argument("--serial", help="adb serial, or a simulator UDID with --os ios")
    ap.add_argument("--build", action="store_true", help="build the app for this variant first")
    ap.add_argument("--no-install", action="store_true")
    # (Tier C, C7.3) The non-final-step A/B's switches; the defaults are what it chose (labkit.NONFINAL).
    ap.add_argument("--nonfinal", choices=["hold", "end"], default=labkit.NONFINAL,
                    help="hold: a non-final step keeps END_GRACE_S (12 s); end: it ends on proof, like a final step")
    ap.add_argument("--post-settle", type=int, choices=[0, 3], default=labkit.POST_SETTLE,
                    help="Android: seconds to pause after a non-final step, before force-stop")
    ap.add_argument("--settle", type=int)
    ap.add_argument("--after", type=int)
    # (WO-027) sweep.py builds every cell first, one at a time, then runs both OS lanes with
    # nothing building: a build that claims no device and needs no scenario.
    ap.add_argument("--build-only", action="store_true", help="build and stamp; claim no device")
    args = ap.parse_args()
    if not args.scenario and not args.build_only:
        ap.error("--scenario is required (unless --build-only)")

    plat = labkit.platform(args.platform, args.os)
    var = labkit.variant(args.variant)
    # A retired variant stays on disk because summarize.py re-scores its captures through it
    # (must_fail inverts the verdict); it is never RUN again.  inventory.py lists them.
    if var.get("retired"):
        raise SystemExit("variant %s is retired: %s" % (var["name"], var["retired"]))
    if args.build_only:
        t = time.time()
        build(plat, var)
        print("[build] %s/%s ok in %.1f s" % (plat["name"], plat.get("os", "android"), time.time() - t))
        return 0
    scen = labkit.scenario(args.scenario)

    reason = labkit.applicability(scen, plat)
    if reason:
        print("n/a  %s / %s: %s" % (plat["name"], scen["name"], reason))
        return 0

    owner = "%s/%s/%s" % (plat["name"], scen["name"], var["name"])
    serial = devices.pick(args.serial, owner, args.os)
    dev = backends.backend(args.os, serial)
    print("[device] %s (%s)" % (serial, args.os))
    try:
        # (WO-027) Before anything is built or installed: a dead or full device fails here, loudly.
        info = dev.preflight()
        print("[device] %s" % ", ".join("%s=%s" % kv for kv in info.items()))
        # (WO-027) How long each phase took, recorded per capture: the question "how long does a
        # test take on this bridge" should be answerable from the captures, not a stopwatch.
        timings = {}
        if args.build:
            t = time.time()
            build(plat, var)
            timings["build_s"] = round(time.time() - t, 1)
        if dev.os == "ios":
            # (WO-027) --no-install cannot mean "use what is installed" on iOS: the first step's
            # reset_app uninstalls and reinstalls the artifact on disk.  So its stamp is checked
            # all the same — it used to reach the device unchecked.  (Tier C, C7.1) And nothing is
            # installed here either: that reinstall made this one redundant (≈0.43 s a run), and
            # assert_native runs right after it (run_step), on the app that actually launches.
            check_stamp(plat, var)
        else:
            if not args.no_install:
                t = time.time()
                install(dev, plat, var)
                timings["install_s"] = round(time.time() - t, 1)
            assert_native(dev, plat, var)

        verdicts = []
        for step in scen["steps"]:
            print("\n== %s / %s / %s / %s / %s" % (plat["name"], args.os, scen["name"], var["name"], step["name"]))
            out, label, j = run_step(dev, plat, scen, var, step, args, timings)
            if j["foreign"]:
                print("   CONTAMINATED: %s" % ", ".join(j["foreign"]))
                print("   => FAIL (another lab, or another lab app, was on %s)" % serial)
                verdicts.append(False)
                continue
            if not j["expect"]:
                print("   (no expectations for this step)")
                continue
            verdicts.append(labkit.report(j["rows"], inverted=j["inverted"]))
            with open(os.path.join(out, "summary.txt"), "w") as f:
                labkit.report(j["rows"], inverted=j["inverted"], stream=f)
        restore_accuracy(dev, scen)
    finally:
        # (WO-027) A run stopped part-way (a device call timing out, ^C, sweep's SIGTERM) must not
        # leave its app running into the next lab's captures.  Best effort, and quick: sweep sends
        # SIGKILL 10 s after SIGTERM.
        try:
            dev.terminate(plat, timeout=5)
        except BaseException:
            pass
        dev.close()
        devices.release(serial)

    ok = all(verdicts) if verdicts else True
    print("\n%s  %s / %s / %s" % ("PASS" if ok else "FAIL", plat["name"], scen["name"], var["name"]))
    return 0 if ok else 1


if __name__ == "__main__":
    # (WO-027) SIGTERM/SIGHUP run main's `finally` (device release, the live logcat stream torn
    # down) like ^C does; otherwise a killed run left `adb logcat` streaming into .claims forever.
    import signal
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: sys.exit(128 + signum))
    # (WO-027) Exit codes a caller can trust: 0 PASS (or n/a), 1 FAIL, 2 the run could not be
    # judged — a refusal, a dead device, a failed build.  A refusal used to exit 1 like a FAIL.
    try:
        sys.exit(main())
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            sys.exit(2)
        raise
