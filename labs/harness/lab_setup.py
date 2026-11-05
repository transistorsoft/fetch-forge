#!/usr/bin/env python3
"""`./labkit doctor` and `./labkit setup`: from a fresh clone to a lab that sweeps (labs/README.md, "Fresh clone").

    ./labkit doctor [--os android,ios] [--variant FIX]    checks; changes nothing
    ./labkit setup  [--os android,ios] [--variant FIX] [--reference DIR]

doctor checks the machine's tools, then the repo: what is there, and for anything that is not, the one command that
fixes it.  setup runs the same checks and stops if a TOOL is missing — it installs nothing outside the repo (Chris,
2026-09-27: print only) — then does the in-repo steps, each skipped when already done, so it can be run again after
any failure:

    1  labs/.venv with PyYAML, when python3 has none (labkit then runs the harness with it)
    2  the submodules the lab builds from (`--reference DIR` borrows objects from an existing bg-forge checkout)
    3  the Capacitor plugin checkout's node_modules (its build bundles the plugin's own src/)
    4  the native SDK builds the variant names, from the commits its `native_source:` records (labs/m2, labs/xcframeworks)
    5  the Lab devices (lab_devices.py up)
    6  the lab apps (run.py --build-only), each one whose stamp is stale

Stdlib only at import: a machine without PyYAML can still run doctor, and setup's first step is what provides it.
"""
import argparse
import datetime
import glob
import json
import os
import platform as host
import re
import shutil
import subprocess
import sys
import time

HARNESS = os.path.dirname(os.path.abspath(__file__))
LABS = os.path.dirname(HARNESS)
REPO = os.path.dirname(LABS)
VENV = os.path.join(REPO, "labs", ".venv")
SDK = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.join(
    os.path.expanduser("~"), "Library", "Android", "sdk")        # lab_devices.py's, and the build.sh files'
# The submodules a sweep builds from: the SDK sources a label is built from (and, for iOS, the public dist repo,
# whose podspec publish_native_ios.sh stamps), the four plugins the apps build against, and the types (the
# vocabulary's source, which the selftest reads).  None of the others.
SUBMODULES = {"android": ["native/android"], "ios": ["native/ios", "native/background-geolocation"],
              "both": ["types/background-geolocation", "flutter/background_geolocation",
                       "react-native/background-geolocation", "capacitor/background-geolocation",
                       "cordova/background-geolocation"]}
# The Android build reads these from ~/.gradle/gradle.properties by bare name at CONFIGURATION time, so Gradle stops
# without them (MEASURED 2026-09-27: a fresh clone with an empty GRADLE_USER_HOME): the four in
# signingConfigs.release (tslocationmanager/ and tsfirebaseproxy/build.gradle) — though nothing signs a library AAR —
# and tsfirebaseproxy's publication version, a module the lab never builds.  Every other key there is read through
# findProperty, which answers null.  A machine without Transistor's file gets these placeholders, as
# ORG_GRADLE_PROJECT_* variables: no file is written.
GRADLE_PLACEHOLDERS = {"TS_LOCATION_MANAGER_RELEASE_STORE_FILE": "lab-unsigned.jks",
                       "TS_LOCATION_MANAGER_RELEASE_STORE_PASSWORD": "lab-unsigned",
                       "TS_LOCATION_MANAGER_RELEASE_KEY_ALIAS": "lab-unsigned",
                       "TS_LOCATION_MANAGER_RELEASE_KEY_PASSWORD": "lab-unsigned",
                       "TS_FIREBASEPROXY_VERSION_NAME": "0.0.0-lab"}
SYSTEM_IMAGE = "system-images;android-36;google_apis_playstore;arm64-v8a"      # avd/lab.config.ini's image.sysdir.1
IOS_RUNTIME = "27.2"                                                            # lab_devices.py: iPhone 17 Pro on 27.2
# The floors, from what the lab needs and what it has been proven on (2026-09-27): Xcode 26.4 (React Native's
# template fix, labs/README.md iOS), Dart 3.12.2 (the Flutter app's pubspec), Node 20 (the React Native app's
# engines), JDK 17 (the Android Gradle plugin), CocoaPods 1.16 and Cordova 13 (what the lab was built with).
FLOORS = {"xcode": (26, 4), "dart": (3, 12, 2), "node": (20,), "java": (17,), "pod": (1, 16), "cordova": (13,)}


def probe(cmd, timeout=60, env=None):
    """(exit code, stdout + stderr) of a command, or (None, why) when it cannot run."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return None, "not found"
    except subprocess.TimeoutExpired:
        return None, "timed out after %d s" % timeout


def version(text):
    """The first dotted number in a tool's version output, as a tuple: 'openjdk version "21.0.9"' -> (21, 0, 9)."""
    m = re.search(r"(\d+(?:\.\d+)+|\d+)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def at_least(v, floor):
    return v is not None and v[:len(floor)] >= floor


def dotted(v):
    return ".".join(map(str, v))


class Report:
    """One line per check: `ok`, or MISSING with the one command that fixes it.  `tool` failures stop setup."""

    def __init__(self):
        self.missing = []

    def line(self, ok, name, detail, fix=None, tool=False):
        print("  %-8s %-24s %s" % ("ok" if ok else "MISSING", name, detail))
        if not ok:
            self.missing.append((name, fix, tool))
            if fix:
                print("  %-8s %-24s → %s" % ("", "", fix))
        return ok

    def tools_missing(self):
        return [m for m in self.missing if m[2]]


# ---------------------------------------------------------------------------------------------------- the machine

def check_tools(r, oses):
    print("tools (installed by you; setup installs nothing outside the repo)")
    mac = host.system() == "Darwin" and host.machine() == "arm64"
    r.line(mac, "macOS, Apple silicon", "%s %s" % (host.system(), host.machine()),
           "the lab runs on an Apple-silicon Mac: arm64 emulator images, the iOS Simulator, CoreGraphics", tool=True)
    code, out = probe(["git", "--version"])
    r.line(code == 0, "git", out.strip(), "xcode-select --install", tool=True)
    code, out = probe(["node", "--version"])
    v = version(out) if code == 0 else None
    r.line(at_least(v, FLOORS["node"]), "node >= 20", dotted(v) if v else out.strip(), "brew install node", tool=True)
    code, out = probe(["flutter", "--version", "--machine"], timeout=180)
    dart = None
    if code == 0:
        try:
            dart = version(json.loads(out[out.index("{"):]).get("dartSdkVersion", ""))
        except ValueError:
            pass
    r.line(at_least(dart, FLOORS["dart"]), "flutter (dart >= 3.12.2)", "dart %s" % dotted(dart) if dart else out.strip()[:60],
           "brew install --cask flutter", tool=True)
    code, out = probe(["cordova", "--version"], timeout=120)
    v = version(out) if code == 0 else None
    r.line(at_least(v, FLOORS["cordova"]), "cordova CLI >= 13", dotted(v) if v else out.strip()[:60],
           "npm install -g cordova", tool=True)
    if "android" in oses:
        code, out = probe(["java", "-version"])
        v = version(out) if code == 0 else None
        jbr = "/Applications/Android Studio.app/Contents/jbr/Contents/Home"
        r.line(at_least(v, FLOORS["java"]), "JDK >= 17", dotted(v) if v else out.strip()[:60],
               ('export JAVA_HOME="%s"   (Android Studio\'s own JDK; add it to your shell profile)' % jbr
                if os.path.isdir(jbr) else "brew install --cask temurin@21"), tool=True)
        sdkmanager = "%s/cmdline-tools/latest/bin/sdkmanager" % SDK
        has = lambda *p: os.path.exists(os.path.join(SDK, *p))
        r.line(os.path.isdir(SDK), "Android SDK", SDK,
               "install Android Studio (it installs the SDK at ~/Library/Android/sdk), or set ANDROID_HOME", tool=True)
        r.line(has("cmdline-tools", "latest", "bin", "sdkmanager"), "  sdkmanager", "cmdline-tools/latest",
               "Android Studio → Settings → Android SDK → SDK Tools → Android SDK Command-line Tools", tool=True)
        for name, rel, pkg in [("  platform-tools (adb)", ("platform-tools", "adb"), "platform-tools"),
                               ("  emulator", ("emulator", "emulator"), "emulator"),
                               ("  system image", ("system-images", "android-36", "google_apis_playstore", "arm64-v8a"),
                                SYSTEM_IMAGE)]:
            r.line(has(*rel), name, "/".join(rel), '%s "%s"' % (sdkmanager, pkg), tool=True)
        studio_skin = "/Applications/Android Studio.app/Contents/plugins/android/resources/device-art-resources/pixel_9"
        r.line(has("skins", "pixel_9"), "  Pixel 9 skin", "skins/pixel_9",
               ('mkdir -p "%s/skins" && cp -R "%s" "%s/skins/"' % (SDK, studio_skin, SDK) if os.path.isdir(studio_skin)
                else "create any Pixel 9 device once in Android Studio's Device Manager (it installs skins/pixel_9)"),
               tool=True)
        r.line(has("licenses", "android-sdk-license"), "  SDK licenses", "licenses/android-sdk-license",
               "%s --licenses" % sdkmanager, tool=True)
        code, out = probe(["swift", "--version"])
        r.line(code == 0, "swift", (out.strip().splitlines() or [""])[0][:60],
               "xcode-select --install   (lab_devices.py reads the emulator's window with avd/windows.swift)", tool=True)
    if "ios" in oses:
        code, out = probe(["xcode-select", "-p"])
        r.line(code == 0 and ".app/Contents/Developer" in out, "Xcode selected", out.strip(),
               "sudo xcode-select -s /Applications/Xcode.app", tool=True)
        code, out = probe(["xcodebuild", "-version"])
        v = version(out) if code == 0 else None
        r.line(at_least(v, FLOORS["xcode"]), "Xcode >= 26.4", dotted(v) if v else out.strip()[:60],
               "install Xcode 26.4 or later from the App Store", tool=True)
        code, out = probe(["xcodebuild", "-checkFirstLaunchStatus"])
        r.line(code == 0, "Xcode first launch", "done" if code == 0 else "not done",
               "sudo xcodebuild -runFirstLaunch", tool=True)
        code, out = probe(["xcrun", "simctl", "list", "runtimes", "-j"])
        runtimes = []
        if code == 0:
            runtimes = [rt.get("version") for rt in json.loads(out).get("runtimes", [])
                        if rt.get("platform") == "iOS" and rt.get("isAvailable")]
        r.line(IOS_RUNTIME in runtimes, "iOS %s runtime" % IOS_RUNTIME, ", ".join(runtimes) or "none",
               "xcodebuild -downloadPlatform iOS -buildVersion %s" % IOS_RUNTIME, tool=True)
        code, out = probe(["pod", "--version"], env=dict(os.environ, LANG="en_US.UTF-8"))
        v = version(out) if code == 0 else None
        r.line(at_least(v, FLOORS["pod"]), "CocoaPods >= 1.16", dotted(v) if v else out.strip()[:60],
               "brew install cocoapods", tool=True)


# ---------------------------------------------------------------------------------------------------- the repo

def yaml_python():
    """The python3 that has PyYAML: this one, or labs/.venv's; None when neither has it yet."""
    try:
        import yaml  # noqa: F401
        return sys.executable
    except ImportError:
        venv_py = os.path.join(VENV, "bin", "python3")
        if os.path.exists(venv_py) and probe([venv_py, "-c", "import yaml"])[0] == 0:
            return venv_py
    return None


def submodules(oses):
    return SUBMODULES["both"] + [s for o in oses for s in SUBMODULES[o]]


def uninitialised(oses):
    out = []
    for path in submodules(oses):
        code, text = probe(["git", "-C", REPO, "submodule", "status", "--", path])
        if code != 0 or text.startswith("-") or not os.path.exists(os.path.join(REPO, path, ".git")):
            out.append(path)
    return out


def capacitor_ready():
    plugin = os.path.join(REPO, "capacitor", "background-geolocation")
    return all(os.path.isfile(os.path.join(plugin, "node_modules", dep, "package.json"))
               for dep in ("@capacitor/core", "@transistorsoft/background-geolocation-types"))


def load_variant(name):
    """The variant through labkit, so what setup builds is what a run will ask for.  Needs PyYAML."""
    sys.path.insert(0, HARNESS)
    import labkit
    return labkit.variant(name)


def published(label, os_name):
    if os_name == "android":
        return (os.path.isdir(os.path.join(LABS, "m2", "com", "transistorsoft", "tslocationmanager", label))
                and os.path.isfile(os.path.join(LABS, "m2", ".provenance", label + ".txt")))
    return (os.path.isdir(os.path.join(LABS, "xcframeworks", label, "TSLocationManager.xcframework"))
            and os.path.isfile(os.path.join(LABS, "xcframeworks", ".provenance", label + ".txt")))


def swift_api_published(label):
    """(WO-143) The Swift API beside the label's xcframework: source, the package a native customer adds
    (publish_native_ios.sh).  Its own question, not part of published(): a label published before the copy
    existed still builds the four bridge apps, which never read it."""
    root = os.path.join(LABS, "xcframeworks", label)
    return (os.path.isfile(os.path.join(root, "Package.swift"))
            and os.path.isfile(os.path.join(root, "Sources", "BackgroundGeolocation", "SwiftInterface", "BGGeo.swift")))


def check_repo(r, oses, var_name):
    print("repo (setup does these)")
    py = yaml_python()
    r.line(py is not None, "PyYAML", py or "neither python3 nor labs/.venv has it", "./labkit setup")
    missing = uninitialised(oses)
    r.line(not missing, "submodules", "%d the lab builds from" % len(submodules(oses)) if not missing else
           "not checked out: " + ", ".join(missing), "./labkit setup")
    r.line(capacitor_ready(), "Capacitor plugin deps", "capacitor/background-geolocation/node_modules",
           "./labkit setup")
    if py is None or py != sys.executable:
        print("  (the native builds, devices and apps are checked once PyYAML is there — run ./labkit setup)")
        return None
    var = load_variant(var_name)
    src = var.get("native_source") or {}
    for os_name in oses:
        ok = published(var["native"], os_name)
        fix = "./labkit setup" if src.get(os_name) else (
            "variant %s records no native_source.%s — publish it by hand (labs/README.md, Pinning)" % (var_name, os_name))
        r.line(ok, "native %s" % os_name, "%s (%s)" % (var["native"], "published" if ok else "not built"), fix)
        if os_name == "ios" and ok:                                         # (WO-143)
            swift = swift_api_published(var["native"])
            r.line(swift, "native ios Swift API", "beside the xcframework" if swift else
                   "%s was published without it" % var["native"], fix)
    return var


def check_lab(r, oses, var):
    print("devices and apps (setup does these)")
    code, out = probe([sys.executable, os.path.join(HARNESS, "lab_devices.py"), "status", "--os", ",".join(oses)],
                      timeout=120)
    rows = [l for l in out.splitlines() if l.startswith(("android", "ios"))]
    for os_name in oses:
        for name in ("Lab1", "Lab2"):
            row = next((l for l in rows if l.split()[:2] == [os_name, name]), None)
            up = row is not None and (" booted" in row.lower() or "Booted" in row)
            r.line(up, "device %s %s" % (os_name, name), (" ".join(row.split()[2:]) if row else "not created")[:70],
                   "./labkit up   (or ./labkit setup)")
    sys.path.insert(0, HARNESS)
    import labkit
    import run
    for os_name in oses:
        for p, _ in labkit.lab_cells(labkit.board_order(), [os_name]):     # (WO-143)
            plat = labkit.platform(p, os_name)
            stamp = run.stamp_path(plat)
            fresh = (os.path.exists(run.artifact(plat)) and os.path.exists(stamp)
                     and open(stamp).read().strip() == run.source_stamp(plat, var))
            r.line(fresh, "app %s %s" % (p, os_name), "built from these sources" if fresh else "not built, or stale",
                   "./labkit setup   (or let ./labkit sweep build it)")


# ---------------------------------------------------------------------------------------------------- setup's steps

def step(title):
    print("\n== %s" % title)


def sh(cmd, cwd=None, env=None, log=None):
    """Run a setup step's command, streaming to the terminal, or to a log (its tail is shown if it fails)."""
    print("$ " + " ".join(cmd) + (" > %s" % os.path.relpath(log, REPO) if log else ""))
    if log:
        with open(log, "w") as f:
            rc = subprocess.run(cmd, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT).returncode
        if rc:
            print("".join(open(log, errors="replace").readlines()[-30:]))
    else:
        rc = subprocess.run(cmd, cwd=cwd, env=env).returncode
    if rc:
        raise SystemExit("setup stopped: `%s` exited %d%s — fix that, then run ./labkit setup again"
                         % (" ".join(cmd[:3]), rc, " (log: %s)" % log if log else ""))


# Every submodule URL is HTTPS (.gitmodules, 2026-09-27), so GitHub's credential helper answers for the private
# ones.  A fetch never prompts: a prompt would hang setup, where a failure names what to do.
NET_ENV = {"GIT_TERMINAL_PROMPT": "0"}


def git_net(args, cwd=None):
    """A git command that fetches, never prompting for credentials."""
    print("$ git " + " ".join(args))
    if subprocess.run(["git"] + args, cwd=cwd, env=dict(os.environ, **NET_ENV)).returncode != 0:
        raise SystemExit("setup stopped: `git %s` could not fetch — this machine needs GitHub access to transistorsoft's "
                         "repos (./labkit doctor checks it)" % " ".join(args[:4]))


def check_access(r, oses):
    """GitHub access to the private repos the lab builds from — checked only while a submodule is missing."""
    missing = uninitialised(oses)
    if not missing:
        return
    private = [p for p in ("native/android", "native/ios") if p in missing]
    if not private:
        return
    env = dict(os.environ, **NET_ENV)
    for path in private:
        url = probe(["git", "config", "-f", os.path.join(REPO, ".gitmodules"), "submodule.%s.url" % path])[1].strip()
        ok = probe(["git", "ls-remote", "--heads", url, "master"], timeout=60, env=env)[0] == 0
        r.line(ok, "GitHub access", "%s (%s)" % (url.split("github.com/")[-1], "readable" if ok else "not readable"),
               "gh auth login && gh auth setup-git   (HTTPS credentials for git); ask Chris for access to "
               "transistorsoft's private repos", tool=True)


def ensure_venv():
    step("1  PyYAML")
    if yaml_python():
        print("ok: %s" % yaml_python())
        return
    sh([sys.executable, "-m", "venv", VENV])
    sh([os.path.join(VENV, "bin", "python3"), "-m", "pip", "install", "--quiet", "pyyaml"])


def ensure_submodules(oses, reference):
    step("2  submodules")
    missing = uninitialised(oses)
    if not missing:
        print("ok: all %d checked out" % len(submodules(oses)))
        return
    for path in missing:
        cmd = ["-C", REPO, "submodule", "update", "--init"]
        # A clone from GitHub takes minutes for the big ones (Flutter's is ~6); an existing bg-forge's objects
        # make it seconds.  --reference borrows them: that checkout's module store must stay where it is.
        mod = os.path.join(reference, ".git", "modules", path) if reference else None
        if mod and os.path.isdir(mod):
            cmd += ["--reference", mod]
        git_net(cmd + ["--", path])


def ensure_capacitor():
    step("3  Capacitor plugin deps")
    if capacitor_ready():
        print("ok: capacitor/background-geolocation/node_modules")
        return
    sh(["npm", "ci", "--no-audit", "--no-fund"], cwd=os.path.join(REPO, "capacitor", "background-geolocation"))


def gradle_defines(key):
    if os.environ.get("ORG_GRADLE_PROJECT_" + key):
        return True
    home = os.environ.get("GRADLE_USER_HOME") or os.path.join(os.path.expanduser("~"), ".gradle")
    try:
        text = open(os.path.join(home, "gradle.properties")).read()
    except OSError:
        return False
    return re.search(r"^\s*%s\s*=" % re.escape(key), text, re.M) is not None


def native_checkout(sub, sha, name):
    """A detached worktree of the submodule at the commit, under labs/checkouts (gitignored), made once."""
    repo = os.path.join(REPO, sub)
    if probe(["git", "-C", repo, "cat-file", "-e", sha + "^{commit}"])[0] != 0:
        git_net(["-C", repo, "fetch", "origin", sha])
    wt = os.path.join(LABS, "checkouts", "%s-%s" % (name, sha[:9]))
    if not os.path.isdir(wt):
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        sh(["git", "-C", repo, "worktree", "add", "--detach", wt, sha])
    head = probe(["git", "-C", wt, "rev-parse", "HEAD"])[1].strip()
    if head != sha:
        raise SystemExit("setup stopped: %s is at %s, not %s — remove it (git -C %s worktree remove %s) and run "
                         "./labkit setup again" % (wt, head[:9], sha[:9], sub, wt))
    return wt


def ensure_native(var, oses, logs):
    step("4  native SDK builds for %s (%s)" % (var["name"], var["native"]))
    src, label = var.get("native_source") or {}, var["native"]
    for os_name in oses:
        if published(label, os_name):
            print("ok: %s %s is published" % (os_name, label))
            # (WO-143) A label published before the Swift API was copied beside it: add the source from the
            # commit the binary was recorded at.  No rebuild, and the xcframework is not touched.
            if os_name == "ios" and not swift_api_published(label) and src.get("ios"):
                sh([os.path.join(HARNESS, "publish_native_ios.sh"), "--swift-api", label,
                    native_checkout("native/ios", src["ios"], "native-ios")])
            continue
        if not src.get(os_name):
            raise SystemExit("setup stopped: %s is not published for %s, and variant %s records no native_source.%s to "
                             "build it from" % (label, os_name, var["name"], os_name))
        if os_name == "android":
            wt = native_checkout("native/android", src["android"], "native-android")
            props = os.path.join(wt, "BackgroundGeolocation", "local.properties")
            if not os.path.exists(props):
                with open(props, "w") as f:
                    f.write("sdk.dir=%s\n" % SDK)
            env = dict(os.environ)
            for key, placeholder in GRADLE_PLACEHOLDERS.items():
                if not gradle_defines(key):
                    env["ORG_GRADLE_PROJECT_" + key] = placeholder
            print("(a few minutes: Gradle builds the library and publishes it to labs/m2)")
            sh([os.path.join(HARNESS, "publish_native.sh"), label, wt], env=env, log=os.path.join(logs, "native-android.log"))
        else:
            wt = native_checkout("native/ios", src["ios"], "native-ios")
            print("(several minutes: CocoaPods, then Xcode archives the simulator slice)")
            sh([os.path.join(HARNESS, "build_native_ios.sh"), label, wt] +
               ([str(src["ios_version"]), str(src["ios_build"])] if src.get("ios_version") else []),
               env=dict(os.environ, LANG="en_US.UTF-8"), log=os.path.join(logs, "native-ios.log"))
            sh([os.path.join(HARNESS, "publish_native_ios.sh"), label,
                os.path.join(wt, "build", "Lab", "TSLocationManager_XCFramework_" + label), wt])


def ensure_devices(oses):
    step("5  Lab devices")
    sh([sys.executable, os.path.join(HARNESS, "lab_devices.py"), "up", "--os", ",".join(oses)])


def ensure_apps(var, oses, logs):
    step("6  lab apps")
    sys.path.insert(0, HARNESS)
    import labkit
    import run
    for os_name in oses:
        for p, _ in labkit.lab_cells(labkit.board_order(), [os_name]):     # (WO-143)
            plat = labkit.platform(p, os_name)
            stamp = run.stamp_path(plat)
            if (os.path.exists(run.artifact(plat)) and os.path.exists(stamp)
                    and open(stamp).read().strip() == run.source_stamp(plat, var)):
                print("ok: %s %s" % (p, os_name))
                continue
            t0 = time.time()
            sh([sys.executable, os.path.join(HARNESS, "run.py"), "--build-only", "--platform", p, "--os", os_name,
                "--variant", var["name"]], env=dict(os.environ, LANG="en_US.UTF-8"),
               log=os.path.join(logs, "build-%s-%s.log" % (p, os_name)))
            print("built %s %s in %d s" % (p, os_name, time.time() - t0))


# ---------------------------------------------------------------------------------------------------- main

def main():
    sys.stdout.reconfigure(line_buffering=True)        # its own lines in order with its commands', into a log too
    ap = argparse.ArgumentParser(prog="./labkit doctor|setup", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["doctor", "setup"])
    ap.add_argument("--os", default="android,ios", help="android, ios, or both (default)")
    ap.add_argument("--variant", default="FIX", help="the variant whose native builds and apps to prepare")
    ap.add_argument("--reference", help="an existing bg-forge checkout to borrow submodule objects from")
    a = ap.parse_args()
    oses = [o for o in a.os.split(",") if o]
    if not oses or any(o not in ("android", "ios") for o in oses):
        ap.error("--os is android, ios, or android,ios")
    r = Report()
    check_tools(r, oses)
    check_access(r, oses)
    if a.command == "doctor":
        var = check_repo(r, oses, a.variant)
        if var is not None:
            check_lab(r, oses, var)
        print("\n%s" % ("ready: ./labkit sweep" if not r.missing else
                        "%d to do%s" % (len(r.missing), " — install the tools above first, then ./labkit setup"
                                        if r.tools_missing() else " — ./labkit setup does them")))
        return 1 if r.missing else 0
    if r.tools_missing():
        print("\nsetup stopped before changing anything: install the %d tool(s) marked MISSING above (each line "
              "gives the command), then run ./labkit setup again" % len(r.tools_missing()))
        return 1
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    logs = os.path.join(HARNESS, ".claims", "setup-" + stamp)
    os.makedirs(logs, exist_ok=True)
    ensure_venv()
    py = yaml_python()
    if py != sys.executable:
        # Steps 2-6 import labkit, which needs PyYAML: carry on under the interpreter that has it.
        os.execv(py, [py, os.path.abspath(__file__), "setup", "--os", a.os, "--variant", a.variant] +
                 (["--reference", a.reference] if a.reference else []))
    ensure_submodules(oses, a.reference)
    ensure_capacitor()
    var = load_variant(a.variant)
    ensure_native(var, oses, logs)
    ensure_devices(oses)
    ensure_apps(var, oses, logs)
    print("\nready.  The first sweep:  ./labkit sweep --scenarios api-smoke\n        then the suite:  ./labkit sweep"
          "\n(logs: %s)" % os.path.relpath(logs, REPO))
    return 0


if __name__ == "__main__":
    sys.exit(main())
