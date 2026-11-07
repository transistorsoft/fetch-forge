"""Shared lab library (WO-026): specs, capture parsing, metrics, scoring.

A capture is one step of one scenario on one variant:

    captures/<platform>/<scenario>/<variant>/<step>/        (captures/<platform>-ios/… on iOS)
        logcat.txt          logcat -v time, whole run (os_log rewritten to that shape on iOS)
        transistor_log.db   the SDK's own log database, pulled with run-as
        top-activity.txt    dumpsys activity + window, 10 s after the trigger
        after-trigger.png   screenshot, same moment
        run.json            what was run: marks, serial, spec ids, build provenance

Metrics are computed from the window between the `trigger` and `capture` marks, because
the log database survives `adb install -r` and would otherwise mix runs together.
"""

import base64
import glob
import importlib.util
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.parse

import yaml

LABS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(LABS, "harness")
TAG = "LAB"
# The driver's tick interval (every driver: setInterval(tick, 4000)).  (WO-027, Tier B) The tick is
# liveness and the legacy clock now: its reads, the per-tick getCurrentPosition on a step tick_gcp()
# allows, and the tick number a legacy `call` hook's `at:` counts.  It runs no api batch and arms
# nothing — `at_s` does that — and here it is the margin _at_gate leaves for a liveness tick.
TICK_SECONDS = 4
# (WO-027, Tier B) `at_s` counts from the FIRST BATCH's completion, not from launch.  The launch ->
# first-batch-complete budget _at_gate assumes: the worst non-dialog launch -> boot-fix end measured
# on Android (6.7 s), plus the worst non-dialog first batch (4.1 s, api-smoke), plus margin.  (Tier C,
# C7.6) The boot fix is gone, and the budget kept its ≈4 s of it as margin.
ANCHOR_BUDGET_S = 12
# (Tier C, C5.0) The standing fix every step starts from (backends' begin_log gives it to the emulator and the
# Simulator), and what a move's `to:` is an offset from.  The mean Earth radius the destination uses.
STANDING_FIX = (49.2827, -123.1207)
EARTH_R_M = 6371008.8


# ---------------------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------------------

def _load(path):
    with open(path) as f:
        return yaml.safe_load(f)


# Every key a scenario (and a step inside one) may carry. A scenario is data, and a key the
# harness does not read is silently inert: `extras: {activity: NoUiModeActivity}` at scenario
# level was ignored for a whole session, so the dark-mode scenario launched the Activity that
# cannot recreate and read as a Flutter defect. Unknown keys are now an error.
SCENARIO_KEYS = {"name", "description", "platforms", "not_applicable", "steps"}
STEP_KEYS = {"name", "trigger", "config", "revoke", "hooks", "settle", "trigger_repeat",
             "after", "expect", "platform_expect", "extras", "measure_from",
             "stopOnTerminate", "watch", "noStart", "persistGcp", "events", "dialog", "prompt",
             "view", "end", "permissions", "fresh", "roundtrip", "invalid_values",
             # (Tier C, C5.0) What the HOST does while the step runs, keyed on the log (_validate_moves)
             "moves", "actions",
             # (Tier C, C6.0; decision 15 A) `process: live`: launch into the process the previous step's finish() left
             "process",
             # (Tier C, C6.1) what the step's lab server answers (LAB_HTTP_SCRIPT)
             "lab_http",
             # (Tier C, C8.0) Google Location Accuracy for the step, Android only: run.py sets it before every launch
             "locationAccuracy"}
# (Tier C, C7.6) The driver's boot getCurrentPosition is gone, and the key that switched it is refused
# by name, with the reason, rather than as one unknown key among many.
BOOT_GCP_RETIRED = ("`bootGcp` was retired in Tier C C7.6: a test produces location with start(); "
                    "getCurrentPosition only as an api hook under test")


def _reject_unknown(where, keys, allowed):
    unknown = sorted(set(keys) - allowed)
    if unknown:
        raise SystemExit("%s: unknown key(s) %s — known: %s"
                         % (where, ", ".join(unknown), ", ".join(sorted(allowed))))


# (WO-027) A hook is data too, and `retruns:` would be just as silently inert as a misspelt step
# key.  Two shapes: a legacy tick hook (`call`, fired at tick `at`) and an `api` hook, which
# calls one public API method — in the first batch, or `at_s` seconds after it completes — and
# declares what must come back.
HOOK_KEYS_CALL = {"at", "call", "args"}
# (Tier C, C2) `declared:` generates `returns` from the vocabulary's State tree — see _declare.
# (Tier C, C3) `sets:` is host-only: the Config leaf a generated round-trip hook sets.  run.json records
# it with the hook, so a capture says which key setConfig#n set; wire_hook builds a fresh dict, so it
# never reaches the device.
# (Tier C, C4.0) Three more, as host-only: `invalid:` asks a form the types forbid on purpose (_validate_hooks),
# `outcome:` asserts how the call settles and makes `returns` optional (_api_items), and `settles_only:` marks a
# hook whose argument the lab cannot see take effect — it still asserts its return, and `summarize --coverage
# --args` counts it apart.  `invalid:`, `settles_only:` and `outcome: settled` each excuse something, so each
# carries its date and reason.
# (Tier C, C4.4) `kind:` is host-only too: the invalid-value kind a generated setConfig asks (`invalid_values:`), beside
# its `sets:`.  run.json records both with the hook, so `summarize --divergence` reads which leaf, which kind and which
# value each capture's setConfig#n asked; wire_hook builds a fresh dict, so neither reaches the device.
# (WO-107) `generate:` and `alongside:` reach the device (wire_hook): the first builds addGeofences' argument there,
# the second issues a call beside this one and times it — see _check_generate and _check_alongside.
HOOK_KEYS_API = {"api", "args", "probe", "returns", "at_s", "declared", "sets", "invalid", "outcome", "settles_only",
                 "absent_on",
                 "kind", "generate", "alongside"}
# (WO-107) What `generate:` asks for, each a whole number within its bounds.  Thousands of polygons is the point —
# RN #2668 froze with 7,000 — so the argument is built on the device: as `args` it would be megabytes, and a launch's
# extras must fit ADB_COMMAND_LIMIT.  The logcat ring buffer is the practical ceiling: the SDK logs ~5 lines per
# polygon at the lab's level, and 20,000 lost the head of the log (geofences-bulk-add, MEASURED 2026-10-02).
GENERATE_BOUNDS = {"geofences": (1, 10000), "vertices": (3, 64), "seed": (0, 2 ** 31 - 1)}
DATED = re.compile(r"^\d{4}-\d{2}-\d{2} +\S")
# `settled` is the outcome the types are silent on: measured, so it carries its own date and reason.
# (Tier C, R5) `threw`: the member threw as it was called, and handed back no promise (labapi.js runBatch).  Its own
# side: a caller's `.catch()` never sees it, so it is not a rejection, and `$err` paths read the thrown value.
OUTCOME = re.compile(r"^(resolved|rejected|threw)$|^settled \d{4}-\d{2}-\d{2} +\S")
# What the driver's `api id= outcome=` says of a call that was made, as the side `outcome:` names.
SIDES = {"ok": "resolved", "rejected": "rejected", "threw": "threw"}
# (Tier C, C4.3) What a `settled` hook asks the device for, and the step's `recorded` row carries (recorded()): the root
# on whichever side the call took, and a rejection's `code` and `message`.  React Native rejects an Error, the Flutter
# driver hands on a {code, message} map (C4 decision 4) and Capacitor's pass-through members (ready, setConfig,
# startSchedule, ... : src/index.ts returns NativeModule.x() itself) reject a CapacitorException, an Error whose own
# `code` is usually undefined — each root alone reads `object`.  The Capacitor and Cordova wrappers reject strings
# (neither sub-path).  The row leaves out a sub-path that read `missing` or `undefined`: `e.code` is undefined to a
# JavaScript caller either way.  Host-only: every driver already reports whatever paths it is sent, on the side the
# call took (labapi.js runBatch).
SETTLED_PATHS = ("$", "$err", "$err.code", "$err.message")


def _settled(hook):
    """(Tier C, C4.3) An api hook whose `outcome:` is `settled YYYY-MM-DD <why>`: measured, its reading recorded."""
    return "api" in hook and str(hook.get("outcome", "")).split(" ")[0] == "settled"
# (WO-027) An `events:` declaration: subscribe to one `on*` member, require at least `min`
# deliveries, and assert `returns` against the FIRST one.  A hook cannot express this — a
# callback is not writable in YAML, and `_validate_hooks` refuses a hook that omits a required
# parameter — which is why the 15 `on*` members were reachable only as `probe: true`.
# (Tier C, C4.0) `declared:` generates the payload's paths from the vocabulary, as on an api hook.
EVENT_KEYS = {"subscribe", "min", "returns", "args", "at_s", "declared",
              # (Tier C, C5.0) Absence and removal (decision 7), and a subscription only armed (decision 9):
              # _validate_events says what each takes, _event_items what each reads.  Only `remove_at_s` reaches
              # the device (wire_events): the driver removes the Subscription in that slot.
              "max", "none_after", "min_after", "remove_at_s", "arm_only",
              # (Tier C, R) Both reach the device.  `delivery: N` reads `returns` from the Nth delivery instead of the
              # first (the driver reports deliveries 1..N, each `ret` line naming its own): an EXIT after an ENTER,
              # power save off after on.  `errors: true` passes the member's failure callback too: each error is
              # counted, and the first reports the declaration's `$err` paths, which are recorded, never judged.
              "delivery", "errors"}
# (Tier C, C5.0) What `none_after:` and `min_after: {ref}` count deliveries after.  `remove` is the declaration's own
# `event-removed` line (it needs `remove_at_s`), `<member>@<i>` another declaration's, `<api>#<n>` a hook's `api id=`
# line — each read in log order, the driver's own lines — and `move<i>` that move's first hop, by the stamp of the
# mark the host writes BEFORE it sends (`point=move-begin i=<i>`, run.run_host) against each delivery's stamp.
EVENT_ID = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)@(0|[1-9]\d*)$")
MOVE_ID = re.compile(r"^move(0|[1-9]\d*)$")
# (WO-027, Tier B) `at:` on an api hook or an event was a tick number.  Reusing the key with a new
# unit would let an unconverted `at: 3` silently mean 3 s, so it is refused, with the rename.
AT_RETIRED = ("`at:` counted 4 s ticks and was retired — write `at_s:`, seconds after the first "
              "batch completes")
COMPARATORS = {"type", "equals", "matches", "startsWith", "contains",
               "gte", "lte", "gt", "lt", "absent", "same", "differs", "optional", "under", "record",
               # (Tier C, C5.1) `in: [v, …]`: one of these values, each compared as `equals` compares.  `declared:`
               # generates it on every leaf whose type is closed (an enum, a union of literals), from the vocabulary
               # (_declare); written by hand it is held to the leaf's values the same way (_in_list).
               "in"}
# (Tier C, C4.0) `optional: true` and `under: <path>` qualify the others, on the host — see _qualifiers.
QUALIFIERS = ("optional", "under")
# (Tier C, C4.2) `record: true` reads a path and judges nothing: the driver reports it as any path, and what it
# read goes to the step's `recorded` row, never to a verdict (the C4 plan §2 C, harness item 8).  It stands
# alone, like `absent` — beside a comparator that judges it would be that check with its verdict thrown away,
# and a qualifier beside it has nothing to qualify — and only on an api hook (_validate_hooks, _validate_events).
RECORD = "record"
# (Tier C, C3) `same: <ref>` / `differs: <ref>` compare a path with what an EARLIER api hook of the same
# step reported at that path: `previous` (the nearest one that reports it, resolved at load to its id) or
# `<api>#<n>`.  The reference is the cell's own reading, so "unchanged" and "restored" are asserted for a
# key whose default differs per OS without writing either default (Chris's decision (1), 2026-09-26).
REFS = ("same", "differs")
# (Tier C, R3; R's review) One dot at most: `logger.getLog#0` is a hook's id too.
REF_ID = re.compile(r"^([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)?)#(0|[1-9]\d*)$")
WIRE_TYPES = {"string", "number", "boolean", "null", "undefined", "array", "object",
              "function", "missing"}
PATH = re.compile(r"^\$(err)?(\.[A-Za-z0-9_]+)*$")
# A declared parameter that IS the callback: `cb`, `locationCallback`, `errorCallback?`.
CALLBACK_ARG = re.compile(r"(^cb$|callback)", re.I)


_VOCAB = None


def vocabulary():
    """The SDK surface a scenario may name (WO-027): labs/harness/vocabulary.yaml, curated.
    Read on the host only — it decides what a scenario may ASK, never what a device answers."""
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = _load(os.path.join(HARNESS, "vocabulary.yaml"))
    return _VOCAB


# (WO-191, D3) The product this harness tests, when it is not background-geolocation: `lab_product.py` beside this file,
# never vendored (fetch-forge has one, bg-forge none).  labkit loads it itself, at the end of its own definitions, so
# every process that imports labkit — run.py, sweep.py, summarize.py, a bare `python3 run.py` — runs the product's
# labkit seams or does not run at all, with no environment variable (what it cannot reach: _load_product).
PRODUCT_PATH = os.path.join(HARNESS, "lab_product.py")


def _load_product(module):
    """(WO-191, D3) Load PRODUCT_PATH, when present, call its register(module), then generate the headless metrics from
    its names (with none, from background-geolocation's).  `module` is THIS labkit, by object (sys.modules[__name__]):
    register is called once per labkit module a process holds — twice in the selftest, which runs as __main__ and
    imports run.py's `labkit` too — and must change only the module it is handed.  The product must not import labkit
    at module level (refused, naming the cycle): register is handed it.

    It changes these, in place or by rebinding, each defaulting to background-geolocation's behaviour:
        HEADLESS_NAMES()                   the names a headless task can be handed (headless_names); a product's own
                                           metric under a generated name (headlessFired.<name>) is kept
        IMPLICIT, EARLY_METRICS            keys every expecting step scores, and which of them read the whole log
        EVIDENCE(lines, step, os_name)     a completeness predicate ANDed into evidence_state (None: none)
        SDK_LOG(capture, like)             the SDK's own lines in the window (Capture.sdk)
        CAPTURE_EXTRAS(dev, plat, out)     run.end_capture_log's per-capture extras, after logcat.txt (None: none)
        METRICS, ACTION_HANDLERS           the metric registry, the host action kinds (Part 2)
        NATIVE                             the native SDK each OS's label is built from and published as (Part 5)
    Nothing outside labkit.  register runs inside labkit's import, and run.py and lab_devices.py import backends
    first, which imports labkit: backends is then in sys.modules with its body unfinished, so a write to it is
    overwritten, silently, when its body resumes.  And run.py and sweep.py run as __main__, so `import run` there
    patches a second copy, not the code that runs.  A seam outside labkit.py is not this hook's.

    A product present but not loadable — its import, a missing register, register raising (SystemExit too), or its
    HEADLESS_NAMES failing as the metrics are generated — exits 2: running on with the wrong product's seams reads
    every SDK count as 0, silently.  Returns the product or None."""
    if not os.path.lexists(PRODUCT_PATH):        # a dangling symlink is a product that does not load, not none
        _headless_metrics()
        return None
    path = os.path.realpath(PRODUCT_PATH)
    try:
        product = sys.modules.get("lab_product")
        if product is not None and os.path.realpath(getattr(product, "__file__", "") or "") == path:
            if getattr(product, "__labkit_loading__", False) or getattr(product.__spec__, "_initializing", False):
                raise SystemExit("lab_product imports labkit at module level, so labkit is loading while lab_product "
                                 "still is: use the module register() is handed")
        else:
            spec = importlib.util.spec_from_file_location("lab_product", PRODUCT_PATH)
            product = importlib.util.module_from_spec(spec)
            product.__labkit_loading__ = True
            sys.modules["lab_product"] = product        # one product module per process, whichever labkit loads it
            try:
                spec.loader.exec_module(product)
            except BaseException:
                sys.modules.pop("lab_product", None)
                raise
            del product.__labkit_loading__
        product.register(module)
        _headless_metrics()                          # from its HEADLESS_NAMES, under the same guard
    except (Exception, SystemExit) as e:
        cause = "SystemExit: %s\n" % (e.code,) if isinstance(e, SystemExit) else traceback.format_exc()
        sys.stderr.write("%slabkit: %s did not load (WO-191) — the harness does not run without its product\n"
                         % (cause, PRODUCT_PATH))
        raise SystemExit(2)
    return product


CAPTURE_EXTRAS = None                    # (WO-191) a product's register() may set it: (dev, plat, out), run.run_step

# (WO-191, Part 5) The native SDK a label is built from and published as, per OS: background-geolocation's by default,
# changed in place by a product's register().  `artifacts` / `artifact` are read by lab_setup.published (and inventory's
# ANDROID_ARTIFACTS).  `build` is the OS's slot in lab_setup.ensure_native, build(var, published, logs, setup), setup
# being the running lab_setup module; None is lab_setup's own (native_slot_android / native_slot_ios, its code before
# Part 5).  Android's other four values are what that default slot hands publish_native.sh, background-geolocation's
# script: a product whose publish is its own sets `build` (fetch: publish_native_fetch.sh, which checks the label,
# verifies the POM and AAR, and writes the provenance only after them), and iOS's default slot runs bg-forge's iOS
# scripts, so a product sets that one too.  SUBMODULES is not here: doctor and check_access read it before PyYAML
# exists, when labkit cannot be imported, and GRADLE_PLACEHOLDERS stays beside it (D4, option A, Chris 2026-11-07).
NATIVE = {
    "android": {"submodule": "native/android",                      # the checkout a label is built from
                "build_root": "BackgroundGeolocation",              # inside it: gradlew and local.properties
                "gradle_task": ":tslocationmanager:publishToMavenLocal",
                "version_property": "TS_LOCATIONMANAGER_VERSION_NAME",
                # under labs/m2/com/transistorsoft: the first is the one published() reads; inventory prunes them all
                "artifacts": ("tslocationmanager", "tslocationmanager-gms20"),
                "build": None},
    "ios": {"artifact": "TSLocationManager.xcframework",            # under labs/xcframeworks/<label>/
            "build": None},
}


_DART = None
# The `declared:` values that hold a probe of an enum namespace to the members the docs spell in a language.
SPELT = {"dart": "Dart", "kotlin": "Kotlin", "swift": "Swift"}


def dart_members():
    """(Tier C, R; addendum A1) The enum members the docs give a Dart spelling, per family: labs/harness/
    dart_members.yaml, generated from docforge/db by dart_members.py (whose `--diff` holds it to the db).  Read on
    the host only, as the vocabulary is: it decides which members a `declared: dart` probe asks."""
    global _DART
    if _DART is None:
        _DART = _load(os.path.join(HARNESS, "dart_members.yaml")) or {}
    return _DART


_NATIVE = None


def native_members():
    """(WO-143 stage 4) The enum members the docs give a Kotlin and a Swift spelling, per language and family:
    labs/harness/native_members.yaml, generated from docforge/db by native_members.py.  Read on the host only: it
    decides which members a `declared: kotlin` or `declared: swift` probe asks."""
    global _NATIVE
    if _NATIVE is None:
        _NATIVE = _load(os.path.join(HARNESS, "native_members.yaml")) or {}
    return _NATIVE


def spelt_members(language):
    """{family: {"members": {...}}} for a `declared:` language: dart, kotlin or swift."""
    return dart_members() if language == "dart" else native_members().get(language) or {}


def wire_of(member, key="returns"):
    """The wire types a declared return (or, with key="emits", a declared event payload) can
    honestly produce: void -> null|undefined, X[] ->
    array, the primitives as themselves, anything else (State, Location, an enum object) ->
    object — unless the vocabulary says `wire:`, which only a name that would otherwise read
    `object` may carry (an enum whose values are numbers).  On a return the types already map,
    a `wire:` is the escape hatch this file exists to close."""
    declared = str(member.get(key, "")).strip()
    parts = [p.strip() for p in declared.split("|")]
    if declared == "void":
        mapped = {"null", "undefined"}
    elif declared in ("number", "string", "boolean"):
        mapped = {declared}
    elif declared.endswith("[]") or declared.startswith("Array<"):
        mapped = {"array"}
    elif len(parts) > 1 and all(p in ("void", "number", "string", "boolean") for p in parts):
        # (Tier C, R3; R's review) `void | boolean` (Logger's emailLog and uploadLog) is each of them: read as an
        # object, no honest `$` could be written for it.  A union with any other member stays an object.
        mapped = {w for p in parts for w in (("null", "undefined") if p == "void" else (p,))}
    else:
        mapped = None
    # `wire:` describes the RETURN only: no `on*` member emits an enum, and a second escape
    # hatch on the payload would reopen exactly what this function closes.
    if member.get("wire") and key == "returns":
        if mapped is not None:
            raise SystemExit("vocabulary: `wire: %s` on a `%s` return overrides what the types already "
                             "say — that is a divergence, write a work order" % (member["wire"], declared))
        return set(str(member["wire"]).split("|"))
    return mapped or {"object"}


# What each comparator can only ever pass against (_check is type-strict), so a comparator
# without a `type:` still names a wire type.
def _implied(checks):
    out = set()
    for op, want in checks.items():
        if op == "equals":
            out.add("boolean" if isinstance(want, bool) else "number" if isinstance(want, (int, float))
                    else "null" if want is None else "string")
        elif op in ("gte", "lte", "gt", "lt"):
            out.add("number")
        elif op in ("matches", "startsWith", "contains"):
            out.add("string")
        elif op == "in":                                  # (Tier C, C5.1) each value's, as `equals`
            for v in want if isinstance(want, list) else []:
                out |= _implied({"equals": v})
    return out


# (Tier C, C0) A State is a tree: Config's compound groups minus State.ts's `Omit<Config, …>` list,
# plus State's own keys (vocabulary.yaml `state:`).  Every `$.a.b` a State-returning hook or event
# asserts resolves against it.  An undeclared path is a typo or a flat copy — the lab asserts compound
# paths only — and would run on the device, read `missing` and look like an SDK defect; a `type:` the
# key cannot produce, or an `equals` outside a closed key's values, would launder a divergence.  Below
# `any` (`Record<string, any>`) and `object` nothing is declared, so nothing is checked.
OPEN = "open"
SCOPE_OTHER = {"android": "ios", "ios": "android"}


def state_tree():
    state = vocabulary().get("state") or {}
    tree = {k: v for k, v in (vocabulary().get("config") or {}).items() if k not in (state.get("omit") or [])}
    tree.update(state.get("keys") or {})
    return tree


def payload_type(ts):
    """(Tier C, C4.0) The interface under the vocabulary's `types:` that a declared type names — its {field:
    type} map, `field?` marking an optional field — or None.  `X | null` names X: stepping into it is
    stepping into X, and the null is the leaf's own wire type."""
    parts = [p for p in _alternatives(ts) if p not in ("null", "undefined")] if isinstance(ts, str) else []
    return ((vocabulary().get("types") or {}).get("interfaces") or {}).get(parts[0]) if len(parts) == 1 else None


def _union(ts):
    """`A | B` at the top level only: `Record<string, A | B>`, `(A | B)[]` and `'a | b'` are each
    one alternative."""
    out, depth, quote, cur, prev = [], 0, None, "", ""
    for ch in str(ts):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "<([{":
            depth += 1
        elif ch in ")]}" or (ch == ">" and prev != "="):     # `=>` is an arrow, not a close
            depth -= 1
        if ch == "|" and depth == 0 and not quote:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
        prev = ch
    return out + [cur.strip()]


def _alternatives(ts):
    """_union, with `readonly` dropped and a parenthesised union flattened into its alternatives."""
    out = []
    for part in _union(ts):
        part = re.sub(r"^readonly\s+", "", part)
        if part.startswith("(") and part.endswith(")") and not part.endswith("[]"):
            out += _alternatives(part[1:-1])
        else:
            out.append(part)
    return out


def _array_element(part):
    """`X[]` / `(A | B)[]` / `Array<X>` -> its element type; None when `part` is not an array."""
    if part.endswith("[]"):
        return part[:-2]
    if part.startswith("Array<") and part.endswith(">"):
        return part[len("Array<"):-1]
    return None


def _record_value(part):
    """`Record<K, V>` -> V; None when `part` is not a Record."""
    if not (part.startswith("Record<") and part.endswith(">")):
        return None
    inner, depth = part[len("Record<"):-1], 0
    for i, ch in enumerate(inner):
        depth += (ch in "<([{") - (ch in ">)]}")
        if ch == "," and depth == 0:
            return inner[i + 1:].strip()
    return None


def _enum(part):
    """The curated `enums:` entry for `part`, or None.  A malformed one (a hand edit) refuses instead of
    raising a KeyError that sweep.py would not catch."""
    entry = (vocabulary().get("enums") or {}).get(part)
    if entry is not None and not (isinstance(entry, dict) and entry.get("wire") in ("number", "string")
                                  and entry.get("values")):
        raise SystemExit("vocabulary.yaml: enums.%s needs `wire` (number or string) and `values` — "
                         "run seed_vocabulary.py" % part)
    return entry


def ts_wire(ts):
    """The wire types a declared TypeScript type can produce: primitives (and `null`, `undefined`) as
    themselves, an array `array`, a literal its own type, an enum its values' type, a Record or `object`
    an object, `any`/`unknown` OPEN.  Anything else refuses — a new types construct must fail here, or
    at `seed_vocabulary.py --diff`, instead of being mis-typed as an object."""
    out = set()
    for part in _alternatives(ts):        # `enums:` is seeded from the enum files: no types checkout at load
        if part in ("number", "string", "boolean", "null", "undefined"):
            out.add(part)
        elif part in ("any", "unknown"):
            out.add(OPEN)
        elif _array_element(part) is not None:
            out.add("array")
        elif part[:1] == "[" and part[-1:] == "]":      # (Tier C, C4.0) a tuple: Geofence.vertices' element
            out.add("array")
        elif part[:1] in ("'", '"'):
            out.add("string")
        elif re.fullmatch(r"-?\d+(\.\d+)?", part):
            out.add("number")
        elif part in ("true", "false"):
            out.add("boolean")
        elif _record_value(part) is not None or part in ("object", "Object"):
            out.add("object")
        elif _enum(part):
            out.add(_enum(part)["wire"])
        elif payload_type(part) is not None:            # (Tier C, C4.0) an interface under `types:`
            out.add("object")
        elif re.match(r"^\(.*\)\s*=>", part):          # (Tier C, C4.0) a callback parameter: no JSON is one
            out.add("function")
        else:
            raise SystemExit("vocabulary.yaml: %r (in `%s`) is no type labkit knows — not a primitive, an "
                             "array, a literal, a Record, an enum under `enums:`, nor an interface under "
                             "`types:`; run seed_vocabulary.py --diff" % (part, ts))
    return out


def ts_values(ts):
    """The values a declared type admits when it is CLOSED — an enum, a union of literals — else None.
    Any open alternative opens it: `TriggerActivity[] | TriggerActivity` also admits an array, which a
    core stores joined (`in_vehicle,on_bicycle`, WO-050), so that key is typed, never value-checked."""
    out = set()
    for part in _alternatives(ts):
        if _enum(part):
            out |= set(_enum(part)["values"])
        elif part[:1] in ("'", '"') and part[-1:] == part[:1]:
            out.add(part[1:-1])
        elif re.fullmatch(r"-?\d+", part):
            out.add(int(part))
        else:
            return None
    return out


def _state_leaf(path, root=None):
    """The declared type at `$.a.b…` on a State — a group (dict), a TypeScript type, OPEN below `any`
    or `object` — or None when State declares no such path.  An array takes `.length` and a canonical
    `.N` (the JavaScript drivers read `.00` as a property, the Dart driver as element 0); a Record
    takes any one segment, `length` included (a map has no length), and yields its value type.
    (Tier C, C4.0) With `root`, the same walk from a member's declared return, emit or rejection type
    (`Location`, `Geofence[]`, `LocationError`) instead of State: an interface under `types:` is a group,
    whose `field?` is its field too; a tuple (`[number, number]`, a Geofence vertex) is an array whose
    `.N` stops at its arity.  `$err.a` walks as `$.a` does."""
    node = state_tree() if root is None else root
    for seg in str(path).split(".")[1:]:
        node = payload_type(node) or node
        if isinstance(node, dict):
            key = next((k for k in (seg, seg + "?") if k in node), None)
            if key is None:
                return None
            node = node[key]
            continue
        if OPEN in ts_wire(node):
            return OPEN
        parts = [p for p in _alternatives(node) if p not in ("null", "undefined")]
        arrays = [e for e in map(_array_element, parts) if e is not None]
        tuples = [[t.strip() for t in p[1:-1].split(",")] for p in parts
                  if p[:1] == "[" and p[-1:] == "]" and _array_element(p) is None]
        records = [v for v in map(_record_value, parts) if v is not None]
        # EVERY alternative must take the step: `TriggerActivity[] | TriggerActivity` is also a plain
        # string — the cores store it joined (WO-050) — and no driver steps into a string.
        if len(arrays) + len(tuples) + len(records) + sum(p in ("object", "Object") for p in parts) != len(parts):
            return None
        if (arrays or tuples) and seg == "length":
            node = "number"
        elif arrays and re.fullmatch(r"0|[1-9]\d*", seg):
            node = arrays[0]
        elif tuples and re.fullmatch(r"0|[1-9]\d*", seg) and int(seg) < len(tuples[0]):
            node = tuples[0][int(seg)]
        elif records:
            node = records[0]
        elif "Object" in parts or "object" in parts:
            return OPEN
        else:
            return None
    return node


def state_path_wire(path, root=None):
    """The wire types `$.a.b…` on a State (or, (Tier C, C4.0), on `root`: see _state_leaf) can produce;
    OPEN below an open-ended key; None when State declares no such path."""
    leaf = _state_leaf(path, root)
    if leaf is None or leaf is OPEN:
        return leaf
    if isinstance(leaf, dict):
        return {"object"}
    wire = ts_wire(leaf)
    return OPEN if OPEN in wire else wire


def state_scope(path):
    """'android' / 'ios' when the types mark `$.a.b…` (or a group it sits in) one-OS; a group all of
    whose attributes carry the same mark is that OS's too.  None for both."""
    scope = {k: os_ for os_ in ("android", "ios") for k in ((vocabulary().get("scope") or {}).get(os_) or [])}
    segs = str(path).split(".")[1:]
    for i in range(len(segs), 0, -1):
        if ".".join(segs[:i]) in scope:
            return scope[".".join(segs[:i])]
    node = _state_leaf(path)
    if not isinstance(node, dict):
        return None
    below = []
    def walk(n, prefix):
        for k, v in n.items():
            walk(v, prefix + k + ".") if isinstance(v, dict) else below.append(scope.get(prefix + k))
    walk(node, ".".join(segs) + ".")
    return below[0] if below and below[0] and all(b == below[0] for b in below) else None


def _check_state_path(where, path, kinds, checks, excused=(), root=None):
    # (Tier C, C4.0) `root` None is State (C0); otherwise the member's declared return, emit or rejection
    # type, walked by _state_leaf — the same checks, but a payload has no Omit list and no one-OS scope.
    # Below `$` of a `void`, or of an interface `types:` does not seed, nothing is declared to hold a path to.
    # (Tier C, C5.0) Every emit is seeded now, the six that were C5's included (seed_vocabulary.PAYLOAD_TYPES).
    bare = root and (_array_element(root) or root)
    if bare == "void" or bare and re.fullmatch(r"[A-Z]\w*", bare) and not _enum(bare) and payload_type(bare) is None:
        raise SystemExit("%s %s: %s — only `$` can be asserted" % (where, path, "the types declare `void`, no value"
                         if bare == "void" else "`%s` is not seeded under the vocabulary's `types:` "
                         "(seed_vocabulary.PAYLOAD_TYPES)" % bare))
    name = "State" if root is None else "`%s`" % root
    allowed = state_path_wire(path, root)
    omitted = (vocabulary().get("state") or {}).get("omit") or []
    if root is None and allowed is None and str(path).split(".")[1] in omitted:
        raise SystemExit("%s %s: State omits it — a Config input only (State.ts `Omit<Config, …>`)" % (where, path))
    parent = _state_leaf(str(path).rsplit(".", 1)[0], root) if "." in str(path) else None
    parent = payload_type(parent) or parent
    if allowed is None and isinstance(parent, str) and parent is not OPEN:
        raise SystemExit("%s %s: %s declares `%s` there, and `.%s` does not step into it — an array takes "
                         "`.length` and a canonical `.N`, a tuple an `.N` below its arity, a Record any one key, "
                         "a scalar nothing, and every "
                         "alternative of a union must take it" % (where, path, name, parent, str(path).rsplit(".", 1)[1]))
    if allowed is None:
        raise SystemExit("%s %s: %s declares no such path — a typo%s" % (
            where, path, name, ", or a flat copy of a compound key (the lab asserts "
            "`$.geolocation.distanceFilter`, never `$.distanceFilter`)" if root is None else ""))
    asked = set(kinds) | _implied(checks)
    if allowed is not OPEN and asked - allowed:
        raise SystemExit("%s %s: the types declare it wire %s — asserting %s here would launder a "
                         "divergence; write a work order instead"
                         % (where, path, "|".join(sorted(allowed)), "|".join(sorted(asked - allowed))))
    leaf = _state_leaf(path, root)
    values = ts_values(leaf) if isinstance(leaf, str) and leaf is not OPEN else None
    want = checks.get("equals")
    if values is not None and "equals" in checks and (isinstance(want, (list, dict)) or want not in values):
        raise SystemExit("%s %s: the types declare `%s` — %r is none of its values (%s)"
                         % (where, path, leaf, checks["equals"], ", ".join(sorted(map(str, values)))))
    # (Tier C, C5.1) Every value an `in:` lists, as `equals`' one: a value outside the type would launder a divergence.
    outside = [v for v in checks.get("in") or [] if values is not None and v not in values]
    if outside:
        raise SystemExit("%s %s: the types declare `%s` — `in:` lists %s, none of its values (%s)"
                         % (where, path, leaf, ", ".join(map(repr, outside)), ", ".join(sorted(map(str, values)))))
    # (Tier C, C4.0) `optional` passes a `missing` whose parent is there: for a `?` field, an array element
    # or a Record's value.  On a field its type declares without `?` it would pass the very absence the types
    # (and, on a State, decision (1)) make red; on an array's `.length`, which every array has, it would pass
    # an object where the types declare the array (the drivers read `.length` of a map as missing, parent=1).
    last = str(path).rsplit(".", 1)[-1]
    arrayed = isinstance(parent, str) and any(_array_element(p) is not None or p[:1] == "[" for p in _alternatives(parent))
    if checks.get("optional") and (isinstance(parent, dict) and last in parent or last == "length" and arrayed):
        raise SystemExit("%s %s: `optional` on %s — its absence is the defect, never a pass" % (
            where, path, "a field %s declares required" % name if isinstance(parent, dict)
            else "an array's `.length`, which every array has"))
    if root is not None:
        return
    # Chris's decision (2), 2026-09-24: a one-OS key is asserted in a scenario that excuses the other
    # OS with a dated not_applicable — never on both, where the other OS would read `missing` as red.
    only = state_scope(path)
    if only and SCOPE_OTHER[only] not in excused:
        raise SystemExit("%s %s: the types mark it [%s only] — assert it in a scenario whose "
                         "not_applicable excuses %s, never on both OSes"
                         % (where, path, "Android" if only == "android" else "iOS", SCOPE_OTHER[only]))


def _refuse_missing(where, kinds):
    if "missing" in kinds:
        raise SystemExit("%s: `type: missing` can never pass — _check reports an absent key as `missing` "
                         "before it reads `type`; assert `absent: true`" % where)


def _in_list(where, checks):
    """(Tier C, C5.1) `in:` is a non-empty list of scalars — strings or numbers, what a closed type's values are."""
    if "in" in checks and not (isinstance(checks["in"], list) and checks["in"] and all(
            isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in checks["in"])):
        raise SystemExit("%s: `in:` is a non-empty list of strings or numbers — not %r" % (where, checks["in"]))


def _qualifiers(where, path, checks, returns):
    """(Tier C, C4.0) `optional: true` and `under: <path>` qualify a path's comparators on the host, like
    every comparator: the device is told the path and nothing else.  `optional` passes a `missing` whose
    parent is there (a `?` field, an empty array's `.0`; _check), and `under` names the container the path
    is conditional on, which the same `returns` reports: where that container reads `missing` with its
    parent there, or fails its own check (null, not an object), the path is excused, so one defect reads
    once, on the container (_excused).  Neither stands alone — `{optional: true}` would pass a null — and
    neither qualifies a root, which is always reported."""
    if not set(checks) & set(QUALIFIERS):
        return
    if not set(checks) - set(QUALIFIERS):
        raise SystemExit("%s %s: `optional` and `under` qualify a comparator, and there is none — write the "
                         "`type:` they qualify" % (where, path))
    if str(path) in ("$", "$err"):
        raise SystemExit("%s %s: `optional`/`under` on a root — the root is always reported" % (where, path))
    if "optional" in checks and checks["optional"] is not True:
        raise SystemExit("%s %s: `optional` is true, or left out — not %r" % (where, path, checks["optional"]))
    under = checks.get("under")
    if "under" in checks and not (isinstance(under, str) and str(path).startswith(under + ".") and under in returns):
        raise SystemExit("%s %s: `under: %s` — the container this path is conditional on is a strict ancestor "
                         "of it that the same `returns` asserts, so the device reports it" % (where, path, under))
    # (Tier C, C4.2) A container's own check decides whether its children are excused; a recorded one decides nothing.
    if "under" in checks and (returns.get(under) or {}).get(RECORD):
        raise SystemExit("%s %s: `under: %s` names a recorded path — `record` judges nothing, so it cannot say "
                         "whether the children are there; type the container" % (where, path, under))


def _validate_config(where, table, vocab):
    """Every `group.key` a scenario configures exists in the types.  A key the SDK does not
    read is merged, echoed and carried by every driver — `configDropped` cannot see a typo.
    (Tier C, C4.0) And returns why the first VALUE is not of its key's type (_conform), or None: a step's
    `config:` refuses it, a hook's argument carries a dated `invalid:` past it.  A misspelt key refuses
    outright, anywhere in the table — it is a typo, never a form under test."""
    why = None
    for key, value in (table or {}).items():
        if key not in vocab:
            raise SystemExit("%s: config key %r is not in the SDK vocabulary (labs/harness/vocabulary.yaml)"
                             % (where, key))
        if isinstance(vocab[key], dict) and isinstance(value, dict):
            found = _validate_config("%s.%s" % (where, key), value, vocab[key])
        else:
            found = _conform(value, vocab[key], "%s.%s" % (where, key))
        why = why or found
    return why


# (Tier C, C4.0) A vocabulary `args:` entry is `name: Type` as the types spell it, `name?` when optional.  The
# Config parameter (ready, setConfig, reset) is walked through the vocabulary's config tree, not `types:`.
CONFIG_TYPES = ("Config", "Partial<Config>")


def param(declared):
    """(Tier C, C4.0) One vocabulary `args:` entry as (name, optional, type)."""
    name, _, ts = str(declared).partition(":")
    return name.strip().rstrip("?"), name.strip().endswith("?"), ts.strip()


def _conform(value, ts, at):
    """(Tier C, C4.0) Why `value` — a hook's argument, or one config value — is not of the declared type `ts`, or
    None.  JSON is what the device is handed, so its wire type first; then a closed type's values; an interface's
    fields, each key one it declares (or its index signature takes) and each field it declares without `?`
    present; and, recursively, what an array, a tuple, a Record or a field holds.  A union admits what any one
    alternative admits.  A dict `ts` is an object spelled inline (LocationInput.coords); a Config goes through
    the config tree (_validate_config).  The reason starts with `at`."""
    wire = ("null" if value is None else "boolean" if isinstance(value, bool) else "number"
            if isinstance(value, (int, float)) else "string" if isinstance(value, str) else
            "array" if isinstance(value, list) else "object")
    parts = [ts] if isinstance(ts, dict) else _alternatives(ts)
    fits = [p for p in parts if (wire == "object" if isinstance(p, dict) or p in CONFIG_TYPES
                                 else bool({wire, OPEN} & ts_wire(p)))]
    if not fits:
        return "%s is %s — the types declare %s" % (
            at, "null" if value is None else "an " + wire if wire[0] in "ao" else "a " + wire,
            "`%s`" % ts if isinstance(ts, str) else "an object")
    values = ts_values(ts) if isinstance(ts, str) else None
    if values is not None:                                # a closed type, named whole: every value it takes
        return None if value in values else "%s is %s — `%s` is none of %s" % (
            at, json.dumps(value), ts, ", ".join(map(str, sorted(values, key=str))))
    why = None
    for part in fits:
        box = part if isinstance(part, dict) else payload_type(part)
        element = None if box is not None or part in CONFIG_TYPES else _array_element(part)
        if part in CONFIG_TYPES:
            found = _validate_config(at, value, vocabulary().get("config") or {})
        elif box is not None:
            unknown = [k for k in value if not {str(k), "%s?" % k, "[key: string]"} & set(box)]
            missing = [f for f in box if not f.endswith("?") and f != "[key: string]" and f not in value]
            found = ("%s.%s: `%s` declares no such field" % (at, unknown[0], part if isinstance(part, str) else at)
                     if unknown else "%s leaves out `%s`, which the types require" % (at, missing[0]) if missing
                     else next(filter(None, (_conform(v, box.get(str(k), box.get("%s?" % k, box.get("[key: string]"))),
                                                      "%s.%s" % (at, k)) for k, v in value.items())), None))
        elif element is not None:
            found = next(filter(None, (_conform(v, element, "%s.%d" % (at, i)) for i, v in enumerate(value))), None)
        elif part[:1] == "[":                             # a tuple: Geofence.vertices' element, `[number, number]`
            each = [p.strip() for p in part[1:-1].split(",")]
            found = ("%s holds %d — the types declare `%s`" % (at, len(value), part) if len(value) != len(each) else
                     next(filter(None, (_conform(v, t, "%s.%d" % (at, i))
                                        for i, (v, t) in enumerate(zip(value, each)))), None))
        elif _record_value(part) is not None:
            found = next(filter(None, (_conform(v, _record_value(part), "%s.%s" % (at, k)) for k, v in value.items())),
                         None)
        else:                                             # an enum or a literal inside an open union
            values = ts_values(part)
            found = (None if values is None or value in values else "%s is %s — `%s` is none of %s"
                     % (at, json.dumps(value), part, ", ".join(map(str, sorted(values, key=str)))))
        if found is None:
            return None
        why = why or found
    return why


def _transportable(where, value):
    """(Tier C, C4.0) What JSON carries to every driver as the lab wrote it.  NaN and Infinity are not JSON: the
    driver's JSON.parse / jsonDecode throws on the whole extra, and every hook of the step goes with it.  An
    integral float (`10.0`) reaches JavaScript as `10` but Dart as a double — two drivers asked two questions,
    and the lab believing it asked a float it never sent; write `10`.  An integer past 2^53 is not one a
    JavaScript number can hold."""
    if isinstance(value, float) and not math.isfinite(value):
        raise SystemExit("%s: %r is not JSON — the driver's parse would throw on the whole extra" % (where, value))
    if isinstance(value, float) and value.is_integer():
        raise SystemExit("%s: %r is an integral float — JavaScript cannot tell it from %d, Dart can; write %d"
                         % (where, value, value, value))
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2 ** 53:
        raise SystemExit("%s: %d is past 2^53 — a JavaScript number cannot hold it exactly" % (where, value))
    for v in value.values() if isinstance(value, dict) else value if isinstance(value, list) else ():
        _transportable(where, v)


def platforms():
    """Every platform that has a lab — the closed world an `api` scenario must account for."""
    root = os.path.join(LABS, "platforms")
    return sorted(d for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "lab.yaml")))


# (WO-191) Every lab's app ids, this one's and the background-fetch lab's (fetch-forge vendors this file): the two labs
# share the Lab devices, and their apps all link TSBackgroundFetch, so one left installed can deliver a fetch mid-step.
LAB_PACKAGE_PREFIXES = ("com.transistorsoft.labs.", "com.transistorsoft.fetchlab.")
# (WO-191, D1) And every other Transistor app: bg-geo's RN example woke on fetch's Lab2 at boot, under neither prefix.
VENDOR_PACKAGE_PREFIX = "com.transistorsoft."


def evictions(installed, package, os_name):
    """(WO-191) A first step's (evict, foreign) from the app ids INSTALLED on its device, never `package` (the app under
    test).  evict: every lab app, and on Android every other Transistor app too (D1: a force-stop is reversible).
    foreign: the non-lab Transistor apps found, which iOS leaves installed (uninstalling one is not the lab's to undo).
    From the device, not platforms(): the other lab's apps are not this repo's to build."""
    others = sorted(p for p in set(installed) if p != package and p.startswith(VENDOR_PACKAGE_PREFIX))
    foreign = [p for p in others if not p.startswith(LAB_PACKAGE_PREFIXES)]
    return (others if os_name == "android" else [p for p in others if p not in foreign]), foreign


def board_order():
    """(WO-143) The platforms in the order a board lists them and a sweep builds and deals them: each manifest's
    `column`.  The order was a list of four names in sweep.py and the same list again in lab_setup.py, so a fifth
    platform would have been swept, set up and checked by no one.  A platform is a manifest, never a name here."""
    columns = {}
    for name in platforms():
        column = (_load(os.path.join(LABS, "platforms", name, "lab.yaml")) or {}).get("column")
        if type(column) is not int or column < 1:
            raise SystemExit("%s: lab.yaml names no `column` — the platform's place on a board and in a sweep's "
                             "queue, a whole number from 1" % name)
        if column in columns:
            raise SystemExit("%s and %s: both name `column: %d` in their lab.yaml" % (columns[column], name, column))
        columns[column] = name
    return [columns[c] for c in sorted(columns)]


def hosts(name):
    """(WO-143) The OSes a platform has a lab host on.  A manifest's top level is its Android half and `ios:` its
    Simulator half (platform(), below); every platform had both until one arrived a half at a time, and each loop
    over the cells asked platform() for a view that was not there."""
    spec = _load(os.path.join(LABS, "platforms", name, "lab.yaml")) or {}
    return ["android"] + (["ios"] if spec.get("ios") else [])


def board_name(name):
    """(WO-143) The platform's name where a column is eight characters wide: the manifest's `short`, at most three
    letters so that `-ios` fits after it.  A sweep's board spelt the four bridges' in a chain of replace() calls,
    and a fifth platform's full name overran its column."""
    abbr = (_load(os.path.join(LABS, "platforms", name, "lab.yaml")) or {}).get("short")
    if not isinstance(abbr, str) or not re.fullmatch(r"[a-z]{2,3}", abbr):
        raise SystemExit("%s: lab.yaml names no `short` — its name on a board, two or three lower-case letters" % name)
    return abbr


def lab_cells(names, oses):
    """(WO-143) The platform-OS pairs a sweep runs, a setup builds and a doctor checks: each named platform on
    each asked OS it has a host on, in the order given."""
    return [(name, os_name) for name in names for os_name in ("android", "ios")
            if os_name in oses and os_name in hosts(name)]


# (Tier C, C2) `declared: both | android | ios` on an `api` hook asserts the TYPE of every State
# attribute of that scope — Chris's decision (3), 2026-09-24: the checks are generated from the
# vocabulary at load, because a hand-written list goes stale the day the types gain a key, and a key
# it forgot is never asked.  Each State LEAF whose state_scope is None (both) / 'android' / 'ios'
# becomes `$.<path>: {type: <its C0 wire>}` — `object` for a Record or an open key — and an explicit
# `returns` path on the same hook wins.  Types only, never values: a value is a config's business,
# and decision (1) already makes a missing key red.  It runs BEFORE the `returns` checks, so C0 holds
# every generated path to the State tree exactly as it would a written one: `declared: android`
# loads only where a dated not_applicable excuses iOS.
# (Tier C, C4.0) The same key on any member whose declared return — or, on an `events:` declaration, whose
# `emits:` — names an interface under the vocabulary's `types:`, or an array of one; the type is always
# the vocabulary's, never the scenario's, so a scenario cannot launder a payload by naming a lesser type.
# A `?` leaf is generated `optional: true` (absent passes; null is red unless the types say `| null`), a
# leaf inside a `?` object `under:` that object, which is generated too, and an array's element `.0` is a
# `?` object of its own (an empty array passes), its leaves `under:` it; `$` is typed object, or array.  A
# Record (`extras`) is `object`, its contents not walked; `types.omit` is left out.  A payload records no one-OS scope — its one-OS
# fields are all `?` — so only `both` expands one.  `Array<Object>` (getLocations, sync) and `void` are
# refused: they declare no path to generate (C4 decision 17).
# (Tier C, C5.1) And every leaf whose type is CLOSED — an enum, or a union of literals (ts_values) — also gets `in:` its
# values (S2): a State's desiredAccuracy, filter policies, kalmanProfile, logLevel, persistMode, notification.priority,
# http.method, activityType, trackingMode, locationAuthorizationRequest, locationsOrderDirection and timestampFormat; a
# payload's GeofenceEvent/GeofenceTrigger action, MotionActivity.type and LocationFilterEvent.reason.  Not
# activity.triggerActivities, 2026-09-28: `TriggerActivity[] | TriggerActivity` also admits the array a core stores
# joined (`in_vehicle,on_bicycle,…` on Android, `""` on iOS: WO-050), so ts_values leaves it open and it is typed only.
# Not ProviderChangeEvent.status or .accuracyAuthorization either: the types declare them `number`, not the enums.
DECLARED = ("both", "android", "ios")


def property_members(member):
    """(Tier C, C5.1) What a probe of a property may ask below `$`, {name: check}: an enum namespace's members (`typeof
    DesiredAccuracy`: `High` a number, -1), or the members of the interface `logger` / `deviceSettings` return
    (vocabulary `members:`: a method a function, `ORDER_ASC` its literal 1).  None for a member that is no property."""
    if not member.get("property"):
        return None
    returns = str(member.get("returns"))
    if returns.startswith("typeof "):
        enum = _enum(returns[len("typeof "):]) or {}
        return {k: {"type": enum["wire"], "equals": v} for k, v in (enum.get("members") or {}).items()}
    out = {}
    for name, m in ((vocabulary().get("members") or {}).get(returns) or {}).items():
        values = ts_values(str(m.get("returns"))) if m.get("property") else None
        out[name] = ({"type": "function"} if not m.get("property") else
                     dict({"type": "|".join(sorted(ts_wire(str(m["returns"]))))},
                          **({"equals": next(iter(values))} if values and len(values) == 1 else {})))
    return out


def _declare(at, hook, member, key="returns"):
    name = hook.get("api", hook.get("subscribe"))
    value = hook["declared"]
    # (Tier C, C5.1) S1: `declared: members` on a probe of a property generates `$` (object|function, however the
    # bridge implements it) and every member the types declare below it, each held to its type and value — never an
    # extra member (the legacy `DESIRED_ACCURACY_*` statics), which the types do not declare.
    if value == "members":
        members = property_members(member) if hook.get("probe") else None
        if not members:
            raise SystemExit("%s (%s): `declared: members` is a probe's, of a property that declares members (an enum "
                             "namespace, `logger`, `deviceSettings`)" % (at, name))
        returns = hook.get("returns") or {}
        if not isinstance(returns, dict):
            return                               # the `returns` check in _validate_hooks refuses it
        returns = dict(returns)
        returns.setdefault("$", {"type": "function|object"})
        for k, check in members.items():
            returns.setdefault("$." + k, dict(check))
        hook["returns"] = returns
        return
    # (Tier C, R; addendum A1) `declared: dart` on a probe of an enum namespace: the members the DOCS give a Dart
    # spelling (dart_members), each held to the value the TYPES give it.  The Flutter driver answers from the
    # generated rows, through that spelling; a member Dart lacks (`ignore: [dart]` in the db) is not asked.
    # (WO-143 stage 4) `declared: kotlin` and `declared: swift` the same, from native_members: the native drivers
    # answer from their generated rows.
    if value in SPELT:
        language = SPELT[value]
        table = spelt_members(value)
        spelt = ((table.get(name) or {}).get("members") or {}) if hook.get("probe") else {}
        members = property_members(member) or {}
        if not spelt or not str(member.get("returns")).startswith("typeof "):
            raise SystemExit("%s (%s): `declared: %s` is a probe's, of an enum namespace the docs give %s "
                             "members (labs/harness/%s_members.yaml: %s)"
                             % (at, name, value, language, "dart" if value == "dart" else "native",
                                ", ".join(sorted(table))))
        unknown = sorted(set(spelt) - set(members))
        if unknown:
            raise SystemExit("%s (%s): docforge/db gives a %s spelling to %s, which the types do not declare — a "
                             "stale db entry, or a stale vocabulary (seed_vocabulary.py, %s_members.py)"
                             % (at, name, language, ", ".join("%s.%s" % (name, k) for k in unknown),
                                "dart" if value == "dart" else "native"))
        returns = hook.get("returns") or {}
        if not isinstance(returns, dict):
            return                               # the `returns` check in _validate_hooks refuses it
        returns = dict(returns)
        returns.setdefault("$", {"type": "object"})          # the driver's own map of them
        for k in members:                                    # the types' order
            if k in spelt:
                returns.setdefault("$." + k, dict(members[k]))
        hook["returns"] = returns
        return
    if value not in DECLARED:
        raise SystemExit("%s (%s): `declared` is both, android or ios — or, on a property probe, `members`, `dart`, "
                         "`kotlin` or `swift` — not %r" % (at, name, value))
    if hook.get("probe"):
        raise SystemExit("%s (%s): `declared` on a probe — a probe reports typeOf(member), never the State "
                         "it would return; drop `probe` to invoke it" % (at, name))
    declared = str(member.get(key))
    element = _array_element(declared)
    box = payload_type(declared if element is None else element)
    if declared != "State" and box is None:
        raise SystemExit("%s (%s): `declared` generates the paths of State, or of an interface under the "
                         "vocabulary's `types:` (or an array of one), and the types declare %s %s `%s` — only "
                         "a member that returns or emits one of those can carry it"
                         % (at, name, name, key, member.get(key)))
    if box is not None and value != "both":
        raise SystemExit("%s (%s): `declared: %s` on a payload — a payload records no one-OS scope (every "
                         "one-OS field of one is `?`), so `declared: both` asserts it, `optional`" % (at, name, value))
    returns = hook.get("returns") or {}
    if not isinstance(returns, dict):
        return                                   # the `returns` check in _validate_hooks refuses it
    returns = dict(returns)                      # a YAML anchor shares the mapping with another hook
    if box is not None:
        omit = (vocabulary().get("types") or {}).get("omit") or {}

        def expand(fields, owner, prefix, under):
            for field, ts in fields.items():
                leaf, optional = field.rstrip("?"), field.endswith("?")
                if field == "[key: string]" or "%s.%s" % (owner, leaf) in omit:
                    continue
                inner = ts if isinstance(ts, dict) else payload_type(ts)
                item = _array_element(ts) if inner is None else None
                wire = {"object"} if isinstance(ts, dict) else ts_wire(ts)
                closed = ts_values(ts) if isinstance(ts, str) else None
                check = dict({"type": "object" if OPEN in wire else "|".join(sorted(wire))},
                             **({"in": _in_values(closed)} if closed else {}),     # (Tier C, C5.1) S2
                             **({"optional": True} if optional else {}), **({"under": under} if under else {}))
                if inner is None or optional:    # a required object is asserted by its leaves
                    returns.setdefault(prefix + leaf, check)
                if inner is not None:
                    owner_ = next((p for p in _alternatives(ts) if p not in ("null", "undefined")), None) \
                        if isinstance(ts, str) else None
                    expand(inner, owner_, prefix + leaf + ".", prefix + leaf if optional else under)
                elif payload_type(item or "") is not None:
                    returns.setdefault(prefix + leaf + ".0", {"type": "object", "optional": True,
                                                              "under": prefix + leaf})
                    expand(payload_type(item), item, prefix + leaf + ".0.", prefix + leaf + ".0")
        # The root is typed too: `.0` and `.length` of an object read missing, parent=1, as of an empty array, so
        # without `$: {type: array}` a bridge handing `{geofences: […]}` for a `Geofence[]` would pass them all.
        returns.setdefault("$", {"type": "object" if element is None else "array"})
        if element is None:
            expand(box, declared, "$.", None)
        else:
            returns.setdefault("$.0", {"type": "object", "optional": True})
            expand(box, element, "$.0.", "$.0")
        hook["returns"] = returns
        return
    leaves = []
    def walk(node, prefix):
        for k, v in node.items():
            walk(v, prefix + k + ".") if isinstance(v, dict) else leaves.append(prefix + k)
    walk(state_tree(), "$.")
    for path in leaves:
        if state_scope(path) == (None if value == "both" else value):
            wire = state_path_wire(path)
            if wire is None:
                raise SystemExit("%s (%s): `declared` found no type for State's %s in "
                                 "labs/harness/vocabulary.yaml — re-seed it (seed_vocabulary.py)"
                                 % (at, name, path))
            leaf = _state_leaf(path)
            closed = ts_values(leaf) if isinstance(leaf, str) and wire is not OPEN else None
            returns.setdefault(path, dict({"type": "object" if wire is OPEN else "|".join(sorted(wire))},
                                          **({"in": _in_values(closed)} if closed else {})))     # (Tier C, C5.1) S2
    hook["returns"] = returns


def _in_values(values):
    """(Tier C, C5.1) A closed type's values as a generated `in:` lists them: numbers, then strings, each in order."""
    return sorted(values, key=lambda v: (isinstance(v, str), v))


def _check_generate(at, hook):
    """(WO-107) `generate: {geofences: N, vertices: V, seed: S}` asks the driver for N polygons of V vertices, built
    from seed S, as addGeofences' argument.  Only there, never beside `args`, and never on a probe."""
    if hook["api"] != "addGeofences" or "args" in hook or hook.get("probe"):
        raise SystemExit("%s (%s): `generate:` builds addGeofences' argument on the device — only on an addGeofences "
                         "call, and never beside `args`" % (at, hook["api"]))
    spec = hook["generate"]
    if not isinstance(spec, dict) or set(spec) != set(GENERATE_BOUNDS):
        raise SystemExit("%s (%s): `generate:` is {geofences: N, vertices: V, seed: S}, not %r" % (at, hook["api"], spec))
    for key, (low, high) in GENERATE_BOUNDS.items():
        value = spec[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise SystemExit("%s (%s): `generate.%s` is a whole number from %d to %d, not %r"
                             % (at, hook["api"], key, low, high, value))


def _check_alongside(at, hook):
    """(WO-107) `alongside: <member>` issues that call right after this one, without awaiting this one, and the driver
    reports how long after issuing THIS call it settled (`alongsideMs`): how long a bridge holds its other calls while
    this one runs.  The hook chain cannot say it — every call there is awaited before the next is issued.  The member
    is one the driver can call with no arguments, on a call: a probe invokes nothing."""
    other = api_member(hook["alongside"]) if isinstance(hook["alongside"], str) else None
    if (hook.get("probe") or other is None or other.get("property")
            or any(not param(a)[1] for a in (other.get("args") or []))):
        raise SystemExit("%s (%s): `alongside:` names an SDK method the driver can call with no arguments, on a call "
                         "— not %r" % (at, hook["api"], hook["alongside"]))


def api_member(name):
    """(Tier C, R3) The vocabulary's entry for an `api:` name: a member of the API itself (`getState`) or, dotted, a
    member of the interface one of its properties returns (`logger.getLog`: `logger` returns Logger, and `members:`
    lists Logger's getLog).  None where the types declare no such member: one level, as the types have it."""
    api = vocabulary().get("api") or {}
    head, dot, tail = str(name).partition(".")
    if not dot:
        return api.get(name)
    owner = api.get(head) or {}
    if not owner.get("property"):
        return None
    return ((vocabulary().get("members") or {}).get(str(owner.get("returns"))) or {}).get(tail)


def _validate_hooks(where, step, excused=()):
    step["hooks"] = step["hooks"] or []          # `hooks:` with nothing after it
    for i, hook in enumerate(step["hooks"]):
        at = "%s hook %d" % (where, i)
        if not isinstance(hook, dict) or ("api" in hook) == ("call" in hook):
            raise SystemExit("%s: a hook carries exactly one of `call` or `api`" % at)
        try:
            # It crosses the wire as JSON and is recorded in run.json as JSON.  YAML reads a bare
            # 2024-01-15 as a date, and json.dump would die on it AFTER the device run.
            # (Tier C, C4.0) allow_nan=False: NaN and Infinity are not JSON either, and the driver's parse
            # would throw on the whole hooksB64.
            json.dumps(hook, allow_nan=False)
        except TypeError as e:
            raise SystemExit("%s: not JSON (%s) — quote dates and timestamps" % (at, e))
        except ValueError as e:
            raise SystemExit("%s: not JSON (%s) — NaN and Infinity would lose the step's every hook" % (at, e))
        if "call" in hook:
            _reject_unknown(at, hook, HOOK_KEYS_CALL)
            continue
        if "at" in hook:
            raise SystemExit("%s (%s): %s" % (at, hook["api"], AT_RETIRED))
        _reject_unknown(at, hook, HOOK_KEYS_API)
        member = api_member(hook["api"])
        if member is None:
            raise SystemExit("%s: %r is not in the SDK vocabulary (labs/harness/vocabulary.yaml) — a "
                             "name the types do not declare would read as a bridge defect" % (at, hook["api"]))
        # (Tier C, R3) A dotted name is for CALLING a member of `logger` or `deviceSettings`.  Its members are read
        # by a probe of the property itself, which `declared: members` holds one by one (api-properties-typescript).
        if "." in str(hook["api"]) and hook.get("probe"):
            owner = str(hook["api"]).partition(".")
            raise SystemExit("%s (%s): a dotted name is called, never probed — probe `%s` and assert `$.%s`"
                             % (at, hook["api"], owner[0], owner[2]))
        if "args" in hook:
            _transportable("%s (%s) args" % (at, hook["api"]), hook["args"])
        if "generate" in hook:
            _check_generate(at, hook)
        if "alongside" in hook:
            _check_alongside(at, hook)
        if "declared" in hook:
            _declare(at, hook, member)           # (Tier C, C2) before every `returns` check below
        why = None                               # (Tier C, C4.0) how the call departs from the types
        if not hook.get("probe"):
            if member.get("property"):
                raise SystemExit("%s: %s is a property — probe it, it cannot be invoked" % (at, hook["api"]))
            declared = member.get("args") or []
            if "args" in hook and not declared:
                raise SystemExit("%s: %s takes no arguments" % (at, hook["api"]))
            # The hook shape carries ONE argument (labapi.js `member.call(BG, hook.args)`, the
            # Dart port `Function.apply(member, [args])`): a second required parameter is
            # unreachable, and the call would be the lab's own wrong call scored against the SDK.
            required = [param(a)[0] for a in declared if not param(a)[1]]
            if len(required) > 1:
                raise SystemExit("%s: %s takes %d required parameters (%s) and a hook carries one — "
                                 "probe it instead" % (at, hook["api"], len(required), ", ".join(required)))
            # (Tier C, C4.0) The argument is held to its parameter's declared type (_conform: keys, wire types,
            # values; a Config through the config tree, as a `config:` block is), and a required one must be
            # there.  Either departure is a form the types forbid, which the lab asks only on purpose, dated —
            # except a callback: a hook passes JSON, never a function, and `invalid:` does not excuse it (an `on*`
            # called without one registers a listener that throws on every later delivery, for the rest of the
            # process).  `events:` subscribes to it.
            first = param(declared[0]) if declared else None
            if first and CALLBACK_ARG.search(first[0]) and ("args" in hook or not first[1]):
                raise SystemExit("%s: %s takes a callback first (%s), and a hook passes JSON, never a function — "
                                 "subscribe with `events:`" % (at, hook["api"], first[0]))
            # (WO-107) A `generate:` hook's argument is built on the device, a Geofence[] by construction.
            if "args" not in hook and "generate" not in hook and required:
                why = "%s (%s): %s requires %s" % (at, hook["api"], hook["api"], required[0])
            elif "args" in hook:
                why = _conform(hook["args"], param(declared[0])[2], "%s (%s) args" % (at, hook["api"]))
        # (Tier C, C4.0) `invalid: "YYYY-MM-DD <why>"` is the only way past both, and refuses on a call the types
        # allow — every required argument there, each of its type — so the marker cannot outlive its reason.
        # A misspelt config key is never excused: _validate_config refuses it outright.
        if "invalid" in hook:
            if not DATED.match(str(hook["invalid"])):
                raise SystemExit("%s (%s): `invalid:` is \"YYYY-MM-DD <why>\" — a dated, diffable reason, not %r"
                                 % (at, hook["api"], hook["invalid"]))
            if why is None:
                raise SystemExit("%s (%s): `invalid:` on a call the types allow — every required argument there, "
                                 "each of its declared type — so the marker would outlive its reason; drop it"
                                 % (at, hook["api"]))
        elif why:
            raise SystemExit("%s — a form the types forbid; ask it on purpose with `invalid: \"YYYY-MM-DD <why>\"`"
                             % why)
        # (Tier C, C4.0) `settles_only: "YYYY-MM-DD <why>"`: the lab cannot see this call's ARGUMENT take effect
        # (README's argument-forwarding class), so the hook passes even where a bridge drops it.  It still
        # asserts its return; only a call that passes an argument can carry it.
        absent_on = hook.get("absent_on")
        if absent_on is not None and not (isinstance(absent_on, dict) and absent_on
                                          and all(p in platforms() and DATED.match(str(r)) for p, r in absent_on.items())):
            raise SystemExit("%s (%s): `absent_on:` is {<platform>: \"YYYY-MM-DD <why>\"}, each a platform of the lab "
                             "— not %r" % (at, hook["api"], absent_on))
        if "settles_only" in hook and not (DATED.match(str(hook["settles_only"])) and "args" in hook):
            raise SystemExit("%s (%s): `settles_only:` is \"YYYY-MM-DD <why>\", on a call that passes `args` — "
                             "it says what the lab cannot see that argument do; not %r"
                             % (at, hook["api"], hook["settles_only"]))
        # (WO-027) `at_s:` defers a call to a slot, that many seconds after the first batch completes.
        # The batch is awaited end to end with no pause anywhere in it, so without this a scenario
        # cannot express "call, wait, call again" — and on iOS that matters: measured, a stop() issued
        # 13 ms after start() cancelled the location request start()'s motionchange was waiting on,
        # and the event never arrived.
        if "at_s" in hook:
            _at_gate(at, hook["api"], hook["at_s"], step)
        # (Tier C, C4.0) `outcome:` asserts how the call settles: `resolved`, `rejected`, or — where the types
        # are silent — `settled YYYY-MM-DD <why>`, which passes either and records the root, never judging it.
        # It makes `returns` optional; a `returns` it keeps must agree with it, and `settled` keeps none.
        outcome = str(hook.get("outcome", "")).split(" ")[0]
        if "outcome" in hook and (not OUTCOME.match(str(hook["outcome"])) or hook.get("probe")):
            raise SystemExit("%s (%s): `outcome` is resolved, rejected, threw or \"settled YYYY-MM-DD <why>\", on a "
                             "call — a probe invokes nothing; not %r" % (at, hook["api"], hook["outcome"]))
        returns = hook.get("returns", {} if outcome else None)
        if not isinstance(returns, dict) or not returns and ("returns" in hook or not outcome):
            raise SystemExit("%s (%s): an `api` hook must declare a non-empty `returns` — or an `outcome:`, and "
                             "then `returns` only to assert more" % (at, hook["api"]))
        if returns and outcome == "settled":
            raise SystemExit("%s (%s): `outcome: settled` records the value and judges none — drop `returns`"
                             % (at, hook["api"]))
        # (Tier C, R5) `$err` is the error side, a rejection's value or a thrown one.
        sides = {str(path).startswith("$err") for path in returns} | \
            ({outcome in ("rejected", "threw")} if outcome else set())
        if len(sides) > 1:
            raise SystemExit("%s (%s): `returns` mixes $ and $err paths, or asserts the side `outcome` rules out — a "
                             "call either resolves or rejects" % (at, hook["api"]))
        for path, checks in returns.items():
            if not PATH.match(str(path)):
                raise SystemExit("%s (%s): bad path %r — write $, $.a.0.b or $err.message"
                                 % (at, hook["api"], path))
            if not isinstance(checks, dict) or not checks:
                raise SystemExit("%s (%s) %s: no comparator" % (at, hook["api"], path))
            _reject_unknown("%s (%s) %s" % (at, hook["api"], path), checks, COMPARATORS)
            if checks.get("absent") and len(checks) > 1:
                raise SystemExit("%s (%s) %s: `absent: true` stands alone — nothing else can be "
                                 "evaluated against a key that is not there" % (at, hook["api"], path))
            _in_list("%s (%s) %s" % (at, hook["api"], path), checks)
            # (Tier C, C4.2) `record: true` stands alone and is true: the path is still held to its root below
            # (a typo refuses), but no type or value is asked of it.
            if RECORD in checks and (checks[RECORD] is not True or len(checks) > 1):
                raise SystemExit("%s (%s) %s: `record` is true, and stands alone — it reads the path and judges "
                                 "nothing, so a comparator beside it would lose its verdict and a qualifier has "
                                 "nothing to qualify; assert the path, or record it" % (at, hook["api"], path))
            try:
                re.compile(str(checks.get("matches", "")))
            except re.error as e:
                raise SystemExit("%s (%s) %s: `matches` is not a regex (%s)" % (at, hook["api"], path, e))
            kinds = str(checks.get("type", "")).split("|") if "type" in checks else []
            if set(kinds) - WIRE_TYPES:
                raise SystemExit("%s (%s) %s: unknown type(s) %s — known: %s"
                                 % (at, hook["api"], path, ", ".join(sorted(set(kinds) - WIRE_TYPES)),
                                    ", ".join(sorted(WIRE_TYPES))))
            # One character should not be able to launder a divergence between bridges.
            if len(kinds) > 2:
                raise SystemExit("%s (%s) %s: a `type` union takes at most two alternatives"
                                 % (at, hook["api"], path))
            _refuse_missing("%s (%s) %s" % (at, hook["api"], path), kinds)
            _qualifiers("%s (%s)" % (at, hook["api"]), path, checks, returns)
            # (Tier C, C4.0) Every path below the root is held to the type it walks: State's (C0), a payload's
            # under `types:`, a scalar's, which takes none (surface H5) — and every `$err` path to the declared
            # REJECTION type (`rejects:`), its root included, as `$` is held to the return.  Where the types
            # declare no rejection there is no `$err` to assert, the root included (C4 decision 4: React Native
            # rejects an Error, Flutter a map, Capacitor and Cordova a string); `outcome: rejected` says that it
            # rejected.  `Array<Object>` (getLocations, sync) declares no record to hold a path to (C4 decision 17).
            err = str(path).startswith("$err")
            root = str(member.get("rejects" if err else "returns") or "")
            if err and not root:
                raise SystemExit("%s (%s) %s: the types declare no rejection type for %s, so no `$err` to assert "
                                 "(C4 decision 4: each bridge rejects its own shape) — write `outcome: rejected`, or "
                                 "`outcome: \"settled YYYY-MM-DD <why>\"` where the types are silent"
                                 % (at, hook["api"], path, hook["api"]))
            if str(path) != "$" and root not in ("", "Array<Object>") and not hook.get("probe"):
                _check_state_path("%s (%s)" % (at, hook["api"]), str(path), kinds, checks, excused,
                                  None if root == "State" else root)
            # (Tier C, C5.1) A probe reports what the member holds, at a path below it: only a member the types
            # declare (property_members), one level down, and only what the types say it is.  A method's probe has
            # none, and an undeclared path would run on the device and read `missing` as a bridge defect.
            if str(path) != "$" and hook.get("probe"):
                allowed = property_members(member) or {}
                want = allowed.get(str(path)[2:])
                if want is None:
                    raise SystemExit("%s (%s) %s: a probe asks only for a member the types declare below `%s`%s"
                                     % (at, hook["api"], path, hook["api"], " (%s)" % ", ".join(sorted(allowed))
                                        if allowed else " — and it declares none"))
                asked = set(kinds) | _implied(checks)
                # (C5.1 review) Its type and its own value, or `record`: never `optional`/`absent` (every member is
                # required, so either would pass the very absence this detects), nor another value by any comparator.
                others = sorted(set(checks) - {"type", "equals", RECORD})
                if others or asked - set(want["type"].split("|")) or "equals" in checks and checks["equals"] != \
                        want.get("equals", _ABSENT):
                    raise SystemExit("%s (%s) %s: the types declare it %s — assert its `type` and that value only%s"
                                     % (at, hook["api"], path, want, " (not `%s`)" % others[0] if others else
                                        "; asserting otherwise would launder a divergence"))
            # The root type must be one the DECLARED return can produce: `boolean` on a method
            # the types say is void is the divergence itself, written into the scenario — and
            # `equals: true` with no `type:` is the same divergence, because _check is
            # type-strict and `equals: true` can only ever pass a boolean.
            if str(path) == "$" and (kinds or _implied(checks)):
                if hook.get("probe"):
                    # A probe reports typeOf(member): a method is a function; a property is
                    # however the bridge implements it — an object literal, an instance, or a
                    # class of statics (typeof a class is `function`).  Never undefined/missing:
                    # an absent member is outcome=absent, red before any comparator.
                    allowed = {"function", "object"} if member.get("property") else {"function"}
                else:
                    allowed = wire_of(member)
                asked = set(kinds) | _implied(checks)
                if asked - allowed:
                    raise SystemExit("%s (%s) $: the types declare `%s` (wire %s) — asserting %s here "
                                     "would launder a divergence; write a work order instead"
                                     % (at, hook["api"], member.get("returns"),
                                        "|".join(sorted(allowed)), "|".join(sorted(asked - allowed))))
        # (Tier C, C3) `previous` becomes the id it names, here, so run.json records the reference a
        # capture was scored against.  Into a new hook and new mappings: a YAML alias (`*h`) is one
        # mapping at two places, and each resolves its own `previous`.
        if any(set(checks) & set(REFS) for checks in returns.values()):
            step["hooks"][i] = hook = dict(hook, returns={
                path: dict(checks, **{op: _ref(at, step["hooks"], i, str(path), op, checks[op])
                                      for op in REFS if op in checks})
                for path, checks in returns.items()})
        if "sets" in hook:
            node, sets = hook.get("args"), hook["sets"]
            for seg in str(sets).split("."):
                node = node.get(seg, _ABSENT) if isinstance(node, dict) else _ABSENT
            if not isinstance(sets, str) or sets not in config_leaves() or node is _ABSENT:
                raise SystemExit("%s (%s): `sets: %r` names the Config leaf this hook's args set, dotted "
                                 "(geolocation.filter.policy) — host-only, recorded in run.json"
                                 % (at, hook["api"], sets))
        # (Tier C, C4.4) `kind:` says which invalid-value kind the setConfig asks of the leaf its `sets:` names.
        if "kind" in hook and (hook["kind"] not in IV_KINDS or "sets" not in hook or hook["api"] != "setConfig"):
            raise SystemExit("%s (%s): `kind: %r` is one of %s, on a setConfig that names its leaf with `sets:` — "
                             "host-only, as invalid_values: generates it" % (at, hook["api"], hook["kind"],
                                                                             ", ".join(IV_KINDS)))


_ABSENT = object()


def _ref(at, hooks, i, path, op, want):
    """(Tier C, C3) The id a `same:`/`differs:` on hooks[i] at `path` compares with — `previous` resolved
    to the nearest usable hook — or a refusal.  The reference has to have RUN first and to have answered
    the same question: an earlier api hook of the step, not a probe (a probe reports typeOf(member)),
    not in a later `at_s` slot (it would run after), reporting the same path.  An EARLIER slot has
    finished: every driver queues each slot's batch on one chain behind the one before (onSlot)."""
    where = "%s (%s) %s: `%s: %s`" % (at, hooks[i]["api"], path, op, want)
    if hooks[i].get("probe"):
        raise SystemExit("%s on a probe — a probe reports typeOf(member), never a reading to compare" % where)
    slot = hooks[i].get("at_s", 0)

    def why_not(j):
        h = hooks[j]
        if "api" not in h:
            return "a legacy `call` hook reports nothing"
        if h.get("probe"):
            return "a probe reports typeOf(member), not a reading"
        if h.get("at_s", 0) > slot:
            return "it runs in a later `at_s` slot, after this hook"
        if path not in (h.get("returns") or {}):
            return "it does not report %s" % path
        return None
    if want == "previous":
        j = next((j for j in range(i - 1, -1, -1) if why_not(j) is None), None)
        if j is None:
            raise SystemExit("%s — no earlier api hook of this step reports %s" % (where, path))
        return "%s#%d" % (hooks[j]["api"], j)
    m = REF_ID.match(want) if isinstance(want, str) else None
    if not m:
        raise SystemExit("%s — a reference is `previous` or `<api>#<n>`" % where)
    j = int(m.group(2))
    if j >= i:
        raise SystemExit("%s — %s is not an EARLIER hook of this step (ids count every hook from 0)"
                         % (where, want))
    if hooks[j].get("api") != m.group(1):
        raise SystemExit("%s — hook %d of this step is %s, not %s"
                         % (where, j, hooks[j].get("api") or "a legacy `call`", m.group(1)))
    if why_not(j):
        raise SystemExit("%s — %s" % (where, why_not(j)))
    return want


def _at_gate(at, member, at_s, step):
    """(WO-027, Tier B) `at_s` is seconds after the FIRST BATCH completes — the driver logs that
    moment as `app=anchor` and starts one timer per distinct `at_s` from it — and the step has to
    watch long enough to reach it: a slot the step never reaches reads as `never-called` /
    `never-armed`, which is a true statement about a lab that stopped watching and a false one about
    the SDK.  Launch to the anchor is budgeted at ANCHOR_BUDGET_S, and a liveness tick must follow
    the slot.  Legacy `call` hooks keep their tick-counted `at:` and are not gated here."""
    if (not isinstance(at_s, (int, float)) or isinstance(at_s, bool) or not math.isfinite(at_s)
            or at_s <= 0):
        raise SystemExit("%s (%s): `at_s` is a number of seconds after the first batch completes, "
                         "more than 0 — not %r" % (at, member, at_s))
    window = int(step.get("settle", 0)) + int(step.get("after", 0))
    need = ANCHOR_BUDGET_S + at_s + TICK_SECONDS
    if window < need:
        raise SystemExit("%s (%s): `at_s: %g` fires %g s after the first batch completes (budgeted "
                         "%g s after launch), but this step only watches for %d s (settle %s + after "
                         "%s) — raise them to at least %d s or lower `at_s`"
                         % (at, member, at_s, at_s, ANCHOR_BUDGET_S + at_s, window,
                            step.get("settle"), step.get("after"), math.ceil(need)))


# (WO-027) What the HOST can do to a window the app cannot reach.  `allow`/`deny` answer the OS's
# own permission dialog; `rationale` the SDK's own pre-prompt on Android; `always` the upgrade —
# a radio on Android's app-permission page, an alert button on iOS; `back` leaves that page, which
# is the only thing that delivers the background grant.
# (WO-027, permissions) `keep` declines the iOS upgrade alert ("Keep Only While Using"); `cancel`
# answers the SDK's OWN window on either OS — the rationale on Android, the Settings alert on iOS
# (TSGeolocationConfig.m:32-33).
# (Tier C, C8.0) `turn-on` / `no-thanks` answer Google's Location Accuracy consent dialog, which the SDK's settings
# check raises over the app while Location Accuracy is off; `approximate` declines Android's precise-location upgrade
# ("Keep approximate location"), whose buttons no `deny` label matches.  All three Android-only.
DIALOG_ACTIONS = ("allow", "deny", "rationale", "always", "back", "keep", "cancel", "turn-on", "no-thanks",
                  "approximate")
# Which actions iOS has no window for, and why.
ANDROID_ONLY_ACTIONS = {
    "rationale": "iOS has no SDK rationale dialog",
    "back": "iOS has no Back key",
    "turn-on": "Location Accuracy is a Google Play services setting, and iOS has none",
    "no-thanks": "Location Accuracy is a Google Play services setting, and iOS has none",
    "approximate": "it answers Android's precise-location upgrade dialog; iOS's temporary full accuracy is an alert "
                   "the lab cannot raise on the Simulator"}


def _dialog_plan(where, value):
    """`dialog:` normalised to {android: [...], ios: [...]} — an ORDERED list of host actions.

    (WO-027) One action was enough while every scenario answered a single dialog.  An `Always`
    grant is a sequence — on Android the OS dialog, then the SDK's rationale, then the radio on
    the page the OS opens, then Back — and a host that answers only the first leaves the SDK
    waiting on a window nobody will touch.  The shapes: `allow` (one action, both OSes, what the
    committed scenarios say), `[allow, always]` (a sequence on both), or a per-OS map, because the
    number of windows is a platform fact and not something a scenario should average over."""
    if value is None:
        return None
    if isinstance(value, dict):
        unknown = sorted(set(value) - {"android", "ios"})
        if unknown:
            raise SystemExit("%s: `dialog` names %s, which is not an OS — the map form is "
                             "{android: [...], ios: [...]}" % (where, ", ".join(unknown)))
        plan = {os_name: list(value.get(os_name) or []) for os_name in ("android", "ios")}
    else:
        actions = list(value) if isinstance(value, list) else [value]
        plan = {"android": list(actions), "ios": list(actions)}
    for os_name, actions in plan.items():
        for action in actions:
            if action not in DIALOG_ACTIONS:
                raise SystemExit("%s: `dialog` action %r is not one of %s"
                                 % (where, action, ", ".join(DIALOG_ACTIONS)))
        if os_name == "ios":
            for android_only, why in ANDROID_ONLY_ACTIONS.items():
                if android_only in actions:
                    raise SystemExit("%s: `dialog: %s` is Android-only — %s; give iOS its own list: "
                                     "{android: [...], ios: [...]}" % (where, android_only, why))
        # (WO-027, permissions) Refused, not mapped: Android's nearest button is a hazard.
        if os_name == "android" and "keep" in actions:
            raise SystemExit("%s: `dialog: keep` is iOS-only (\"Keep Only While Using\") — on Android, "
                             "tapping \"Allow only while using the app\" while ACCESS_BACKGROUND_LOCATION "
                             "is held revokes background and kills the running app (measured "
                             "2026-09-25); decline Android's upgrade with `cancel` on the SDK's "
                             "rationale, and give iOS its own list: {android: [...], ios: [...]}" % where)
    return plan


def _validate_events(where, step, excused=()):
    """(WO-027) Every `events:` declaration names an `on*` member the types declare, and asserts
    only what that member's callback can deliver.  The same closed world as `api` hooks, for the
    same reason: a name the types do not declare would run on the device and read as a bridge
    defect, and a root `type:` the payload cannot produce would launder a divergence into the
    scenario instead of into a work order."""
    step["events"] = step["events"] or []
    for i, decl in enumerate(step["events"]):
        at = "%s events %d" % (where, i)
        # YAML 1.1 reads a bare `on` as the boolean True, so `on: onLocation` would arrive as
        # {True: "onLocation"} and the member name would vanish.  The key is `subscribe:`; a
        # scenario that writes `on:` out of habit is told exactly that.
        if isinstance(decl, dict) and True in decl:
            raise SystemExit("%s: `on:` is a YAML boolean (1.1) — the key is `subscribe: %s`"
                             % (at, decl[True]))
        if not isinstance(decl, dict) or "subscribe" not in decl:
            raise SystemExit("%s: an `events:` entry names the member as `subscribe: onLocation`" % at)
        if "at" in decl:
            raise SystemExit("%s (%s): %s" % (at, decl["subscribe"], AT_RETIRED))
        _reject_unknown(at, decl, EVENT_KEYS)
        try:
            json.dumps(decl, allow_nan=False)         # (Tier C, C4.0) NaN would lose the whole eventsB64
        except (TypeError, ValueError) as e:
            raise SystemExit("%s: not JSON (%s)" % (at, e))
        member = (vocabulary().get("api") or {}).get(decl["subscribe"])
        if member is None:
            raise SystemExit("%s: %r is not in the SDK vocabulary (labs/harness/vocabulary.yaml)"
                             % (at, decl["subscribe"]))
        if not member.get("emits"):
            raise SystemExit("%s: %s carries no `emits:` in the vocabulary — a scenario can only "
                             "subscribe to a member that hands a callback a payload, and `emits:` "
                             "is that payload's type" % (at, decl["subscribe"]))
        # A member may take arguments BEFORE its callback — watchPosition takes its options that
        # way.  The declaration supplies them, the way an `api` hook supplies `args`; a member
        # that takes only a callback has nothing to supply.
        needs = [param(a) for a in member.get("args") or []
                 if not param(a)[1] and not CALLBACK_ARG.search(param(a)[0])]
        if needs and "args" not in decl:
            raise SystemExit("%s (%s): %s takes %s before its callback — pass it as `args:`"
                             % (at, decl["subscribe"], decl["subscribe"], ", ".join(n for n, _, _ in needs)))
        if "args" in decl and not needs:
            raise SystemExit("%s (%s): %s takes only a callback, so it has no `args`"
                             % (at, decl["subscribe"], decl["subscribe"]))
        if len(needs) > 1:
            raise SystemExit("%s (%s): %s takes %d values before its callback (%s) and a "
                             "declaration carries one" % (at, decl["subscribe"], decl["subscribe"],
                                                          len(needs), ", ".join(n for n, _, _ in needs)))
        # (Tier C, C4.0) Held to its parameter's type as a hook's argument is (watchPosition's options), with no
        # `invalid:`: a subscription is how the lab observes, never the form under test.
        if "args" in decl:
            _transportable("%s (%s) args" % (at, decl["subscribe"]), decl["args"])
            why = _conform(decl["args"], needs[0][2], "%s (%s) args" % (at, decl["subscribe"]))
            if why:
                raise SystemExit("%s — the types forbid it" % why)
        if "at_s" in decl:
            _at_gate(at, decl["subscribe"], decl["at_s"], step)
        whole = lambda v, least: isinstance(v, int) and not isinstance(v, bool) and v >= least
        # (Tier C, C5.0) `max: N` bounds the deliveries, and `min: 0` is allowed only beside it: without one, `min: 0`
        # would assert nothing at all.  `max: 0` is the absence itself, so `min: 0` must be written with it.
        if "max" in decl and not whole(decl["max"], 0):
            raise SystemExit("%s (%s): `max` is a whole number of deliveries, 0 or more" % (at, decl["subscribe"]))
        if "min" in decl and not whole(decl["min"], 0 if "max" in decl else 1):
            raise SystemExit("%s: `min` is a whole number of deliveries, at least 1 — 0 only beside a `max`" % at)
        if "max" in decl and decl.get("min", 1) > decl["max"]:
            raise SystemExit("%s (%s): `min` (%s) is above `max: %d` — write `min: 0` beside `max: 0`"
                             % (at, decl["subscribe"], decl.get("min", "1 by default"), decl["max"]))
        # (Tier C, C5.0) `arm_only: "YYYY-MM-DD <why>"` asserts the subscription and nothing it delivers: a member no
        # lab cause reaches (onActivityChange, onAuthorization: decision 9).  So none of the delivery keys, and a
        # `returns` of recorded paths only — its delivery count goes to the `recorded` row (decision 25).
        if "arm_only" in decl:
            if not DATED.match(str(decl["arm_only"])):
                raise SystemExit("%s (%s): `arm_only:` is \"YYYY-MM-DD <why>\" — a dated reason, not %r"
                                 % (at, decl["subscribe"], decl["arm_only"]))
            both = sorted({"min", "max", "none_after", "min_after"} & set(decl))
            if both:
                raise SystemExit("%s (%s): `arm_only` asserts no delivery, and `%s` asserts deliveries"
                                 % (at, decl["subscribe"], both[0]))
        # (Tier C, R4) `delivery: N`: `returns` is read from the Nth delivery, so it needs one, and N of them: `min`
        # counts from N.  Nothing an `arm_only` or a `max` below N could ever deliver.
        if "delivery" in decl:
            if not whole(decl["delivery"], 1):
                raise SystemExit("%s (%s): `delivery` is the delivery `returns` is read from, a whole number from 1"
                                 % (at, decl["subscribe"]))
            if "arm_only" in decl or decl.get("max", decl["delivery"]) < decl["delivery"]:
                raise SystemExit("%s (%s): `delivery: %d` reads a delivery that `%s` says never comes"
                                 % (at, decl["subscribe"], decl["delivery"],
                                    "arm_only" if "arm_only" in decl else "max: %d" % decl["max"]))
        # (Tier C, R6) `errors: true`: the member's failure callback is passed as well.  Only a member the types give
        # one: a second callback (onLocation's `onError?`, watchPosition's `errorCallback?`).
        if "errors" in decl:
            callbacks = [a for a in member.get("args") or [] if "=>" in str(param(a)[2])]      # by type: a function
            if decl["errors"] is not True or len(callbacks) < 2:
                raise SystemExit("%s (%s): `errors: true` passes the member's failure callback — `true`, on a member "
                                 "the types give one (onLocation, watchPosition)" % (at, decl["subscribe"]))
        # (Tier C, C5.0) `remove_at_s: N`: in slot N, after that slot's api hooks, the driver removes the Subscription
        # this declaration armed and logs `event-removed`.  After its arm, and inside the window as `at_s` is.
        if "remove_at_s" in decl:
            _at_gate(at, decl["subscribe"], decl["remove_at_s"], step)
            if decl["remove_at_s"] <= decl.get("at_s", 0):
                raise SystemExit("%s (%s): `remove_at_s: %g` is not after its arm (`at_s: %g`) — the slot arms first"
                                 % (at, decl["subscribe"], decl["remove_at_s"], decl.get("at_s", 0)))
        # (Tier C, C5.0) `none_after: <anchor>` — no delivery after it; `min_after: {ref: <anchor>, n}` — at least n.
        if "none_after" in decl:
            _event_anchor(at, decl, i, step, decl["none_after"], "none_after")
        if "min_after" in decl:
            after = decl["min_after"]
            if not (isinstance(after, dict) and set(after) == {"ref", "n"} and whole(after["n"], 1)):
                raise SystemExit("%s (%s): `min_after` is {ref: <anchor>, n: <at least 1>}, not %r"
                                 % (at, decl["subscribe"], after))
            if after["ref"] == "remove":
                raise SystemExit("%s (%s): `min_after: {ref: remove}` would count deliveries after its own removal — "
                                 "name the other declaration, `<member>@<i>`" % (at, decl["subscribe"]))
            _event_anchor(at, decl, i, step, after["ref"], "min_after.ref")
        if "declared" in decl:
            _declare(at, decl, member, "emits")         # (Tier C, C4.0) before every `returns` check below
        returns = decl.get("returns")
        # (Tier C, C5.0) `max: 0` passes only where nothing was delivered, so there is nothing to assert of a delivery;
        # `arm_only` asserts none.
        if decl.get("max") == 0 and "returns" in decl:
            raise SystemExit("%s (%s): `max: 0` passes only when nothing is delivered — drop `returns`"
                             % (at, decl["subscribe"]))
        if decl.get("max") == 0 or "arm_only" in decl and "returns" not in decl:
            continue
        if not isinstance(returns, dict) or not returns:
            raise SystemExit("%s (%s): an `events:` entry must declare a non-empty `returns`"
                             % (at, decl["subscribe"]))
        # (Tier C, R6) An error path is the failure callback's value: only where `errors: true` passes one, and
        # recorded, never judged (decision 21: events-errors measures).  It cannot be the declaration's only path:
        # what a delivery carries is still asserted.
        errs = [str(path) for path in returns if str(path).startswith("$err")]
        if errs and (decl.get("errors") is not True or len(errs) == len(returns)
                     or any((returns[path] or {}) != {RECORD: True} for path in returns if str(path) in errs)):
            raise SystemExit("%s (%s): %s is the failure callback's value — it needs `errors: true`, is recorded "
                             "(`record: true`) and never judged, and stands beside a path of the delivery"
                             % (at, decl["subscribe"], errs[0]))
        if "arm_only" in decl and any(not (checks or {}).get(RECORD) for checks in returns.values()):
            raise SystemExit("%s (%s): `arm_only` asserts no delivery, so its `returns` records paths "
                             "(`record: true`) and judges none" % (at, decl["subscribe"]))
        for path, checks in returns.items():
            if not PATH.match(str(path)):
                raise SystemExit("%s (%s): bad path %r — write $ or $.a.0.b" % (at, decl["subscribe"], path))
            if str(path) in errs:                # held to PATH and to `record: true` above: no type to hold it to
                continue
            if not isinstance(checks, dict) or not checks:
                raise SystemExit("%s (%s) %s: no comparator" % (at, decl["subscribe"], path))
            _reject_unknown("%s (%s) %s" % (at, decl["subscribe"], path), checks, COMPARATORS)
            # (Tier C, C3) COMPARATORS is shared, and the reference is an api hook's reading.
            refs = sorted(set(checks) & set(REFS))
            if refs:
                raise SystemExit("%s (%s) %s: `%s` compares with an earlier api hook's reading, and an "
                                 "event delivery has none — assert it in an api hook"
                                 % (at, decl["subscribe"], path, refs[0]))
            # (Tier C, C4.2) The `recorded` row read api hooks only: "a delivery is asserted; a value worth recording is
            # read back by a call".  (Tier C, C5.0) Decision 25 A reversed that: a value only an event carries cannot be
            # read back by a call.  So `record: true` on an event path, the api hook's rule: true, and standing alone.
            if RECORD in checks and (checks[RECORD] is not True or len(checks) > 1):
                raise SystemExit("%s (%s) %s: `record` is true, and stands alone — it reads the path and judges "
                                 "nothing" % (at, decl["subscribe"], path))
            if checks.get("absent") and len(checks) > 1:
                raise SystemExit("%s (%s) %s: `absent: true` stands alone" % (at, decl["subscribe"], path))
            _in_list("%s (%s) %s" % (at, decl["subscribe"], path), checks)
            try:
                re.compile(str(checks.get("matches", "")))
            except re.error as e:
                raise SystemExit("%s (%s) %s: `matches` is not a regex (%s)" % (at, decl["subscribe"], path, e))
            kinds = str(checks.get("type", "")).split("|") if "type" in checks else []
            if set(kinds) - WIRE_TYPES:
                raise SystemExit("%s (%s) %s: unknown type(s) %s" % (at, decl["subscribe"], path,
                                 ", ".join(sorted(set(kinds) - WIRE_TYPES))))
            if len(kinds) > 2:
                raise SystemExit("%s (%s) %s: a `type` union takes at most two alternatives"
                                 % (at, decl["subscribe"], path))
            _refuse_missing("%s (%s) %s" % (at, decl["subscribe"], path), kinds)
            _qualifiers("%s (%s)" % (at, decl["subscribe"]), path, checks, returns)
            # (Tier C, C4.0) Held to the emitted type as a hook's path is to its return.
            root = str(member.get("emits"))
            if str(path) != "$" and root != "Array<Object>":
                _check_state_path("%s (%s)" % (at, decl["subscribe"]), str(path), kinds, checks, excused,
                                  None if root == "State" else root)
            if str(path) == "$" and (kinds or _implied(checks)):
                allowed = wire_of(member, "emits")
                asked = set(kinds) | _implied(checks)
                if asked - allowed:
                    raise SystemExit("%s (%s) $: the types say the callback is handed `%s` (wire %s) "
                                     "— asserting %s here would launder a divergence; write a work "
                                     "order instead" % (at, decl["subscribe"], member.get("emits"),
                                                        "|".join(sorted(allowed)),
                                                        "|".join(sorted(asked - allowed))))


def _event_anchor(at, decl, i, step, anchor, key):
    """(Tier C, C5.0) Refuses a `none_after` / `min_after.ref` anchor this step cannot produce (EVENT_ID's comment)."""
    where = "%s (%s): `%s: %s`" % (at, decl["subscribe"], key, anchor)
    events, hooks, moves = step.get("events") or [], step.get("hooks") or [], step.get("moves") or []
    ev, api, mv = (EVENT_ID.match(anchor), REF_ID.match(anchor), MOVE_ID.match(anchor)) if isinstance(anchor, str) \
        else (None, None, None)
    if anchor == "remove":
        if "remove_at_s" not in decl:
            raise SystemExit("%s — this declaration removes nothing: give it `remove_at_s`" % where)
    elif ev:
        j = int(ev.group(2))
        if j == i or j >= len(events) or events[j].get("subscribe") != ev.group(1):
            raise SystemExit("%s — no other declaration %s in this step's events" % (where, anchor))
        if "remove_at_s" not in events[j]:
            raise SystemExit("%s — %s removes nothing (no `remove_at_s`), so it has no removal to count from"
                             % (where, anchor))
    elif api:
        n = int(api.group(2))
        if n >= len(hooks) or hooks[n].get("api") != api.group(1):
            raise SystemExit("%s — hook %d of this step is not %s (ids count every hook from 0)"
                             % (where, n, api.group(1)))
    elif mv:
        if int(mv.group(1)) >= len(moves):
            raise SystemExit("%s — this step declares %d move(s)" % (where, len(moves)))
    else:
        raise SystemExit("%s — an anchor is `remove`, `<member>@<i>`, `<api>#<n>` or `move<i>`" % where)


# (Tier C, C5.0) `moves:` and `actions:` — what the HOST does while a step runs, each once the log shows every one of
# its `after:` conditions, plus `delay_s`.  Host-only: the device is never told (it is not in launch_payloads), and
# every send is marked into the device's own log.  A move puts the emulator's or Simulator's location `to:` an offset
# from STANDING_FIX, in `hops` equal steps `every_s` apart from where the previous move left it (the fix for the first),
# and the step may not end until `observe_s` after its last hop; an action does ONE thing, to the device (Android:
# `network`, wifi and mobile data; (C6.1) `location`, the master location switch; `power_save`, battery saver;
# (WO-114) `kill: true`, SIGKILL of the app's process, as the low-memory killer does) or to
# the step's lab server (`server: revoke`, LabHttp in run.py).  The `after:` grammar, one key per condition, all of
# which must hold:
#   {event: <member>@<i>}   that declaration was delivered (`app=event id=… n=`); `{event: "onGeofence ENTER"}` is
#                           the one onGeofence declaration asserting `$.action equals ENTER`, delivered with that value
#   {armed: <member>@<i>}   that declaration armed (`app=event-arm … outcome=ok`)
#   {api: <api>#<n>}        that hook settled (`app=api id=… outcome=`)
#   {move: <j>}             move j's last hop was sent (an earlier move, for a move)
#   {point: <name>}         a `LAB point=<name>` line: a host mark, or the driver's own (`point=onDestroy`)
#   {headless: <name>}      (C6.1) the headless task received that event (`point=headless event=<name>`)
MOVE_KEYS = {"after", "delay_s", "to", "hops", "every_s", "observe_s"}
AFTER_KINDS = ("event", "armed", "api", "move", "point", "headless")
# (C6.1) What `server:` can do to the step's lab server: `revoke` invalidates the access token it issued, so the SDK's
# next upload is answered 401 and it refreshes (LabHttp).
SERVER_ACTIONS = ("revoke",)
# (Tier C, C5.3; item 3b) `notification_tap: <id>` taps the app's own notification button `<package>:id/<id>` in the
# shade (the SDK's `notificationButtonPause`): the SDK's service is not exported, so only a tap sends its intent.
NOTIFICATION_BUTTON = re.compile(r"^[A-Za-z_]\w*$")
# (C5.3 review) What a tap can take beyond its `delay_s`: the wait for the button (backends.Adb.NOTIFICATION_WAIT_S,
# 6 s) and the look that was running when it ran out (one `uiautomator dump` took 2.9 to 3.5 s on the pilots).  A
# button that never comes is a scored miss (`actionsDone: action<i>=missed(…)`), so the step's window holds it.
NOTIFICATION_TAP_S = 10
SUGAR = re.compile(r"^([A-Za-z]\w*) ([A-Z]+)$")


class ActionHandler:
    """(WO-191) One kind of `actions:` item, keyed by its name in ACTION_HANDLERS.  validate(at, item, step) refuses a
    bad value at load (SystemExit) and returns the seconds the action can take beyond its `delay_s` (0 or None for
    none); `item` is step["actions"][i] itself, `after:` resolved, and the items before it are resolved too.
    run(dev, item, plat, server) does it, on run_host's thread, and returns the `what` of its `host i=<i> action=<what>`
    mark; `oses`, where it can run: a scenario excuses each other OS, dated; `keys`, what the item may carry besides
    `after`, `delay_s` and the kind's own key (fetch's `job:` takes `expect:`), refused on any other kind."""

    def __init__(self, validate, run, oses=("android",), keys=()):
        self.validate, self.run, self.oses, self.keys = validate, run, tuple(oses), frozenset(keys)


def _network_ok(at, item, step):
    net = item["network"]
    if not (isinstance(net, dict) and net and set(net) <= {"wifi", "data"}
            and all(isinstance(v, bool) for v in net.values())):
        raise SystemExit("%s: `network:` is {wifi: true|false, data: true|false}, one or both — not %r" % (at, net))
    return 0


def _network_run(dev, item, plat, server):
    dev.network(**item["network"])
    return "network " + " ".join("%s=%s" % (k, "on" if v else "off") for k, v in sorted(item["network"].items()))


def _switch(kind):
    """(C6.1) `location` and `power_save`: a switch, set and read back."""
    def ok(at, item, step):
        if not isinstance(item[kind], bool):
            raise SystemExit("%s: `%s:` is true (on) or false (off) — not %r" % (at, kind, item[kind]))
        return 0

    def run(dev, item, plat, server):
        note = getattr(dev, kind)(item[kind])    # (C6.1 pilot) location on: `dismissed=…` or ""
        return "%s %s%s" % (kind, "on" if item[kind] else "off", " " + note if note else "")
    return ActionHandler(ok, run)


def _server_ok(at, item, step):
    if item["server"] not in SERVER_ACTIONS:
        raise SystemExit("%s: `server:` is one of %s — not %r" % (at, ", ".join(SERVER_ACTIONS), item["server"]))
    if not uses_lab_http(step):
        raise SystemExit("%s: `server:` acts on the step's lab server, and this step starts none — its config and "
                         "hooks never name %s" % (at, LAB_HTTP))
    return 0


def _server_run(dev, item, plat, server):
    server.revoke()                      # (C6.1) the step's lab server: run_step started it
    return "server %s" % item["server"]


def _notification_tap_ok(at, item, step):
    if not (isinstance(item["notification_tap"], str) and NOTIFICATION_BUTTON.match(item["notification_tap"])):
        raise SystemExit("%s: `notification_tap:` names the button's resource id in the app's notification layout "
                         "(`notificationButtonPause`) — not %r" % (at, item["notification_tap"]))
    # (C5.3 review) The tap finds its button with `uiautomator dump`, as the dialog watcher reads the screen (run.py),
    # into the same file: two at once would read each other's.
    blocks = [step.get("expect")] + list((step.get("platform_expect") or {}).values())
    if step.get("dialog") or any("undeclaredDialogs" in (b or {}) for b in blocks):
        raise SystemExit("%s: `notification_tap:` cannot share a step with `dialog:` or `undeclaredDialogs` — the tap "
                         "and the dialog watcher both read the screen with `uiautomator dump`, one at a time" % at)
    return NOTIFICATION_TAP_S


def _notification_tap_run(dev, item, plat, server):
    # (C5.3 review) a button that is not there is the SDK's doing (its layout or its id lost): marked and scored
    # (actionsDone), with the step's log kept, not a SystemExit
    missed = dev.notification_tap((plat or {}).get("package"), item["notification_tap"])
    return "notification_tap %s%s" % (item["notification_tap"], " missed=%s" % missed if missed else "")


def _kill_ok(at, item, step):
    if item["kill"] is not True:
        raise SystemExit("%s: `kill:` is true, a SIGKILL of the app's process — not %r" % (at, item["kill"]))
    return 0


def _kill_run(dev, item, plat, server):
    what = "kill " + dev.kill(plat)      # (WO-114) SIGKILL the app; the lab server counts from it
    if server is not None:
        server.killed()
    return what


# (WO-191) The action kinds, a table a product adds its own to (fetch: `job`, `force_stop`) instead of copying run_host.
# Read on use, never frozen at import: ACTION_KEYS was, so a kind added after it was refused as unknown.  In the order
# an action's refusal lists them.
ACTION_HANDLERS = {
    "network": ActionHandler(_network_ok, _network_run),
    "location": _switch("location"),
    "power_save": _switch("power_save"),
    "server": ActionHandler(_server_ok, _server_run),
    "notification_tap": ActionHandler(_notification_tap_ok, _notification_tap_run),
    "kill": ActionHandler(_kill_ok, _kill_run),
}


def _validate_moves(where, step, excused=()):
    """(Tier C, C5.0) A step's `moves:` and `actions:` (the comment above), with each `after:` resolved: the sugar to
    the declaration it names, so run.json records what the host waited for, as `previous` is recorded resolved."""
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    # (Tier C, C6.1) `need` is the longest chain, not the sum: each item starts once its own conditions hold (a slot,
    # or the end of a move it waits for) and ends its delay, hops and observe_s later.  Items that wait on the same
    # thing run together; summed, headless-android-causes' seven toggles asked a 38 s window for a 6 s chain.
    need, move_end, kinds = 0.0, [], []
    for key in ("moves", "actions"):
        if step.get(key) is None:
            continue
        if not isinstance(step[key], list) or not step[key]:
            raise SystemExit("%s: `%s:` is a non-empty list" % (where, key))
        step[key] = list(step[key])          # (C5.0 review) a YAML alias shares the list between steps: each its own
        for i, item in enumerate(step[key]):
            at = "%s %s %d" % (where, key, i)
            if not isinstance(item, dict):
                raise SystemExit("%s: a mapping with `after:` and what to do" % at)
            _reject_unknown(at, item, MOVE_KEYS if key == "moves" else {"after", "delay_s"} | set(ACTION_HANDLERS)
                            | {k for h in ACTION_HANDLERS.values() for k in h.keys})   # (WO-191) read on use
            after = item.get("after")
            after = [after] if isinstance(after, dict) else after
            if not isinstance(after, list) or not after:
                raise SystemExit("%s: `after:` names what the log must show first — a host that sends on its own clock "
                                 "is timed from its launch, which nothing on the device saw" % at)
            conds, start = [], 0
            for cond in after:
                resolved, s = _after_condition(at, cond, step, i if key == "moves" else None)
                conds.append(resolved)
                start = max(start, move_end[resolved["move"]] if "move" in resolved else s)
            # (C5.0 review) move_plan starts each move's hops where the one before it left the device, so the moves go
            # in list order: a later one sent first would hop from the wrong place.
            if key == "moves" and i and {"move": i - 1} not in conds:
                raise SystemExit("%s: a later move waits for the one before it — add {move: %d} to its `after:` "
                                 "(its hops start where move %d left the device)" % (at, i - 1, i - 1))
            step[key][i] = item = dict(item, after=conds)        # a YAML alias shares the mapping: a new one
            delay = item.get("delay_s", 0)
            if not number(delay) or delay < 0:
                raise SystemExit("%s: `delay_s` is seconds after the conditions hold, 0 or more — not %r" % (at, delay))
            span = delay
            if key == "moves":
                to = item.get("to")
                if not (isinstance(to, dict) and set(to) == {"offset_m", "bearing"} and number(to["offset_m"])
                        and 0 < to["offset_m"] <= 5000 and number(to["bearing"]) and 0 <= to["bearing"] < 360):
                    raise SystemExit("%s: `to:` is {offset_m: (0, 5000], bearing: [0, 360)} from the standing fix — "
                                     "not %r" % (at, to))
                hops = item.get("hops", 1)
                if not (isinstance(hops, int) and not isinstance(hops, bool) and hops >= 1):
                    raise SystemExit("%s: `hops` is a whole number, at least 1 — not %r" % (at, hops))
                if ("every_s" in item) != (hops > 1) or hops > 1 and not (number(item["every_s"])
                                                                           and item["every_s"] >= 1):
                    raise SystemExit("%s: `every_s` (at least 1 s) spaces the hops, so it is there exactly when "
                                     "`hops` is above 1" % at)
                observe = item.get("observe_s", 0)
                if not number(observe) or observe < 0:
                    raise SystemExit("%s: `observe_s` is seconds after the last hop, 0 or more — not %r" % (at, observe))
                span += (hops - 1) * item.get("every_s", 0) + observe
            else:
                does = [k for k in ACTION_HANDLERS if k in item]
                if len(does) != 1:
                    raise SystemExit("%s: an action does exactly one of %s — not %s"
                                     % (at, ", ".join(ACTION_HANDLERS), " and ".join(does) or "nothing"))
                others = sorted(k for k in item if k not in ACTION_HANDLERS and k not in ("after", "delay_s")
                                and k not in ACTION_HANDLERS[does[0]].keys)
                if others:                       # (WO-191) another kind's key (fetch's `expect:` on a `kill:`)
                    raise SystemExit("%s: %s belong%s to another kind of action, not a `%s:`"
                                     % (at, ", ".join("`%s:`" % k for k in others), "" if len(others) > 1 else "s",
                                        does[0]))
                extra = ACTION_HANDLERS[does[0]].validate(at, item, step) or 0    # (WO-191) the kind's own rules
                if not number(extra) or extra < 0:
                    raise SystemExit("%s: the `%s` handler's validate returned %r — the seconds the action can take "
                                     "beyond its delay_s, 0 or more (labkit.ActionHandler)" % (at, does[0], extra))
                span += extra
                kinds.append(does[0])
            need = max(need, start + span)
            if key == "moves":
                move_end.append(start + span)
    # (WO-191) Each kind says where it runs (ActionHandler.oses).  Every built-in is Android's, so for them this is the
    # rule it was: a step with actions is in a scenario that excuses ios.
    for os_name in ("android", "ios"):
        cannot = [k for k in dict.fromkeys(kinds) if os_name not in ACTION_HANDLERS[k].oses]
        if cannot and os_name not in excused:
            raise SystemExit("%s: `actions:` are Android's (the Simulator has no network of its own) — assert them in a "
                             "scenario whose not_applicable excuses ios, with its date" % where if os_name == "ios" else
                             "%s: `actions:` %s cannot run on %s — assert them in a scenario whose not_applicable "
                             "excuses %s, with its date" % (where, ", ".join(cannot), os_name, os_name))
    window = int(step.get("settle", 0)) + int(step.get("after", 0))
    if need and window < ANCHOR_BUDGET_S + need + TICK_SECONDS:
        raise SystemExit("%s: the moves and actions need %g s after the first batch (each after its own conditions — "
                         "a slot, or the end of a move it waits for — then its delay, hops and observe_s) and this "
                         "step watches %d s (settle %s + after %s) — raise them "
                         "to at least %d s" % (where, need, window, step.get("settle"), step.get("after"),
                                               math.ceil(ANCHOR_BUDGET_S + need + TICK_SECONDS)))


def _after_condition(at, cond, step, move_i):
    """(Tier C, C5.0) One `after:` condition, resolved, and the `at_s` slot it waits for (0 for none)."""
    kinds = [k for k in AFTER_KINDS if isinstance(cond, dict) and k in cond]
    if not isinstance(cond, dict) or len(cond) != 1 or len(kinds) != 1:
        raise SystemExit("%s: an `after:` condition is one of {%s: …}, not %r" % (at, ": …}, {".join(AFTER_KINDS), cond))
    kind, value = kinds[0], cond[kinds[0]]
    events, hooks, moves = step.get("events") or [], step.get("hooks") or [], step.get("moves") or []
    where = "%s: `after: {%s: %s}`" % (at, kind, value)
    if kind in ("event", "armed"):
        sugar = SUGAR.match(value) if kind == "event" and isinstance(value, str) else None
        if sugar:
            found = [j for j, d in enumerate(events) if d.get("subscribe") == sugar.group(1)
                     and ((d.get("returns") or {}).get("$.action") or {}).get("equals") == sugar.group(2)]
            if len(found) != 1:
                raise SystemExit("%s — %d %s declarations assert `$.action equals %s`; the sugar names exactly one"
                                 % (where, len(found), sugar.group(1), sugar.group(2)))
            return {"event": "%s@%d" % (sugar.group(1), found[0]), "action": sugar.group(2)}, \
                events[found[0]].get("at_s", 0)
        m = EVENT_ID.match(value) if isinstance(value, str) else None
        if not m or int(m.group(2)) >= len(events) or events[int(m.group(2))].get("subscribe") != m.group(1):
            raise SystemExit("%s — no such declaration in this step's events (ids are <member>@<index>)" % where)
        return {kind: value}, events[int(m.group(2))].get("at_s", 0)
    if kind == "api":
        m = REF_ID.match(value) if isinstance(value, str) else None
        if not m or int(m.group(2)) >= len(hooks) or hooks[int(m.group(2))].get("api") != m.group(1):
            raise SystemExit("%s — no such api hook in this step (ids count every hook from 0)" % where)
        return {kind: value}, hooks[int(m.group(2))].get("at_s", 0)
    if kind == "move":
        limit = len(moves) if move_i is None else move_i
        if not (isinstance(value, int) and not isinstance(value, bool) and 0 <= value < limit):
            raise SystemExit("%s — %s" % (where, "a move waits for an EARLIER one" if move_i is not None
                                          else "this step declares %d move(s)" % len(moves)))
        return {kind: value}, 0
    if kind == "headless":
        if value not in headless_names():
            raise SystemExit("%s — not a headless event name (%s)" % (where, ", ".join(headless_names())))
        return {kind: value}, 0
    if not (isinstance(value, str) and re.fullmatch(r"[A-Za-z][\w-]*", value)):
        raise SystemExit("%s — a point is the name a `LAB point=` line starts with" % where)
    return {kind: value}, 0


def after_unmet(conds, norm, step):
    """(Tier C, C5.0) The first `after:` condition the normalized lines `norm` do not show yet, or None when all hold —
    for run.py's move runner, over the log so far, and for movesSent / actionsDone, over the capture."""
    def holds(cond):
        if "event" in cond:
            needle = "%s app=event id=%s n=" % (TAG, cond["event"])
            # (Tier C, R4; R's review) Whichever delivery said so: a declaration with `delivery:` names each
            # line's own (` n=<k>` before `path=`), and its ENTER is as much an ENTER.
            action = re.compile(r"%s app=ret id=%s (?:n=\d+ )?path=\$\.action type=string value=%s "
                                % (re.escape(TAG), re.escape(cond["event"]), re.escape(str(cond.get("action")))))
            return any(needle in n for n in norm) and ("action" not in cond or any(action.search(n) for n in norm))
        if "armed" in cond:
            return any("%s app=event-arm id=%s outcome=ok" % (TAG, cond["armed"]) in n for n in norm)
        if "api" in cond:
            return any("%s app=api id=%s outcome=" % (TAG, cond["api"]) in n for n in norm)
        if "move" in cond:
            hops = (step.get("moves") or [])[cond["move"]].get("hops", 1)
            return any("%s point=move i=%d hop=%d/%d " % (TAG, cond["move"], hops, hops) in n for n in norm)
        if "headless" in cond:
            return any("%s point=headless event=%s " % (TAG, cond["headless"]) in n + " " for n in norm)
        return any("%s point=%s " % (TAG, cond["point"]) in n + " " for n in norm)
    return next((cond for cond in conds if not holds(cond)), None)


def destination(offset_m, bearing, origin=STANDING_FIX):
    """(Tier C, C5.0) The point `offset_m` from `origin` along `bearing` (degrees from north), on a sphere of EARTH_R_M,
    to 6 decimals (about 0.1 m).  Against Android's `distanceTo` (an ellipsoid) the error is under 1 m at 5 km."""
    lat1, lon1, theta = math.radians(origin[0]), math.radians(origin[1]), math.radians(bearing)
    delta = offset_m / EARTH_R_M
    lat2 = math.asin(math.sin(lat1) * math.cos(delta) + math.cos(lat1) * math.sin(delta) * math.cos(theta))
    lon2 = lon1 + math.atan2(math.sin(theta) * math.sin(delta) * math.cos(lat1),
                             math.cos(delta) - math.sin(lat1) * math.sin(lat2))
    return round(math.degrees(lat2), 6), round(math.degrees(lon2), 6)


def move_plan(step):
    """(Tier C, C5.0) Per move, its hops as [(lat, lon), …]: equal steps from where the previous move left the device
    (the standing fix for the first) to its own `to:`.  Straight in latitude and longitude between the two, which for
    a few kilometres is the great circle to well under a metre."""
    plan, here = [], STANDING_FIX
    for move in step.get("moves") or []:
        there = destination(move["to"]["offset_m"], move["to"]["bearing"])
        n = move.get("hops", 1)
        plan.append([(round(here[0] + (there[0] - here[0]) * h / n, 6), round(here[1] + (there[1] - here[1]) * h / n, 6))
                     for h in range(1, n + 1)])
        here = there
    return plan


def wire_events(events):
    """What the device is told: WHICH member to subscribe to, under which id, and which paths to
    report from the first delivery (or up to its `delivery`).  Never `min`, never a comparator — a
    driver that knows what is expected can pass its own bad result.  The id is `<member>@<index>`:
    `@`, not `#`, so an event id can never collide with an `api` hook's `<member>#<index>` in the
    shared `ret` grammar."""
    wired = []
    for i, d in enumerate(events or []):
        # (Tier C, C5.0) An `arm_only` or `max: 0` declaration may assert no path: the driver reports none.
        one = {"on": d["subscribe"], "id": "%s@%d" % (d["subscribe"], i), "paths": list(d.get("returns") or {})}
        if "args" in d:      # what the member takes BEFORE its callback (watchPosition's options)
            one["args"] = d["args"]
        if "at_s" in d:      # the slot to ARM in, s after the first batch; absent means at boot
            one["at_s"] = d["at_s"]
        if "remove_at_s" in d:   # (Tier C, C5.0) the slot to REMOVE the Subscription in (the drivers act on it after R)
            one["remove_at_s"] = d["remove_at_s"]
        if "delivery" in d:      # (Tier C, R4) the delivery the paths are read from: reported for 1..N, each named
            one["delivery"] = d["delivery"]
        if d.get("errors"):      # (Tier C, R6) the failure callback is passed too
            one["errors"] = True
        wired.append(one)
    return wired


def wire_hook(hook):
    """What the device is told (WO-027): the method, its args, and WHICH paths to report.
    Never the comparators — a driver that knows what is expected can pass its own bad result."""
    if "api" not in hook:
        return hook
    # (Tier C, C4.0) A hook that asserts only its `outcome` still reports its root, at `$` if the call resolves
    # and `$err` if it rejects — the value `settled` records.  Never `outcome` itself, `invalid` or `settles_only`.
    # (Tier C, C4.3) A `settled` one also asks a rejection's `code` and `message` (SETTLED_PATHS): its reading is
    # the `recorded` row's, and an Error or a map reads `object` at the root.
    wire = {"api": hook["api"], "paths": list(hook["returns"]) if "returns" in hook else
            list(SETTLED_PATHS) if _settled(hook) else ["$", "$err"]}
    if "args" in hook:                   # key presence, not truthiness: absent means ZERO arguments
        wire["args"] = hook["args"]
    if "at_s" in hook:                   # seconds after the first batch completes; absent means in it
        wire["at_s"] = hook["at_s"]
    if hook.get("probe"):
        wire["probe"] = True
    if "generate" in hook:               # (WO-107) the argument, built on the device
        wire["generate"] = hook["generate"]
    if "alongside" in hook:              # (WO-107) the call issued beside this one, timed
        wire["alongside"] = hook["alongside"]
    return wire


# (Tier C, C4.4 pilot) What adb sends as one shell request: "shell,v2,raw:" + the argv joined by spaces, its length
# four hex digits.  MEASURED 2026-09-27 on emulator-5582 (adb 34.0.4): 65,535 B ran, 65,536 exited 1 with `error:
# closed` and ran nothing.  Android's launch is one such request (backends.Adb.launch), which refuses past it.
ADB_COMMAND_LIMIT = 65535
# (Tier C, C4.0) scenario() refuses a step whose launch extras are larger (see launch_payloads).  (Tier C, C4.4
# pilot) The request's cap less 2 KiB for the rest of the command — the component and the other extras with their
# flags, about 300 B today.  It was 96 KiB, INFERRED from the device shell's 128 KiB argument limit, which adb's
# request never reaches: the pilot's 72,460 B step fit that and never launched.
LAUNCH_LIMIT = ADB_COMMAND_LIMIT - 2048


# (Tier C, C6.1) The step's own HTTP server (run.py LabHttp), named in a step's config or hook args as this placeholder:
# `http: {url: "{lab_http}/locations"}`, `authorization: {refreshUrl: "{lab_http}/refresh"}`.  run.py starts one for a
# step that names it, and sends the step with the placeholder replaced by the address the device reaches the host at:
# the emulator's alias for the host's loopback, or the Simulator's own loopback (it shares the Mac's).  Only the exact
# text: the SDK's own `{refreshToken}` / `{accessToken}` templates pass through untouched.
LAB_HTTP = "{lab_http}"
LAB_HTTP_HOST = {"android": "10.0.2.2", "ios": "127.0.0.1"}
LAB_HTTP_ROUTES = ("locations", "refresh")
LAB_HTTP_STATUSES = (200, 401, 503)
# (C6.1, launch-http-flush) A step's `lab_http:` scripts its server: `{locations: <status>}` answers every upload with
# that status, whatever its token (launch-http-flush's queue step refuses them all with 503, so they stay queued).
# (Tier C, C5.3) `{revoked: true}` starts the server with the step's own access token already revoked, as
# `actions: server: revoke` leaves it: the first upload is refused 401 and the SDK refreshes.  A step's key, not an
# action, so it holds on iOS too (actions are Android's) and waits on nothing.
LAB_HTTP_SCRIPT = {"locations", "revoked"}
# The tokens it issues: a scenario's own accessToken is the first, refreshToken the one /refresh accepts.
LAB_HTTP_ACCESS = "lab-access-%d"
LAB_HTTP_REFRESH = "lab-refresh"


def _validate_lab_http(where, step):
    """(Tier C, C6.1) A step's `lab_http:` script, on a step that starts a lab server: `{locations: <status>}`, or
    (C5.3) `{revoked: true}` where the step configures the access token there is to revoke."""
    script = step.get("lab_http")
    if script is None:
        return
    if not (isinstance(script, dict) and len(script) == 1 and set(script) <= LAB_HTTP_SCRIPT):
        raise SystemExit("%s: `lab_http:` is {locations: <status>} or {revoked: true}, one of them — not %r"
                         % (where, script))
    status = script.get("locations")
    if "locations" in script and not (isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599):
        raise SystemExit("%s: `lab_http: {locations: …}` is an HTTP status, 100-599 — not %r" % (where, status))
    if "revoked" in script and not (script["revoked"] is True
                                    and ((step.get("config") or {}).get("authorization") or {}).get("accessToken")):
        raise SystemExit("%s: `lab_http: {revoked: true}` starts the server with the step's access token revoked — it "
                         "is `true`, on a step whose config carries `authorization.accessToken`" % where)
    if not uses_lab_http(step):
        raise SystemExit("%s: `lab_http:` scripts the step's lab server, and this step starts none — its config and "
                         "hooks never name %s" % (where, LAB_HTTP))


def uses_lab_http(step):
    """(Tier C, C6.1) Does this step name its lab server — in its config, or in a hook's args?"""
    return LAB_HTTP in json.dumps([step.get("config"), step.get("hooks")])


def with_lab_http(step, base):
    """(Tier C, C6.1) The step as sent: `{lab_http}` replaced by `base` in its config and hooks, every string leaf."""
    def sub(v):
        if isinstance(v, str):
            return v.replace(LAB_HTTP, base)
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v
    return dict(step, config=sub(step.get("config")), hooks=sub(step.get("hooks")))


def launch_payloads(step):
    """(Tier C, C4.0) The three launch extras that carry a step to the device, base64'd JSON, as run.py sends
    them — built here, once, so the size scenario() holds under LAUNCH_LIMIT is the size that travels.
    (WO-027) wire_hook strips an api hook's comparators, and wire_events does the same for an `events:`
    declaration: the device is told what to call or subscribe to and which paths to report, never what is
    expected back, never `min`.  allow_nan=False: NaN or Infinity is not JSON, and the driver's parse would
    throw on the whole extra."""
    return {key: base64.b64encode(json.dumps(value, allow_nan=False).encode()).decode() for key, value in (
        ("configB64", step["config"]), ("hooksB64", [wire_hook(h) for h in step["hooks"]]),
        ("eventsB64", wire_events(step["events"])))}


# (Tier C, C3) The config round trip.  A step's `roundtrip: {scope, groups}` generates its hooks: each
# Config leaf of the named groups is set on its own, read back, and restored by one reset(), and every
# other leaf is held `same:` the cell's own earlier reading.  Paths come from the vocabulary; the one
# thing a person supplies is a safe VALUE per leaf, in labs/harness/roundtrip.yaml.
ROUNDTRIP_KEYS = {"scope", "groups"}
RT_ENTRY_KEYS = {"value", "skip", "readback", "restore"}
RT_BASELINE = "getState#2"          # the plan's third hook: stop, reset, then this
# Printable ASCII, 1-64 characters, no edge whitespace: Android trims (SpecIndex.java:427), a ret value is
# clipped at 200 (labapi.js LIMIT) and an iOS os_log line at about 1 KB (README).
RT_STRING = re.compile(r"^[\x21-\x7e](?:[\x20-\x7e]{0,62}[\x21-\x7e])?$")
RT_RECORD_KEY = re.compile(r"^[A-Za-z0-9_]+$")          # PATH's segment: the key is asserted as `$.k.<key>`
# The launch carries the wired hooks base64'd in one argument; 6,044 B is the largest proven on all eight
# cells (api-config).  Until C3.1 measures the transport, a step stays under this.
# (Tier C, C4.0) LAUNCH_LIMIT (the three extras together) holds every step, this one too; this limit, on hooksB64
# alone, is the tighter and binds a generated step.  The transport is proven to 37,748 B (C3's largest step, on
# Android; (Tier C, C4.4 review) iOS's largest is 30,984 B), so a step a group list can grow stays at 48 KiB until a
# device run proves more — C4.4's ~75 KB generated steps (the C4 plan's item 8) raise or retire it then.
# (Tier C, C4.4) Not raised: the invalid-value steps carry their own budget (IV_STEP_LIMIT), and their pilot proves the
# transport at that size first.  A roundtrip step keeps this one whatever the pilot says, until a change here is its own.
# (Tier C, C4.4 pilot) LAUNCH_LIMIT is adb's measured request cap less 2 KiB (63,487 B), no longer 96 KiB.
RT_STEP_LIMIT = 48 * 1024
_RT_TABLE = None
_RT_PRINTED = set()


def config_leaves():
    """(Tier C, C3) Every Config leaf State carries, dotted (`geolocation.filter.policy`), in vocabulary
    order: Config minus State.ts's `Omit<Config, …>` list.  A Record or an array is one leaf."""
    omit = (vocabulary().get("state") or {}).get("omit") or []
    out = []

    def walk(node, prefix):
        for k, v in node.items():
            walk(v, prefix + k + ".") if isinstance(v, dict) else out.append(prefix + k)
    walk({k: v for k, v in (vocabulary().get("config") or {}).items() if k not in omit}, "")
    return out


def _rt_kind(v):
    return ("boolean" if isinstance(v, bool) else "number" if isinstance(v, int) else
            "string" if isinstance(v, str) else "array" if isinstance(v, list) else "object")


def _rt_literal(where, v):
    """A table scalar: an integer, a boolean, or a short printable-ASCII string."""
    if isinstance(v, float):
        raise SystemExit("%s: %r — integers only: React Native widens a float on Android (10.1 reads back "
                         "10.100000381469727, RNBackgroundGeolocationModule.java:1132), and some keys are int "
                         "on one OS and double on the other" % (where, v))
    if v is None:
        raise SystemExit("%s: null — never: keep-or-default on null is itself a cross-OS divergence (C4)" % where)
    if isinstance(v, (list, dict)):
        raise SystemExit("%s: %r — a list or map holds scalars: the expansion asserts one level down" % (where, v))
    if isinstance(v, str) and not RT_STRING.match(v):
        raise SystemExit("%s: %r — a string is printable ASCII, 1-64 characters, with no leading or trailing "
                         "whitespace (Android trims, SpecIndex.java:427)" % (where, v))
    if not isinstance(v, (bool, int, str)):
        raise SystemExit("%s: %r is not JSON — quote dates and timestamps" % (where, v))


def _rt_entry(key, entry):
    """(Tier C, C3) One roundtrip.yaml entry, held to the vocabulary as a scenario's literal would be."""
    where = "labs/harness/roundtrip.yaml %s" % key
    if key not in config_leaves():
        raise SystemExit("%s: not a Config leaf State carries (vocabulary.yaml `config:` minus `state.omit`) — "
                         "a stale entry the types renamed or dropped, a group, or a typo" % where)
    if not isinstance(entry, dict) or ("value" in entry) == ("skip" in entry):
        raise SystemExit("%s: an entry is {value: …} or {skip: \"YYYY-MM-DD why\"}, exactly one" % where)
    _reject_unknown(where, entry, RT_ENTRY_KEYS)
    if "skip" in entry:
        if set(entry) != {"skip"} or not re.match(r"\d{4}-\d{2}-\d{2}\b\s*\S", str(entry["skip"] or "")):
            raise SystemExit("%s: a skip is one dated line, `skip: \"YYYY-MM-DD why\"`, and nothing else" % where)
        return
    value, path, both = entry["value"], "$." + key, ("android", "ios")    # scope is the scenario's business
    nested = value
    for seg in reversed(key.split(".")):
        nested = {seg: nested}
    _validate_config(where, nested, vocabulary().get("config") or {})
    if isinstance(value, (list, dict)):
        if not value:
            raise SystemExit("%s: empty — what an empty value means differs per OS (WO-050), and it round-trips "
                             "nothing" % where)
        _check_state_path(where, path, [_rt_kind(value)], {}, both)
        leaf = _state_leaf(path)
        for k, v in (value.items() if isinstance(value, dict) else enumerate(value)):
            _rt_literal("%s.%s" % (where, k), v)
            if isinstance(value, dict):
                if not RT_RECORD_KEY.match(str(k)):
                    raise SystemExit("%s: Record key %r — [A-Za-z0-9_] only, it is asserted as a path segment"
                                     % (where, k))
                _check_state_path(where, "%s.%s" % (path, k), [_rt_kind(v)], {"equals": v}, both)
                continue
            # An element is held to the array alternative's element type — `TriggerActivity[] |
            # TriggerActivity` takes no `.N` path, so C0's path check cannot reach it.
            elem = next((e for e in map(_array_element, _alternatives(leaf)) if e is not None), None) \
                if isinstance(leaf, str) and leaf is not OPEN else None
            values = ts_values(elem) if elem else None
            if elem and (_rt_kind(v) not in ts_wire(elem) or values is not None and v not in values):
                raise SystemExit("%s: %r is not a `%s`" % (where, v, elem))
    else:
        _rt_literal(where, value)
        _check_state_path(where, path, [_rt_kind(value)], {"equals": value}, both)
    if "readback" in entry:
        # The State reads the key as another scalar: triggerActivities [walking] reads `walking` (WO-050).
        readback = entry["readback"]
        if isinstance(readback, (list, dict)):
            raise SystemExit("%s: `readback` is a scalar — a list's or a Record's contents follow from `value`"
                             % where)
        _rt_literal(where + " readback", readback)
        if type(readback) is type(value) and readback == value:
            raise SystemExit("%s: `readback` equals `value` — drop it" % where)
        _check_state_path(where + " readback", path, [_rt_kind(readback)], {"equals": readback}, both)
    elif isinstance(value, list):
        try:
            _check_state_path(where, path + ".length", ["number"], {}, both)
        except SystemExit as e:
            raise SystemExit("%s — if State reads this key as a scalar, give it as `readback:`" % e)
    if "restore" in entry and (entry["restore"] != "same" or not isinstance(value, dict) or "readback" in entry):
        raise SystemExit("%s: `restore: same` is for a Record whose default carries these same keys (iOS's "
                         "locationAuthorizationAlert): restore then compares each with the baseline instead "
                         "of asserting it absent" % where)


def roundtrip_table():
    """(Tier C, C3) labs/harness/roundtrip.yaml, every entry validated at first use (its header is the schema)."""
    global _RT_TABLE
    if _RT_TABLE is None:
        table = _load(os.path.join(HARNESS, "roundtrip.yaml")) or {}
        if not isinstance(table, dict):
            raise SystemExit("labs/harness/roundtrip.yaml: a map of `<group>.<key>: {value: …}` entries")
        for key, entry in table.items():
            _rt_entry(str(key), entry)
        # Every leaf, not only those of the groups some step names: a key the types gain is never
        # skipped in silence (Chris's decision (2), 2026-09-26).  The selftest holds each value to a step.
        missing = [k for k in config_leaves() if k not in table]
        if missing:
            raise SystemExit("labs/harness/roundtrip.yaml has no entry for %s — every Config leaf State carries "
                             "has one: a `value:`, or a dated `skip:`" % ", ".join(missing))
        _RT_TABLE = table
    return _RT_TABLE


def _roundtrip(where, rt, table):
    """(Tier C, C3) `roundtrip: {scope, groups}` -> the step's hooks (labs/README.md §5).  `groups` names Config
    groups (`app`, `app.notification`, `geolocation.filter`); each contributes its OWN leaves of the scope
    — a nested group is named on its own — and the keys are set in roundtrip.yaml's order:

        #0 stop, #1 reset                      $.enabled false
        #2 getState, the baseline              every in-scope leaf typed
        #3 … setConfig, one key each           the key {equals, differs: #2}; the rest of its top-level
                                               group (geolocation includes filter) same: previous
        getState, the read-back                each set key equals; every other leaf same: previous
        reset, the restore; getState           every leaf same: #2; a Record's set keys absent

    "In scope" is every Config leaf of both OSes, plus the scope's own for `android`/`ios`; the set keys are
    the scope's alone, as `declared:` partitions them.  A Record or array is asserted a level down: its
    keys, its `.length` and elements, which the baseline also reports where restore compares them."""
    if not isinstance(rt, dict):
        raise SystemExit("%s: `roundtrip` is {scope: both|android|ios, groups: [...]}" % where)
    _reject_unknown(where + " roundtrip", rt, ROUNDTRIP_KEYS)
    scope, groups = rt.get("scope"), rt.get("groups")
    if scope not in DECLARED:
        raise SystemExit("%s: roundtrip `scope` is both, android or ios — not %r" % (where, scope))
    if not isinstance(groups, list) or not groups or len(set(map(str, groups))) != len(groups):
        raise SystemExit("%s: roundtrip `groups` is a list of Config groups, each once" % where)
    own = None if scope == "both" else scope
    leaves = config_leaves()
    keys = []
    for group in map(str, groups):
        if not isinstance(_state_leaf("$." + group), dict):
            raise SystemExit("%s: roundtrip group %r is not a Config group (app, app.notification, "
                             "geolocation.filter, …)" % (where, group))
        mine = [k for k in leaves if k.rsplit(".", 1)[0] == group and state_scope("$." + k) == own]
        if not mine:
            raise SystemExit("%s: roundtrip group %s has no %s leaf of its own — a nested group is named on "
                             "its own (app.notification)" % (where, group, scope))
        for k in mine:
            if k not in table:
                raise SystemExit("%s: labs/harness/roundtrip.yaml has no entry for %s — give it a `value:`, "
                                 "or a dated `skip:`" % (where, k))
        keys += mine
    sets = [k for k in table if k in keys and "value" in table[k]]     # the table's order
    if not sets:
        raise SystemExit("%s: every key of roundtrip groups %s is skipped — the step sets nothing"
                         % (where, ", ".join(map(str, groups))))
    tree = [k for k in leaves if state_scope("$." + k) in (None, own)]
    top = lambda k: k.split(".")[0]
    shape = lambda k: table[k].get("readback", table[k]["value"])
    wire = lambda k: "object" if state_path_wire("$." + k) is OPEN else "|".join(sorted(state_path_wire("$." + k)))

    def written(k):
        """What setting k asserts about k: its value, or a container's type and contents."""
        v, p = shape(k), "$." + k
        if isinstance(v, dict):
            return dict([(p, {"type": "object"})] +
                        [("%s.%s" % (p, sk), {"type": _rt_kind(sv), "equals": sv}) for sk, sv in v.items()])
        if isinstance(v, list):
            return dict([(p, {"type": "array"}), (p + ".length", {"type": "number", "equals": len(v)})] +
                        [("%s.%d" % (p, n), {"type": _rt_kind(x), "equals": x}) for n, x in enumerate(v)])
        return {p: {"type": _rt_kind(v), "equals": v, "differs": RT_BASELINE}}

    def baseline(k):
        """What the baseline reports about k: its type — and, where restore compares them, an array's
        length and the keys of a Record whose default carries them."""
        v, p = (shape(k) if k in sets else None), "$." + k
        out = {p: {"type": wire(k)}}
        if isinstance(v, list):
            out[p + ".length"] = {"type": "number"}
        elif isinstance(v, dict) and table[k].get("restore") == "same":
            out.update(("%s.%s" % (p, sk), {"type": _rt_kind(sv)}) for sk, sv in v.items())
        return out

    def held(k, done):
        """Every path the step tracks for k, `same: previous` — its contents once k has been set."""
        return {p: {"same": "previous"} for p in (written(k) if k in done else baseline(k))}

    def restored(k):
        out = {p: {"same": RT_BASELINE} for p in baseline(k)}
        if k in sets and isinstance(shape(k), dict) and table[k].get("restore") != "same":
            out.update(("$.%s.%s" % (k, sk), {"absent": True}) for sk in shape(k))
        return out

    enabled = {"$.enabled": {"type": "boolean", "equals": False}}
    hooks = [{"api": "stop", "returns": enabled}, {"api": "reset", "returns": enabled},
             {"api": "getState", "returns": {p: c for k in tree for p, c in baseline(k).items()}}]
    for n, k in enumerate(sets):
        args = table[k]["value"]
        for seg in reversed(k.split(".")):
            args = {seg: args}
        returns = {}
        for other in tree:
            if top(other) == top(k):
                returns.update(written(k) if other == k else held(other, sets[:n]))
        hooks.append({"api": "setConfig", "args": args, "sets": k, "returns": returns})
    readback = {}
    for k in tree:
        readback.update({p: {x: y for x, y in c.items() if x != "differs"} for p, c in written(k).items()}
                        if k in sets else held(k, ()))
    restore = {p: c for k in tree for p, c in restored(k).items()}
    hooks += [{"api": "getState", "returns": readback}, {"api": "reset", "returns": restore},
              {"api": "getState", "returns": restore}]
    hooks = json.loads(json.dumps(hooks))      # no mapping shared between hooks, or with the table
    # The size guard: printed once per step, refused past the limit.
    size = len(base64.b64encode(json.dumps([wire_hook(h) for h in hooks]).encode()))
    if where not in _RT_PRINTED:
        _RT_PRINTED.add(where)
        print("%s: roundtrip %d keys, %d hooks, %d paths, hooksB64 %d B"
              % (where, len(sets), len(hooks), sum(len(h["returns"]) for h in hooks), size), file=sys.stderr)
    if size > RT_STEP_LIMIT:
        raise SystemExit("%s: hooksB64 is %d B, over the %d B a generated step may carry until a device run proves "
                         "the launch transport past it — split its groups across steps" % (where, size, RT_STEP_LIMIT))
    return hooks


# (Tier C, C4.4) The invalid-value table: the C4 plan's harness item 8, measured as its §2 C says.  A step's
# `invalid_values: {scope, groups, kinds, mode}` generates its hooks on C3's plan with one question changed: each
# number, enum and string leaf of the named groups is set, one value at a time, to one its range, its closed values or
# its type refuses, and what each cell STORED is recorded (`record: true`), never judged — decisions 5-10 rule on it
# first (C4.5).  Paths come from the vocabulary; the values per leaf and kind from labs/harness/invalid.yaml; the prime
# set before each one from roundtrip.yaml.
# (Tier C, C4.5) Ruled (docs/labs-tier-c-c4/c4.5-rulings.md): each value in the table carries its ruling beside it, and
# `mode: assert` asserts the ruled ones that no open work order still owns (R22's SUITE steps, api-config-values).
INVALID_KEYS = {"scope", "groups", "kinds", "mode"}
IV_MODES = ("measure", "assert")
IV_KINDS = ("below", "above", "undeclared", "float", "case", "explicitNull", "wrongType")
# The plan's split: the range kinds share a step; an explicit null and a value of the wrong type each run in steps of
# their own — how iOS takes them is NOT CHECKED (harness §8), and a raise there would take every later hook of the step.
IV_CLASS = {"below": "range", "above": "range", "undeclared": "range", "float": "range", "case": "range",
            "explicitNull": "explicitNull", "wrongType": "wrongType"}
IV_ENTRY_KEYS = {"below", "above", "undeclared", "float", "case", "wrongType", "skip", "unprimed", "couples",
                 "explicitNull", "unasserted"}
IV_SINCE = "2026-09-27"             # the date a generated set's `invalid:` carries: the day the table was written
# (Tier C, C4.5) A value's ruling: {value, ruled[, pending]} or {value, unasserted}.  `os_default` is R11's: the five keys
# whose default differs by OS read `same:` their own baseline, never one literal.  A ruled string is what a ret line
# carries whole (labapi.js clips a value at 200): the defaults it asserts are longer than a table value's 64.
IV_VALUE_KEYS = {"value", "ruled", "pending", "unasserted"}
IV_OS_DEFAULT = "os_default"
IV_RULED_STRING = re.compile(r"^[\x20-\x7e]{0,200}$")
WO_ID = re.compile(r"^WO-\d{3}$")
# One budget for a generated step's hooksB64: the plan's "about 1,500 paths, about 75 KB" (C3 measured 47-51 B a path),
# under LAUNCH_LIMIT for the three extras.  INFERRED: a device has carried 37,740 B and 801 rets on Android
# (api-roundtrip-android/app) and only 30,976 B and 600 rets on iOS (api-roundtrip/geolocation-filter), so the C4.4
# pilot runs the largest J step on one iOS and one Android cell before any board (README §5).  (Tier C, C4.4 review)
# The pilot proves the largest step there IS, not this budget: once it reads CARRIED on both, this constant is pinned
# to what it proved in the commit that records the pilot.  RT_STEP_LIMIT is not this.
# (Tier C, C4.4 pilot) Sweep 20260927-142735 (Capacitor, both OSes): 72,460 B (geolocation-range) CARRIED on iOS and
# never launched on Android — past ADB_COMMAND_LIMIT — so that step is split in two, and the largest J step is now
# filter-range, 59,796 B, CARRIED on both cells.  Pinned to 58.5 KiB over it, until a re-run of the pilot proves the
# split steps; LAUNCH_LIMIT still binds the three extras together.
IV_STEP_LIMIT = 58 * 1024 + 512
_IV_TABLE = None
_IV_PRINTED = set()


def iv_class(key):
    """(Tier C, C4.4) What J measures a Config leaf as: `number`, `enum` (a closed type — an enum or a literal union) or
    `string`; None for a leaf J leaves alone — a boolean, an array, a Record, triggerActivities' open union (the C4 plan's
    decision 13 names the number, enum and string leaves)."""
    leaf = _state_leaf("$." + key)
    if not isinstance(leaf, str) or leaf is OPEN:
        return None
    if ts_values(leaf) is not None:
        return "enum"
    wire = ts_wire(leaf)
    return "number" if wire == {"number"} else "string" if wire == {"string"} else None


def iv_kinds(key):
    """(Tier C, C4.4) (the kinds a leaf must answer — with values, or a dated skip —, the kinds it may).  A number:
    below and above a range, a non-integer, the wrong type, null.  An enum: an undeclared value, then a non-integer for
    a number enum or another case for a string one, the wrong type, null.  A string: the wrong type and null; and it may
    take a value its documented form refuses (`undeclared`: a url that is not http) or another case (strategy's `sas`)."""
    cls = iv_class(key)
    if cls == "number":
        return ("below", "above", "float", "wrongType", "explicitNull"), ()
    if cls == "enum":
        numeric = ts_wire(_state_leaf("$." + key)) == {"number"}
        return ("undeclared", "float" if numeric else "case", "wrongType", "explicitNull"), ()
    return ("wrongType", "explicitNull"), ("undeclared", "case")


def _iv_value(where, key, kind, v):
    """(Tier C, C4.4) One invalid.yaml value, held to what its kind means for its leaf."""
    leaf = _state_leaf("$." + key)
    # (Tier C, C4.4 review) An EMPTY {} or [] is a wrongType value — native §2.8's "other uncoercible inputs", R5 and R8
    # data; a container with anything in it is a question about walking into a leaf, not about its value.
    if isinstance(v, (list, dict)) and (v or kind != "wrongType"):
        raise SystemExit("%s: %r — a scalar (a `wrongType` may also be an empty {} or []): J sets one leaf to one value, "
                         "and a list or map with anything in it is a question about walking into a leaf, not about its "
                         "value" % (where, v))
    if isinstance(v, float) and not math.isfinite(v):
        raise SystemExit("%s: %r is not JSON — the driver's parse would throw on the whole extra" % (where, v))
    _transportable(where, v)
    if isinstance(v, str) and not RT_STRING.match(v):
        raise SystemExit("%s: %r — a string is printable ASCII, 1-64 characters, with no leading or trailing whitespace "
                         "(Android trims, SpecIndex.java:427)" % (where, v))
    if v is not None and not isinstance(v, (bool, int, float, str, list, dict)):
        raise SystemExit("%s: %r is not JSON — quote dates and timestamps" % (where, v))
    wire = ("null" if v is None else "boolean" if isinstance(v, bool) else "number" if isinstance(v, (int, float))
            else "object" if isinstance(v, dict) else "array" if isinstance(v, list) else "string")
    own, values = ts_wire(leaf), ts_values(leaf)
    whole = isinstance(v, int) and not isinstance(v, bool)
    why = None
    if kind in ("below", "above") and not whole:
        why = "an integer — a non-integer is `float`'s question, another type `wrongType`'s"
    elif kind == "float" and not isinstance(v, float):
        why = "a non-integer number (10.1)"
    elif kind == "undeclared" and (wire not in own or isinstance(v, float) or values is not None and v in values):
        why = "of the leaf's own type (%s) and none of its declared values%s" % (
            "|".join(sorted(own)), "" if values is None else " (%s)" % ", ".join(map(str, sorted(values, key=str))))
    elif kind == "case" and (wire != "string" or values is not None and (
            v in values or v.lower() not in {str(x).lower() for x in values})):
        why = "a declared value in another case"
    elif kind == "wrongType" and wire in own | {"null"}:
        why = "a value of another type than the leaf's (%s) — null is `explicitNull`'s" % "|".join(sorted(own))
    if why:
        raise SystemExit("%s: %r — a `%s` value is %s" % (where, v, kind, why))


def wo_status(wo):
    """(Tier C, C4.5) A work order's `status:` (workorders/WO-NNN-*.md's frontmatter), or None where there is no such
    file: what `pending:` is held to."""
    for path in glob.glob(os.path.join(os.path.dirname(LABS), "workorders", wo + "-*.md")):
        with open(path) as f:
            for line in f:
                m = re.match(r"^status:\s*([\w-]+)", line)
                if m:
                    return m.group(1)
    return None


def _iv_ruling(where, key, kind, item, loose, prime):
    """(Tier C, C4.5) One value as the table writes it -> the value itself.  Bare where its kind or leaf is `unasserted:`
    in writing (`loose`); otherwise {value, ruled[, pending]} or {value, unasserted}.  A ruling is the leaf's own type (an
    enum's declared value) or `os_default`, never the prime but for `case`; `pending:` names work orders that exist and
    are not done."""
    if not (isinstance(item, dict) and "value" in item):
        if not loose:
            raise SystemExit("%s: %r has no ruling — {value: …, ruled: …} (C4.5; with `pending: WO-NNN` until the work "
                             "order that fixes it merges), {value: …, unasserted: \"YYYY-MM-DD why\"}, or the kind "
                             "`unasserted:` in writing" % (where, item))
        return item
    if loose:
        raise SystemExit("%s: %r — the kind is `unasserted:` in writing, so its values are bare, with no ruling of their "
                         "own" % (where, item["value"]))
    _reject_unknown("%s %r" % (where, item["value"]), item, IV_VALUE_KEYS)
    if ("ruled" in item) == ("unasserted" in item) or "pending" in item and "ruled" not in item:
        raise SystemExit("%s %r: a value is {value, ruled[, pending]} or {value, unasserted}" % (where, item["value"]))
    if "unasserted" in item:
        if not DATED.match(str(item["unasserted"] or "")):
            raise SystemExit("%s %r: `unasserted:` is \"YYYY-MM-DD why\"" % (where, item["value"]))
        return item["value"]
    ruled, leaf = item["ruled"], _state_leaf("$." + key)
    if ruled != IV_OS_DEFAULT:
        wire = ("boolean" if isinstance(ruled, bool) else "number" if isinstance(ruled, (int, float)) else
                "string" if isinstance(ruled, str) else None)
        values = ts_values(leaf)
        if wire not in ts_wire(leaf) or values is not None and ruled not in values:
            raise SystemExit("%s %r: ruled %r — the leaf's own type (%s)%s, or `os_default` (R11)"
                             % (where, item["value"], ruled, "|".join(sorted(ts_wire(leaf))), "" if values is None else
                                ", one of %s" % ", ".join(map(str, sorted(values, key=str)))))
        if wire == "number" and not math.isfinite(ruled) or wire == "string" and not IV_RULED_STRING.match(ruled):
            raise SystemExit("%s %r: ruled %r — a finite number, or printable ASCII of at most 200 characters (what a ret "
                             "line carries whole)" % (where, item["value"], ruled))
        _transportable("%s %r ruled" % (where, item["value"]), ruled)
        # Where the ruling is the prime, a cell that kept the prime passes.  `case` alone may: its ruling is the declared
        # token whatever the prime, and a cell that stores the value as sent still fails (strategy's SAS).
        if kind != "case" and prime is not _ABSENT and wire == _rt_kind(prime) and (
                float(ruled) == float(prime) if wire == "number" else ruled == prime):
            raise SystemExit("%s %r: ruled %r is the prime roundtrip.yaml sets first — a cell that kept the prime would "
                             "pass" % (where, item["value"], ruled))
    if "pending" in item:
        wos = item["pending"] if isinstance(item["pending"], list) else [item["pending"]]
        if not wos or len(set(map(str, wos))) != len(wos) or not all(isinstance(w, str) and WO_ID.match(w) for w in wos):
            raise SystemExit("%s %r: `pending:` names work orders, WO-NNN or a list of them, each once"
                             % (where, item["value"]))
        for wo in wos:
            status = wo_status(wo)
            if status is None:
                raise SystemExit("%s %r: pending %s — there is no workorders/%s-*.md" % (where, item["value"], wo, wo))
            if status == "done":
                raise SystemExit("%s %r: pending %s, which is done — its values are asserted now: drop the `pending:`"
                                 % (where, item["value"], wo))
    return item["value"]


def iv_ruling(entry, kind, value):
    """(Tier C, C4.5) What the table rules for one value it asks: {"ruled": R, "pending": [WO-…]} or {"unasserted": why}.
    The implicit null's is `explicitNull:`'s; a bare value's is its kind's or its leaf's `unasserted:`."""
    unasserted = entry.get("unasserted")
    if isinstance(unasserted, str) or kind in (unasserted or {}):
        return {"unasserted": unasserted if isinstance(unasserted, str) else unasserted[kind]}
    items = [entry.get("explicitNull") or {}] if kind == "explicitNull" else [
        v for v in entry.get(kind) or [] if isinstance(v, dict) and "value" in v and json.dumps(v["value"]) == json.dumps(value)]
    item = items[0] if items else {}
    if "unasserted" in item:
        return {"unasserted": item["unasserted"]}
    pending = item.get("pending", [])
    return {"ruled": item.get("ruled"), "pending": pending if isinstance(pending, list) else [pending]}


def _iv_entry(key, entry, rt):
    """(Tier C, C4.4) One invalid.yaml entry, against the vocabulary and roundtrip.yaml (its header is the schema)."""
    where = "labs/harness/invalid.yaml %s" % key
    if key not in config_leaves():
        raise SystemExit("%s: not a Config leaf State carries (vocabulary.yaml `config:` minus `state.omit`) — a stale "
                         "entry the types renamed or dropped, a group, or a typo" % where)
    if iv_class(key) is None:
        raise SystemExit("%s: `%s` is none of the number, enum and string leaves J measures (C4 decision 13) — no entry"
                         % (where, _state_leaf("$." + key)))
    if not isinstance(entry, dict) or not entry:
        raise SystemExit("%s: an entry is {<kind>: [values…], …}, or {skip: \"YYYY-MM-DD why\"}" % where)
    if None in entry:
        raise SystemExit("%s: a `null:` key is YAML's null — the null kind is implicit (`explicitNull`: every leaf is set "
                         "to null), and a dated `skip: {explicitNull: …}` leaves it out" % where)
    _reject_unknown(where, entry, IV_ENTRY_KEYS)
    required, optional = iv_kinds(key)
    skip = entry.get("skip")
    if "skip" in entry and not isinstance(skip, dict):
        if set(entry) != {"skip"} or not DATED.match(str(skip or "")):
            raise SystemExit("%s: a whole-leaf skip is one dated line, `skip: \"YYYY-MM-DD why\"`, and nothing else" % where)
        return
    skip = skip or {}
    for kind, why in skip.items():
        if kind not in required + optional:
            raise SystemExit("%s: skips `%s`, which a %s leaf does not take (%s)"
                             % (where, kind, iv_class(key), ", ".join(required + optional)))
        if not DATED.match(str(why or "")):
            raise SystemExit("%s: skip.%s is \"YYYY-MM-DD why\" — a dated, diffable reason" % (where, kind))
        if kind in entry and kind != "explicitNull":
            raise SystemExit("%s: `%s` has values and a skip — one or the other" % (where, kind))
    # (Tier C, C4.5) What is never asserted between the OSes, in writing: the whole leaf, or a kind, whose values stay bare.
    unasserted = entry.get("unasserted")
    if isinstance(unasserted, dict) and unasserted:
        for kind, why in unasserted.items():
            if kind not in required + optional or kind in skip:
                raise SystemExit("%s: `unasserted:` names `%s`, which this %s leaf does not ask (%s)"
                                 % (where, kind, iv_class(key), ", ".join(k for k in required + optional if k not in skip)))
            if not DATED.match(str(why or "")):
                raise SystemExit("%s: unasserted.%s is \"YYYY-MM-DD why\" — a dated, diffable reason" % (where, kind))
    elif "unasserted" in entry and not DATED.match(str(unasserted or "")):
        raise SystemExit("%s: `unasserted:` is \"YYYY-MM-DD why\" for the whole leaf, or {<kind>: \"YYYY-MM-DD why\"}"
                         % where)
    loose = lambda kind: isinstance(unasserted, str) or kind in (unasserted or {})
    prime = (rt.get(key) or {}).get("value", _ABSENT)
    if "unprimed" in entry:
        prime = _ABSENT
    for kind in [k for k in IV_KINDS if k in entry and k != "explicitNull"]:
        if kind not in required + optional:
            raise SystemExit("%s: a %s leaf takes %s — not `%s`" % (where, iv_class(key), ", ".join(required + optional),
                                                                    kind))
        values = entry[kind]
        if not isinstance(values, list) or not values:
            raise SystemExit("%s: `%s` is a non-empty list of values" % (where, kind))
        plain = [_iv_ruling("%s %s" % (where, kind), key, kind, v, loose(kind), prime) for v in values]
        if len({json.dumps(v) for v in plain}) != len(plain):
            raise SystemExit("%s: `%s` names a value twice" % (where, kind))
        for v in plain:
            _iv_value("%s %s" % (where, kind), key, kind, v)
    missing = [k for k in required if k != "explicitNull" and k not in entry and k not in skip]
    if missing:
        raise SystemExit("%s: no values and no dated skip for %s — every kind a %s leaf takes is asked, or skipped in "
                         "writing" % (where, ", ".join(missing), iv_class(key)))
    # (Tier C, C4.5) The implicit null's ruling has a key of its own: the null itself is never written.
    asks_null = "explicitNull" not in skip and not loose("explicitNull")
    null = entry.get("explicitNull")
    if "explicitNull" in entry and (not asks_null or not isinstance(null, dict) or "value" in null or "ruled" not in null):
        raise SystemExit("%s: `explicitNull:` is the implicit null's ruling, {ruled: …[, pending: …]} — never the null "
                         "itself, and never beside a skip or an `unasserted:` of the kind" % where)
    if asks_null and "explicitNull" not in entry:
        raise SystemExit("%s: no ruling for the implicit null — `explicitNull: {ruled: …}` (C4.5), or the kind "
                         "`unasserted:` or skipped in writing" % where)
    if null is not None:
        _iv_ruling(where + " explicitNull", key, "explicitNull", dict(null, value=None), False, prime)
    if "unprimed" in entry and not DATED.match(str(entry["unprimed"] or "")):
        raise SystemExit("%s: `unprimed:` is \"YYYY-MM-DD why\" — the reason this leaf is set without a prime" % where)
    if "unprimed" not in entry:
        if prime is _ABSENT or isinstance(prime, (list, dict)):
            raise SystemExit("%s: roundtrip.yaml has no scalar value for %s to prime with — give `unprimed:` with its "
                             "dated reason" % (where, key))
        same = [v for k in IV_KINDS if k != "explicitNull" for v in iv_values(entry, k) if json.dumps(v) == json.dumps(prime)]
        if same:
            raise SystemExit("%s: %r is the prime roundtrip.yaml sets first — a cell that keeps it and one that stores it "
                             "would read alike" % (where, same[0]))
    couples = entry.get("couples", {})
    if not isinstance(couples, dict) or "couples" in entry and not couples:
        raise SystemExit("%s: `couples:` is {<leaf>: \"YYYY-MM-DD why\"} — the leaves a set moves by design" % where)
    for other, why in couples.items():
        if other not in config_leaves() or other == key or other.split(".")[0] != key.split(".")[0]:
            raise SystemExit("%s: couples %r — another Config leaf of the same top-level group, which the set's neighbour "
                             "check would otherwise hold" % (where, other))
        if not DATED.match(str(why or "")):
            raise SystemExit("%s: couples.%s is \"YYYY-MM-DD why\" — the documented coupling, dated" % (where, other))
    # (Tier C, C4.5) A set that moves its partner by design has no ruled reading on the generated plan: R22 asserts such a
    # leaf by hand (desiredAccuracy, a reset() before each value), so its table entry rules nothing.
    if couples and not isinstance(unasserted, str):
        raise SystemExit("%s: `couples:` and a ruling — the coupled leaf has no ruled reading on the generated plan; R22 "
                         "asserts such a leaf by hand, and its entry is `unasserted:` as a whole" % where)


def invalid_table():
    """(Tier C, C4.4) labs/harness/invalid.yaml, every entry validated at first use, and one per leaf J measures."""
    global _IV_TABLE
    if _IV_TABLE is None:
        table = _load(os.path.join(HARNESS, "invalid.yaml")) or {}
        if not isinstance(table, dict):
            raise SystemExit("labs/harness/invalid.yaml: a map of `<group>.<key>: {<kind>: [values…]}` entries")
        rt = roundtrip_table()
        for key, entry in table.items():
            _iv_entry(str(key), entry, rt)
        # Every leaf, as roundtrip.yaml's rule: a number, enum or string key the types gain is never left out in silence.
        missing = [k for k in config_leaves() if iv_class(k) and k not in table]
        if missing:
            raise SystemExit("labs/harness/invalid.yaml has no entry for %s — every number, enum and string Config leaf "
                             "has one: its values per kind, or a dated `skip:`" % ", ".join(missing))
        _IV_TABLE = table
    return _IV_TABLE


def iv_values(entry, kind):
    """(Tier C, C4.4) What one kind asks of a leaf: the entry's values, `[None]` for the implicit explicitNull, and
    nothing where the entry skips the kind or the whole leaf.  (Tier C, C4.5) The values themselves, their rulings
    (iv_ruling) left beside them."""
    skip = entry.get("skip")
    if isinstance(skip, str) or kind in (skip or {}):
        return []
    return [None] if kind == "explicitNull" else [v["value"] if isinstance(v, dict) and "value" in v else v
                                                  for v in entry.get(kind) or []]


def _invalid_values(where, iv, table):
    """(Tier C, C4.4) `invalid_values: {scope, groups, kinds, mode}` -> the step's hooks (labs/README.md §5).  C3's plan
    (_roundtrip), each set asking a value invalid.yaml gives for its kind, and recording what the cell stored:

        #0 stop, #1 reset                  $.enabled false
        #2 getState, the baseline          every in-scope leaf typed
        per (leaf, kind, value) — invalid.yaml's leaf order, IV_KINDS' kind order, the kind's value order:
          the prime: setConfig             roundtrip.yaml's value; the leaf {type, equals}
          the set:   setConfig             the value; the leaf `record: true`; the rest of its top-level group
                                           (geolocation includes filter) same: previous — except a `couples:` leaf,
                                           recorded (desiredAccuracy's filter.policy, Option B)
        getState, the read-back            every in-scope leaf same: previous
        reset, the restore; getState       every in-scope leaf same: #2

    The prime is what gives a reading its meaning.  After it, a core that keeps its value on an input it refuses reads
    the prime's value, one that takes the default reads the baseline's, a clamp the bound, a store the value sent; a set
    straight after reset() could not tell keep from default (native §2.5, N-2).  An `unprimed:` leaf is set from wherever
    the step left it.  A set whose value the types forbid carries a generated, dated `invalid:` (_validate_hooks
    refuses it anywhere else), and every set a host-only `sets:` and `kind:`.

    (Tier C, C4.5) `mode: assert` is the same plan over the values the table RULES and no open work order still owns
    (iv_ruling: `ruled:` without `pending:`), each set's leaf asserted instead of recorded — `{type, equals: <ruled>}`,
    or R11's `os_default` as `{type, same: getState#2}`, each cell against its own OS's baseline.  A pending or an
    unasserted value is never generated here (the SUITE steps stay green, and api-config-invalid keeps measuring it); a
    `couples:` leaf never rules (R22 asserts it by hand).  `mode: measure` sets every value, whatever its ruling."""
    if not isinstance(iv, dict):
        raise SystemExit("%s: `invalid_values` is {scope: both|android|ios, groups: [...], kinds: [...], "
                         "mode: measure|assert}" % where)
    _reject_unknown(where + " invalid_values", iv, INVALID_KEYS)
    scope, groups, kinds, mode = iv.get("scope"), iv.get("groups"), iv.get("kinds"), iv.get("mode")
    if mode not in IV_MODES:
        raise SystemExit("%s: invalid_values `mode` is measure (record what each cell stores, C4.4) or assert (the "
                         "rulings, C4.5) — not %r" % (where, mode))
    # (Tier C, C4.5) What a step asks: in measure, every value; in assert, the ruled values no open work order owns.
    asks = lambda k, kind: [v for v in iv_values(table[k], kind) if mode == "measure" or
                            iv_ruling(table[k], kind, v).get("ruled") is not None and
                            not iv_ruling(table[k], kind, v)["pending"]]
    if scope not in DECLARED:
        raise SystemExit("%s: invalid_values `scope` is both, android or ios — not %r" % (where, scope))
    if not isinstance(groups, list) or not groups or len(set(map(str, groups))) != len(groups):
        raise SystemExit("%s: invalid_values `groups` is a list of Config groups, each once" % where)
    if (not isinstance(kinds, list) or not kinds or len(set(map(str, kinds))) != len(kinds)
            or any(k not in IV_KINDS for k in kinds)):
        raise SystemExit("%s: invalid_values `kinds` is a list of %s, each once — not %r"
                         % (where, ", ".join(IV_KINDS), kinds))
    if len({IV_CLASS[k] for k in kinds}) > 1:
        raise SystemExit("%s: invalid_values `kinds` %s spans the kind classes %s — the range kinds, explicitNull and "
                         "wrongType each run in steps of their own (the C4 plan §2 C)"
                         % (where, kinds, ", ".join(sorted({IV_CLASS[k] for k in kinds}))))
    own = None if scope == "both" else scope
    keys = []
    for group in map(str, groups):
        if not isinstance(_state_leaf("$." + group), dict):
            raise SystemExit("%s: invalid_values group %r is not a Config group (app, app.notification, "
                             "geolocation.filter, …)" % (where, group))
        mine = [k for k in config_leaves() if k.rsplit(".", 1)[0] == group and state_scope("$." + k) == own and iv_class(k)]
        if not mine:
            raise SystemExit("%s: invalid_values group %s has no %s number, enum or string leaf of its own — a nested "
                             "group is named on its own (app.notification)" % (where, group, scope))
        for k in mine:
            if k not in table:
                raise SystemExit("%s: labs/harness/invalid.yaml has no entry for %s — give it values, or a dated `skip:`"
                                 % (where, k))
        if not any(asks(k, kind) for k in mine for kind in kinds):
            raise SystemExit("%s: invalid_values group %s asks nothing of kinds %s%s — drop it from this step"
                             % (where, group, ", ".join(kinds), " (every value is pending or unasserted)"
                                if mode == "assert" else ""))
        keys += mine
    sets = [(k, kind, v) for k in table if k in keys for kind in IV_KINDS if kind in kinds for v in asks(k, kind)]
    rt = roundtrip_table()
    tree = [k for k in config_leaves() if state_scope("$." + k) in (None, own)]
    top = lambda k: k.split(".")[0]
    wire = lambda k: "object" if state_path_wire("$." + k) is OPEN else "|".join(sorted(state_path_wire("$." + k)))

    def nest(k, v):
        for seg in reversed(k.split(".")):
            v = {seg: v}
        return v
    enabled = {"$.enabled": {"type": "boolean", "equals": False}}
    hooks = [{"api": "stop", "returns": enabled}, {"api": "reset", "returns": enabled},
             {"api": "getState", "returns": {"$." + k: {"type": wire(k)} for k in tree}}]
    primes = 0
    for k, kind, v in sets:
        entry = table[k]
        if "unprimed" not in entry:
            prime = rt[k]["value"]
            hooks.append({"api": "setConfig", "args": nest(k, prime), "sets": k,
                          "returns": {"$." + k: {"type": _rt_kind(prime), "equals": prime}}})
            primes += 1
        couples = entry.get("couples") or {}
        returns = {"$." + other: {"record": True} if other == k or other in couples else {"same": "previous"}
                   for other in tree if top(other) == top(k)}
        if mode == "assert":
            ruled = iv_ruling(entry, kind, v)["ruled"]
            returns["$." + k] = ({"type": wire(k), "same": RT_BASELINE} if ruled == IV_OS_DEFAULT else
                                 {"type": "number" if isinstance(ruled, (int, float)) else "string", "equals": ruled})
        hook = {"api": "setConfig", "args": nest(k, v), "sets": k, "kind": kind, "returns": returns}
        why = _validate_config("args", hook["args"], vocabulary().get("config") or {})
        if why:
            hook["invalid"] = "%s (Tier C, C4.4) invalid.yaml %s %s: %s" % (IV_SINCE, k, kind, why)
        hooks.append(hook)
    restore = {"$." + k: {"same": RT_BASELINE} for k in tree}
    hooks += [{"api": "getState", "returns": {"$." + k: {"same": "previous"} for k in tree}},
              {"api": "reset", "returns": restore}, {"api": "getState", "returns": restore}]
    hooks = json.loads(json.dumps(hooks))      # no mapping shared between hooks, or with the tables
    size = len(base64.b64encode(json.dumps([wire_hook(h) for h in hooks]).encode()))
    if where not in _IV_PRINTED:
        _IV_PRINTED.add(where)
        print("%s: invalid_values %d sets (%d primed), %d hooks, %d paths, hooksB64 %d B"
              % (where, len(sets), primes, len(hooks), sum(len(h["returns"]) for h in hooks), size), file=sys.stderr)
    if size > IV_STEP_LIMIT:
        raise SystemExit("%s: hooksB64 is %d B, over the %d B an invalid-value step may carry (IV_STEP_LIMIT) — split its "
                         "groups across steps" % (where, size, IV_STEP_LIMIT))
    return hooks


def scenario_exists(name):
    """(Tier C, C4.4) Whether labs/harness/scenarios/<name>.yaml is there, by its exact name — variant()'s rule: the lab
    Mac's filesystem ignores case, a capture's run.json does not.  summarize.py asks before it re-scores: a retired
    scenario's leftover capture (null-args', C4.3) is skipped, never a traceback that stops `--all`."""
    try:
        return str(name) + ".yaml" in os.listdir(os.path.join(HARNESS, "scenarios"))
    except OSError:
        return False


def scenario(name):
    try:
        spec = _load(os.path.join(HARNESS, "scenarios", name + ".yaml"))
    except FileNotFoundError:
        # (Tier C, C4.4) A refusal that names what is missing, not a traceback.
        raise SystemExit("scenario %s: there is no labs/harness/scenarios/%s.yaml — misspelt, or retired (a retired "
                         "scenario's leftover capture: summarize.py skips it)" % (name, name))
    spec.setdefault("name", name)
    if any("bootGcp" in block for block in [spec] + list(spec.get("steps") or [])):
        raise SystemExit("scenario %s: %s" % (name, BOOT_GCP_RETIRED))
    # A scenario may declare any step key at top level as the default for its steps.
    _reject_unknown("scenario %s" % name, spec, SCENARIO_KEYS | STEP_KEYS)
    if "roundtrip" in spec:
        raise SystemExit("scenario %s: `roundtrip:` is a step's own key — one set of groups per step, never "
                         "a default every step would inherit" % name)
    if "invalid_values" in spec:
        raise SystemExit("scenario %s: `invalid_values:` is a step's own key — one set of groups and kinds per step, "
                         "never a default every step would inherit" % name)
    # (Tier C, C5.0) What the host does keys on ids of the step's own hooks and events (_validate_moves).
    for key in ("moves", "actions"):
        if key in spec:
            raise SystemExit("scenario %s: `%s:` is a step's own key — it names that step's declarations" % (name, key))
    # (Tier C, C6.0) And a live process is one step's: as a default it would reach the first step, which has none.
    if "process" in spec:
        raise SystemExit("scenario %s: `process:` is a step's own key — only a later step can relaunch into a live "
                         "process" % name)
    # (WO-143) A scenario that names no platform is every platform's: applicability() then reads each manifest's
    # `supports`.  It was a list of the four bridges, so a fifth platform was silently left off.
    spec.setdefault("platforms", platforms())
    spec.setdefault("steps", [{"name": "run"}])
    # (Tier C, C4.0) A value its key's type does not admit refuses too: a step's config is the SDK's input on every
    # launch, never the form under test — that is a hook's, with a dated `invalid:`.
    why = _validate_config("scenario %s config" % name, spec.get("config"), vocabulary().get("config") or {})
    if why:
        raise SystemExit("%s — a step's config is the SDK's input, never the form under test" % why)
    # (Tier C, C5.2 review) A scenario-level `expect` reaches a single step only (below); on two steps it was dropped
    # silently, and neither step scored crashes.
    if "expect" in spec and len(spec["steps"]) > 1:
        raise SystemExit("scenario %s: a top-level `expect:` applies to a single-step scenario only — give each of its "
                         "%d steps its own (`crashes: 0` at least)" % (name, len(spec["steps"])))
    for i, step in enumerate(spec["steps"]):
        step.setdefault("name", "run" if len(spec["steps"]) == 1 else "step%d" % (i + 1))
        # (Tier C, C4.4) YAML reads `name: null` (or `no`, `1`) as another type; the capture directory and must_fail would
        # then name the step `None`.
        if not isinstance(step["name"], str) or not step["name"]:
            raise SystemExit("scenario %s: step %d's name is %r — a step name is a string (quote a YAML keyword)"
                             % (name, i, step["name"]))
        why = _validate_config("scenario %s step %s config" % (name, step.get("name")), step.get("config"),
                               vocabulary().get("config") or {})
        if why:
            raise SystemExit("%s — a step's config is the SDK's input, never the form under test" % why)
        step.setdefault("trigger", spec.get("trigger", "none"))
        step.setdefault("config", spec.get("config", {}))
        step.setdefault("revoke", spec.get("revoke", []))
        if "roundtrip" in step:
            # (Tier C, C3) The hooks are generated, so a written list would be a second source of them.
            # And every side-effect key is harmless only on a step that never starts tracking (roundtrip.yaml's
            # header): each listener is gated on `enabled`, a running service or a stationary region, or
            # only sets a property of the idle CLLocationManager.
            where = "scenario %s step %s" % (name, step["name"])
            if "hooks" in step or "hooks" in spec:
                raise SystemExit("%s: `roundtrip:` generates the step's hooks — drop its `hooks:`" % where)
            if step.get("noStart", spec.get("noStart")) is not True:
                raise SystemExit("%s: `roundtrip:` needs `noStart: true` — the side-effect keys it sets are "
                                 "safe because tracking never starts" % where)
            step["hooks"] = _roundtrip(where, step["roundtrip"], roundtrip_table())
        if "invalid_values" in step:
            # (Tier C, C4.4) The same two rules as `roundtrip:`, for the same reasons — and never both in one step.
            where = "scenario %s step %s" % (name, step["name"])
            if "roundtrip" in step:
                raise SystemExit("%s: `roundtrip:` and `invalid_values:` each generate the step's hooks — one per step"
                                 % where)
            if "hooks" in step or "hooks" in spec:
                raise SystemExit("%s: `invalid_values:` generates the step's hooks — drop its `hooks:`" % where)
            if step.get("noStart", spec.get("noStart")) is not True:
                raise SystemExit("%s: `invalid_values:` needs `noStart: true` — the side-effect keys it sets are "
                                 "safe because tracking never starts (roundtrip.yaml's header)" % where)
            step["hooks"] = _invalid_values(where, step["invalid_values"], invalid_table())
        step.setdefault("hooks", spec.get("hooks", []))
        step.setdefault("events", spec.get("events", []))
        step.setdefault("dialog", spec.get("dialog"))
        step.setdefault("prompt", spec.get("prompt", []))
        # (WO-027, permissions) What a step's permissions start from.  `grant`, the default, re-grants
        # everything but `revoke:` before the launch; `keep` leaves them as the previous step's user
        # answered — a real denial or a declined upgrade, carried into the next launch.  `fresh: true`
        # reinstalls first, as the first step always does: the only never-asked state after an answer.
        step.setdefault("permissions", spec.get("permissions", "grant"))
        step.setdefault("fresh", spec.get("fresh", False))
        # (Tier C, C8.0; decision 22 B) Google Location Accuracy, which run.py sets before every Android launch.
        step.setdefault("locationAccuracy", spec.get("locationAccuracy", True))
        # (WO-027) Which view the DEVICE renders.  (Tier C, C7.6) Derived when unset from what the
        # step declares: what arrived for `events:`, what each call answered for an `api` hook, and
        # otherwise the scenario's title alone.  The State table is retired.
        step.setdefault("view", spec.get("view") or ("events" if step["events"] else
                                                     "api" if any("api" in h for h in step["hooks"]) else "title"))
        step.setdefault("settle", spec.get("settle", 20))
        step.setdefault("trigger_repeat", spec.get("trigger_repeat", 1))
        step.setdefault("after", spec.get("after", 25))
        for key in ("stopOnTerminate", "watch", "noStart", "persistGcp"):
            if key in spec:
                step.setdefault(key, spec[key])
        step.setdefault("expect", spec.get("expect", {}) if len(spec["steps"]) == 1 else {})
        if "roundtrip" in step or "invalid_values" in step:   # (Tier C, C3) a later step inherits no `expect`
            step["expect"] = dict({"crashes": 0}, **step["expect"])
        step.setdefault("platform_expect", spec.get("platform_expect", {}))
        step.setdefault("extras", spec.get("extras", {}))
        step.setdefault("measure_from", spec.get("measure_from", "trigger"))
        _reject_unknown("scenario %s step %s" % (name, step["name"]), step, STEP_KEYS)
        # Only a DATED excuse counts: an events-only scenario skips the api-hook dated-reason check, and
        # `ios: null` would still run on iOS (applicability() returns the falsy reason).
        excused = {o for o, why in (spec.get("not_applicable") or {}).items()
                   if o in ("android", "ios") and re.match(r"\d{4}-\d{2}-\d{2}\b", str(why or ""))}
        _validate_hooks("scenario %s step %s" % (name, step["name"]), step, excused)
        _validate_events("scenario %s step %s" % (name, step["name"]), step, excused)
        _validate_moves("scenario %s step %s" % (name, step["name"]), step, excused)     # (Tier C, C5.0)
        _validate_lab_http("scenario %s step %s" % (name, step["name"]), step)            # (Tier C, C6.1)
        # (Tier C, C4.0) What the launch carries, sized here rather than found too big on a device: on Android
        # configB64 + hooksB64 + eventsB64 travel in ONE `adb shell am start` command (backends.Adb.launch), under
        # adb's one-request cap (ADB_COMMAND_LIMIT, MEASURED at the C4.4 pilot; C4.0 had INFERRED the device shell's
        # 128 KiB).  The largest step today is api-config-invalid/filter-range, 59,804 B (59,796 of them hooksB64).
        _transportable("scenario %s step %s config" % (name, step["name"]), step["config"])
        size = sum(map(len, launch_payloads(step).values()))
        if size > LAUNCH_LIMIT:
            raise SystemExit("scenario %s step %s: the launch carries %d B (configB64 + hooksB64 + eventsB64), over "
                             "the %d B a step may — split the step" % (name, step["name"], size, LAUNCH_LIMIT))
        # (WO-027, Tier B) A conformance step runs no per-tick fix (tick_gcp), so `persistGcp` there
        # would persist nothing.
        if "persistGcp" in step and not tick_gcp(step):
            raise SystemExit("scenario %s step %s: `persistGcp` on a conformance step does nothing — "
                             "its driver runs no per-tick getCurrentPosition" % (name, step["name"]))
        # (WO-027) `dialog:` answers a SYSTEM permission dialog while the step runs — the OS's
        # window, not the app's, so only the host can reach it.
        # (WO-027) `end: window` keeps a step's full settle + after even when it could end on its
        # evidence (EARLY_METRICS); the default lets the host end it a grace after the last evidence
        # (labkit.Grace) and a liveness tick.
        step.setdefault("end", spec.get("end", "evidence"))
        if step["end"] not in ("evidence", "window"):
            raise SystemExit("scenario %s step %s: `end` is `evidence` or `window`, not %r"
                             % (name, step["name"], step["end"]))
        if step["view"] == "state":
            raise SystemExit("scenario %s step %s: `view: state` was retired in Tier C C7.6 with the "
                             "State table — api, events or title" % (name, step["name"]))
        if step["view"] not in ("api", "events", "title"):
            raise SystemExit("scenario %s step %s: `view` is api, events or title, not %r"
                             % (name, step["name"], step["view"]))
        step["dialog"] = _dialog_plan("scenario %s step %s" % (name, step["name"]), step["dialog"])
        if step["permissions"] not in ("grant", "keep"):
            raise SystemExit("scenario %s step %s: `permissions` is grant or keep, not %r"
                             % (name, step["name"], step["permissions"]))
        if not isinstance(step["fresh"], bool):
            raise SystemExit("scenario %s step %s: `fresh` is true or false, not %r"
                             % (name, step["name"], step["fresh"]))
        # (WO-027, permissions) `keep` carries the answers an EARLIER step's user gave, untouched: so
        # there has to be an earlier step, no reinstall between, and no harness revoke or reset on
        # the way in.  On Android a keep after a reinstall would also launch with POST_NOTIFICATIONS
        # and ACTIVITY_RECOGNITION un-granted — nothing grants them.
        if step["permissions"] == "keep":
            if i == 0:
                raise SystemExit("scenario %s step %s: `permissions: keep` on the first step — it starts "
                                 "from a fresh install, so there is no answer to keep"
                                 % (name, step["name"]))
            if step["fresh"]:
                raise SystemExit("scenario %s step %s: `permissions: keep` with `fresh: true` — the "
                                 "reinstall wipes the answers keep would carry" % (name, step["name"]))
            for key in ("prompt", "revoke"):
                if step[key]:
                    raise SystemExit("scenario %s step %s: `permissions: keep` with a `%s:` — keep carries "
                                     "the user's answers untouched, and %s would replace them"
                                     % (name, step["name"], key, key))
        # (Tier C, C6.0; decision 15 A) `process: live` launches into the process the previous step's finish() left
        # running (the C6 headless analysis §3-§4): run.py skips the force-stop at this step's start and at the
        # previous step's end, and refuses (unscored) when the process is gone rather than cold-launching one.  So the
        # step needs an earlier step that FINISHED the app (`am start` would otherwise reach a live Activity's
        # onNewIntent and nothing boots), `permissions: keep` — whose own refusals, above, keep out the first step,
        # the reinstall and the revoke (`pm revoke` kills the process, backends.py Adb.permissions) — and a dated iOS
        # excusal: the Simulator host has no finish trigger and iOS no headless task.
        if "process" in step:
            where = "scenario %s step %s" % (name, step["name"])
            if step["process"] != "live":
                raise SystemExit("%s: `process` is `live` (or absent, the default force-stop), not %r"
                                 % (where, step["process"]))
            if step["permissions"] != "keep":
                raise SystemExit("%s: `process: live` needs `permissions: keep` — the step carries the previous one's "
                                 "process, and with it everything that process was granted; keep's own rules (above) "
                                 "then refuse the reinstall (`fresh:`) and the revoke that would kill it" % where)
            if spec["steps"][i - 1]["trigger"] != "finish":
                raise SystemExit("%s: `process: live` after a step whose trigger is %r, not finish — the Activity is "
                                 "still up, so the launch reaches its onNewIntent and nothing boots"
                                 % (where, spec["steps"][i - 1]["trigger"]))
            if "ios" not in excused:
                raise SystemExit("%s: `process: live` needs a dated `not_applicable: ios` — the Simulator host has "
                                 "no finish trigger, and iOS no headless task" % where)
        # (Tier C, C8.0) A boolean: YAML reads `off` and `on` unquoted as one, and a quoted "off" is a string that would
        # read as truthy, so it is refused.  Off is Google Play services' setting and Android's alone: a scenario that
        # turns it off excuses iOS in writing (the genesis analysis §2.3 a).
        if not isinstance(step["locationAccuracy"], bool):
            raise SystemExit("scenario %s step %s: `locationAccuracy` is true or false (or YAML's unquoted on/off), "
                             "not %r" % (name, step["name"], step["locationAccuracy"]))
        if step["locationAccuracy"] is False and "ios" not in excused:
            raise SystemExit("scenario %s step %s: `locationAccuracy: false` needs a dated `not_applicable: ios` — "
                             "Location Accuracy is a Google Play services setting, and iOS has none"
                             % (name, step["name"]))
        # (WO-045) `dialogAnswered: declared` expects the step's own `dialog:` sequence for the OS the
        # capture ran on — the windows a grant takes are a platform fact (Android's Always: the OS
        # dialog, the SDK's rationale, the upgrade, Back; iOS's: the prompt, then the upgrade).
        for block in [step["expect"]] + list((step["platform_expect"] or {}).values()):
            if (block or {}).get("dialogAnswered") == "declared" and not step["dialog"]:
                raise SystemExit("scenario %s step %s: `dialogAnswered: declared` needs a `dialog:` to "
                                 "declare the sequence" % (name, step["name"]))
        for block in [step["expect"]] + list((step["platform_expect"] or {}).values()):
            for implicit in ("apiAssertions", "eventAssertions", "recorded", "movesSent", "actionsDone"):
                if implicit in (block or {}):
                    raise SystemExit("scenario %s: %s is implicit and always `ok` — a "
                                     "scenario may not restate or relax it" % (name, implicit))
            # (WO-027, permissions) An expectation no metric computes used to raise only at score
            # time, after the device run — every step ran, then the run exited 2.
            unknown = sorted(set(block or {}) - set(METRICS))
            if unknown:
                raise SystemExit("scenario %s step %s: `expect` names %s, which no metric computes — "
                                 "known: %s" % (name, step["name"], ", ".join(unknown),
                                                ", ".join(sorted(METRICS))))
        _validate_windowed("scenario %s step %s" % (name, step["name"]), step, spec["platforms"])  # (C5.3 review)
    if any("api" in h for step in spec["steps"] for h in step["hooks"]):
        # (WO-027) Closed world: the only exit from a conformance scenario is a written one.
        reasons = spec.get("not_applicable") or {}
        silent = set(platforms()) - set(spec["platforms"]) - set(reasons)
        if silent:
            raise SystemExit("scenario %s has `api` hooks but does not account for %s — list it "
                             "under platforms, or under not_applicable with a dated reason"
                             % (name, ", ".join(sorted(silent))))
        # (Tier C, C5.1 pilot) applicability() consults a platform's excusal only when the platform is not listed, so one
        # both listed and excused runs — api-properties did, on both Flutter cells, reading `absent` x17.
        both = sorted(set(spec["platforms"]) & set(reasons))
        if both:
            raise SystemExit("scenario %s lists %s under platforms AND excuses it under not_applicable — an excused "
                             "platform is left out of platforms, or its excusal never applies" % (name, ", ".join(both)))
        unknown = sorted(set(reasons) - set(platforms()) - {"android", "ios"})
        if unknown:
            raise SystemExit("scenario %s: not_applicable names %s, which is neither a platform "
                             "nor an OS" % (name, ", ".join(unknown)))
        undated = sorted(p for p, why in reasons.items()
                         if not re.match(r"\d{4}-\d{2}-\d{2}\b", str(why or "")))
        if undated:
            raise SystemExit("scenario %s: the not_applicable reason for %s must open with its date "
                             "(YYYY-MM-DD) — an exemption is a dated, diffable line"
                             % (name, ", ".join(undated)))
        # The manifest gates (supports.triggers / supports.calls) report n/a and exit 0 before a
        # device is picked: mixed with api hooks, that is a second, unwritten exit — on EVERY OS
        # view the manifest offers, not just Android's.
        for plat in sorted(set(spec["platforms"]) & set(platforms())):
            raw = _load(os.path.join(LABS, "platforms", plat, "lab.yaml"))
            for os_name in ["android"] + [o for o in ("ios",) if raw.get(o)]:
                # (WO-027) …but a cell the scenario has ALREADY excused in writing is not a
                # silent exit — it is the exit this guard asks for.  Without this the OS axis
                # could not be used by a conformance scenario at all: `not_applicable: {ios: …}`
                # made applicability() answer, and the answer tripped the guard against it.
                if plat in reasons or os_name in reasons:
                    continue
                reason = applicability(spec, platform(plat, os_name))
                if reason:
                    raise SystemExit("scenario %s has `api` hooks but would be n/a on %s/%s (%s) — "
                                     "list it under not_applicable with a dated reason"
                                     % (name, plat, os_name, reason))
    return spec


def variant(name):
    spec = _load(os.path.join(HARNESS, "variants", name + ".yaml"))
    spec.setdefault("name", name)
    spec.setdefault("must_fail", [])
    # (Tier C, C3.1) An entry is `<scenario>`, every step of it inverted, or `<scenario>/<step>`, that
    # step alone (must_fail(), below).  Anything else would invert nothing, and the arm would read its
    # red step as a plain FAIL.
    if not isinstance(spec["must_fail"], list):
        raise SystemExit("variant %s: must_fail is a list of `<scenario>` or `<scenario>/<step>`" % name)
    for entry in spec["must_fail"]:
        if not isinstance(entry, str) or not re.fullmatch(r"[^/\s]+(/[^/\s]+)?", entry):
            raise SystemExit("variant %s: must_fail entry %r is neither `<scenario>` nor `<scenario>/<step>`"
                             % (name, entry))
        # ...and on a variant that can still run, it names a scenario and a step that exist.  Not on a
        # retired one: it is only re-scored, and what it names may have been renamed or re-stepped since.
        if spec.get("retired"):
            continue
        # The exact file name, not os.path.isfile: the lab Mac's filesystem ignores case, must_fail() does
        # not.  And the step names as written, with scenario()'s defaults, not scenario() itself: its
        # validation (roundtrip.yaml against vocabulary.yaml, …) is the scenario's to fail, not the variant's.
        scen_name, _, step_name = entry.partition("/")
        if scen_name + ".yaml" not in os.listdir(os.path.join(HARNESS, "scenarios")):
            raise SystemExit("variant %s: must_fail names %s, which is not a scenario" % (name, entry))
        if step_name:
            steps = _load(os.path.join(HARNESS, "scenarios", scen_name + ".yaml")).get("steps", [{}])
            if step_name not in [s.get("name", "run" if len(steps) == 1 else "step%d" % (i + 1))
                                 for i, s in enumerate(steps)]:
                raise SystemExit("variant %s: must_fail names %s, but %s has no step %s"
                                 % (name, entry, scen_name, step_name))
    # (Fresh clone) What its native label was built from, tracked: labs/m2 and labs/xcframeworks are gitignored, and
    # `./labkit setup` builds a label that is not there from these commits (lab_setup.py).  `ios_version` and
    # `ios_build` stand for a release's versioning bump, which build_native_ios.sh passes instead of editing.
    src = spec.get("native_source")
    if src is not None:
        if (not isinstance(src, dict) or not src.keys() & {"android", "ios"}
                or src.keys() - {"android", "ios", "ios_version", "ios_build"}):
            raise SystemExit("variant %s: native_source is {android: <commit>, ios: <commit>[, ios_version: X.Y.Z, "
                             "ios_build: N]}" % name)
        for side in ("android", "ios"):
            if side in src and not re.fullmatch(r"[0-9a-f]{40}", str(src[side])):
                raise SystemExit("variant %s: native_source.%s is a full 40-character commit, not %r — a short one "
                                 "can grow ambiguous" % (name, side, src[side]))
        if ("ios_version" in src) != ("ios_build" in src) or "ios_version" in src and (
                "ios" not in src or not isinstance(src["ios_version"], str)
                or not re.fullmatch(r"\d+\.\d+\.\d+", src["ios_version"]) or type(src["ios_build"]) is not int):
            raise SystemExit("variant %s: native_source's ios_version (X.Y.Z) and ios_build (an integer) come "
                             "together, beside an ios commit" % name)
    return spec


def must_fail(var, scenario_name, step_name):
    """(Tier C, C3.1) Whether the variant inverts this step's verdict: the step must break at least one
    expectation (report).  A must_fail entry `<scenario>` inverts every step of it, `<scenario>/<step>`
    that one step, and the scenario's other steps are judged normally."""
    return scenario_name in var["must_fail"] or "%s/%s" % (scenario_name, step_name) in var["must_fail"]


def platform(name, os_name="android"):
    """One merged view of a platform manifest.  (WO-027) The iOS half lives under `ios:` and
    overlays the Android top level — `package` becomes the bundle id, `build` the .app build,
    `supports` what the Simulator can express — so run.py never asks which OS it is on."""
    spec = _load(os.path.join(LABS, "platforms", name, "lab.yaml"))
    spec.setdefault("name", name)
    spec.setdefault("supports", {})
    if os_name != "android":
        half = spec.get(os_name)
        if not half:
            raise SystemExit("%s has no `%s:` block in its lab.yaml — no lab host for that OS"
                             % (name, os_name))
        # Nothing Android leaks through: a trigger or tick hook the Android half supports would
        # otherwise admit a scenario the Simulator cannot express.
        spec.pop("activity", None)
        spec.pop("web", None)
        spec["supports"] = {"triggers": ["none"], "calls": []}
        spec.update(half)
    # (Tier C, C2) Both OSes carry it: applicability() reads it, and an Android view without it
    # let `not_applicable: {android: …}` excuse nothing — api-state-ios ran on the 4 Android cells.
    spec["os"] = os_name
    return spec


def capture_dir(platform_name, scenario_name, variant_name, step_name, os_name="android"):
    # (WO-027) The OS axis: Android keeps its path (146 committed files), iOS gets a suffix.
    folder = platform_name if os_name == "android" else "%s-%s" % (platform_name, os_name)
    return os.path.join(LABS, "captures", folder, scenario_name, variant_name, step_name)


# ---------------------------------------------------------------------------------------
# applicability
# ---------------------------------------------------------------------------------------

def applicability(scen, plat):
    """None when the scenario applies, else the reason it does not (reported as n/a)."""
    reasons = scen.get("not_applicable", {})
    if plat["name"] not in scen["platforms"]:
        return reasons.get(plat["name"], "not listed in the scenario's platforms")
    # (WO-027) The same exit, on the OTHER axis.  A scenario can be specific to one OS — battery
    # saver has no Simulator equivalent, `headless` does not exist on iOS, and an issue reproduced
    # on Flutter/iOS is about one cell, not eight.  `not_applicable: {ios: "<date> — <why>"}`
    # excludes that OS for every bridge, and the dated-reason rule applies to it exactly as it
    # does to a bridge: an exemption stays a written, diffable line.
    if plat.get("os") in reasons:
        return reasons[plat["os"]]
    needed = {s["trigger"] for s in scen["steps"]} - {"none"}
    unsupported = needed - set(plat.get("supports", {}).get("triggers", []))
    if unsupported:
        return "%s does not support trigger(s) %s" % (plat["name"], ", ".join(sorted(unsupported)))
    for step in scen["steps"]:
        for hook in step["hooks"]:
            # (WO-027) An `api` hook never consults supports.calls: that list is consulted before
            # a device is even picked and exits 0, so a bridge could excuse itself from an
            # assertion by editing its own manifest.  An absent method is the verdict.
            if "api" in hook:
                continue
            if hook["call"] not in plat.get("supports", {}).get("calls", []):
                return "%s has no lab call %r" % (plat["name"], hook["call"])
    return None


# ---------------------------------------------------------------------------------------
# adb
# ---------------------------------------------------------------------------------------

DEVICE_TIMEOUT = 60      # seconds; any single device call — adb, simctl, idb


def device_call(argv, timeout=DEVICE_TIMEOUT, text=True):
    """(WO-027) Every device call ends: past `timeout` it RAISES, never returns partial output.
    On 2026-09-23 the lab emulator died mid-run and an unbounded `adb logcat -d` waited on it for
    80 minutes; a sweep stalled with nothing on screen to say why."""
    try:
        return subprocess.run(argv, capture_output=True, text=text, stdin=subprocess.DEVNULL,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SystemExit("`%s` did not return within %d s — the device may be dead or wedged; "
                         "nothing from this step is scored" % (" ".join(argv[:6]), timeout))


def adb(serial, *args, capture=True, check=False, timeout=DEVICE_TIMEOUT):
    r = device_call(["adb", "-s", serial, *args], timeout=timeout)
    if check and r.returncode != 0:
        raise SystemExit("adb %s failed: %s" % (" ".join(args), r.stderr))
    return r.stdout if capture else ""


def mark(serial, text):
    adb(serial, "shell", "log", "-t", TAG, "'point=%s'" % text)


# ---------------------------------------------------------------------------------------
# capture parsing
# ---------------------------------------------------------------------------------------

TS = re.compile(r"(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\.(\d\d\d)")
# `logcat -v time`: "09-18 08:12:31.004 I/LAB     ( 2461): point=onDestroy …"
LINE = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d\d\d\s+[VDIWEF]/([^(]+)\(\s*\d+\):\s?(.*)$")


def normalize(line):
    """Tag + message as one string, so a native `LAB` line and a JS console line that
    embeds `LAB app=…` are matched the same way."""
    m = LINE.match(line)
    if not m:
        return line
    tag, msg = m.group(1).strip(), m.group(2)
    return (TAG + " " + msg) if tag == TAG else msg


# The process a line came from: `I/LAB     ( 2461): …`, and the iOS tap is rendered the same way.
PID = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d\d\d\s+[VDIWEF]/[^(]+\(\s*(\d+)\):")


def line_pid(line):
    m = PID.match(line)
    return m.group(1) if m else None


def app_pids(lines):
    """(WO-027) The processes that ran THIS step's driver: the ones that logged `app=boot`.  Another
    lab app left running on the device writes into the same stream — on 2026-09-24 a lab app left on
    the Simulator for two hours ticked through all 14 flutter-ios board steps and decided when each
    one ended — and only its pid tells it apart.  (Every `app=` line in the 337 logs on disk carries
    a pid, and none but that app's came from a process without an `app=boot`.)"""
    return {line_pid(l) for l in lines if TAG + " app=boot " in normalize(l)} - {None}


def stamp_ms(line, year):
    """A log line's own stamp as epoch milliseconds (the host's timezone), or None: the clock a host mark's
    `tapped_ms` / `sent_ms` is compared with."""
    m = TS.match(line)
    if not m:
        return None
    mo, d, h, mi, s, ms = (int(x) for x in m.groups())
    import datetime
    return datetime.datetime(year, mo, d, h, mi, s, ms * 1000).timestamp() * 1000


def sent_on_device(raw, norm, year):
    """(Tier C, C5.0 review) When a host move or action began, on the DEVICE's clock, or None: the mark's own stamp less
    the host time between the send and the mark (`mark_ms` - `sent_ms`, both host clock).  The Lab emulators run 0.4 to
    2.6 s off the host (run.json `clock_offset_s`), so `sent_ms` is never compared with a device stamp itself.  A mark
    without `mark_ms` gives its own stamp, which is later than the send.

    (C5.3 review) Late by the mark's own transport time, 0.3 s on the Simulator: good for placing a cause in the
    summary's timeline (summarize.py), and not what an absence is counted from (_delivered_after)."""
    sent, mark, stamp = re.search(r"\bsent_ms=(\d+)", norm), re.search(r"\bmark_ms=(\d+)", norm), stamp_ms(raw, year)
    if not sent or stamp is None:
        return None
    return stamp - (int(mark.group(1)) - int(sent.group(1))) if mark else stamp


def move_anchors(step):
    """(Tier C, C5.3 review) The moves of a step that a declaration counts deliveries after (`none_after: move<i>`,
    `min_after: {ref: move<i>}`), as indices: the ones run.run_host marks before it sends (_delivered_after)."""
    anchors = [a for d in step.get("events") or [] for a in (d.get("none_after"), (d.get("min_after") or {}).get("ref"))]
    return {int(m.group(1)) for m in (MOVE_ID.match(a) for a in anchors if isinstance(a, str)) if m}


def _delivered_after(pairs, ident, anchor, year):
    """(Tier C, C5.0) How many deliveries of the declaration `ident` the log holds after `anchor`, or None while it
    holds no anchor.  `pairs` is the log so far as (raw, normalized) lines, in order.  The driver's own anchors are
    read in log order — its `event-removed` and `api id=` lines and its deliveries come from one process — and a
    move by its `move-begin` mark's stamp against each delivery's.

    (C5.3 review) The begin mark, not the mark that follows the send.  That one is stamped the mark's own transport
    time after the send returned, and sent_on_device takes off only the host time up to the call: on the Simulator a
    mark is stamped 284 to 299 ms after it is sent, and the one fix a watch is handed for a move came 120 to 160 ms
    before the anchor that made (events-remove-all, three iOS cells, 2026-10-05), so a watch that survived its removal
    read green.  A mark returns after its line is stamped, so the begin mark is never later than the send; it can be
    earlier by the mark's own time, and a delivery in that gap counts as after the move."""
    delivery = "%s app=event id=%s " % (TAG, ident)
    mv = MOVE_ID.match(anchor)
    if mv:
        began = next((stamp_ms(raw, year) for raw, n in pairs
                      if "%s point=move-begin i=%s " % (TAG, mv.group(1)) in n + " "), None)
        if began is None:
            return None
        return sum(1 for raw, n in pairs if delivery in n and (stamp_ms(raw, year) or 0) > began)
    needle = ("%s app=api id=%s " % (TAG, anchor) if REF_ID.match(anchor) else
              "%s app=event-removed id=%s " % (TAG, ident if anchor == "remove" else anchor))
    at = next((k for k, (_, n) in enumerate(pairs) if needle in n), None)
    return None if at is None else sum(1 for _, n in pairs[at + 1:] if delivery in n)


class Capture:
    """One capture directory, parsed. Everything is scoped to the trigger..capture window."""

    def __init__(self, path, year=None):
        self.path = path
        self.meta = {}
        meta_path = os.path.join(path, "run.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                self.meta = json.load(f)
        self.year = year or self.meta.get("year") or time.localtime().tm_year
        with open(os.path.join(path, "logcat.txt"), errors="replace") as f:
            self.lines = f.read().splitlines()
        self.norm = {l: normalize(l) for l in self.lines}
        # The window starts at the trigger, which is what almost every scenario means by "did
        # the SDK react to this?".  A step whose trigger is the LAST thing it does (queueing
        # records, then finishing the app) measures from the launch mark instead: measuring
        # from its trigger would score only the seconds after the app is gone.
        self.t_trigger = self._mark("point=" + self.meta.get("measure_from", "trigger"))
        self.t_capture = self._mark("point=capture", last=True)
        self.window = [l for l in self.lines
                       if self._ts(l) and self.t_trigger <= self._ts(l) <= self.t_capture]
        top = os.path.join(path, "top-activity.txt")
        self.top = open(top, errors="replace").read() if os.path.exists(top) else ""

    @classmethod
    def live(cls, lines, meta):
        """(WO-027) The log SO FAR, for the host's live read of a running step (run.py): no marks
        and no window, so only the whole-log readers (_api_items, _event_items) mean anything."""
        c = cls.__new__(cls)
        c.path, c.meta, c.year = "<live>", dict(meta), time.localtime().tm_year
        c.lines = list(lines)
        c.norm = {l: normalize(l) for l in c.lines}
        c.t_trigger = c.t_capture = None
        c.window, c.top = [], ""
        return c

    def _ts(self, line):
        return stamp_ms(line, self.year)

    def _mark(self, needle, last=False):
        hits = [self._ts(l) for l in self.lines if needle in l and self._ts(l)]
        if not hits:
            raise SystemExit("%s: no %r mark in logcat" % (self.path, needle))
        return hits[-1] if last else hits[0]

    # -- log access -----------------------------------------------------------------

    def app(self, suffix="", window=True):
        """`LAB app=…` lines (the driver). Substring-matched: hosts wrap console output."""
        src = self.window if window else self.lines
        return [self.norm[l] for l in src if (TAG + " app=" + suffix) in self.norm[l]]

    def point(self, name, window=True):
        src = self.window if window else self.lines
        return [self.norm[l] for l in src if (TAG + " point=" + name) in self.norm[l]]

    def contains(self, needle, window=True):
        src = self.window if window else self.lines
        return [l for l in src if needle in l]

    def field(self, line, key, default="?"):
        m = re.search(r"\b%s=(\S+)" % re.escape(key), line)
        return m.group(1) if m else default

    def last_state(self, key, default="?"):
        """The freshest probe value in the window: ticks first, else the post-destroy probe."""
        for line in (self.norm[l] for l in reversed(self.window)):
            if TAG + " app=tick" in line or TAG + " point=" in line:
                if re.search(r"\b%s=" % re.escape(key), line):
                    return self.field(line, key, default)
        return default

    # -- the SDK's own log ----------------------------------------------------------

    def sdk(self, like):
        """The SDK's own lines in the window matching `like` (an SQL LIKE pattern, `%substring%`): the product's
        reading (SDK_LOG, WO-191)."""
        return SDK_LOG(self, like)


def _geo_sdk_log(c, like):
    """background-geolocation's SDK log (WO-191: SDK_LOG's default, Capture.sdk's body until then): the pulled
    transistor_log.db on Android, the TSLocationManager-tagged lines on iOS."""
    if c.meta.get("os") == "ios":
        # (WO-027) The SDK mirrors every log line into os_log and the tap keeps them under
        # the TSLocationManager tag; the pulled database is flushed on a 60 s timer and is
        # empty for a short run.  `like` is always %substring%.
        needle = like.strip("%")
        return [c.norm[l] for l in c.window
                if "I/TSLocationManager(" in l and needle in c.norm[l]]
    db = os.path.join(c.path, "transistor_log.db")
    if not os.path.exists(db) or os.path.getsize(db) == 0:
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "select formatted_message from logging_event "
            "where timestmp between ? and ? and formatted_message like ? order by event_id",
            (c.t_trigger, c.t_capture, like)).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        con.close()
    return [r[0] for r in rows]


SDK_LOG = _geo_sdk_log                   # (WO-191) a product's register() replaces it


# ---------------------------------------------------------------------------------------
# metrics — the vocabulary a scenario's `expect:` may use
# ---------------------------------------------------------------------------------------

METRICS = {}


def metric(fn):
    METRICS[fn.__name__] = fn
    return fn


@metric
def headless(c):
    """Does the SDK believe the app is headless — i.e. that it was terminated?"""
    return c.last_state("headless")


@metric
def enabled(c):
    """Is the SDK still tracking, as the probe last reported it?"""
    return c.last_state("enabled")


@metric
def adapterIsCurrent(c):
    """Does the SDK hold the Activity the user is actually looking at?"""
    return c.last_state("adapterIsCurrent")


@metric
def emListeners(c):
    """EventManager listener count — a count that climbs per recreation is a leak."""
    return c.last_state("emListeners")


@metric
def readyEnabled(c):
    """`enabled` reported by a ready() that ran after the trigger ('-' when it did not)."""
    lines = c.app("ready ")
    return c.field(lines[-1], "enabled") if lines else "-"


@metric
def recreations(c):
    """Configuration-change recreations the SDK saw (its own log)."""
    return len(c.sdk("%Activity recreating%"))


@metric
def terminationPasses(c):
    """Times the SDK ran its termination path. A recreation must produce none."""
    return len(c.sdk("%MainActivity was destroyed%"))


@metric
def offMainGetWindow(c):
    """(Tier C, flutter#1715) Calls to the Activity's getWindow() off the main thread, as each lab
    app's MainActivity counts them and reports `LAB app=window offMain=<n>` 2 s after resume ('-'
    when it never reported). An SDK that touches the window off main races
    PhoneWindow.installDecor() against setContentView(): a black screen or a launch crash. The
    whole log, not the window: the report can land after the capture mark."""
    lines = c.app("window ", window=False)
    return max(int(c.field(l, "offMain", "0")) for l in lines) if lines else "-"


@metric
def mainConfigWaits(c):
    """(RN #2677) Times the main thread waited for TSConfig's class lock during the launch, as the React Native lab
    app's StartupWatch counts them (`startupProbe`) and reports `LAB point=configwatch waits=<n> …` when its watch
    ends ('-' when no watch ran). Bridgeless React Native builds the plugin's module, and with it TSConfig, on
    ReactHost's background thread (or BootReceiver builds it on the SDK's pool, as on the lab's launches, which
    follow a force-stop and so also deliver BOOT_COMPLETED); an onHostResume that reaches the SDK on main meanwhile
    waits there for as long as TSConfig's constructor runs: an ANR on a device where that is seconds. The whole
    log, not the window: the wait is at launch, before the trigger mark."""
    lines = c.point("configwatch ", window=False)
    return max(int(c.field(l, "waits", "0")) for l in lines) if lines else "-"


@metric
def mainConfigWaitMs(c):
    """(RN #2677) The longest of those waits in ms, a wait still going when the watch ended included (its
    `point=configwait` line reads `open=true`: a lock cycle, not a slow constructor); '-' when no watch ran."""
    lines = c.point("configwatch ", window=False)
    return max(int(c.field(l, "maxMs", "0")) for l in lines) if lines else "-"


@metric
def mainSdkWaitMs(c):
    """(RN #2677) The longest main-thread wait at any other SDK frame during the launch (`point=sdkwait`), from the
    watch's summary (`sdkMaxMs=`; 0 in a summary from before the field, '-' when no watch ran). Flutter's main thread
    meets BackgroundGeolocation's lock, not TSConfig's: its first call into the SDK is a stream listener's
    getInstance while the SDK's pool is inside that class's init(), itself waiting for TSConfig. Lock contention of a
    few ms is ordinary (3-8 ms, RN-2677's 44 launches), so a step bounds it rather than expecting none."""
    lines = c.point("configwatch ", window=False)
    return max(int(c.field(l, "sdkMaxMs", "0")) for l in lines) if lines else "-"


@metric
def alongsideMs(c):
    """(WO-107) How long a bridge held its other calls while an `alongside:` hook's call ran: the driver's
    `LAB app=alongside id=<id> api=<member> ms=<n>` lines, n from issuing the hook's call to the alongside call
    settling; the largest, the whole log ('-' when none).  RN #2668: thousands of polygons held them for seconds."""
    lines = c.app("alongside ", window=False)
    return max(int(c.field(l, "ms", "0")) for l in lines) if lines else "-"


@metric
def alongsideLeadMs(c):
    """(WO-107) By how many ms the `alongside:` call settled BEFORE the bridge built its last polygon: the SDK's last
    `[MiniBall] Minimum Enclosing Circle` line (TSGeofence.Builder, Android, one per polygon built, on whichever thread
    built it) less the driver's last `alongside` line; the whole log ('-' without both).  A bridge that builds on the
    thread its calls share cannot answer before that line — the second call is queued behind the build — so it reads
    0 or less however fast or loaded the device; one that hands the build to the SDK's pool answers first, by about the
    build's length.  An order, not a duration: alongsideMs alone also counts what no bridge change can remove
    (Flutter decodes the argument on the main thread).  MEASURED 2026-10-02, 4,000 polygons on Lab2, FIX / WO-107:
    Flutter -3 / +144, React Native -4 / +502."""
    # Raw lines, for their stamps: app() hands back the normalized text, the stamp stripped.
    beside = [l for l in c.lines if (TAG + " app=alongside ") in c.norm[l]]
    built = c.contains("Minimum Enclosing Circle", window=False)
    if not beside or not built or c._ts(built[-1]) is None or c._ts(beside[-1]) is None:
        return "-"
    return int(c._ts(built[-1]) - c._ts(beside[-1]))


@metric
def configChanges(c):
    """The Configuration bits Android reported changing, e.g. 10000000 = fontWeightAdjustment."""
    hits = c.contains("Config changes=")
    return re.search(r"Config changes=(\w+)", hits[0]).group(1) if hits else "-"


@metric
def isChangingConfigurations(c):
    """Activity.isChangingConfigurations() at onDestroy: true = recreation, not termination."""
    lines = c.point("onDestroy ")
    return c.field(lines[0], "isChangingConfigurations") if lines else "-"


@metric
def foregroundLocations(c):
    """Locations the app's own listener received (headless deliveries are not these)."""
    return len(c.app("event=location"))


@metric
def duplicateLocations(c):
    """Locations delivered more than once — duplicate SDK callback registrations. A bridge
    that re-registers after an Activity recreation without clearing shows this."""
    uuids = [c.field(l, "uuid") for l in c.app("event=location")]
    uuids = [u for u in uuids if u not in ("?", "undefined", "null")]
    return len(uuids) - len(set(uuids))


@metric
def watchLocations(c):
    """watchPosition deliveries the app received."""
    return len(c.app("event=watchposition"))


@metric
def headlessEvents(c):
    """Events delivered to the headless task — none of these while the app is on screen."""
    return len(c.point("headless"))


def _geo_headless_names():
    """(Tier C, C6.0) background-geolocation's: HeadlessEvent.name is `Event | 'locationerror'`
    (types/background-geolocation src/core/events/HeadlessEvent.ts:42), Event being the vocabulary's own enum."""
    return list(vocabulary()["enums"]["Event"]["values"]) + ["locationerror"]


HEADLESS_NAMES = _geo_headless_names     # (WO-191) a product's register() replaces it


def headless_names():
    """(Tier C, C6.0) The names a headless task can be handed: the product's (HEADLESS_NAMES, WO-191)."""
    return list(HEADLESS_NAMES())


def _headless_delivered(c):
    """Each `LAB point=headless event=<name>` line's name, in the window."""
    return [c.field(line, "event", "") for line in c.point("headless")]


def _headless_metrics():
    """(Tier C, C6.0) One `headlessEvents.<name>` and one `headlessFired.<name>` per headless name, generated so
    scenario()'s unknown-metric refusal and score() hold them like any other metric.  Delivered is the task's own
    line (the name matched exactly); fired is the SDK's `🛜 💀⚡️ <name>` line (HeadlessEventTx.java:55, debug level),
    `%` before and none after, so `location` never counts `locationfilter` or `locationerror`.  Apart they tell
    "never fired" from "fired but not delivered" — 510bbbf's signature on Flutter (the C6 headless analysis §3).
    (WO-191) Called by _load_product at the end of this module, after the product's register(): the names are its
    HEADLESS_NAMES, and a metric it registered under one of these names is its own, kept."""
    for name in headless_names():
        def delivered(c, name=name):
            return _headless_delivered(c).count(name)
        def fired(c, name=name):
            return len(c.sdk("%💀⚡️ " + name))
        delivered.__doc__ = "Headless `%s` events the task received (its `point=headless` line)." % name
        fired.__doc__ = "Headless `%s` events the SDK fired (its own log; Android)." % name
        METRICS.setdefault("headlessEvents." + name, delivered)    # (WO-191) a product's own is kept
        METRICS.setdefault("headlessFired." + name, fired)



@metric
def headlessTally(c):
    """(Tier C, C6.0) What the headless task received, `name=n,…` sorted by name ('-' for nothing): recorded, for the
    names a step does not assert."""
    names = _headless_delivered(c)
    return ",".join("%s=%d" % (n, names.count(n)) for n in sorted(set(names))) or "-"


@metric
def headlessCalls(c):
    """(Tier C, C6.1) What the headless task called on a fence's `labChangePace` extra, `<api>:<outcome>=n,…` sorted
    ('-' for none): its `point=task-api api=… outcome=…` lines (every lab app's task, C6.1)."""
    seen = ["%s:%s" % (c.field(l, "api"), c.field(l, "outcome")) for l in c.point("task-api ")]
    return ",".join("%s=%d" % (k, seen.count(k)) for k in sorted(set(seen))) or "-"


@metric
def headlessUnknown(c):
    """(Tier C, C6.0) Headless deliveries whose name is not a HeadlessEvent name — a typo in a driver, or a name the
    types do not declare."""
    known = set(headless_names())
    return sum(1 for n in _headless_delivered(c) if n not in known)


def _lab_http_requests(c):
    """(Tier C, C6.1) Each request the step's lab server answered, in the window: (route, status, records, token), from
    its `LAB point=server … path=/<route> status=<n> auth=<token> records=<n>` mark (run.py LabHttp)."""
    out = []
    for line in c.point("server "):
        status, records = c.field(line, "status", ""), c.field(line, "records", "0")
        out.append((c.field(line, "path", "?").lstrip("/") or "/", int(status) if status.isdigit() else status,
                    int(records) if records.isdigit() else 0, c.field(line, "auth", "-")))
    return out


@metric
def labHttp(c):
    """(Tier C, C6.1) What the step's lab server answered, `<route>:<status>=n,…` sorted ('-' for nothing): recorded,
    for the routes and statuses a step does not assert."""
    seen = ["%s:%s" % (route, status) for route, status, _, _ in _lab_http_requests(c)]
    return ",".join("%s=%d" % (k, seen.count(k)) for k in sorted(set(seen))) or "-"


@metric
def labHttpRecords(c):
    """(Tier C, C6.1) Location records the lab server accepted (200 on /locations): the `location` root's entries, one
    for an object, n for a batch."""
    return sum(records for route, status, records, _ in _lab_http_requests(c) if route == "locations" and status == 200)


@metric
def labHttpRefreshedUploads(c):
    """(Tier C, C6.1) Uploads the lab server accepted (200 on /locations) that carried a token /refresh issued — any
    but the scenario's own first one: the SDK's retry after its authorization refresh (HttpService.java:448-452)."""
    return sum(1 for route, status, _, token in _lab_http_requests(c)
               if route == "locations" and status == 200 and token not in ("-", LAB_HTTP_ACCESS % 1))


def _lab_http_since_kill(c, key):
    """(WO-114) `key` summed over the uploads the lab server accepted (200 on /locations) after a host `kill`: its
    marks carry `kills=<n> replayed=<n>` from the first kill on (run.py LabHttp), and neither before it."""
    return sum(int(c.field(l, key, "0")) for l in c.point("server ")
               if c.field(l, "path", "") == "/locations" and c.field(l, "status", "") == "200"
               and c.field(l, "kills", "0") != "0")


@metric
def labHttpReplayed(c):
    """(WO-114) Records the lab server accepted after a host `kill` that it had already accepted before that kill: the
    same `timestamp`, uploaded on both sides of a process death.  A restarted app that processes its old location
    deliveries again uploads each one again."""
    return _lab_http_since_kill(c, "replayed")


@metric
def labHttpRecordsAfterKill(c):
    """(WO-114) Records the lab server accepted after a host `kill`, repeats included: 0 means the app uploaded
    nothing once its process had died, so labHttpReplayed's 0 would prove nothing."""
    return _lab_http_since_kill(c, "records")


def _lab_http_metrics():
    """(Tier C, C6.1) One `labHttp.<route>.<status>` per route and status the lab server scripts (LAB_HTTP_ROUTES,
    LAB_HTTP_STATUSES), generated like the headless ones so scenario()'s unknown-metric refusal holds them."""
    for route in LAB_HTTP_ROUTES:
        for status in LAB_HTTP_STATUSES:
            def answered(c, route=route, status=status):
                return sum(1 for r, s, _, _ in _lab_http_requests(c) if r == route and s == status)
            answered.__doc__ = "Requests to the lab server's /%s it answered %d." % (route, status)
            METRICS["labHttp.%s.%d" % (route, status)] = answered


_lab_http_metrics()


@metric
def driverBoot(c):
    """(Tier C, C6.0) The `n=` of the driver's last `app=boot` line, the whole log ('-' when none): the boot counters
    are static per process, so 2 means the launch went into a live process, and '-' a main engine that never booted
    (Flutter's stuck splash)."""
    lines = c.app("boot ", window=False)
    return c.field(lines[-1], "n", "-") if lines else "-"


@metric
def backgroundPermissionRequests(c):
    """Times the SDK asked for background location. A config that did not change must ask none."""
    return len(c.sdk("%Requesting Background permission%"))


def _top_component(cap):
    """The RESUMED activity 10 s after the trigger — never the task history, which still
    lists activities from earlier runs."""
    for pattern in (r"topResumedActivity=ActivityRecord\{\S+ \S+ (\S+)",
                    r"mResumedActivity: ActivityRecord\{\S+ \S+ (\S+)",
                    r"mCurrentFocus=Window\{\S+ \S+ ([^}]+)\}"):
        m = re.search(pattern, cap.top)
        if m:
            return m.group(1)
    return "-"


@metric
def topActivity(c):
    """The resumed activity 10 s after the trigger."""
    return _top_component(c)


@metric
def permissionDialog(c):
    """A system permission dialog resumed 10 s after the trigger."""
    return "yes" if "GrantPermissionsActivity" in _top_component(c) else "no"


@metric
def slcTransitions(c):
    """useSignificantChangesOnly switches. A reset that changes nothing must produce none."""
    return len(c.sdk("%useSignificantChangesOnly →%"))


@metric
def sdkEnabledChange(c):
    """enabledchange events the SDK emitted — a spurious one means tracking was cycled."""
    return len(c.sdk("%enabledchange%"))


@metric
def watchStopped(c):
    """Times watchPosition was stopped (the recreation guard stops it deliberately)."""
    return len(c.sdk("%watchPosition: OFF%"))


@metric
def watchResults(c):
    """watchPosition location results the SDK produced — a zombie watch keeps producing them."""
    return len(c.sdk("%watchPosition LocationResult%"))


@metric
def httpFlushes(c):
    """Upload attempts. Queued records must still upload on a launch that only calls ready()."""
    return len(c.sdk("%HTTP Service (count%"))


@metric
def schedulerEnabled(c):
    """schedulerEnabled as the last ready() reported it — a later launch must not switch it off.

    The WHOLE log, unlike readyEnabled: the step that asserts this is a cold launch with
    `settle: 0` and no trigger at all, so its ready() IS the event under test, and whether it
    lands before or after the harness's own trigger mark is a race with `am start -W`.  The
    Cordova driver reports schedulerEnabled 153 ms BEFORE that mark, so windowed it read '-'
    for correct behaviour.  readyEnabled stays windowed on purpose: there, "a ready() that ran
    AFTER the trigger" is the whole claim."""
    lines = c.app("ready ", window=False)
    return c.field(lines[-1], "schedulerEnabled") if lines else "-"


@metric
def destroyedWebViewWarnings(c):
    """Capacitor/Cordova warnings that the SDK held a WebView belonging to a destroyed Activity."""
    return len(c.contains("destroyed WebView"))


@metric
def crashes(c):
    """FATAL EXCEPTION lines, and on iOS the crash reports the Simulator filed (`point=crash`).
    Every scenario expects 0, including the mutation variants.

    The WHOLE log, like labCalls and apiAssertions: an `api` scenario does all its work before
    the trigger mark (the first batch at the end of the driver's boot, settle 15 s), and both logs
    are already per-step — logcat is cleared at begin_log, the iOS tap is fresh and
    crash_reports() keeps only the reports filed after it — so windowing added no discrimination;
    it dropped a crash between api-done and the trigger.

    (WO-027) The APP's crashes, not every process in the log.  An Android FATAL EXCEPTION block
    names its process on the next line, `Process: <name>, PID: <pid>`, and the lab's own tools
    crash too: on Android 16 the dialog watcher's `uiautomator` died with `Bad file descriptor`
    and scored `permission-request-always` on Cordova `crashes 1`.  A shell tool's block carries a
    bare `PID:` and no `Process:`; so a block counts when its Process is the package or one of its
    `:sub` processes.  A capture that cannot name its platform still counts every block.

    (WO-027) And the two Android failures that leave NO FATAL EXCEPTION: an ANR (`ANR in
    <process>`, ActivityManager) and a native crash — Hermes, the Flutter engine, the WebView or
    sqlite.  Both counted only for the app's own processes, like the blocks.  A native crash is
    counted from crash_dump's header, `pid: P, tid: T, name: <thread>  >>> <process> <<<`, never from
    libc's `Fatal signal N … pid P (<name>)`: that name is /proc/self/comm, which Android fills with
    the LAST 15 characters of the process name (`ft.labs.cordova`), so it can never be the package.
    A foreground service that misses its deadline is not a third kind: Android crashes the app with
    a RemoteServiceException, which is a FATAL EXCEPTION."""
    try:
        pkg = platform(c.meta["platform"], c.meta.get("os", "android"))["package"]
    except (KeyError, SystemExit):
        pkg = None

    def ours(proc):
        return pkg is None or (proc is not None and (proc == pkg or proc.startswith(pkg + ":")))
    fatal = 0
    for i, line in enumerate(c.lines):
        if "FATAL EXCEPTION" in line:
            proc = next((re.search(r"Process: ([^,\s]+)", l) for l in c.lines[i + 1:i + 4]
                         if "Process: " in l), None)
            if pkg is None or (proc and ours(proc.group(1))):
                fatal += 1
            continue
        anr = re.search(r"\bANR in ([^\s(]+)", line)
        native = re.search(r"\bpid: \d+, tid: \d+, name: .*>>> ([^\s<]+) <<<", line)
        if (anr and ours(anr.group(1))) or (native and ours(native.group(1))):
            fatal += 1
    return fatal + len(c.point("crash ", window=False))


@metric
def configDropped(c):
    """Scenario config keys the driver did not carry into the config it built ('-' when none).

    Each driver echoes `LAB app=config json={…}` before ready().  A driver that names the
    keys it maps instead of merging them silently drops the rest: the Flutter driver dropped
    `app.schedule`, so the scheduler scenario armed nothing and read as an SDK defect."""
    echoed = c.app("config json=", window=False)
    wanted = c.meta.get("config") or {}
    if not wanted:
        return "-"
    if not echoed:
        return "no-echo"          # a driver that does not implement the echo yet
    try:
        built = json.loads(echoed[-1].split("config json=", 1)[1])
    except ValueError:
        return "unparsable"
    missing = []
    for group, values in wanted.items():
        if not isinstance(values, dict):
            if built.get(group) != values:
                missing.append(group)
            continue
        got = built.get(group)
        for key, value in values.items():
            if not isinstance(got, dict) or got.get(key) != value:
                missing.append("%s.%s" % (group, key))
    return ", ".join(missing) if missing else "-"


@metric
def labCalls(c):
    """Outcome of each legacy `call` hook (tick-counted `at:`): 'reset=ok, addGeofence=rejected'.

    The WHOLE log, like configDropped: a hook fires exactly once and logcat is cleared at the
    start of every step, so windowing adds no discrimination — it only makes the metric a race
    between the driver's tick clock and the trigger mark.  The hook scenarios settle for 12 s
    and fire at tick 3, i.e. ~12 s after ready(), so whether the hook lands inside the window is
    decided by how long the bridge took to boot: Capacitor, Flutter and React Native are slow
    enough to win it, and the Cordova driver logged `call=nullArgs outcome=ok` 14 ms too early."""
    out = []
    for line in c.app("call=", window=False):
        out.append("%s=%s" % (c.field(line, "call"), c.field(line, "outcome")))
    return ", ".join(out) if out else "-"


def _clip(text, limit=40):
    text = urllib.parse.quote(str(text), safe="|:.-_")
    return text if len(text) <= limit else text[:limit] + "~"


def _check(ret, checks, ref=None):
    """One reported path against its comparators (WO-027): None, or why it fails.

    `ret` is {"type", "value", "parent", "trunc"} as the driver reported it.  Every comparator
    lives here, on the host — never in a driver, where two implementations in two languages
    could each pass their own bad result.  (Tier C, C3) `ref(id)` is the ret that hook reported
    at the same path, for `same:`/`differs:` — None when it reported none."""
    kind, value = ret["type"], ret["value"]
    # (Tier C, C4.2) `record: true` judges nothing: what it read is the `recorded` row's, never a reason.
    if checks.get(RECORD):
        return None
    if checks.get("absent"):
        if kind != "missing":
            return "want-absent-got-%s" % kind
        # `$.0.sample` is vacuously "absent" from [] — the PARENT has to be there.
        return None if ret["parent"] else "missing"
    # (Tier C, C4.0) `optional: true`: a `?` leaf, or an empty array's `.0`, may be absent — `missing` with
    # its parent there.  Null is not absence: the comparator it qualifies judges a null.
    if kind == "missing" and checks.get("optional") and ret["parent"]:
        return None
    # (Tier C, C3) Alone, `same:` counts `missing` as one more type: a key the baseline already read
    # missing and still missing is unchanged — that absence is red once, where it is typed.  Never
    # `differs:`: a key that vanished differs from anything, so its absence would pass.  (Tier C, C4.0) "Alone"
    # ignores the qualifiers: an `under:` qualifies `same:`, never turns it into another comparator.
    if kind == "missing" and not set(checks) - set(QUALIFIERS) <= {"same"}:
        return "missing"
    if "type" in checks and kind not in str(checks["type"]).split("|"):
        return "want-%s-got-%s" % (checks["type"], kind)
    # (Tier C, C5.3; R's review, ruled A) A string the DRIVER clipped is judged by `contains` alone, and only to a
    # pass: its first characters already holding the needle means the whole does.  Not holding it says nothing of
    # the rest, no other comparator can be read off a prefix, and a line the transport cut is trusted for nothing.
    judged = set(checks) - {"type"} - set(QUALIFIERS)
    if ret["trunc"] and judged and not (judged == {"contains"} and not ret.get("cut")
                                        and str(checks["contains"]) in value):
        return "truncated"
    for op, want in checks.items():
        if op in ("type", "absent") + QUALIFIERS:
            continue
        if op == "equals":
            # Type-strict: "0" for 0 is exactly the drift this suite exists to find.
            want_kind = ("boolean" if isinstance(want, bool) else
                         "number" if isinstance(want, (int, float)) else
                         "null" if want is None else "string")
            if kind != want_kind:
                return "want-%s-got-%s" % (_clip(want), kind)
            if want_kind == "number":        # numerically: Dart prints 45.0 where JS prints 45
                try:
                    same = float(value) == float(want)
                except (TypeError, ValueError):
                    same = False
            elif want_kind == "boolean":
                same = value == str(want).lower()
            else:
                same = want is None or value == str(want)
            if not same:
                return "want-%s-got-%s" % (_clip(want), _clip(value))
        elif op == "in":
            # (Tier C, C5.1) One of the values, each compared exactly as `equals` compares it.
            if all(_check(ret, {"equals": v}) for v in want):
                return "want-in-%s-got-%s" % (_clip("|".join(map(str, want))), _clip(value) if kind in (
                    "number", "string", "boolean") else kind)
        elif op in ("gte", "lte", "gt", "lt"):
            if kind != "number":             # as strict as equals: float("45") would pass a string
                return "want-number-got-%s" % kind
            try:
                left, right = float(value), float(want)
            except (TypeError, ValueError):
                return "want-%s-%s-got-%s" % (op, _clip(want), kind)
            if not {"gte": left >= right, "lte": left <= right,
                    "gt": left > right, "lt": left < right}[op]:
                return "want-%s-%s-got-%s" % (op, _clip(want), _clip(value))
        elif op in REFS:
            other = ref(want) if ref else None
            if other is None:
                return "unreported-ref"
            if other["trunc"]:
                return "truncated"
            # The type, then a scalar's value — numerically, as `equals` does: Flutter prints 5 on one OS
            # and 5.0 on the other.  A container by its type alone: a driver reports no container's value.
            scalar = other["type"] == kind and kind in ("number", "boolean", "string")
            alike = other["type"] == kind and (not scalar or other["value"] == value)
            if scalar and kind == "number" and not alike:
                try:
                    alike = float(other["value"]) == float(value)
                except (TypeError, ValueError):
                    pass
            if alike != (op == "same"):
                return "want-%s-%s-got-%s" % ("same-as" if op == "same" else "differs-from", want,
                                              _clip(value) if scalar else kind)
        else:
            if kind != "string":
                return "want-string-got-%s" % kind
            hit = {"matches": lambda: re.search(str(want), value) is not None,
                   "startsWith": lambda: value.startswith(str(want)),
                   "contains": lambda: str(want) in value}[op]()
            if not hit:
                return "want-%s-%s-got-%s" % (op, _clip(want), _clip(value))
    return None


# (WO-027) WHEN a hook runs or a declaration arms is not WHAT it asks: a capture recorded with `at: 3`
# (ticks, before Tier B) answers the same question as the YAML's `at_s: 8`, and so does one taken
# before a schedule was retuned.  The spec comparisons below leave the schedule out.  (Tier C, C5.0) A declaration's
# `remove_at_s` is its schedule too.
SCHEDULE_KEYS = ("at", "at_s", "remove_at_s")


def _asked(entry):
    if not isinstance(entry, dict):
        return entry
    # (Tier C, C5.0 review) Whether a declaration removes is WHAT it asks (never-removed, removed-<outcome>); when is not.
    # (WO-143) `absent_on` is how a platform's answer is READ, not what is asked of any device: a capture made before
    # one was written answers the same question.
    return dict({k: v for k, v in entry.items() if k not in SCHEDULE_KEYS and k != "absent_on"},
                **({"remove_at_s": True} if "remove_at_s" in entry else {}))


@metric
def apiAssertions(c):
    """Every `api` hook's verdict: 'ok', or what failed — 'getCount#1.$=want-number-got-object'.

    Scored against the hook list in run.json — the spec as WRITTEN, before it was stripped for
    the wire — so a harness bug that drops a hook is itself a failure.  The WHOLE log, like
    labCalls: the first batch runs at the end of the driver's boot and every `at_s` slot a few
    seconds after it, long before the trigger mark.  Nothing here can return '-': a batch that
    never ran is `never-called`, not an absence of evidence."""
    hooks = [(i, h) for i, h in enumerate(c.meta.get("hooks") or []) if "api" in h]
    if not hooks:
        # Only a step whose YAML carries api hooks is ever asked, so none recorded means the
        # capture predates them: it is evidence about nothing, not a pass.
        return "no-hooks-recorded"
    if c.meta.get("scenario"):
        # Re-scoring: the comparators come from the capture, the decision to ask from the YAML.
        # When the YAML has moved on, the capture no longer answers it — re-run, do not re-read.
        steps = scenario(c.meta["scenario"])["steps"]
        now = next((s for s in steps if s["name"] == c.meta.get("step")), steps[0])["hooks"]
        if [_asked(h) for h in now if "api" in h] != [_asked(h) for _, h in hooks]:
            return "spec-changed-since-capture"
    items, calls = _api_items(c, hooks)
    failed = []
    done = c.app("api-done ", window=False)
    ran = c.field(done[-1], "n") if done else str(len(calls))
    if ran != str(len(hooks)):
        failed.append("batch-incomplete=%s/%d" % (ran, len(hooks)))
    for ident, why, paths in items:
        if why:
            failed.append("%s=%s" % (ident, why))
        else:
            failed += ["%s.%s=%s" % (ident, path, pwhy) for path, pwhy in paths if pwhy]
    return ", ".join(failed) if failed else "ok"


def _rets(c):
    """Every `ret` line, first per (id, path) — the reported values both assertion metrics read.  (Tier C, R4) A line
    that names its delivery (`n=`, from a declaration with `delivery:`) is kept under (id, path, n) instead, apart
    from every reading without one: _delivery picks a declaration's."""
    rets = {}
    for line in c.app("ret id=", window=False):
        value = c.field(line, "value", None)
        n = c.field(line, "n", None)
        key = (c.field(line, "id"), c.field(line, "path"))
        rets.setdefault(key if n is None else key + (n,), {
            "type": c.field(line, "type"),
            # `value=` with nothing after it is the empty string, not a missing value.
            "value": urllib.parse.unquote(value) if value is not None else "",
            "parent": c.field(line, "parent", "0") == "1",
            # `parent=` closes every ret line: without it the transport cut the line, and the
            # value (and any trunc=1 behind it) cannot be trusted.
            "trunc": c.field(line, "trunc", "0") == "1" or c.field(line, "parent", None) is None,
            # (Tier C, C5.3) apart from a driver's own clip: _check passes a `contains` on a clipped value, never here
            "cut": c.field(line, "parent", None) is None})
    return rets


def min_deliveries(decl):
    """(Tier C, R4) How many deliveries an `events:` declaration needs: its `min` (1 by default), and its `delivery`
    where that is later.  One rule for the scorer (_event_items) and the end rule (evidence_state): a step whose
    declaration reads its `returns` from the second delivery must not end on the first (R's review)."""
    return max(int(decl.get("min", 1)), int(decl.get("delivery", 0)))


def _delivery(rets, decl, ident):
    """(Tier C, R4) The readings an `events:` declaration's paths are held to, keyed (id, path) as _rets keys them:
    its `delivery`'s own lines where it names one, and otherwise every line that names none."""
    if "delivery" not in decl:
        return rets
    n = str(decl["delivery"])
    # Of its own lines, only an `$err` one names no delivery.  (R's review) Any other untagged line of this id
    # is a driver from before the rebuild reporting the FIRST delivery: not this declaration's reading, so the
    # path reads `unreported` instead of passing, or failing, on another delivery's value.
    mine = {k: r for k, r in rets.items() if len(k) == 2 and (k[0] != ident or str(k[1]).startswith("$err"))}
    mine.update({k[:2]: r for k, r in rets.items() if len(k) == 3 and k[0] == ident and k[2] == n})
    return mine


def _excused(rets, ident, returns, path):
    """(Tier C, C4.0) Whether `path` is excused by its `under:` container: that container reported, and read
    `missing` with its parent there (an absent `?` object, an empty array's `.0`) or, present, failed its own
    check (null, not an object) — or is itself excused.  One defect reads once, on the container's own path; a
    container that reported nothing excuses nothing.  A container read `missing` with its parent ALSO gone
    (parent=0) excuses nothing either, unless that parent excuses it in turn: a required object is missing
    (the C4 plan §2 T), which every leaf below it reads — api-types-control's pattern, where Flutter hands
    onMotionChange a bare Location and every generated path but `$` reads `missing`."""
    under = (returns.get(path) or {}).get("under")
    box = rets.get((ident, under)) if under is not None else None
    if box is None:
        return False
    return (box["parent"] if box["type"] == "missing" else
            _check(box, returns.get(under) or {}, lambda ref: rets.get((ref, under))) is not None) \
        or _excused(rets, ident, returns, under)


def _api_items(c, hooks):
    """(WO-027) Per api hook, in order: (id, why, [(path, why), …]) — `why` None where it holds.
    The ONE evaluation apiAssertions scores and the device view is shown (verdict_view): the
    screen cannot disagree with the capture because there is nothing else for it to agree with."""
    calls, rets = {}, _rets(c)
    for line in c.app("api id=", window=False):
        calls.setdefault(c.field(line, "id"), c.field(line, "outcome"))
    out = []
    for i, hook in hooks:
        ident = "%s#%d" % (hook["api"], i)
        outcome = calls.get(ident)
        # (Tier C, C4.0) `outcome:` names the side: `resolved`/`rejected` fail the other one as
        # want-<it>-got-<other>, and `settled` passes both, its paths recorded, never judged.  No `api id=` line
        # at all is `unsettled` — the driver logs the line after the await, which has no timeout, so a promise
        # that never settles leaves none — and fails every outcome.  So do `absent` (the bridge lacks the
        # member: nothing was called, as a hook without `outcome` scores it) and any outcome of the driver's
        # own (`unexpressible`, Flutter's refusal to marshal the argument).  Without `outcome`, as before.
        # (Tier C, R5) A call that was made took one of three sides (SIDES): it resolved, it rejected, or it threw
        # as it was called.  `outcome:` names the one expected; without it, `$err` paths expect a rejection, as
        # before R, and `$` paths a resolution.  A throw where a rejection was expected is its own red.
        want = str(hook.get("outcome", "")).split(" ")[0]
        returns = hook.get("returns") or {}
        expects = want if want in SIDES.values() else \
            "rejected" if any(str(p).startswith("$err") for p in returns) else "resolved"
        # (WO-143) `absent_on: {<platform>: "YYYY-MM-DD <why>"}`: a member that platform's API does not have, by
        # decision.  There `absent` holds and anything else is red; every other platform is read as before.
        if c.meta.get("platform") in (hook.get("absent_on") or {}):
            out.append((ident, None if outcome == "absent" else "want-absent-got-%s" % (outcome or "never-called"),
                        []))
            continue
        if outcome is None:
            out.append((ident, "unsettled" if want else "never-called", []))
        elif outcome == "absent":
            out.append((ident, "absent", []))
        elif outcome not in SIDES:
            out.append((ident, _clip(outcome), []))
        elif want == "settled":
            out.append((ident, None, []))
        elif SIDES[outcome] != expects:
            out.append((ident, "want-%s-got-%s" % (want, SIDES[outcome]) if want else SIDES[outcome], []))
        else:
            paths = []
            for path, checks in returns.items():
                ret = rets.get((ident, str(path)))
                # (Tier C, C4.2) A recorded path is never red, reported or not: `recorded` says what it read.
                paths.append((path, None if checks.get(RECORD) or _excused(rets, ident, returns, path) else
                               "unreported" if ret is None else
                               _check(ret, checks, lambda ref: rets.get((ref, str(path))))))
            out.append((ident, None, paths))
    return out, calls


@metric
def recorded(c):
    """(Tier C, C4.2) What each `record: true` path read, in hook order — 'getState#6.$.authorization.accessToken=
    string:abc' — for the reader of summary.txt, never a verdict: its expectation is `any` (IMPLICIT_RECORD), so the
    row is always ok.  A scalar reads `<type>:<value>` (%-encoded and clipped as a failure reason is; `~` marks a
    value clipped here, by the driver, or cut in transport), anything else its type alone — `missing`, `null`,
    `object` — and a path the driver never reported `unreported`, whatever became of its call: judging the call
    is apiAssertions'.  The WHOLE log, like apiAssertions; '-' when no hook records a path.

    (Tier C, C4.3) And each `outcome: settled` hook's reading, in the same hook order — the value the C4 plan §2 E
    says `settled` records: `getGeofence#2.$=null` where the call resolved, `getGeofence#2.$err=string:404` where it
    rejected, then the rejection's `$err.code` and `$err.message` where it has them (SETTLED_PATHS; one that read
    `missing` or `undefined` — a string has neither, a TypeError no code, a CapacitorException an undefined one — is
    left out), and `getGeofence#2=unreported` where the driver reported none of them: a call that never settled, a
    member it lacks, an argument it refused.  The root keeps any reading, `undefined` too (Cordova's wrapper)."""
    rets, out = _rets(c), []
    threw = {c.field(line, "id"): c.field(line, "outcome") for line in reversed(c.app("api id=", window=False))}

    def reading(ret):
        got = ("unreported" if ret is None else "%s:%s" % (ret["type"], _clip(ret["value"]))
               if ret["type"] in ("string", "number", "boolean") else ret["type"])
        return got + ("~" if ret and ret["trunc"] and not got.endswith("~") else "")

    for i, hook in enumerate(c.meta.get("hooks") or []):
        ident = "%s#%d" % (hook.get("api"), i)
        if _settled(hook):
            got = [(path, rets[(ident, path)]) for path in SETTLED_PATHS if (ident, path) in rets]
            # (Tier C, R5) A call that threw reads `$err` as a rejection does: say which it was.
            out += ["%s=threw" % ident] if threw.get(ident) == "threw" else []
            out += ["%s.%s=%s" % (ident, path, reading(ret)) for path, ret in got
                    if path in ("$", "$err") or ret["type"] not in ("missing", "undefined")] or ["%s=unreported" % ident]
            continue
        for path, checks in (hook.get("returns") or {}).items() if "api" in hook else ():
            if not (checks or {}).get(RECORD):
                continue
            out.append("%s.%s=%s" % (ident, path, reading(rets.get((ident, str(path))))))
    # (Tier C, C5.0) Decision 25 A: an `events:` declaration that records a path or is `arm_only` adds how many times it
    # was delivered — `onAuthorization@4.delivered=0` — and, beside a `none_after` / `min_after` anchor, how many after
    # it (`onLocation@2.after-removeListeners#3=1`), then each recorded path as a hook's, from the first delivery.
    decls = [(i, d) for i, d in enumerate(c.meta.get("events") or []) if _records(d)]
    if decls:
        delivered, pairs = {}, [(l, c.norm[l]) for l in c.lines]
        for line in c.app("event id=", window=False):
            try:
                delivered[c.field(line, "id")] = max(delivered.get(c.field(line, "id"), 0), int(c.field(line, "n", "0")))
            except ValueError:
                pass
    for i, decl in decls:
        ident = "%s@%d" % (decl["subscribe"], i)
        out.append("%s.delivered=%d" % (ident, delivered.get(ident, 0)))
        anchor = decl.get("none_after") or (decl.get("min_after") or {}).get("ref")
        if anchor:
            k = _delivered_after(pairs, ident, anchor, c.year)
            out.append("%s.after-%s=%s" % (ident, anchor, "no-anchor" if k is None else k))
        # (Tier C, R6) With `errors: true`, how many times the failure callback was called.
        if decl.get("errors"):
            out.append("%s.errors=%d" % (ident, max([0] + [int(c.field(line, "n", "0")) for line in
                                                           c.app("event-error id=%s " % ident, window=False)
                                                           if c.field(line, "n", "0").isdigit()])))
        mine = _delivery(rets, decl, ident)                  # (Tier C, R4) its `delivery`'s readings
        out += ["%s.%s=%s" % (ident, path, reading(mine.get((ident, str(path)))))
                for path, checks in (decl.get("returns") or {}).items() if (checks or {}).get(RECORD)]
    return ", ".join(out) if out else "-"


def _records(decl):
    """(Tier C, C5.0) Whether an `events:` declaration feeds the `recorded` row: `arm_only`, or a `record: true` path.
    (Tier C, R6) Or `errors: true`, whose error count is a reading."""
    return "arm_only" in decl or bool(decl.get("errors")) or \
        any((checks or {}).get(RECORD) for checks in (decl.get("returns") or {}).values())


@metric
def dialogAnswered(c):
    """How a system dialog was answered — 'allow', 'deny', or '-' for no dialog at all.

    (WO-027) A permission dialog belongs to the OS, not to the app: the driver cannot see it and
    the SDK cannot report it, so the HOST answers it and marks the answer into the device's own
    log.  The metric reports the ACTION, because the button's wording is the platform's ("While
    using the app" on Android, "Allow While Using App" on iOS) and an expectation naming one would
    be an expectation about Android.  The mark carries the exact label it tapped — the capture
    therefore records which button, while the assertion stays portable."""
    # The WHOLE log: the dialog is answered while the app settles, long before the trigger mark,
    # so the default window would never see it — the same reason apiAssertions reads whole.
    # (WO-027, permissions) ANSWERS only: the observer's `point=dialog seen=` and the watcher's
    # `point=dialog missed=` share the prefix, and either would join the sequence as '-'.
    answered = c.point("dialog action=", window=False)
    # (WO-027) Every answer, in order — `allow,rationale,always,back` for an Always grant. A
    # sequence that stops early is the failure this metric exists to name, and reporting only the
    # last action would report the stall as whatever it managed to answer.
    return ",".join(c.field(line, "action", "-") for line in answered) if answered else "-"


def _host_items(c, key):
    """(Tier C, C5.0) movesSent's and actionsDone's reading: what the capture's `moves` / `actions` (run.json, as
    resolved) asked, against the host's marks — the WHOLE log, like dialogAnswered, since a move can come before the
    trigger mark."""
    items = c.meta.get(key)
    if not items:
        return "no-%s-recorded" % key          # only a step that declares them is asked: a capture from before
    if c.meta.get("scenario"):
        steps = scenario(c.meta["scenario"])["steps"]
        now = next((s for s in steps if s["name"] == c.meta.get("step")), steps[0]).get(key) or []
        if now != items:
            return "spec-changed-since-capture"
    norm = [c.norm[l] for l in c.lines]
    step = {"moves": c.meta.get("moves") or [], "actions": c.meta.get("actions") or []}
    failed = []
    for i, item in enumerate(items):
        if key == "moves":
            n = item.get("hops", 1)
            sent = [int(m.group(1)) for m in (re.search(r"%s point=move i=%d hop=(\d+)/" % (TAG, i), x) for x in norm) if m]
            if len(sent) >= n:
                # (C5.0 review) and watched for its `observe_s`: a window that ended first leaves an absence it bounds
                # under-observed, which would otherwise read green.  Both marks are the host's, on the device's clock.
                last = next((c._ts(l) for l in reversed(c.lines)
                             if "%s point=move i=%d hop=%d/%d " % (TAG, i, n, n) in c.norm[l]), None)
                if item.get("observe_s") and c.t_capture and last and c.t_capture < last + 1000 * item["observe_s"]:
                    failed.append("move%d=observed-%gs/%gs" % (i, round((c.t_capture - last) / 1000, 1), item["observe_s"]))
                continue
            if sent:
                failed.append("move%d=hop%d/%d" % (i, max(sent), n))
                continue
        else:
            done = next((x for x in norm if "%s point=host i=%d action=" % (TAG, i) in x), None)
            # (C5.3 review) An action the host tried and could not do, in its own mark: a notification button that
            # was never in the shade.  Scored, where a SystemExit left the step unscored with no log to read.
            if done and " missed=" in done:
                failed.append("action%d=missed(%s)" % (i, c.field(done, "missed", "?")))
            if done:
                continue
        cond = after_unmet(item["after"], norm, step)
        what = "unsent" if key == "moves" else "undone"
        failed.append("%s%d=%s" % (key[:-1], i, "%s(%s:%s)" % ((what,) + next(
            (k, cond[k]) for k in AFTER_KINDS if k in cond)) if cond else what))     # ids, checked at load: no clip
    return ", ".join(failed) if failed else "ok"


@metric
def movesSent(c):
    """(Tier C, C5.0) Did the host send every move a step declares, every hop: 'ok', or `move<i>=unsent(<the first
    condition the log never showed>)`, `move<i>=hop<k>/<n>` (sent k of n).  A move whose condition never came is a
    lab-visible red of its own, not a misleading `onGeofence@1=never-fired` alone.  Implied wherever moves are."""
    return _host_items(c, "moves")


@metric
def actionsDone(c):
    """(Tier C, C5.0) movesSent's sibling for `actions:`: 'ok', or `action<i>=undone(<condition>)`; (C5.3 review)
    `action<i>=missed(<why>)` for one the host tried and could not do (a notification button not in the shade)."""
    return _host_items(c, "actions")


@metric
def deferredExitDropped(c):
    """(Tier C, C5.0) Android geofence EXITs the SDK deferred for a confirming fix and then dropped, with no onGeofence
    and no onLocationFilter (the moves analysis' F1): its warnings "Ignoring deferred geofence transition after confirm"
    and "…; ignoring deferred transitions" (GeofencingProcessor.java:478, :496, :522 at native/android cd438d8).  Only
    an EXIT is ever deferred (GeofenceTriggerGate).  The SDK log, windowed from the step's `measure_from` mark to the
    capture; iOS has no such gate and reads 0.  (C5.2) In EARLY_METRICS: the step's move holds the window's END open
    `observe_s` after the jump (Play Services' EXIT latency plus the confirm, usually immediate, at most its 5 s
    timeout).  Its START is the trigger mark, launch + settle, unless the step measures from launch: a jump sent before
    the trigger mark can drop its EXIT before the window opens (geofence-exit-far-android; the C5.2 review)."""
    return len(c.sdk("%gnoring deferred%"))


@metric
def undeclaredDialogs(c):
    """(WO-027, permissions) Windows that appeared and no declared action answered: the host's
    `point=dialog seen=<buttons>` marks, one per NEW window its observer found (run.py) — polling
    once the declared sequence is done, or from the launch when there is none, until the step ends.

    An ABSENCE assertion, and the only one that gives a `[]` sequence or a step with no `dialog:` a
    meaning: without it, "already granted, no window" is proven only by a promise settling, and a
    window that blocks nothing (the SDK's Settings alert) is invisible.  A window is recognised by
    its buttons, never by the app's name.  The WHOLE log, like dialogAnswered; the observer is
    stopped before the capture mark, so nothing lands after it."""
    return len(c.point("dialog seen=", window=False))


@metric
def eventAssertions(c):
    """Every `events:` declaration's verdict: 'ok', or what failed — 'onLocation=never-fired',
    'onMotionChange.$.isMoving=missing'.

    The sibling of apiAssertions for the 15 `on*` members (WO-027), and deliberately NOT part of
    it: an event is caused by one call and delivered later, zero or many times, to a callback the
    scenario never wrote.  What the device is told is which member to subscribe to and which paths
    to report; `min` and every comparator stay here.

    The WHOLE log, like apiAssertions: the subscriptions are installed at boot and the first
    deliveries land during `settle`, both long before the trigger mark.  `c.app("event id=")` also
    keeps the headless task's `point=headless event=<name>` lines out of it — a different channel,
    counted by `headlessEvents`.

    Nothing here can return '-': a step that declared events and produced no evidence is a
    failure, not an absence of one."""
    decls = c.meta.get("events")
    if not decls:
        # Only a step whose YAML carries `events:` is ever asked, so none recorded means the
        # capture predates them: evidence about nothing, not a pass.
        return "no-events-recorded"
    if c.meta.get("scenario"):
        # Re-scoring: the comparators come from the capture, the decision to ask from the YAML.
        steps = scenario(c.meta["scenario"])["steps"]
        now = next((s for s in steps if s["name"] == c.meta.get("step")), steps[0])["events"]
        if [_asked(d) for d in now] != [_asked(d) for d in decls]:
            return "spec-changed-since-capture"
    failed = []
    done = c.app("events-armed ", window=False)
    n_armed = c.field(done[-1], "n") if done else None
    if n_armed != str(len(decls)):
        # The subscription pass itself is evidence: a driver that dropped half the list must not
        # look like an SDK that never fired.
        failed.append("arm-incomplete=%s/%d" % (n_armed if n_armed is not None else "-", len(decls)))
    for ident, why, paths in _event_items(c, decls):
        if why:
            failed.append("%s=%s" % (ident, why))
        else:
            failed += ["%s.%s=%s" % (ident, path, pwhy) for path, pwhy in paths if pwhy]
    return ", ".join(failed) if failed else "ok"


def _event_items(c, decls):
    """(WO-027) Per `events:` declaration: (id, why, [(path, why), …]), as _api_items is for hooks."""
    armed, delivered, rets = {}, {}, _rets(c)
    for line in c.app("event-arm id=", window=False):
        armed.setdefault(c.field(line, "id"), c.field(line, "outcome"))
    for line in c.app("event id=", window=False):
        ident = c.field(line, "id")
        try:                                  # `n=` is the driver's own delivery counter
            delivered[ident] = max(delivered.get(ident, 0), int(c.field(line, "n", "0")))
        except ValueError:
            delivered[ident] = delivered.get(ident, 0)
    # (Tier C, C5.0) The removal each `remove_at_s` asked for, and the whole log in order for the anchors.
    removed = {}
    for line in c.app("event-removed id=", window=False):
        removed.setdefault(c.field(line, "id"), c.field(line, "outcome"))
    pairs = [(l, c.norm[l]) for l in c.lines]
    out = []
    for i, decl in enumerate(decls):
        ident = "%s@%d" % (decl["subscribe"], i)
        outcome = armed.get(ident)
        # (Tier C, R4) `delivery: N` needs N deliveries, whatever `min` says.
        seen, want = delivered.get(ident, 0), min_deliveries(decl)
        if outcome is None:
            out.append((ident, "never-armed", []))
        elif outcome == "absent":
            out.append((ident, "absent", []))
        elif outcome != "ok":
            out.append((ident, _clip(outcome), []))
        # (Tier C, C5.0) A removal that never came is `never-removed` — a driver from before R reads that, and it is
        # the lab's defect, named — and one that did not remove, `removed-<outcome>` (threw, absent).
        elif "remove_at_s" in decl and removed.get(ident) != "ok":
            out.append((ident, "never-removed" if ident not in removed else "removed-" + _clip(removed[ident]), []))
        elif "arm_only" in decl:
            out.append((ident, None, [(path, None) for path in decl.get("returns") or {}]))
        elif seen < want:
            out.append((ident, "never-fired" if not seen else "fired-%d/%d" % (seen, want), []))
        elif "max" in decl and seen > decl["max"]:
            out.append((ident, "fired-%d-over-max-%d" % (seen, decl["max"]), []))
        else:
            why = None
            if "none_after" in decl:
                k = _delivered_after(pairs, ident, decl["none_after"], c.year)
                why = ("no-anchor-%s" % decl["none_after"] if k is None else
                       "deliveredAfter%s-%d" % ("Move" if MOVE_ID.match(decl["none_after"]) else "Remove", k)
                       if k else None)
            if why is None and "min_after" in decl:
                ref, n = decl["min_after"]["ref"], decl["min_after"]["n"]
                k = _delivered_after(pairs, ident, ref, c.year)
                why = "no-anchor-%s" % ref if k is None else "fired-%d/%d-after-%s" % (k, n, ref) if k < n else None
            if why:
                out.append((ident, why, []))
            else:
                # The paths are asserted against the FIRST delivery: the driver reports `ret`
                # lines once, and _rets keeps the first line per (id, path) anyway.  (Tier C, R4) Or against the
                # declaration's `delivery`, whose lines name it (_delivery).  (Tier C, C4.0) A path
                # its `under:` container excuses holds, as on an api hook (_excused).  (Tier C, C5.0) With `min: 0`
                # and nothing delivered, there is no delivery to hold them to; a recorded path is never red.
                mine = _delivery(rets, decl, ident)
                out.append((ident, None, [] if not seen else
                            [(path, None if checks.get(RECORD) or _excused(mine, ident, decl["returns"], path) else
                              "unreported" if mine.get((ident, str(path))) is None
                              else _check(mine[(ident, str(path))], checks))
                             for path, checks in (decl.get("returns") or {}).items()]))
    return out


# (WO-027) What a running step has simply not produced YET.  Live, these read `pending`; once the
# step is over they are the failure reasons apiAssertions and eventAssertions report.  (Tier C, C3)
# `unreported-ref`: the hook a `same:`/`differs:` compares with has not reported that path.  (Tier C, C4.0)
# `unsettled`: an `outcome:` hook with no `api id=` line yet.
PENDING = ("never-called", "never-armed", "never-fired", "unreported", "unreported-ref", "unsettled",
           "never-removed")      # (Tier C, C5.0) and `no-anchor-<anchor>`, `fired-<k>/<n>-after-<ref>` (verdict_view)


def verdict_view(c, step, final=False):
    """(WO-027) The host's verdict per api hook and `events:` row, and per reported path, for the
    device view to colour: {"rows": {id: v}, "paths": {id: {path: v}}}, v = 'ok', 'pending'
    (live only) or the failure reason.  Built from _api_items/_event_items — the same evaluation
    the capture is scored by — and pushed to the app by run.py.  The device never judges."""
    items = []
    api = [(i, h) for i, h in enumerate(step.get("hooks") or []) if "api" in h]
    if api:
        items += _api_items(c, api)[0]
    if step.get("events"):
        items += _event_items(c, step["events"])
    rows, paths = {}, {}

    def state(why):
        if not why:
            return "ok"
        # (Tier C, C5.0) `fired-<n>-over-max-<m>` is final: a count only grows.
        if not final and (why in PENDING or why.startswith(("fired-", "no-anchor-")) and "-over-max-" not in why):
            return "pending"
        return why
    for ident, why, ps in items:
        paths[ident] = {str(p): state(w) for p, w in ps}
        row = state(why)
        if row == "ok" and ps:
            got = list(paths[ident].values())
            row = next((v for v in got if v not in ("ok", "pending")), None) or \
                ("pending" if "pending" in got else "ok")
            if row not in ("ok", "pending"):
                row = "fail"
        rows[ident] = row
    return {"rows": rows, "paths": paths}


# ---------------------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------------------

def compare(actual, expected):
    """`expected` is a literal, or '>=N' / '<=N' / '>N' / '<N' / 'any'."""
    if expected == "any":
        return True
    if isinstance(expected, bool):
        return str(actual).lower() == str(expected).lower()
    if isinstance(expected, str):
        m = re.match(r"^(>=|<=|>|<)\s*(-?\d+)$", expected.strip())
        if m:
            try:
                left = int(actual)
            except (TypeError, ValueError):
                return False
            op, right = m.group(1), int(m.group(2))
            return {">=": left >= right, "<=": left <= right,
                    ">": left > right, "<": left < right}[op]
    return str(actual).lower() == str(expected).lower()


# Scored for every step, declared or not: a lab whose driver quietly dropped the scenario's
# config produces confident, wrong verdicts about the SDK.
IMPLICIT = {"configDropped": "-"}
# (WO-027) Only for a step that carries an `api` hook — report() prints every row, so scoring it
# everywhere would add a line to every committed capture's verdict table.
IMPLICIT_API = {"apiAssertions": "ok"}
# (WO-027) The same, for a step that declares `events:`.
IMPLICIT_EVENTS = {"eventAssertions": "ok"}
# (Tier C, C5.0) For a step that declares `moves:` / `actions:`.
IMPLICIT_MOVES = {"movesSent": "ok"}
IMPLICIT_ACTIONS = {"actionsDone": "ok"}
# (Tier C, C4.2) Only for a step with a `record: true` path, and `any`: the row carries the readings, never a verdict.
# (Tier C, C4.3) Or with an `outcome: settled` hook, whose reading the row carries too.
IMPLICIT_RECORD = {"recorded": "any"}


# (WO-027) Ending a step on its evidence.  70 % of step time on the WO-027 scenarios fell after the
# last thing the step asserts (1330 of 1910 s, 2026-09-23), and a human watching called "done" 8-11 s
# after it.  A step may end early only when every metric it scores reads the WHOLE log and is
# complete once its evidence has arrived — these five, and undeclaredDialogs, which like crashes
# only holds so far and is covered by a grace floor (DIALOG_GRACE_S).  Anything windowed (enabled,
# terminationPasses, headless, labCalls …) keeps the full window, as does a scenario that says
# `end: window`, a triggered step, and a step a mutation variant inverts (must_fail()).
LAB_HTTP_METRICS = {"labHttp", "labHttpRecords", "labHttpRefreshedUploads"} | {
    "labHttp.%s.%d" % (route, status) for route in LAB_HTTP_ROUTES for status in LAB_HTTP_STATUSES}
EARLY_METRICS = {"crashes", "configDropped", "apiAssertions", "eventAssertions", "dialogAnswered",
                 "undeclaredDialogs", "recorded",      # (Tier C, C4.2) `recorded` is complete when apiAssertions is
                 "movesSent", "actionsDone",           # (Tier C, C5.0) the whole log; evidence_state waits for the marks
                 # (Tier C, C5.2) complete once the moves' `observe_s` has passed, which evidence_state already waits for:
                 # a step that expects it sizes its move's observe_s to hold the drop, and measures from launch so the
                 # window opens before its jump (geofence-exit-far-android).  Left out in C5.0, its one expecting step
                 # could not end on evidence, and a step with hooks or events must (tick_gcp).
                 "deferredExitDropped",
                 # (WO-107) the whole log, and complete with apiAssertions: a driver logs a hook's `alongside` line
                 # before that hook's `api id=… outcome=`, so it is there by the time `api-done` reaches the count —
                 # and the bridge builds its last polygon before addGeofences can settle.
                 "alongsideMs", "alongsideLeadMs",
                 # (Tier C, C5.3) the lab server's marks.  Complete with the step's evidence only because a step that
                 # expects them asks, after its upload, something the upload's end makes true (events-http's getCount,
                 # in a later slot where a retry follows the first answer): its `api-done` then comes after the
                 # server's last answer.  A step with hooks or events must end on evidence (tick_gcp).
                 *LAB_HTTP_METRICS}
# (Tier C, C5.3 review) The members of EARLY_METRICS that read the WINDOW (`measure_from` to the capture mark), not
# the whole log.  A step that ends on evidence has no trigger, so its trigger mark is only the host loop's first pass:
# after the launch has returned, with the app already running (events-http's first upload was marked by the server
# 140 ms after it on Flutter Android), or just before the capture mark when the step ends inside `settle`.  A window
# opened there can lose what the metric counts, so such a step measures from launch (scenario() refuses otherwise).
WINDOWED_EARLY = {"deferredExitDropped"} | LAB_HTTP_METRICS
# `crashes` is never "complete" — it only holds so far — so a step waits a GRACE after its evidence,
# and the liveness tick must come after the grace (run.py): a crash inside it stops the ticks and
# drops the step back to its full window.
#
# (WO-027, 2026-09-24) The grace was a flat 12 s, sized from the wrong number: "Android kills an
# unpromoted foreground service ~10 s after startForegroundService()".  The deadline is 30 s — AOSP's
# DEFAULT_SERVICE_START_FOREGROUND_TIMEOUT_MS; `device_config get activity_manager
# service_start_foreground_timeout_ms` reads null, the default, on the lab's Android 16 emulator — and
# the drivers' own tick starts a fresh LocationRequestService every other tick, so 12 s after the
# evidence never covered 30 s after the last start.  A step now ends on PROOF (class Grace):
#  - Android: every foreground-service start Android logged for the app has been promoted — the
#    SDK's startId-1 line for that service — and the framework agrees (dumpsys, run.py: no record
#    still owes its startForeground(), and every service started this step isForeground).  The grace
#    ends FGS_GRACE_S after the evidence, never while a start awaits its promotion: a start pending
#    then — or at the liveness tick, which run.py reads twice for it where the driver still runs its
#    per-tick fix (tick_gcp) — holds the step until it is promoted or END_GRACE_S has passed.  A
#    promotion does not push the end back: a promoted service cannot miss its deadline, and the
#    drivers' tick starts (and promotes) one every other tick, which would otherwise hold every step
#    to the cap (measured in the replay).  Anything unproven — a start
#    still unpaired at the cap (reset() drops the log level, so api-config's promotions go unlogged),
#    a veto, the app's crash, a promotion with no logged start (a device whose Android does not log
#    them: API 33 omits a foreground app's) — holds the step to END_GRACE_S, the old rule, and
#    end.grace records why.  A device whose deadline is 15 s or less adds a floor.
#  - iOS has no foreground services: IOS_GRACE_S by what the step runs.  watchPosition: the SDK's
#    5 s transient-error timer fired at evidence + 5.1 s on the per-tick getCurrentPosition, which
#    Tier B's B2 removed from conformance steps, so a watching step now takes the default.  Geofences
#    took 12 s: a proximity evaluation ran at evidence + 12 s, fed by that same per-tick fix.  (Tier C,
#    C7.4) With it gone, 13 of 13 iOS geofence captures were quiet from evidence + 0.5 s, and the SDK's
#    only delayed re-evaluation, -[TSGeofenceManager evaluateProximity:delay:], has no caller
#    (TSGeofenceManager.m:387, native/ios 7807571): an evaluation follows a new location or a radius
#    change.  So a geofence step takes the default too.
#  - A non-final step of a multi-step scenario kept END_GRACE_S until an A/B run showed the next step
#    unaffected by an earlier end.  (Tier C, C7.3) It did, 2026-09-25: arms hold/3, end/3 and end/0 on the
#    four scenarios whose non-final steps carry state into the next launch, 112 steps on 8 cells each —
#    `--trace-diff` SAME 112, 0 ORDER-CHANGED, 0 SUMMARY-CHANGED for both.  So a non-final step now ends
#    on the same proof as a final one (NONFINAL), and Android's pause after it is gone (POST_SETTLE);
#    `--nonfinal hold` / `--post-settle 3` still give the old rule.
# Replayed over the 2026-09-23 baseline (summarize.py --replay-end): no metric changed — neither did a
# grace of 0, so the replay cannot rank rules; safety rests on the mechanism above.
END_GRACE_S = 12            # the cap, and the rule wherever proof is missing
NONFINAL, POST_SETTLE = "end", 0     # (Tier C, C7.3) the defaults the A/B chose; run.py and sweep.py read them
# (WO-027 Tier B, B3) How often the host reads the log while a step runs.  The liveness line it waits
# for after the grace is the driver's beat now (1 s; 0.5 s since Tier C, C7.6), so a 1 s poll would add
# up to a second of its own.
# run.json `end` records the parse cost per poll: if it ever reads above ~50 ms, halve the rate.
POLL_S = 0.25
FGS_GRACE_S = 2
# (Tier B, B5) "watch" was 6 s, for the SDK's 5 s transient-error timer: it fired on the per-tick
# getCurrentPosition a watching step made ("Transient error grace expired (≈5.0s); failing 1
# request(s)", every iOS watch-position capture of 2026-09-23).  B2 took that request off
# conformance steps, and no iOS watch-position capture since carries the expiry (S2, S3: 0 of 8).
IOS_GRACE_S = {"default": 2, "watch": 2, "geofences": 2}
# (WO-027, permissions) A step that scores undeclaredDialogs asserts an ABSENCE, which its evidence
# never completes, and 2 s after it is too short to see a late window: the SDK defers its Settings
# alert 1 s while a view controller is presented (TSLocationAuthorization.m:624-627), and one idb
# describe-all poll takes about 1 s.  So such a step waits at least this long past its evidence —
# which includes api-done — on both OSes.  Chosen over leaving the metric out of EARLY_METRICS: that
# would hold every such step to its full window, and break the rule that a conformance step (no
# per-tick fix) ends on its evidence.
DIALOG_GRACE_S = 8

# Android's own line for each new service record's foreground start (Android 16 logs it even for a TOP
# app; later startForegroundService() deliveries to a live record are not logged — hence startId 1), and
# the SDK's line after startForeground() (AbstractService.java: `🟢 <action> [<Service> startId: N`).
FGS_START = re.compile(r"ActivityManager\(\s*\d+\): Background started FGS: (Allowed|Disallowed) \[.*?"
                       r"cmp=([\w.]+)/([\w.$]+)")
FGS_PROMOTED = re.compile(r"TSLocationManager\(\s*\d+\):\s+\U0001F7E2\s+\S+ \[(\w+)\s+startId: (\d+),")
# Vetoes: the SDK's own (AbstractService.java), and Android's, which name the app's ServiceRecord.
FGS_VETO = re.compile(r"Cannot promote \w+|\(guarded stop startId: \d+ -> |Background FGS launch denied")
FGS_MISSED = re.compile(r"Bringing down service while still waiting for start foreground|"
                        r"did not then call Service\.startForeground|ForegroundServiceDidNotStartInTimeException")


def fgs_deadline_s(device):
    """The device's foreground-service start deadline: device_config's value, else AOSP's default
    by API level (30 s since API 33; an older or unknown level is taken as 10 s, which floors)."""
    ms = (device or {}).get("fgs_timeout_ms")
    if ms:
        return int(ms) / 1000.0
    return 30.0 if int((device or {}).get("sdk") or 0) >= 33 else 10.0


def ios_grace(step):
    hooks = step.get("hooks") or []
    if any(h.get("api") in ("addGeofence", "addGeofences") for h in hooks):
        return IOS_GRACE_S["geofences"]
    if (step.get("watch") or any(h.get("api") == "watchPosition" for h in hooks)
            or any(d.get("subscribe") == "watchPosition" for d in step.get("events") or [])):
        return IOS_GRACE_S["watch"]
    return IOS_GRACE_S["default"]


def observes_dialogs(step):
    """(WO-027, permissions) Whether a step scores `undeclaredDialogs`, in `expect` or ANY
    `platform_expect` block: the steps whose grace is floored at DIALOG_GRACE_S (Grace).  Any block,
    because Grace is not told the platform — a floor on a cell that does not score the metric costs
    a few seconds, where a missing one on a cell that does would be a false green."""
    blocks = [step.get("expect") or {}] + list((step.get("platform_expect") or {}).values())
    return any("undeclaredDialogs" in (block or {}) for block in blocks)


def fgs_state(lines, package):
    """(pending, promotions, hold, started) from the log so far: starts of the app's foreground
    services not yet promoted, promotions paired so far, the first reason the step cannot be proven
    safe, and the services started (class names, for the dumpsys check)."""
    pending, promotions, hold, started = {}, 0, None, set()
    for i, line in enumerate(lines):
        m = FGS_START.search(line)
        if m:
            if m.group(2) == package:
                if m.group(1) == "Disallowed":
                    hold = hold or "a foreground-service start was disallowed"
                else:
                    svc = m.group(3).rsplit(".", 1)[-1]
                    pending[svc] = pending.get(svc, 0) + 1
                    started.add(svc)
            continue
        m = FGS_PROMOTED.search(line)
        if m and m.group(2) == "1":                       # a new service record's first delivery
            if pending.get(m.group(1)):
                pending[m.group(1)] -= 1
                promotions += 1
            else:
                hold = hold or "a promotion with no logged start: %s" % m.group(1)
            continue
        if hold is not None:
            continue
        m = FGS_VETO.search(line) or (package + "/" in line and FGS_MISSED.search(line))
        if m:
            hold = m.group(0)
        elif "FATAL EXCEPTION" in line:
            # The app's, as crashes() counts it: the harness's uiautomator dies here too (a bare
            # `PID:`, no `Process:`), and that is no reason to hold the app's step.
            proc = next((re.search(r"Process: ([^,\s]+)", l) for l in lines[i + 1:i + 4]
                         if "Process: " in l), None)
            if proc and (proc.group(1) == package or proc.group(1).startswith(package + ":")):
                hold = "the app crashed: FATAL EXCEPTION in %s" % proc.group(1)
    return sum(pending.values()), promotions, hold, started


class Grace:
    """(WO-027) When may an evidence step end?  Fed the log and the clock on every poll; the SAME
    object decides live (run.py, host seconds) and in the replay (summarize.py, log seconds), so the
    two cannot drift.  update() returns the time the grace ends, or None while a start awaits its
    promotion — until END_GRACE_S, when a start still unpaired holds the step to the cap."""

    def __init__(self, step, os_name, package, final_step=True, deadline_s=30.0):
        self.os, self.package = os_name, package
        # (WO-027, permissions) An absence assertion's floor, on both OSes (DIALOG_GRACE_S).
        self.absence = DIALOG_GRACE_S if observes_dialogs(step) else 0
        self.fixed = max(ios_grace(step), self.absence) if os_name != "android" else None
        self.held = None if final_step else "not the scenario's last step"
        self.floor = max(0.0, 17.0 - deadline_s) if os_name == "android" and deadline_s <= 15 else 0.0
        self.pending, self.promotions, self.started = 0, 0, set()

    def hold(self, reason):
        self.held = self.held or reason

    def update(self, lines, now, t_evidence):
        if self.os == "android" and self.held is None:
            self.pending, self.promotions, veto, self.started = fgs_state(lines, self.package)
            if veto:
                self.hold(veto)
        if t_evidence is None:
            return None
        cap = t_evidence + END_GRACE_S
        if self.held:
            return cap
        if self.fixed is not None:
            return t_evidence + self.fixed
        if self.pending:
            if now < cap:
                return None
            self.hold("%d foreground-service start(s) unpaired at the cap" % self.pending)
            return cap
        return min(cap, t_evidence + max(self.floor, FGS_GRACE_S, self.absence))

    def record(self):
        return {"rule": "held" if self.held else ("fixed" if self.fixed is not None else "fgs"),
                "grace_s": END_GRACE_S if self.held else (self.fixed if self.fixed is not None
                                                          else max(self.floor, FGS_GRACE_S, self.absence)),
                "held": self.held, "fgs_promotions": self.promotions, "floor_s": self.floor}


def tick_gcp(step):
    """(WO-027, Tier B) Whether the driver runs its per-tick getCurrentPosition on this step: only
    on one with no `api` hook and no `events:` — the SAME rule every driver derives from what it was
    wired (LabApi.tickGcpFor), so there is no knob to disagree with.  Nothing on the host reads what
    that fix produces; the legacy windowed metrics count its deliveries (foregroundLocations,
    duplicateLocations, httpFlushes with persistGcp), while on a conformance step it was an
    undeclared cause (the Android geofence ENTER) and started every foreground service Android saw
    after the evidence.  The driver's anchor line says what it decided; schedule_report compares."""
    return not (any(isinstance(h, dict) and isinstance(h.get("api"), str) for h in step.get("hooks") or [])
                or bool(step.get("events")))


def keeps_process(scen, step):
    """(Tier C, C6.0) Whether run.py leaves this step's app process running at its end: the NEXT step says
    `process: live`, so it relaunches into this one.  Here, not inline in run.py, so the selftest holds it."""
    steps = scen["steps"]
    following = next((b for a, b in zip(steps, steps[1:]) if a is step), None)
    return following is not None and following.get("process") == "live"


def live_refusal(before_dir, pid):
    """(Tier C, C6.0 review) Why a `process: live` step must not launch into `pid`, or None.  `pid` is what Adb.alive
    read; `before_dir` is the previous step's capture.  pidof says only that a process exists: one the system started
    after the previous step's process died would pass it, and its cold launch would be scored as the relaunch (boot 1,
    and on an arm a red row that proves nothing).  So the pid must be one that ran the previous step's driver, its
    `app=boot` in that capture.  Here, not inline in run.py, so the selftest holds it."""
    if not pid:
        return "has no process: the previous step's finish() left none to relaunch into"
    try:
        with open(os.path.join(before_dir, "logcat.txt"), errors="replace") as f:
            own = app_pids(f.read().splitlines())
    except OSError:
        own = set()
    if pid not in own:
        return ("runs process %s, which did not boot the previous step's driver (%s): not the process its finish() left"
                % (pid, ", ".join(sorted(own, key=int)) or "no app=boot in %s" % before_dir))
    return None


def _validate_windowed(where, step, platform_names):
    """(Tier C, C5.3 review) WINDOWED_EARLY's comment: refuses a windowed metric on a step that ends on its evidence
    and does not measure from launch."""
    for plat in platform_names:
        windowed = sorted(set(expectations(step, plat)) & WINDOWED_EARLY)
        if windowed and step.get("measure_from", "trigger") != "launch" and end_policy(step, plat) == "evidence":
            raise SystemExit("%s: %s is counted over the window, and this step ends on its evidence: with no trigger, "
                             "its window would open at a mark the host writes after the app is already running — add "
                             "`measure_from: launch`" % (where, ", ".join(windowed)))


def end_policy(step, platform_name, inverted=False):
    """'evidence' when the step may end once everything it asserts has arrived, else 'window'."""
    if step.get("end", "evidence") != "evidence" or inverted or step.get("trigger", "none") != "none":
        return "window"
    want = expectations(step, platform_name)
    return "evidence" if want and set(want) <= EARLY_METRICS else "window"


def evidence_state(lines, step, os_name):
    """(complete, alive) from the raw log lines seen SO FAR — the host's live read, computed with
    the same ids the scorers use.  The device is never told what counts as done.  `alive` COUNTS
    the driver's liveness lines — `app=tick`, and (Tier B, B3) the `app=beat` that touches neither
    the SDK nor a native module, every 0.5 s since Tier C's C7.6 (it was 1 s) — a count only grows,
    where a tick number starts again on a recreated page.  A capture from before the beat counts its
    ticks alone.

    Complete when: `api-done n=` has reached the step's api-hook count; every event declaration
    has reached its `min` (default 1) — or was armed with an outcome other than `ok`, so nothing
    can ever arrive; and every declared dialog action has been answered.  (WO-027) Only the step's
    own driver counts (app_pids): another lab app's ticks proved nothing about this one's life, and
    its `api-done` or `event` lines would complete a step that never did.  Dialog marks are the
    host's, so they are not scoped.  (WO-191) And, last, the product's own predicate (EVIDENCE), handed the
    lines as given."""
    raw, lines = lines, [l for l in lines if TAG in l]    # every line that matters carries it
    own = app_pids(lines)
    norm = [normalize(l) for l in lines]
    app = [normalize(l) for l in lines if line_pid(l) in own and TAG + " app=" in normalize(l)]

    def num(line, key):
        m = re.search(r"\b%s=(\d+)" % key, line)
        return int(m.group(1)) if m else None

    def field_(line, key):
        m = re.search(r"\b%s=(\S+)" % key, line)
        return m.group(1) if m else None

    alive = sum(1 for l in app if " app=tick " in l or " app=beat " in l)
    n_api = sum(1 for h in step.get("hooks") or [] if "api" in h)
    if n_api:
        done = [num(l, "n") for l in app if " app=api-done " in l]
        if max([d for d in done if d is not None], default=0) < n_api:
            return False, alive
    # (Tier C, C5.0) The driver's own lines and the host's move marks, in order, for `min_after` (_delivered_after).
    pairs = [(raw, n) for raw, n in zip(lines, norm) if line_pid(raw) in own and " app=" in n
             or TAG + " point=move-begin " in n] if any("min_after" in d for d in step.get("events") or []) else []
    year = time.localtime().tm_year
    for i, decl in enumerate(step.get("events") or []):
        ident = "%s@%d" % (decl["subscribe"], i)
        arms = [field_(l, "outcome") for l in app if " app=event-arm " in l and field_(l, "id") == ident]
        if arms and arms[-1] != "ok":
            continue
        # (Tier C, C5.0) An `arm_only` declaration is complete once armed; a removal once the driver says it removed;
        # `min_after` once enough deliveries follow its anchor.  `max` and `none_after` are absences, which no
        # evidence completes: a move's `observe_s` bounds them (below), or the step's own causes (api-done).  (C5.0
        # review) A `min: 0` declaration is not complete before its arm — in a later `at_s` slot the step would end
        # first and read never-armed — and a removal is waited for before an `arm_only` one is taken as done.
        if not arms and min_deliveries(decl) == 0:
            return False, alive
        if "remove_at_s" in decl and not any(" app=event-removed " in l and field_(l, "id") == ident for l in app):
            return False, alive
        if "arm_only" in decl:
            if not arms:
                return False, alive
            continue
        seen = [num(l, "n") for l in app if " app=event " in l and field_(l, "id") == ident]
        if max([n for n in seen if n is not None], default=0) < min_deliveries(decl):     # (R4) its `delivery` too
            return False, alive
        if "min_after" in decl and (_delivered_after(pairs, ident, decl["min_after"]["ref"], year) or 0) \
                < decl["min_after"]["n"]:
            return False, alive
    actions = (step.get("dialog") or {}).get(os_name) or []
    if actions and sum(1 for l in norm if "point=dialog action=" in l) < len(actions):
        return False, alive
    # (Tier C, C5.0) Every move's last hop sent, and `observe_s` of the device's log since it; every action done.
    # Host marks, unscoped, as the dialog marks are.
    for i, move in enumerate(step.get("moves") or []):
        hops = move.get("hops", 1)
        last = next((raw for raw, n in zip(lines, norm) if "%s point=move i=%d hop=%d/%d " % (TAG, i, hops, hops) in n),
                    None)
        if last is None or move.get("observe_s") and \
                (stamp_ms(lines[-1], year) or 0) < stamp_ms(last, year) + 1000 * move["observe_s"]:
            return False, alive
    for i in range(len(step.get("actions") or [])):
        if not any("%s point=host i=%d action=" % (TAG, i) in n for n in norm):
            return False, alive
    return EVIDENCE is None or bool(EVIDENCE(raw, step, os_name)), alive


EVIDENCE = None                          # (WO-191) a product's register() may set it: (lines, step, os_name) -> bool


def schedule_report(c, step):
    """(WO-027, Tier B) LAB defects in how the driver scheduled this step, read from its own lines:
    once its first batch completes it logs `app=anchor batchMs=<ms> slots=<s,…|-> tickGcp=<on|off>`,
    and `app=slot at_s=<s> late_ms=<ms>` as each deferred slot starts.  A list of strings, empty when
    the schedule held — never a metric: it is about the lab, not the SDK, so run.py prints it and
    records it in run.json and the step's verdict does not move.

    (Tier C, C7.6) The anchor carried `bootGcp=<on|off>` until the boot fix went.  Handed a capture
    from before with its run.json as `meta`, the field is read against `driver.bootGcp`, what that
    driver was told; without one — run.py's live check — it is a stale driver, which runs the boot
    getCurrentPosition unless told otherwise."""
    anchors = c.app("anchor ", window=False)
    if not anchors and step.get("process") == "live":
        # (Tier C, C6.0) By design: Capacitor, Cordova and Flutter run their first batch on boot 1 only, and a live
        # relaunch boots 2 (the C6 headless analysis §3); React Native runs it once per JS context (`firstQueued`,
        # index.js:36, :261), which a live relaunch keeps.  So no batch completes and no anchor is logged.
        return []
    if not anchors:
        return ["no app=anchor line — a driver from before Tier B (rebuild it), or its first batch "
                "never completed"]
    defects = []
    want = {"tickGcp": "on" if tick_gcp(step) else "off"}
    told = (c.meta.get("driver") or {}).get("bootGcp")
    if told is not None:
        want["bootGcp"] = "on" if told else "off"
    for line in anchors:
        for key, value in want.items():
            if c.field(line, key) != value:
                defects.append("anchor %s=%s, the host expects %s" % (key, c.field(line, key), value))
        if told is None and c.field(line, "bootGcp", None) is not None:
            defects.append("anchor bootGcp=%s — a driver from before Tier C C7.6 (rebuild it)"
                           % c.field(line, "bootGcp"))
    slots = []
    for line in c.app("slot at_s=", window=False):
        try:              # compared as numbers: a Dart driver prints a YAML `8.0` as 8.0, JavaScript as 8
            slots.append((float(c.field(line, "at_s")), int(c.field(line, "late_ms"))))
        except ValueError:
            defects.append("unreadable slot line: %s" % line)
    # (Tier C, C5.0) A declaration's `remove_at_s` is a slot too (R2): a driver from before R starts none for it.
    declared = sorted({float(d[key]) for d in [h for h in step.get("hooks") or [] if "api" in h]
                       + list(step.get("events") or []) for key in ("at_s", "remove_at_s") if key in d})
    for at_s in declared:
        late = [ms for s, ms in slots if s == at_s]
        if not late:
            defects.append("no app=slot line for at_s=%g — the slot never started" % at_s)
        defects += ["slot at_s=%g started %d ms late (over 1000)" % (at_s, ms) for ms in late if ms > 1000]
    return defects


def expectations(step, platform_name, os_name=None):
    """A step's expectations for one platform: `platform_expect` overrides `expect` per key,
    for behaviour that legitimately differs (React Native keeps its JS context across a
    recreation, so ready() is not re-run).  (WO-045) With the OS, `dialogAnswered: declared`
    becomes that OS's declared `dialog:` sequence ('-' where it declares none)."""
    merged = dict(step.get("expect") or {})
    merged.update((step.get("platform_expect") or {}).get(platform_name, {}))
    if os_name and merged.get("dialogAnswered") == "declared":
        merged["dialogAnswered"] = ",".join((step.get("dialog") or {}).get(os_name) or []) or "-"
    # (WO-027) A conformance step with `expect: {}` would otherwise contribute no verdict at all,
    # and the run would exit PASS on all([]): the scenario cannot forget to ask.
    has_api = any("api" in hook for hook in step.get("hooks") or [])
    has_events = bool(step.get("events"))
    if merged or has_api or has_events:            # a step with no expectations is a setup step; leave it
        for key, value in IMPLICIT.items():
            merged.setdefault(key, value)
    if has_api:
        merged.update(IMPLICIT_API)
    if any(_settled(hook) or any((checks or {}).get(RECORD) for checks in (hook.get("returns") or {}).values())
           for hook in step.get("hooks") or [] if "api" in hook) or any(map(_records, step.get("events") or [])):
        merged.update(IMPLICIT_RECORD)
    if has_events:
        merged.update(IMPLICIT_EVENTS)
    if step.get("moves"):
        merged.update(IMPLICIT_MOVES)
    if step.get("actions"):
        merged.update(IMPLICIT_ACTIONS)
    return merged


def score(cap, expectations):
    """[(metric, actual, expected, ok)] for one capture."""
    rows = []
    for key, expected in expectations.items():
        if key not in METRICS:
            raise SystemExit("unknown metric %r (known: %s)" % (key, ", ".join(sorted(METRICS))))
        actual = METRICS[key](cap)
        rows.append((key, actual, expected, compare(actual, expected)))
    return rows


def report(rows, inverted=False, stream=sys.stdout):
    """Print one PASS/FAIL block. `inverted`: a mutation that MUST fail at least one row."""
    for key, actual, expected, ok in rows:
        print("   %-32s %-22s expected %-12s %s"
              % (key, actual, expected, "ok" if ok else "MISMATCH"), file=stream)
    if inverted:
        verdict = any(not ok for _, _, _, ok in rows)
        print("   => %s (this variant must break at least one expectation)"
              % ("PASS" if verdict else "FAIL"), file=stream)
    else:
        verdict = all(ok for _, _, _, ok in rows)
        print("   => %s" % ("PASS" if verdict else "FAIL"), file=stream)
    return verdict


# ---------------------------------------------------------------------------------------
# selftest (WO-027) — the api/ret vocabulary, scored offline against hand-written logcat
# ---------------------------------------------------------------------------------------

def _selftest():
    import io
    import shutil
    import tempfile

    hooks = [
        {"api": "stop", "returns": {"$.enabled": {"type": "boolean", "equals": False}}},
        {"at": 3, "call": "reset", "args": "same"},      # a legacy hook keeps its list position
        {"api": "getCount", "returns": {"$": {"type": "number", "equals": 45}}},
        {"api": "getLocations", "returns": {"$.length": {"equals": 1},
                                            "$.0.timestamp": {"type": "string", "startsWith": "2024-01-15"},
                                            "$.0.sample": {"absent": True}}},
        {"api": "insertLocation", "args": {"uuid": "u 1"},
         "returns": {"$": {"type": "string", "equals": "u 1"}}},
        {"api": "destroyLocation", "args": "nope", "returns": {"$err": {"type": "string", "equals": ""}}},
    ]
    # Three hosts' line shapes on purpose: a native LAB tag, and two consoles that embed it.
    green = ["I/LAB     ( 1): app=api id=stop#0 outcome=ok",
             "I/LAB     ( 1): app=ret id=stop#0 path=$.enabled type=boolean value=false parent=1",
             "I/ReactNativeJS( 1): LAB app=api id=getCount#2 outcome=ok",
             "I/ReactNativeJS( 1): LAB app=ret id=getCount#2 path=$ type=number value=45.0 parent=1",
             "I/flutter ( 1): LAB app=api id=getLocations#3 outcome=ok",
             "I/flutter ( 1): LAB app=ret id=getLocations#3 path=$.length type=number value=1 parent=1",
             "I/flutter ( 1): LAB app=ret id=getLocations#3 path=$.0.timestamp type=string value=2024-01-15T10%3A30%3A00.000Z parent=1",
             "I/flutter ( 1): LAB app=ret id=getLocations#3 path=$.0.sample type=missing parent=1",
             "I/LAB     ( 1): app=api id=insertLocation#4 outcome=ok",
             "I/LAB     ( 1): app=ret id=insertLocation#4 path=$ type=string value=u%201 parent=1",
             "I/LAB     ( 1): app=api id=destroyLocation#5 outcome=rejected",
             "I/LAB     ( 1): app=ret id=destroyLocation#5 path=$err type=string value= parent=1",
             "I/LAB     ( 1): app=api-done n=5"]

    def swap(lines, needle, *replacement):
        out = []
        for line in lines:
            out += list(replacement) if needle in line else [line]
        return out

    cases = [
        ("green: three hosts, 45.0 == 45, %-decoding, an empty-string $err", green, "ok"),
        ("absent", swap(swap(green, "ret id=getCount#2"), "api id=getCount#2",
                        "I/LAB     ( 1): app=api id=getCount#2 outcome=absent"),
         "getCount#2=absent"),
        ("rejected with no $err declared", swap(swap(green, "ret id=stop#0"), "api id=stop#0",
                                               "I/LAB     ( 1): app=api id=stop#0 outcome=rejected"),
         "stop#0=rejected"),
        ("resolved where a rejection was declared",
         swap(green, "api id=destroyLocation#5", "I/LAB     ( 1): app=api id=destroyLocation#5 outcome=ok"),
         "destroyLocation#5=resolved"),
        ("the batch died part-way", green[:4],
         "batch-incomplete=2/5, getLocations#3=never-called, insertLocation#4=never-called, "
         "destroyLocation#5=never-called"),
        ("nothing ran at all — a failure, never '-'", [],
         "batch-incomplete=0/5, stop#0=never-called, getCount#2=never-called, "
         "getLocations#3=never-called, insertLocation#4=never-called, destroyLocation#5=never-called"),
        ("a declared path the driver never reported", swap(green, "path=$.length"),
         "getLocations#3.$.length=unreported"),
        ("wrong type", swap(green, "ret id=getCount#2",
                            "I/LAB     ( 1): app=ret id=getCount#2 path=$ type=object parent=1"),
         "getCount#2.$=want-number-got-object"),
        ("equals is type-strict: the string '45' is not 45",
         swap(green, "ret id=getCount#2", "I/LAB     ( 1): app=ret id=getCount#2 path=$ type=string value=45 parent=1"),
         "getCount#2.$=want-number-got-string"),
        ("wrong value", swap(green, "path=$.length",
                             "I/flutter ( 1): LAB app=ret id=getLocations#3 path=$.length type=number value=2 parent=1"),
         "getLocations#3.$.length=want-1-got-2"),
        ("a Date where a string was promised",
         swap(green, "path=$.0.timestamp",
              "I/flutter ( 1): LAB app=ret id=getLocations#3 path=$.0.timestamp type=object parent=1"),
         "getLocations#3.$.0.timestamp=want-string-got-object"),
        ("`absent` is not vacuous: $.0 has to exist",
         swap(green, "path=$.0.sample",
              "I/flutter ( 1): LAB app=ret id=getLocations#3 path=$.0.sample type=missing parent=0"),
         "getLocations#3.$.0.sample=missing"),
        ("`absent` against a key that is there",
         swap(green, "path=$.0.sample",
              "I/flutter ( 1): LAB app=ret id=getLocations#3 path=$.0.sample type=boolean value=true parent=1"),
         "getLocations#3.$.0.sample=want-absent-got-boolean"),
        ("a clipped value is 'could not evaluate', not 'failed'",
         swap(green, "ret id=insertLocation#4",
              "I/LAB     ( 1): app=ret id=insertLocation#4 path=$ type=string value=u%201 trunc=1 parent=1"),
         "insertLocation#4.$=truncated"),
    ]

    def capture(lines, meta_hooks, meta_events=None, **meta):
        path = tempfile.mkdtemp(prefix="labkit-selftest-")
        body = ["09-21 10:00:00.000 I/LAB     ( 1): point=launch label=selftest"]
        body += ["09-21 10:00:05.%03d %s" % (n, l) for n, l in enumerate(lines)]
        body += ["09-21 10:00:20.000 I/LAB     ( 1): point=trigger none",
                 "09-21 10:00:30.000 I/LAB     ( 1): point=capture"]
        with open(os.path.join(path, "logcat.txt"), "w") as f:
            f.write("\n".join(body) + "\n")
        with open(os.path.join(path, "run.json"), "w") as f:
            json.dump(dict({"hooks": meta_hooks, "events": meta_events, "year": 2026}, **meta), f)
        try:
            return Capture(path)          # read whole at construction; nothing is left behind
        finally:
            shutil.rmtree(path)

    failures = 0

    def check(title, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print("%s  %s%s" % ("ok  " if ok else "FAIL", title,
                            "" if ok else "\n      got  %r\n      want %r" % (got, want)))

    for title, lines, want in cases:
        check(title, apiAssertions(capture(lines, hooks)), want)

    check("a capture that recorded no hooks is a failure, never a vacuous ok",
          apiAssertions(capture(green, None)), "no-hooks-recorded")
    ordered = [{"api": "getCount", "returns": {"$": {"gte": 0, "lt": 100}}}]
    for title, line, want in [
            ("gte/lt pass a number — 45.0 included", "type=number value=45.0 parent=1", "ok"),
            ("gte is as type-strict as equals", "type=string value=45 parent=1", "getCount#0.$=want-number-got-string"),
            ("lt fails on the value", "type=number value=100 parent=1", "getCount#0.$=want-lt-100-got-100")]:
        check(title, apiAssertions(capture(["I/LAB     ( 1): app=api id=getCount#0 outcome=ok",
                                            "I/LAB     ( 1): app=ret id=getCount#0 path=$ " + line,
                                            "I/LAB     ( 1): app=api-done n=1"], ordered)), want)
    check("a ret line the transport cut (no closing parent=) is `truncated`, not compared",
          apiAssertions(capture(swap(green, "ret id=insertLocation#4",
                                     "I/LAB     ( 1): app=ret id=insertLocation#4 path=$ type=string value=u%201"), hooks)),
          "insertLocation#4.$=truncated")
    check("labCalls never sees an api line", labCalls(capture(green, hooks)), "-")

    # ------------------------------------------------------------ not_applicable, the OS axis
    one_os = {"platforms": ["flutter"], "steps": [{"trigger": "none", "hooks": []}],
              "not_applicable": {"ios": "2026-09-23 — reported on Android only"}}
    check("a scenario can be specific to one OS, with a dated reason",
          applicability(one_os, platform("flutter", "ios")), "2026-09-23 — reported on Android only")
    check("and still applies on the other one",
          applicability(one_os, platform("flutter", "android")), None)
    one_os["not_applicable"] = {"android": "2026-09-25 — reported on iOS only"}
    check("(Tier C, C2) the android key excuses Android too — an Android view carries its os",
          applicability(one_os, platform("flutter", "android")), "2026-09-25 — reported on iOS only")
    check("(Tier C, C2) ... and the scenario still applies on iOS",
          applicability(one_os, platform("flutter", "ios")), None)

    # ------------------------------------------------------------------ eventAssertions (WO-027)
    events = [{"subscribe": "onProviderChange", "returns": {"$.enabled": {"type": "boolean"}}},
              {"subscribe": "onEnabledChange", "min": 2, "returns": {"$": {"type": "boolean"}}}]
    fired = ["I/LAB     ( 1): app=event-arm id=onProviderChange@0 outcome=ok",
             "I/LAB     ( 1): app=event-arm id=onEnabledChange@1 outcome=ok",
             "I/LAB     ( 1): app=events-armed n=2",
             "I/LAB     ( 1): app=event id=onProviderChange@0 n=1",
             "I/LAB     ( 1): app=ret id=onProviderChange@0 path=$.enabled type=boolean value=true parent=1",
             "I/LAB     ( 1): app=event id=onEnabledChange@1 n=1",
             "I/LAB     ( 1): app=ret id=onEnabledChange@1 path=$ type=boolean value=true parent=1",
             "I/LAB     ( 1): app=event id=onEnabledChange@1 n=2"]
    for title, lines, want in [
            ("every declared event fired with the declared shape", fired, "ok"),
            ("a capture that recorded no events is a failure, never a vacuous ok", fired, "SENTINEL"),
            ("an event that never fired is named, not excused",
             [l for l in fired if "id=onProviderChange@0 n=" not in l and "path=$.enabled" not in l],
             "onProviderChange@0=never-fired"),
            ("a member the bridge does not have is `absent`",
             swap(fired, "event-arm id=onProviderChange@0",
                  "I/LAB     ( 1): app=event-arm id=onProviderChange@0 outcome=absent"),
             "onProviderChange@0=absent"),
            ("subscribing threw — the arm outcome says so, and it is not a silent zero",
             swap(fired, "event-arm id=onEnabledChange@1",
                  "I/LAB     ( 1): app=event-arm id=onEnabledChange@1 outcome=threw"),
             "onEnabledChange@1=threw"),
            ("fewer deliveries than `min` reports the count, not just a failure",
             [l for l in fired if "id=onEnabledChange@1 n=2" not in l], "onEnabledChange@1=fired-1/2"),
            ("a payload of the wrong type is named with its path",
             swap(fired, "ret id=onProviderChange@0",
                  "I/LAB     ( 1): app=ret id=onProviderChange@0 path=$.enabled type=string value=yes parent=1"),
             "onProviderChange@0.$.enabled=want-boolean-got-string"),
            ("a driver that armed half the list cannot look like an SDK that never fired",
             swap(fired, "events-armed", "I/LAB     ( 1): app=events-armed n=1"),
             "arm-incomplete=1/2"),
            ("the headless task's own event lines are a different channel",
             fired + ["I/LAB     ( 1): point=headless event=location"], "ok")]:
        if want == "SENTINEL":
            check(title, eventAssertions(capture(lines, hooks)), "no-events-recorded")
        else:
            check(title, eventAssertions(capture(lines, hooks, events)), want)
    check("an api ret line never answers an event path (ids cannot collide: @ vs #)",
          eventAssertions(capture(fired + ["I/LAB     ( 1): app=ret id=onProviderChange#0 path=$.enabled "
                                           "type=string value=x parent=1"], hooks, events)), "ok")
    armed_late = [{"subscribe": "onLocation", "returns": {"$.uuid": {"type": "string"}}},
                  {"subscribe": "watchPosition", "at_s": 4, "args": {"interval": 1000},
                   "returns": {"$.uuid": {"type": "string"}}}]
    late_lines = ["I/LAB     ( 1): app=event-arm id=onLocation@0 outcome=ok",
                  "I/LAB     ( 1): app=events-armed n=1",
                  "I/LAB     ( 1): app=event id=onLocation@0 n=1",
                  "I/LAB     ( 1): app=ret id=onLocation@0 path=$.uuid type=string value=u1 parent=1",
                  "I/LAB     ( 1): app=event-arm id=watchPosition@1 outcome=ok",
                  "I/LAB     ( 1): app=events-armed n=2",
                  "I/LAB     ( 1): app=event id=watchPosition@1 n=1",
                  "I/LAB     ( 1): app=ret id=watchPosition@1 path=$.uuid type=string value=w1 parent=1"]
    check("a declaration armed in a later slot is scored from the LAST events-armed",
          eventAssertions(capture(late_lines, hooks, armed_late)), "ok")
    check("a later arm the step never reached is named, and the count says so",
          eventAssertions(capture(late_lines[:4], hooks, armed_late)),
          "arm-incomplete=1/2, watchPosition@1=never-armed")
    check("a member's options cross the wire with it, and nothing else does",
          wire_events(armed_late)[1],
          {"on": "watchPosition", "id": "watchPosition@1", "paths": ["$.uuid"],
           "args": {"interval": 1000}, "at_s": 4})
    check("the device is told which member and which paths, never `min` or a comparator",
          wire_events(events),
          [{"on": "onProviderChange", "id": "onProviderChange@0", "paths": ["$.enabled"]},
           {"on": "onEnabledChange", "id": "onEnabledChange@1", "paths": ["$"]}])
    check("expect: {} still scores a step that only declares events",
          expectations({"events": events, "expect": {}}, "flutter"),
          {"configDropped": "-", "eventAssertions": "ok"})
    wire = wire_hook(hooks[4])
    # ----------------------------------------------------------------- `at_s:` on an api hook
    deferred = [{"api": "getCount", "returns": {"$": {"type": "number"}}},
                {"api": "stop", "at_s": 8, "returns": {"$.enabled": {"type": "boolean"}}}]
    late = ["I/LAB     ( 1): app=api id=getCount#0 outcome=ok",
            "I/LAB     ( 1): app=ret id=getCount#0 path=$ type=number value=0 parent=1",
            "I/LAB     ( 1): app=api-done n=1",
            "I/LAB     ( 1): app=api id=stop#1 outcome=ok",
            "I/LAB     ( 1): app=ret id=stop#1 path=$.enabled type=boolean value=false parent=1",
            "I/LAB     ( 1): app=api-done n=2"]
    check("a deferred hook is scored from the LAST api-done, not the first",
          apiAssertions(capture(late, deferred)), "ok")
    check("a deferred hook the step never reached is `never-called`, and the count says so",
          apiAssertions(capture(late[:3], deferred)),
          "batch-incomplete=1/2, stop#1=never-called")
    check("`at_s` crosses the wire — the driver has to know which slot, and nothing else",
          wire_hook(deferred[1]), {"api": "stop", "paths": ["$.enabled"], "at_s": 8})
    check("the device is never told what is expected", wire,
          {"api": "insertLocation", "paths": ["$"], "args": {"uuid": "u 1"}})
    check("no `args` key on the wire means ZERO arguments", "args" in wire_hook(hooks[0]), False)
    check("a legacy hook crosses the wire untouched", wire_hook(hooks[1]), hooks[1])

    # The sentinel: `absent` has to come out of score()/report() as a RED row — a metric that
    # can only ever say ok would pass every case above it that expects ok.
    step = {"hooks": hooks, "expect": {}}
    check("expect: {} still scores a step that carries api hooks",
          expectations(step, "cordova"), {"configDropped": "-", "apiAssertions": "ok"})
    check("a step without api hooks gains no row", expectations({"hooks": [hooks[1]], "expect": {"crashes": 0}}, "cordova"),
          {"crashes": 0, "configDropped": "-"})
    out = io.StringIO()
    verdict = report(score(capture(cases[1][1], hooks), expectations(step, "cordova")), stream=out)
    check("the absent sentinel is a red row", (verdict, "MISMATCH" in out.getvalue(), "=> FAIL" in out.getvalue()),
          (False, True, True))

    def refuses(title, mutate):
        step = {"name": "run", "hooks": [json.loads(json.dumps(hooks[2]))], "expect": {}, "platform_expect": {}}
        mutate(step["hooks"][0])
        try:
            _validate_hooks("selftest", step)
        except SystemExit as e:
            check(title, True, True)
            return str(e)
        check(title, "loaded", "refused")

    refuses("a misspelt hook key refuses to load", lambda h: h.update(retruns=h.pop("returns")))
    refuses("a misspelt config key in setConfig args refuses to load",
            lambda h: (h.update(api="setConfig", args={"geolocation": {"distanceFiltr": 1}}), h["returns"]["$"].update(type="object")))
    refuses("an api hook with no `returns` refuses to load", lambda h: h.pop("returns"))
    refuses("an unknown comparator refuses to load", lambda h: h["returns"]["$"].update(equal=0))
    refuses("a three-way type union refuses to load", lambda h: h["returns"]["$"].update(type="number|string|null"))
    refuses("a hook that is both `call` and `api` refuses to load", lambda h: h.update(call="reset"))
    retired = refuses("`at:` on an api hook refuses to load — it counted 4 s ticks", lambda h: h.update(at=3))
    check("... and the refusal names the rename", AT_RETIRED in (retired or ""), True)

    # (WO-027, Tier B) `at_s`: seconds after the first batch completes, gated against the window.
    def gate(at_s, settle=20, after=25):
        step = {"name": "run", "settle": settle, "after": after,
                "hooks": [{"api": "getCount", "at_s": at_s, "returns": {"$": {"type": "number"}}}]}
        try:
            _validate_hooks("selftest", step)
            return "loaded"
        except SystemExit as e:
            return str(e)
    for value in (2, 2.5):
        check("`at_s: %r` loads — seconds, and a fraction is fine" % value, gate(value), "loaded")
    for value in (0, -1, True, "2"):
        check("`at_s: %r` refuses to load" % (value,), "more than 0" in gate(value), True)
    check("`at_s: nan` refuses to load — (Tier C, C4.0) first as not JSON, before the gate sees it",
          "not JSON" in gate(float("nan")), True)
    check("an `at_s` the step's settle + after cannot reach refuses to load, counted from the first batch",
          "s after the first batch completes" in gate(99), True)
    check("the gate needs 12 + at_s + 4 s: at_s 4 loads on 20 s and asks for 20 s on 19",
          (gate(4, 10, 10), "at least 20 s" in gate(4, 10, 9)), ("loaded", True))
    check("... at_s 2.5 loads on 19 s (need 18.5) and not on 18",
          (gate(2.5, 10, 9), gate(2.5, 10, 8) != "loaded"), ("loaded", True))
    legacy = {"at": 3, "call": "reset", "args": "same"}
    try:
        _validate_hooks("selftest", {"name": "run", "hooks": [dict(legacy)]})    # no window: ungated
        check("a legacy {at: 3, call: reset} still loads, tick-counted and ungated", True, True)
    except SystemExit as e:
        check("a legacy {at: 3, call: reset} still loads, tick-counted and ungated", str(e), "loaded")
    check("... and wires unchanged", wire_hook(legacy), {"at": 3, "call": "reset", "args": "same"})

    def arm_gate(decl):
        try:
            _validate_events("selftest", {"settle": 20, "after": 25, "events": [decl]})
            return "loaded"
        except SystemExit as e:
            return str(e)
    watch = {"subscribe": "watchPosition", "args": {"interval": 1000}, "returns": {"$.uuid": {"type": "string"}}}
    check("`at:` on an events: declaration refuses to load, with the rename",
          AT_RETIRED in arm_gate(dict(watch, at=2)), True)
    check("`at_s:` on an events: declaration loads", arm_gate(dict(watch, at_s=4)), "loaded")
    check("an events: `at_s` the window cannot reach refuses to load",
          arm_gate(dict(watch, at_s=40)) != "loaded", True)

    # (WO-027, Tier B) persistGcp, refused where it cannot mean what it says; (Tier C, C7.6) bootGcp,
    # refused everywhere.
    real_load = globals()["_load"]

    def load_with(step, plats, **spec):
        fake = dict({"platforms": plats, "trigger": "none", "steps": [step]}, **spec)
        globals()["_load"] = lambda path: (json.loads(json.dumps(fake))
                                           if path.endswith(os.sep + "selftest-gcp.yaml") else real_load(path))
        try:
            scenario("selftest-gcp")
            return "loaded"
        except SystemExit as e:
            return str(e)
        finally:
            globals()["_load"] = real_load
    old_step = {"name": "run", "hooks": [dict(legacy)], "expect": {"crashes": 0}}
    api_step = {"name": "run", "hooks": [{"api": "getCount", "returns": {"$": {"type": "number"}}}],
                "expect": {"crashes": 0}}
    check("a legacy step and a conformance step both load as they are",
          (load_with(old_step, ["cordova"]), load_with(api_step, platforms())), ("loaded", "loaded"))
    check("`bootGcp` on a legacy or a conformance step refuses to load, naming why",
          [BOOT_GCP_RETIRED in load_with(dict(s, bootGcp=v), p)
           for s, v, p in ((old_step, True, ["cordova"]), (api_step, False, platforms()))], [True, True])
    check("`persistGcp` on a conformance step refuses to load",
          "`persistGcp` on a conformance step" in load_with(dict(api_step, persistGcp=True), platforms()), True)
    check("`persistGcp` on a legacy step loads", load_with(dict(old_step, persistGcp=True), ["cordova"]), "loaded")
    refuses("mixed $ and $err paths refuse to load", lambda h: h["returns"].update({"$err": {"type": "string"}}))
    refuses("`absent` with a sibling comparator refuses to load", lambda h: h["returns"]["$"].update(absent=True))
    refuses("a name the types do not declare refuses to load", lambda h: h.update(api="getCoutn"))
    refuses("`args` on a zero-parameter method refuses to load", lambda h: h.update(args={}))
    refuses("a missing required argument refuses to load", lambda h: h.update(api="destroyLocation"))
    refuses("invoking a property refuses to load", lambda h: h.update(api="logger"))
    refuses("a root `type` the declared return cannot produce refuses to load (void -> boolean)",
            lambda h: (h.update(api="removeListeners"), h["returns"]["$"].update(type="boolean")))
    refuses("a `wire:` enum is held to its wire type (AuthorizationStatus -> string)",
            lambda h: (h.update(api="requestPermission"), h["returns"]["$"].update(type="string")))
    refuses("a comparator's implied type is held to the declared return (equals: true on void)",
            lambda h: (h.update(api="removeListeners"), h["returns"]["$"].clear(), h["returns"]["$"].update(equals=True)))
    refuses("a member with two required parameters cannot be invoked from a hook",
            lambda h: h.update(api="findOrCreateTransistorAuthorizationToken", args={"orgname": "x", "username": "y"}))
    refuses("a property probe is held to object|function (Logger -> string)",
            lambda h: (h.update(api="logger", probe=True), h["returns"]["$"].update(type="string")))
    refuses("a probe cannot assert absence at $ — absence is outcome=absent, not a type",
            lambda h: (h.update(probe=True), h["returns"]["$"].clear(), h["returns"]["$"].update(type="undefined")))
    for title, hook_ in [("a property probe with type object loads",
                          {"api": "logger", "probe": True, "returns": {"$": {"type": "object"}}}),
                         ("an enum-namespace probe loads", {"api": "LogLevel", "probe": True, "returns": {"$": {"type": "object"}}}),
                         ("a method probe with type function loads",
                          {"api": "registerHeadlessTask", "probe": True, "returns": {"$": {"type": "function"}}}),
                         ("an optional first argument may be omitted", {"api": "reset", "returns": {"$": {"type": "object"}}})]:
        try:
            _validate_hooks("selftest", {"hooks": [hook_]})
            check(title, True, True)
        except SystemExit as e:
            check(title, str(e), "loaded")
    saved = globals()["_VOCAB"]
    globals()["_VOCAB"] = {"api": {"destroyLocations": {"returns": "void", "wire": "null|boolean"}}, "config": {}}
    try:
        refuses("a `wire:` on a return the types already map refuses to load (the escape hatch)",
                lambda h: (h.update(api="destroyLocations"), h["returns"]["$"].update(type="boolean")))
    finally:
        globals()["_VOCAB"] = saved
    try:
        _validate_config("selftest", {"geolocation": {"distanceFiler": 10}}, vocabulary()["config"])
        check("a misspelt config key refuses to load", "loaded", "refused")
    except SystemExit:
        check("a misspelt config key refuses to load", True, True)
    try:
        _validate_config("selftest", {"app": {"notification": {"title": "x"}}, "http": {"url": ""}, "reset": True},
                         vocabulary()["config"])
        check("nested and scalar config keys the types declare load", True, True)
    except SystemExit as e:
        check("nested and scalar config keys the types declare load", str(e), "loaded")
    refuses("a `matches` that is not a regex refuses to load", lambda h: h["returns"]["$"].update(matches="("))
    import datetime
    refuses("a bare YAML date refuses to load", lambda h: h.update(args=datetime.date(2024, 1, 15)))

    # (Tier C, C0) State paths resolve against the vocabulary's State tree.  Each refusal is held to
    # its own reason: a refusal for the wrong reason would pass a bare "it refused".
    def refused_hook(title, hook, reason, excused=()):
        try:
            _validate_hooks("selftest", {"hooks": [hook]}, excused)
            check(title, "loaded", "refused")
        except SystemExit as e:
            check(title, reason in str(e) or str(e), True)
    def refused_for(title, returns, reason, excused=()):
        refused_hook(title, {"api": "getState", "returns": returns}, reason, excused)
    refused_for("(Tier C) an undeclared State path refuses to load (a typo)",
                {"$.geolocation.distanceFiltr": {"type": "number"}}, "declares no such path")
    refused_for("(Tier C) a flat copy of a compound key refuses to load",
                {"$.distanceFilter": {"type": "number"}}, "declares no such path")
    refused_for("(Tier C) a key State omits refuses to load (reset is an input only)",
                {"$.reset": {"type": "boolean"}}, "State omits it")
    refused_for("(Tier C) a type the State key cannot produce refuses to load (number -> string)",
                {"$.geolocation.distanceFilter": {"type": "string"}}, "launder")
    refused_for("(Tier C) an implied type is held to the State key (equals 'x' on a boolean)",
                {"$.enabled": {"equals": "x"}}, "launder")
    refused_for("(Tier C) an enum key is held to its values' type (LogLevel -> string)",
                {"$.logger.logLevel": {"type": "string"}}, "launder")
    refused_for("(Tier C) a path below a scalar refuses to load",
                {"$.enabled.value": {"type": "boolean"}}, "does not step into it")
    refused_hook("(Tier C) `type: …|missing` refuses to load — it can never pass (a non-root, non-State path)",
                 {"api": "getLocations", "returns": {"$.0.sample": {"type": "string|missing"}}}, "can never pass")
    refused_for("(Tier C) `.length` into a union that is also a string refuses (triggerActivities is stored joined)",
                {"$.activity.triggerActivities.length": {"type": "number"}}, "does not step into it")
    step_ok = {"hooks": [{"api": "getState", "returns": {
        "$.geolocation.distanceFilter": {"type": "number"}, "$.logger.logLevel": {"equals": 5},
        "$.geolocation.filter": {"type": "object"}, "$.trackingMode": {"type": "number"},
        "$.app.schedule.0": {"type": "string"}, "$.app.schedule.length": {"type": "number"},
        "$.activity.triggerActivities": {"type": "array|string"},
        "$.persistence.extras.source": {"type": "string"}, "$.geolocation.locationAuthorizationRequest": {"equals": "Always"},
        "$.enabled": {"absent": True}}}]}
    try:
        _validate_hooks("selftest", step_ok)
        check("(Tier C) declared State paths load: leaf, enum, group, State key, array element/length, "
              "a union, below an open key, a string-literal union, absent", True, True)
    except SystemExit as e:
        check("(Tier C) declared State paths load", str(e), "loaded")
    try:
        _validate_events("selftest", {"settle": 20, "after": 25, "events": [
            {"subscribe": "onSchedule", "returns": {"$.enabledd": {"type": "boolean"}}}]})
        check("(Tier C) an event emitting State is held to the State tree", "loaded", "refused")
    except SystemExit:
        check("(Tier C) an event emitting State is held to the State tree", True, True)
    check("(Tier C) ts_wire maps the declared types",
          [sorted(ts_wire(t)) for t in ("number", "TriggerActivity[] | TriggerActivity", "DesiredAccuracy",
                                         "'Always' | 'WhenInUse' | 'Any'", "Record<string, string>", "number | null",
                                         "string | undefined", "any", "'a | b'", "(A | B)[]", "readonly string[]")],
          [["number"], ["array", "string"], ["number"], ["string"], ["object"], ["null", "number"],
           ["string", "undefined"], [OPEN], ["string"], ["array"], ["array"]])
    try:
        ts_wire("PermissionRationale")
        check("(Tier C) a type labkit does not know refuses instead of becoming an object", "object", "refused")
    except SystemExit as e:
        check("(Tier C) a type labkit does not know refuses instead of becoming an object",
              "no type labkit knows" in str(e), True)
    # (Tier C, C0 review) An interface-typed key is a group: its attributes are declared, a typo below
    # it is refused.  (Android-only, so the scope check needs the iOS excuse to reach the path check.)
    def loads(title, returns, excused=()):
        try:
            _validate_hooks("selftest", {"hooks": [{"api": "getState", "returns": returns}]}, excused)
            check(title, True, True)
        except SystemExit as e:
            check(title, str(e), "loaded")
    refused_for("(Tier C) a typo below an interface-typed key refuses (backgroundPermissionRationale)",
                {"$.app.backgroundPermissionRationale.titel": {"type": "string"}}, "declares no such path", {"ios"})
    refused_for("(Tier C) a wrong type below an interface-typed key refuses",
                {"$.app.backgroundPermissionRationale.title": {"equals": 5}}, "launder", {"ios"})
    loads("(Tier C) an interface-typed key's declared attribute loads",
          {"$.app.backgroundPermissionRationale.title": {"type": "string"}}, {"ios"})
    # A Record's value type is declared: any one key, `length` included, yields it.
    refused_for("(Tier C) a Record<string, string> value is held to string (equals 5)",
                {"$.http.headers.authorization": {"equals": 5}}, "launder")
    refused_for("(Tier C) `.length` on a map is a key, not a length (type number refuses)",
                {"$.http.headers.length": {"type": "number"}}, "launder")
    loads("(Tier C) a Record value of its declared type loads; below Record<string, any> is open",
          {"$.http.headers.authorization": {"type": "string"}, "$.persistence.extras.a.b": {"equals": 5}})
    refused_for("(Tier C) a non-canonical array index refuses (`.00`: JavaScript and Dart read it differently)",
                {"$.app.schedule.00": {"type": "string"}}, "does not step into it")
    # A closed key (an enum, a literal union) is held to its values; one that also admits an array is not.
    refused_for("(Tier C) an enum key refuses an `equals` none of its values (HttpMethod put)",
                {"$.http.method": {"equals": "put"}}, "none of its values")
    refused_for("(Tier C) a numeric enum refuses a value outside it (LogLevel 99)",
                {"$.logger.logLevel": {"equals": 99}}, "none of its values")
    refused_for("(Tier C) a string-literal union refuses a value outside it (always)",
                {"$.geolocation.locationAuthorizationRequest": {"equals": "always"}}, "none of its values")
    refused_for("(Tier C) an `equals` list on an enum key refuses instead of crashing",
                {"$.http.method": {"equals": ["PUT"]}}, "none of its values")
    loads("(Tier C) a key that also admits an array is typed, not value-checked (the joined triggerActivities)",
          {"$.activity.triggerActivities": {"equals": "in_vehicle,on_bicycle"}, "$.http.method": {"equals": "PUT"}})
    # Chris's decision (2): a one-OS key only in a scenario that excuses the other OS.
    refused_for("(Tier C) an Android-only key asserted on both OSes refuses",
                {"$.app.notification.title": {"type": "string"}}, "[Android only]")
    refused_for("(Tier C) an iOS-only key asserted on both OSes refuses",
                {"$.geolocation.activityType": {"type": "number"}}, "[iOS only]")
    refused_for("(Tier C) a group whose attributes are all Android-only is Android-only",
                {"$.app.notification": {"type": "object"}}, "[Android only]")
    refused_for("(Tier C) excusing the SAME OS does not excuse a one-OS key",
                {"$.app.notification.title": {"type": "string"}}, "[Android only]", {"android"})
    loads("(Tier C) a one-OS key loads where the other OS is excused",
          {"$.app.notification.title": {"type": "string"}, "$.app.enableHeadless": {"type": "boolean"}}, {"ios"})
    check("(Tier C) a mixed group is both OSes'", (state_scope("$.app"), state_scope("$.geolocation.filter")), (None, None))
    def load_scoped(step, not_applicable):
        fake = {"platforms": platforms(), "trigger": "none", "steps": [step], "not_applicable": not_applicable}
        globals()["_load"] = lambda path: (json.loads(json.dumps(fake))
                                           if path.endswith(os.sep + "selftest-scope.yaml") else real_load(path))
        try:
            scenario("selftest-scope")
            return "loaded"
        except SystemExit as e:
            return str(e)
        finally:
            globals()["_load"] = real_load
    one_os = {"name": "run", "hooks": [{"api": "getState", "returns": {"$.app.notification.title": {"type": "string"}}}],
              "expect": {"crashes": 0}}
    check("(Tier C) scenario(): an Android-only State path with no excuse refuses to load",
          "[Android only]" in load_scoped(one_os, {}), True)
    check("(Tier C) scenario(): the same path loads where a dated not_applicable excuses iOS",
          load_scoped(one_os, {"ios": "2026-09-25 — Android-only attribute"}), "loaded")
    on_event = {"name": "run", "events": [{"subscribe": "onSchedule", "returns": {"$.app.notification.title": {"type": "string"}}}],
                "expect": {"crashes": 0}}
    check("(Tier C) scenario(): an undated excuse does not count (events-only, `ios: null` still runs on iOS)",
          "[Android only]" in load_scoped(on_event, {"ios": None}), True)
    saved_enums = vocabulary()["enums"]["LogLevel"]
    vocabulary()["enums"]["LogLevel"] = {"wire": "number"}
    try:
        refused_for("(Tier C) a malformed `enums:` entry refuses instead of raising KeyError",
                    {"$.logger.logLevel": {"equals": 5}}, "needs `wire`")
    finally:
        vocabulary()["enums"]["LogLevel"] = saved_enums
    check("(Tier C) the State tree omits reset and the token and carries State's own keys",
          ("reset" in state_tree(), "transistorAuthorizationToken" in state_tree(), "didDeviceReboot" in state_tree()),
          (False, False, True))

    # (Tier C, C2) `declared:` generates a `type:` per State leaf of its scope.  What each scope SHOULD
    # generate is counted here straight from vocabulary.yaml's `scope:` lists, not from state_scope,
    # so a drift between the two is a failure rather than a tautology.
    scoped = vocabulary().get("scope") or {}
    all_leaves = []
    def leaf_walk(node, prefix):
        for k, v in node.items():
            leaf_walk(v, prefix + k + ".") if isinstance(v, dict) else all_leaves.append(prefix + k)
    leaf_walk(state_tree(), "$.")
    def listed_os(path):            # the leaf, or a group above it, listed under `scope:`
        return next((o for o in ("android", "ios") for s in scoped.get(o) or []
                     if path == "$." + s or path.startswith("$." + s + ".")), None)
    want_n = {v: sum(listed_os(p) == (None if v == "both" else v) for p in all_leaves) for v in DECLARED}
    excuse_for = {"both": (), "android": {"ios"}, "ios": {"android"}}
    def declared_hook(value, returns=None, **extra):
        hook = dict({"api": "getState", "declared": value}, **extra)
        if returns is not None:
            hook["returns"] = returns
        _validate_hooks("selftest", {"hooks": [hook]}, excuse_for.get(value, ()))
        return hook
    def refused_declared(title, reason, value="both", excused=None, **extra):
        hook = dict({"api": "getState", "declared": value}, **extra)
        try:
            _validate_hooks("selftest", {"hooks": [hook]}, excuse_for.get(value, ()) if excused is None else excused)
            check(title, "loaded", "refused")
        except SystemExit as e:
            check(title, reason in str(e) or str(e), True)
    try:
        expanded = {v: declared_hook(v)["returns"] for v in DECLARED}
        check("(Tier C, C2) each scope generates one path per State leaf the vocabulary gives it "
              "(both = no `scope:` entry)", {v: len(expanded[v]) for v in DECLARED}, want_n)
        check("(Tier C, C2) ... and the three scopes partition the tree: every leaf once, no group",
              sorted(p for v in DECLARED for p in expanded[v]), sorted(all_leaves))
        # (Tier C, C5.1) Decision 4 A: and a closed leaf's `in:`, every value its type has — the type's, never a config's.
        check("(Tier C, C2; C5.1) a generated check is a `type` and, on a closed leaf, `in:` its type's values — never "
              "one value",
              ({tuple(c) for v in DECLARED for c in expanded[v].values()},
               all(set(c["in"]) == ts_values(_state_leaf(p)) for v in DECLARED for p, c in expanded[v].items() if "in" in c)),
              ({("type",), ("type", "in")}, True))
        check("(Tier C, C2) generated types are the C0 wire exactly (a Record -> object, a joined union two)",
              [expanded[v].get(p) for v, p in [("both", "$.geolocation.distanceFilter"), ("both", "$.logger.logLevel"),
                                                ("both", "$.activity.triggerActivities"), ("both", "$.http.headers"),
                                                ("both", "$.trackingMode"), ("android", "$.app.notification.strings"),
                                                ("ios", "$.geolocation.activityType")]],
              [{"type": "number"}, {"type": "number", "in": [0, 1, 2, 3, 4, 5]}, {"type": "array|string"},
               {"type": "object"}, {"type": "number", "in": [0, 1]}, {"type": "object"},
               {"type": "number", "in": [1, 2, 3, 4, 5]}])
        check("(Tier C, C2) every generated type matches state_path_wire (an open key -> object)",
              [p for v in DECLARED for p, c in expanded[v].items() if c["type"] != (
                  "object" if state_path_wire(p) is OPEN else "|".join(sorted(state_path_wire(p))))], [])
    except SystemExit as e:
        check("(Tier C, C2) each scope expands and loads", str(e), "loaded")
    # Explicit returns on the same hook win; a path outside the scope is kept; the explicit ones come first.
    try:
        mixed = declared_hook("both", {"$.enabled": {"equals": True}, "$.geolocation.filter": {"type": "object"}})["returns"]
        check("(Tier C, C2) an explicit path wins over its generated one, and one outside the scope is kept",
              (mixed["$.enabled"], len(mixed), list(mixed)[:2]),
              ({"equals": True}, want_n["both"] + 1, ["$.enabled", "$.geolocation.filter"]))
    except SystemExit as e:
        check("(Tier C, C2) an explicit path wins over its generated one", str(e), "loaded")
    refused_declared("(Tier C, C2) an explicit path on a declared hook is still held to State (equals 'x' on a boolean)",
                     "launder", returns={"$.enabled": {"equals": "x"}})
    refused_declared("(Tier C, C2) a declared hook asserts a resolve: an explicit $err path refuses",
                     "mixes $ and $err", returns={"$err": {"type": "string"}})
    # A mapping shared by a YAML anchor stays the other hook's own: the expansion copies it.
    shared = {"$.enabled": {"type": "boolean"}}
    try:
        pair = [{"api": "getState", "returns": shared}, {"api": "getState", "declared": "both", "returns": shared}]
        _validate_hooks("selftest", {"hooks": pair})
        check("(Tier C, C2) a `returns` shared by a YAML anchor: only the declared hook gains the generated paths",
              (len(pair[0]["returns"]), len(pair[1]["returns"]), len(shared)), (1, want_n["both"], 1))
    except SystemExit as e:
        check("(Tier C, C2) a `returns` shared by a YAML anchor loads", str(e), "loaded")
    # A State leaf the vocabulary gives no type refuses by name, not with a TypeError.
    real_wire = state_path_wire
    globals()["state_path_wire"] = lambda path: None if path == "$.enabled" else real_wire(path)
    try:
        refused_declared("(Tier C, C2) a State leaf with no type in the vocabulary refuses, naming it",
                         "no type for State's $.enabled")
    finally:
        globals()["state_path_wire"] = real_wire
    # Each refusal, held to its own reason.
    refused_declared("(Tier C, C2) `declared: all` refuses — both, android or ios", "is both, android or ios", "all")
    refused_declared("(Tier C, C2) `declared: yes` (a YAML boolean) refuses", "is both, android or ios", True)
    refused_declared("(Tier C, C2) `declared` on a member that does not return State refuses (getCount -> number)",
                     "only a member that returns or emits one of those", api="getCount")
    refused_declared("(Tier C, C2) `declared` on a probe refuses", "on a probe", probe=True)
    refused_declared("(Tier C, C2) `declared: android` with iOS not excused refuses (C0's scope check)",
                     "[Android only]", "android", ())
    refused_declared("(Tier C, C2) `declared: ios` with only iOS excused refuses", "[iOS only]", "ios", {"ios"})
    try:
        _validate_hooks("selftest", {"hooks": [{"at": 3, "call": "reset", "declared": "both"}]})
        check("(Tier C, C2) `declared` on a legacy call hook is an unknown key", "loaded", "refused")
    except SystemExit as e:
        check("(Tier C, C2) `declared` on a legacy call hook is an unknown key", "unknown key(s) declared" in str(e), True)
    # Through scenario(): the dated-excuse rule, the hook list, the wire, and a re-score of its own capture.
    def with_declared(steps, not_applicable, fn):
        fake = {"platforms": platforms(), "trigger": "none", "steps": steps, "not_applicable": not_applicable}
        globals()["_load"] = lambda path: (json.loads(json.dumps(fake))
                                           if path.endswith(os.sep + "selftest-declared.yaml") else real_load(path))
        try:
            return fn()
        except SystemExit as e:
            return str(e)
        finally:
            globals()["_load"] = real_load
    load_hooks = lambda: scenario("selftest-declared")["steps"][0]["hooks"]
    two = [{"name": "run", "expect": {"crashes": 0},
            "hooks": [{"api": "getCount", "returns": {"$": {"type": "number"}}}, {"api": "getState", "declared": "both"}]}]
    hooks_both = with_declared(two, {}, load_hooks)
    if isinstance(hooks_both, str):
        check("(Tier C, C2) scenario(): a `declared: both` hook loads", hooks_both, "loaded")
    else:
        check("(Tier C, C2) scenario(): a `declared: both` hook loads with its generated paths, and adds no hook",
              (len(hooks_both), hooks_both[1]["api"], len(hooks_both[1]["returns"])), (2, "getState", want_n["both"]))
        check("(Tier C, C2) the device is told the generated paths — never `declared`, never a type",
              wire_hook(hooks_both[1]), {"api": "getState", "paths": list(expanded["both"])})
        recorded = json.loads(json.dumps(hooks_both))
        rescore = lambda hooks_: with_declared(two, {}, lambda: apiAssertions(
            capture([], hooks_, scenario="selftest-declared", step="run")))
        check("(Tier C, C2) a capture recorded with the expansion re-scores against the re-loaded scenario",
              rescore(recorded) != "spec-changed-since-capture", True)
        stale = json.loads(json.dumps(recorded))
        stale[1]["returns"].pop("$.enabled")
        check("(Tier C, C2) ... and one recorded before the vocabulary gained a key is a changed question",
              rescore(stale), "spec-changed-since-capture")
    android_step = [{"name": "run", "expect": {"crashes": 0}, "hooks": [{"api": "getState", "declared": "android"}]}]
    check("(Tier C, C2) scenario(): `declared: android` with no excuse refuses to load",
          "[Android only]" in with_declared(android_step, {}, load_hooks), True)
    check("(Tier C, C2) scenario(): an undated `ios:` excuse does not count",
          "[Android only]" in with_declared(android_step, {"ios": None}, load_hooks), True)
    android_hooks = with_declared(android_step, {"ios": "2026-09-25 — Android-only attributes"}, load_hooks)
    check("(Tier C, C2) scenario(): `declared: android` loads where a dated not_applicable excuses iOS",
          len(android_hooks[0]["returns"]) if isinstance(android_hooks, list) else android_hooks, want_n["android"])
    # summarize.py --coverage --leaves reads scenario(), so it counts a generated path as typed.  It
    # imports its own `labkit` (this file runs as __main__), so that module's HARNESS is the one pointed
    # at a scratch scenarios/ holding the one declared scenario.
    import contextlib
    import summarize
    summarize.labkit.vocabulary()                  # cached before HARNESS moves
    tmp = tempfile.mkdtemp(prefix="labkit-declared-")
    harness_was = summarize.labkit.HARNESS
    try:
        os.makedirs(os.path.join(tmp, "scenarios"))
        with open(os.path.join(tmp, "scenarios", "selftest-declared.yaml"), "w") as f:
            yaml.safe_dump({"platforms": platforms(), "trigger": "none", "steps": two}, f)
        summarize.labkit.HARNESS = tmp
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            summarize.coverage_leaves()
        head = re.search(r"typed (\d+), valued (\d+)", out.getvalue())
        rows = {l.split()[0]: l.split() for l in out.getvalue().splitlines()[2:]}
        check("(Tier C, C2) coverage --leaves counts every generated path as typed, none as valued",
              (int(head.group(1)), int(head.group(2))) if head else out.getvalue()[:200], (want_n["both"], 0))
        check("(Tier C, C2) ... by the declared hook, and an Android-only leaf stays untyped",
              (rows.get("geolocation.distanceFilter", [])[-1:], rows.get("app.notification.title", [])[-2:]),
              (["selftest-declared:getState#1"], ["-", "-"]))
    except SystemExit as e:
        check("(Tier C, C2) coverage --leaves reads a declared scenario", str(e), "read")
    finally:
        summarize.labkit.HARNESS = harness_was
        shutil.rmtree(tmp)

    # ------------------------------------------------------------ payload types (Tier C, C4.0)
    # Every path below a non-State root is held to the vocabulary's `types:` (surface H5), `$err` to the
    # declared rejection type; `declared:` generates a payload's leaves; `optional:` and `under:` let an
    # absent `?` field, an absent `?` object and an empty array pass without passing a missing required
    # leaf or a null.  Each refusal is held to its own reason.
    def load(hooks=(), events=()):
        try:
            _validate_hooks("selftest", {"hooks": list(hooks)})
            _validate_events("selftest", {"settle": 20, "after": 25, "events": list(events)})
            return "loaded"
        except SystemExit as e:
            return str(e)
    gcp = lambda returns, **extra: dict({"api": "getCurrentPosition", "returns": returns}, **extra)
    check("(Tier C, C4.0) typed payload paths load: Location, Geofence (a vertex's element and length: a tuple walks "
          "as an array), a Geofence[] element and length, a Record's key, an enum's value, a `?` field `optional`, an "
          "emitted GeofenceEvent and GeofencesChangeEvent",
          load([gcp({"$.coords.latitude": {"type": "number"}, "$.uuid": {"type": "string"},
                     "$.activity.type": {"equals": "still"}, "$.extras.anything": {"type": "string"}}),
                {"api": "getGeofence", "args": "x", "returns": {"$.extras.source": {"equals": "lab"},
                                                                "$.notifyOnExit": {"equals": False},
                                                                "$.vertices.0.0": {"type": "number"},
                                                                "$.vertices.0.length": {"equals": 2}}},
                {"api": "getGeofences", "returns": {"$.length": {"equals": 0}, "$.0.identifier": {"type": "string"}}},
                {"api": "getSensors", "returns": {"$.significant_motion": {"type": "boolean", "optional": True}}}],
               [{"subscribe": "onGeofence", "returns": {"$.action": {"equals": "ENTER"},
                                                        "$.location.geofence.identifier": {"type": "string"},
                                                        "$.geofence.identifier": {"type": "string"}}},
                {"subscribe": "onGeofencesChange", "returns": {"$.on.0.identifier": {"type": "string"}}}]), "loaded")
    for title, hooks_, events_, reason in [
            ("a typo'd Location path refuses to load", [gcp({"$.coordz.latitude": {"type": "number"}})], [],
             "`Location` declares no such path"),
            ("a typo two types down refuses (MotionChangeEvent -> Location -> Coords)", [],
             [{"subscribe": "onMotionChange", "returns": {"$.location.coords.lat": {"type": "number"}}}],
             "`MotionChangeEvent` declares no such path"),
            ("a type the payload leaf cannot produce refuses (Coords.latitude -> string)",
             [gcp({"$.coords.latitude": {"type": "string"}})], [], "launder"),
            ("an enum field refuses an `equals` none of its values (GeofenceAction ENTRY)", [],
             [{"subscribe": "onGeofence", "returns": {"$.action": {"equals": "ENTRY"}}}], "none of its values"),
            ("a scalar return takes no path (getCount -> number)",
             [{"api": "getCount", "returns": {"$.value": {"type": "number"}}}], [], "does not step into it"),
            ("an emitted scalar takes no path (onEnabledChange -> boolean)", [],
             [{"subscribe": "onEnabledChange", "returns": {"$.value": {"type": "boolean"}}}], "does not step into it"),
            ("a tuple's `.N` stops at its arity (a vertex's `.2`)",
             [{"api": "getGeofence", "args": "x", "returns": {"$.vertices.0.2": {"type": "number"}}}], [],
             "a tuple an `.N` below its arity"),
            ("a `void` return takes no path (stopBackgroundTask)",
             [{"api": "stopBackgroundTask", "args": 1, "returns": {"$.x": {"type": "number"}}}], [],
             "the types declare `void`, no value — only `$`"),
            ("(Tier C, C5.0) a seeded emit holds its paths: HeartbeatEvent has a location, not bare coords", [],
             [{"subscribe": "onHeartbeat", "returns": {"$.coords.latitude": {"type": "number"}}}],
             "`HeartbeatEvent` declares no such path"),
            ("(Tier C, C5.0) ... and its closed values: LocationFilterEvent.reason is a LocationFilterReason", [],
             [{"subscribe": "onLocationFilter", "returns": {"$.reason": {"equals": "too-fast"}}}],
             "none of its values"),
            ("`optional` on a field the types declare required refuses (Coords.latitude)",
             [gcp({"$.coords.latitude": {"type": "number", "optional": True}})], [], "declares required"),
            ("`optional` on a State attribute refuses (decision (1): absence is red)",
             [{"api": "getState", "returns": {"$.enabled": {"type": "boolean", "optional": True}}}], [],
             "declares required"),
            ("`optional` standing alone refuses — it would pass a null", [gcp({"$.coords.floor": {"optional": True}})],
             [], "qualify a comparator"),
            ("`optional` on a root refuses", [{"api": "getCount", "returns": {"$": {"type": "number", "optional": True}}}],
             [], "on a root"),
            ("... on `$err` too", [gcp({"$err": {"type": "number", "optional": True}})], [], "on a root"),
            ("`optional` on an array's `.length` refuses — it would pass an object where the array is declared",
             [{"api": "getGeofences", "returns": {"$.length": {"type": "number", "optional": True}}}], [],
             "an array's `.length`, which every array has"),
            ("`optional: yes` refuses", [gcp({"$.coords.floor": {"type": "number", "optional": "yes"}})], [],
             "true, or left out"),
            ("`under:` a path that is not an ancestor refuses",
             [gcp({"$.provider": {"type": "object", "optional": True}, "$.uuid": {"type": "string", "under": "$.provider"}})],
             [], "strict ancestor"),
            ("`under:` the path itself refuses — the path would excuse its own failure",
             [gcp({"$.coords.floor": {"type": "number", "under": "$.coords.floor"}})], [], "strict ancestor"),
            ("`under:` a prefix that is no segment refuses ($.coordsX under $.coords)",
             [gcp({"$.coords": {"type": "object"}, "$.coordsX": {"type": "number", "under": "$.coords"}})], [],
             "strict ancestor"),
            ("`under:` a container the hook does not report refuses",
             [gcp({"$.provider.enabled": {"type": "boolean", "under": "$.provider"}})], [], "strict ancestor"),
            ("`under:` on an event is held to the same rule", [],
             [{"subscribe": "onGeofence", "returns": {"$.geofence.identifier": {"type": "string", "under": "$.geofence"}}}],
             "strict ancestor"),
            ("`declared` on an Array<Object> member refuses (getLocations; C4 decision 17)",
             [{"api": "getLocations", "declared": "both"}], [], "only a member that returns or emits one of those"),
            ("`declared` on a void member refuses (removeListeners)",
             [{"api": "removeListeners", "declared": "both"}], [], "only a member that returns or emits one of those"),
            ("`declared` on an event that emits a scalar refuses (onEnabledChange)", [],
             [{"subscribe": "onEnabledChange", "declared": "both"}], "only a member that returns or emits one of those"),
            ("`declared: android` on a payload refuses — a payload records no one-OS scope",
             [{"api": "getSensors", "declared": "android"}], [], "records no one-OS scope"),
            ("an explicit path on a declared payload is still held to its type (equals 'x' on a number)",
             [gcp({"$.coords.latitude": {"equals": "x"}}, declared="both")], [], "launder"),
            # (item 5) `$err` against the declared rejection type
            ("`$err` is held to the declared rejection (getCurrentPosition rejects a LocationError, a number)",
             [gcp({"$err": {"type": "string"}})], [], "launder"),
            ("`$err` refuses an `equals` outside the rejection enum (LocationError 7)",
             [gcp({"$err": {"equals": 7}})], [], "none of its values"),
            ("... and on requestPermission (AuthorizationStatus 9)",
             [{"api": "requestPermission", "args": "location", "returns": {"$err": {"equals": 9}}}], [],
             "none of its values"),
            ("a `$err` sub-path the rejection type does not have refuses (LocationError.code)",
             [gcp({"$err.code": {"type": "number"}})], [], "does not step into it"),
            ("a `$err` sub-path where no rejection type is declared refuses (getGeofence; C4 decision 4)",
             [{"api": "getGeofence", "args": "x", "returns": {"$err.message": {"type": "string"}}}], [],
             "no rejection type"),
            ("... and so does the `$err` root there — WO-054's object|string union is `outcome: rejected` (C4 decision 4)",
             [{"api": "getGeofence", "args": "x", "returns": {"$err": {"type": "object|string"}}}], [],
             "no rejection type")]:
        got = load(hooks_, events_)
        check("(Tier C, C4.0) %s" % title, reason in got or got, True)
    check("(Tier C, C4.0) `$err` loads against its rejection type (permission-request-location's 2); getLocations' "
          "records stay unchecked (C4 decision 17)",
          load([{"api": "requestPermission", "args": "location", "returns": {"$err": {"type": "number", "equals": 2}}},
                gcp({"$err": {"type": "number", "equals": 408}}),
                {"api": "getLocations", "returns": {"$.first.uuid": {"type": "boolean"}}}]), "loaded")
    gone = {"type": "missing", "value": "", "parent": True, "trunc": False}
    check("(Tier C, C4.0) a qualifier leaves `same:` alone: a key missing on both readings is unchanged, `under:` or not",
          [_check(gone, {"same": "getState#0"}, lambda ref: gone),
           _check(gone, {"same": "getState#0", "under": "$.geolocation.filter"}, lambda ref: gone)], [None, None])
    check("(Tier C, C4.0) `X | null` names X — stepped into as X — and a union of two interfaces names none",
          (payload_type("Coords | null") is payload_type("Coords"), payload_type("Coords") is not None,
           payload_type("Coords | Battery")), (True, True, None))
    # The expansion: the type from the vocabulary, a `?` leaf optional, a `?` object's leaves under it, an
    # array's `.0` a `?` object of its own, a Record not walked, `types.omit` left out, explicit paths first.
    declared_gcp = gcp({"$.coords.latitude": {"type": "number", "gte": -90}}, declared="both")
    declared_fences = {"api": "getGeofences", "declared": "both"}
    declared_mc = {"subscribe": "onMotionChange", "declared": "both"}
    declared_gc = {"subscribe": "onGeofencesChange", "declared": "both"}
    got = load([declared_gcp, declared_fences], [declared_mc, declared_gc])
    if got != "loaded":
        check("(Tier C, C4.0) `declared: both` expands a Location, a Geofence[], a MotionChangeEvent and a "
              "GeofencesChangeEvent", got, "loaded")
    else:
        gen, fences, mc, gc = (declared_gcp["returns"], declared_fences["returns"], declared_mc["returns"],
                               declared_gc["returns"])
        check("(Tier C, C4.0) a Location expands leaf by leaf: required typed, `?` optional, a `?` object under "
              "itself, a union kept, a Record not walked, an explicit path first and winning",
              ([gen.get(p) for p in ("$.coords.longitude", "$.coords.floor", "$.provider", "$.provider.enabled",
                                     "$.geofence.action", "$.recorded_at", "$.extras")], list(gen)[0], gen["$.coords.latitude"],
               [p for p in gen if p.startswith("$.extras.") or p in ("$.coords", "$.battery", "$.activity")]),
              # (WO-067) coords.floor is `number | null`: iOS sends null where it has no floor, Android no key
              ([{"type": "number"}, {"type": "null|number", "optional": True}, {"type": "object", "optional": True},
                {"type": "boolean", "under": "$.provider"},
                {"type": "string", "in": ["DWELL", "ENTER", "EXIT"], "under": "$.geofence"},    # (Tier C, C5.1)
                {"type": "number|string"}, {"type": "object", "optional": True}], "$.coords.latitude",
               {"type": "number", "gte": -90}, []))
        check("(Tier C, C4.0) types.omit is left out (Location.timestamp, WO-029), at the root and one type down",
              ("$.timestamp" in gen, "$.location.timestamp" in mc, "$.location.recorded_at" in mc), (False, False, True))
        check("(Tier C, C4.0) an array's element is a `?` object and its leaves are under it (getGeofences, and "
              "GeofencesChangeEvent.on, whose element is under the array; `off: string[]` is typed, not walked)",
              (fences.get("$.0"), fences.get("$.0.identifier"), fences.get("$.0.notifyOnDwell"),
               gc.get("$.on"), gc.get("$.on.0"), gc.get("$.on.0.identifier"), gc.get("$.off"), "$.off.0" in gc),
              ({"type": "object", "optional": True}, {"type": "string", "under": "$.0"},
               {"type": "boolean", "optional": True, "under": "$.0"}, {"type": "array"},
               {"type": "object", "optional": True, "under": "$.on"}, {"type": "string", "under": "$.on.0"},
               {"type": "array"}, False))
        check("(Tier C, C4.0) a generated check is a type and its qualifiers — never a value — and every `under` "
              "names a path the same expansion reports",
              ({k for r in (gen, fences, mc, gc) for p, c in r.items() if p != "$.coords.latitude" for k in c},
               [c["under"] for r in (gen, fences, mc, gc) for c in r.values() if c.get("under") not in r.keys() | {None}]),
              ({"type", "optional", "under", "in"}, []))      # (Tier C, C5.1) `in:`: a closed type's own values
        check("(Tier C, C4.0) the device is told the generated paths — never `declared`, a type or a qualifier",
              (wire_hook(declared_gcp), wire_events([declared_mc])[0]),
              ({"api": "getCurrentPosition", "paths": list(gen)},
               {"on": "onMotionChange", "id": "onMotionChange@0", "paths": list(mc)}))
        check("(Tier C, C4.0) the root is typed too: object for an interface, array for a `T[]`",
              [r.get("$") for r in (gen, fences, mc, gc)],
              [{"type": "object"}, {"type": "array"}, {"type": "object"}, {"type": "object"}])
        fl = lambda path, rest: "I/LAB     ( 1): app=ret id=getGeofences#0 path=%s %s" % (path, rest)
        handed = (["I/LAB     ( 1): app=api id=getGeofences#0 outcome=ok", fl("$", "type=object parent=1")] +
                  [fl(p, "type=missing parent=%d" % (p == "$.0")) for p in fences if p != "$"] +
                  ["I/LAB     ( 1): app=api-done n=1"])
        check("(Tier C, C4.0) ... so an object handed where `Geofence[]` is declared reads red, on `$` — its `.0` reads "
              "missing, parent=1, exactly as an empty array's does",
              apiAssertions(capture(handed, [declared_fences])), "getGeofences#0.$=want-array-got-object")
        ml = lambda path, rest: "I/LAB     ( 1): app=ret id=onMotionChange@0 path=%s %s" % (path, rest)
        bare = (["I/LAB     ( 1): app=event-arm id=onMotionChange@0 outcome=ok", "I/LAB     ( 1): app=events-armed n=1",
                 "I/LAB     ( 1): app=event id=onMotionChange@0 n=1", ml("$", "type=object parent=1")] +
                [ml(p, "type=missing parent=%d" % (p.count(".") == 1)) for p in mc if p != "$"])
        check("(Tier C, C4.0) the control's pattern (api-types-control): a bare Location handed to onMotionChange reads "
              "every generated path but `$` missing — a `?` object whose own parent is gone excuses nothing (the C4 plan §2 T)",
              eventAssertions(capture(bare, hooks, [declared_mc])),
              ", ".join("onMotionChange@0.%s=missing" % p for p in mc if p != "$"))
    # Scoring.  A `?` object, its leaf under it, and a `?` scalar; then an array's element.
    box = [{"api": "getCurrentPosition", "returns": {
        "$.provider": {"type": "object", "optional": True},
        "$.provider.enabled": {"type": "boolean", "under": "$.provider"},
        "$.coords.heading": {"type": "number", "optional": True}}}]
    r = lambda path, rest: "I/LAB     ( 1): app=ret id=getCurrentPosition#0 path=%s %s" % (path, rest)
    absent = ["I/LAB     ( 1): app=api id=getCurrentPosition#0 outcome=ok",
              r("$.provider", "type=missing parent=1"), r("$.provider.enabled", "type=missing parent=0"),
              r("$.coords.heading", "type=missing parent=1"), "I/LAB     ( 1): app=api-done n=1"]
    at = lambda path, *new: swap(absent, "path=%s " % path, *new)
    present = swap(absent, "path=$.provider ", r("$.provider", "type=object parent=1"))
    for title, lines, want in [
            ("an absent `?` object excuses its required leaf, and an absent `?` scalar passes", absent, "ok"),
            ("a present `?` object with a required leaf missing reads red",
             swap(present, "path=$.provider.enabled", r("$.provider.enabled", "type=missing parent=1")),
             "getCurrentPosition#0.$.provider.enabled=missing"),
            ("a present `?` object with its leaf typed passes",
             swap(present, "path=$.provider.enabled", r("$.provider.enabled", "type=boolean value=true parent=1")), "ok"),
            ("a null `?` object reads red once, on itself",
             at("$.provider", r("$.provider", "type=null parent=1")), "getCurrentPosition#0.$.provider=want-object-got-null"),
            ("a `?` object that is not an object reads red once, on itself",
             at("$.provider", r("$.provider", "type=string value=x parent=1")),
             "getCurrentPosition#0.$.provider=want-object-got-string"),
            ("null is red under `optional` (the types do not say `| null`)",
             at("$.coords.heading", r("$.coords.heading", "type=null parent=1")),
             "getCurrentPosition#0.$.coords.heading=want-number-got-null"),
            ("`optional` does not pass a missing parent", at("$.coords.heading", r("$.coords.heading", "type=missing parent=0")),
             "getCurrentPosition#0.$.coords.heading=missing"),
            ("a clipped value under `optional` is judged by its type alone, as under a bare `type:`",
             at("$.coords.heading", r("$.coords.heading", "type=number value=3 trunc=1 parent=1")), "ok"),
            ("a `?` object read missing with its own parent gone (parent=0) excuses nothing — its leaf reads missing too",
             at("$.provider", r("$.provider", "type=missing parent=0")),
             "getCurrentPosition#0.$.provider=missing, getCurrentPosition#0.$.provider.enabled=missing"),
            ("a container that reported nothing excuses nothing", at("$.provider"),
             "getCurrentPosition#0.$.provider=unreported, getCurrentPosition#0.$.provider.enabled=missing")]:
        check("(Tier C, C4.0) %s" % title, apiAssertions(capture(lines, box)), want)
    # (WO-067) coords.floor as the vocabulary generates it, `number | null` under `optional`.
    floor = [{"api": "getCurrentPosition", "returns": {"$.coords.floor": {"type": "null|number", "optional": True}}}]
    f = lambda rest: ["I/LAB     ( 1): app=api id=getCurrentPosition#0 outcome=ok", r("$.coords.floor", rest),
                      "I/LAB     ( 1): app=api-done n=1"]
    check("(WO-067) a `number | null` floor passes iOS's null, Android's absent key and a level, and reds a string",
          [apiAssertions(capture(f(rest), floor)) for rest in ("type=null parent=1", "type=missing parent=1",
                                                                 "type=number value=2 parent=1",
                                                                 "type=string value=x parent=1")],
          ["ok", "ok", "ok", "getCurrentPosition#0.$.coords.floor=want-null|number-got-string"])
    element = [{"api": "getGeofences", "returns": {"$.0": {"type": "object", "optional": True},
                                                   "$.0.identifier": {"type": "string", "under": "$.0"}}}]
    e = lambda path, rest: "I/LAB     ( 1): app=ret id=getGeofences#0 path=%s %s" % (path, rest)
    empty = ["I/LAB     ( 1): app=api id=getGeofences#0 outcome=ok", e("$.0", "type=missing parent=1"),
             e("$.0.identifier", "type=missing parent=0"), "I/LAB     ( 1): app=api-done n=1"]
    one = swap(empty, "path=$.0 ", e("$.0", "type=object parent=1"))
    for title, lines, want in [
            ("`$.0.x` of an empty array passes", empty, "ok"),
            ("`$.0.x` of a non-empty array with x missing reads red",
             swap(one, "path=$.0.identifier", e("$.0.identifier", "type=missing parent=1")),
             "getGeofences#0.$.0.identifier=missing"),
            ("`$.0.x` of a non-empty array with x typed passes",
             swap(one, "path=$.0.identifier", e("$.0.identifier", "type=string value=f parent=1")), "ok")]:
        check("(Tier C, C4.0) %s" % title, apiAssertions(capture(lines, element)), want)
    # Recursive: a leaf whose own container passes is still excused when THAT container's container is absent.
    chain = [{"api": "getCurrentPosition", "returns": {
        "$.geofence": {"type": "object", "optional": True},
        "$.geofence.extras": {"type": "object", "optional": True, "under": "$.geofence"},
        "$.geofence.extras.k": {"type": "string", "under": "$.geofence.extras"}}}]
    check("(Tier C, C4.0) `under` is recursive: an absent grand-container excuses a leaf whose container passes",
          apiAssertions(capture(["I/LAB     ( 1): app=api id=getCurrentPosition#0 outcome=ok",
                                 r("$.geofence", "type=missing parent=1"), r("$.geofence.extras", "type=object parent=1"),
                                 r("$.geofence.extras.k", "type=missing parent=1"), "I/LAB     ( 1): app=api-done n=1"],
                                chain)), "ok")
    check("(Tier C, C4.0) ... and as a driver reports it, the container missing below the absent one (parent=0): that "
          "container is excused, so its leaf is",
          apiAssertions(capture(["I/LAB     ( 1): app=api id=getCurrentPosition#0 outcome=ok",
                                 r("$.geofence", "type=missing parent=1"), r("$.geofence.extras", "type=missing parent=0"),
                                 r("$.geofence.extras.k", "type=missing parent=0"), "I/LAB     ( 1): app=api-done n=1"],
                                chain)), "ok")
    # The same rule on an event's delivery.
    fence_decl = [{"subscribe": "onGeofence", "returns": {
        "$.location.geofence": {"type": "object", "optional": True},
        "$.location.geofence.identifier": {"type": "string", "under": "$.location.geofence"}}}]
    ev = lambda path, rest: "I/LAB     ( 1): app=ret id=onGeofence@0 path=%s %s" % (path, rest)
    delivered = ["I/LAB     ( 1): app=event-arm id=onGeofence@0 outcome=ok", "I/LAB     ( 1): app=events-armed n=1",
                 "I/LAB     ( 1): app=event id=onGeofence@0 n=1", ev("$.location.geofence", "type=missing parent=1"),
                 ev("$.location.geofence.identifier", "type=missing parent=0")]
    check("(Tier C, C4.0) an event delivery: an absent `?` object excuses its leaf",
          eventAssertions(capture(delivered, hooks, fence_decl)), "ok")
    check("(Tier C, C4.0) an event delivery: a null `?` object reads red once",
          eventAssertions(capture(swap(delivered, "path=$.location.geofence ",
                                       ev("$.location.geofence", "type=null parent=1")), hooks, fence_decl)),
          "onGeofence@0.$.location.geofence=want-object-got-null")
    # Through scenario(): a declared payload hook and event load, add no hook, and re-score their own capture.
    payload_steps = [{"name": "run", "expect": {"crashes": 0},
                      "hooks": [{"api": "getCurrentPosition", "args": {"samples": 1}, "declared": "both"}],
                      "events": [{"subscribe": "onMotionChange", "declared": "both"}]}]
    step_ = with_declared(payload_steps, {}, lambda: scenario("selftest-declared")["steps"][0])
    if isinstance(step_, str):
        check("(Tier C, C4.0) scenario(): a declared payload hook and event load", step_, "loaded")
    else:
        check("(Tier C, C4.0) scenario(): a declared payload hook and event load with the generated paths",
              (len(step_["hooks"]), sorted(step_["hooks"][0]["returns"]), list(step_["events"][0]["returns"])),
              (1, sorted(declared_gcp["returns"]), list(declared_mc["returns"])))
        recorded = json.loads(json.dumps(step_))
        rescored = with_declared(payload_steps, {}, lambda: [f(capture([], recorded["hooks"], recorded["events"],
                                                                       scenario="selftest-declared", step="run"))
                                                             for f in (apiAssertions, eventAssertions)])
        check("(Tier C, C4.0) ... and a capture recorded with the expansion re-scores against the re-loaded scenario",
              [v == "spec-changed-since-capture" for v in rescored], [False, False])
        stale = json.loads(json.dumps(recorded))
        stale["events"][0]["returns"].pop("$.isMoving")
        check("(Tier C, C4.0) ... and an event recorded before the vocabulary changed is a changed question",
              with_declared(payload_steps, {}, lambda: eventAssertions(capture(
                  [], stale["hooks"], stale["events"], scenario="selftest-declared", step="run"))),
              "spec-changed-since-capture")

    # ------------------------------------------------------ transport guards (Tier C, C4.0)
    # What crosses the wire is JSON a JavaScript driver reads as the lab wrote it, and a step's launch fits the
    # device shell's single argument.  Each refusal is held to its own reason.
    odo = lambda value, **extra: dict({"api": "setOdometer", "args": value, "returns": {"$.odometer": {"type": "number"}}},
                                      **extra)
    for title, hooks_, reason in [
            ("NaN in a hook's args refuses to load — the driver's parse would lose the whole hooksB64",
             [odo(float("nan"))], "not JSON"),
            ("Infinity deep in setConfig's args refuses too",
             [{"api": "setConfig", "args": {"geolocation": {"distanceFilter": float("inf")}},
               "returns": {"$": {"type": "object"}}}], "not JSON"),
            ("NaN in a comparator refuses too — run.json would record a bare NaN",
             [{"api": "getCount", "returns": {"$": {"lt": float("nan")}}}], "not JSON"),
            ("an integral float refuses (10.0 — JavaScript reads 10, Dart a double)", [odo(10.0)], "integral float"),
            ("an integral float deep in a config refuses", [{"api": "setConfig", "args": {"geolocation": {
                "distanceFilter": 10.0}}, "returns": {"$": {"type": "object"}}}], "integral float"),
            ("... and inside an array (addGeofences' radius 200.0)", [{"api": "addGeofences", "args": [
                {"identifier": "f", "latitude": 45.5, "longitude": -73.6, "radius": 200.0, "notifyOnEntry": True}],
                "returns": {"$": {"type": "boolean"}}}], "integral float"),
            ("an integer past 2^53 refuses", [odo(2 ** 53 + 1)], "past 2^53"),
            ("... on either side of zero", [odo(-(2 ** 53) - 1)], "past 2^53"),
            ("a dated `invalid:` excuses no transport guard", [odo(10.0, invalid="2026-09-26 selftest")], "integral float")]:
        got = load(hooks_)
        check("(Tier C, C4.0) %s" % title, reason in got or got, True)
    check("(Tier C, C4.0) a fraction, and 2^53 itself, load", load([odo(10.5), odo(2 ** 53), odo(-(2 ** 53))]), "loaded")
    check("(Tier C, C4.0) an event's args are held to the same guards (watchPosition's interval 1000.0)",
          "integral float" in load([], [dict(watch, args={"interval": 1000.0})]), True)
    big = lambda n: {"name": "run", "config": {"http": {"headers": {"x": "c" * n}}}, "expect": {"crashes": 0},
                     "hooks": [{"api": "insertLocation", "args": {"coords": {"latitude": 1, "longitude": 2}, "note": "h" * n},
                                "returns": {"$": {"type": "string"}}}]}
    over, under = load_with(big(26000), platforms()), load_with(big(20000), platforms())
    check("(Tier C, C4.0) a step whose configB64 + hooksB64 + eventsB64 pass LAUNCH_LIMIT refuses to load — each "
          "under it alone — and one under it loads",
          ("the launch carries" in over and "over the %d B" % LAUNCH_LIMIT in over, under), (True, "loaded"))
    check("(Tier C, C4.0) a step's config value its key's type does not admit refuses — no `invalid:` there — and "
          "so does the scenario's own, which every step inherits",
          ["distanceFilter is a string" in load_with(dict(api_step, config={"geolocation": {"distanceFilter": "10"}}),
                                                     platforms()),
           "scenario selftest-gcp config.geolocation.distanceFilter is a string" in load_with(
               api_step, platforms(), config={"geolocation": {"distanceFilter": "10"}})], [True, True])
    check("(Tier C, C4.0) NaN in an `events:` entry refuses to load — the driver's parse would lose the whole eventsB64",
          "not JSON" in load([], [dict(watch, returns={"$.uuid": {"type": "string"}, "$.odometer": {"lt": float("nan")}})]),
          True)
    check("(Tier C, C4.0) NaN, or an integral float, in a step's config refuses to load",
          ["not JSON" in load_with(dict(api_step, config={"geolocation": {"distanceFilter": float("nan")}}), platforms()),
           "integral float" in load_with(dict(api_step, config={"geolocation": {"distanceFilter": 10.0}}), platforms())],
          [True, True])
    sent = {"config": {"http": {"url": ""}}, "hooks": [odo(1, invalid="x"), legacy],
            "events": [{"subscribe": "onLocation", "min": 2, "returns": {"$.uuid": {"type": "string"}}}]}
    try:
        launch_payloads({"config": {"x": float("nan")}, "hooks": [], "events": []})
        got = "sent"
    except ValueError:
        got = "refused"
    check("(Tier C, C4.0) launch_payloads refuses NaN itself — run.py's last guard, behind scenario()'s", got, "refused")
    check("(Tier C, C4.0) launch_payloads is the step as wire_hook and wire_events tell it, base64'd JSON — the "
          "size scenario() refuses is the size run.py sends",
          {k: json.loads(base64.b64decode(v)) for k, v in launch_payloads(sent).items()},
          {"configB64": sent["config"], "hooksB64": [wire_hook(h) for h in sent["hooks"]],
           "eventsB64": wire_events(sent["events"])})

    # ---------------------------------------------- typed arguments and `invalid:` (Tier C, C4.0)
    # A hook's argument is held to its parameter's declared type — keys, wire types, closed values, required
    # fields; a Config through the config tree — and a required argument must be there.  A dated `invalid:` is
    # the only way past, and refuses where nothing is forbidden.
    fence = {"identifier": "f", "latitude": 45.5, "longitude": -73.6, "radius": 200, "notifyOnEntry": True}
    call = lambda api, args=_ABSENT, **extra: dict(dict({"api": api}, **({} if args is _ABSENT else {"args": args})),
                                                  **dict({"returns": {"$": {"type": "boolean"}}}, **extra))
    state = {"returns": {"$": {"type": "object"}}}
    check("(Tier C, C4.0) arguments of their declared types load: a Geofence and a Geofence[], a LocationInput (its "
          "index signature takes any key), request options, a Permission, a string[], a union, a LocationQuery's "
          "SQLQueryOrder, a Config's enums, literal unions and a TransistorAuthorizationToken, and watchPosition's options",
          load([call("addGeofence", dict(fence, extras={"k": [1, None]}, vertices=[[1.5, 2.5], [3.5, 4.5]])),
                call("addGeofences", [fence, dict(fence, identifier="g")]),
                call("insertLocation", {"coords": {"latitude": 1.5, "longitude": 2.5}, "timestamp": 1, "anything": {}},
                     returns={"$": {"type": "string"}}),
                gcp({"$.uuid": {"type": "string"}}, args={"samples": 1, "persist": False, "extras": {"a": 1}}),
                {"api": "requestPermission", "args": "motion", "returns": {"$": {"type": "number"}}},
                call("removeGeofences", ["a", "b"]),
                {"api": "playSound", "args": "x", "returns": {"$": {"type": "undefined"}}},
                {"api": "playSound", "args": 3, "returns": {"$": {"type": "undefined"}}},
                {"api": "getLocations", "args": {"limit": 2, "order": -1}, "returns": {"$": {"type": "array"}}},
                dict(call("setConfig", {"geolocation": {"desiredAccuracy": -1, "locationAuthorizationRequest": "Always",
                                                         "filter": {"policy": 1}}, "logger": {"logLevel": 5},
                                        "activity": {"triggerActivities": ["in_vehicle"]}, "http": {"headers": {"a": "b"}},
                                        "transistorAuthorizationToken": {"url": "u", "accessToken": "a",
                                                                         "refreshToken": "r", "expires": 1}}), **state),
                dict(call("setConfig", {"activity": {"triggerActivities": "in_vehicle"}}), **state),
                dict(call("reset", {}), **state)],
               [dict(watch, args={"interval": 1000, "desiredAccuracy": 10, "persist": False})]), "loaded")
    for title, hooks_, events_, reason in [
            ("a wrong wire type refuses (destroyLocation(123))", [call("destroyLocation", 123)], [],
             "args is a number — the types declare `string`"),
            ("a key the interface does not declare refuses (a Geofence's `radiuss`)",
             [call("addGeofence", dict(fence, radiuss=1))], [], "args.radiuss: `Geofence` declares no such field"),
            ("a required field left out refuses (a Geofence without latitude)",
             [call("addGeofence", {k: v for k, v in fence.items() if k != "latitude"})], [], "leaves out `latitude`"),
            ("a wrong type one object down refuses (LocationInput.coords.latitude '45')",
             [call("insertLocation", {"coords": {"latitude": "45", "longitude": 1}}, returns={"$": {"type": "string"}})],
             [], "args.coords.latitude is a string"),
            ("an array's bad element is named (addGeofences' second, no radius)",
             [call("addGeofences", [fence, {k: v for k, v in fence.items() if k != "radius"}])], [],
             "args.1 leaves out `radius`"),
            ("a tuple of the wrong arity refuses (a vertex of three)",
             [call("addGeofence", dict(fence, vertices=[[1.5, 2.5, 3.5]]))], [], "args.vertices.0 holds 3"),
            ("an enum argument refuses a value outside it (requestPermission('bogus'))",
             [{"api": "requestPermission", "args": "bogus", "returns": {"$": {"type": "number"}}}], [],
             "`Permission` is none of location, motion"),
            ("an enum field refuses a value outside it (LocationQuery.order 2)",
             [{"api": "getLocations", "args": {"order": 2}, "returns": {"$": {"type": "array"}}}], [], "is none of"),
            ("a config value of the wrong type refuses (distanceFilter '10')",
             [dict(call("setConfig", {"geolocation": {"distanceFilter": "10"}}), **state)], [],
             "args.geolocation.distanceFilter is a string — the types declare `number`"),
            ("a config enum refuses a value outside it (desiredAccuracy 12345)",
             [dict(call("setConfig", {"geolocation": {"desiredAccuracy": 12345}}), **state)], [], "is none of"),
            ("a config null refuses (desiredAccuracy null: WO-059's probe 1 needs `invalid:`)",
             [dict(call("setConfig", {"geolocation": {"desiredAccuracy": None}}), **state)], [],
             "desiredAccuracy is null"),
            ("a config literal union refuses a case variant (locationAuthorizationRequest 'always')",
             [dict(call("setConfig", {"geolocation": {"locationAuthorizationRequest": "always"}}), **state)], [],
             "is none of Always, Any, WhenInUse"),
            ("a config group handed a scalar refuses ({geolocation: 5})",
             [dict(call("setConfig", {"geolocation": 5}), **state)], [], "the types declare an object"),
            ("an interface-typed config key is held to its fields (a token without expires)",
             [dict(call("setConfig", {"transistorAuthorizationToken": {"url": "u", "accessToken": "a", "refreshToken": "r"}}),
                   **state)], [], "leaves out `expires`"),
            ("setConfig(null) refuses", [dict(call("setConfig", None), **state)], [],
             "args is null — the types declare `Partial<Config>`"),
            ("a callback parameter takes no JSON (onLocation({}) as a hook)",
             [{"api": "onLocation", "args": {}, "returns": {"$": {"type": "object"}}}], [],
             "onLocation takes a callback first (cb)"),
            ("... and `invalid:` does not excuse one left out — the listener would throw on every later delivery",
             [{"api": "onLocation", "invalid": "2026-09-26 selftest", "outcome": "settled 2026-09-26 selftest"}], [],
             "onLocation takes a callback first (cb)"),
            ("a scalar outside the closed alternative of an open union refuses (triggerActivities 'bogus')",
             [dict(call("setConfig", {"activity": {"triggerActivities": "bogus"}}), **state)], [],
             "`TriggerActivity` is none of"),
            ("an event's args are held to the parameter's type (watchPosition's interval '1000')", [],
             [dict(watch, args={"interval": "1000"})], "args.interval is a string"),
            ("... and a key its type does not declare refuses there too", [], [dict(watch, args={"intervall": 1})],
             "declares no such field"),
            ("a misspelt config key refuses even under `invalid:` — a typo, never a form under test",
             [dict(call("setConfig", {"geolocation": {"distanceFiltr": 1}}, invalid="2026-09-26 selftest"), **state)], [],
             "not in the SDK vocabulary"),
            ("`invalid:` refuses on a call the types allow (destroyLocation('x'))",
             [call("destroyLocation", "x", invalid="2026-09-26 selftest")], [], "a call the types allow"),
            ("... on one that supplies every required argument, each of its type (a whole Geofence)",
             [call("addGeofence", dict(fence), invalid="2026-09-26 selftest")], [], "a call the types allow"),
            ("... on one whose only parameter is optional, left out (getCurrentPosition())",
             [gcp({"$.uuid": {"type": "string"}}, invalid="2026-09-26 selftest")], [], "a call the types allow"),
            ("... and on a probe, which calls nothing",
             [{"api": "logger", "probe": True, "invalid": "2026-09-26 selftest", "returns": {"$": {"type": "object"}}}], [],
             "a call the types allow"),
            ("`invalid:` without its date refuses", [call("destroyLocation", 123, invalid="the types forbid it")], [],
             "YYYY-MM-DD <why>"),
            ("`invalid:` with a date and no reason refuses", [call("destroyLocation", 123, invalid="2026-09-26")], [],
             "YYYY-MM-DD <why>")]:
        got = load(hooks_, events_)
        check("(Tier C, C4.0) %s" % title, reason in got or got, True)
    check("(Tier C, C4.0) a union admits what any one alternative admits — two that share a wire type are each tried, "
          "and a value neither takes is named by the first",
          [_conform({"x": 1}, "Geofence | Record<string, number>", "v"),
           _conform({"x": "s"}, "Geofence | Record<string, number>", "v")],
          [None, "v.x: `Geofence` declares no such field"])
    forbidden = [call("destroyLocation", 123, invalid="2026-09-26 a wrong type, measured"),
                 call("destroyLocation", invalid="2026-09-26 the required uuid left out"),
                 dict(call("setConfig", None, invalid="2026-09-26 setConfig(null)"), **state),
                 dict(call("setConfig", {"geolocation": {"desiredAccuracy": None}}, invalid="2026-09-26 WO-059 P1"), **state),
                 call("addGeofence", {k: v for k, v in fence.items() if k not in ("latitude", "longitude", "radius")},
                      invalid="2026-09-26 the documented polygon form (C4 decision 16)")]
    check("(Tier C, C4.0) a dated `invalid:` carries a forbidden form past the type check, the required-argument "
          "check and the config-value check", load(forbidden), "loaded")
    check("(Tier C, C4.0) ... and the device is told the call, never the marker",
          [sorted(wire_hook(h)) for h in forbidden[:2]], [["api", "args", "paths"], ["api", "paths"]])

    # ----------------------------------------------- `outcome:` and `settles_only:` (Tier C, C4.0)
    # `outcome` says which way the call settles and makes `returns` optional; `settled` passes either, recording
    # the root; no `api id=` line is `unsettled`, which fails every outcome.
    dated = "settled 2026-09-26 the types are silent"
    fine = [call("getGeofence", "x", outcome="rejected"), call("getGeofence", "x", outcome=dated),
            dict(call("addGeofence", fence, outcome="resolved"), returns={"$": {"equals": True}}),
            dict(call("getCurrentPosition", {"samples": 1}, outcome="rejected"), returns={"$err": {"type": "number"}}),
            dict(call("stopBackgroundTask", 987654, settles_only="2026-09-26 an id it never issued"),
                 returns={"$": {"type": "null|undefined"}})]
    for h in fine[:2]:
        h.pop("returns")
    check("(Tier C, C4.0) `outcome` loads alone or with a `returns` on its own side; `settles_only` on a call with args",
          load(fine), "loaded")
    for title, hooks_, reason in [
            ("`outcome: settled` without its date refuses", [dict(fine[1], outcome="settled")], "settled YYYY-MM-DD"),
            ("... and with a reason but no date", [dict(fine[1], outcome="settled the types are silent")],
             "settled YYYY-MM-DD"),
            ("an outcome that is not one refuses (ok)", [dict(fine[0], outcome="ok")], "resolved, rejected, threw or"),
            ("... nor one with a word after it", [dict(fine[0], outcome="resolved please")],
             "resolved, rejected, threw or"),
            ("`settled` with its date and no reason refuses", [dict(fine[1], outcome="settled 2026-09-26")],
             "resolved, rejected, threw or"),
            ("`settled` with a `returns` refuses — it judges nothing", [dict(fine[1], returns={"$": {"type": "object"}})],
             "judges none"),
            ("`resolved` with a `$err` path refuses", [dict(fine[3], outcome="resolved")], "the side `outcome` rules out"),
            ("`rejected` with a `$` path refuses", [dict(fine[2], outcome="rejected")], "the side `outcome` rules out"),
            ("`outcome` on a probe refuses", [{"api": "logger", "probe": True, "outcome": "resolved"}], "a probe invokes"),
            ("an empty `returns` beside `outcome` refuses", [dict(fine[0], returns={})], "non-empty `returns`"),
            ("`settles_only` without its date refuses", [dict(fine[4], settles_only="an id it never issued")],
             "`settles_only:` is"),
            ("`settles_only` on a call without args refuses — there is no argument to not see",
             [call("getCount", settles_only="2026-09-26 x", returns={"$": {"type": "number"}})], "`settles_only:` is")]:
        got = load(hooks_)
        check("(Tier C, C4.0) %s" % title, reason in got or got, True)

    # ----------------------------------------------- `generate:` and `alongside:` (WO-107)
    # `generate` builds addGeofences' argument on the device and stands in for it; `alongside` names a second,
    # zero-argument call the driver issues beside the hook's and times.
    bulk = call("addGeofences", generate={"geofences": 5000, "vertices": 12, "seed": 7}, alongside="getCount",
                returns={"$": {"equals": True}})
    check("(WO-107) `generate:` stands in for addGeofences' required argument; `alongside:` names a zero-argument "
          "method", load([bulk]), "loaded")
    for title, hooks_, reason in [
            ("`generate` on another member refuses", [dict(bulk, api="addGeofence")], "only on an addGeofences"),
            ("`generate` beside `args` refuses", [dict(bulk, args=[fence])], "only on an addGeofences"),
            ("`generate` with a key it does not take refuses",
             [dict(bulk, generate={"geofences": 5, "vertices": 4, "seed": 1, "radius": 9})], "`generate:` is"),
            ("`generate` without a key refuses", [dict(bulk, generate={"geofences": 5, "vertices": 4})],
             "`generate:` is"),
            ("`generate` past its bound refuses", [dict(bulk, generate={"geofences": 10001, "vertices": 4, "seed": 1})],
             "`generate.geofences`"),
            ("`generate` with fewer than three vertices refuses",
             [dict(bulk, generate={"geofences": 5, "vertices": 2, "seed": 1})], "`generate.vertices`"),
            ("`generate` with a fractional count refuses",
             [dict(bulk, generate={"geofences": 5.5, "vertices": 4, "seed": 1})], "`generate.geofences`"),
            ("`alongside` naming a method that needs an argument refuses", [dict(bulk, alongside="getGeofence")],
             "`alongside:` names"),
            ("`alongside` naming nothing the SDK declares refuses", [dict(bulk, alongside="getCounts")],
             "`alongside:` names"),
            ("`alongside` on a probe refuses",
             [{"api": "logger", "probe": True, "alongside": "getCount", "returns": {"$": {"type": "object"}}}],
             "`alongside:` names")]:
        got = load(hooks_)
        check("(WO-107) %s" % title, reason in got or got, True)
    check("(WO-107) the device is told the generator and the second call, never a comparator",
          wire_hook(bulk), {"api": "addGeofences", "paths": ["$"],
                            "generate": {"geofences": 5000, "vertices": 12, "seed": 7}, "alongside": "getCount"})
    # (Tier C, C4.3) ... and a `settled` hook its rejection's code and message too (SETTLED_PATHS): its reading is
    # the `recorded` row's.  `resolved`/`rejected` with no `returns` keep the root alone: nothing records them.
    check("(Tier C, C4.0; C4.3) the device is told the call and its root — a `settled` one also a rejection's code "
          "and message — never `outcome` or `settles_only`",
          (wire_hook(fine[0]), wire_hook(fine[1]), wire_hook(fine[4])),
          ({"api": "getGeofence", "paths": ["$", "$err"], "args": "x"},
           {"api": "getGeofence", "paths": ["$", "$err", "$err.code", "$err.message"], "args": "x"},
           {"api": "stopBackgroundTask", "paths": ["$"], "args": 987654}))
    line = lambda ident, outcome: "I/LAB     ( 1): app=api id=%s outcome=%s" % (ident, outcome)
    settles = [{"api": "addGeofence", "args": fence, "outcome": "resolved"},
               {"api": "getGeofence", "args": "x", "outcome": "rejected"},
               {"api": "getGeofence", "args": "x", "outcome": dated},
               {"api": "addGeofence", "args": fence, "outcome": "resolved", "returns": {"$": {"equals": True}}}]
    ran = [line("addGeofence#0", "ok"), line("getGeofence#1", "rejected"), line("getGeofence#2", "ok"),
           "I/LAB     ( 1): app=ret id=getGeofence#2 path=$ type=null parent=1", line("addGeofence#3", "ok"),
           "I/LAB     ( 1): app=ret id=addGeofence#3 path=$ type=boolean value=true parent=1", "I/LAB     ( 1): app=api-done n=4"]
    for title, lines, want in [
            ("`ok` scores resolved, `rejected` rejected, and `settled` records its root without judging it", ran, "ok"),
            ("`settled` passes a rejection too", swap(swap(ran, "path=$ type=null"), "id=getGeofence#2",
                                                     line("getGeofence#2", "rejected")), "ok"),
            ("`resolved` that rejected reads want-resolved-got-rejected",
             swap(ran, "id=addGeofence#0", line("addGeofence#0", "rejected")), "addGeofence#0=want-resolved-got-rejected"),
            ("`rejected` that resolved reads want-rejected-got-resolved",
             swap(ran, "id=getGeofence#1", line("getGeofence#1", "ok")), "getGeofence#1=want-rejected-got-resolved"),
            ("no `api id=` line is `unsettled`, on every outcome, and the batch that never finished says so",
             [l for l in ran if "id=getGeofence#2" not in l and "id=addGeofence#0" not in l and "api-done" not in l],
             "batch-incomplete=2/4, addGeofence#0=unsettled, getGeofence#2=unsettled"),
            ("`absent` fails `settled` — nothing was called", swap(swap(ran, "path=$ type=null"), "id=getGeofence#2",
                                                                  line("getGeofence#2", "absent")), "getGeofence#2=absent"),
            ("the driver's own refusal fails `settled` (unexpressible, item 7)",
             swap(swap(ran, "path=$ type=null"), "id=getGeofence#2", line("getGeofence#2", "unexpressible")),
             "getGeofence#2=unexpressible"),
            ("`resolved` still judges the paths it keeps",
             swap(ran, "id=addGeofence#3 path=$", "I/LAB     ( 1): app=ret id=addGeofence#3 path=$ type=boolean value=false parent=1"),
             "addGeofence#3.$=want-True-got-false")]:
        check("(Tier C, C4.0) %s" % title, apiAssertions(capture(lines, settles)), want)
    live = verdict_view(capture([], settles), {"hooks": settles})["rows"]
    check("(Tier C, C4.0) `unsettled` is pending while the step runs, and red once it is over",
          (live["getGeofence#2"], verdict_view(capture([], settles), {"hooks": settles}, final=True)["rows"]["getGeofence#2"]),
          ("pending", "unsettled"))

    # --------------------------------------------------------------------- `record: true` (Tier C, C4.2)
    # A path read and recorded, never judged: it loads on any path its root declares, stands alone, is an api hook's,
    # never reads red however it came back, and what it read is the step's `recorded` row — `any`, so always ok.
    # (`recorded` is a local name further up, so the metric is reached through METRICS.)
    tok = "$.authorization.accessToken"
    rec = lambda checks, path=tok: {"api": "getState", "returns": {"$.enabled": {"type": "boolean"}, path: checks}}
    check("(Tier C, C4.2) `record: true` loads on a State path, a payload path and a root — beside an `outcome` too",
          load([rec({RECORD: True}), gcp({"$.age": {RECORD: True}}, args={"maximumAge": 60000}),
                {"api": "getCount", "returns": {"$": {RECORD: True}}},
                dict(call("destroyTransistorAuthorizationToken", "http://x", outcome="resolved"),
                     returns={"$": {RECORD: True}})]), "loaded")
    for title, hooks_, events_, reason in [
            ("beside a comparator that judges (type) refuses — it would lose that verdict",
             [rec({RECORD: True, "type": "string"})], [], "stands alone"),
            ("... beside `equals`", [rec({RECORD: True, "equals": "x"})], [], "stands alone"),
            ("... beside a qualifier, which has nothing to qualify", [rec({RECORD: True, "optional": True})], [],
             "stands alone"),
            ("`record: false` refuses — leave the key out", [rec({RECORD: False})], [], "`record` is true"),
            ("... and so does a truthy non-boolean", [rec({RECORD: "yes"})], [], "`record` is true"),
            ("a typo'd State path refuses at load, recorded or not", [rec({RECORD: True}, "$.authorization.accesToken")],
             [], "declares no such path"),
            ("... and a typo'd Location path", [gcp({"$.coordz.latitude": {RECORD: True}})], [], "declares no such path"),
            ("`under:` a recorded container refuses — a recorded path judges nothing, so excuses nothing",
             [gcp({"$.provider": {RECORD: True}, "$.provider.enabled": {"type": "boolean", "under": "$.provider"}})], [],
             "names a recorded path"),
            # (Tier C, C5.0) Decision 25 A: an event may record now (its loading is the C5.0 section's), by the same rule.
            ("under `events:` it stands alone too", [],
             [dict(watch, returns={"$.uuid": {RECORD: True, "type": "string"}})], "stands alone")]:
        got = load(hooks_, events_)
        check("(Tier C, C4.2) %s" % title, reason in got or got, True)
    check("(Tier C, C4.2) the device is told the path, never `record`", wire_hook(rec({RECORD: True})),
          {"api": "getState", "paths": ["$.enabled", tok]})
    kept = [rec({RECORD: True}), {"api": "getOdometer", "returns": {"$": {RECORD: True}}}]
    rl = lambda ident, path, rest: "I/LAB     ( 1): app=ret id=%s path=%s %s" % (ident, path, rest)
    read = [line("getState#0", "ok"), rl("getState#0", "$.enabled", "type=boolean value=false parent=1"),
            rl("getState#0", tok, "type=string value=lab%20token parent=1"), line("getOdometer#1", "ok"),
            rl("getOdometer#1", "$", "type=number value=1234.0 parent=1"), "I/LAB     ( 1): app=api-done n=2"]
    got_tok = lambda rest: swap(read, "path=%s " % tok, rl("getState#0", tok, rest))
    odo_read = "getOdometer#1.$=number:1234.0"
    for title, lines, want in [
            ("a string, %-decoded then %-encoded as a reason is; a number as the driver printed it", read,
             ("ok", "getState#0.%s=string:lab%%20token, %s" % (tok, odo_read))),
            ("an empty string", got_tok("type=string value= parent=1"), ("ok", "getState#0.%s=string:, %s" % (tok, odo_read))),
            ("a key that is gone (missing, its parent there)", got_tok("type=missing parent=1"),
             ("ok", "getState#0.%s=missing, %s" % (tok, odo_read))),
            ("... or whose parent is gone", got_tok("type=missing parent=0"),
             ("ok", "getState#0.%s=missing, %s" % (tok, odo_read))),
            ("null", got_tok("type=null parent=1"), ("ok", "getState#0.%s=null, %s" % (tok, odo_read))),
            ("a value the driver clipped", got_tok("type=string value=abc trunc=1 parent=1"),
             ("ok", "getState#0.%s=string:abc~, %s" % (tok, odo_read))),
            ("a path the driver never reported", swap(read, "path=%s " % tok),
             ("ok", "getState#0.%s=unreported, %s" % (tok, odo_read))),
            ("a recorded path excuses nothing about its call: a rejection is still red",
             [l for l in swap(read, "api id=getOdometer#1", line("getOdometer#1", "rejected")) if "id=getOdometer#1 path" not in l],
             ("getOdometer#1=rejected", "getState#0.%s=string:lab%%20token, getOdometer#1.$=unreported" % tok)),
            ("... and a call the batch never reached is still never-called", read[:3],
             ("batch-incomplete=1/2, getOdometer#1=never-called",
              "getState#0.%s=string:lab%%20token, getOdometer#1.$=unreported" % tok))]:
        cap = capture(lines, kept)
        check("(Tier C, C4.2) never red — %s" % title, (apiAssertions(cap), METRICS["recorded"](cap)), want)
    check("(Tier C, C4.2) `_check` itself judges nothing under `record` — null, a number, missing",
          [_check({"type": t, "value": v, "parent": p, "trunc": False}, {RECORD: True})
           for t, v, p in (("null", "", True), ("number", "3", True), ("missing", "", False))], [None, None, None])
    rec_step = {"hooks": kept, "expect": {}}
    check("(Tier C, C4.2) a step with a recorded path gains `recorded: any`, ends on its evidence, and one without "
          "gains no row",
          (expectations(rec_step, "cordova"), end_policy(rec_step, "cordova"), "recorded" in expectations(step, "cordova")),
          ({"configDropped": "-", "apiAssertions": "ok", "recorded": "any"}, "evidence", False))
    out = io.StringIO()
    verdict = report(score(capture(got_tok("type=null parent=1"), kept), expectations(rec_step, "cordova")), stream=out)
    row = [l.split() for l in out.getvalue().splitlines() if l.split()[:1] == ["recorded"]]
    check("(Tier C, C4.2) ... and summary.txt carries the reading on an ok row: the step PASSes",
          (verdict, row), (True, [["recorded", "getState#0.%s=null," % tok, odo_read, "expected", "any", "ok"]]))
    check("(Tier C, C4.2) `recorded` is implicit: a scenario's expect may not restate it",
          "is implicit" in load_with(dict(api_step, expect={"recorded": "any"}), platforms()), True)
    check("(Tier C, C4.2) a capture with no recorded path reads `-`", METRICS["recorded"](capture(green, hooks)), "-")

    # ------------------------------------------------- a `settled` hook's reading, recorded (Tier C, C4.3)
    # `outcome: settled` passes either side and judges nothing (C4.0); what it read goes into the same `recorded`
    # row as a `record: true` path, in hook order: the side it took, a rejection's code and message where it has
    # them, `unreported` where the driver reported nothing.
    settle = lambda api, args=_ABSENT, **extra: dict(
        {"api": api, "outcome": dated}, **dict({} if args is _ABSENT else {"args": args}, **extra))
    mixed = [settle("getGeofence", "lab-c4-missing"), settle("getGeofence", "lab-c4-missing"),
             settle("destroyLocation", "lab-c4-missing"), rec({RECORD: True}),
             settle("insertLocation", {}, invalid="2026-09-27 selftest: LocationInput requires coords"),
             settle("destroyTransistorAuthorizationToken", "not a url", at_s=2)]
    check("(Tier C, C4.3) a `settled` hook loads beside a `record: true` path and an `invalid:`",
          load([{k: v for k, v in h.items() if k != "at_s"} for h in mixed]), "loaded")
    said = [line("getGeofence#0", "ok"), rl("getGeofence#0", "$", "type=null parent=1"),
            line("getGeofence#1", "rejected"), rl("getGeofence#1", "$err", "type=string value=404 parent=1"),
            rl("getGeofence#1", "$err.code", "type=missing parent=0"),
            rl("getGeofence#1", "$err.message", "type=missing parent=0"),
            line("destroyLocation#2", "rejected"), rl("destroyLocation#2", "$err", "type=object parent=1"),
            rl("destroyLocation#2", "$err.code", "type=string value=EUNSPECIFIED parent=1"),
            rl("destroyLocation#2", "$err.message", "type=string value= parent=1"),
            line("getState#3", "ok"), rl("getState#3", "$.enabled", "type=boolean value=false parent=1"),
            rl("getState#3", tok, "type=string value=t parent=1"),
            line("insertLocation#4", "rejected"), rl("insertLocation#4", "$err", "type=object parent=1"),
            rl("insertLocation#4", "$err.code", "type=string value=no%20coords parent=1"),
            rl("insertLocation#4", "$err.message", "type=null parent=1"),
            "I/LAB     ( 1): app=api-done n=5"]
    head = ("getGeofence#0.$=null, getGeofence#1.$err=string:404, destroyLocation#2.$err=object, "
            "destroyLocation#2.$err.code=string:EUNSPECIFIED, destroyLocation#2.$err.message=string:, "
            "getState#3.%s=string:t, insertLocation#4.$err=object, insertLocation#4.$err.code=string:no%%20coords, "
            "insertLocation#4.$err.message=null" % tok)
    for title, lines, want in [
            ("each side's reading in hook order, beside a recorded path: a string rejection has no code or message "
             "(left out), an Error and a map have both, a null message is null; a call that never settled is "
             "`unreported` here and `unsettled`, red, in apiAssertions", said,
             ("batch-incomplete=5/6, destroyTransistorAuthorizationToken#5=unsettled",
              head + ", destroyTransistorAuthorizationToken#5=unreported")),
            ("... and the same call settling late: its root, and Cordova's wrapper resolving no promise reads `undefined`",
             said[:-1] + [line("destroyTransistorAuthorizationToken#5", "ok"),
                          rl("destroyTransistorAuthorizationToken#5", "$", "type=undefined parent=1"),
                          "I/LAB     ( 1): app=api-done n=6"],
             ("ok", head + ", destroyTransistorAuthorizationToken#5.$=undefined")),
            ("a TypeError's message without a code (missing, its parent there) keeps the message alone; a clipped one "
             "is marked", swap(swap(said, "id=getGeofence#1 path=$err.code", rl("getGeofence#1", "$err.code",
                                                                                   "type=missing parent=1")),
                               "id=getGeofence#1 path=$err.message",
                               rl("getGeofence#1", "$err.message", "type=string value=Cannot%20read trunc=1 parent=1")),
             ("batch-incomplete=5/6, destroyTransistorAuthorizationToken#5=unsettled",
              head.replace("getGeofence#1.$err=string:404", "getGeofence#1.$err=string:404, "
                           "getGeofence#1.$err.message=string:Cannot%20read~")
              + ", destroyTransistorAuthorizationToken#5=unreported")),
            ("Capacitor's pass-through rejection, a CapacitorException whose own `code` is undefined: the undefined "
             "code is left out as a missing one is (`e.code` reads the same), the message kept",
             swap(swap(swap(said, "id=getGeofence#1 path=$err ", rl("getGeofence#1", "$err", "type=object parent=1")),
                       "id=getGeofence#1 path=$err.code", rl("getGeofence#1", "$err.code", "type=undefined parent=1")),
                  "id=getGeofence#1 path=$err.message",
                  rl("getGeofence#1", "$err.message", "type=string value=Failed%20to%20start parent=1")),
             ("batch-incomplete=5/6, destroyTransistorAuthorizationToken#5=unsettled",
              head.replace("getGeofence#1.$err=string:404", "getGeofence#1.$err=object, "
                           "getGeofence#1.$err.message=string:Failed%20to%20start")
              + ", destroyTransistorAuthorizationToken#5=unreported")),
            ("an argument the driver refused (Flutter's `unexpressible`) reported nothing: `unreported`, and red",
             [l for l in swap(said, "api id=getGeofence#0", line("getGeofence#0", "unexpressible"))
              if "id=getGeofence#0 path" not in l],
             ("batch-incomplete=5/6, getGeofence#0=unexpressible, destroyTransistorAuthorizationToken#5=unsettled",
              head.replace("getGeofence#0.$=null", "getGeofence#0=unreported")
              + ", destroyTransistorAuthorizationToken#5=unreported"))]:
        cap = capture(lines, mixed)
        check("(Tier C, C4.3) %s" % title, (apiAssertions(cap), METRICS["recorded"](cap)), want)
    only = {"hooks": [settle("getGeofence", "lab-c4-missing")], "expect": {"crashes": 0}}
    bare = {"hooks": [dict(settle("getGeofence", "lab-c4-missing"), outcome="rejected")], "expect": {"crashes": 0}}
    check("(Tier C, C4.3) a step whose one `settled` hook records gains `recorded: any` and ends on its evidence; one "
          "with `outcome: rejected` alone records nothing and gains no row, nor asks more than its root",
          (expectations(only, "cordova"), end_policy(only, "cordova"), "recorded" in expectations(bare, "cordova"),
           [wire_hook(h)["paths"] for h in only["hooks"] + bare["hooks"]]),
          ({"crashes": 0, "configDropped": "-", "apiAssertions": "ok", "recorded": "any"}, "evidence", False,
           [list(SETTLED_PATHS), ["$", "$err"]]))
    out = io.StringIO()
    one = [line("getGeofence#0", "rejected"), rl("getGeofence#0", "$err", "type=string value=404 parent=1"),
           "I/LAB     ( 1): app=api-done n=1"]
    verdict = report(score(capture(one, only["hooks"]), expectations(only, "cordova")), stream=out)
    row = [l.split() for l in out.getvalue().splitlines() if l.split()[:1] == ["recorded"]]
    check("(Tier C, C4.3) ... and summary.txt carries the reading on an ok row: the step PASSes",
          (verdict, row), (True, [["recorded", "getGeofence#0.$err=string:404", "expected", "any", "ok"]]))

    # ------------------------------------------- coverage: payload leaves, argument forms (Tier C, C4.0)
    # summarize.py --coverage --leaves and --args, over a scratch scenarios/ holding one scenario (as C2's case).
    def covered(fn, steps):
        tmp = tempfile.mkdtemp(prefix="labkit-c40-")
        harness_was = summarize.labkit.HARNESS
        try:
            os.makedirs(os.path.join(tmp, "scenarios"))
            with open(os.path.join(tmp, "scenarios", "selftest-c40.yaml"), "w") as f:
                yaml.safe_dump({"platforms": platforms(), "trigger": "none", "steps": steps}, f)
            summarize.labkit.HARNESS = tmp
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                fn()
            return out.getvalue().splitlines()
        except SystemExit as e:
            return [str(e)]
        finally:
            summarize.labkit.HARNESS = harness_was
            shutil.rmtree(tmp)
    lines = covered(summarize.coverage_leaves, [{"name": "run", "expect": {"crashes": 0}, "hooks": [
        gcp({"$.coords.latitude": {"type": "number", "gte": -90}}, declared="both"),
        {"api": "getGeofences", "returns": {"$.length": {"equals": 1}, "$.1.identifier": {"type": "string"}}},
        # (Tier C, C5.1) a probe's path is a property member's now (a method's probe has none): it types no payload leaf
        {"api": "DesiredAccuracy", "probe": True, "returns": {"$.High": {"type": "number"}}},
        {"api": "getState", "returns": {"$.geolocation.filter": {"type": "object"},
                                        "$.geolocation.filter.policy": {"type": "number", "under": "$.geolocation.filter"}}}],
        "events": [{"subscribe": "onGeofencesChange", "returns": {
            "$.on.0": {"type": "object", "optional": True},
            "$.on.0.notifyOnExit": {"type": "boolean", "optional": True, "under": "$.on.0"}}}]}])
    head = [l for l in lines if l.startswith("   leaf ")]
    col = head[0].index("typed") if head else 0
    rows = {l.split()[0]: l[col:].split() for l in lines[lines.index(head[0]) + 1:]} if head else {}
    check("(Tier C, C4.0) coverage --leaves: each payload leaf once, under the type that declares it — a declared "
          "Location types every leaf it reaches, through a `?` object too, but not an omitted one; an explicit `gte` "
          "values one; `.1` counts as `.0`; `optional`/`under` value nothing; a probe counts for nothing, and a "
          "container is no leaf",
          dict({leaf: rows.get(leaf, lines[:1]) for leaf in (
              "Coords.latitude", "Coords.floor?", "ProviderChangeEvent.enabled", "GeofenceTrigger.action",
              "Location.extras?", "Location.timestamp", "Geofence.identifier", "Geofence.notifyOnExit?",
              "Sensors.platform", "DeviceInfo.model")},
               containers=[k for k in rows if k in ("Location.coords", "Location.provider?", "GeofencesChangeEvent.on")]),
          {"containers": [], "Coords.latitude": ["value", "selftest-c40:getCurrentPosition#0"],
           "Coords.floor?": ["type", "selftest-c40:getCurrentPosition#0"],
           "ProviderChangeEvent.enabled": ["type", "selftest-c40:getCurrentPosition#0"],
           "GeofenceTrigger.action": ["type", "selftest-c40:getCurrentPosition#0"],
           "Location.extras?": ["type", "selftest-c40:getCurrentPosition#0"], "Location.timestamp": ["-"],
           "Geofence.identifier": ["type", "selftest-c40:getGeofences#1"],
           "Geofence.notifyOnExit?": ["type", "selftest-c40:onGeofencesChange@0"],
           "Sensors.platform": ["-"], "DeviceInfo.model": ["-"]})
    counts = [re.search(r"typed (\d+), valued (\d+)", l) for l in lines if l.startswith("== ")]
    check("(Tier C, C4.0) ... the payload line counts what its rows say, after the State line, where an `under` "
          "values nothing either",
          [m.groups() if m else None for m in counts],
          [("1", "0"), (str(sum(r[:1] != ["-"] for r in rows.values())), "1")])
    lines = covered(summarize.coverage_args, [{"name": "run", "expect": {"crashes": 0}, "events": [
        dict(watch, args={"interval": 1000, "persist": False})], "hooks": [
        call("addGeofence", fence), call("addGeofence", dict(fence, identifier="g")),
        {"api": "requestPermission", "args": "motion", "returns": {"$": {"type": "number"}}}, odo(5),
        call("removeGeofences", ["a", "b"]), gcp({"$.uuid": {"type": "string"}}),
        dict(call("stopBackgroundTask", 987654, settles_only="2026-09-26 an id it never issued"),
             returns={"$": {"type": "null|undefined"}}),
        call("destroyLocation", 123, invalid="2026-09-26 a wrong type"),
        dict(call("setConfig", {"geolocation": {"desiredAccuracy": None}}, invalid="2026-09-26 WO-059 P1"), **state),
        dict(call("setConfig", {"geolocation": {"desiredAccuracy": 12345}}, invalid="2026-09-26 undeclared"), **state),
        call("addGeofence", {k: v for k, v in fence.items() if k not in ("latitude", "longitude", "radius")},
             invalid="2026-09-26 the documented polygon form"),
        call("addGeofence", dict(fence, bogus=1), invalid="2026-09-26 a key Geofence does not declare"),
        dict(call("ready", invalid="2026-09-26 the required config left out"), **state)]}])
    head = [l for l in lines if l.startswith("   member ")]
    cols = [head[0].index(c) for c in ("class", "form", "by")] if head else [0, 0, 0]
    rows = {(l[:cols[0]].strip(), l[cols[0]:cols[1]].strip(), l[cols[1]:cols[2]].strip()): l[cols[2]:].strip()
            for l in lines[lines.index(head[0]) + 1:]} if head else {}
    check("(Tier C, C4.0) coverage --args: a conforming form by its shape (an object's keys, an array's elements, a "
          "closed value as itself, a scalar's wire type, `()`), one row however many hooks ask it; `settles_only` "
          "apart; an `invalid:` form by its kind and where",
          ({k: v for k, v in rows.items() if k[1] != "-"}, rows.get(("playSound", "-", ""), lines[:1])),
          ({("addGeofence", "asked", "{identifier,latitude,longitude,notifyOnEntry,radius}"): "selftest-c40",
            ("requestPermission", "asked", '"motion"'): "selftest-c40",
            ("setOdometer", "asked", "number"): "selftest-c40",
            ("removeGeofences", "asked", "[string]"): "selftest-c40",
            ("getCurrentPosition", "asked", "()"): "selftest-c40",
            ("watchPosition", "asked", "{interval,persist}"): "selftest-c40",
            ("stopBackgroundTask", "settles", "number"): "selftest-c40",
            ("destroyLocation", "invalid", "wrong type args"): "selftest-c40",
            ("setConfig", "invalid", "null args.geolocation.desiredAccuracy"): "selftest-c40",
            ("setConfig", "invalid", "out of range args.geolocation.desiredAccuracy"): "selftest-c40",
            ("addGeofence", "invalid", "omitted args.latitude"): "selftest-c40",
            ("addGeofence", "invalid", "unknown args.bogus"): "selftest-c40",
            ("ready", "invalid", "omitted ()"): "selftest-c40"}, ""))
    check("(Tier C, C4.0) ... and its line counts the forms by class",
          (lines[:1] or [""])[0].split(" — ")[-1], "6 conforming forms, 1 settles-only, 6 invalid")
    check("(Tier C, C4.0) ... and a member whose first parameter is a callback takes no argument form — `events:` "
          "subscribes to it",
          sorted({k[0] for k in rows if CALLBACK_ARG.search(param(api_member(k[0])["args"][0])[0])}), [])
    invalid_set = [{"name": "run", "expect": {"crashes": 0}, "hooks": [
        {"api": "stop", "returns": {"$.enabled": {"type": "boolean"}}},
        dict(call("setConfig", {"geolocation": {"desiredAccuracy": None}}, invalid="2026-09-26 WO-059 P1"),
             returns={"$.geolocation.desiredAccuracy": {"equals": -1}}),
        dict(call("setConfig", {"geolocation": {"distanceFilter": 10}}),
             returns={"$.geolocation.distanceFilter": {"equals": 10}})]}]
    rows = {l.split()[0]: l.split()[4:6] for l in covered(summarize.coverage_leaves, invalid_set)
            if l.startswith("   geolocation.d")}
    listed = covered(summarize.coverage, invalid_set)
    check("(Tier C, C4.0) a Config set carrying `invalid:` sets nothing — in the State table (the value read back after "
          "it is still valued) and in the config keys exercised — where a conforming set does",
          (rows.get("geolocation.desiredAccuracy"), rows.get("geolocation.distanceFilter"),
           "   geolocation.desiredAccuracy" in listed, "   geolocation.distanceFilter" in listed),
          (["-", "value"], ["yes", "value"], False, True))
    lines = covered(summarize.coverage_leaves, [{"name": "run", "expect": {"crashes": 0}, "hooks": [
        {"api": "getState", "returns": {"$.geolocation.distanceFilter": {RECORD: True}}},
        gcp({"$.coords.latitude": {RECORD: True}})]}])
    check("(Tier C, C4.2) coverage --leaves: `record: true` types nothing and values nothing, on State or a payload",
          (next((l.split()[4:6] for l in lines if l.startswith("   geolocation.distanceFilter ")), lines[:1]),
           next((l.split()[3:] for l in lines if l.startswith("   Coords.latitude ")), lines[:1])),
          (["-", "-"], ["-"]))

    # ----------------------------------------------------------- same / differs (Tier C, C3)
    # Scored against the step's own earlier reading: a container by its type, a number numerically,
    # `missing` one more type when the comparator stands alone.
    ref_step = {"hooks": [
        {"api": "getState", "returns": {"$.geolocation.distanceFilter": {"type": "number"},
                                        "$.geolocation.stationaryRadius": {"type": "number"},
                                        "$.http.headers": {"type": "object"}, "$.enabled": {"type": "boolean"}}},
        {"api": "setConfig", "args": {"geolocation": {"distanceFilter": 50}}, "sets": "geolocation.distanceFilter",
         "returns": {"$.geolocation.distanceFilter": {"type": "number", "equals": 50, "differs": "getState#0"},
                     "$.geolocation.stationaryRadius": {"same": "previous"},
                     "$.http.headers": {"same": "previous"}, "$.enabled": {"same": "previous"}}}]}
    _validate_hooks("selftest", ref_step)
    rh = ref_step["hooks"]
    check("(Tier C, C3) `previous` resolves at load to the id it names, which run.json then records",
          [rh[1]["returns"][p] for p in ("$.http.headers", "$.enabled")], [{"same": "getState#0"}] * 2)

    def ret_line(ident, path, kind, value=None):
        return ("I/LAB     ( 1): app=ret id=%s path=%s type=%s%s parent=1"
                % (ident, path, kind, "" if value is None else " value=" + value))
    got = ["I/LAB     ( 1): app=api id=getState#0 outcome=ok",
           ret_line("getState#0", "$.geolocation.distanceFilter", "number", "10"),
           ret_line("getState#0", "$.geolocation.stationaryRadius", "number", "5"),
           ret_line("getState#0", "$.http.headers", "object"),
           ret_line("getState#0", "$.enabled", "boolean", "false"),
           "I/LAB     ( 1): app=api id=setConfig#1 outcome=ok",
           ret_line("setConfig#1", "$.geolocation.distanceFilter", "number", "50.0"),
           ret_line("setConfig#1", "$.geolocation.stationaryRadius", "number", "5.0"),
           ret_line("setConfig#1", "$.http.headers", "object"),
           ret_line("setConfig#1", "$.enabled", "boolean", "false"),
           "I/LAB     ( 1): app=api-done n=2"]
    at_set = lambda lines, path, *new: swap(lines, "id=setConfig#1 path=%s " % path, *new)
    at_base = lambda lines, path, *new: swap(lines, "id=getState#0 path=%s " % path, *new)
    gone = at_base(got, "$.enabled")
    for title, lines, want in [
            ("(Tier C, C3) same/differs green: 5.0 is the baseline's 5, 50.0 differs from 10, a map by its type",
             got, "ok"),
            ("(Tier C, C3) same: a neighbour that moved names the hook it had to match, and what it read",
             at_set(got, "$.enabled", ret_line("setConfig#1", "$.enabled", "boolean", "true")),
             "setConfig#1.$.enabled=want-same-as-getState#0-got-true"),
            ("(Tier C, C3) same: 5 then 6 is a move, numerically",
             at_set(got, "$.geolocation.stationaryRadius",
                    ret_line("setConfig#1", "$.geolocation.stationaryRadius", "number", "6")),
             "setConfig#1.$.geolocation.stationaryRadius=want-same-as-getState#0-got-6"),
            ("(Tier C, C3) same: a container compares by type, so a changed type is named by it",
             at_set(got, "$.http.headers", ret_line("setConfig#1", "$.http.headers", "array")),
             "setConfig#1.$.http.headers=want-same-as-getState#0-got-array"),
            ("(Tier C, C3) differs: a value the baseline already read is a table error, named with it",
             at_base(got, "$.geolocation.distanceFilter",
                     ret_line("getState#0", "$.geolocation.distanceFilter", "number", "50")),
             "setConfig#1.$.geolocation.distanceFilter=want-differs-from-getState#0-got-50.0"),
            ("(Tier C, C3) a reference that never reported the path is `unreported-ref`, beside its own row",
             gone, "getState#0.$.enabled=unreported, setConfig#1.$.enabled=unreported-ref"),
            ("(Tier C, C3) missing at the baseline and still missing: red once, where it is typed",
             at_set(at_base(got, "$.enabled", ret_line("getState#0", "$.enabled", "missing")),
                    "$.enabled", ret_line("setConfig#1", "$.enabled", "missing")),
             "getState#0.$.enabled=missing"),
            ("(Tier C, C3) ... while missing, then there, is a change",
             at_base(got, "$.enabled", ret_line("getState#0", "$.enabled", "missing")),
             "getState#0.$.enabled=missing, setConfig#1.$.enabled=want-same-as-getState#0-got-boolean"),
            ("(Tier C, C3) a reference whose line the transport cut is `truncated`, never compared",
             at_base(got, "$.geolocation.distanceFilter",
                     "I/LAB     ( 1): app=ret id=getState#0 path=$.geolocation.distanceFilter type=number value=10"),
             "setConfig#1.$.geolocation.distanceFilter=truncated")]:
        check(title, apiAssertions(capture(lines, rh)), want)
    check("(Tier C, C3) live, `unreported-ref` is pending",
          verdict_view(Capture.live(["09-21 10:00:05.%03d %s" % (n, l) for n, l in enumerate(gone)], {}),
                       {"hooks": rh})["paths"]["setConfig#1"]["$.enabled"], "pending")
    check("(Tier C, C3) `differs:` alone on a key that vanished (its parent too) is `missing`, never a pass",
          _check({"type": "missing", "value": None, "parent": False, "trunc": False}, {"differs": "getState#0"},
                 lambda ref: {"type": "string", "value": "c3", "parent": True, "trunc": False}), "missing")
    check("(Tier C, C3) `sets:` is host-only — never on the wire", "sets" in wire_hook(rh[1]), False)

    def ref_refused(title, hooks_, reason):
        try:
            _validate_hooks("selftest", {"settle": 20, "after": 25, "hooks": json.loads(json.dumps(hooks_))})
            check(title, "loaded", "refused")
        except SystemExit as e:
            check(title, reason in str(e) or str(e), True)
    gs = lambda path="$.enabled", **extra: dict({"api": "getState", "returns": {path: {"type": "boolean"}}}, **extra)
    vs = lambda ref, **extra: dict({"api": "getState", "returns": {"$.enabled": {"same": ref}}}, **extra)
    ref_refused("(Tier C, C3) a reference to a LATER hook refuses to load", [vs("getState#1"), gs()], "not an EARLIER hook")
    # (Tier C, C5.1) A probe's path below `$` is a property member's (a method's probe has none): the probes here are.
    ref_refused("(Tier C, C3) a reference to a probe refuses to load",
                [{"api": "DesiredAccuracy", "probe": True, "returns": {"$": {"type": "object"}}}, vs("DesiredAccuracy#0")],
                "a probe reports")
    ref_refused("(Tier C, C3) a reference in a later `at_s` slot refuses to load — it runs after",
                [gs(at_s=4), vs("getState#0")], "later `at_s` slot")
    ref_refused("(Tier C, C3) a reference that does not report the path refuses to load",
                [gs("$.isMoving"), vs("getState#0")], "does not report $.enabled")
    ref_refused("(Tier C, C3) `previous` with no earlier hook reporting the path refuses to load",
                [{"at": 3, "call": "reset"}, gs("$.isMoving"), vs("previous")], "no earlier api hook")
    ref_refused("(Tier C, C3) a reference naming another member refuses to load", [gs(), vs("stop#0")],
                "is getState, not stop")
    ref_refused("(Tier C, C3) a reference is `previous` or `<api>#<n>`", [gs(), vs("getState 0")], "`<api>#<n>`")
    ref_refused("(Tier C, C3) `same:` on a probe refuses to load",
                # (C5.1 review) refused by the member rule now, before `_ref` sees it: a member path takes its type and value
                [gs(), {"api": "DesiredAccuracy", "probe": True, "returns": {"$.High": {"same": "getState#0"}}}], "(not `same`)")
    ref_refused("(Tier C, C3) `sets:` naming a leaf its args do not set refuses to load",
                [{"api": "setConfig", "args": {"geolocation": {"distanceFilter": 50}}, "sets": "geolocation.stationaryRadius",
                  "returns": {"$": {"type": "object"}}}], "`sets: ")
    skipping = {"settle": 20, "after": 25, "hooks": [gs(), {"api": "DesiredAccuracy", "probe": True, "returns": {
        "$": {"type": "object"}}}, gs(at_s=4), vs("previous")]}
    _validate_hooks("selftest", skipping)
    check("(Tier C, C3) `previous` skips a probe and a later slot for the nearest hook that ran first",
          skipping["hooks"][3]["returns"]["$.enabled"], {"same": "getState#0"})
    shared = {"$.enabled": {"same": "previous"}}
    anchored = {"hooks": [gs(), {"api": "getState", "returns": shared}, {"api": "getState", "returns": shared}]}
    _validate_hooks("selftest", anchored)
    check("(Tier C, C3) a `returns` shared by a YAML anchor resolves per hook, and the YAML's mapping is untouched",
          ([h["returns"]["$.enabled"]["same"] for h in anchored["hooks"][1:]], shared["$.enabled"]["same"]),
          (["getState#0", "getState#1"], "previous"))
    aliased = yaml.safe_load("hooks: [{api: getState, returns: {$.enabled: {type: boolean}}}, "
                             "&h {api: getState, returns: {$.enabled: {same: previous}}}, "
                             "{api: stop, returns: {$.enabled: {equals: false}}}, *h]")
    _validate_hooks("selftest", aliased)
    check("(Tier C, C3) a whole hook repeated by a YAML alias resolves `previous` where each copy stands",
          [h["returns"]["$.enabled"] for h in aliased["hooks"][1::2]], [{"same": "getState#0"}, {"same": "stop#2"}])
    try:
        _validate_events("selftest", {"events": [{"subscribe": "onSchedule", "returns": {"$.enabled": {"same": "previous"}}}]})
        check("(Tier C, C3) `same:` in `events:` refuses to load", "loaded", "refused")
    except SystemExit as e:
        check("(Tier C, C3) `same:` in `events:` refuses to load — a delivery has no earlier reading",
              "event delivery has none" in str(e), True)

    # ------------------------------------------------ roundtrip.yaml and `roundtrip:` (Tier C, C3)
    def rt_says(key, entry):
        try:
            _rt_entry(key, entry)
            return "loaded"
        except SystemExit as e:
            return str(e)
    for title, key, entry, needle in [
            ("a stale entry (a key the vocabulary does not have) refuses the table", "geolocation.distanceFiltr",
             {"value": 5}, "stale entry"),
            ("... and so does a group", "geolocation.filter", {"value": 5}, "stale entry"),
            ("a float refuses — integers only", "geolocation.distanceFilter", {"value": 10.5}, "integers only"),
            ("null refuses", "geolocation.distanceFilter", {"value": None}, "null"),
            ("an undated skip refuses", "geolocation.distanceFilter", {"skip": "flaky"}, "one dated line"),
            ("a skip AND a value refuses", "geolocation.distanceFilter", {"skip": "2026-09-26 x", "value": 5}, "exactly one"),
            ("an unknown entry key refuses", "geolocation.distanceFilter", {"value": 5, "valu": 6}, "unknown key"),
            ("a string with edge whitespace refuses — Android trims", "http.rootProperty", {"value": " c3"}, "no leading"),
            ("a non-ASCII string refuses", "http.rootProperty", {"value": "c3-é"}, "printable ASCII"),
            ("a type the key cannot take refuses (C0)", "geolocation.distanceFilter", {"value": "50"}, "launder"),
            ("an enum value outside the key's refuses (C0)", "http.method", {"value": "put"}, "none of its values"),
            ("a Record key that is no path segment refuses", "http.headers", {"value": {"x-c3": "c3"}}, "Record key"),
            ("an empty Record refuses", "http.headers", {"value": {}}, "empty"),
            ("a list inside a list refuses", "app.schedule", {"value": [["c3"]]}, "holds scalars"),
            ("an array State stores joined needs `readback:`", "activity.triggerActivities", {"value": ["walking"]},
             "give it as `readback:`"),
            ("an element outside its enum refuses", "activity.triggerActivities", {"value": ["walk"], "readback": "walk"},
             "is not a `TriggerActivity`"),
            ("a `readback` equal to the value refuses", "http.method", {"value": "PUT", "readback": "PUT"}, "drop it"),
            ("a container `readback` refuses", "app.schedule", {"value": ["c3"], "readback": ["c3"]}, "is a scalar"),
            ("`restore: same` on a scalar refuses", "http.method", {"value": "PUT", "restore": "same"}, "`restore: same`")]:
        check("(Tier C, C3) roundtrip.yaml: " + title, needle in rt_says(key, entry) or rt_says(key, entry), True)
    check("(Tier C, C3) roundtrip.yaml: a scalar, a dated skip, a Record, a list, a joined array, a restored Record load",
          [rt_says(k, e) for k, e in [("http.method", {"value": "PUT"}), ("http.url", {"skip": "2026-09-26 offline"}),
                                      ("http.headers", {"value": {"c3": "c3"}}),
                                      ("app.schedule", {"value": ["1 09:00-17:00"]}),
                                      ("activity.triggerActivities", {"value": ["walking"], "readback": "walking"}),
                                      ("geolocation.locationAuthorizationAlert",
                                       {"value": {"titleWhenOff": "c3"}, "restore": "same"})]], ["loaded"] * 6)
    saved_rt = globals()["_RT_TABLE"]
    globals()["_RT_TABLE"] = None
    try:
        check("(Tier C, C3) labs/harness/roundtrip.yaml loads, every entry validated", type(roundtrip_table()), dict)
        # A value reaches a device only through a step whose groups and scope name its key.
        def unset(names):
            with contextlib.redirect_stderr(io.StringIO()):
                done = {h["sets"] for n in names for s in scenario(n)["steps"] for h in s["hooks"] if "sets" in h}
            return [k for k, e in roundtrip_table().items() if "value" in e and k not in done]
        rt_names = [n[:-5] for n in sorted(os.listdir(os.path.join(HARNESS, "scenarios"))) if n.endswith(".yaml") and
                    any("roundtrip" in s for s in _load(os.path.join(HARNESS, "scenarios", n)).get("steps") or [])]
        check("(Tier C, C3) every roundtrip.yaml value is set by some scenario's roundtrip step", unset(rt_names), [])
        check("(Tier C, C3) ... and one no step sets is named: api-roundtrip alone sets no one-OS key",
              unset(["api-roundtrip"]), [k for k, e in roundtrip_table().items() if "value" in e and state_scope("$." + k)])
    except SystemExit as e:
        check("(Tier C, C3) labs/harness/roundtrip.yaml loads", str(e), "loaded")
    finally:
        globals()["_RT_TABLE"] = saved_rt
    globals()["_RT_TABLE"] = None
    globals()["_load"] = lambda path: ({k: v for k, v in real_load(path).items() if k != "logger.logMaxDays"}
                                       if path.endswith(os.sep + "roundtrip.yaml") else real_load(path))
    try:
        roundtrip_table()
        check("(Tier C, C3) a Config leaf with no roundtrip.yaml entry refuses the table", "loaded", "refused")
    except SystemExit as e:
        check("(Tier C, C3) a Config leaf with no roundtrip.yaml entry refuses the table, naming it, before any "
              "step names its group", "no entry for logger.logMaxDays " in str(e) or str(e), True)
    finally:
        globals()["_load"], globals()["_RT_TABLE"] = real_load, saved_rt
    # A table of its own, so these cases do not move when the real one does.
    rt_table = {
        "logger.debug": {"value": True}, "logger.logLevel": {"value": 1}, "logger.logMaxDays": {"value": 7},
        "authorization.accessToken": {"value": "c3-accessToken"}, "authorization.expires": {"value": 4102444800},
        "authorization.refreshHeaders": {"value": {"c3": "c3"}},
        "authorization.refreshPayload": {"skip": "2026-09-26 selftest: a skipped key is held, never set"},
        "authorization.refreshToken": {"value": "c3-refreshToken"},
        "authorization.refreshUrl": {"value": "http://10.0.2.2:9/c3"}, "authorization.strategy": {"value": "SAS"},
        "app.heartbeatInterval": {"value": 120}, "app.schedule": {"value": ["1 09:00-17:00"]},
        "app.startOnBoot": {"value": True}, "app.stopOnTerminate": {"value": False},
        "activity.activityRecognitionInterval": {"value": 5000}, "activity.disableMotionActivityUpdates": {"value": True},
        "activity.disableStopDetection": {"value": True}, "activity.minimumActivityRecognitionConfidence": {"value": 50},
        "activity.motionTriggerDelay": {"value": 5000}, "activity.stopOnStationary": {"value": True},
        "activity.triggerActivities": {"value": ["walking"], "readback": "walking"}}
    for key, entry in rt_table.items():
        _rt_entry(key, entry)

    def rt_load(steps, fn=None, table=rt_table, err=None, **top):
        fake = dict({"platforms": platforms(), "trigger": "none", "noStart": True, "steps": steps}, **top)
        globals()["_load"] = lambda path: (json.loads(json.dumps(fake))
                                           if path.endswith(os.sep + "selftest-rt.yaml") else real_load(path))
        saved = globals()["_RT_TABLE"]
        globals()["_RT_TABLE"] = table
        try:
            with contextlib.redirect_stderr(err or io.StringIO()):
                return fn() if fn else scenario("selftest-rt")["steps"]
        except SystemExit as e:
            return str(e)
        finally:
            globals()["_load"], globals()["_RT_TABLE"] = real_load, saved
    one = lambda groups, scope="both", **extra: [dict({"name": "rt", "roundtrip": {"scope": scope, "groups": groups}},
                                                      **extra)]
    sets_of = lambda steps: steps if isinstance(steps, str) else [h.get("sets") for h in steps[0]["hooks"] if "sets" in h]
    steps = rt_load(one(["logger", "authorization", "app"]))
    if isinstance(steps, str):
        check("(Tier C, C3) a roundtrip step loads", steps, "loaded")
    else:
        hk = steps[0]["hooks"]
        both_leaves = ["$." + k for k in config_leaves() if state_scope("$." + k) is None]
        idx = lambda key: next(i for i, h in enumerate(hk) if h.get("sets") == key)
        check("(Tier C, C3) the plan: stop, reset, the baseline, one setConfig per key in the table's order (never a "
              "skipped one), the read-back, the restore, a final getState",
              ([h["api"] for h in hk[:3]], sets_of(steps), [h["api"] for h in hk[-3:]], len(hk)),
              (["stop", "reset", "getState"],
               [k for k in rt_table if k.split(".")[0] in ("logger", "authorization", "app") and "value" in rt_table[k]],
               ["getState", "reset", "getState"], 13 + 6))
        check("(Tier C, C3) the baseline types every both-OS Config leaf, no State-own key, and a set array's length",
              (sorted(hk[2]["returns"]), {tuple(c) for c in hk[2]["returns"].values()}),
              (sorted(both_leaves + ["$.app.schedule.length"]), {("type",)}))
        check("(Tier C, C3) a set: the key {type, equals, differs: the baseline}, its top-level group held same as "
              "the baseline, nothing else",
              (hk[3]["returns"]["$.logger.debug"], hk[3]["returns"]["$.logger.logLevel"], sorted(hk[3]["returns"]),
               hk[3]["args"]),
              ({"type": "boolean", "equals": True, "differs": "getState#2"}, {"same": "getState#2"},
               ["$.logger.debug", "$.logger.logLevel", "$.logger.logMaxDays"], {"logger": {"debug": True}}))
        check("(Tier C, C3) ... the next set holds the key just set, same as that set",
              hk[4]["returns"]["$.logger.debug"], {"same": "setConfig#3"})
        check("(Tier C, C3) the first set of another group holds its neighbours against the baseline",
              hk[idx("app.heartbeatInterval")]["returns"]["$.app.stopOnTerminate"], {"same": "getState#2"})
        rec = idx("authorization.refreshHeaders")
        check("(Tier C, C3) a Record is asserted a level down, and held there by the sets after it, not before",
              (hk[rec]["returns"]["$.authorization.refreshHeaders"], hk[rec]["returns"]["$.authorization.refreshHeaders.c3"],
               hk[rec + 1]["returns"]["$.authorization.refreshHeaders.c3"],
               "$.authorization.refreshHeaders.c3" in hk[rec - 1]["returns"]),
              ({"type": "object"}, {"type": "string", "equals": "c3"}, {"same": "setConfig#%d" % rec}, False))
        arr = idx("app.schedule")
        check("(Tier C, C3) an array: its length and elements; the baseline's length is held until it is set",
              ({p: c for p, c in hk[arr]["returns"].items() if p.startswith("$.app.schedule")},
               hk[arr - 1]["returns"]["$.app.schedule.length"]),
              ({"$.app.schedule": {"type": "array"}, "$.app.schedule.length": {"type": "number", "equals": 1},
                "$.app.schedule.0": {"type": "string", "equals": "1 09:00-17:00"}}, {"same": "getState#2"}))
        rb, rs = hk[-3]["returns"], hk[-2]["returns"]
        check("(Tier C, C3) the read-back: a set key equals its value; a skipped key and another group same as before",
              (rb["$.authorization.strategy"], rb["$.authorization.refreshPayload"], rb["$.http.url"]),
              ({"type": "string", "equals": "SAS"}, {"same": "setConfig#%d" % idx("authorization.strategy")},
               {"same": "getState#2"}))
        check("(Tier C, C3) the restore: every in-scope leaf same as the baseline, a set Record's key absent, an "
              "array's length same",
              (sorted(set(map(json.dumps, rs.values()))), rs["$.authorization.refreshHeaders.c3"],
               rs["$.app.schedule.length"], len(rs)),
              (sorted([json.dumps({"same": "getState#2"}), json.dumps({"absent": True})]), {"absent": True},
               {"same": "getState#2"}, len(both_leaves) + 2))
        check("(Tier C, C3) ... the final getState asks the restore's question again", hk[-1]["returns"], rs)
        check("(Tier C, C3) a roundtrip step expects no crash, and shows the api view",
              (steps[0]["expect"], steps[0]["view"]), ({"crashes": 0}, "api"))
    act = rt_load(one(["activity"]))
    trig = act if isinstance(act, str) else next(h for h in act[0]["hooks"]
                                                 if h.get("sets") == "activity.triggerActivities")
    check("(Tier C, C3) `readback:` is what the set asserts, against the baseline; `value` is what it sends",
          trig if isinstance(trig, str) else (trig["returns"]["$.activity.triggerActivities"], trig["args"]),
          ({"type": "string", "equals": "walking", "differs": "getState#2"},
           {"activity": {"triggerActivities": ["walking"]}}))
    check("(Tier C, C3) `scope: android` loads where a dated not_applicable excuses iOS, and sets only Android's keys",
          sets_of(rt_load(one(["activity"], "android"), not_applicable={"ios": "2026-09-26 — Android-only keys"})),
          ["activity.motionTriggerDelay"])
    skipped_logger = dict(rt_table, **{k: {"skip": "2026-09-26 x"} for k in rt_table if k.startswith("logger.")})
    for title, steps_, top, table_, needle in [
            ("`roundtrip:` with a written `hooks:` refuses to load", one(["logger"], hooks=[]), {}, rt_table,
             "drop its `hooks:`"),
            ("... and with a scenario-level `hooks:` it would inherit", one(["logger"]), {"hooks": []}, rt_table,
             "drop its `hooks:`"),
            ("a scenario-level `roundtrip:` refuses to load", one(["logger"]),
             {"roundtrip": {"scope": "both", "groups": ["logger"]}}, rt_table, "a step's own key"),
            ("a roundtrip step that may start tracking refuses to load", one(["logger"]), {"noStart": False}, rt_table,
             "`noStart: true`"),
            ("a named group's key with no table entry refuses to load, naming the key", one(["logger"]), {},
             {k: v for k, v in rt_table.items() if k != "logger.logMaxDays"}, "no entry for logger.logMaxDays"),
            ("a step whose every key is skipped sets nothing, and refuses", one(["logger"]), {}, skipped_logger,
             "sets nothing"),
            ("a leaf is no group", one(["logger.debug"]), {}, rt_table, "not a Config group"),
            ("a group with no leaf of the scope refuses (logger has no Android-only key)", one(["logger"], "android"),
             {"not_applicable": {"ios": "2026-09-26 x"}}, rt_table, "no android leaf"),
            ("`scope: android` without a dated iOS excuse refuses to load (C0's scope check)",
             one(["activity"], "android"), {}, rt_table, "[Android only]"),
            ("`scope` is both, android or ios", one(["logger"], "all"), {}, rt_table, "both, android or ios"),
            ("a group named twice refuses", one(["logger", "logger"]), {}, rt_table, "each once"),
            ("an unknown `roundtrip` key refuses",
             [{"name": "rt", "roundtrip": {"scope": "both", "groups": ["logger"], "group": []}}], {}, rt_table,
             "unknown key")]:
        said = rt_load(steps_, table=table_, **top)
        check("(Tier C, C3) " + title, isinstance(said, str) and needle in said or said, True)
    limit_was = globals()["RT_STEP_LIMIT"]
    globals()["RT_STEP_LIMIT"] = 1000
    try:
        check("(Tier C, C3) a step whose wired hooks exceed the limit refuses to load, with its size",
              "hooksB64 is" in str(rt_load(one(["logger"]))), True)
    finally:
        globals()["RT_STEP_LIMIT"] = limit_was
    printed = io.StringIO()
    for _ in range(2):
        rt_load([dict(one(["logger"])[0], name="sized")], err=printed)
    check("(Tier C, C3) the expansion prints a step's keys, hooks, paths and hooksB64 once per process",
          len(re.findall(r"step sized: roundtrip 3 keys, 9 hooks, \d+ paths, hooksB64 \d+ B", printed.getvalue())), 1)

    # A simulated SDK answers the generated plan: green when every set merges and reset() restores, and
    # named by key and hook when a set resets a sibling (WO-051's shape) or reset() keeps a value.
    def simulate(hooks_, drop=None, keep=None, defaults=None):
        base = {"$.enabled": ("boolean", "false")}
        for k in config_leaves():
            wire = state_path_wire("$." + k)
            kind = "object" if wire is OPEN else sorted(wire)[0]
            base["$." + k] = (kind, {"number": "0", "boolean": "false", "string": ""}.get(kind))
        base.update(defaults or {})
        now, lines = dict(base), []
        for i, h in enumerate(hooks_):
            ident = "%s#%d" % (h["api"], i)
            if h["api"] == "reset":
                now = dict(base, **({keep: now[keep]} if keep else {}))
            elif h["api"] == "setConfig":
                v = rt_table[h["sets"]]["value"]
                now["$." + h["sets"]] = (_rt_kind(v), str(v).lower() if isinstance(v, bool) else str(v))
                if drop and h["sets"] == drop[0]:
                    now[drop[1]] = base[drop[1]]
            lines.append("I/LAB     ( 1): app=api id=%s outcome=ok" % ident)
            for path in h["returns"]:
                kind, value = now.get(path, ("missing", None))
                lines.append(ret_line(ident, path, kind, value or None))
        return lines + ["I/LAB     ( 1): app=api-done n=%d" % len(hooks_)]
    lg = rt_load(one(["logger"]))
    lg = lg if isinstance(lg, str) else lg[0]["hooks"]
    for title, lines, want in [
            ("an SDK that merges and restores passes the generated step", lambda: simulate(lg), "ok"),
            ("a set that resets a sibling is named by the sibling and the set it had to match, then at the read-back",
             lambda: simulate(lg, drop=("logger.logLevel", "$.logger.debug")),
             "setConfig#4.$.logger.debug=want-same-as-setConfig#3-got-false, getState#6.$.logger.debug=want-True-got-false"),
            ("a reset() that keeps a value is named by the key, at the restore and the final getState",
             lambda: simulate(lg, keep="$.logger.logMaxDays"),
             "reset#7.$.logger.logMaxDays=want-same-as-getState#2-got-7, getState#8.$.logger.logMaxDays=want-same-as-getState#2-got-7"),
            ("a table value this cell already holds is `differs`' red — a table error",
             lambda: simulate(lg, defaults={"$.logger.logLevel": ("number", "1")}),
             "setConfig#4.$.logger.logLevel=want-differs-from-getState#2-got-1")]:
        check("(Tier C, C3) simulated: " + title, lg if isinstance(lg, str) else apiAssertions(capture(lines(), lg)), want)
    if not isinstance(lg, str):
        recorded = json.loads(json.dumps(lg))
        check("(Tier C, C3) a capture recorded with the expansion re-scores against the re-loaded scenario",
              rt_load(one(["logger"]), fn=lambda: apiAssertions(capture(simulate(recorded), recorded,
                                                                       scenario="selftest-rt", step="rt"))), "ok")

    # summarize.py --coverage --leaves, over a scratch scenarios/ holding one scenario (as C2's case above).
    def leaves_of(spec):
        tmp = tempfile.mkdtemp(prefix="labkit-rt-")
        harness_was, table_was = summarize.labkit.HARNESS, summarize.labkit._RT_TABLE
        try:
            os.makedirs(os.path.join(tmp, "scenarios"))
            with open(os.path.join(tmp, "scenarios", "selftest-rt.yaml"), "w") as f:
                yaml.safe_dump(dict({"platforms": platforms(), "trigger": "none", "noStart": True}, **spec), f)
            summarize.labkit.HARNESS, summarize.labkit._RT_TABLE = tmp, rt_table
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                summarize.coverage_leaves()
            head = re.search(r"valued (\d+), round-tripped (\d+)", out.getvalue())
            lines = out.getvalue().splitlines()
            col = lines[1].index("round-tripped")
            return ((int(head.group(1)), int(head.group(2))) if head else out.getvalue()[:200],
                    sorted(l.split()[0] for l in lines[2:] if l[col:col + 3] == "yes"))
        except SystemExit as e:
            return str(e), []
        finally:
            summarize.labkit.HARNESS, summarize.labkit._RT_TABLE = harness_was, table_was
            shutil.rmtree(tmp)
    check("(Tier C, C3) coverage --leaves: a roundtrip step round-trips each key it sets; `same:` values "
          "nothing, so only what an `equals` names is valued (the three, and $.enabled)",
          leaves_of({"steps": one(["logger"])}),
          ((4, 3), ["logger.debug", "logger.logLevel", "logger.logMaxDays"]))
    check("(Tier C, C3) coverage --leaves: round-tripped needs the value asked for BEFORE the restore, and a "
          "restore compared with a reading from before the set — a value merely held is not one",
          leaves_of({"steps": [{"name": "rt", "hooks": [
              {"api": "getState", "returns": {"$.logger.debug": {"type": "boolean"}}},
              {"api": "setConfig", "args": {"logger": {"debug": True, "logLevel": 1, "logMaxDays": 7}},
               "returns": {"$.logger.debug": {"equals": True}, "$.logger.logLevel": {"equals": 1},
                           "$.logger.logMaxDays": {"type": "number"}}},
              {"api": "reset", "returns": {"$.logger.debug": {"same": "getState#0"},
                                           "$.logger.logLevel": {"same": "setConfig#1"},
                                           "$.logger.logMaxDays": {"equals": 3}}}]}]})[1],
          ["logger.debug"])
    check("(Tier C, C3) coverage --leaves: a restore proves a path whose value was asked — a Record's own type "
          "and the value set asked again are no restore; its key absent is, and so is another value",
          leaves_of({"steps": [{"name": "rt", "hooks": [
              {"api": "getState", "returns": {"$.http.headers": {"type": "object"}}},
              {"api": "setConfig", "args": {"http": {"headers": {"c3": "c3"}, "params": {"c3": "c3"},
                                                     "method": "PUT", "timeout": 30000}},
               "returns": {"$.http.headers.c3": {"equals": "c3"}, "$.http.params.c3": {"equals": "c3"},
                           "$.http.method": {"equals": "PUT"}, "$.http.timeout": {"equals": 30000}}},
              {"api": "reset", "returns": {"$.http.headers": {"same": "getState#0"}, "$.http.params.c3": {"absent": True},
                                           "$.http.method": {"equals": "PUT"}, "$.http.timeout": {"equals": 60000}}}]}]})[1],
          ["http.params", "http.timeout"])

    # ------------------------------------------------- invalid.yaml and `invalid_values:` (Tier C, C4.4)
    # (Tier C, C4.5) The C4.4 cases ask about values, not rulings: each fixture is `unasserted:` as a whole leaf, so its
    # values stay bare (a whole-leaf skip takes nothing beside it).
    loose_ = lambda e: e if not isinstance(e, dict) or isinstance(e.get("skip"), str) or "unasserted" in e else \
        dict(e, unasserted="2026-09-27 selftest: a C4.4 case")

    def iv_says(key, entry, rt=None):
        try:
            _iv_entry(key, entry, rt_table if rt is None else rt)
            return "loaded"
        except SystemExit as e:
            return str(e)
    for title, key, entry, needle in [
            ("a stale entry refuses the table", "logger.logLevl", {"undeclared": [9]}, "stale entry"),
            ("a boolean leaf takes no entry — J measures number, enum and string leaves", "logger.debug",
             {"wrongType": [5]}, "C4 decision 13"),
            ("... nor does a Record", "authorization.refreshHeaders", {"wrongType": [5]}, "C4 decision 13"),
            ("an unknown entry key refuses", "logger.logMaxDays",
             {"below": [-5], "above": [5000], "float": [5.1], "wrongType": ["c4"], "belw": [-6]}, "unknown key"),
            ("a `null:` key is YAML's null, and says the null kind is implicit", "logger.logMaxDays",
             {None: [None], "below": [-5]}, "implicit"),
            ("`explicitNull` is never written as a value: it is implicit, and its key holds its ruling alone (C4.5)",
             "authorization.accessToken", {"wrongType": [5], "explicitNull": [None]}, "the implicit null's ruling"),
            ("a kind the leaf takes with no values and no skip refuses, naming it", "logger.logMaxDays",
             {"below": [-5], "float": [5.1], "wrongType": ["c4"]}, "no values and no dated skip for above"),
            ("an undated skip refuses", "logger.logMaxDays",
             {"below": [-5], "float": [5.1], "wrongType": ["c4"], "skip": {"above": "no ceiling"}}, "dated"),
            ("a whole-leaf skip beside values refuses", "authorization.accessToken",
             {"wrongType": [5], "skip": "2026-09-27 x"}, "nothing else"),
            ("a kind with values and a skip refuses", "authorization.accessToken",
             {"wrongType": [5], "skip": {"wrongType": "2026-09-27 x"}}, "one or the other"),
            ("a kind the leaf does not take refuses (below on a string)", "authorization.accessToken",
             {"wrongType": [5], "below": [1]}, "takes"),
            ("... and so does its skip", "authorization.accessToken",
             {"wrongType": [5], "skip": {"float": "2026-09-27 x"}}, "does not take"),
            ("a kind is a non-empty list", "authorization.accessToken", {"wrongType": []}, "non-empty list"),
            ("a value named twice refuses", "authorization.accessToken", {"wrongType": [5, 5]}, "twice"),
            ("the prime's own value refuses — roundtrip.yaml's: keep and store would read alike", "logger.logMaxDays",
             {"below": [-5], "above": [7], "float": [5.1], "wrongType": ["c4"]}, "is the prime"),
            ("below takes integers — a non-integer is `float`'s", "logger.logMaxDays",
             {"below": [-5.5], "above": [5000], "float": [5.1], "wrongType": ["c4"]}, "an integer"),
            ("float takes a non-integer", "logger.logMaxDays",
             {"below": [-5], "above": [5000], "float": [6], "wrongType": ["c4"]}, "a non-integer"),
            ("an integral float refuses (C4.0's transport)", "logger.logMaxDays",
             {"below": [-5], "above": [5000], "float": [6.0], "wrongType": ["c4"]}, "integral float"),
            ("NaN refuses", "logger.logMaxDays",
             {"below": [-5], "above": [5000], "float": [float("nan")], "wrongType": ["c4"]}, "not JSON"),
            ("undeclared must be of the leaf's type and none of its values", "logger.logLevel",
             {"undeclared": [3], "float": [3.1], "wrongType": ["verbose"]}, "none of its declared values"),
            ("... a string for a number enum is `wrongType`'s", "logger.logLevel",
             {"undeclared": ["9"], "float": [3.1], "wrongType": ["verbose"]}, "own type"),
            ("case must be a declared value in another case", "http.method",
             {"undeclared": ["GET"], "case": ["pat"], "wrongType": [1]}, "another case"),
            ("... never the declared value itself", "http.method",
             {"undeclared": ["GET"], "case": ["PATCH"], "wrongType": [1]}, "another case"),
            ("wrongType must be another type than the leaf's", "logger.logMaxDays",
             {"below": [-5], "above": [5000], "float": [5.1], "wrongType": [4]}, "another type"),
            ("... and null is explicitNull's", "authorization.accessToken", {"wrongType": [None]}, "explicitNull"),
            ("a list or a map with anything in it is no value", "authorization.accessToken", {"wrongType": [[5]]},
             "a scalar"),
            ("... and an empty one is a `wrongType` alone (Tier C, C4.4 review)", "logger.logMaxDays",
             {"below": [{}], "above": [5000], "float": [5.1], "wrongType": ["c4"]}, "a scalar"),
            ("a non-ASCII string refuses", "authorization.accessToken", {"wrongType": [5], "undeclared": ["é"]},
             "printable ASCII"),
            ("an undated `unprimed:` refuses", "authorization.accessToken",
             {"wrongType": [5], "unprimed": "no reason"}, "unprimed"),

            ("`couples:` names a leaf of the same top-level group", "logger.logMaxDays",
             {"below": [-5], "above": [5000], "float": [5.1], "wrongType": ["c4"],
              "couples": {"http.timeout": "2026-09-27 x"}}, "same top-level group"),
            ("... with a dated reason", "logger.logMaxDays",
             {"below": [-5], "above": [5000], "float": [5.1], "wrongType": ["c4"],
              "couples": {"logger.logLevel": "because"}}, "dated")]:
        said = iv_says(key, loose_(entry))
        check("(Tier C, C4.4) invalid.yaml: " + title, needle in said or said, True)
    check("(Tier C, C4.4) invalid.yaml: a leaf roundtrip.yaml does not value needs `unprimed:`",
          "to prime with" in iv_says("authorization.accessToken", loose_({"wrongType": [5]}), rt={}), True)
    check("(Tier C, C4.4) invalid.yaml: a number, an enum, a string, a skip, an unprimed leaf and a coupled one load — "
          "the coupled one with an empty {} and [] among its wrongType values (native §2.8)",
          [iv_says(k, loose_(e), roundtrip_table()) for k, e in [
              ("logger.logMaxDays", {"below": [-5], "above": [5000], "float": [5.1], "wrongType": ["c4"]}),
              ("logger.logLevel", {"undeclared": [9], "float": [3.1], "wrongType": ["verbose"]}),
              ("http.method", {"undeclared": ["GET"], "case": ["patch"], "wrongType": [1]}),
              ("authorization.strategy", {"case": ["sas"], "undeclared": ["foo"], "wrongType": [5]}),
              ("authorization.refreshToken", {"skip": "2026-09-27 selftest"}),
              ("http.url", {"wrongType": [5], "unprimed": "2026-09-27 no url",
                            "skip": {"explicitNull": "2026-09-27 vacuous"}}),
              ("geolocation.desiredAccuracy", {"undeclared": [0], "float": [10.1], "wrongType": ["10m", True, {}, []],
                                               "couples": {"geolocation.filter.policy": "2026-09-27 Option B"}})]],
          ["loaded"] * 7)
    iv_was = globals()["_IV_TABLE"]
    globals()["_IV_TABLE"] = None
    try:
        real_iv = invalid_table()
        classes_ = {}
        for k in real_iv:
            classes_[iv_class(k)] = classes_.get(iv_class(k), 0) + 1
        check("(Tier C, C4.4) labs/harness/invalid.yaml loads: one entry per number, enum and string leaf (60: 28, 12, 20)",
              (len(real_iv), classes_), (60, {"number": 28, "enum": 12, "string": 20}))
        # Every value reaches a device only through a step whose groups, scope and kinds name it.
        with contextlib.redirect_stderr(io.StringIO()):
            asked = {(h["sets"], h["kind"], json.dumps(h["args"])) for n in
                     ("api-config-invalid", "api-config-invalid-android", "api-config-invalid-ios")
                     for s in scenario(n)["steps"] for h in s["hooks"] if "kind" in h}

        def nest_(k, v):
            for seg in reversed(k.split(".")):
                v = {seg: v}
            return v
        table_asks = {(k, kind, json.dumps(nest_(k, v))) for k, e in real_iv.items() for kind in IV_KINDS
                      for v in iv_values(e, kind)}
        check("(Tier C, C4.4) every invalid.yaml value, the implicit nulls included, is set by an api-config-invalid* step "
              "(%d)" % len(table_asks), sorted(table_asks - asked), [])
        # (Tier C, C4.5) What the table rules with no `pending:` is exactly what SUITE asserts: a value whose work order
        # merges joins by dropping its `pending:`, and nothing is asserted that the table does not rule.
        with contextlib.redirect_stderr(io.StringIO()):
            ruled_steps = [s for n in ("api-config-values", "api-config-values-android", "api-config-values-ios")
                           for s in scenario(n)["steps"]]
        asserted = {(h["sets"], h["kind"], json.dumps(h["args"])) for s in ruled_steps for h in s["hooks"] if "kind" in h}
        table_rules = {(k, kind, json.dumps(nest_(k, v))) for k, e in real_iv.items() for kind in IV_KINDS
                       for v in iv_values(e, kind) if iv_ruling(e, kind, v).get("ruled") is not None
                       and not iv_ruling(e, kind, v)["pending"]}
        check("(Tier C, C4.5) every value invalid.yaml rules with no `pending:` is asserted by an api-config-values* step, "
              "and nothing else is (%d)" % len(table_rules), (sorted(table_rules - asserted), sorted(asserted - table_rules)),
              ([], []))
        check("(Tier C, C4.5) api-config-values* assert and never record: no step of theirs has a `recorded` row",
              [s["name"] for s in ruled_steps if "recorded" in expectations(s, "capacitor", "android")], [])
    except SystemExit as e:
        check("(Tier C, C4.4) labs/harness/invalid.yaml loads", str(e), "loaded")
    finally:
        globals()["_IV_TABLE"] = iv_was
    globals()["_IV_TABLE"] = None
    globals()["_load"] = lambda path: ({k: v for k, v in real_load(path).items() if k != "http.timeout"}
                                       if path.endswith(os.sep + "invalid.yaml") else real_load(path))
    try:
        invalid_table()
        check("(Tier C, C4.4) a number, enum or string leaf with no invalid.yaml entry refuses the table", "loaded", "refused")
    except SystemExit as e:
        check("(Tier C, C4.4) a number, enum or string leaf with no invalid.yaml entry refuses the table, naming it",
              "no entry for http.timeout " in str(e) or str(e), True)
    finally:
        globals()["_load"], globals()["_IV_TABLE"] = real_load, iv_was

    # A table of its own, over rt_table's leaves (their primes), so these cases do not move when the real one does.
    iv_table = {k: loose_(e) for k, e in {
        "app.heartbeatInterval": {"below": [30, -5], "above": [100000], "float": [90.1], "wrongType": ["c4"]},
        "logger.logLevel": {"undeclared": [9], "float": [3.1], "wrongType": ["verbose"],
                            "couples": {"logger.logMaxDays": "2026-09-27 selftest: a set that moves its neighbour"}},
        "logger.logMaxDays": {"below": [-5], "above": [5000], "float": [5.1], "wrongType": ["c4"]},
        "authorization.accessToken": {"wrongType": [5]},
        "authorization.expires": {"below": [-5], "float": [1700000000.1], "wrongType": ["c4"],
                                  "skip": {"above": "2026-09-27 selftest: no ceiling"}},
        "authorization.refreshToken": {"skip": "2026-09-27 selftest: a whole-leaf skip"},
        "authorization.refreshUrl": {"undeclared": ["ftp://x.test/c4"], "wrongType": [5],
                                     "unprimed": "2026-09-27 selftest: set from where the step left it"},
        "authorization.strategy": {"case": ["sas"], "wrongType": [5],
                                   "skip": {"explicitNull": "2026-09-27 selftest: no null"}},
        "activity.motionTriggerDelay": {"below": [-5], "above": [70000], "float": [3000.1], "wrongType": ["c4"]}}.items()}
    for key, entry in iv_table.items():
        _iv_entry(key, entry, rt_table)

    def iv_load(steps, fn=None, table=iv_table, err=None, rt=None, **top):
        fake = dict({"platforms": platforms(), "trigger": "none", "noStart": True, "steps": steps}, **top)
        globals()["_load"] = lambda path: (json.loads(json.dumps(fake))
                                           if path.endswith(os.sep + "selftest-iv.yaml") else real_load(path))
        saved = globals()["_RT_TABLE"], globals()["_IV_TABLE"]
        globals()["_RT_TABLE"], globals()["_IV_TABLE"] = rt or rt_table, table
        try:
            with contextlib.redirect_stderr(err or io.StringIO()):
                return fn() if fn else scenario("selftest-iv")["steps"]
        except SystemExit as e:
            return str(e)
        finally:
            globals()["_load"] = real_load
            globals()["_RT_TABLE"], globals()["_IV_TABLE"] = saved
    RANGE = ["below", "above", "undeclared", "float", "case"]
    ivs = lambda groups, kinds=RANGE, scope="both", mode="measure", **extra: [dict(
        {"name": "iv", "invalid_values": {"scope": scope, "groups": groups, "kinds": kinds, "mode": mode}}, **extra)]
    plan = lambda steps: steps if isinstance(steps, str) else [(h.get("sets"), h.get("kind")) for h in steps[0]["hooks"]
                                                                if "sets" in h]
    lg = iv_load(ivs(["logger"]))
    if isinstance(lg, str):
        check("(Tier C, C4.4) an invalid_values step loads", lg, "loaded")
    else:
        hk = lg[0]["hooks"]
        both_leaves = ["$." + k for k in config_leaves() if state_scope("$." + k) is None]
        check("(Tier C, C4.4) the plan: stop, reset, the baseline; per value a prime then the set — the table's leaf "
              "order, IV_KINDS' order, the values' order — then the read-back, the restore, a final getState",
              ([h["api"] for h in hk[:3]], plan(lg), [h["api"] for h in hk[-3:]], len(hk)),
              (["stop", "reset", "getState"],
               [("logger.logLevel", None), ("logger.logLevel", "undeclared"), ("logger.logLevel", None),
                ("logger.logLevel", "float"), ("logger.logMaxDays", None), ("logger.logMaxDays", "below"),
                ("logger.logMaxDays", None), ("logger.logMaxDays", "above"), ("logger.logMaxDays", None),
                ("logger.logMaxDays", "float")], ["getState", "reset", "getState"], 16))
        check("(Tier C, C4.4) the baseline types every both-OS Config leaf, as C3's does",
              (sorted(hk[2]["returns"]), {tuple(c) for c in hk[2]["returns"].values()}), (sorted(both_leaves), {("type",)}))
        check("(Tier C, C4.4) a prime: roundtrip.yaml's value, asserted equal, its leaf alone, no kind and no `invalid:`",
              (hk[3]["args"], hk[3]["returns"], "kind" in hk[3], "invalid" in hk[3]),
              ({"logger": {"logLevel": 1}}, {"$.logger.logLevel": {"type": "number", "equals": 1}}, False, False))
        check("(Tier C, C4.4) a set: its leaf and a documented couple recorded, the rest of its top-level group held same "
              "as the baseline; `sets`, `kind` and a generated, dated `invalid:` naming the departure",
              (hk[4]["args"], hk[4]["returns"], hk[4]["sets"], hk[4]["kind"], hk[4]["invalid"]),
              ({"logger": {"logLevel": 9}}, {"$.logger.logLevel": {"record": True}, "$.logger.logMaxDays": {"record": True},
                                             "$.logger.debug": {"same": "getState#2"}},
               "logger.logLevel", "undeclared",
               "2026-09-27 (Tier C, C4.4) invalid.yaml logger.logLevel undeclared: args.logger.logLevel is 9 — "
               "`LogLevel` is none of 0, 1, 2, 3, 4, 5"))
        check("(Tier C, C4.4) ... the next set holds a neighbour same as the set before it, never the prime (which reports "
              "only its own leaf)", hk[6]["returns"]["$.logger.debug"], {"same": "setConfig#4"})
        check("(Tier C, C4.4) ... and the leaf just set, same as its own last set", hk[8]["returns"]["$.logger.logLevel"],
              {"same": "setConfig#6"})
        check("(Tier C, C4.4) a value the types allow (a number below Android's range) is asked with no `invalid:`",
              (hk[8]["kind"], "invalid" in hk[8]), ("below", False))
        rb, rs = hk[-3]["returns"], hk[-2]["returns"]
        check("(Tier C, C4.4) the read-back holds every in-scope leaf same as before; the restore and the final getState "
              "every one same as the baseline",
              (rb["$.logger.logMaxDays"], rb["$.http.url"], set(map(json.dumps, rs.values())), len(rs), hk[-1]["returns"] == rs),
              ({"same": "setConfig#12"}, {"same": "getState#2"}, {json.dumps({"same": "getState#2"})}, len(both_leaves),
               True))
        check("(Tier C, C4.4) an invalid_values step expects no crash, and shows the api view",
              (lg[0]["expect"], lg[0]["view"]), ({"crashes": 0}, "api"))
        check("(Tier C, C4.4) `kind:`, `sets:` and `invalid:` are host-only — never on the wire",
              sorted(wire_hook(hk[4])), ["api", "args", "paths"])
    # (Tier C, C4.4 review) The neighbour scope is the TOP-LEVEL group: geolocation holds geolocation.filter, and a filter
    # leaf's set holds geolocation's own leaves — logger's flat group cannot tell that from the leaf's own group.
    nest_rt = dict(rt_table, **{"geolocation.stopTimeout": {"value": 7}, "geolocation.filter.rollingWindow": {"value": 8}})
    nest_tbl = dict(iv_table, **{k: {"skip": "2026-09-27 selftest: one leaf a group"} for k in config_leaves()
                                 if k.split(".")[0] == "geolocation" and iv_class(k)})
    nest_tbl.update({"geolocation.stopTimeout": loose_({"below": [-5], "above": [2000], "float": [9.1], "wrongType": ["c4"]}),
                     "geolocation.filter.rollingWindow": loose_({"below": [1], "above": [25], "float": [10.1],
                                                                 "wrongType": ["c4"]})})
    for key in ("geolocation.stopTimeout", "geolocation.filter.rollingWindow"):
        _iv_entry(key, nest_tbl[key], nest_rt)
    geo_, filt_ = (iv_load(ivs([g]), table=nest_tbl, rt=nest_rt) for g in ("geolocation", "geolocation.filter"))
    filter_paths = ["$." + k for k in config_leaves() if k.startswith("geolocation.filter.") and state_scope("$." + k) is None]
    geo_paths = ["$." + k for k in config_leaves() if k.startswith("geolocation.") and not k.startswith("geolocation.filter.")
                 and state_scope("$." + k) is None]
    check("(Tier C, C4.4 review) a nested group: a geolocation leaf's set holds every both-OS geolocation.filter leaf "
          "`same: previous`, and a filter leaf's set every geolocation one — the top-level group, never the leaf's own",
          geo_ if isinstance(geo_, str) else filt_ if isinstance(filt_, str) else (
              [sorted({json.dumps(h["returns"].get(p)) for p in filter_paths}) for h in geo_[0]["hooks"] if "kind" in h][0],
              [sorted({json.dumps(h["returns"].get(p)) for p in geo_paths}) for h in filt_[0]["hooks"] if "kind" in h][0],
              [(h["sets"], h["returns"].get(p)) for h in geo_[0]["hooks"] if "kind" in h
               for p in ["$.geolocation.filter.rollingWindow"]][1]),
          ([json.dumps({"same": "getState#2"})], [json.dumps({"same": "getState#2"})],
           ("geolocation.stopTimeout", {"same": "setConfig#4"})))
    wt = iv_load(ivs(["authorization"], ["wrongType"]))
    check("(Tier C, C4.4) a wrongType step: a whole-leaf skip sets nothing, an `unprimed:` leaf has no prime, a number "
          "for a string is `invalid:`",
          plan(wt) if isinstance(wt, str) else (plan(wt), ["invalid" in h for h in wt[0]["hooks"] if "kind" in h]),
          ([("authorization.accessToken", None), ("authorization.accessToken", "wrongType"),
            ("authorization.expires", None), ("authorization.expires", "wrongType"),
            ("authorization.refreshUrl", "wrongType"),
            ("authorization.strategy", None), ("authorization.strategy", "wrongType")], [True] * 4))
    nl = iv_load(ivs(["authorization"], ["explicitNull"]))
    check("(Tier C, C4.4) the explicitNull kind is implicit: every leaf gets one null set, but a skipped one",
          nl if isinstance(nl, str) else [(h["sets"], h["args"]) for h in nl[0]["hooks"] if "kind" in h],
          [("authorization.accessToken", {"authorization": {"accessToken": None}}),
           ("authorization.expires", {"authorization": {"expires": None}}),
           ("authorization.refreshUrl", {"authorization": {"refreshUrl": None}})])
    check("(Tier C, C4.4) `scope: android` loads where a dated not_applicable excuses iOS, and sets only Android's leaves",
          plan(iv_load(ivs(["activity"], scope="android"), not_applicable={"ios": "2026-09-27 — Android-only keys"})),
          [("activity.motionTriggerDelay", None), ("activity.motionTriggerDelay", "below"),
           ("activity.motionTriggerDelay", None), ("activity.motionTriggerDelay", "above"),
           ("activity.motionTriggerDelay", None), ("activity.motionTriggerDelay", "float")])
    for title, steps_, top, needle in [
            ("(C4.5) `mode: assert` over a table that rules nothing refuses: its groups assert nothing",
             ivs(["logger"], mode="assert"), {}, "every value is pending or unasserted"),
            ("`mode` is measure or assert", ivs(["logger"], mode=None), {}, "is measure"),
            ("kinds from two classes refuse: range, explicitNull and wrongType run in steps of their own",
             ivs(["logger"], ["below", "wrongType"]), {}, "steps of their own"),
            ("... and so do range and explicitNull", ivs(["logger"], ["float", "explicitNull"]), {}, "steps of their own"),
            ("an unknown kind refuses", ivs(["logger"], ["null"]), {}, "is a list of"),
            ("a kind named twice refuses", ivs(["logger"], ["below", "below"]), {}, "each once"),
            ("`scope` is both, android or ios", ivs(["logger"], scope="all"), {}, "both, android or ios"),
            ("a leaf is no group", ivs(["logger.logLevel"]), {}, "not a Config group"),
            ("a group named twice refuses", ivs(["logger", "logger"]), {}, "each once"),
            ("a group with no leaf of the scope J measures refuses", ivs(["logger"], scope="android"),
             {"not_applicable": {"ios": "2026-09-27 x"}}, "no android number"),
            ("a group that asks nothing of the step's kinds refuses — drop it",
             ivs(["logger", "authorization"], ["case"]), {}, "asks nothing of kinds case"),
            ("an unknown invalid_values key refuses",
             [{"name": "iv", "invalid_values": {"scope": "both", "groups": ["logger"], "kinds": RANGE,
                                                "mode": "measure", "kind": []}}], {}, "unknown key"),
            ("invalid_values with a written `hooks:` refuses", ivs(["logger"], hooks=[]), {}, "drop its `hooks:`"),
            ("... and with a scenario-level `hooks:` it would inherit", ivs(["logger"]), {"hooks": []}, "drop its `hooks:`"),
            ("a scenario-level `invalid_values:` refuses",
             ivs(["logger"]), {"invalid_values": {"scope": "both", "groups": ["logger"], "kinds": RANGE, "mode": "measure"}},
             "a step's own key"),
            ("an invalid_values step that may start tracking refuses", ivs(["logger"]), {"noStart": False},
             "`noStart: true`"),
            ("`roundtrip:` and `invalid_values:` in one step refuse",
             ivs(["logger"], roundtrip={"scope": "both", "groups": ["logger"]}), {}, "one per step"),
            ("`scope: android` without a dated iOS excuse refuses (C0's scope check)",
             ivs(["activity"], scope="android"), {}, "[Android only]"),
            ("a named group's leaf with no table entry refuses, naming it", ivs(["http"]), {}, "no entry for http.")]:
        said = iv_load(steps_, **top)
        check("(Tier C, C4.4) " + title, isinstance(said, str) and needle in said or said, True)
    limit_was = globals()["IV_STEP_LIMIT"]
    globals()["IV_STEP_LIMIT"] = 1000
    try:
        check("(Tier C, C4.4) a step whose wired hooks exceed IV_STEP_LIMIT refuses to load, with its size",
              "over the 1000 B an invalid-value step may carry" in str(iv_load(ivs(["logger"]))), True)
    finally:
        globals()["IV_STEP_LIMIT"] = limit_was
    check("(Tier C, C4.4) IV_STEP_LIMIT sits under LAUNCH_LIMIT, and RT_STEP_LIMIT stays C3's 48 KiB",
          (IV_STEP_LIMIT < LAUNCH_LIMIT, RT_STEP_LIMIT), (True, 48 * 1024))
    printed = io.StringIO()
    for _ in range(2):
        iv_load([dict(ivs(["logger"])[0], name="sized")], err=printed)
    check("(Tier C, C4.4) the expansion prints a step's sets, primes, hooks, paths and hooksB64 once per process",
          len(re.findall(r"step sized: invalid_values 5 sets \(5 primed\), 16 hooks, \d+ paths, hooksB64 \d+ B",
                         printed.getvalue())), 1)
    for title, hook, needle in [
            ("`kind:` without `sets:` refuses", {"api": "setConfig", "args": {"logger": {"logLevel": 1}}, "kind": "float",
                                                 "returns": {"$.logger.logLevel": {"record": True}}}, "`kind:"),
            ("`kind:` names one of IV_KINDS", {"api": "setConfig", "args": {"logger": {"logLevel": 1}}, "kind": "nul",
                                               "sets": "logger.logLevel",
                                               "returns": {"$.logger.logLevel": {"record": True}}}, "`kind:"),
            ("`kind:` on another member refuses", {"api": "reset", "args": {"logger": {"logLevel": 1}}, "kind": "float",
                                                   "sets": "logger.logLevel",
                                                   "returns": {"$.logger.logLevel": {"record": True}}}, "`kind:")]:
        try:
            _validate_hooks("selftest", {"hooks": [hook]})
            check("(Tier C, C4.4) " + title, "loaded", "refused")
        except SystemExit as e:
            check("(Tier C, C4.4) " + title, needle in str(e) or str(e), True)
    check("(Tier C, C4.4) a step name YAML read as another type (`name: null`) refuses",
          "a step name is a string" in str(iv_load([dict(ivs(["logger"])[0], name=None)])), True)
    try:
        scenario("selftest-no-such-scenario")
        check("(Tier C, C4.4) a scenario with no file refuses, naming it — never a traceback", "loaded", "refused")
    except SystemExit as e:
        check("(Tier C, C4.4) a scenario with no file refuses, naming it — never a traceback",
              "no labs/harness/scenarios/selftest-no-such-scenario.yaml" in str(e), True)
    check("(Tier C, C4.4) scenario_exists: by the exact file name, as variant() reads must_fail",
          (scenario_exists("api-config-invalid"), scenario_exists("API-Config-Invalid"), scenario_exists("null-args")),
          (True, False, False))

    # A simulated SDK answers the generated plan: whatever a cell stores for an invalid value is recorded, never red; a
    # set that moves a neighbour, a prime that does not take and a reset() that keeps a value are.
    def iv_sim(hooks_, store=None, move=None, keep=None, stuck=None):
        base = {"$.enabled": ("boolean", "false")}
        for k in config_leaves():
            wire = state_path_wire("$." + k)
            kind = "object" if wire is OPEN else sorted(wire)[0]
            base["$." + k] = (kind, {"number": "0", "boolean": "false", "string": ""}.get(kind))
        now, lines = dict(base), []
        for i, h in enumerate(hooks_):
            ident = "%s#%d" % (h["api"], i)
            if h["api"] == "reset":
                now = dict(base, **({keep: now[keep]} if keep else {}))
            elif h["api"] == "setConfig":
                node = h["args"]
                for seg in h["sets"].split("."):
                    node = node[seg]
                path = "$." + h["sets"]
                if "kind" in h:
                    now[path] = (store or (lambda k, v, prev: prev))(h["sets"], node, now[path])
                    if move and h["sets"] == move[0]:
                        now[move[1]] = ("number", "42")
                elif h["sets"] != stuck:
                    now[path] = ({str: "string", bool: "boolean"}.get(type(node), "number"),
                                 str(node).lower() if isinstance(node, bool) else str(node))
            lines.append("I/LAB     ( 1): app=api id=%s outcome=ok" % ident)
            for path in h["returns"]:
                kind, value = now.get(path, ("missing", None))
                lines.append(ret_line(ident, path, kind, value or None))
        return lines + ["I/LAB     ( 1): app=api-done n=%d" % len(hooks_)]
    clamp = lambda k, v, prev: ("number", "3650") if v == 5000 else ("number", "0") if v == -5 else \
        ("number", "5") if v == 5.1 else prev              # a clamp, a truncation, and keep for the rest
    if not isinstance(lg, str):
        hk = lg[0]["hooks"]
        for title, lines, want in [
                ("an SDK that stores, clamps, truncates or keeps passes: a stored value is recorded, never judged",
                 lambda: iv_sim(hk, clamp), "ok"),
                ("a set that moves a neighbour is named by the neighbour and the set it had to match — once: the "
                 "sets after it hold the value it moved to",
                 lambda: iv_sim(hk, clamp, move=("logger.logMaxDays", "$.logger.debug")),
                 "setConfig#8.$.logger.debug=want-same-as-setConfig#6-got-number"),
                ("a documented couple that moves is recorded, and then held from its new reading",
                 lambda: iv_sim(hk, clamp, move=("logger.logLevel", "$.logger.logMaxDays")), "ok"),
                ("a prime that does not take is named at the prime",
                 lambda: iv_sim(hk, clamp, stuck="logger.logMaxDays"),
                 "setConfig#7.$.logger.logMaxDays=want-7-got-0, setConfig#9.$.logger.logMaxDays=want-7-got-0, "
                 "setConfig#11.$.logger.logMaxDays=want-7-got-3650"),
                ("a reset() that keeps a value is named by the leaf, at the restore and the final getState",
                 lambda: iv_sim(hk, clamp, keep="$.logger.logMaxDays"),
                 "reset#14.$.logger.logMaxDays=want-same-as-getState#2-got-5, "
                 "getState#15.$.logger.logMaxDays=want-same-as-getState#2-got-5")]:
            check("(Tier C, C4.4) simulated: " + title, apiAssertions(capture(lines(), hk)), want)
        cap = capture(iv_sim(hk, clamp), hk)
        check("(Tier C, C4.4) the recorded row carries every set's reading, a couple's too, in hook order",
              METRICS["recorded"](cap),
              "setConfig#4.$.logger.logLevel=number:1, setConfig#4.$.logger.logMaxDays=number:0, "
              "setConfig#6.$.logger.logLevel=number:1, setConfig#6.$.logger.logMaxDays=number:0, "
              "setConfig#8.$.logger.logMaxDays=number:0, setConfig#10.$.logger.logMaxDays=number:3650, "
              "setConfig#12.$.logger.logMaxDays=number:5")
        check("(Tier C, C4.4) ... so an invalid_values step gains `recorded: any` and ends on its evidence",
              (expectations(lg[0], "capacitor", "android"), end_policy(lg[0], "capacitor")),
              ({"crashes": 0, "configDropped": "-", "apiAssertions": "ok", "recorded": "any"}, "evidence"))
        recorded_ = json.loads(json.dumps(hk))
        check("(Tier C, C4.4) a capture recorded with the expansion re-scores against the re-loaded scenario",
              iv_load(ivs(["logger"]), fn=lambda: apiAssertions(capture(iv_sim(recorded_, clamp), recorded_,
                                                                       scenario="selftest-iv", step="iv"))), "ok")

    # ------------------------------------------------- (Tier C, C4.5) the rulings, and `mode: assert`
    # wo_status stubbed, so these cases never move when a real work order does: WO-900 and WO-902 open, WO-901 done.
    wo_was = globals()["wo_status"]
    globals()["wo_status"] = lambda wo: {"WO-900": "draft", "WO-902": "verify", "WO-901": "done"}.get(wo)
    try:
        ruled_mx = {"below": [{"value": -5, "ruled": 0}], "above": [{"value": 5000, "ruled": 3650, "pending": "WO-900"}],
                    "float": [{"value": 5.1, "ruled": 5}], "wrongType": [{"value": "c4", "unasserted": "2026-09-27 x"}],
                    "explicitNull": {"ruled": 3, "pending": ["WO-900", "WO-902"]}}
        with_ = lambda kind, item: dict(ruled_mx, **{kind: [item]})
        heart_ = {"below": [30, -5], "above": [100000], "float": [{"value": 90.1, "ruled": 90.1}],
                  "wrongType": [{"value": "c4", "ruled": "os_default"}],
                  "explicitNull": {"ruled": "os_default", "pending": "WO-900"},
                  "unasserted": {"below": "2026-09-27 selftest: decision 7", "above": "2026-09-27 selftest: decision 7"}}
        for title, key, entry, needle in [
                ("a bare value refuses: every value carries its ruling", "logger.logMaxDays",
                 dict(ruled_mx, below=[-5]), "has no ruling"),
                ("a value both ruled and unasserted refuses", "logger.logMaxDays",
                 with_("below", {"value": -5, "ruled": 0, "unasserted": "2026-09-27 x"}), "or {value, unasserted}"),
                ("... and one with neither", "logger.logMaxDays", with_("below", {"value": -5}), "or {value, unasserted}"),
                ("... and a `pending:` without a ruling", "logger.logMaxDays",
                 with_("below", {"value": -5, "unasserted": "2026-09-27 x", "pending": "WO-900"}), "or {value, unasserted}"),
                ("an unknown key beside a value refuses", "logger.logMaxDays",
                 with_("below", {"value": -5, "rueld": 0}), "unknown key"),
                ("an undated per-value `unasserted:` refuses", "logger.logMaxDays",
                 with_("wrongType", {"value": "c4", "unasserted": "R6"}), "YYYY-MM-DD"),
                ("a ruling of another type than the leaf's refuses", "logger.logMaxDays",
                 with_("below", {"value": -5, "ruled": "0"}), "the leaf's own type"),
                ("... and an enum's undeclared value", "logger.logLevel",
                 {"undeclared": [{"value": 9, "ruled": 7}], "float": [{"value": 3.1, "ruled": 3}],
                  "wrongType": [{"value": "verbose", "unasserted": "2026-09-27 x"}], "explicitNull": {"ruled": 0}},
                 "one of 0, 1, 2, 3, 4, 5"),
                ("... and a string past what a ret line carries whole", "authorization.accessToken",
                 {"wrongType": [{"value": 5, "ruled": "x" * 201}], "explicitNull": {"ruled": ""}}, "at most 200"),
                ("a ruling that is the prime refuses: a cell that kept the prime would pass", "logger.logMaxDays",
                 with_("below", {"value": -5, "ruled": 7}), "kept the prime"),
                ("... and so does the null's", "logger.logMaxDays", dict(ruled_mx, explicitNull={"ruled": 7}),
                 "kept the prime"),
                ("`pending:` names a work order that exists", "logger.logMaxDays",
                 with_("below", {"value": -5, "ruled": 0, "pending": "WO-999"}), "no workorders/WO-999"),
                ("... that is not done: a done one's values are asserted now", "logger.logMaxDays",
                 with_("below", {"value": -5, "ruled": 0, "pending": ["WO-900", "WO-901"]}), "which is done"),
                ("... by its id", "logger.logMaxDays", with_("below", {"value": -5, "ruled": 0, "pending": "84"}),
                 "names work orders"),
                ("a ruled value under an `unasserted:` kind refuses: its values are bare", "logger.logMaxDays",
                 dict(ruled_mx, unasserted={"below": "2026-09-27 decision 7"}), "values are bare"),
                ("an undated `unasserted:` kind refuses", "logger.logMaxDays",
                 dict(ruled_mx, below=[-5], unasserted={"below": "decision 7"}), "dated"),
                ("... and so does a kind the leaf does not ask", "logger.logMaxDays",
                 dict(ruled_mx, unasserted={"case": "2026-09-27 x"}), "does not ask"),
                ("the implicit null needs its ruling", "logger.logMaxDays",
                 {k: v for k, v in ruled_mx.items() if k != "explicitNull"}, "no ruling for the implicit null"),
                ("... and `explicitNull:` holds that ruling alone, never the null", "logger.logMaxDays",
                 dict(ruled_mx, explicitNull={"value": None, "ruled": 3}), "the implicit null's ruling"),
                ("... never beside a skip of the null", "logger.logMaxDays",
                 dict(ruled_mx, skip={"explicitNull": "2026-09-27 x"}), "never beside a skip"),
                ("a leaf that `couples:` another rules nothing (R22)", "logger.logMaxDays",
                 dict(ruled_mx, couples={"logger.logLevel": "2026-09-27 x"}), "`couples:` and a ruling")]:
            said = iv_says(key, entry)
            check("(Tier C, C4.5) invalid.yaml: " + title, needle in said or said, True)
        check("(Tier C, C4.5) invalid.yaml: rulings load — a literal, R11's os_default, a pending on one work order and on "
              "two, a per-value `unasserted:`, an `unasserted:` kind of bare values, and `case` ruling the prime (SAS)",
              [iv_says(k, e) for k, e in [
                  ("logger.logMaxDays", ruled_mx), ("app.heartbeatInterval", heart_),
                  ("authorization.strategy", {"case": [{"value": "sas", "ruled": "SAS", "pending": "WO-900"}],
                                              "wrongType": [{"value": 5, "ruled": "JWT", "pending": "WO-900"}],
                                              "explicitNull": {"ruled": "JWT", "pending": "WO-900"}})]],
              ["loaded"] * 3)

        # The generator over a ruled table: logLevel 9 and logMaxDays -5 and 5.1 are ruled; logLevel 3.1 and logMaxDays
        # 5000 pending; "verbose" unasserted; heartbeatInterval "c4" is R11's os_default.
        iv_rtable = dict(iv_table, **{
            "logger.logLevel": {"undeclared": [{"value": 9, "ruled": 0}],
                                "float": [{"value": 3.1, "ruled": 3, "pending": "WO-900"}],
                                "wrongType": [{"value": "verbose", "unasserted": "2026-09-27 selftest: R6"}],
                                "explicitNull": {"ruled": 0, "pending": "WO-900"}},
            "logger.logMaxDays": dict(ruled_mx, wrongType=[{"value": "c4", "ruled": 3}]),
            "app.heartbeatInterval": heart_})
        for key in ("logger.logLevel", "logger.logMaxDays", "app.heartbeatInterval"):
            _iv_entry(key, iv_rtable[key], rt_table)
        la = iv_load(ivs(["logger"], mode="assert"), table=iv_rtable)
        check("(Tier C, C4.5) `mode: assert` sets what the table rules with no `pending:` — never a pending or an "
              "unasserted value — each after its prime",
              plan(la), [("logger.logLevel", None), ("logger.logLevel", "undeclared"),
                         ("logger.logMaxDays", None), ("logger.logMaxDays", "below"),
                         ("logger.logMaxDays", None), ("logger.logMaxDays", "float")])
        check("(Tier C, C4.5) `mode: measure` over the same table sets every value, pending and unasserted ones too",
              plan(iv_load(ivs(["logger"]), table=iv_rtable)),
              [("logger.logLevel", None), ("logger.logLevel", "undeclared"), ("logger.logLevel", None),
               ("logger.logLevel", "float"), ("logger.logMaxDays", None), ("logger.logMaxDays", "below"),
               ("logger.logMaxDays", None), ("logger.logMaxDays", "above"), ("logger.logMaxDays", None),
               ("logger.logMaxDays", "float")])
        if not isinstance(la, str):
            ha = la[0]["hooks"]
            check("(Tier C, C4.5) ... its leaf asserted `{type, equals: <ruling>}`, the rest of its group held `same: "
                  "previous` (resolved at load: logLevel's own set, #4), nothing recorded — so the step expects "
                  "apiAssertions alone, and no `recorded` row",
                  ([h["returns"]["$." + h["sets"]] for h in ha if "kind" in h], ha[6]["returns"]["$.logger.logLevel"],
                   any((c or {}).get(RECORD) for h in ha for c in h["returns"].values()),
                   expectations(la[0], "capacitor", "android")),
                  ([{"type": "number", "equals": 0}, {"type": "number", "equals": 0}, {"type": "number", "equals": 5}],
                   {"same": "setConfig#4"}, False, {"crashes": 0, "configDropped": "-", "apiAssertions": "ok"}))
            obey = lambda k, v, prev: ("number", {9: "0", -5: "0", 5.1: "5"}[v])
            check("(Tier C, C4.5) simulated: an SDK that stores each ruling passes",
                  apiAssertions(capture(iv_sim(ha, obey), ha)), "ok")
            check("(Tier C, C4.5) simulated: one that keeps the prime on a value the table rules is named at the set",
                  apiAssertions(capture(iv_sim(ha, lambda k, v, prev: prev if v == -5 else obey(k, v, prev)), ha)),
                  "setConfig#6.$.logger.logMaxDays=want-0-got-7")
            mut = dict(iv_rtable, **{"logger.logMaxDays": dict(iv_rtable["logger.logMaxDays"],
                                                               below=[{"value": -5, "ruled": 1}])})
            hm = iv_load(ivs(["logger"], mode="assert"), table=mut)
            check("(Tier C, C4.5) teeth: the same SDK against a mutated ruling (-5 -> 1) is red at that set, and only there",
                  hm if isinstance(hm, str) else apiAssertions(capture(iv_sim(hm[0]["hooks"], obey), hm[0]["hooks"])),
                  "setConfig#6.$.logger.logMaxDays=want-1-got-0")
        ho = iv_load(ivs(["app"], ["wrongType"], mode="assert"), table=iv_rtable)
        if not isinstance(ho, str):
            ho = ho[0]["hooks"]
            check("(Tier C, C4.5) R11's os_default asserts `same: getState#2`, each cell against its own baseline: the "
                  "default passes, a keep or a parse is named",
                  (ho[4]["returns"]["$.app.heartbeatInterval"],
                   apiAssertions(capture(iv_sim(ho, lambda k, v, prev: ("number", "0")), ho)),
                   apiAssertions(capture(iv_sim(ho, lambda k, v, prev: ("number", "60")), ho))),
                  ({"type": "number", "same": "getState#2"}, "ok",
                   "setConfig#4.$.app.heartbeatInterval=want-same-as-getState#2-got-60"))
        else:
            check("(Tier C, C4.5) an os_default step loads", ho, "loaded")
        check("(Tier C, C4.5) a group whose values are all pending or unasserted refuses in `mode: assert` — drop it",
              "(every value is pending or unasserted)" in str(iv_load(ivs(["logger", "app"], ["wrongType"], mode="assert"),
                                                                        table=dict(iv_rtable, **{
                                                                            "logger.logMaxDays": loose_(ruled_mx)}))), True)
    finally:
        globals()["wo_status"] = wo_was

    # summarize.py --divergence over a scratch capture root: the matrix is read from run.json and the ret lines alone.
    # A cell is (hooks, lines[, label[, head]]): another label is another run's launch mark (contamination), and
    # head=False drops the launch mark and the boot line, as a ring buffer that overflowed does.
    def div_root(cells):
        root = tempfile.mkdtemp(prefix="labkit-div-")
        for (plat, os_name, step), cell_ in cells.items():
            hooks_, lines, label, head = (tuple(cell_) + ("selftest", True))[:4]
            path = os.path.join(root, plat + ("" if os_name == "android" else "-ios"), "selftest-iv", "FIX", step)
            os.makedirs(path)
            with open(os.path.join(path, "logcat.txt"), "w") as f:
                f.write("\n".join((["09-27 10:00:00.000 I/LAB     ( 1): point=launch label=%s" % label,
                                    "09-27 10:00:01.000 I/LAB     ( 1): app=boot n=1 args={}"] if head else []) +
                                  ["09-27 10:00:05.%03d %s" % (n % 1000, l) for n, l in enumerate(lines)] +
                                  ["09-27 10:00:20.000 I/LAB     ( 1): point=trigger none",
                                   "09-27 10:00:30.000 I/LAB     ( 1): point=capture"]) + "\n")
            with open(os.path.join(path, "run.json"), "w") as f:
                json.dump({"platform": plat, "os": os_name, "scenario": "selftest-iv", "step": step, "variant": "FIX",
                           "label": "selftest", "hooks": hooks_, "year": 2026}, f)
        return root

    def div_out(root, variant_="FIX"):
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = summarize.divergence(variant_, root)
            return code, out.getvalue()
        finally:
            shutil.rmtree(root)
    # A capture's lines edited: `drop` the lines containing any of its needles, `swap` one needle's line for another.
    edit = lambda lines, drop=(), swap=None: [(swap[1] if swap and swap[0] in l else l) for l in lines
                                              if not any(d in l for d in drop)]
    row = lambda out, leaf, kind, sent: next((l for l in out.splitlines() if " %s %s %s " % (leaf, kind, sent) in l
                                              or l.rstrip().endswith(" %s %s %s" % (leaf, kind, sent))), "")
    cells_ = lambda l: l.split()[4:] if l.startswith("  ≠") else l.split()[3:]
    if not isinstance(lg, str):
        hk = lg[0]["hooks"]
        ob = [{"api": "stop", "returns": {"$.enabled": {"type": "boolean", "equals": False}}},
              {"api": "setConfig", "args": {"logger": {"logLevel": 2}}, "sets": "logger.logLevel",
               "returns": {"$.logger.logLevel": {"record": True}}}]
        code, out = div_out(div_root({
            ("capacitor", "android", "iv"): (hk, iv_sim(hk, clamp)),
            ("capacitor", "ios", "iv"): (hk, iv_sim(hk, lambda k, v, prev: ("number", str(v)) if v != 9 else prev)),
            ("capacitor", "android", "ob"): (ob, iv_sim(ob)), ("capacitor", "ios", "ob"): (ob, iv_sim(ob))}))
        above, under = row(out, "logger.logMaxDays", "above", "5000"), row(out, "logger.logLevel", "undeclared", "9")
        check("(Tier C, C4.4) --divergence: a row per set, `≠` where the cells read differently, each cell tagged "
              "with the reading it equals — [=] the value sent, [p] the prime's, [b] the baseline's",
              (code, above.startswith("  ≠"), cells_(above)[:1], cells_(above)[4:5], under.startswith("  ≠"),
               cells_(under)[:1], cells_(under)[4:5]),
              (0, True, ["3650"], ["5000[=]"], False, ["1[p]"], ["1[p]"]))
        check("(Tier C, C4.4) --divergence: a cell with no capture reads `.`, a coupled leaf reads under its set, and "
              "the header counts the rows that diverge",
              (cells_(above)[1:2], any("+ $.logger.logMaxDays" in l for l in out.splitlines()),
               "5 sets in selftest-iv, 4 of them read differently" in out), (["."], True, True))
        check("(Tier C, C4.4) --divergence: every other recorded reading of the scenario follows (the Option B probes)",
              any(l.split()[:4] == ["ob", "setConfig#1", '{"logger":', '{"logLevel":'] for l in out.splitlines()), True)
        # (Tier C, C4.4 review) A set the bridge rejected reads `rej`, from its `api id=` line; a set with no reading and
        # no outcome `?` — and a `?` alone marks its row `≠`, the other cells all alike.
        code, out = div_out(div_root({
            ("capacitor", "android", "iv"): (hk, iv_sim(hk, clamp)),
            ("capacitor", "ios", "iv"): (hk, edit(iv_sim(hk, clamp),
                                                  drop=(" id=setConfig#10 path=$.logger.logMaxDays ",
                                                        " id=setConfig#8 path="),
                                                  swap=("id=setConfig#8 outcome=ok",
                                                        "I/LAB     ( 1): app=api id=setConfig#8 outcome=rejected")))}))
        below, above, flt = (row(out, "logger.logMaxDays", k, v) for k, v in (("below", "-5"), ("above", "5000"),
                                                                              ("float", "5.1")))
        check("(Tier C, C4.4 review) --divergence: a rejected set reads `rej`, a set that reported nothing `?`, and "
              "either marks its row `≠` where every other cell reads alike",
              [(l.startswith("  ≠"), cells_(l)[:1], cells_(l)[4:5]) for l in (below, above, flt)],
              [(True, ["0[b]"], ["rej"]), (True, ["3650"], ["?"]), (False, ["5"], ["5"])])
        # (Tier C, C4.4 review) A contaminated capture is left out — its column reads `.` — and named.
        code, out = div_out(div_root({("capacitor", "android", "iv"): (hk, iv_sim(hk, clamp)),
                                      ("cordova", "android", "iv"): (hk, iv_sim(hk, clamp), "another-run")}))
        above = row(out, "logger.logMaxDays", "above", "5000")
        check("(Tier C, C4.4 review) --divergence: a contaminated capture is left out, its column `.`, and named",
              (cells_(above)[:2], bool(re.search(r"contaminated, left out: \S*cordova/selftest-iv/FIX/iv", out))),
              (["3650", "."], True))
        code, out = div_out(div_root({("capacitor", "android", "ob"): (ob, iv_sim(ob))}))
        check("(Tier C, C4.4) --divergence with no invalid_values capture says so and exits 1",
              (code, "carries an invalid_values set" in out), (1, True))
        check("(Tier C, C4.4) --divergence reads its variant's captures only",
              div_out(div_root({("capacitor", "android", "iv"): (hk, iv_sim(hk, clamp))}), "MUT-x")[0], 1)
    # (Tier C, C4.4 review) Strings: iOS canonicalises a case (strategy "sas" -> "SAS"), which is [=i] before it is the
    # prime's [p]; and a reading past DIV_CLIP is cut with `…`, while `≠` still compares the whole of it.
    au = iv_load(ivs(["authorization"]))
    if isinstance(au, str):
        check("(Tier C, C4.4 review) an authorization invalid_values step loads", au, "loaded")
    else:
        ah = au[0]["hooks"]
        long_ = lambda end: lambda k, v, prev: (("string", "ftp://x.test/c4/" + "a" * 15 + end) if k.endswith("refreshUrl")
                                                else ("string", v if end == "1" else v.upper()) if isinstance(v, str)
                                                else prev)
        code, out = div_out(div_root({("capacitor", "android", "iv"): (ah, iv_sim(ah, long_("1"))),
                                      ("capacitor", "ios", "iv"): (ah, iv_sim(ah, long_("2")))}))
        url, strat = (row(out, "authorization.refreshUrl", "undeclared", '"ftp://x.test/c4"'),
                      row(out, "authorization.strategy", "case", '"sas"'))
        check("(Tier C, C4.4 review) --divergence: a string stored in another case reads [=i] (never the prime's [p]); "
              "a long reading is clipped with `…`, and two that differ past the clip still read `≠`",
              (strat.startswith("  ≠"), cells_(strat)[:1], cells_(strat)[4:5], url.startswith("  ≠"),
               cells_(url)[:1], cells_(url)[4:5]),
              (True, ['"sas"[=]'], ['"SAS"[=i]'], True, ['"ftp://x.test/c4/aa…'], ['"ftp://x.test/c4/aa…']))
    check("(Tier C, C4.4) a matrix cell: a number as a number, a string quoted, null or a container by its type, nothing "
          "as `?` — (Tier C, C4.4 review) a rejection as `rej`, the driver's own outcome by name",
          [summarize._div_cell(r, s, outcome=o)[0] for r, s, o in [
              ({"type": "number", "value": "-1.0", "trunc": False}, summarize.labkit._ABSENT, None),
              ({"type": "string", "value": "5", "trunc": False}, 5, None),
              ({"type": "number", "value": "5", "trunc": False}, 5, None),
              ({"type": "null", "value": "", "trunc": False}, None, None),
              ({"type": "object", "value": "", "trunc": False}, {}, None),
              ({"type": "string", "value": "JWT", "trunc": True}, "jwt", None),
              (None, 5, None), (None, 5, "rejected"), (None, 5, "unexpressible")]],
          ["-1", '"5"', "5[=]", "null[=]", "object[=]", '"JWT"[=i]~', "?", "rej", "unexpressible"])

    # (Tier C, C4.4 review) summarize --pilot: per capture, whether the transport CARRIED the step, apart from whether the
    # log kept its answers — each reading from the one fact that decides it.
    if not isinstance(lg, str):
        hk = lg[0]["hooks"]
        full = iv_sim(hk, clamp)
        upto8 = [l for l in full if not re.search(r"id=\w+#(9|1\d)\b|api-done", l)]      # hooks #0..#8 only
        root = div_root({("capacitor", "android", "carried"): (hk, full),
                         ("capacitor", "android", "short"): (hk, edit(full, drop=(" id=setConfig#10 path=",))),
                         ("capacitor", "android", "cut"): (hk, upto8 + ["I/LAB     ( 1): app=api-done n=9"]),
                         ("capacitor", "android", "died"): (hk, upto8 + ["I/LAB     ( 1): point=crash report=selftest"]),
                         ("capacitor", "android", "head"): (hk, full, "selftest", False),
                         ("capacitor", "android", "foreign"): (hk, full, "another-run")})
        try:
            got = {}
            for step in ("carried", "short", "cut", "died", "head", "foreign"):
                got[step] = summarize.pilot_one(os.path.join(root, "capacitor", "selftest-iv", "FIX", step))
            size = len(launch_payloads({"config": {}, "hooks": hk, "events": []})["hooksB64"])
            check("(Tier C, C4.4 review) --pilot: CARRIED, LOG SHORT (carried, a ret missing), NOT CARRIED (api-done short), "
                  "DIED (a crash), HEAD LOST (the ring buffer), CONTAMINATED — with the facts that decide them",
                  ({k: v[0] for k, v in got.items()}, got["carried"][1], got["short"][1]["rets"], got["died"][1]["stopped"]),
                  ({"carried": "CARRIED", "short": "LOG SHORT", "cut": "NOT CARRIED", "died": "DIED", "head": "HEAD LOST",
                    "foreign": "CONTAMINATED"},
                   {"hooksB64": size, "hooks": 16, "api-done": "16", "calls": "16/16", "rets": "%d/%d" % ((sum(
                       len(h["returns"]) for h in hk),) * 2), "stopped": "-"},
                   "%d/%d" % (sum(len(h["returns"]) for h in hk) - len(hk[10]["returns"]), sum(len(h["returns"]) for h in hk)),
                   "setConfig#9"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                codes = (summarize.pilot([os.path.join(root, "capacitor", "selftest-iv", "FIX", "carried")]),
                         summarize.pilot([os.path.join(root, "capacitor", "selftest-iv", "FIX", s)
                                          for s in ("carried", "short")]))
            check("(Tier C, C4.4 review) --pilot exits 0 only when every capture reads CARRIED", codes, (0, 1))
        finally:
            shutil.rmtree(root)

    # summarize.py survives a capture whose scenario is gone (a retired one's leftover logcat: null-args, C4.3).
    def leftover(scenario_, step_):
        path = tempfile.mkdtemp(prefix="labkit-retired-")
        with open(os.path.join(path, "logcat.txt"), "w") as f:
            f.write("09-27 10:00:00.000 I/LAB     ( 1): point=launch label=selftest\n"
                    "09-27 10:00:20.000 I/LAB     ( 1): point=trigger none\n"
                    "09-27 10:00:30.000 I/LAB     ( 1): point=capture\n")
        with open(os.path.join(path, "run.json"), "w") as f:
            json.dump({"platform": "capacitor", "os": "android", "scenario": scenario_, "step": step_,
                       "variant": "FIX", "label": "selftest", "hooks": [], "year": 2026}, f)
        return path

    def summarized(fn, *args):
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                return fn(*args), out.getvalue()
        except (SystemExit, OSError) as e:
            return "raised", str(e)
    gone, stale, refused = (leftover("selftest-retired", "run"), leftover("api-config-invalid", "no-such-step"),
                            leftover("api-config-invalid", "general-range"))
    lk_scenario = summarize.labkit.scenario
    try:
        ok, said = summarized(summarize.score_one, gone)
        check("(Tier C, C4.4) summarize: a capture of a retired scenario is reported RETIRED and skipped, never a stop",
              (ok, "RETIRED: scenario selftest-retired" in said), (True, True))
        ok, said = summarized(summarize.replay_end, [gone], None)
        check("(Tier C, C4.4) summarize --replay-end: ... and tallied `retired`, never a stop", "retired 1" in said, True)
        # (Tier C, C4.4 review) A step since renamed or retired is skipped as RETIRED, never scored as the first step.
        ok, said = summarized(summarize.score_one, stale)
        check("(Tier C, C4.4 review) summarize: a capture of a step its scenario no longer has is RETIRED, not scored "
              "against steps[0]", (ok, "RETIRED: step no-such-step is no longer in scenario api-config-invalid" in said),
              (True, True))
        ok, said = summarized(summarize.replay_end, [stale], None)
        check("(Tier C, C4.4 review) summarize --replay-end: ... tallied `retired`", "retired 1" in said, True)

        # (Tier C, C4.4 review) A scenario whose file is there but refuses to load (invalid.yaml refusing its table) is
        # UNLOADABLE: named, a failure, and every capture after it still scored.
        def refuse(name):
            raise SystemExit("labs/harness/invalid.yaml has no entry for selftest.leaf")
        summarize.labkit.scenario = refuse
        oks, said = summarized(lambda: [summarize.score_one(p) for p in (refused, gone)])
        check("(Tier C, C4.4 review) summarize: an UNLOADABLE scenario fails its capture, and --all carries on past it",
              (oks, "UNLOADABLE: labs/harness/invalid.yaml has no entry for selftest.leaf — skipped" in said),
              ([False, True], True))
        ok, said = summarized(summarize.replay_end, [refused], None)
        check("(Tier C, C4.4 review) summarize --replay-end: ... tallied `unloadable`", "unloadable 1" in said, True)
    finally:
        summarize.labkit.scenario = lk_scenario
        for path in (gone, stale, refused):
            shutil.rmtree(path)

    # (WO-027) a device call that hangs raises instead of waiting forever.
    try:
        device_call(["sleep", "5"], timeout=1)
        check("a device call past its timeout raises", "returned", "raised")
    except SystemExit as e:
        check("a device call past its timeout raises", "did not return within 1 s" in str(e), True)
    check("a device call inside its timeout returns its output",
          device_call(["echo", "ok"], timeout=5).stdout.strip(), "ok")

    # (WO-027) end-on-evidence: complete only when every declared piece of evidence is in.
    ev_step = {"hooks": [{"api": "stop"}, {"api": "getState", "at_s": 4}],
               "events": [{"subscribe": "onEnabledChange", "min": 2}, {"subscribe": "onGeofence"}],
               "dialog": {"android": ["allow"]}}
    L = lambda *msgs, pid=1: ["09-21 10:00:05.000 I/LAB     (%5d): %s" % (pid, m) for m in msgs]
    base = L("app=boot n=1 args={}", "app=tick n=1", "app=api-done n=2", "app=event id=onEnabledChange@0 n=2",
             "app=event id=onGeofence@1 n=1", "point=dialog action=allow button=Allow")
    check("evidence: everything in", evidence_state(base, ev_step, "android"), (True, 1))
    stray = L("app=tick n=1800", "app=tick n=1801", "app=api-done n=2", "app=event id=onEnabledChange@0 n=2",
              "app=event id=onGeofence@1 n=1", pid=82460)
    check("evidence: another lab app's ticks are not this app's liveness",
          evidence_state(base + stray, ev_step, "android"), (True, 1))
    check("evidence: another lab app's api-done and events do not complete the step",
          evidence_state(L("app=boot n=1 args={}", "app=tick n=1", "point=dialog action=allow button=Allow") + stray,
                         ev_step, "android")[0], False)
    check("evidence: two processes that each logged app=boot are both the app (a relaunch)",
          evidence_state(L("app=boot n=1 args={}", "app=tick n=1") + L("app=boot n=1 args={}", "app=tick n=1",
                         "app=api-done n=2", "app=event id=onEnabledChange@0 n=2", "app=event id=onGeofence@1 n=1",
                         "point=dialog action=allow button=Allow", pid=2), ev_step, "android"), (True, 2))
    check("evidence: `alive` counts liveness lines — a recreated page's n=1 still counts",
          evidence_state(base + L("app=tick n=2", "app=tick n=1"), ev_step, "android")[1], 3)
    check("evidence: a beat is liveness too (Tier B, B3), and another lab app's is not",
          evidence_state(base + L("app=beat n=7", "app=beat n=8") + L("app=beat n=9000", pid=82460), ev_step, "android")[1], 3)
    # A beat carries no probe fields; one that did must still not be read as the app's state.
    beaten = Capture.live(["09-21 10:00:25.000 I/LAB     ( 1): app=tick n=1 enabled=true",
                           "09-21 10:00:26.000 I/LAB     ( 1): app=beat n=40 enabled=false"], {})
    beaten.window = beaten.lines
    check("last_state reads a tick's probe fields and never a beat", beaten.last_state("enabled"), "true")
    import devices
    mark = L("point=launch label=selftest")
    check("contamination: a lab app this run never booted is named by its pid",
          devices.contamination(mark + base + stray, "selftest"), ["foreign lab process pid 82460"])
    check("contamination: the app's own relaunch is not, on Android",
          devices.contamination(mark + base + L("app=boot n=1 args={}", "app=tick n=1", pid=2), "selftest"), [])
    check("contamination: on iOS a second boot in one step is another lab app",
          devices.contamination(mark + base + L("app=boot n=1 args={}", "app=tick n=1", pid=2), "selftest", "ios"),
          ["lab process pid 2 booted mid-step"])
    check("contamination: the app's driver failing before its boot is not a stray",
          devices.contamination(mark + L("app=error TypeError: Lab.args is not a function", pid=4242), "selftest"), [])
    check("(C6.0 review) contamination: a process whose only app= line is the native window report is the app's "
          "stuck splash, not a stray",
          devices.contamination(mark + L("app=window offMain=0 threads=-", pid=4242), "selftest"), [])
    check("(C6.0 review) contamination: a window report does not excuse a process that also ticks",
          devices.contamination(mark + base + L("app=window offMain=0 threads=-", "app=tick n=1", pid=4242), "selftest"),
          ["foreign lab process pid 4242"])
    check("contamination: a lost log head is named, not blamed on a stray",
          devices.contamination(L("app=tick n=40", "app=api-done n=2"), "selftest"),
          ["log head lost: pid 1 logs app= lines with no app=boot, and this run's launch mark is gone too"])
    # (WO-191, D2) A headless process of another lab's app boots no driver, so only its package gives it away:
    # ActivityManager's `Start proc` line (MEASURED: fetch's termination-calibration, pid 4162 for FetchJobService).
    own_ = "com.transistorsoft.labs.react"
    start_ = lambda pid, proc: ["09-21 10:00:04.000 I/ActivityManager(  674): Start proc %d:%s/u0a236 for service "
                                "{%s/com.transistorsoft.tsbackgroundfetch.FetchJobService}" % (pid, proc, proc.split(":")[0])]
    fetched_ = L("point=headless event=fetch taskId=flutter_background_fetch", "point=task-api call=finish", pid=900)
    check("(WO-191) contamination: a process Start proc ties to another package that writes LAB lines (a headless "
          "fetch) is foreign, named with its package; tied to the app's own package (or a `:sub` process of it), not; "
          "no Start proc line, not; and no package given, today's verdict",
          [devices.contamination(mark + base + start_(900, "com.transistorsoft.fetchlab.react") + fetched_, "selftest",
                                 package=own_),
           devices.contamination(mark + base + start_(900, own_ + ":remote") + fetched_, "selftest", package=own_),
           devices.contamination(mark + base + fetched_, "selftest", package=own_),
           devices.contamination(mark + base + start_(900, "com.transistorsoft.fetchlab.react") + fetched_, "selftest")],
          [["foreign lab process pid 900 (com.transistorsoft.fetchlab.react)"], [], [], []])
    check("(WO-191) contamination: a stray app= process Start proc ties to another package is named once, with it; and "
          "a pid reused by the app's own later start is the app's",
          [devices.contamination(mark + base + start_(82460, "com.transistorsoft.fetchlab.react") + stray, "selftest",
                                 package=own_),
           devices.contamination(mark + base + start_(900, "com.transistorsoft.fetchlab.react") + start_(900, own_)
                                 + fetched_, "selftest", package=own_)],
          [["foreign lab process pid 82460 (com.transistorsoft.fetchlab.react)"], []])
    # Every shape a `LAB app=` line arrives in (557 logs on disk, 2026-09-24): the native tag, React
    # Native's and Capacitor's console, Flutter's print, and the iOS tap rendered by to_logcat.
    import summarize
    ev = lambda after, t: {"after": after, "t": t, "gap_ms": None, "next_ms": None}
    ref = {"api": [("start#2", 1000), ("changePace#3", 5000), ("stop#4", 9000)],
           "events": {"onMotionChange@3": ev("start#2", 1006)}, "dialog": {"after": "stateView", "t": 500},
           "actions": ["allow"]}
    check("trace-diff: an event that stops riding its hook moved, however close it rode it",
          summarize._trace_changes(ref, dict(ref, events={"onMotionChange@3": ev("stop#4", 11500)})),
          ["event onMotionChange@3 first delivered after start#2 -> after stop#4 (moved across changePace#3)"])
    check("trace-diff: crossing a hook within 50 ms in both runs is jitter",
          summarize._trace_changes(ref, dict(ref, events={"onMotionChange@3": ev("boot", 990)})), [])
    check("trace-diff: a dialog the boot fix raised, now raised after a hook, moved",
          summarize._trace_changes(ref, dict(ref, dialog={"after": "api changePace#3", "t": 5200})),
          ["dialog raised after stateView -> after api changePace#3 (moved across start#2)"])
    check("trace-diff: a dialog no longer raised",
          summarize._trace_changes(ref, dict(ref, dialog=None)), ["dialog no longer raised"])
    geo = scenario("events-geofence")["steps"][0]
    tick_era = [dict({k: v for k, v in h.items() if k != "at_s"}, **({"at": 3} if "at_s" in h else {}))
                for h in geo["hooks"]]
    check("spec: a capture recorded with ticks (`at: 3`) still answers the YAML's `at_s` — the schedule is not the question",
          apiAssertions(capture([], tick_era, geo["events"], scenario="events-geofence", step=geo["name"])) != "spec-changed-since-capture",
          True)
    moved = [dict(h, returns={"$": {"type": "boolean", "equals": False}}) if h.get("api") == "removeGeofences" else h
             for h in geo["hooks"]]
    check("spec: ... but a changed comparator is a changed question",
          apiAssertions(capture([], moved, geo["events"], scenario="events-geofence", step=geo["name"])),
          "spec-changed-since-capture")
    # (WO-027) The source stamp ignores watchman's transient sync cookies — in the app tree and among
    # a plugin checkout's untracked files — and still sees a real file appear.
    import run, subprocess as sp
    tmp = tempfile.mkdtemp(prefix="labkit-stamp-")
    try:
        app, repo = os.path.join(tmp, "platforms", "fake", "app"), os.path.join(tmp, "plugin")
        os.makedirs(app)
        os.makedirs(repo)
        open(os.path.join(app, "index.js"), "w").write("main()\n")
        open(os.path.join(repo, "a.txt"), "w").write("a\n")
        for cmd in (["init", "-q"], ["add", "a.txt"],
                    ["-c", "user.email=lab@example.invalid", "-c", "user.name=lab", "commit", "-qm", "a"]):
            sp.run(["git", "-C", repo] + cmd, check=True, capture_output=True)
        plat, var = {"name": "fake", "os": "android", "plugins": {"worktree": repo}}, {"name": "T", "native": "x"}
        labs_was, run.labkit.LABS = run.labkit.LABS, tmp
        try:
            before = run.source_stamp(plat, var)
            for d in (app, repo):
                open(os.path.join(d, ".watchman-cookie-Mac-57877-4543"), "w").close()
            check("stamp: a watchman cookie in the app tree or the plugin checkout changes nothing",
                  run.source_stamp(plat, var), before)
            open(os.path.join(repo, "real.txt"), "w").write("x\n")
            check("stamp: ... while a real untracked file still does", run.source_stamp(plat, var) != before, True)
            # (WO-143) A platform with no plugin: built from the native label alone.  Every one of these raised.
            bare = {"name": "fake", "os": "android", "build": {"cmd": ["./build.sh", "{native}", "{plugin}"]}}
            bare_stamp = run.source_stamp(bare, var)
            open(os.path.join(app, "index.js"), "w").write("main() // edited\n")
            check("(WO-143) a manifest that names no plugin has none: no path, no checkout, a null record, and a "
                  "stamp of its label and its own sources that an edit to them still moves",
                  (run.plugin_path(bare, var), run.plugin_checkout(bare, var), run.plugin_source(bare, var),
                   len(bare_stamp), run.source_stamp(bare, var) != bare_stamp,
                   run.source_stamp(bare, {"name": "T", "native": "y"}) != run.source_stamp(bare, var),
                   run.plugin_path(bare, {"name": "T", "native": "x", "plugin": "wo107"})),
                  (None, None, None, 64, True, True, None))
            os.makedirs(os.path.join(app, "android", ".kotlin", "sessions"))
            kotlin_before = run.source_stamp(plat, var)
            open(os.path.join(app, "android", ".kotlin", "sessions", "kotlin-compiler-1.salive"), "w").close()
            check("(WO-143) stamp: a Kotlin compiler session file a killed build left behind changes nothing",
                  run.source_stamp(plat, var), kotlin_before)
            check("(WO-143) ... and a manifest that names one keeps it, unchanged: a `default_checkout` alone is "
                  "a plugin too",
                  (run.plugin_path(plat, var), run.plugin_checkout(plat, var) == os.path.realpath(repo),
                   run.plugin_path({"name": "fake", "default_checkout": repo}, {"plugin": "default"})),
                  (repo, True, "default"))
        finally:
            run.labkit.LABS = labs_was
    finally:
        shutil.rmtree(tmp)
    declared = {"hooks": [], "events": [], "expect": {"dialogAnswered": "declared"},
                "dialog": _dialog_plan("t", {"android": ["allow", "rationale", "always", "back"], "ios": ["allow", "always"]})}
    check("expect: `dialogAnswered: declared` is the OS's own declared sequence (WO-045)",
          (expectations(declared, "cordova", "android")["dialogAnswered"], expectations(declared, "cordova", "ios")["dialogAnswered"]),
          ("allow,rationale,always,back", "allow,always"))
    check("line_pid reads every host's line shape",
          [line_pid(l) for l in ["09-24 12:31:32.105 I/Capacitor/Console(19838): File:  - Line 333 - Msg: LAB app=tick n=1",
                                 "09-24 12:27:27.755 I/ReactNativeJS(17495): LAB app=boot n=1",
                                 "09-24 12:19:34.209 I/flutter (19964): LAB app=boot n=1",
                                 "09-24 12:19:35.815 I/LAB     (82460): app=tick n=1821",
                                 "LAB app=tick n=1"]],
          ["19838", "17495", "19964", "82460", None])
    check("evidence: api batch short of its count", evidence_state(
        [l.replace("api-done n=2", "api-done n=1") for l in base], ev_step, "android")[0], False)
    check("evidence: an event below its min", evidence_state(
        [l.replace("onEnabledChange@0 n=2", "onEnabledChange@0 n=1") for l in base], ev_step, "android")[0], False)
    check("evidence: an absent member cannot deliver, so it does not hold the step",
          evidence_state([l for l in base if "onGeofence@1" not in l]
                         + L("app=event-arm id=onGeofence@1 outcome=absent"), ev_step, "android")[0], True)
    check("evidence: an unanswered dialog", evidence_state(
        [l for l in base if "dialog" not in l], ev_step, "android")[0], False)
    check("evidence: an iOS step with no iOS dialog does not wait for one",
          evidence_state([l for l in base if "dialog" not in l], ev_step, "ios")[0], True)
    check("end policy: a windowed metric keeps the full window",
          end_policy({"hooks": [], "events": [], "expect": {"enabled": True}, "trigger": "none"}, "cordova"), "window")
    check("end policy: `end: window` keeps it too",
          end_policy({"hooks": [{"api": "stop", "returns": {"$": {"type": "object"}}}], "events": [],
                      "expect": {"crashes": 0}, "trigger": "none", "end": "window"}, "cordova"), "window")
    # (WO-027, Tier B) The per-tick fix is derived on the device (LabApi.tickGcpFor) and here
    # (tick_gcp); a step that ends on its evidence must be one with nothing that fix feeds.
    # (Tier C, C6.0) Held on the untriggered steps: a triggered step is always a window step (end_policy), and
    # headless-android's `move` is the first with api hooks too — a conformance step that must keep its window (the
    # C6 headless analysis §4), with no per-tick fix to count.  Every triggered step must still be a window step.
    stepped, mismatched, triggered = 0, [], []
    for f in sorted(os.listdir(os.path.join(HARNESS, "scenarios"))):
        if not f.endswith(".yaml"):
            continue
        spec = scenario(f[:-len(".yaml")])
        plat = next(p for p in spec["platforms"] if p in platforms())
        for st in spec["steps"]:
            stepped += 1
            if st.get("trigger", "none") != "none":
                if end_policy(st, plat) != "window":
                    mismatched.append("%s/%s" % (spec["name"], st["name"]))
                elif not tick_gcp(st):
                    triggered.append("%s/%s" % (spec["name"], st["name"]))
            elif tick_gcp(st) != (end_policy(st, plat) == "window"):
                mismatched.append("%s/%s" % (spec["name"], st["name"]))
    check("tick_gcp(step) == (end_policy == window) on every untriggered step, and every triggered step a window "
          "step (%d steps)" % stepped, mismatched, [])
    check("(C6.0; C6.1; WO-114) the triggered steps with api hooks or events (no per-tick fix, a window all the same)",
          triggered, ["headless-android-causes/moving", "headless-android-kill/kill", "headless-android/move"])

    # (Tier C, C3.1) must_fail: `<scenario>` inverts every step of it, `<scenario>/<step>` that step alone —
    # never a scenario, or a step, whose name merely starts with the one named.
    rt_steps = ["activity-app-logger", "geolocation", "geolocation-filter"]
    check("must_fail: a bare scenario inverts every step of it, and nothing of api-roundtrip-android",
          ([must_fail({"must_fail": ["api-roundtrip"]}, "api-roundtrip", s) for s in rt_steps],
           must_fail({"must_fail": ["api-roundtrip"]}, "api-roundtrip-android", "app")), ([True] * 3, False))
    check("must_fail: `<scenario>/<step>` inverts that step alone — geolocation, not geolocation-filter",
          [must_fail({"must_fail": ["api-roundtrip/geolocation"]}, "api-roundtrip", s) for s in rt_steps],
          [False, True, False])

    def var_load(name, fake=None):
        if fake is not None:
            globals()["_load"] = lambda path: (json.loads(json.dumps(fake))
                                               if path.endswith(os.sep + name + ".yaml") else real_load(path))
        try:
            variant(name)
            return "loaded"
        except SystemExit as e:
            return str(e)
        finally:
            globals()["_load"] = real_load
    var_live, var_gone = {"native": "x"}, {"native": "x", "retired": "2026-09-26 (selftest): gone"}
    check("variant: `<scenario>` and `<scenario>/<step>` entries that exist load",
          var_load("selftest-var", dict(var_live, must_fail=["recreate-watch", "api-roundtrip/geolocation-filter"])),
          "loaded")
    malformed = ["api-roundtrip/", "/geolocation", "api-roundtrip/geolocation/x", "api roundtrip", "", 3, None]
    check("variant: a malformed must_fail entry refuses to load, retired or not",
          [["neither" in var_load("selftest-var", dict(v, must_fail=[e])) for v in (var_live, var_gone)]
           for e in malformed],
          [[True, True]] * len(malformed))
    check("variant: a must_fail that is not a list refuses to load — `in` a string matched substrings",
          "is a list" in var_load("selftest-var", dict(var_live, must_fail="api-roundtrip")), True)
    missing = {"api-roundtrip/geolocation-filtr": "has no step", "api-rountrip": "not a scenario",
               "api-rountrip/geolocation-filter": "not a scenario", "recreate-watch/step1": "has no step",
               "API-Roundtrip": "not a scenario", "API-ROUNDTRIP/geolocation-filter": "not a scenario"}
    check("variant: a live variant naming a step or a scenario that does not exist refuses to load — by its "
          "exact name: the lab Mac's filesystem finds API-Roundtrip.yaml, must_fail() would not",
          [why in var_load("selftest-var", dict(var_live, must_fail=[e])) for e, why in missing.items()],
          [True] * len(missing))
    check("variant: ... a retired one loads: it is only re-scored, whatever its scenarios became",
          [var_load("selftest-var", dict(var_gone, must_fail=[e])) for e in missing], ["loaded"] * len(missing))
    # (Fresh clone) native_source: what a label was built from, so `./labkit setup` can build it on a new machine.
    sha_a, sha_i = "f29892dcfb693a103beeb179d438aa21b782da5c", "ee6d0da0f186d8d44c841773727ce3a430fc7391"
    check("variant: native_source loads — both commits, one, and the iOS versioning bump beside its commit",
          [var_load("selftest-var", dict(var_live, native_source=src)) for src in (
              {"android": sha_a, "ios": sha_i}, {"android": sha_a},
              {"ios": sha_i, "ios_version": "4.7.1", "ios_build": 4701})], ["loaded"] * 3)
    for title, src, needle in [
            ("a short commit refuses", {"android": sha_a[:7]}, "40-character"),
            ("neither side refuses", {"ios_version": "4.7.1", "ios_build": 4701}, "native_source is"),
            ("an unknown key refuses", {"android": sha_a, "ios_sha": sha_i}, "native_source is"),
            ("ios_version without ios_build refuses", {"ios": sha_i, "ios_version": "4.7.1"}, "come together"),
            ("... and without an ios commit", {"android": sha_a, "ios_version": "4.7.1", "ios_build": 4701},
             "come together"),
            ("... and as a number YAML read (4.7)", {"ios": sha_i, "ios_version": 4.7, "ios_build": 4701},
             "come together")]:
        check("variant: native_source — " + title, needle in var_load("selftest-var", dict(var_live, native_source=src)),
              True)
    fix_src = variant("FIX").get("native_source") or {}
    check("variant: FIX records the commits of both halves of its label, so a fresh clone can build what SUITE runs "
          "(`./labkit setup`)", sorted(k for k in ("android", "ios") if k in fix_src), ["android", "ios"])
    every_step = ["%s/%s" % (f[:-len(".yaml")], st["name"]) for f in os.listdir(os.path.join(HARNESS, "scenarios"))
                  if f.endswith(".yaml") for st in scenario(f[:-len(".yaml")])["steps"]]
    check("variant: every `<scenario>/<step>` scenario() yields loads, default step names included (%d)"
          % len(every_step), var_load("selftest-var", dict(var_live, must_fail=every_step)), "loaded")

    def lagging(load, name):
        """load(name) while roundtrip.yaml has no entry for a Config leaf, as after a types reseed."""
        rt_was, globals()["_RT_TABLE"] = globals()["_RT_TABLE"], None
        globals()["_load"] = lambda path: ({k: v for k, v in real_load(path).items() if k != "logger.logMaxDays"}
                                           if path.endswith(os.sep + "roundtrip.yaml") else real_load(path))
        try:
            load(name)
            return "loaded"
        except SystemExit as e:
            return "no entry for logger.logMaxDays" in str(e) or str(e)
        finally:
            globals()["_load"], globals()["_RT_TABLE"] = real_load, rt_was
    check("variant: loads while roundtrip.yaml lags vocabulary.yaml, which only the scenario it names refuses",
          (lagging(scenario, "api-roundtrip"), lagging(variant, "MUT-pre-wo051")), (True, "loaded"))
    # inventory.py reports a variant that no longer loads, and still counts the label it names, so --prune
    # keeps that.  Its own `labkit` (this file runs as __main__), pointed at a scratch harness.
    import inventory
    tmp = tempfile.mkdtemp(prefix="labkit-inventory-")
    harness_was = inventory.labkit.HARNESS
    try:
        os.makedirs(os.path.join(tmp, "scenarios"))
        os.makedirs(os.path.join(tmp, "variants"))
        with open(os.path.join(tmp, "variants", "selftest-dead.yaml"), "w") as f:
            yaml.safe_dump({"native": "selftest-label", "must_fail": ["selftest-gone"]}, f)
        inventory.labkit.HARNESS = tmp
        try:
            dead = inventory.variants()["selftest-dead"]
            got = (dead["native"], "not a scenario" in dead.get("broken", ""))
        except SystemExit as e:
            got = str(e)
        check("inventory: a live variant naming a scenario that is gone is reported, and its label still named",
              got, ("selftest-label", True))
    finally:
        inventory.labkit.HARNESS = harness_was
        shutil.rmtree(tmp)
    on_disk = sorted(f[:-len(".yaml")] for f in os.listdir(os.path.join(HARNESS, "variants")) if f.endswith(".yaml"))
    check("variant: every variant on disk loads, retired ones included (%d)" % len(on_disk),
          [var_load(v) for v in on_disk], ["loaded"] * len(on_disk))
    pre = variant("MUT-pre-wo051")
    check("variant: MUT-pre-wo051 inverts api-roundtrip's geolocation-filter step and no other",
          [st["name"] for st in scenario("api-roundtrip")["steps"] if must_fail(pre, "api-roundtrip", st["name"])],
          ["geolocation-filter"])
    with contextlib.redirect_stderr(io.StringIO()):
        aci = scenario("api-config-invalid")
    check("(Tier C, C4.4) variant: MUT-pre-wo051 inverts api-config-invalid's filter-range step and no other",
          [st["name"] for st in aci["steps"] if must_fail(pre, "api-config-invalid", st["name"])], ["filter-range"])
    check("(Tier C, C4.4) ... a step of the filter leaves' range kinds alone, and no neighbour it holds is recorded but "
          "its own leaf — so a neighbour that moved reads `want-same-as-`, the token the arm counts (README, Variants)",
          [(sorted({h["kind"] for h in st["hooks"] if "kind" in h}), sorted({h["sets"].rsplit(".", 1)[0] for h in st["hooks"]
                                                                              if "sets" in h}),
            all(c.get("record") == (p == "$." + h["sets"]) or "same" in c
                for h in st["hooks"] if "kind" in h for p, c in h["returns"].items()))
           for st in aci["steps"] if st["name"] == "filter-range"],
          [(["above", "below", "float", "undeclared"], ["geolocation.filter"], True)])
    # (Tier C, C4.4 review) The plan's §4 Arms: filter-range asks kalmanProfile 3 (Android stores it: native §2.2) BEFORE
    # another filter leaf's set, so a pre-WO-051 partial block has a non-default value to reset under a neighbour check.
    fr = next((st["hooks"] for st in aci["steps"] if st["name"] == "filter-range"), [])
    k3 = next((i for i, h in enumerate(fr) if "kind" in h and h["args"] == {"geolocation": {"filter": {"kalmanProfile": 3}}}),
              None)
    check("(Tier C, C4.4 review) ... and it sets {kalmanProfile: 3} before another filter leaf's set",
          (k3 is not None, k3 is not None and any("kind" in h and h["sets"].startswith("geolocation.filter.")
                                                  and h["sets"] != "geolocation.filter.kalmanProfile" for h in fr[k3 + 1:])),
          (True, True))
    # (Tier C, C4.4 review) The plan's §2 C on the real expansion: every set records its own leaf and holds the rest of
    # its TOP-LEVEL group `same:` — the geolocation steps every geolocation.filter leaf too — except the one documented
    # pair, desiredAccuracy -> filter.policy, which every desiredAccuracy set records.
    with contextlib.redirect_stderr(io.StringIO()):
        j_sets = [(n, st["name"], h) for n in ("api-config-invalid", "api-config-invalid-android", "api-config-invalid-ios")
                  for st in scenario(n)["steps"] for h in st["hooks"] if "kind" in h]
    da, policy = "$.geolocation.desiredAccuracy", "$.geolocation.filter.policy"
    check("(Tier C, C4.4 review) every desiredAccuracy set records exactly desiredAccuracy and filter.policy (Option B)",
          sorted({json.dumps(sorted(p for p, c in h["returns"].items() if c.get(RECORD)))
                  for _, _, h in j_sets if h["sets"] == "geolocation.desiredAccuracy"}),
          [json.dumps([da, policy])])
    check("(Tier C, C4.4 review) every other generated set records exactly its own leaf, and holds every other path `same:`",
          [(n, st, h["sets"], h["kind"]) for n, st, h in j_sets if h["sets"] != "geolocation.desiredAccuracy" and (
              [p for p, c in h["returns"].items() if c.get(RECORD)] != ["$." + h["sets"]]
              or any(not c.get(RECORD) and set(c) != {"same"} for c in h["returns"].values()))], [])
    check("(Tier C, C4.4 review) geolocation-wrongtype asks desiredAccuracy the two empty containers (native §2.8)",
          sorted(json.dumps(h["args"]["geolocation"]["desiredAccuracy"]) for n, st, h in j_sets
                 if st == "geolocation-wrongtype" and h["sets"] == "geolocation.desiredAccuracy"
                 and isinstance(h["args"]["geolocation"]["desiredAccuracy"], (dict, list))), ["[]", "{}"])
    filt_both = ["$." + k for k in config_leaves() if k.startswith("geolocation.filter.") and state_scope("$." + k) is None]
    check("(Tier C, C4.4 review) every set of geolocation-range-bounds, -range-values, -null and -wrongtype holds every both-OS "
          "geolocation.filter leaf `same:`, but desiredAccuracy's filter.policy (recorded)",
          [(st, i, h["sets"], p) for n, st, h in j_sets for i, p in enumerate(filt_both)
           if n == "api-config-invalid" and st in ("geolocation-range-bounds", "geolocation-range-values", "geolocation-null",
                                                    "geolocation-wrongtype")
           and set(h["returns"].get(p) or {}) != ({RECORD} if h["sets"] == "geolocation.desiredAccuracy" and p == policy
                                                   else {"same"})], [])
    check("(Tier C, C4.4) api-config-invalid: each generated step asks one kind class, and every step fits IV_STEP_LIMIT",
          [(st["name"], len({IV_CLASS[h["kind"]] for h in st["hooks"] if "kind" in h}),
            len(launch_payloads(st)["hooksB64"]) <= IV_STEP_LIMIT) for st in aci["steps"] if "invalid_values" in st],
          [(st["name"], 1, True) for st in aci["steps"] if "invalid_values" in st])

    # (WO-027, Tier B) schedule_report: the driver's anchor and slots against what the step declared.
    sched = {"hooks": [{"api": "stop"}, {"api": "getState", "at_s": 4}],
             "events": [{"subscribe": "watchPosition", "at_s": 2.5}]}
    good = L("app=boot n=1 args={}", "app=anchor batchMs=812 slots=2.5,4 tickGcp=off",
             "app=slot at_s=2.5 late_ms=3", "app=slot at_s=4.0 late_ms=0")     # a Dart driver prints 4.0

    def report_of(lines, step=sched, meta=None):
        return schedule_report(Capture.live(lines, meta or {}), step)
    check("schedule: a good capture has no defect", report_of(good), [])
    check("schedule: a legacy step's anchor has no slots and the per-tick fix on",
          report_of(L("app=boot n=1 args={}", "app=anchor batchMs=0 slots=- tickGcp=on"),
                    {"hooks": [dict(legacy)], "events": []}), [])
    check("schedule: no anchor line is a stale driver",
          [d.split(" — ")[0] for d in report_of([l for l in good if "anchor" not in l])], ["no app=anchor line"])
    check("schedule: a declared at_s with no slot line",
          report_of([l for l in good if "at_s=2.5" not in l]),
          ["no app=slot line for at_s=2.5 — the slot never started"])
    check("schedule: a slot over 1000 ms late",
          report_of([l.replace("late_ms=3", "late_ms=1500") for l in good]),
          ["slot at_s=2.5 started 1500 ms late (over 1000)"])
    check("schedule: the driver's tickGcp disagrees with the host's",
          report_of([l.replace("tickGcp=off", "tickGcp=on") for l in good]),
          ["anchor tickGcp=on, the host expects off"])
    # (Tier C, C7.6) The boot fix's field: read against run.json in a capture from before, a stale
    # driver anywhere else.
    pre_c76 = [l.replace("tickGcp=off", "tickGcp=off bootGcp=on") for l in good]
    check("schedule: a capture from before C7.6 is read against the bootGcp its run.json recorded",
          [report_of(pre_c76, meta={"driver": {"bootGcp": told}}) for told in (True, False)],
          [[], ["anchor bootGcp=on, the host expects off"]])
    check("schedule: an anchor that still carries bootGcp, with nothing recorded, is a stale driver",
          [d.split(" — ")[0] for d in report_of(pre_c76, meta={"driver": {"tickGcp": False}})], ["anchor bootGcp=on"])

    # (WO-027) crashes counts the app under test, never the lab's own tooling.
    fatal = lambda proc: ["E/AndroidRuntime( 9): FATAL EXCEPTION: main", "E/AndroidRuntime( 9): " + proc,
                          "E/AndroidRuntime( 9): java.lang.RuntimeException: boom"]
    pkg = platform("cordova")["package"]
    check("crashes: the app's own process counts",
          crashes(capture(fatal("Process: %s, PID: 9" % pkg), [], platform="cordova")), 1)
    check("crashes: one of the app's :sub processes counts",
          crashes(capture(fatal("Process: %s:remote, PID: 9" % pkg), [], platform="cordova")), 1)
    check("crashes: the harness's uiautomator (a bare PID:, no Process:) does not",
          crashes(capture(["E/AndroidRuntime(21369): FATAL EXCEPTION: UiAutomation",
                           "E/AndroidRuntime(21369): PID: 21369"], [], platform="cordova")), 0)
    check("crashes: another app's process does not",
          crashes(capture(fatal("Process: com.google.android.gms, PID: 7"), [], platform="cordova")), 0)
    check("crashes: a capture that cannot name its platform still counts every block",
          crashes(capture(fatal("PID: 9"), [])), 1)
    anr = "E/ActivityManager(  512): ANR in %s (%s/.MainActivity)"
    check("crashes: an ANR in the app counts — it leaves no FATAL EXCEPTION",
          crashes(capture([anr % (pkg, pkg)], [], platform="cordova")), 1)
    check("crashes: another app's ANR does not",
          crashes(capture([anr % ("com.google.android.gms", "com.google.android.gms")], [], platform="cordova")), 0)
    # A native crash as a device logs it: libc's line names /proc/self/comm, the process name's last
    # 15 characters; crash_dump's header, a moment later, carries the whole of it.
    sig = "F/libc    ( 4321): Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0 in tid 4330 (mqt_js), pid 4321 (%s)"
    dump = "F/DEBUG   ( 4400): pid: 4321, tid: 4330, name: mqt_js  >>> %s <<<"
    check("crashes: a native crash in the app counts once, from crash_dump's header",
          crashes(capture([sig % pkg[-15:], dump % pkg], [], platform="cordova")), 1)
    check("crashes: ... in one of its :sub processes too",
          crashes(capture([sig % ("%s:remote" % pkg)[-15:], dump % ("%s:remote" % pkg)], [], platform="cordova")), 1)
    check("crashes: a native crash in another process does not",
          crashes(capture([sig % "surfaceflinger", dump % "/system/bin/surfaceflinger"], [], platform="cordova")), 0)

    # ------------------------------------------------------------------ verdict_view (WO-027)
    # Timestamped as capture() writes them — and as peek() returns them: normalize() only rewrites
    # a logcat line it can date, so bare native `I/LAB` lines would read as absent and pass vacuously.
    def live(ls):
        return Capture.live(["09-21 10:00:05.%03d %s" % (n, l) for n, l in enumerate(ls)], {})
    step = {"hooks": hooks, "events": events}
    mid = green[:4]                                          # stop + getCount reported, nothing else yet
    v = verdict_view(live(mid), step)
    check("live: what has reported is judged, the rest is pending",
          v["rows"], {"stop#0": "ok", "getCount#2": "ok", "getLocations#3": "pending",
                      "insertLocation#4": "pending", "destroyLocation#5": "pending",
                      "onProviderChange@0": "pending", "onEnabledChange@1": "pending"})
    v = verdict_view(live(swap(mid, "path=$ type=number",
                                "I/LAB     ( 1): app=ret id=getCount#2 path=$ type=number value=46 parent=1")), step)
    check("live: a wrong value fails its row, and its path carries the reason",
          (v["rows"]["getCount#2"], v["paths"]["getCount#2"]), ("fail", {"$": "want-45-got-46"}))
    v = verdict_view(live(mid), step, final=True)
    check("final: nothing is pending — a hook that never ran is the metric's own reason",
          (v["rows"]["getLocations#3"], v["rows"]["onProviderChange@0"]), ("never-called", "never-armed"))
    short = fired[:-1]                                       # onEnabledChange delivered once of min 2
    check("live: an event short of its `min` is still pending",
          verdict_view(live(short), {"events": events})["rows"]["onEnabledChange@1"], "pending")
    check("final: the same row fails with eventAssertions' reason",
          verdict_view(live(short), {"events": events}, final=True)["rows"]["onEnabledChange@1"],
          "fired-1/2")
    check("the view's failures are the metric's failures, row for row",
          sorted(k for k, r in verdict_view(live(short), {"events": events}, final=True)["rows"].items()
                 if r != "ok"),
          sorted(f.split("=")[0] for f in eventAssertions(capture(short, None, events)).split(", ")
                 if "=" in f and not f.startswith("arm-incomplete")))

    # ------------------------------------------------------------------ Grace (WO-027, 2026-09-24)
    app = "com.transistorsoft.labs.cordova"
    start = ("I/ActivityManager(  512): Background started FGS: Allowed [callingPackage: %s; callingUid: 10200; "
             "intent: Intent { act=start cmp=%s/com.transistorsoft.locationmanager.service.LocationRequestService }; "
             "code:PROC_STATE_TOP]" % (app, app))
    promoted = "D/TSLocationManager( 4321):   \U0001F7E2  start [LocationRequestService  startId: 1, eventCount: 1]"
    for title, os_name, step, want in [
            ("iOS: 2 s after the evidence by default", "ios", {}, 7),
            ("iOS: a watching step takes the default 2 s too (Tier B, B5)", "ios", {"watch": True}, 7),
            ("iOS: ... and so does an events: declaration subscribing watchPosition", "ios",
             {"events": [{"subscribe": "watchPosition"}]}, 7),
            ("iOS: a geofence step takes the default 2 s too (Tier C, C7.4)", "ios", {"hooks": [{"api": "addGeofence"}]}, 7)]:
        check(title, Grace(step, os_name, app).update([], 10, 5), want)
    g = Grace({}, "android", app)
    check("Android: a start awaiting promotion keeps the step open", g.update([start], 6, 5), None)
    check("Android: promoted — the grace ends 2 s after the evidence",
          (g.update([start, promoted], 6, 5), g.promotions), (7, 1))
    check("Android: a later start, already promoted, does not push the end back",
          g.update([start, promoted, start, promoted], 9, 5), 7)
    check("Android: a new start re-opens it", g.update([start, promoted, start], 9, 5), None)
    gg = Grace({}, "android", app)
    check("Android: a start still unpaired at the cap ends at the cap, held (the old rule)",
          (gg.update([start], 18, 5), gg.record()["rule"], gg.record()["grace_s"]), (17, "held", END_GRACE_S))
    check("Android: the services started are what dumpsys is asked about",
          (g.started, g.record()["grace_s"]), ({"LocationRequestService"}, FGS_GRACE_S))
    for title, lines, extra in [
            ("Android: a veto holds to the cap",
             [start, "W/TSLocationManager( 4321): Cannot promote LocationRequestService: FGS permissions not declared"], {}),
            ("Android: the app's crash holds to the cap", [start, promoted, "E/AndroidRuntime( 4321): FATAL EXCEPTION: main",
                                                           "E/AndroidRuntime( 4321): Process: %s, PID: 4321" % app], {}),
            ("Android: Android's missed-deadline line for the app holds to the cap",
             [start, "W/ActivityManager(  512): Bringing down service while still waiting for start foreground: "
                     "ServiceRecord{7d9e9bd u0 %s/com.transistorsoft.locationmanager.service.LocationRequestService}" % app], {}),
            ("Android: a promotion with no logged start holds to the cap (the device does not log starts)",
             [promoted], {}),
            ("Android: a disallowed start holds to the cap", [start.replace("Allowed [", "Disallowed [")], {})]:
        gg = Grace({}, "android", app)
        check(title, (gg.update(lines, 6, 5), bool(gg.held)), (17, True))
    check("Android: another app's start is not the app's", Grace({}, "android", app).update(
        [start.replace("cmp=%s/" % app, "cmp=com.google.android.gms/")], 6, 5), 7)
    check("Android: the harness's uiautomator crashing (no Process:) holds nothing",
          Grace({}, "android", app).update([start, promoted, "E/AndroidRuntime(21369): FATAL EXCEPTION: UiAutomation",
                                            "E/AndroidRuntime(21369): PID: 21369"], 6, 5), 7)
    check("iOS records no foreground-service floor", Grace({}, "ios", app, deadline_s=10).record()["floor_s"], 0.0)
    check("a non-final step keeps the old rule", Grace({}, "ios", app, final_step=False).update([], 6, 5), 17)
    check("a 10 s deadline floors the Android grace at 17 - 10 s",
          Grace({}, "android", app, deadline_s=10).update([start, promoted], 6, 5), 12)
    check("fgs_deadline_s: device_config's value, else 30 s from API 33, else 10 s",
          (fgs_deadline_s({"fgs_timeout_ms": "15000"}), fgs_deadline_s({"sdk": "36"}), fgs_deadline_s({"sdk": "30"})),
          (15.0, 30.0, 10.0))

    # ------------------------------------------------------ permissions (WO-027, permissions)
    # Multi-step scenarios through scenario() itself, so the defaults, the refusals and the expect
    # check are the loader's own.
    rp = {"api": "requestPermission", "args": "location", "returns": {"$": {"type": "number", "equals": 4}}}
    first = {"name": "a", "hooks": [rp], "expect": {"crashes": 0}}
    later = {"name": "b", "hooks": [rp], "expect": {"crashes": 0}}

    def load_steps(steps, **top):
        fake = dict({"platforms": platforms(), "trigger": "none", "steps": steps}, **top)
        globals()["_load"] = lambda path: (json.loads(json.dumps(fake))
                                           if path.endswith(os.sep + "selftest-perm.yaml") else real_load(path))
        try:
            return scenario("selftest-perm")["steps"]
        except SystemExit as e:
            return str(e)
        finally:
            globals()["_load"] = real_load

    def keys_of(steps):
        return steps if isinstance(steps, str) else [(s["permissions"], s["fresh"]) for s in steps]
    check("permissions: every step defaults to `grant`, not fresh",
          keys_of(load_steps([first, later])), [("grant", False), ("grant", False)])
    check("permissions: `keep` on a later step loads", keys_of(load_steps([first, dict(later, permissions="keep")])),
          [("grant", False), ("keep", False)])
    for title, steps, top, needle in [
            ("`permissions: keep` on the first step refuses to load",
             [dict(first, permissions="keep"), later], {}, "on the first step"),
            ("`permissions: keep` with `fresh: true` refuses to load",
             [first, dict(later, permissions="keep", fresh=True)], {}, "with `fresh: true`"),
            ("`permissions: keep` with a `prompt:` refuses to load",
             [first, dict(later, permissions="keep", prompt=["ACCESS_FINE_LOCATION"])], {}, "with a `prompt:`"),
            ("`permissions: keep` with a `revoke:` refuses to load",
             [first, dict(later, permissions="keep", revoke=["ACCESS_BACKGROUND_LOCATION"])], {}, "with a `revoke:`"),
            ("... and a scenario-level `prompt:` it would inherit",
             [first, dict(later, permissions="keep")], {"prompt": ["ACCESS_FINE_LOCATION"]}, "with a `prompt:`"),
            ("`permissions` is grant or keep", [first, dict(later, permissions="revoke")], {}, "grant or keep"),
            ("`fresh` is a boolean", [first, dict(later, fresh="yes")], {}, "true or false"),
            ("an `expect` key no metric computes refuses to load, not after the device run",
             [first, dict(later, expect={"undeclaredDialog": 0})], {}, "no metric computes"),
            ("... in a `platform_expect` block too",
             [first, dict(later, platform_expect={"flutter": {"crashs": 0}})], {}, "no metric computes"),
            # (Tier C, C7.6)
            ("`bootGcp` at scenario level refuses to load, naming why", [first, later], {"bootGcp": False},
             BOOT_GCP_RETIRED),
            ("`view: state` refuses to load, naming its retirement", [dict(first, view="state"), later], {},
             "`view: state` was retired"),
            ("`view` is api, events or title", [first, dict(later, view="table")], {}, "api, events or title")]:
        got = load_steps(steps, **top)
        check(title, isinstance(got, str) and needle in got, True)
    # (Tier C, C7.6) A step that names no view gets the one its declarations call for.
    onloc = {"subscribe": "onLocation", "returns": {"$.uuid": {"type": "string"}}}
    derived = [load_steps([dict({"name": "v", "expect": {"crashes": 0}}, **decl)])
               for decl in ({"events": [onloc], "hooks": [rp]}, {"hooks": [rp]}, {"hooks": [dict(legacy)]}, {})]
    check("view: events where a step declares events, else api where it has an api hook, else title",
          [d if isinstance(d, str) else d[0]["view"] for d in derived], ["events", "api", "title", "title"])
    check("view: a scenario-level `view: title` reaches a step with api hooks",
          [s["view"] for s in load_steps([first], view="title")], ["title"])
    # Scenario-level defaults propagate like every other step key, and a step overrides them.
    check("permissions: scenario-level `fresh: true` reaches every step that does not override it",
          keys_of(load_steps([first, later, dict(later, name="c", fresh=False)], fresh=True)),
          [("grant", True), ("grant", True), ("grant", False)])
    check("permissions: scenario-level `permissions: keep` reaches a later step; the first overrides it",
          keys_of(load_steps([dict(first, permissions="grant"), later], permissions="keep")),
          [("grant", False), ("keep", False)])

    def plan(value):
        try:
            return _dialog_plan("selftest", value)
        except SystemExit as e:
            return str(e)
    check("dialog: `cancel` answers the SDK's own window on both OSes, and `keep` loads on iOS",
          plan({"android": ["allow", "cancel"], "ios": ["allow", "keep"]}),
          {"android": ["allow", "cancel"], "ios": ["allow", "keep"]})
    check("dialog: `cancel` alone is both OSes'", plan("cancel"), {"android": ["cancel"], "ios": ["cancel"]})
    for title, value in [("dialog: `keep` on Android refuses to load, naming the kill", {"android": ["keep"]}),
                         ("dialog: ... and so does `keep` written for both OSes", "keep")]:
        check(title, "kills the running app" in str(plan(value)), True)

    # undeclaredDialogs, and the marks dialogAnswered must NOT read.
    marks = ["I/LAB     ( 1): point=dialog action=allow button=Allow_While_Using_App",
             "I/LAB     ( 1): point=dialog seen=allow_once|allow_while_using_app|dont_allow",
             "I/LAB     ( 1): point=dialog missed=cancel",
             "I/LAB     ( 1): point=dialog seen=cancel|settings"]
    cap = capture(marks, [])
    check("undeclaredDialogs counts the observer's seen= marks over the whole log (both before the trigger)",
          undeclaredDialogs(cap), 2)
    check("dialogAnswered reads only action= marks — seen= and missed= never join it as '-'",
          dialogAnswered(cap), "allow")
    check("dialogAnswered with only observer marks is '-', and a clean log counts 0 windows",
          (dialogAnswered(capture(marks[1:], [])), undeclaredDialogs(capture(marks[:1], []))), ("-", 0))
    check("evidence: a seen= or missed= mark is not an answered action",
          evidence_state(L("app=boot n=1 args={}", "app=tick n=1", "app=api-done n=2", "app=event id=onEnabledChange@0 n=2",
                           "app=event id=onGeofence@1 n=1", "point=dialog seen=allow|deny", "point=dialog missed=allow"),
                         ev_step, "android")[0], False)

    # An absence assertion ends on evidence, but not 2 s after it (DIALOG_GRACE_S, both OSes).
    absent = {"hooks": [rp], "events": [], "trigger": "none", "expect": {"crashes": 0, "undeclaredDialogs": 0}}
    check("undeclaredDialogs is an early metric: its step still ends on evidence (and so keeps tick_gcp's rule)",
          (end_policy(absent, "cordova"), tick_gcp(absent)), ("evidence", False))
    check("grace: a step asserting undeclaredDialogs waits DIALOG_GRACE_S past its evidence on iOS",
          (Grace(absent, "ios", app).update([], 10, 5), Grace(absent, "ios", app).record()["grace_s"]),
          (5 + DIALOG_GRACE_S, DIALOG_GRACE_S))
    ga = Grace(absent, "android", app)
    check("grace: ... and on Android, once every start is promoted",
          (ga.update([start, promoted], 6, 5), ga.record()["grace_s"]), (5 + DIALOG_GRACE_S, DIALOG_GRACE_S))
    check("grace: a platform_expect naming it floors the grace too; a step without it keeps 2 s",
          (Grace(dict(absent, expect={"crashes": 0}, platform_expect={"flutter": {"undeclaredDialogs": 0}}),
                 "ios", app).update([], 10, 5), Grace(dict(absent, expect={"crashes": 0}), "ios", app).update([], 10, 5)),
          (5 + DIALOG_GRACE_S, 7))
    # A non-final step is held to the cap before any floor is read ("a non-final step keeps the old
    # rule", above), so only a final step can show the floor.  Here a short deadline's own floor
    # (17 - 10 s = 7) is below it: 12 without DIALOG_GRACE_S.
    check("grace: a short deadline's own floor does not replace the absence floor — the larger wins",
          Grace(absent, "android", app, deadline_s=10).update([start, promoted], 6, 5), 5 + DIALOG_GRACE_S)

    # H7/H8's pure rules (backends.py), on synthetic dumps shaped like the lab's: what a window IS
    # (by its buttons), and when a tap has ANSWERED it.  Both OSes' dialogs share one label set.
    import backends
    import run
    pc = "com.android.permissioncontroller:id/"

    def node(text, cls="TextView", rid="", checked=None, children=""):
        return ('<node index="0" text="%s" resource-id="%s" class="android.widget.%s" checkable="%s" '
                'checked="%s" bounds="[96,1500][984,1620]">%s</node>'
                % (text, rid, cls, "false" if checked is None else "true", "true" if checked else "false", children))

    def ui(*nodes, pkg="com.google.android.permissioncontroller"):
        return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">"
                '<node index="0" text="" class="android.widget.FrameLayout" package="%s">%s</node></hierarchy>'
                % (pkg, "".join(nodes)))

    def radio(label, checked):          # a RadioButtonPreference row: the text is not the radio
        return node("", "LinearLayout", children=node("", "LinearLayout", "android:id/widget_frame",
                                                      children=node("", "RadioButton", checked=checked))
                    + node("", "RelativeLayout", children=node(label, rid="android:id/title")))

    def page(always):                   # Android 11+'s app-permission page, which `always` taps
        return ui(node("Location permission", rid=pc + "entity_header_title"),
                  radio("Allow all the time", always), radio("Allow only while using the app", not always),
                  radio("Ask every time", False), radio("Don’t allow", False),
                  node("", "LinearLayout", children=node("Use precise location") + node("", "Switch", checked=True)))
    location = ui(node("Allow Lab to access this device’s location?", rid=pc + "permission_message"),
                  node("Precise"), node("Approximate"),
                  node("While using the app", "Button", pc + "permission_allow_foreground_only_button"),
                  node("Only this time", "Button", pc + "permission_allow_one_time_button"),
                  node("Don’t allow", "Button", pc + "permission_deny_button"))
    motion = ui(node("Allow Lab to access your physical activity?", rid=pc + "permission_message"),
                node("Allow", "Button", pc + "permission_allow_button"),
                node("Don’t allow", "Button", pc + "permission_deny_button"))
    lab = ui(node("requestPermission"), node("Settings", "Button"), node("OK", "Button"), pkg=app)
    labels = backends.dump_labels
    check("dump_labels: entities decoded, and a text uiautomator delimited with ' (it swaps, never escapes, a \")",
          labels(ui(node("Motion &amp; Fitness"), "<node index=\"0\" text='Change to \"Allow all the time\"' />")),
          ["Motion & Fitness", 'Change to "Allow all the time"'])

    sig = backends.dialog_signature
    ios_location = ["Allow “Lab” to use your location?", "Allow Once", "Allow While Using App", "Don’t Allow"]
    ios_motion = ["“Lab” Would Like to Access Your Motion & Fitness Activity", "Don’t Allow", "OK"]
    check("dialog_signature: the location and motion dialogs, both OSes, by their buttons, mark-safe",
          (sig(labels(location)), sig(labels(motion)), sig(ios_location), sig(ios_motion)),
          ("dont_allow|only_this_time|while_using_the_app", "allow|dont_allow",
           "allow_once|allow_while_using_app|dont_allow", "dont_allow|ok"))
    check("dialog_signature: generic words alone are nobody's window — Settings + Cancel untitled, OK, Allow",
          (sig(["Lab", "requestPermission", "Settings", "Cancel", "OK", "Allow"]), sig(labels(lab))), (None, None))
    check("dialog_signature: under an SDK_ALERT_TITLES title, Settings + Cancel is the SDK's Settings alert",
          sig(["Background location is not enabled", "Open Settings to allow Always.", "Cancel", "Settings"]),
          "cancel|settings")

    gone = backends.window_gone
    check("window_gone: the tapped label left; a swallowed tap did not; an unreadable re-read is not gone",
          (gone("While using the app", labels(location), labels(lab)),
           gone("Don’t allow", labels(location), labels(location)), gone("Don’t allow", labels(location), None)),
          (True, False, False))
    check("window_gone: location -> motion, the same deny label on a DIFFERENT window, is gone (both OSes)",
          (gone("Don’t allow", labels(location), labels(motion)), gone("Don’t Allow", ios_location, ios_motion)),
          (True, True))

    rc = backends.radio_checked
    ambiguous = ui(node("", "RadioGroup", children=node("", "RadioButton", checked=True)
                        + node("", "RadioButton", checked=False) + node("Allow all the time")))
    check("radio_checked: checked, unchecked; None where two radios share the row, on a plain button, unreadable",
          (rc(page(True), "Allow all the time"), rc(page(False), "Allow all the time"),
           rc(ambiguous, "Allow all the time"), rc(location, "While using the app"), rc(None, "Allow all the time")),
          (True, False, None, None, None))

    answered = backends.android_answered
    check("android_answered: deny, location -> motion — still the permission controller, yet answered",
          answered("deny", "Don’t allow", location, motion), True)
    check("android_answered: deny swallowed, or a re-dump that failed, is not answered; allow gone to the app is",
          (answered("deny", "Don’t allow", location, location), answered("deny", "Don’t allow", location, None),
           answered("allow", "While using the app", location, lab)), (False, False, True))
    check("android_answered: always — the page stays up; only a radio still unchecked is a swallowed tap",
          (answered("always", "Allow all the time", page(False), page(True)),
           answered("always", "Allow all the time", page(False), page(False)),
           answered("always", "Allow all the time", page(False), ambiguous)), (True, False, True))
    check("android_answered: back — the permission controller has gone, and a failed re-dump is not that",
          (answered("back", "KEYCODE_BACK", page(True), lab), answered("back", "KEYCODE_BACK", page(True), page(True)),
           answered("back", "KEYCODE_BACK", page(True), None)), (True, False, False))

    # (Tier C, C7.5) Adb.answer_dialog's gone-check on scripted dumps: the re-dump follows the tap with
    # no sleep, a window still up keeps the tap pending, and the dump that confirmed a tap is the next
    # call's first look, once.
    rationale = ui(node("Allow Lab to access this device’s location even when closed?"),
                   node("Change to Allow all the time", "Button"), node("Cancel", "Button"), pkg=app)

    class Scripted(backends.Adb):
        def __init__(self, *dumps):
            backends.Adb.__init__(self, "emulator-0")
            self.dumps, self.sent = list(dumps), []

        def _dump(self):                     # an exhausted script reads as an unreadable dump
            return self.dumps.pop(0) if self.dumps else None

        def _adb(self, *args, **kw):
            self.sent.append(args[2])        # "tap" or "keyevent": every input the host sends
            return ""

    def answers(dev, *actions):
        return [dev.answer_dialog(a, {}) for a in actions]

    real_sleep, slept = backends.time.sleep, []
    backends.time.sleep = slept.append
    try:
        full = Scripted(location, rationale, page(False), page(True), lab)
        check("answer_dialog: an Always sequence reads 5 dumps, not 8 — each proof is the next look — and never sleeps",
              (answers(full, "allow", "rationale", "always", "back"), full.sent, full.dumps, slept),
              (["While using the app", "Change to Allow all the time", "Allow all the time", "KEYCODE_BACK"],
               ["tap", "tap", "tap", "keyevent"], [], []))
        late = Scripted(location, location, lab)
        first = answers(late, "allow", "allow")
        check("answer_dialog: a window still up right after the tap stays pending; the next look confirms that same "
              "tap, tapping nothing", (first[0], first[1], late.sent, late.dumps), (None, "While using the app", ["tap"], []))
        stuck = Scripted(location, location, location, lab)
        again = answers(stuck, "allow", "allow")
        check("answer_dialog: still up on the next look too — a swallowed tap — is tapped again",
              (again, stuck.sent, stuck.dumps), ([None, "While using the app"], ["tap", "tap"], []))
        radio_late = Scripted(page(False), page(False), page(True))
        check("answer_dialog: always — the radio still unchecked right after the tap is pending, then reads checked",
              (answers(radio_late, "always", "always"), radio_late.sent), ([None, "Allow all the time"], ["tap"]))
        back_late = Scripted(page(True), page(True), lab)
        check("answer_dialog: back — the page still up right after the press is pending; gone, it is one press",
              (answers(back_late, "back", "back"), back_late.sent), ([None, "KEYCODE_BACK"], ["keyevent"]))
        late_next = Scripted(location, lab, rationale, lab)
        check("answer_dialog: a reused dump that predates the next window is followed by a fresh read in the same "
              "call — no poll lost to it", (answers(late_next, "allow", "rationale"), late_next.sent, late_next.dumps),
              (["While using the app", "Change to Allow all the time"], ["tap", "tap"], []))
        once = Scripted(location, rationale, lab, None)
        once.answer_dialog("allow", {})
        once.answer_dialog("rationale", {})
        check("answer_dialog: the confirming dump is used ONCE — taken by the next call, then gone",
              (once._fresh is not None, once.answer_dialog("back", {}), once._fresh, once.dumps), (True, None, None, []))
        seen = Scripted(location, lab, motion)
        seen.answer_dialog("allow", {})
        check("answer_dialog: visible_dialog never takes the confirming dump — the observer reads the screen afresh",
              (seen.visible_dialog({}), seen.dumps), ("allow|dont_allow", []))
        other = Scripted(location, location, location, lab)
        check("answer_dialog: a tap pending for one action is dropped by another's call, as before",
              (answers(other, "allow", "deny"), other.sent, other.dumps), ([None, "Don’t Allow"], ["tap", "tap"], []))
    finally:
        backends.time.sleep = real_sleep

    # (Tier C, C4.4 pilot) Adb.launch on a scripted adb.  A launch that did not happen raises — adb's own failure is on
    # stderr, which the old check never read — and a command past adb's one-request cap is refused unsent.
    class Proc:
        def __init__(self, returncode, stdout, stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
    real_call, calls = backends.labkit.device_call, []

    def launched(reply, extras, package="com.x"):
        calls.clear()
        backends.labkit.device_call = lambda argv, **kw: calls.append(argv) or reply
        try:
            backends.Adb("emulator-0").launch({"package": package}, ".MainActivity", extras)
            return "launched"
        except SystemExit as e:
            return str(e)
        finally:
            backends.labkit.device_call = real_call
    started = "Starting: Intent { cmp=com.x/.MainActivity }\nStatus: ok\nLaunchState: COLD\nComplete\n"
    check("(Tier C, C4.4 pilot) Adb.launch: adb's own failure (exit 1, `error: closed` on stderr) raises, naming it; so "
          "does am's (a missing Activity) and an exit 0 with no `Status:`; `Status: ok` launches",
          [launched(Proc(1, "", "error: closed\n"), {}),
           "Error type 3" in launched(Proc(1, "Starting: Intent { cmp=com.x/.Nope }\nError type 3\n"), {}),
           launched(Proc(0, ""), {}), launched(Proc(0, started), {"hooksB64": "e30="})],
          ["launch failed (adb exit 1): error: closed", True, "launch failed (adb exit 0): no output", "launched"])
    head = len("shell,v2,raw:am start -W -n com.x/.MainActivity --es k ")
    at, past = ({"k": "A" * (ADB_COMMAND_LIMIT - head + extra)} for extra in (0, 1))
    check("(Tier C, C4.4 pilot) Adb.launch: a command of ADB_COMMAND_LIMIT B is sent; one byte more is refused before "
          "adb is called", (launched(Proc(0, started), at), len(calls),
                            "over the %d B adb sends" % ADB_COMMAND_LIMIT in launched(Proc(0, started), past), len(calls)),
          ("launched", 1, True, 0))
    # LAUNCH_LIMIT's 2 KiB reserve holds the rest of a real launch: the largest J step with run.py's other extras.
    fr_step = next(st for st in aci["steps"] if st["name"] == "filter-range")
    fr_extras = launch_payloads(fr_step)
    launched(Proc(0, started), dict(fr_extras, scenario="api-config-invalid", nativeLabel="4.7.1-master-f29892d",
                                    view="api", stopOnTerminate=True, watch=False, noStart=False, persistGcp=False),
             package="com.transistorsoft.labs.capacitor")
    rest = len(("shell,v2,raw:" + " ".join(calls[0][4:])).encode()) - sum(map(len, fr_extras.values()))
    check("(Tier C, C4.4 pilot) the rest of a launch command fits LAUNCH_LIMIT's reserve under ADB_COMMAND_LIMIT (%d B)"
          % rest, (0 < rest <= ADB_COMMAND_LIMIT - LAUNCH_LIMIT, ADB_COMMAND_LIMIT), (True, 65535))

    # H9: `dumpsys package`'s runtime permissions (a second denial: USER_SET|USER_FIXED).  The first
    # user's section wins; flags that do not decide whether the next request asks are dropped.
    sensitive = "USER_SENSITIVE_WHEN_GRANTED|USER_SENSITIVE_WHEN_DENIED"
    dumpsys = "\n".join([
        "Packages:", "  Package [%s] (5b1e0c1):" % app, "    requested permissions:",
        "      android.permission.ACCESS_FINE_LOCATION", "    install permissions:",
        "      android.permission.FOREGROUND_SERVICE: granted=true",
        "    User 0: ceDataInode=10427 installed=true hidden=false suspended=false stopped=false enabled=0",
        "      runtime permissions:"] + [
        "        android.permission.%s: granted=%s, flags=[ %s]" % row for row in [
            ("ACCESS_FINE_LOCATION", "false", "USER_SET|USER_FIXED|" + sensitive),
            ("ACCESS_COARSE_LOCATION", "false", "USER_SET|USER_FIXED|" + sensitive),
            ("ACCESS_BACKGROUND_LOCATION", "false", sensitive + "|RESTRICTION_INSTALLER_EXEMPT"),
            ("ACTIVITY_RECOGNITION", "true", "USER_SET|" + sensitive)]] + [
        "    User 10: ceDataInode=0 installed=true", "      runtime permissions:",
        "        android.permission.ACCESS_FINE_LOCATION: granted=true, flags=[ USER_SET]"])
    check("permission_flags_summary: one mark-safe field per permission, the first user's section",
          backends.permission_flags_summary(dumpsys),
          "ACCESS_FINE_LOCATION:denied:USER_SET|USER_FIXED,ACCESS_COARSE_LOCATION:denied:USER_SET|USER_FIXED,"
          "ACCESS_BACKGROUND_LOCATION:denied:-,ACTIVITY_RECOGNITION:granted:USER_SET")
    check("permission_flags_summary: one not listed reads `NAME:?`; a package not installed is None",
          (backends.permission_flags_summary("        android.permission.ACCESS_FINE_LOCATION: granted=true"),
           backends.permission_flags_summary("Unable to find package: %s" % app)),
          ("ACCESS_FINE_LOCATION:granted:-,ACCESS_COARSE_LOCATION:?,ACCESS_BACKGROUND_LOCATION:?,"
           "ACTIVITY_RECOGNITION:?", None))
    check("mark_value: a straight apostrophe cannot close Adb.mark's shell quote; spaces are _, a \" stays",
          (run.mark_value("Don't allow"), run.mark_value(' Change to "Allow all the time" ')),
          ("Don’t_allow", 'Change_to_"Allow_all_the_time"'))

    # (Tier C, flutter#1715) offMainGetWindow: the Activity's own report, the highest count wins (a
    # report per resume, cumulative), '-' when it never reported — which `expect: 0` scores red.
    def window_report(*ls):
        return offMainGetWindow(Capture.live(["09-26 10:00:0%d.000 I/LAB     ( 1): %s" % (n, l)
                                              for n, l in enumerate(ls)], {}))
    # (WO-107) alongsideMs: the largest of the driver's reports, '-' when none — which `expect: "<N"` scores red.
    def alongside_report(*ls):
        return alongsideMs(Capture.live(["10-02 10:00:0%d.000 I/LAB     ( 1): %s" % (n, l)
                                         for n, l in enumerate(ls)], {}))
    check("alongsideMs: one report, the largest of several, '-' for none",
          (alongside_report("app=alongside id=addGeofences#1 api=getCount ms=41"),
           alongside_report("app=alongside id=addGeofences#1 api=getCount ms=8123",
                            "app=alongside id=addGeofences#3 api=getCount ms=12"),
           alongside_report("app=api id=addGeofences#1 outcome=ok")),
          (41, 8123, "-"))
    # (WO-107) alongsideLeadMs: the last polygon's build line less the alongside line — the shapes measured 2026-10-02.
    built = "D/TSLocationManager( 1): [TSGeofence$Builder build] [MiniBall] Minimum Enclosing Circle: 45.5 / -73.6, radius: 3"
    beside = "I/LAB     ( 1): app=alongside id=addGeofences#2 api=getCount outcome=ok ms=%d"
    def lead_report(*ls):
        return alongsideLeadMs(Capture.live(["10-02 10:00:%s %s" % l for l in ls], {}))
    check("alongsideLeadMs: a bridge that waited reads <= 0, one that answered first leads by the build, '-' "
          "without either line",
          (lead_report(("01.000", built), ("01.200", built), ("01.203", beside % 253)),
           lead_report(("01.000", built), ("01.005", beside % 75), ("01.149", built)),
           lead_report(("01.005", beside % 75)),
           lead_report(("01.000", built))),
          (-3, 144, "-", "-"))
    check("offMainGetWindow: 0 reported, 1 from the self-test thread, the highest of several, '-' for none",
          (window_report("app=window offMain=0 threads=-"),
           window_report("app=window offMain=1 threads=lab-probe-selftest"),
           window_report("app=window offMain=0 threads=-", "app=window offMain=2 threads=pool-1,pool-2"),
           window_report("app=tick n=1 enabled=false")),
          (0, 1, 2, "-"))
    check("decor-race-1715: run expects 0, selftest launches with probeSelfTest and expects exactly 1",
          [(st["name"], st["expect"].get("offMainGetWindow"), st["extras"].get("probeSelfTest", False))
           for st in scenario("decor-race-1715")["steps"]],
          [("run", 0, False), ("selftest", 1, True)])
    # (RN #2677) mainConfigWaits / mainConfigWaitMs: StartupWatch's summary, '-' when no watch ran — which
    # `expect: 0` scores red.  An open wait counts in both.
    def watch_report(fn, *ls):
        return fn(Capture.live(["10-28 10:00:0%d.000 I/LAB     ( 1): %s" % (n, l) for n, l in enumerate(ls)], {}))
    waited = ("point=configwait n=1 ms=2984 open=false via=ReactContext.onHostResume from=RNBackgroundGeolocationModule"
              ".getAdapter holder=pool-3-thread-1 holderTop=Thread.sleep holderIn=TSConfig.<init>")
    watches = [("point=configwatch waits=0 maxMs=0 totalMs=0 open=false watchMs=8000",),
               (waited, "point=configwatch waits=1 maxMs=2984 totalMs=2984 open=false watchMs=8000"),
               ("point=configwait n=1 ms=7950 open=true via=ReactContext$1.run",
                "point=configwatch waits=1 maxMs=7950 totalMs=7950 open=true watchMs=8000"),
               (waited, "app=tick n=1 enabled=false"),
               # the summary's other kind, sdkwait: its fields are not these
               ("point=configwatch waits=0 maxMs=0 totalMs=0 open=false sdkWaits=2 sdkMaxMs=41 sdkOpen=false "
                "watchMs=8000",)]
    check("mainConfigWaits / mainConfigWaitMs: none, one, an open one, '-' without the watch's summary, not sdkWaits",
          ([watch_report(mainConfigWaits, *w) for w in watches], [watch_report(mainConfigWaitMs, *w) for w in watches]),
          ([0, 1, 1, "-", 0], [0, 2984, 7950, "-", 0]))
    check("mainSdkWaitMs: the summary's sdkMaxMs, 0 from a summary before the field, '-' without the watch's summary",
          [watch_report(mainSdkWaitMs, *w) for w in watches], [0, 0, 0, "-", 41])
    check("startup-config-wait-2677: every step watches; slow-config expects no wait, selftest exactly one",
          [(st["name"], st["expect"].get("mainConfigWaits"), st["extras"].get("startupProbe"),
            st["extras"].get("slowConfigMs", 0), st["extras"].get("configWaitSelfTest", False))
           for st in scenario("startup-config-wait-2677")["steps"]],
          [("first-launch", "any", True, 0, False), ("relaunch", "any", True, 0, False),
           ("slow-config", 0, True, 3000, False), ("selftest", 1, True, 1500, True)])

    # ================================================================ (Tier C, C5.0)
    # ---- item 1: the vocabulary.  Every namespace the default export carries is an enum under `enums:`, whose
    # `members:` are its values, name by name; the headless names are Event's and 'locationerror'.
    vocab = vocabulary()
    spaces = sorted(m["returns"][len("typeof "):] for m in vocab["api"].values() if str(m.get("returns")).startswith("typeof "))
    check("(C5.0) vocabulary: all 15 enum namespaces carry `members:`, each list equal to `values:`",
          (len(spaces), [n for n in spaces if list((vocab["enums"].get(n) or {}).get("members", {}).values())
                         != (vocab["enums"].get(n) or {}).get("values")]), (15, []))
    check("(C5.0) vocabulary: YAML 1.1 cannot read LogLevel.Off as a boolean — the seed quotes it",
          vocab["enums"]["LogLevel"]["members"].get("Off"), 0)
    headless = ts_values(vocab["types"]["interfaces"]["HeadlessEvent"]["name"])
    check("(C5.0) vocabulary: HeadlessEvent.name admits Event's 18 names and 'locationerror'",
          (len(headless), "locationerror" in headless, "powersavechange" in headless), (19, True, True))
    check("(C5.0) vocabulary: the Logger and DeviceSettings members, `ORDER_ASC: 1` a literal type",
          (sorted(vocab["members"]), vocab["members"]["Logger"]["ORDER_ASC"]["returns"],
           vocab["members"]["DeviceSettings"]["showPowerManager"]["returns"]),
          (["DeviceSettings", "Logger"], "1", "DeviceSettingsRequest"))

    def refused(fn):
        try:
            fn()
            return "loaded"
        except SystemExit as e:
            return str(e)

    def events_step(*decls, **step):
        return dict({"settle": 20, "after": 25, "events": [json.loads(json.dumps(d)) for d in decls]}, **step)
    filt = {"subscribe": "onLocationFilter", "declared": "both"}
    step_ = events_step(filt)
    _validate_events("selftest", step_)
    got = step_["events"][0]["returns"]
    check("(C5.0) seeded: `declared: both` on onLocationFilter types its reason and its `?` geofence, under it",
          (got.get("$.reason"), got.get("$.geofence"), got.get("$.geofence.outsideBy"), "$.location.coords.latitude" in got),
          ({"type": "string", "in": ["geofence-duplicate-enter", "geofence-spurious-exit", "implied-speed", "low-accuracy",
                                     "outlier-capped"]},
           {"type": "object", "optional": True}, {"type": "number", "under": "$.geofence"}, True))
    for title, decl in [("onConnectivityChange's $.connected equals true", {"subscribe": "onConnectivityChange",
                                                                           "returns": {"$.connected": {"equals": True}}}),
                        ("onHttp's status and responseText", {"subscribe": "onHttp", "returns": {
                            "$.status": {"type": "number"}, "$.responseText": {"type": "string"}}}),
                        ("onActivityChange's activity, a MotionActivityType", {"subscribe": "onActivityChange",
                                                                               "returns": {"$.activity": {"equals": "still"}}}),
                        ("onAuthorization's error, `string | null`", {"subscribe": "onAuthorization",
                                                                      "returns": {"$.error": {"type": "null"}}}),
                        ("onHeartbeat's location", {"subscribe": "onHeartbeat",
                                                    "returns": {"$.location.coords.latitude": {"type": "number"}}})]:
        check("(C5.0) seeded: %s loads" % title, refused(lambda: _validate_events("selftest", events_step(decl))), "loaded")

    # ---- item 2: the event keys, at load
    loc, en = {"subscribe": "onLocation", "returns": {"$.uuid": {"type": "string"}}}, \
        {"subscribe": "onEnabledChange", "returns": {"$": {"type": "boolean"}}}
    hooks_ = [{"api": "stop", "returns": {"$.enabled": {"type": "boolean"}}},
              {"api": "removeListeners", "at_s": 8, "returns": {"$": {"type": "null|undefined"}}}]
    for title, step_, want in [
            ("`max: -1` refuses", events_step(dict(en, max=-1)), "`max` is a whole number"),
            ("`min: 0` without `max` refuses — it would assert nothing", events_step(dict(en, min=0)), "0 only beside a `max`"),
            ("`max: 0` with the default `min` refuses — write `min: 0`", events_step({"subscribe": "onEnabledChange",
                                                                                       "max": 0}), "is above `max: 0`"),
            ("`max: 0` asserting a payload refuses — nothing is delivered on a pass",
             events_step(dict(en, min=0, max=0)), "drop `returns`"),
            ("`min: 0, max: 0` without `returns` loads", events_step({"subscribe": "onEnabledChange", "min": 0, "max": 0}),
             "loaded"),
            ("`arm_only` undated refuses", events_step({"subscribe": "onAuthorization", "arm_only": "someday"}), "dated"),
            ("`arm_only` beside `min` refuses", events_step({"subscribe": "onAuthorization", "min": 1,
                                                              "arm_only": "2026-09-27 no 401 server"}), "asserts deliveries"),
            ("`arm_only` judging a path refuses", events_step({"subscribe": "onAuthorization", "arm_only": "2026-09-27 x",
                                                               "returns": {"$.status": {"type": "number"}}}), "judges none"),
            ("`arm_only` recording a path loads", events_step({"subscribe": "onAuthorization", "arm_only": "2026-09-27 x",
                                                               "returns": {"$.status": {"record": True}}}), "loaded"),
            ("`arm_only` alone loads", events_step({"subscribe": "onActivityChange", "arm_only": "2026-09-27 x"}), "loaded"),
            ("(decision 25) `record: true` on an event path loads", events_step(dict(loc, returns={"$.uuid": {"record": True}})),
             "loaded"),
            ("`remove_at_s` not after its arm refuses", events_step(dict(loc, at_s=8, remove_at_s=8)), "not after its arm"),
            ("`remove_at_s` past the window refuses", events_step(dict(loc, remove_at_s=40)), "only watches for 45 s"),
            ("`none_after: remove` without `remove_at_s` refuses", events_step(dict(loc, none_after="remove")),
             "removes nothing"),
            ("`none_after: remove` with it loads", events_step(dict(loc, remove_at_s=8, none_after="remove")), "loaded"),
            ("`none_after` naming the wrong hook refuses", events_step(dict(loc, none_after="removeListeners#0"),
                                                                        hooks=hooks_), "is not removeListeners"),
            ("`none_after: removeListeners#1` loads", events_step(dict(loc, none_after="removeListeners#1"), hooks=hooks_),
             "loaded"),
            ("`none_after` on a declaration that removes nothing refuses",
             events_step(dict(loc, none_after="onLocation@1"), loc), "removes nothing (no `remove_at_s`)"),
            ("`none_after: move0` with no moves refuses", events_step(dict(loc, none_after="move0")), "declares 0 move"),
            ("`none_after` of no known shape refuses", events_step(dict(loc, none_after="later")), "an anchor is"),
            ("`min_after: {ref: remove}` refuses", events_step(dict(loc, min_after={"ref": "remove", "n": 1})),
             "its own removal"),
            ("`min_after` with `n: 0` refuses", events_step(dict(loc, min_after={"ref": "removeListeners#1", "n": 0}),
                                                             hooks=hooks_), "at least 1"),
            ("`min_after: {ref: onLocation@0}` loads beside a removed declaration",
             events_step(dict(loc, remove_at_s=8), dict(loc, min_after={"ref": "onLocation@0", "n": 1})), "loaded")]:
        got = refused(lambda: _validate_events("selftest", step_))
        check("(C5.0) events: %s" % title, want in got or got, True)
    check("(C5.0) events: the device is told `remove_at_s`, never `max`, `none_after`, `min_after` or `arm_only`",
          wire_events([dict(loc, remove_at_s=8, none_after="remove", max=3),
                       {"subscribe": "onActivityChange", "arm_only": "2026-09-27 x"}]),
          [{"on": "onLocation", "id": "onLocation@0", "paths": ["$.uuid"], "remove_at_s": 8},
           {"on": "onActivityChange", "id": "onActivityChange@1", "paths": []}])

    # ---- item 2: the event keys, scored
    def ev(*ls):
        return ["I/LAB     ( 1): app=" + l for l in ls]
    armed2 = ev("event-arm id=onLocation@0 outcome=ok", "event-arm id=onLocation@1 outcome=ok", "events-armed n=2")
    decls = [dict(loc, remove_at_s=8, none_after="remove"), dict(loc, min_after={"ref": "onLocation@0", "n": 1})]
    ret0 = ev("ret id=onLocation@0 path=$.uuid type=string value=u parent=1")
    ret1 = ev("ret id=onLocation@1 path=$.uuid type=string value=u parent=1")
    before = ev("event id=onLocation@0 n=1") + ret0 + ev("event id=onLocation@1 n=1") + ret1
    for title, lines_, want in [
            ("removed, silent, and the control delivered after", armed2 + before + ev(
                "event-removed id=onLocation@0 n=1 outcome=ok", "event id=onLocation@1 n=2"), "ok"),
            ("a driver from before R removes nothing: `never-removed`", armed2 + before + ev("event id=onLocation@1 n=2"),
             "onLocation@0=never-removed, onLocation@1=no-anchor-onLocation@0"),
            ("a removal that threw", armed2 + before + ev("event-removed id=onLocation@0 n=1 outcome=threw",
                                                          "event id=onLocation@1 n=2"), "onLocation@0=removed-threw"),
            ("delivered after its own removal", armed2 + before + ev("event-removed id=onLocation@0 n=1 outcome=ok",
                                                                     "event id=onLocation@0 n=2", "event id=onLocation@1 n=2"),
             "onLocation@0=deliveredAfterRemove-1"),
            ("the control silent after the removal", armed2 + before + ev("event-removed id=onLocation@0 n=1 outcome=ok"),
             "onLocation@1=fired-0/1-after-onLocation@0")]:
        check("(C5.0) scored: %s" % title, eventAssertions(capture(lines_, [], decls)), want)
    api_decls = [dict(en, none_after="removeListeners#1")]
    api_arm = ev("event-arm id=onEnabledChange@0 outcome=ok", "events-armed n=1", "event id=onEnabledChange@0 n=1",
                 "ret id=onEnabledChange@0 path=$ type=boolean value=true parent=1")
    check("(C5.0) scored: `none_after: <api>#<n>` counts the deliveries after the hook's own line",
          [eventAssertions(capture(api_arm + ev(*more), hooks_, api_decls)) for more in (
              ["api id=removeListeners#1 outcome=ok"],
              ["api id=removeListeners#1 outcome=ok", "event id=onEnabledChange@0 n=2"],
              ["event id=onEnabledChange@0 n=2"])],
          ["ok", "onEnabledChange@0=deliveredAfterRemove-1", "onEnabledChange@0=no-anchor-removeListeners#1"])
    bounded = [{"subscribe": "onGeofence", "min": 0, "max": 0}, dict(en, max=2)]
    bound_arm = ev("event-arm id=onGeofence@0 outcome=ok", "event-arm id=onEnabledChange@1 outcome=ok", "events-armed n=2",
                   "event id=onEnabledChange@1 n=1", "ret id=onEnabledChange@1 path=$ type=boolean value=true parent=1",
                   "event id=onEnabledChange@1 n=2")
    check("(C5.0) scored: `max` bounds the deliveries — none at all, and at most 2",
          [eventAssertions(capture(bound_arm + ev(*more), [], bounded)) for more in (
              [], ["event id=onGeofence@0 n=1", "event id=onEnabledChange@1 n=3"])],
          ["ok", "onGeofence@0=fired-1-over-max-0, onEnabledChange@1=fired-3-over-max-2"])
    check("(C5.0) live: `never-removed` and `no-anchor-` wait, `fired-<n>-over-max-<m>` is final",
          verdict_view(capture(bound_arm + ev("event id=onEnabledChange@1 n=3") + armed2 + before, [], None),
                       {"events": [bounded[0], bounded[1]] + decls})["rows"],
          {"onGeofence@0": "ok", "onEnabledChange@1": "fired-3-over-max-2",
           "onLocation@2": "pending", "onLocation@3": "pending"})
    # A move anchor is the stamp of the mark the host writes before it sends, against each delivery's own stamp
    # (capture() stamps 10:00:05.<n>, a line's place in the list).
    sent = int(stamp_ms("09-21 10:00:05.005 x", 2026))
    mv_decl = [dict(en, none_after="move0")]
    mv_begin = ["I/LAB     ( 2): point=move-begin i=0"]
    mv_sent = ["I/LAB     ( 2): point=move i=0 hop=1/1 to=49.2827,-123.117254 sent_ms=%d mark_ms=%d" % (sent, sent)]
    mv_lines = api_arm + mv_begin + mv_sent
    check("(C5.0; C5.3 review) scored: `none_after: move0` counts the deliveries stamped after the move's begin mark",
          [eventAssertions(capture(mv_lines + more, [], mv_decl, moves=[{"after": [{"api": "stop#0"}],
                                                                          "to": {"offset_m": 250, "bearing": 90}}]))
           for more in ([], ev("event id=onEnabledChange@0 n=2"))],
          ["ok", "onEnabledChange@0=deliveredAfterMove-1"])
    arm_only = [{"subscribe": "onAuthorization", "arm_only": "2026-09-27 no 401 server"},
                dict(loc, returns={"$.uuid": {"record": True}, "$.coords.latitude": {"type": "number"}})]
    ao_lines = ev("event-arm id=onAuthorization@0 outcome=ok", "event-arm id=onLocation@1 outcome=ok", "events-armed n=2",
                  "event id=onLocation@1 n=1", "ret id=onLocation@1 path=$.uuid type=string value=u9 parent=1",
                  "ret id=onLocation@1 path=$.coords.latitude type=number value=49.28 parent=1", "event id=onLocation@1 n=2")
    cap_ = capture(ao_lines, [], arm_only)
    check("(C5.0) scored: `arm_only` passes on its arm, with nothing delivered; a recorded event path is never red",
          eventAssertions(cap_), "ok")
    check("(C5.0) decision 25: the `recorded` row carries each recording declaration's deliveries and paths",
          METRICS["recorded"](cap_), "onAuthorization@0.delivered=0, onLocation@1.delivered=2, onLocation@1.$.uuid=string:u9")
    check("(C5.0) decision 25: an event that records makes the `recorded` row implicit",
          expectations({"events": arm_only}, "cordova").get("recorded"), "any")

    # ---- item 2: evidence_state for the event keys (the host's live read)
    def ev_state(lines_, step_):
        return evidence_state(["09-21 10:00:0%d.000 I/LAB     ( 7): app=%s" % (min(n, 9), l)
                               for n, l in enumerate(["boot n=1"] + lines_)], step_, "android")[0]
    check("(C5.0) evidence: a removal completes once the driver logs it; arm_only once armed; min 0 at once",
          [ev_state(["event-arm id=onLocation@0 outcome=ok", "event id=onLocation@0 n=1"],
                    {"events": [dict(loc, remove_at_s=8)]}),
           ev_state(["event-arm id=onLocation@0 outcome=ok", "event id=onLocation@0 n=1",
                     "event-removed id=onLocation@0 n=1 outcome=ok"], {"events": [dict(loc, remove_at_s=8)]}),
           ev_state(["event-arm id=onAuthorization@0 outcome=ok"], {"events": arm_only[:1]}),
           ev_state([], {"events": arm_only[:1]}),
           ev_state(["event-arm id=onGeofence@0 outcome=ok"], {"events": bounded[:1]})],
          [False, True, True, False, True])
    ma = {"events": decls}
    check("(C5.0) evidence: `min_after` completes only on a delivery after its anchor",
          [ev_state(["event-arm id=onLocation@0 outcome=ok", "event-arm id=onLocation@1 outcome=ok",
                     "event id=onLocation@0 n=1", "event id=onLocation@1 n=1",
                     "event-removed id=onLocation@0 n=1 outcome=ok"] + more, ma)
           for more in ([], ["event id=onLocation@1 n=2"])], [False, True])
    check("(C5.0) schedule: `remove_at_s` is a slot the driver must start, and a retuned one is no spec change",
          (schedule_report(Capture.live(["09-21 10:00:01.000 I/LAB     ( 1): app=anchor batchMs=5 slots=- tickGcp=off"], {}),
                           {"events": [dict(loc, remove_at_s=8)]}),
           _asked(dict(loc, remove_at_s=8)) == _asked(dict(loc, remove_at_s=9))),
          (["no app=slot line for at_s=8 — the slot never started"], True))

    # ---- item 3a: moves and actions
    def haversine(a, b):
        (p1, l1), (p2, l2) = [(math.radians(x), math.radians(y)) for x, y in (a, b)]
        return 2 * EARTH_R_M * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2
                                                   + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    check("(C5.0) moves: 250 m and 1000 m due east of the standing fix, to within the 6-decimal rounding (0.1 m) — the "
          "analysis INFERRED -123.117254 for the first, 0.07 m off",
          [(destination(m, 90), abs(haversine(STANDING_FIX, destination(m, 90)) - m) < 0.1) for m in (250, 1000)],
          [((49.2827, -123.117253), True), ((49.282699, -123.106914), True)])

    check("(C5.0) moves: three equal hops to 250 m, then a second move onward from there",
          move_plan({"moves": [{"to": {"offset_m": 250, "bearing": 90}, "hops": 3, "every_s": 3},
                               {"to": {"offset_m": 1000, "bearing": 90}}]}),
          [[(49.2827, -123.119551), (49.2827, -123.118402), (49.2827, -123.117253)], [(49.282699, -123.106914)]])
    fence = [{"subscribe": "onGeofence", "returns": {"$.action": {"equals": "ENTER"}}},
             {"subscribe": "onGeofence", "at_s": 10, "returns": {"$.action": {"equals": "EXIT"}}}]
    gp = [{"api": "stop", "returns": {"$.enabled": {"type": "boolean"}}},
          {"api": "changePace", "args": True, "at_s": 2, "returns": {"$.isMoving": {"type": "boolean"}}}]

    def mv_step(moves=None, actions=None, **kw):
        s = dict({"settle": 20, "after": 70, "events": json.loads(json.dumps(fence)), "hooks": gp}, **kw)
        if moves is not None:
            s["moves"] = json.loads(json.dumps(moves))
        if actions is not None:
            s["actions"] = json.loads(json.dumps(actions))
        return s
    jump = {"after": [{"event": "onGeofence ENTER"}, {"armed": "onGeofence@1"}], "delay_s": 8,
            "to": {"offset_m": 250, "bearing": 90}}
    s_ = mv_step([jump])
    _validate_moves("selftest", s_)
    check("(C5.0) moves: the ENTER sugar resolves to the one declaration asserting it, and the value it waits for",
          s_["moves"][0]["after"], [{"event": "onGeofence@0", "action": "ENTER"}, {"armed": "onGeofence@1"}])
    net = {"after": {"armed": "onGeofence@1"}, "network": {"wifi": False, "data": False}}
    for title, step_, excused, want in [
            ("a move with no `after:` refuses", mv_step([dict(jump, after=[])]), (), "names what the log must show"),
            ("sugar no declaration asserts refuses", mv_step([dict(jump, after=[{"event": "onGeofence DWELL"}])]), (),
             "0 onGeofence declarations"),
            ("a condition naming no declaration refuses", mv_step([dict(jump, after=[{"armed": "onGeofence@5"}])]), (),
             "no such declaration"),
            ("a condition of two kinds refuses", mv_step([dict(jump, after=[{"armed": "onGeofence@1", "api": "stop#0"}])]),
             (), "one of"),
            ("a move waiting on a later move refuses", mv_step([dict(jump, after=[{"move": 1}]), jump]), (),
             "waits for an EARLIER one"),
            ("hops without every_s refuses", mv_step([dict(jump, hops=3)]), (), "exactly when"),
            ("every_s on one hop refuses", mv_step([dict(jump, every_s=3)]), (), "exactly when"),
            ("an offset past 5 km refuses", mv_step([dict(jump, to={"offset_m": 6000, "bearing": 90})]), (), "(0, 5000]"),
            ("a bearing of 360 refuses", mv_step([dict(jump, to={"offset_m": 250, "bearing": 360})]), (), "[0, 360)"),
            ("a window too short for the move refuses", mv_step([dict(jump, observe_s=60)], after=25), (),
             "raise them to at least"),
            ("actions on a scenario that runs on iOS refuse", mv_step(actions=[net]), (), "excuses ios"),
            ("network with no switch refuses", mv_step(actions=[dict(net, network={})]), ("ios",), "one or both"),
            ("network `on` as a string refuses (YAML's on is true)", mv_step(actions=[dict(net, network={"wifi": "on"})]),
             ("ios",), "true|false"),
            ("an action after an api hook, on Android only, loads", mv_step(actions=[dict(net, after={"api": "changePace#1"})]),
             ("ios",), "loaded")]:
        got = refused(lambda: _validate_moves("selftest", step_, excused))
        check("(C5.0) moves: %s" % title, want in got or got, True)
    real_load = globals()["_load"]

    def c50_load(**spec):
        fake = dict({"platforms": ["cordova"], "steps": [{"name": "run"}]}, **spec)
        globals()["_load"] = lambda path: (json.loads(json.dumps(fake))
                                           if path.endswith(os.sep + "selftest-c50.yaml") else real_load(path))
        try:
            return refused(lambda: scenario("selftest-c50"))
        finally:
            globals()["_load"] = real_load
    check("(C5.0) moves: a scenario-level `moves:` refuses — it names one step's declarations",
          "a step's own key" in c50_load(moves=[jump]), True)
    check("(C5.0) moves: a scenario may not restate the implicit movesSent",
          "is implicit" in c50_load(steps=[dict(mv_step([jump]), name="run", expect={"movesSent": "ok"})]), True)
    host = lambda *ls: ["I/LAB     ( 2): point=" + l for l in ls]
    ent = ev("event-arm id=onGeofence@0 outcome=ok", "event-arm id=onGeofence@1 outcome=ok", "events-armed n=2",
             "event id=onGeofence@0 n=1", "ret id=onGeofence@0 path=$.action type=string value=ENTER parent=1")
    norm_ = lambda ls: [normalize("09-21 10:00:05.000 " + l) for l in ls]
    check("(C5.0) after_unmet: the sugar needs the ENTER value too; each kind is read from the log",
          [after_unmet(s_["moves"][0]["after"], norm_(ls), s_) for ls in (
              ent, swap(ent, "value=ENTER", ent[-1].replace("ENTER", "EXIT")), ent[:1])],
          [None, {"event": "onGeofence@0", "action": "ENTER"}, {"event": "onGeofence@0", "action": "ENTER"}])
    check("(C5.0) after_unmet: api settled, a previous move's last hop, a point",
          [after_unmet([cond], norm_(ls), {"moves": [{"hops": 2}]}) is None for cond, ls in (
              ({"api": "changePace#1"}, ev("api id=changePace#1 outcome=rejected")),
              ({"move": 0}, host("move i=0 hop=1/2 to=1,2 sent_ms=1")),
              ({"move": 0}, host("move i=0 hop=1/2 to=1,2 sent_ms=1", "move i=0 hop=2/2 to=1,2 sent_ms=2")),
              ({"point": "trigger"}, host("trigger none")),
              ({"point": "trig"}, host("trigger none")))],
          [True, False, True, True, False])
    hops_step = mv_step([dict(jump, hops=2, every_s=3, observe_s=5)])
    _validate_moves("selftest", hops_step)
    moves_meta = hops_step["moves"]
    for title, lines_, want in [
            ("every hop sent", ent + host("move i=0 hop=1/2 to=a sent_ms=1", "move i=0 hop=2/2 to=b sent_ms=2"), "ok"),
            ("one of two hops", ent + host("move i=0 hop=1/2 to=a sent_ms=1"), "move0=hop1/2"),
            ("never sent, and why", ent[:3], "move0=unsent(event:onGeofence@0)")]:
        check("(C5.0) movesSent: %s" % title, movesSent(capture(lines_, gp, fence, moves=moves_meta)), want)
    check("(C5.0) actionsDone: done, or undone and why",
          [actionsDone(capture(ls, gp, fence, actions=[dict(net, after=[net["after"]])])) for ls in (
              host("host i=0 action=network data=off wifi=off sent_ms=1 done_ms=2"), [])],
          ["ok", "action0=undone(armed:onGeofence@1)"])
    check("(C5.0) movesSent: a capture from before moves were declared is no pass", movesSent(capture([], [], None)),
          "no-moves-recorded")
    check("(C5.0) expect: moves imply movesSent, actions actionsDone",
          (expectations(hops_step, "cordova").get("movesSent"),
           expectations(mv_step(actions=[net]), "cordova").get("actionsDone")), ("ok", "ok"))

    def ms_lines(*pairs_):
        return ["09-21 10:00:%s I/LAB     ( %s): %s" % (ts, "7" if "app=" in l else "2", l) for ts, l in pairs_]
    boot = ("00.000", "app=boot n=1")
    done = [boot, ("01.000", "app=api-done n=2"), ("02.000", "app=event-arm id=onGeofence@0 outcome=ok"),
            ("02.100", "app=event-arm id=onGeofence@1 outcome=ok"), ("03.000", "app=event id=onGeofence@0 n=1")]
    check("(C5.0) evidence: a move's last hop, then `observe_s` of the device's log after it",
          [evidence_state(ms_lines(*pairs_), dict(hops_step, events=[dict(fence[0]), dict(fence[1], min=0, max=0)]),
                          "android")[0] for pairs_ in (
              done + [("12.000", "point=move i=0 hop=1/2 to=a sent_ms=1")],
              done + [("12.000", "point=move i=0 hop=1/2 to=a sent_ms=1"), ("15.000", "point=move i=0 hop=2/2 to=b sent_ms=2"),
                      ("19.000", "app=beat n=9")],
              done + [("12.000", "point=move i=0 hop=1/2 to=a sent_ms=1"), ("15.000", "point=move i=0 hop=2/2 to=b sent_ms=2"),
                      ("20.000", "app=beat n=9")])],
          [False, False, True])
    check("(C5.0) evidence: an action's mark",
          [evidence_state(ms_lines(*pairs_), {"hooks": gp, "actions": [net]}, "android")[0] for pairs_ in (
              [boot, ("01.000", "app=api-done n=2")],
              [boot, ("01.000", "app=api-done n=2"), ("02.000", "point=host i=0 action=network data=off wifi=off sent_ms=1")])],
          [False, True])

    # The move runner on a fake device: it waits for the log, then sends each hop `every_s` apart, marking each after it.
    import run, threading
    class FakeDev:
        def __init__(self, lines_):
            self.lines, self.calls, self.t0 = list(lines_), [], time.time()

        def peek(self):
            return list(self.lines)

        def move(self, lat, lon):
            self.calls.append(("move", lat, lon))

        def network(self, **kw):
            self.calls.append(("network", tuple(sorted(kw.items()))))

        def mark(self, text):
            self.calls.append(("mark", re.sub(r"(sent|done|mark)_ms=\d+", r"\1_ms=T", text)))
            self.lines.append("09-21 10:00:09.000 I/LAB     ( 2): point=" + text)
    fast = mv_step([dict(jump, delay_s=0, hops=2, every_s=1)], actions=[dict(net, after=[{"move": 0}])])
    _validate_moves("selftest", fast, ("ios",))
    dev_ = FakeDev(["09-21 10:00:0%d.000 I/LAB     ( 7): %s" % (n, l) for n, l in enumerate(
        ["app=boot n=1", "app=event-arm id=onGeofence@1 outcome=ok"])])
    host_ = {"error": None, "held_s": {}, "sent_s": {}}
    stop_ = threading.Event()
    th = threading.Thread(target=run.run_host, args=(dev_, fast, time.time(), time.time() + 6, stop_, host_), daemon=True)
    th.start()
    time.sleep(0.6)
    idle = list(dev_.calls)
    dev_.lines += ["09-21 10:00:03.000 I/LAB     ( 7): app=event id=onGeofence@0 n=1",
                   "09-21 10:00:03.000 I/LAB     ( 7): app=ret id=onGeofence@0 path=$.action type=string value=ENTER parent=1"]
    th.join(timeout=8)
    check("(C5.0) run_host: nothing before its conditions; then each hop, every_s apart, and the action after the move",
          (idle, dev_.calls, host_["error"], sorted(host_["sent_s"]), th.is_alive()),
          ([], [("move", 49.2827, -123.118977), ("mark", "move i=0 hop=1/2 to=49.2827,-123.118977 sent_ms=T mark_ms=T"),
                ("move", 49.2827, -123.117253), ("mark", "move i=0 hop=2/2 to=49.2827,-123.117253 sent_ms=T mark_ms=T"),
                ("network", (("data", False), ("wifi", False))),
                ("mark", "host i=0 action=network data=off wifi=off sent_ms=T done_ms=T mark_ms=T")],
           None, ["action0", "move0.1", "move0.2"], False))
    check("(C5.0) run_host: the hops were every_s apart", host_["sent_s"]["move0.2"] - host_["sent_s"]["move0.1"] >= 0.99,
          True)
    class Refusing(FakeDev):
        def move(self, lat, lon):
            raise SystemExit("move failed (adb exit 1): KO")
    dev_ = Refusing(ent_ := ["09-21 10:00:01.000 I/LAB     ( 7): app=" + l for l in (
        "boot n=1", "event-arm id=onGeofence@1 outcome=ok", "event id=onGeofence@0 n=1",
        "ret id=onGeofence@0 path=$.action type=string value=ENTER parent=1")])
    host_ = {"error": None, "held_s": {}, "sent_s": {}}
    run.run_host(dev_, fast, time.time(), time.time() + 3, threading.Event(), host_)
    check("(C5.0) run_host: a send that failed stops it and is kept, never swallowed (stop_runner leaves it unscored)",
          host_["error"], "move failed (adb exit 1): KO")

    # neutral(): wifi / data / battery saver read against the recorded default; put back only where the gate allows.
    import backends
    class FakeAdb(backends.Adb):
        def __init__(self, reads):
            backends.Adb.__init__(self, "emulator-5580")
            self.info, self.reads, self.sent = {"avd": "Lab1"}, reads, []

        def _adb(self, *args, **kw):
            self.sent.append(args)
            return self.reads if args[:1] == ("shell",) and "; " in args[1] else ""

        def network_default(self):
            return {"wifi": "1", "data": "1", "powerSave": "0"}
    calls_ = []
    real_call = backends.labkit.device_call
    backends.labkit.device_call = lambda argv, **kw: (calls_.append(argv[4:]), sp.CompletedProcess(argv, 0, "", ""))[1]
    gate = backends.NEUTRAL_WRITES_NETWORK
    check("(C5.1 Pilot B) the gate is open: svc took both Lab AVDs off and back on, and with it closed one red step left "
          "Lab2 offline for the next two", gate, True)
    try:
        launcher = "  mCurrentFocus=Window{d u0 com.google.android.apps.nexuslauncher/.NexusLauncherActivity}\n"
        neutral_ok = "0\n1.0\nNight mode: no\ntrue\n1\n1\n0\n" + launcher
        off = "0\n1.0\nNight mode: no\ntrue\n0\n0\n1\n" + launcher
        a = FakeAdb(neutral_ok)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            changed = a.neutral()
        check("(C5.0) neutral: all at their recorded defaults — nothing put, nothing differs, nothing printed",
              (changed, a.network_state["differs"], calls_, len(a.sent), out.getvalue()), (False, [], [], 1, ""))
        backends.NEUTRAL_WRITES_NETWORK = False
        a = FakeAdb(off)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            changed = a.neutral()
        check("(C5.0) neutral: off, gate closed — recorded and printed as a LAB DEFECT, nothing put",
              (changed, a.network_state, calls_, out.getvalue().split(" — ")[-1].strip()),
              (False, {"read": {"wifi": "0", "data": "0", "powerSave": "1"}, "default": {"wifi": "1", "data": "1",
                                                                                     "powerSave": "0"},
                       "differs": ["data", "powerSave", "wifi"], "put": False}, [],
               "not put back (backends.NEUTRAL_WRITES_NETWORK off)"))
        backends.NEUTRAL_WRITES_NETWORK = True
        a = FakeAdb(off)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            changed = a.neutral()
        check("(C5.0) neutral: gate open — put back as recorded, wifi before data when turning on, battery saver off; "
              "(C5.1 Pilot B) still printed as a LAB DEFECT: the step before left it so",
              (changed, calls_, a.sent[1:], a.network_state["put"], out.getvalue().split(" — ")[-1].strip()),
              (True, [["svc", "wifi", "enable"], ["svc", "data", "enable"]], [("shell", "cmd", "power", "set-mode", "0")],
               True, "put back"))
        a = FakeAdb(neutral_ok.replace("true", "false"))
        check("(C6.1) neutral: the master location switch left off (`actions: location`) is put back on, like a "
              "Configuration setting; wifi, data and battery saver untouched",
              (a.neutral(), a.sent[1:]), (True, [("shell", "cmd", "location", "set-location-enabled", "true")]))
        warned = neutral_ok.replace(launcher, "  mCurrentFocus=Window{a u0 com.google.android.gms/"
                                              "com.google.android.gms.location.settings.LocationOffWarningActivity}\n")
        a = FakeAdb(warned)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            changed = a.neutral()
        check("(C6.1 pilot) neutral: Play Services' location-off warning left focused is dismissed (BACK, then a "
              "re-read), printed as a LAB DEFECT, and settles nothing",
              (changed, a.sent[1:], "LAB DEFECT" in out.getvalue()),
              (False, [("shell", "input", "keyevent", "KEYCODE_BACK"), ("shell", FakeAdb.FOCUS)], True))
        calls_.clear()
        FakeAdb(neutral_ok).network(wifi=False, data=False)
        FakeAdb(neutral_ok).network(wifi=True, data=False)
        check("(C5.0) network: data off before wifi off; offs before ons", calls_,
              [["svc", "data", "disable"], ["svc", "wifi", "disable"], ["svc", "data", "disable"], ["svc", "wifi", "enable"]])
        # item 6: every simctl privacy call's return code is kept
        backends.labkit.device_call = lambda argv, **kw: sp.CompletedProcess(argv, 1 if "motion" in argv else 0, "",
                                                                                "denied" if "motion" in argv else "")
        sim = backends.Simctl("UDID")
        # (C5.1) and a failure refuses the step (unscored), after it is recorded
        check("(C5.0; C5.1) privacy: each simctl privacy call's return code is recorded, a failure's stderr with it, "
              "and a failure refuses the step",
              (refused(lambda: sim.permissions({"package": "com.x"}, [])).split(" failed")[0], sim.privacy),
              ("simctl privacy grant motion com.x", [{"call": "grant location-always", "rc": 0, "stderr": None},
                                                      {"call": "grant motion", "rc": 1, "stderr": "denied"}]))
        # (C5.2, sweep 20260928-144003) evict: another lab app, out of the step's way.  The run_step wiring is read
        # from its source: driving run_step whole needs a device.
        sent_ = []
        def sim_call(stays):
            def call(argv, **kw):
                sent_.append(argv[2])
                return sp.CompletedProcess(argv, 0, "/fake/app" if stays and argv[2] == "get_app_container" else "", "")
            return call
        backends.labkit.device_call = sim_call(False)
        backends.Simctl("UDID").evict("com.other")
        gone_ = list(sent_)
        backends.labkit.device_call = sim_call(True)
        stuck = refused(lambda: backends.Simctl("UDID").evict("com.other"))
        fa = FakeAdb("")
        fa.evict("com.other")
        import inspect
        check("(C5.2, sweep 20260928-144003) evict: another lab app is force-stopped on Android and uninstalled on "
              "iOS, where a stopped one's geofences relaunch it mid-step; one still installed refuses the step; and "
              "run_step evicts the others, never merely terminates them",
              (fa.sent, gone_, stuck.split(" left")[0],
               "dev.evict(package)" in inspect.getsource(run.run_step), "dev.terminate(package)" in inspect.getsource(run)),
              ([("shell", "am", "force-stop", "com.other")], ["uninstall", "get_app_container"],
               "simctl uninstall com.other", True, False))
        # (WO-191) What a first step evicts comes from the device, under every lab's prefix: the background-fetch lab
        # (com.transistorsoft.fetchlab.*) shares the Lab devices.  On Android every other Transistor app is force-stopped
        # too (D1); on iOS it is left installed and listed.  Never the app under test, never anyone else's.
        listed_ = ["com.google.android.gms", "com.transistorsoft.backgroundgeolocation.react",
                   "com.transistorsoft.fetchlab.react", "com.transistorsoft.labs.flutter", "com.transistorsoft.labs.react"]
        check("(WO-191) evictions: every lab prefix minus the app under test, plus D1's other Transistor apps on Android; "
              "on iOS those are listed, not uninstalled",
              (evictions(listed_, "com.transistorsoft.labs.react", "android"),
               evictions(listed_, "com.transistorsoft.labs.react", "ios")),
              ((["com.transistorsoft.backgroundgeolocation.react", "com.transistorsoft.fetchlab.react",
                 "com.transistorsoft.labs.flutter"], ["com.transistorsoft.backgroundgeolocation.react"]),
               (["com.transistorsoft.fetchlab.react", "com.transistorsoft.labs.flutter"],
                ["com.transistorsoft.backgroundgeolocation.react"])))

        class ListingAdb(FakeAdb):
            def _adb(self, *args, **kw):
                self.sent.append(args)
                return self.reads if args == ("shell", "pm", "list", "packages") else ""
        listapps_ = ('{\n    "com.apple.Preferences" =     {\n        CFBundleIdentifier = "com.apple.Preferences";\n'
                     '    };\n    "com.transistorsoft.fetchlab.react" =     {\n        CFBundleIdentifier = '
                     '"com.transistorsoft.fetchlab.react";\n        GroupContainers =         {\n        };\n    };\n}\n')
        backends.labkit.device_call = lambda argv, **kw: sp.CompletedProcess(argv, 0, listapps_, "")
        sim_listed = backends.Simctl("UDID").installed((VENDOR_PACKAGE_PREFIX,))
        backends.labkit.device_call = lambda argv, **kw: sp.CompletedProcess(argv, 0, "", "")
        sim_unread = refused(lambda: backends.Simctl("UDID").installed((VENDOR_PACKAGE_PREFIX,)))
        check("(WO-191) installed: `pm list packages` and `simctl listapps` (an old-style plist) read under the prefixes; "
              "a listing that reads as empty refuses the step, never passes as a clean device",
              (ListingAdb("package:com.google.android.gms\npackage:com.transistorsoft.labs.react\n"
                          "package:com.transistorsoft.fetchlab.react\n").installed((VENDOR_PACKAGE_PREFIX,)),
               refused(lambda: ListingAdb("").installed((VENDOR_PACKAGE_PREFIX,))).split(" listed")[0],
               sim_listed, sim_unread.split(" could")[0]),
              (["com.transistorsoft.fetchlab.react", "com.transistorsoft.labs.react"],
               "`pm list packages` on emulator-5580", ["com.transistorsoft.fetchlab.react"], "`simctl listapps UDID`"))
        check("(WO-191) run_step evicts from the device's listing (labkit.evictions), not from this repo's platforms(), "
              "and records what it evicted and the package under test in run.json",
              ["labkit.evictions(dev.installed(" in inspect.getsource(run.run_step),
               "labkit.platforms()" in inspect.getsource(run.run_step)]
              + ['"%s":' % k in inspect.getsource(run.run_step) for k in ("package", "evicted", "foreignTransistorApps")],
              [True, False, True, True, True])
    finally:
        backends.labkit.device_call = real_call
        backends.NEUTRAL_WRITES_NETWORK = gate

    # ---- the C5.0 review's fixes (workflow wf_c66a7d37-b93), each confirmed by a skeptic
    late0 = {"events": [{"subscribe": "onGeofence", "at_s": 10, "min": 0, "max": 0}]}
    check("(C5.0 review) evidence: a `min: 0` declaration in a later slot holds the step until it arms",
          [ev_state(ls, late0) for ls in ([], ["event-arm id=onGeofence@0 outcome=ok"])], [False, True])
    ao_rm = {"events": [{"subscribe": "onActivityChange", "arm_only": "2026-09-27 x", "remove_at_s": 8}]}
    check("(C5.0 review) evidence: an `arm_only` declaration that removes waits for its removal too",
          [ev_state(ls, ao_rm) for ls in (["event-arm id=onActivityChange@0 outcome=ok"],
                                          ["event-arm id=onActivityChange@0 outcome=ok",
                                           "event-removed id=onActivityChange@0 n=0 outcome=ok"])], [False, True])
    check("(C5.0 review) a declaration that starts or stops removing is a spec change; a retuned removal is not",
          (_asked(loc) == _asked(dict(loc, remove_at_s=8)), _asked(dict(loc, remove_at_s=8)) == _asked(dict(loc, remove_at_s=9))),
          (False, True))
    # (C5.3 review) The shape measured on three iOS cells of events-remove-all: the one fix a live watch is handed for
    # a move is stamped BEFORE the mark that follows the send (that mark's own transport, 0.3 s on the Simulator), so
    # an anchor made from that mark never counted it.  Read from the begin mark, it counts; and a log with no begin
    # mark has no anchor, whatever its send mark says.
    early = ev("event id=onEnabledChange@0 n=2")
    check("(C5.3 review) move anchors: a delivery between the send and the mark that follows it counts — the anchor "
          "is the mark written before the send; without that mark there is no anchor",
          ([eventAssertions(capture(api_arm + mv_begin + early + mv_sent, [], mv_decl)),
            eventAssertions(capture(api_arm + early + mv_sent, [], mv_decl))],
           _delivered_after([("09-21 10:00:05.000 x", "LAB point=move-begin i=10"),
                             ("09-21 10:00:06.000 x", "LAB app=event id=onEnabledChange@0 n=1")],
                            "onEnabledChange@0", "move1", 2026)),
          (["onEnabledChange@0=deliveredAfterMove-1", "onEnabledChange@0=no-anchor-move0"], None))
    check("(C5.3 review) move_anchors: the moves a declaration counts deliveries after, by either key; no other anchor",
          move_anchors({"events": [dict(en, none_after="move1"), dict(en, min_after={"ref": "move0", "n": 1}),
                                   dict(en, remove_at_s=8, none_after="remove"), dict(en, none_after="stop#0"), en]}),
          {0, 1})
    mv_min = {"events": [dict(en, min_after={"ref": "move0", "n": 1})],
              "moves": [{"after": [{"armed": "onEnabledChange@0"}], "to": {"offset_m": 20, "bearing": 90}}]}
    L7 = lambda *ls: ["09-21 10:00:0%d.000 I/LAB     ( 7): app=%s" % (n, l) for n, l in enumerate(ls)]
    live_ = L7("boot n=1", "event-arm id=onEnabledChange@0 outcome=ok", "event id=onEnabledChange@0 n=1")
    live_ += ["09-21 10:00:04.000 I/LAB     ( 2): point=move-begin i=0"]
    live_sent = ["09-21 10:00:06.000 I/LAB     ( 2): point=move i=0 hop=1/1 to=a sent_ms=1 mark_ms=2"]
    check("(C5.3 review) evidence: `min_after: {ref: move0}` completes on a delivery after the begin mark, in the "
          "live read as in the score",
          [evidence_state(live_ + more + live_sent, mv_min, "android")[0] for more in (
              [], ["09-21 10:00:05.000 I/LAB     ( 7): app=event id=onEnabledChange@0 n=2"])], [False, True])
    check("(C5.0 review) sent_on_device: the mark's stamp less mark_ms - sent_ms; its own stamp without mark_ms",
          (sent_on_device("09-21 10:00:05.010 I/LAB ( 2): x", "point=move i=0 hop=1/1 sent_ms=1000 mark_ms=1006", 2026)
           - stamp_ms("09-21 10:00:05.004 x", 2026),
           sent_on_device("09-21 10:00:05.010 I/LAB ( 2): x", "point=move sent_ms=9", 2026)
           == stamp_ms("09-21 10:00:05.010 x", 2026)), (0, True))
    sent2 = ent + host("move i=0 hop=1/2 to=a sent_ms=1", "move i=0 hop=2/2 to=b sent_ms=2")   # hop 2 at 10:00:05.006
    check("(C5.0 review) movesSent: a window that ended inside `observe_s` is named, not green (capture at 10:00:30)",
          [movesSent(capture(sent2, gp, fence, moves=[dict(moves_meta[0], observe_s=s)])) for s in (20, 40)],
          ["ok", "move0=observed-25s/40s"])
    two = mv_step([jump, dict(jump, after=[{"api": "changePace#1"}], to={"offset_m": 500, "bearing": 90})])
    check("(C5.0 review) moves: a later move that does not wait for the one before it refuses",
          "waits for the one before it" in refused(lambda: _validate_moves("selftest", two)), True)
    shared = [dict(jump)]
    a_, b_ = mv_step(), mv_step()
    a_["moves"] = b_["moves"] = shared                   # a YAML alias: one list at two steps
    check("(C5.0 review) moves: a list two steps share (a YAML alias) loads in both, the sugar resolved in each",
          (refused(lambda: _validate_moves("selftest a", a_)), refused(lambda: _validate_moves("selftest b", b_)),
           shared[0]["after"][0]), ("loaded", "loaded", {"event": "onGeofence ENTER"}))
    import summarize
    tr = lambda ev_t: {"api": [("stop#0", 0)], "events": {"onGeofence@1": {"t": ev_t, "after": "?"}}, "dialog": None,
                       "actions": [], "host": [(1000, "move0.1"), (4000, "move0.2"), (7000, "move0.3")]}
    check("(C5.0 review) trace-diff: a first delivery that moved from after one host send to before it is a change",
          summarize._trace_changes(tr(8000), tr(5000)), ["event onGeofence@1 first delivered after ? -> after ? "
                                                         "(moved across move0.3)"])
    import lab_devices
    tmp_ = tempfile.mkdtemp(prefix="labkit-c50-")
    real_path, real_adb = backends.network_default_path, lab_devices.labkit.adb
    try:
        backends.network_default_path = lambda avd: os.path.join(tmp_, "neutral-%s.json" % avd)
        reads_ = iter(["1\n1\n0\n", "0\n0\n0\n"])
        lab_devices.labkit.adb = lambda *a, **k: next(reads_)
        first, again = lab_devices.record_network("Lab1", "emulator-5580"), lab_devices.record_network("Lab1", "emulator-5580")
        check("(C5.0 review) lab_devices: the network defaults are recorded once — a later `up` of an AVD a failed step "
              "left offline records nothing", (first.startswith("; network defaults data=1 powerSave=0 wifi=1"), again,
                                              json.load(open(os.path.join(tmp_, "neutral-Lab1.json")))),
              (True, "", {"wifi": "1", "data": "1", "powerSave": "0"}))
    finally:
        backends.network_default_path, lab_devices.labkit.adb = real_path, real_adb
        shutil.rmtree(tmp_)
    # (WO-191) `--name`: one device of the pair.  Parsed and filtered offline; what each job then does is unchanged.
    both_ = [("android", "Lab1"), ("android", "Lab2"), ("ios", "Lab1"), ("ios", "Lab2")]
    check("(WO-191) lab_devices --name: a comma list defaulting to both, on up, status and down; it filters the jobs, "
          "android first and Lab1 first whatever the order given; an unknown name, or none, refuses as an unknown OS "
          "does",
          ([lab_devices.parser().parse_args([c_]).name for c_ in ("up", "status", "down")],
           lab_devices.parser().parse_args(["up", "--name", "Lab2", "--os", "ios"]).name,
           lab_devices.jobs_for("android,ios", "Lab1,Lab2"), lab_devices.jobs_for("ios", "Lab2"),
           lab_devices.jobs_for("ios,android", "Lab2,,Lab1"), lab_devices.jobs_for("android", "Lab1,Lab1"),
           [refused(lambda a=a: lab_devices.jobs_for(*a)) for a in (("android", "Lab3"), ("android", ","),
                                                                   ("android", "lab1"), ("tvos", "Lab1"))],
           # main() itself needs devices: read, it builds its jobs from both switches and never walks NAMES itself
           ("jobs_for(args.os, args.name)" in inspect.getsource(lab_devices.main),
            "NAMES" in inspect.getsource(lab_devices.main))),
          (["Lab1,Lab2"] * 3, "Lab2", both_, [("ios", "Lab2")], both_, [("android", "Lab1")],
           ["--name takes Lab1, Lab2 or both"] * 3 + ["--os takes android, ios or both"], (True, False)))

    # ---- item 7: the named lists — every scenario on SUITE or exactly one list, and `--scenarios` expands them
    import sweep
    listed = list(sweep.SUITE) + [s for names in sweep.LISTS.values() for s in names]
    on_disk = sorted(f[:-5] for f in os.listdir(os.path.join(HARNESS, "scenarios")) if f.endswith(".yaml"))
    check("(C5.0) sweep: every scenario is on SUITE or one named list, once, and every name is a scenario",
          (sorted(set(on_disk) - set(listed)), sorted({s for s in listed if listed.count(s) > 1}),
           sorted(set(listed) - set(on_disk))), ([], [], []))
    check("(C5.0) sweep: `--scenarios` takes list names beside scenarios, in order, each once",
          sweep.scenario_list("LEGACY,reset-no-args,events"), ["scheduler-survives-launch", "significant-changes",
                                                              "reset-no-args", "events"])

    # ================================================================ (Tier C, C5.1)
    # ---- item 9: `in:` — one of a closed type's values, each compared as `equals` compares it
    num = lambda v: {"type": "number", "value": v, "parent": True, "trunc": False}
    txt = lambda v: {"type": "string", "value": v, "parent": True, "trunc": False}
    check("(C5.1) in: a member passes (45.0 as 45), a non-member fails, as type-strict as equals",
          [_check(num("-1.0"), {"in": [-2, -1, 10]}), _check(num("5"), {"in": [-2, -1, 10]}),
           _check(txt("-1"), {"in": [-2, -1, 10]}), _check(txt("EXIT"), {"in": ["ENTER", "EXIT"]}),
           _check({"type": "string", "value": "x", "parent": True, "trunc": True}, {"in": ["x"]})],
          [None, "want-in--2|-1|10-got-5", "want-in--2|-1|10-got--1", None, "truncated"])
    for title, returns, want in [
            ("`in:` must be a non-empty list", {"$.logger.logLevel": {"in": []}}, "non-empty list"),
            ("... of strings or numbers", {"$.logger.logLevel": {"in": [True]}}, "non-empty list"),
            ("... each one of the leaf's values (LogLevel has no 9)", {"$.logger.logLevel": {"in": [1, 9]}},
             "`in:` lists 9, none of its values"),
            ("... and of the leaf's wire type (a string on a number)", {"$.logger.logLevel": {"in": ["1"]}}, "launder"),
            ("a narrower `in:` than the type loads", {"$.logger.logLevel": {"in": [4, 5]}}, "loaded")]:
        got = refused(lambda: _validate_hooks("selftest", {"hooks": [{"api": "getState", "returns": returns}]}))
        check("(C5.1) in: %s" % title, want in got or got, True)
    ev_in = refused(lambda: _validate_events("selftest", events_step(
        {"subscribe": "onLocationFilter", "returns": {"$.reason": {"in": ["implied-speed", "outlier-capped"]}}})))
    check("(C5.1) in: on an event path, held to its type's values (the design's geofence-exit-far-android form)",
          ev_in, "loaded")
    gs_decl = {"api": "getState", "declared": "both"}
    _validate_hooks("selftest", {"hooks": [gs_decl]})
    check("(C5.1) declared: a closed State leaf gains `in:` its type's values; triggerActivities (an array too) and an "
          "open number do not",
          (gs_decl["returns"]["$.geolocation.desiredAccuracy"], gs_decl["returns"]["$.persistence.timestampFormat"],
           gs_decl["returns"]["$.activity.triggerActivities"], gs_decl["returns"]["$.geolocation.distanceFilter"]),
          ({"type": "number", "in": [-2, -1, 10, 100, 1000, 3000]}, {"type": "string", "in": ["epoch", "iso"]},
           {"type": "array|string"}, {"type": "number"}))
    got = eventAssertions(capture(ev("event-arm id=onGeofence@0 outcome=ok", "events-armed n=1", "event id=onGeofence@0 n=1",
                                     "ret id=onGeofence@0 path=$.action type=string value=ARRIVE parent=1"), [],
                                  [{"subscribe": "onGeofence", "returns": {"$.action": {"type": "string",
                                                                                        "in": ["DWELL", "ENTER", "EXIT"]}}}]))
    check("(C5.1) scored: a delivered value outside its enum is red", got, "onGeofence@0.$.action=want-in-DWELL|ENTER|EXIT-got-ARRIVE")

    # ---- item 8: `declared: members` on a property probe, and a probe path the types do not declare refused
    props = {"api": "DesiredAccuracy", "probe": True, "declared": "members"}
    _validate_hooks("selftest", {"hooks": [props]})
    check("(C5.1) declared: members — the namespace object|function, then each member the types declare, its value",
          (props["returns"]["$"], props["returns"]["$.High"], len(props["returns"])),
          ({"type": "function|object"}, {"type": "number", "equals": -1}, 7))
    lg = {"api": "logger", "probe": True, "declared": "members"}
    _validate_hooks("selftest", {"hooks": [lg]})
    check("(C5.1) declared: members on `logger` — each method a function, ORDER_ASC 1 and ORDER_DESC -1",
          (lg["returns"]["$.getLog"], lg["returns"]["$.ORDER_ASC"], lg["returns"]["$.ORDER_DESC"], len(lg["returns"])),
          ({"type": "function"}, {"type": "number", "equals": 1}, {"type": "number", "equals": -1}, 12))
    for title, hook, want in [
            ("on a method's probe refuses — it declares no members", {"api": "getState", "probe": True,
                                                                      "declared": "members"}, "of a property"),
            ("without `probe` refuses", {"api": "DesiredAccuracy", "declared": "members"}, "of a property"),
            ("`declared: both` on a probe still refuses", {"api": "DesiredAccuracy", "probe": True, "declared": "both"},
             "on a probe"),
            ("a probe path the namespace does not declare refuses (a legacy static)",
             {"api": "DesiredAccuracy", "probe": True, "returns": {"$.DESIRED_ACCURACY_HIGH": {"type": "number"}}},
             "only for a member the types declare"),
            ("a probe path two levels down refuses", {"api": "logger", "probe": True, "returns": {"$.getLog.length": {
                "type": "number"}}}, "only for a member the types declare"),
            ("a method probe's path refuses — it declares none", {"api": "getState", "probe": True, "returns": {
                "$.enabled": {"type": "boolean"}}}, "declares none"),
            ("a member asserted another value refuses", {"api": "DesiredAccuracy", "probe": True, "returns": {
                "$.High": {"equals": -2}}}, "launder"),
            ("a method member asserted a number refuses", {"api": "logger", "probe": True, "returns": {
                "$.getLog": {"type": "number"}}}, "launder"),
            ("a declared member, written by hand, loads", {"api": "Event", "probe": True, "returns": {
                "$.NotificationAction": {"type": "string", "equals": "notificationaction"}}}, "loaded")]:
        got = refused(lambda: _validate_hooks("selftest", {"hooks": [json.loads(json.dumps(hook))]}))
        check("(C5.1) probes: %s" % title, want in got or got, True)
    apip = scenario("api-properties-typescript")
    check("(C5.1) api-properties-typescript: 17 property probes, every one `declared: members`, 105 paths; Flutter "
          "excused, dated",
          (len(apip["steps"][0]["hooks"]), all(h.get("declared") == "members" for h in apip["steps"][0]["hooks"]),
           sum(len(h["returns"]) for h in apip["steps"][0]["hooks"]), DATED.match(apip["not_applicable"]["flutter"]) is not None),
          (17, True, 105, True))
    check("(C5.1) the device is told a property probe's member paths, never their values",
          wire_hook(apip["steps"][0]["hooks"][4])["paths"][:3], ["$", "$.Navigation", "$.High"])
    check("(C5.1) coverage: an `in:` of every value a closed type has values nothing (it is the type's); a narrower "
          "one does", (summarize._whole_in({"in": [0, 1, 2, 3, 4, 5]}, "$.logger.logLevel", None),
                        summarize._whole_in({"in": [4, 5]}, "$.logger.logLevel", None),
                        summarize._whole_in({"in": ["DWELL", "ENTER", "EXIT"]}, "$.action", "GeofenceEvent")),
          ({"in"}, set(), {"in"}))

    for title, returns, want in [
            ("`optional` on a member refuses — it would pass the missing member api-properties exists to catch",
             {"$.High": {"type": "number", "optional": True}}, "(not `optional`)"),
            ("`absent` on a member refuses", {"$.High": {"absent": True}}, "(not `absent`)"),
            ("`in:` another value on a member refuses", {"$.High": {"in": [5]}}, "(not `in`)"),
            ("`gte` on a member refuses", {"$.High": {"gte": 100}}, "(not `gte`)"),
            ("`equals` on a method member refuses", {"$.Medium": {"type": "number", "equals": 10}}, "loaded"),
            ("`record` on a member loads", {"$.High": {"record": True}}, "loaded")]:
        got = refused(lambda: _validate_hooks("selftest", {"hooks": [{"api": "DesiredAccuracy", "probe": True,
                                                                     "returns": returns}]}))
        check("(C5.1 review) probes: %s" % title, want in got or got, True)
    check("(C5.1 review) probes: `equals` on a logger method refuses",
          "launder" in refused(lambda: _validate_hooks("selftest", {"hooks": [{"api": "logger", "probe": True, "returns": {
              "$.getLog": {"equals": "x"}}}]})), True)
    check("(C5.1 review) `declared: members` with a `returns:` that is not a mapping refuses, never a traceback",
          "non-empty `returns`" in refused(lambda: _validate_hooks("selftest", {"hooks": [{
              "api": "DesiredAccuracy", "probe": True, "declared": "members", "returns": ["$"]}]})), True)

    # ---- item 13: the offline `in:` scan over captures, before any board
    ios_step = scenario("api-state-ios")["steps"][0]
    root_ = tempfile.mkdtemp(prefix="labkit-c51-scan-")
    try:
        for cell, value in (("cordova-ios", "1"), ("capacitor-ios", "9")):
            d = os.path.join(root_, cell, "api-state-ios", "FIX", "run")
            os.makedirs(d)
            body = ["09-21 10:00:00.000 I/LAB     ( 1): point=launch label=x",
                    "09-21 10:00:05.000 I/LAB     ( 1): app=api id=getState#2 outcome=ok",
                    "09-21 10:00:05.001 I/LAB     ( 1): app=ret id=getState#2 path=$.geolocation.activityType type=number "
                    "value=%s parent=1" % value,
                    "09-21 10:00:20.000 I/LAB     ( 1): point=trigger none", "09-21 10:00:30.000 I/LAB     ( 1): point=capture"]
            open(os.path.join(d, "logcat.txt"), "w").write("\n".join(body) + "\n")
            json.dump({"scenario": "api-state-ios", "step": "run", "hooks": ios_step["hooks"], "events": [], "year": 2026},
                      open(os.path.join(d, "run.json"), "w"))
        out_ = io.StringIO()
        with contextlib.redirect_stdout(out_):
            rc = summarize.enum_scan(root_)
        check("(C5.1) in-scan: each reading held to the value set the current YAML generates; one outside it is red",
              (rc, [l.strip() for l in out_.getvalue().splitlines()]),
              (1, ["RED  capacitor-ios/api-state-ios/FIX/run  getState#2.$.geolocation.activityType=want-in-1|2|3|4|5-got-9",
                   "== in-scan: 2 readings held to their value sets, 1 red"]))
    finally:
        shutil.rmtree(root_)
    unread = tempfile.mkdtemp(prefix="labkit-c51-unread-")
    try:
        d = os.path.join(unread, "cordova-ios", "api-state-ios", "FIX", "run")
        os.makedirs(d)
        json.dump({"scenario": "api-state-ios", "step": "run", "hooks": ios_step["hooks"], "events": [], "year": 2026},
                  open(os.path.join(d, "run.json"), "w"))
        out_ = io.StringIO()
        with contextlib.redirect_stdout(out_):
            rc = summarize.enum_scan(unread)
        check("(C5.1 review) in-scan: a capture without its logcat (gitignored: a worktree has run.json alone) is named "
              "and fails the scan, never a traceback or a clean read", (rc, "UNREADABLE" in out_.getvalue()), (1, True))
    finally:
        shutil.rmtree(unread)
    er = scenario("events-remove-all")["steps"][0]
    check("(C5.1 review) events-remove-all: a move after the controls arm (slot 9, about 11 s), past getCurrentPosition#4 "
          "(10 s), feeds the iOS session and the watch",
          (er["moves"][0]["after"], er["moves"][0]["delay_s"], er["hooks"][4]["api"], er["hooks"][4]["at_s"],
           er["moves"][0]["to"]["offset_m"] < 45),
          ([{"armed": "onEnabledChange@5"}], 2, "getCurrentPosition", 10, True))
    check("(C5.1 pilot) a platform both listed and excused refuses — its excusal would never apply",
          "AND excuses it" in c50_load(platforms=platforms(), not_applicable={     # (WO-143) every platform, not four names
              "flutter": "2026-09-28 x"}, steps=[{"name": "run", "hooks": [{"api": "getCount", "returns": {
                  "$": {"type": "number"}}}]}]), True)
    check("(C5.1 pilot) api-properties-typescript is n/a on both Flutter cells and runs on the six JavaScript ones",
          [applicability(apip, platform(n_, o_)) is None for n_ in ("capacitor", "cordova", "flutter", "react-native")
           for o_ in ("android", "ios")], [True, True, True, True, False, False, True, True])
    eg = scenario("events-geofence")["steps"][0]["events"]
    check("(C5.1) events-geofence folds onConnectivityChange as @3 (decision 5 A): declared, and true from start() "
          "(five declarations since C5.3 appended onActivityChange, arm-only)",
          (len(eg), eg[3]["subscribe"], eg[3]["returns"]["$.connected"]), (5, "onConnectivityChange",
                                                                          {"type": "boolean", "equals": True}))
    ca = scenario("connectivity-android")
    check("(C5.1) connectivity-android: Android only, dated; data off before wifi, then wifi on before data, each after "
          "its declaration arms",
          (DATED.match(ca["not_applicable"]["ios"]) is not None,
           [(a["after"], a["network"]) for a in ca["steps"][0]["actions"]]),
          (True, [([{"armed": "onConnectivityChange@1"}], {"data": False, "wifi": False}),
                  ([{"armed": "onConnectivityChange@2"}], {"wifi": True, "data": True})]))
    check("(C5.1 Pilot B) connectivity-android's step boots with disableLocationAuthorizationAlert: the network loss "
          "raised Play Services' dialog, and React Native's timers stop under it",
          ca["steps"][0]["config"], {"geolocation": {"disableLocationAuthorizationAlert": True}})

    # ---- (Tier C, C5.2) the four movement and filter scenarios: what each one's prediction rests on
    cells = lambda spec: [applicability(spec, platform(n_, o_)) is None
                          for n_ in ("capacitor", "cordova", "flutter", "react-native") for o_ in ("android", "ios")]
    gx = scenario("events-geofence-exit")
    fence = gx["steps"][0]["hooks"][3]["args"]
    (j500,) = gx["steps"]
    jm = j500["moves"][0]
    arm = j500["events"][1]["at_s"]
    # MEASURED (the C5.2 pilots): Android's ENTER up to about 7.5 s after launch (the first live tracking fix 3.5-5.4 s
    # after changePace); iOS's EXIT within 170 ms of a move to 400 m or 500 m, never at 250 m or 300 m.
    live_max = [h["at_s"] for h in j500["hooks"] if h["api"] == "changePace"][0] + 5.4
    check("(C5.2) events-geofence-exit: one step, `jump-500`, scoring crashes; the move waits for the delivered ENTER, "
          "@1's arm and changePace's answer, and ends outside the fence's margin (offset - radius - 50) by less than the "
          "Android gate's 500 m, so an INSIDE EXIT is trusted, not deferred",
          ([(s["name"], s["moves"][0]["after"], s["expect"].get("crashes")) for s in gx["steps"]], fence["radius"],
           0 < jm["to"]["offset_m"] - fence["radius"] - 50 < 500, j500["events"][1]["returns"]["$.action"]),
          ([("jump-500", [{"event": "onGeofence@0", "action": "ENTER"}, {"armed": "onGeofence@1"},
                          {"api": "changePace#4"}], 0)], 100, True, {"type": "string", "equals": "EXIT"}))
    # (2026-10-02) The 7.4 s bound failed on a slowed emulator (board 20261002-100020: the first live fix at 14.3 s,
    # the jump 9.1 s after it), so the jump also waits for changePace's answer, which comes with that fix.
    check("(C5.2 pilot) events-geofence-exit's jump-500 (Chris: quicker, one step): @1 arms after the latest measured "
          "ENTER; the jump goes when the last tracking fix before it, the first live one, is over burstWindow 10 s old "
          "however late that fix comes (it waits for changePace#4's answer, which carries the fix, then over 10 s), so "
          "the Android EXIT survives even if its trigger beats the tracking fix; 500 m, where the Simulator reports "
          "within 170 ms; the trigger mark (settle) before the move, so the step ends on the EXIT",
          (arm > live_max, {"api": "changePace#4"} in jm["after"] and jm["delay_s"] > 10, arm + jm["delay_s"],
           jm["to"]["offset_m"], j500["settle"] < arm + jm["delay_s"]),
          (True, True, 22, 500, True))
    gd = scenario("events-geofence-dwell")
    add = gd["steps"][0]["hooks"][3]["args"]
    check("(C5.2) events-geofence-dwell: notifyOnDwell alone, so the DWELL is the only delivery and one declaration at "
          "boot asserts it, and its location's geofence action, which the pilot read DWELL on both OSes",
          ([add[k] for k in ("notifyOnEntry", "notifyOnExit", "notifyOnDwell", "loiteringDelay")],
           [(e.get("at_s"), e["returns"]["$.action"]["equals"], e["returns"].get("$.location.geofence.action"))
            for e in gd["steps"][0]["events"]], cells(gd)),
          ([False, False, True, 10000], [(None, "DWELL", {"type": "string", "equals": "DWELL"})], [True] * 8))
    lf = scenario("events-location-filter")
    jump, acc = lf["steps"]
    jmove = jump["moves"][0]
    thr = acc["config"]["geolocation"]["filter"]["trackingAccuracyThreshold"]
    # MEASURED on board 20260928-110423 (the C5.2 review): the first live tracking fix 3.5-5.4 s after changePace; the
    # host sends a move delay_s + 0.15-0.25 s after its condition; the emulator's best fix 3.107 m.
    first_fix, send, best_acc = (3.5, 5.4), (0.15, 0.25), 3.107
    check("(C5.2) events-location-filter: all eight cells (the JavaScript ones until R gave Flutter its row; "
          "C5.3), each step scoring crashes; the jump is over "
          "maxBurstDistance 300, sent after the latest measured first live fix and within burstWindow 10 s of the "
          "earliest one plus a fix interval, so implied-speed whether the cache was warm or not; its longitude bound lies "
          "between the standing fix and the jump",
          (cells(lf), [s["expect"].get("crashes") for s in lf["steps"]], jmove["to"]["offset_m"] > 300,
           jmove["delay_s"] + send[0] > first_fix[1], jmove["delay_s"] + send[1] + 1 - first_fix[0] <= 10,
           STANDING_FIX[1] < jump["events"][0]["returns"]["$.location.coords.longitude"]["gte"]
           <= destination(jmove["to"]["offset_m"], 90)[1],
           jump["events"][0]["returns"]["$.reason"]["equals"], [s["events"][0]["returns"]["$.geofence"] for s in lf["steps"]]),
          ([True] * 8, [0, 0], True, True, True, True, "implied-speed", [{"absent": True}] * 2))
    check("(C5.2) events-location-filter's accuracy step: a fresh install whose threshold is at least 1 m under every fix "
          "the lab has measured, and the accuracy asserted over it, not equal to one OS's 5 m",
          (acc["fresh"], thr <= best_acc - 1, acc["events"][0]["returns"]["$.reason"]["equals"],
           {k: acc["events"][0]["returns"][k] for k in ("$.accuracy", "$.trackingAccuracyThreshold",
                                                         "$.location.coords.accuracy")}),
          (True, True, "low-accuracy", {"$.accuracy": {"type": "number", "gt": thr},
                                        "$.trackingAccuracyThreshold": {"type": "number", "equals": thr},
                                        "$.location.coords.accuracy": {"type": "number", "gt": thr}}))
    gf = scenario("geofence-exit-far-android")
    far = gf["steps"][0]
    check("(C5.2) geofence-exit-far-android: the four Android cells (three until R gave Flutter its onLocationFilter "
          "row; C5.3); the jump ends outside the fence's margin "
          "(offset - radius - 50) by at least the gate's 500 m, so the INSIDE EXIT is deferred, then dropped: no EXIT "
          "through observe_s, and the drop counted from the launch mark, since the jump precedes the trigger mark",
          (cells(gf), far["moves"][0]["to"]["offset_m"] - far["hooks"][3]["args"]["radius"] - 50 >= 500,
           {k: far["events"][1].get(k) for k in ("at_s", "min", "max")}, far["moves"][0]["observe_s"],
           far["expect"].get("deferredExitDropped"), far["measure_from"], far["events"][2]["returns"]["$.reason"]["in"]),
          ([True, False] * 4, True, {"at_s": 10, "min": 0, "max": 0}, 10, 1, "launch",
           ["implied-speed", "outlier-capped"]))
    # MEASURED (sweep 20260928-145837's three cells and the pilot): the drop 4.36-4.6 s after the jump.
    fm, drop_max = far["moves"][0], 4.6
    far_arm = far["events"][1]["at_s"]
    far_live = [h["at_s"] for h in far["hooks"] if h["api"] == "changePace"][0] + 5.4
    check("(C5.2 board) geofence-exit-far-android, quicker (Chris): the jump after the latest first live fix; observe_s "
          "at least twice the latest measured drop; the window holds the jump and observe_s, and is at most 40 s",
          (far_arm + fm["delay_s"] > far_live, fm["observe_s"] >= 2 * drop_max, fm["delay_s"], fm["observe_s"],
           far_arm + fm["delay_s"] + fm["observe_s"] <= far["settle"] + far["after"] <= 40),
          (True, True, 2, 10, True))
    one = {"name": "a", "hooks": [{"api": "getCount", "returns": {"$": {"type": "number"}}}]}
    all4 = platforms()                    # (WO-143) every platform: a fifth one is not accounted for by four names
    check("(C5.2 review) a top-level `expect` refuses on a two-step scenario, which it would never reach, and still loads "
          "on a single step",
          ("single-step scenario only" in c50_load(expect={"crashes": 0}, platforms=all4, steps=[dict(one), dict(one, name="b")]),
           c50_load(expect={"crashes": 0}, platforms=all4, steps=[dict(one, name="run")])), (True, "loaded"))
    import sweep as sweep_
    check("(C5.2 board) events-geofence-exit and -dwell in SUITE, the dwell after the exit (board 20260928-145837's "
          "order); (C5.3 board) events-remove and events-location-filter after them, green on all eight on board "
          "20261005-111238; geofence-exit-far-android opt-in until F1; (board 20261010-144634) events-remove-all "
          "last, green on all eight and the native pair once WO-096 and WO-097 landed; thirty-one in all, with "
          "api-properties-dart (Tier C's closure)",
          (sweep_.SUITE[-5:], [n_ in sweep_.LISTS["OPTIN"] for n_ in (
              "events-location-filter", "events-remove", "events-remove-all", "geofence-exit-far-android")],
           len(sweep_.SUITE)),
          (["events-geofence-exit", "events-geofence-dwell", "events-remove", "events-location-filter",
            "events-remove-all"], [False, False, False, True], 31))

    # ---- Tier C, C6.0: Android headless (item 4) ----
    def headless_cap(points, fired=(), boots=("app=boot n=1",), os_name="android"):
        """A capture whose SDK database is still there when the metrics read it (capture() removes its directory)."""
        path = tempfile.mkdtemp(prefix="labkit-c60-")
        try:
            body = ["09-21 10:00:00.000 I/LAB     ( 1): point=launch label=selftest"]
            body += ["09-21 10:00:01.%03d I/LAB     ( 1): %s" % (n, l) for n, l in enumerate(boots)]
            body += ["09-21 10:00:20.000 I/LAB     ( 1): point=trigger finish"]
            body += ["09-21 10:00:2%d.000 I/LAB     ( 1): point=headless event=%s" % (n, e) for n, e in enumerate(points)]
            body += ["09-21 10:00:30.000 I/LAB     ( 1): point=capture"]
            with open(os.path.join(path, "logcat.txt"), "w") as f:
                f.write("\n".join(body) + "\n")
            with open(os.path.join(path, "run.json"), "w") as f:
                json.dump({"os": os_name, "year": 2026}, f)
            con = sqlite3.connect(os.path.join(path, "transistor_log.db"))
            con.execute("create table logging_event (event_id integer primary key, timestmp integer, "
                        "formatted_message text)")
            t = stamp_ms("09-21 10:00:25.000 x", 2026)
            con.executemany("insert into logging_event (timestmp, formatted_message) values (?, ?)",
                            [(t, "🛜 💀⚡️ " + name) for name in fired])
            con.commit()
            con.close()
            cap = Capture(path)
            names = ("location", "locationfilter", "locationerror", "terminate")
            return ({n: (METRICS["headlessEvents." + n](cap), METRICS["headlessFired." + n](cap)) for n in names},
                    headlessTally(cap), headlessUnknown(cap), driverBoot(cap), headlessEvents(cap))
        finally:
            shutil.rmtree(path)
    trap = headless_cap(["locationfilter", "locationerror", "location", "terminate", "terminate", "bogus"],
                        fired=["locationfilter", "locationerror", "location", "terminate"],
                        boots=("app=boot n=1", "app=boot n=2"))
    check("(C6.0) headless per name: `location` counts neither locationfilter nor locationerror, delivered (the task's "
          "line) or fired (the SDK's, no trailing wildcard); the tally, the unknown name, driverBoot the last boot's n",
          trap, ({"location": (1, 1), "locationfilter": (1, 1), "locationerror": (1, 1), "terminate": (2, 1)},
                 "bogus=1,location=1,locationerror=1,locationfilter=1,terminate=2", 1, "2", 6))
    check("(C6.0) nothing headless: 0, 0, tally '-'; no boot line: driverBoot '-' (a main engine that never booted)",
          headless_cap([], boots=()), ({"location": (0, 0), "locationfilter": (0, 0), "locationerror": (0, 0),
                                        "terminate": (0, 0)}, "-", 0, "-", 0))
    stuck = Capture.live(L("point=launch label=selftest trigger=finish process=live pid=1",
                           "app=window offMain=0 threads=-", "point=headless event=terminate"), {})
    check("(C6.0 review) a live relaunch whose driver never boots (a stuck splash): not contaminated, and driverBoot "
          "reads '-': a red row, not an ERROR",
          (devices.contamination(stuck.lines, "selftest"), driverBoot(stuck)), ([], "-"))
    before = tempfile.mkdtemp(prefix="labkit-c60-")
    try:
        with open(os.path.join(before, "logcat.txt"), "w") as f:
            f.write("\n".join(L("point=launch label=selftest", "app=boot n=1 args={}", pid=4242)
                              + L("app=window offMain=0 threads=-", pid=4243)) + "\n")
        check("(C6.0 review) live_refusal: the pid that booted the previous step's driver is live; another pid, none, or "
              "a previous step with no capture is refused",
              [live_refusal(before, "4242"), "did not boot the previous step's driver (4242)" in live_refusal(before, "4243"),
               "has no process" in live_refusal(before, None),
               "no app=boot in" in live_refusal(os.path.join(before, "missing"), "4242")], [None, True, True, True])
    finally:
        shutil.rmtree(before)
    check("(C6.0) the names: the vocabulary's Event plus locationerror (HeadlessEvent.ts:42), each a delivered and a "
          "fired metric",
          (len(headless_names()), "locationerror" in headless_names(),
           all("headlessEvents." + n in METRICS and "headlessFired." + n in METRICS for n in headless_names())),
          (19, True, True))
    live2 = dict(name="relaunch", process="live", permissions="keep", expect={"crashes": 0})
    fin = dict(name="move", trigger="finish", expect={"crashes": 0})
    ios_na = {"ios": "2026-09-28 — selftest"}
    check("(C6.0) process: live loads on a later step after a finish, with permissions: keep and a dated iOS excusal",
          c50_load(platforms=["cordova"], not_applicable=ios_na, steps=[fin, live2]), "loaded")
    for title, spec_, reason in (
            ("at scenario level", dict(process="live", not_applicable=ios_na, steps=[fin, live2]), "a step's own key"),
            ("with a value other than live", dict(not_applicable=ios_na, steps=[fin, dict(live2, process="new")]),
             "is `live`"),
            ("on the first step", dict(not_applicable=ios_na, steps=[dict(live2, trigger="finish"), fin]),
             "on the first step"),
            ("with fresh: true", dict(not_applicable=ios_na, steps=[fin, dict(live2, fresh=True)]),
             "with `fresh: true`"),
            ("without permissions: keep", dict(not_applicable=ios_na, steps=[fin, dict(live2, permissions="grant")]),
             "needs `permissions: keep`"),
            ("after a step whose trigger is not finish",
             dict(not_applicable=ios_na, steps=[dict(fin, trigger="bold"), live2]), "not finish"),
            ("after an untriggered step", dict(not_applicable=ios_na, steps=[dict(fin, trigger="none"), live2]),
             "not finish"),
            ("without a dated iOS excusal", dict(steps=[fin, live2]), "needs a dated `not_applicable: ios`"),
            ("with an undated iOS excusal", dict(not_applicable={"ios": "no date"}, steps=[fin, live2]),
             "needs a dated `not_applicable: ios`")):
        got = c50_load(platforms=["cordova"], **spec_)
        check("(C6.0) process: live refuses %s" % title, reason in got, True)
    # keep's own refusals cover a live step's reinstall and revoke (it must be keep).
    check("(C6.0) a live step with a revoke is refused (keep's rule)",
          "with a `revoke:`" in c50_load(platforms=["cordova"], not_applicable=ios_na,
                                           steps=[fin, dict(live2, revoke=["ACCESS_FINE_LOCATION"])]), True)
    hs = scenario("headless-android")
    pair = {"steps": [dict(fin), dict(fin, name="again")]}
    check("(C6.0) keeps_process: a step whose next says process: live is left running; the last step, and a step "
          "before a fresh process, are not",
          ([keeps_process(hs, st) for st in hs["steps"]], keeps_process(pair, pair["steps"][0])),
          ([True, False], False))
    anchorless = Capture.live(["09-21 10:00:01.000 I/LAB     ( 1): app=boot n=2"], {})
    check("(C6.0) schedule_report: a live step without an anchor is by design; any other step's is a lab defect",
          (schedule_report(anchorless, hs["steps"][1]), bool(schedule_report(anchorless, hs["steps"][0]))), ([], True))
    alive_calls = []
    for rc, out_, want in ((0, "4242\n", "4242"), (0, "4242 4243\n", "4242"), (1, "", None), (1, "4242\n", None)):
        backends.labkit.device_call = lambda argv, rc=rc, out_=out_, **kw: (alive_calls.append(argv[3:]),
                                                                            sp.CompletedProcess(argv, rc, out_, ""))[1]
        try:
            check("(C6.0) Adb.alive: `pidof` %r (exit %d) -> %r" % (out_, rc, want),
                  backends.Adb("emulator-5580").alive({"package": "com.x"}), want)
        finally:
            backends.labkit.device_call = real_call
    check("(C6.0) Adb.alive asks pidof for the package", alive_calls[0], ["shell", "pidof", "com.x"])
    check("(C6.0) headless-android: the four Android cells (iOS excused, dated); move asserts one TERMINATE delivered "
          "and fired, no enabledchange, and at most one location (the stop timer's restart, board 20261002-115024); "
          "relaunch is live, reads driverBoot 2 and one TERMINATE from its launch mark",
          ([s_["name"] for s_ in hs["steps"]], hs["platforms"], DATED.match(hs["not_applicable"]["ios"]) is not None,
           [(s_["trigger"], s_.get("process"), s_["permissions"], s_["measure_from"],
             s_["expect"].get("headlessEvents.terminate"), s_["expect"].get("headlessFired.terminate"),
             s_["expect"].get("headlessEvents.enabledchange"), s_["expect"].get("driverBoot"),
             s_["expect"].get("headlessEvents.location")) for s_ in hs["steps"]],
           hs["steps"][0]["stopOnTerminate"], len(hs["steps"][1]["hooks"])),
          (["move", "relaunch"], ["capacitor", "cordova", "flutter", "react-native"], True,
           [("finish", None, "grant", "trigger", 1, 1, 0, None, "<=1"),
            ("finish", "live", "keep", "launch", 1, 1, 0, 2, "any")],
           False, 0))
    tf = scenario("terminate-finish")["steps"][0]["expect"]
    b2 = scenario("recreate-surviving-engine-no-stop-on-terminate")
    check("(C6.0) terminate-finish folds per name (enabledchange 1, terminate 0); B2 is Flutter's, a Bold recreation "
          "with stopOnTerminate false and a host engine that fires and delivers no TERMINATE",
          ((tf.get("headlessEvents.enabledchange"), tf.get("headlessEvents.terminate"), tf.get("headlessEvents")),
           (b2["platforms"], b2["steps"][0]["trigger"], b2["steps"][0]["stopOnTerminate"], b2["steps"][0]["extras"],
            {k: b2["steps"][0]["expect"].get(k) for k in ("headless", "terminationPasses", "headlessEvents",
                                                           "headlessEvents.terminate", "headlessFired.terminate")},
            "readyEnabled" in b2["steps"][0]["expect"])),
          ((1, 0, ">=1"), (["flutter"], "bold", False, {"hostEngine": True},
                           {"headless": False, "terminationPasses": 0, "headlessEvents": 0,
                            "headlessEvents.terminate": 0, "headlessFired.terminate": 0}, False)))
    check("(C6.0) run_step checks the live process (dev.alive) instead of force-stopping it, and keeps a process the "
          "next step relaunches into (keeps_process) — read from its source, as evict's wiring is",
          ["live_pid = dev.alive(plat)" in inspect.getsource(run.run_step),
           "if labkit.keeps_process(scen, step):" in inspect.getsource(run.run_step)], [True, True])
    check("(C6.0 review) run_step refuses a live pid that did not run the previous step's driver (live_refusal), and "
          "re-reads it just before the launch",
          [s_ in inspect.getsource(run.run_step) for s_ in (
              "        before = scen[\"steps\"][scen[\"steps\"].index(step) - 1]\n"
              "        why = labkit.live_refusal(",
              "        if why:\n            raise SystemExit(",
              "    if live_pid is not None and dev.alive(plat) != live_pid:\n        raise SystemExit(")], [True] * 3)
    arms = [variant(v) for v in ("MUT-terminate-retain", "MUT-flutter-headless-registered")]
    check("(C6.0) the two arms (decision 16 A) invert headless-android/relaunch alone, so `move` is judged normally: "
          "native c62a8fa reverted in its own snapshot, flutter 510bbbf reverted in its own checkout on FIX's native",
          [(a["must_fail"], a["native"], a["plugin"], bool(a.get("mutation"))) for a in arms],
          [(["headless-android/relaunch"], "4.7.1-mut-terminate-retain", "worktree", True),
           (["headless-android/relaunch"], variant("FIX")["native"], "{labs}/checkouts/mut-flutter-headless/flutter",
            True)])
    check("(WO-114 board) headless-android-kill is on GUARDS since board 20261005-115027, green on four on the first "
          "FIX that carries the fix; PRE-WO-114 stays as its control arm, and the stopgap POST-WO-114 is retired",
          ("headless-android-kill" in sweep_.LISTS["GUARDS"], "headless-android-kill" in sweep_.LISTS["OPTIN"],
           [os.path.exists(os.path.join(HARNESS, "variants", v_ + ".yaml")) for v_ in ("PRE-WO-114", "POST-WO-114")]),
          (True, False, [True, False]))
    check("(C6.0) GUARDS carries headless-android and B2",
          [n_ in sweep_.LISTS["GUARDS"] for n_ in ("headless-android", "recreate-surviving-engine-no-stop-on-terminate")],
          [True, True])

    # ---- Tier C, C6.1: the headless causes (H5), the lab server, the switches ----
    causes = scenario("headless-android-causes")["steps"][0]
    base_ = {"settle": 8, "after": 25, "hooks": [], "events": [], "config": {"http": {"url": LAB_HTTP + "/locations"}}}
    act = lambda **kw: dict({"after": [{"point": "onDestroy"}]}, **kw)
    for title, acts_, want in [
            ("location and power_save take true or false", [act(location=False), act(power_save=True)], "loaded"),
            ("`location: off` as a string refuses (YAML's off is false)", [act(location="off")], "true (on) or false"),
            ("an action doing two things refuses", [act(location=False, power_save=True)], "exactly one of"),
            ("an action doing nothing refuses", [act()], "not nothing"),
            ("`server:` names a known act", [act(server="revoke")], "loaded"),
            ("an unknown `server:` act refuses", [act(server="drop")], "one of revoke"),
            ("a headless condition names a headless event", [dict(act(location=False), after=[{"headless": "motionchange"}])],
             "loaded"),
            ("an unknown headless name refuses", [dict(act(location=False), after=[{"headless": "motion"}])],
             "not a headless event name")]:
        got = refused(lambda: _validate_moves("selftest", dict(base_, actions=json.loads(json.dumps(acts_))), ("ios",)))
        check("(C6.1) actions: %s" % title, want in got or got, True)
    check("(C6.1) actions: `server:` on a step that starts no lab server refuses",
          "starts none" in refused(lambda: _validate_moves("selftest", dict(base_, config={}, actions=[act(server="revoke")]),
                                                           ("ios",))), True)
    chain = dict(base_, moves=[{"after": [{"point": "onDestroy"}], "to": {"offset_m": 250, "bearing": 90}},
                               {"after": [{"move": 0}], "delay_s": 1, "to": {"offset_m": 350, "bearing": 90}}],
                 actions=[act(server="revoke")] + [{"after": [{"move": 1}], "delay_s": d, "location": d == 5}
                                                   for d in (2, 2, 2, 5, 5, 5)])
    check("(C6.1) the window floor is the longest chain (move 1's 1 s, then 5 s), not the sum of the seven toggles "
          "(the sum asked 38 s of headless-android-causes); a chain of moves still adds up",
          (refused(lambda: _validate_moves("selftest", dict(chain, settle=2, after=20), ("ios",))),
           "at least 22 s" in refused(lambda: _validate_moves("selftest", dict(chain, settle=2, after=15), ("ios",))),
           "at least 20 s" in refused(lambda: _validate_moves("selftest", dict(
               base_, settle=2, after=10, moves=[{"after": [{"point": "x"}], "delay_s": 3, "to": {"offset_m": 9, "bearing": 0}},
                                                 {"after": [{"move": 0}], "delay_s": 1, "to": {"offset_m": 9, "bearing": 0}}])))),
          ("loaded", True, True))
    hl = lambda *ls: [normalize("09-21 10:00:05.000 I/flutter ( 7): LAB point=" + l) for l in ls]
    check("(C6.1) after_unmet: a headless condition holds on its own name only — not a longer one, not another",
          [after_unmet([{"headless": "location"}], hl(l), {}) is None for l in (
              "headless event=location", "headless event=locationfilter", "headless event=motionchange")],
          [True, False, False])
    sent_ = with_lab_http(dict(causes, hooks=[{"api": "setConfig", "args": {"http": {"url": LAB_HTTP + "/x"}}}]),
                          "http://10.0.2.2:5")
    check("(C6.1) {lab_http}: the step names it (config or hook args), and every string leaf is replaced — the SDK's own "
          "{refreshToken} template untouched; a step naming none starts no server",
          (uses_lab_http(causes), sent_["config"]["http"]["url"], sent_["config"]["authorization"]["refreshUrl"],
           sent_["config"]["authorization"]["refreshPayload"], sent_["hooks"][0]["args"]["http"]["url"],
           causes["config"]["http"]["url"], uses_lab_http(scenario("headless-android")["steps"][0]),
           uses_lab_http({"config": {}, "hooks": [{"api": "setConfig", "args": {"http": {"url": LAB_HTTP}}}]})),
          (True, "http://10.0.2.2:5/locations", "http://10.0.2.2:5/refresh", {"refresh_token": "{refreshToken}"},
           "http://10.0.2.2:5/x", LAB_HTTP + "/locations", False, True))

    # The server end to end, over real HTTP on the loopback, its marks into a fake device's log, then scored.
    import urllib.request, urllib.error
    class MarkDev:
        def __init__(self):
            self.lines = []

        def mark(self, text):
            self.lines.append("I/LAB     ( 2): point=" + re.sub(r"(got|mark)_ms=\d+", r"\1_ms=T", text))
    mdev = MarkDev()
    lab = run.LabHttp(mdev, "lab-access-1")
    url = "http://127.0.0.1:%d" % lab.port

    def post(path, body, token=None, method="POST"):
        req = urllib.request.Request(url + path, data=body.encode() if body is not None else None, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    one = json.dumps({"location": {"uuid": "a"}})
    answers = [post("/locations", one, "lab-access-1")]
    lab.revoke()
    answers += [post("/locations", one, "lab-access-1"), post("/refresh", "refresh_token=lab-refresh"),
                post("/locations", json.dumps({"location": [{"uuid": "a"}, {"uuid": "b"}]}), "lab-access-2"),
                post("/refresh", "refresh_token=someone-else"), post("/", None, method="GET")]
    lab.close()
    try:
        post("/locations", one, "lab-access-2")
        after_close = "answered"
    except (urllib.error.URLError, OSError):
        after_close = "refused"
    check("(C6.1) LabHttp: 200 on the issued token; after revoke, 401; /refresh issues lab-access-2 for the refresh "
          "token and 401 for another; a batch counts its records; an unknown route 404; closed, it answers nothing",
          ([a[0] for a in answers], answers[2][1], after_close),
          ([200, 401, 200, 200, 401, 404], {"access_token": "lab-access-2", "refresh_token": "lab-refresh"}, "refused"))
    class LateReq:                    # an answer already in flight when close() ran: sent, never marked
        command, path, headers, wfile = "POST", "/locations", {"Content-Length": "0"}, io.BytesIO()
        rfile = io.BytesIO()

        def send_response(self, code):
            self.code = code

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass
    late = LateReq()
    marked = len(mdev.lines)
    lab.answer(late)
    # (launch-http-flush) no token configured: any upload 200; a `locations` script: that status for every upload;
    # and the port a device's first server took is the one its next step's server binds (the URL does not change).
    pdev = MarkDev()
    open_ = run.LabHttp(pdev)
    url, first_port = "http://127.0.0.1:%d" % open_.port, open_.port
    answers_open = [post("/locations", one), post("/locations", one, "anything")]
    open_.close()
    refusing = run.LabHttp(pdev, "lab-access-1", 503)
    url = "http://127.0.0.1:%d" % refusing.port
    answers_503 = [post("/locations", one, "lab-access-1"), post("/refresh", "refresh_token=lab-refresh")]
    refusing.close()
    check("(C6.1, launch-http-flush) LabHttp: with no accessToken configured every upload is 200; `lab_http: "
          "{locations: 503}` answers every upload 503, whatever its token (refresh unscripted); the device's next "
          "server binds the port its first took",
          ([a[0] for a in answers_open], [a[0] for a in answers_503], refusing.port == first_port == pdev.lab_http_port),
          ([200, 200], [503, 200], True))
    for title, script, want in [("a status in range loads", {"locations": 503}, "loaded"),
                                ("an unknown route refuses", {"refresh": 503}, "is {locations: <status>}"),
                                ("a status as a string refuses", {"locations": "503"}, "an HTTP status"),
                                ("a status past 599 refuses", {"locations": 600}, "an HTTP status")]:
        check("(C6.1) lab_http: %s" % title, want in refused(lambda: _validate_lab_http(
            "selftest", {"config": {"http": {"url": LAB_HTTP}}, "hooks": [], "lab_http": script})), True)
    check("(C6.1) lab_http: scenario() holds a step's script at load",
          "an HTTP status" in c50_load(config={"http": {"url": LAB_HTTP + "/x"}},
                                       steps=[{"name": "run", "lab_http": {"locations": "503"},
                                               "expect": {"crashes": 0}}]), True)
    check("(C6.1) lab_http: on a step that starts no lab server refuses",
          "starts none" in refused(lambda: _validate_lab_http("selftest", {"config": {}, "hooks": [],
                                                                            "lab_http": {"locations": 503}})), True)
    lhf = scenario("launch-http-flush")["steps"]
    check("(C6.1) launch-http-flush against the lab server: queue refuses every upload 503 and accepts none; launch "
          "accepts at least one and refuses none; both on {lab_http}",
          ([(s_.get("lab_http"), s_["expect"].get("labHttpRecords"), s_["expect"].get("labHttp.locations.503"),
             uses_lab_http(s_)) for s_ in lhf]),
          [({"locations": 503}, 0, ">=1", True), (None, ">=1", 0, True)])
    check("(C6.1) LabHttp: closed, an answer still in flight is sent but not marked (none after the capture mark)",
          (late.code, len(mdev.lines) - marked), (401, 0))
    check("(C6.1) LabHttp: each answer marked into the device's log, in order, with its token and records",
          mdev.lines[:4],
          ["I/LAB     ( 2): point=server i=0 method=POST path=/locations status=200 auth=lab-access-1 records=1 got_ms=T mark_ms=T",
           "I/LAB     ( 2): point=server i=1 method=POST path=/locations status=401 auth=lab-access-1 records=1 got_ms=T mark_ms=T",
           "I/LAB     ( 2): point=server i=2 method=POST path=/refresh status=200 auth=- records=0 got_ms=T mark_ms=T",
           "I/LAB     ( 2): point=server i=3 method=POST path=/locations status=200 auth=lab-access-2 records=2 got_ms=T mark_ms=T"])
    cap_ = capture(mdev.lines, [], None, measure_from="launch")
    check("(C6.1) labHttp metrics over those marks: the tally, the per-route counts, the records accepted, and the "
          "uploads that carried a refreshed token",
          (labHttp(cap_), METRICS["labHttp.locations.401"](cap_), METRICS["labHttp.refresh.200"](cap_),
           METRICS["labHttp.refresh.401"](cap_), labHttpRecords(cap_), labHttpRefreshedUploads(cap_)),
          ("/:404=1,locations:200=2,locations:401=1,refresh:200=1,refresh:401=1", 1, 1, 1, 3, 1))
    check("(C6.1) labHttp metrics are windowed: marks before the trigger mark are not the step's",
          labHttp(capture(mdev.lines, [], None)), "-")

    # run_host: the switches and the server act, each marked; a switch that fails stops the runner.
    class SwitchDev(FakeDev):
        def location(self, on):
            self.calls.append(("location", on))

        def power_save(self, on):
            self.calls.append(("power_save", on))
    class FakeServer:
        def __init__(self):
            self.revoked = 0

        def revoke(self):
            self.revoked += 1
    sw = dict(base_, actions=[act(server="revoke"), act(location=False), act(power_save=True)])
    _validate_moves("selftest", sw, ("ios",))
    dev_, srv_ = SwitchDev(["09-21 10:00:01.000 I/LAB     ( 7): app=boot n=1",
                            "09-21 10:00:02.000 I/LAB     ( 7): point=onDestroy isChangingConfigurations=false"]), FakeServer()
    host_ = {"error": None, "held_s": {}, "sent_s": {}}
    run.run_host(dev_, sw, time.time(), time.time() + 3, threading.Event(), host_, srv_)
    check("(C6.1) run_host: server revoke, location off, battery saver on — each done once, then marked",
          (srv_.revoked, dev_.calls, host_["error"]),
          (1, [("mark", "host i=0 action=server revoke sent_ms=T done_ms=T mark_ms=T"), ("location", False),
               ("mark", "host i=1 action=location off sent_ms=T done_ms=T mark_ms=T"), ("power_save", True),
               ("mark", "host i=2 action=power_save on sent_ms=T done_ms=T mark_ms=T")], None))

    # (WO-191) ACTION_HANDLERS: the built-ins are the six kinds the refusal always named, each Android's.  A product adds
    # a kind by registering a handler (fetch: `job`, `force_stop`), and _validate_moves, run_host and actionsDone take
    # it with no change of their own.  Removed in `finally`, so no later check sees it.
    check("(WO-191) ACTION_HANDLERS: the six built-in kinds, in the order an action's refusal lists them, each Android's",
          [(k, h.oses) for k, h in ACTION_HANDLERS.items()],
          [(k, ("android",)) for k in ("network", "location", "power_save", "server", "notification_tap", "kill")])

    def job_ok(at, item, step):
        if not (isinstance(item["selftest_job"], int) and not isinstance(item["selftest_job"], bool)):
            raise SystemExit("%s: `selftest_job:` is the seconds it runs — not %r" % (at, item["selftest_job"]))
        return item["selftest_job"]

    def job_run(dev, item, plat, server):
        dev.calls.append(("selftest_job", item["selftest_job"], (plat or {}).get("package")))
        return "selftest_job s=%d" % item["selftest_job"]
    job_ = lambda s: dict(base_, actions=[act(selftest_job=s)])
    load_ = lambda step_, excused=("ios",): refused(lambda: _validate_moves("selftest", json.loads(json.dumps(step_)),
                                                                            excused))
    woke_ = ["09-21 10:00:01.000 I/LAB     ( 7): app=boot n=1",
             "09-21 10:00:02.000 I/LAB     ( 7): point=onDestroy isChangingConfigurations=false"]
    # Run as __main__ (the selftest), this file is not the `labkit` run.py imports: the kind goes into both tables, as a
    # product registers into the imported one (Part 3's hook).
    tables_ = [ACTION_HANDLERS] + [t_ for t_ in [run.labkit.ACTION_HANDLERS] if t_ is not ACTION_HANDLERS]

    def register_(handler):
        for t_ in tables_:
            t_["selftest_job"] = handler
    unknown_before = load_(job_(0))
    try:
        register_(ActionHandler(job_ok, job_run))
        # a window of 33 s (settle 8 + after 25) holds a 17 s action and no more: ANCHOR_BUDGET_S 12 + TICK_SECONDS 4
        product_loads = [load_(job_(17)), "at least 34 s" in load_(job_(18)), load_(job_("x")).split(" — ")[0],
                         "are Android's" in load_(job_(0), ())]
        register_(ActionHandler(job_ok, job_run, oses=("android", "ios")))
        both_oses = load_(job_(0), ())
        register_(ActionHandler(lambda at, item, step: "soon", job_run))
        bad_span = load_(job_(0)).split(" — ")[0]
        # a kind's own extra keys (fetch's `job:` takes `expect:`): unknown until a handler declares one, then taken
        # by that kind only
        keyed_ = dict(base_, actions=[act(selftest_job=0, expect="absent")])
        key_loads = [load_(keyed_).split(" — ")[0]]
        register_(ActionHandler(job_ok, job_run, keys=("expect",)))
        key_loads += [load_(keyed_), load_(dict(base_, actions=[act(location=False, expect="absent")]))]
        register_(ActionHandler(job_ok, job_run))
        ran_step = job_(0)
        ran_loads = refused(lambda: _validate_moves("selftest", ran_step, ("ios",)))   # a FAIL below, not a crash here
        dev_, host_ = SwitchDev(woke_), {"error": None, "held_s": {}, "sent_s": {}}
        run.run_host(dev_, ran_step, time.time(), time.time() + 3, threading.Event(), host_, FakeServer(),
                     {"package": "com.lab.app"})
        scored_ = actionsDone(capture([l.split(" ", 2)[2] for l in dev_.lines], [], None, actions=ran_step["actions"]))
    finally:
        for t_ in tables_:
            t_.pop("selftest_job", None)
    stray_host = {"error": None, "held_s": {}, "sent_s": {}}
    run.run_host(SwitchDev(woke_), ran_step, time.time(), time.time() + 3, threading.Event(), stray_host, FakeServer())
    check("(WO-191) a product's action kind: unknown until registered; registered, it is validated by its own handler "
          "(its span counted in the window, its refusal, Android's unless its oses say otherwise, its own extra keys and "
          "no other kind's), run by run_host with its mark, and scored by actionsDone; a validate that returns no "
          "seconds refuses; unregistered, run_host refuses it rather than sending it as another kind",
          (unknown_before.split(" — ")[0], product_loads, both_oses, bad_span, key_loads, ran_loads, dev_.calls,
           host_["error"], scored_, load_(job_(0)).split(" — ")[0], stray_host["error"]),
          ("selftest actions 0: unknown key(s) selftest_job",
           ["loaded", True, "selftest actions 0: `selftest_job:` is the seconds it runs", True], "loaded",
           "selftest actions 0: the `selftest_job` handler's validate returned 'soon'",
           ["selftest actions 0: unknown key(s) expect", "loaded",
            "selftest actions 0: `expect:` belongs to another kind of action, not a `location:`"], "loaded",
           [("selftest_job", 0, "com.lab.app"), ("mark", "host i=0 action=selftest_job s=0 sent_ms=T done_ms=T mark_ms=T")],
           None, "ok", "selftest actions 0: unknown key(s) selftest_job",
           "action 0 does nothing — not exactly one kind labkit.ACTION_HANDLERS knows"))

    # Adb's switches: set, then read back until it reads what was set; one that never does refuses the step.
    reads_ = []
    def switch_call(results):
        def call(argv, **kw):
            reads_.append(" ".join(argv[4:]))
            return sp.CompletedProcess(argv, 0, results.pop(0) if "get" in argv or "is-location-enabled" in argv else "", "")
        return call
    real_call = backends.labkit.device_call
    try:
        backends.labkit.device_call = switch_call(["true", "false"])
        backends.Adb("emulator-5580").location(False)
        loc_reads = list(reads_)
        reads_.clear()
        backends.labkit.device_call = switch_call(["0"] * 30)
        t_ = time.time()
        stuck_ = refused(lambda: backends.Adb("emulator-5580").power_save(True))
        waited = time.time() - t_
    finally:
        backends.labkit.device_call = real_call
    focus_ = {"n": 0}
    def warn_call(argv, **kw):
        reads_.append(" ".join(argv[4:]))
        if "is-location-enabled" in argv:
            return sp.CompletedProcess(argv, 0, "true", "")
        if argv[4:] == [backends.Adb.FOCUS]:
            focus_["n"] += 1
            return sp.CompletedProcess(argv, 0, "mCurrentFocus=Window{a u0 com.google.android.gms/x."
                                       "LocationOffWarningActivity}" if focus_["n"] == 1 else "launcher", "")
        return sp.CompletedProcess(argv, 0, "", "")
    try:
        reads_.clear()
        backends.labkit.device_call = warn_call
        noted = backends.Adb("emulator-5580").location(True)
        warn_reads = list(reads_)
        focus_["n"] = 1
        reads_.clear()
        quiet = backends.Adb("emulator-5580").location(True)
    finally:
        backends.labkit.device_call = real_call
    check("(C6.1 pilot) Adb.location(True): Play Services' location-off warning, focused after the switch is back on, "
          "is dismissed and the action's mark says so; with none, nothing is pressed and the mark adds nothing",
          (noted, warn_reads[-3:], quiet, "input keyevent KEYCODE_BACK" in reads_),
          ("dismissed=LocationOffWarningActivity", [backends.Adb.FOCUS, "input keyevent KEYCODE_BACK", backends.Adb.FOCUS],
           "", False))
    # (WO-191) Driven, now that the switch's mark is its handler's (ACTION_HANDLERS), not a line of run_host's source.
    class NotingDev(SwitchDev):
        def location(self, on):
            SwitchDev.location(self, on)
            return "dismissed=LocationOffWarningActivity"
    noting_ = dict(base_, actions=[act(location=True)])
    _validate_moves("selftest", noting_, ("ios",))
    noted_dev = NotingDev(woke_)
    run.run_host(noted_dev, noting_, time.time(), time.time() + 3, threading.Event(),
                 {"error": None, "held_s": {}, "sent_s": {}})
    check("(C6.1 pilot) run_host adds a switch's note to its mark (`action=location on dismissed=…`)",
          [c_ for c_ in noted_dev.calls if c_[0] == "mark"],
          [("mark", "host i=0 action=location on dismissed=LocationOffWarningActivity sent_ms=T done_ms=T mark_ms=T")])
    check("(C6.1) Adb.location: set, then read back until it flips (a stale first read is polled past); "
          "Adb.power_save that never reads 1 refuses after 2 s",
          (loc_reads, stuck_, 1.9 < waited < 3.5),
          (["cmd location set-location-enabled false", "cmd location is-location-enabled",
            "cmd location is-location-enabled"],
           "settings get global low_power reads '0' 2 s after cmd power set-mode 1, not '1'", True))
    src_ = inspect.getsource(run.run_step)
    check("(C6.1) run_step: a step naming {lab_http} starts LabHttp and sends the substituted step (payloads and the "
          "launch mark), hands the server to run_host, closes it in stop_runner, and records the config sent",
          [n_ in src_ for n_ in ("if labkit.uses_lab_http(step):\n        server = LabHttp(dev, (step[\"config\"].get("
                                 "\"authorization\") or {}).get(\"accessToken\"),\n                         (step.get("
                                 "\"lab_http\") or {}).get(\"locations\"), bool((step.get(\"lab_http\") or {})"
                                 ".get(\"revoked\")))",
                                 "labkit.launch_payloads(sent)", "json.dumps(sent[\"config\"]",
                                 "stop_host, host,\n                                                         server, plat)",
                                 "        if server is not None:\n            server.close()",
                                 "\"config\": sent.get(\"config\") or {},")], [True] * 6)
    # ---- WO-114: the host's kill, and the uploads on both sides of it ----
    for title, acts_, want in [
            ("`kill: true` loads", [act(kill=True)], "loaded"),
            ("`kill: false` refuses (there is nothing to switch off)", [act(kill=False)], "`kill:` is true"),
            ("an action that kills and switches refuses", [act(kill=True, location=False)], "exactly one of")]:
        got = refused(lambda: _validate_moves("selftest", dict(base_, actions=json.loads(json.dumps(acts_))), ("ios",)))
        check("(WO-114) actions: %s" % title, want in got or got, True)

    class KillDev(FakeDev):
        def kill(self, plat):
            self.calls.append(("kill", plat["package"]))
            return "pid=4242"

    class KillServer:
        def __init__(self):
            self.kills = 0

        def killed(self):
            self.kills += 1
    kdev, ksrv = KillDev(["09-21 10:00:01.000 I/LAB     ( 7): app=boot n=1",
                          "09-21 10:00:02.000 I/LAB     ( 7): point=onDestroy isChangingConfigurations=false"]), KillServer()
    khost = {"error": None, "held_s": {}, "sent_s": {}}
    run.run_host(kdev, dict(base_, actions=[act(kill=True)]), time.time(), time.time() + 3, threading.Event(), khost, ksrv,
                 {"package": "com.lab.app"})
    check("(WO-114) run_host: kill goes to the step's own app, the lab server is told, and the mark names the pid",
          (kdev.calls, ksrv.kills, khost["error"]),
          ([("kill", "com.lab.app"), ("mark", "host i=0 action=kill pid=4242 sent_ms=T done_ms=T mark_ms=T")], 1, None))

    kill_calls = []
    def kill_call(pids):
        def call(argv, **kw):
            kill_calls.append(" ".join(argv[3:]))
            if "pidof" in argv:
                return sp.CompletedProcess(argv, 0 if pids[0] else 1, pids.pop(0), "")
            return sp.CompletedProcess(argv, 0, "", "")
        return call
    try:
        backends.labkit.device_call = kill_call(["4242", "4301"])
        killed_ = backends.Adb("emulator-5580").kill({"package": "com.lab.app"})
        backends.labkit.device_call = kill_call([""])
        none_ = refused(lambda: backends.Adb("emulator-5580").kill({"package": "com.lab.app"}))
    finally:
        backends.labkit.device_call = real_call
    check("(WO-114) Adb.kill: SIGKILL through run-as to the pid pidof names, done once that pid is gone (the restarted "
          "process has another); with nothing running it refuses",
          (killed_, kill_calls[:3], none_),
          ("pid=4242", ["shell pidof com.lab.app", "shell run-as com.lab.app kill -9 4242", "shell pidof com.lab.app"],
           "kill: com.lab.app has no running process"))

    kdev2 = MarkDev()
    klab = run.LabHttp(kdev2)
    url = "http://127.0.0.1:%d" % klab.port
    rec = lambda *stamps: json.dumps({"location": [{"timestamp": t} for t in stamps]})
    post("/locations", rec("t1", "t2"))
    klab.killed()
    post("/locations", rec("t2", "t3"))
    post("/locations", json.dumps({"location": {"timestamp": "t1"}}))
    klab.killed()
    post("/locations", rec("t3", "t4"))
    klab.close()
    check("(WO-114) LabHttp: before a kill a mark is as it was; after one it adds the kills so far and how many of its "
          "records were accepted before an earlier kill (t2, then t1, then t3: first seen after kill 1, back after kill 2)",
          [l.split("records=")[1] for l in kdev2.lines],
          ["2 got_ms=T mark_ms=T", "2 kills=1 replayed=1 got_ms=T mark_ms=T", "1 kills=1 replayed=1 got_ms=T mark_ms=T",
           "2 kills=2 replayed=1 got_ms=T mark_ms=T"])
    kcap = capture(kdev2.lines, [], None, measure_from="launch")
    check("(WO-114) labHttpReplayed and labHttpRecordsAfterKill over those marks; a step with no kill reads 0 and 0",
          (labHttpReplayed(kcap), labHttpRecordsAfterKill(kcap), labHttpRecords(kcap), labHttpReplayed(cap_),
           labHttpRecordsAfterKill(cap_)), (3, 5, 7, 0, 0))

    check("(C6.1 pilot) headless-android-causes' window, sized from its pilots: every asserted row by 11.1 s after the "
          "trigger, about 16 s with the 2026-10-01 moves' later EXIT jump (`after` 21), and by 16.86-22.57 s with the "
          "2026-10-03 moves, whose far fix comes up to 2 s after its hop: `after` 25, 3.7 s to spare on a board, "
          "behind `settle` 9",
          (causes["settle"], causes["after"]), (9, 25))
    check("(C6.1) the lab server's address: the emulator's alias for the host's loopback; the Simulator's own loopback, "
          "which is the Mac's", LAB_HTTP_HOST, {"android": "10.0.2.2", "ios": "127.0.0.1"})
    check("(C6.1) headless-android-causes asserts its precondition before the finish, on changePace(true)'s answer: "
          "moving, enabled, stopOnTerminate false, headless enabled",
          (causes["hooks"][4]["api"], {k: v["equals"] for k, v in causes["hooks"][4]["returns"].items()}),
          ("changePace", {"$.enabled": True, "$.isMoving": True, "$.app.stopOnTerminate": False,
                          "$.app.enableHeadless": True}))
    check("(C6.1) headless-android-causes: GUARDS, one step (Chris: fastest); moving, lab-home at the standing fix; "
          "(2026-10-01: the race at sweep 20260929-093427, then the 70 m walk at 20261001-130425; 2026-10-03: the "
          "speed-less fix at sweep 20261002-130440) lab-home 2850 m; 2750 m west 1 s after the destroy, inside the "
          "fence, a jump the filter rejects on its implied speed whatever the fix carries (a filter), then 30 m back "
          "once the task has that locationfilter (a location), then 2950 m west 5 s later (the EXIT, under the "
          "gate's 60 m/s from move 1's fix); lab-home asks "
          "the task for changePace(false) and its own removal; the toggles only once the task "
          "has that motionchange, "
          "off then on 3 s later; authorization exactly once, refreshed uploads at least one",
          ("headless-android-causes" in sweep_.LISTS["GUARDS"], len(scenario("headless-android-causes")["steps"]),
           [h["api"] for h in causes["hooks"]],
           [(m["to"]["offset_m"], m["to"]["bearing"], m.get("delay_s")) for m in causes["moves"]],
           causes["moves"][1]["after"], [(a["after"], a.get("delay_s", 0)) for a in causes["actions"] if "server" not in a],
           (causes["hooks"][3]["args"]["radius"], causes["hooks"][3]["args"]["extras"]),
           {k: causes["expect"][k] for k in ("headlessEvents.authorization", "labHttpRefreshedUploads",
                                              "headlessEvents.locationerror", "headlessEvents.heartbeat",
                                              "headlessEvents.motionchange", "headlessEvents.geofenceschange",
                                              "headlessCalls")}),
          (True, 1, ["stop", "removeGeofences", "start", "addGeofence", "changePace"],
           [(2750, 270, 1), (2720, 270, 1), (2950, 270, 5)],
           [{"move": 0}, {"headless": "locationfilter"}], [([{"headless": "motionchange"}], d) for d in (0, 0, 0, 3, 3, 3)],
           (2850, {"labChangePace": False, "labRemoveGeofence": True}),
           {"headlessEvents.authorization": 1, "labHttpRefreshedUploads": ">=1", "headlessEvents.locationerror": "any",
            "headlessEvents.heartbeat": 0, "headlessEvents.motionchange": ">=1",
            "headlessEvents.geofenceschange": ">=1", "headlessCalls": "changePace:ok=1,removeGeofence:ok=1"}))
    task_lines = ["I/LAB     ( 7): point=headless event=geofence",
                  "I/LAB     ( 7): point=task-api api=changePace args=false outcome=ok",
                  "I/flutter ( 7): LAB point=task-api api=changePace args=false outcome=error message=x"]
    cap_ = capture(task_lines, [], None, measure_from="launch")
    check("(C6.1) headlessCalls tallies the task's own calls, and a `task-api` line is neither a delivery nor an "
          "unknown name to the headless counters",
          (headlessCalls(cap_), headlessTally(cap_), headlessUnknown(cap_), headlessCalls(capture([], [], None))),
          ("changePace:error=1,changePace:ok=1", "geofence=1", 0, "-"))
    drivers_ = {n_: os.path.join(LABS, "platforms", *rel) for n_, rel in (
        ("capacitor", ("capacitor", "app", "android", "app", "src", "main", "java", "com", "transistorsoft", "labs",
                       "capacitor", "BackgroundGeolocationHeadlessTask.java")),
        ("cordova", ("cordova", "app", "native", "BackgroundGeolocationHeadlessTask.java")),
        ("flutter", ("flutter", "app", "lib", "main.dart")),
        ("react-native", ("react-native", "app", "index.js")))}
    check("(C6.1) every lab app's headless task answers a fence's labChangePace with changePace(<it>) and its "
          "labRemoveGeofence with removeGeofence(<its identifier>), saying so as `point=task-api`, on a geofence event only",
          {n_: all(t_ in open(f_).read() for t_ in ("labChangePace", "labRemoveGeofence", "point=task-api api=",
                                                     "removeGeofence(", "geofence"))
           for n_, f_ in drivers_.items()}, {n_: True for n_ in drivers_})

    # ---- WO-106: one sweep queue per OS ----
    import threading

    def fake_lane(queue_, statuses=(), raise_at=None, wait=None):
        """A device's lane through sweep.drain: `statuses[i]` is its i-th cell's (status, ran), PASS when past
        the end; `raise_at` the cell it dies on; `wait` a barrier its first cell meets."""
        got_ = {"ran": [], "failed": [], "uninstalled": [], "why": None}

        def run_cell(plat, scen):
            if wait is not None and not got_["ran"]:
                wait.wait(timeout=10)
            if (plat, scen) == raise_at:
                raise SystemExit("device call timed out")
            st_, ran_ = statuses[len(got_["ran"])] if len(got_["ran"]) < len(statuses) else ("PASS", True)
            got_["ran"].append((plat, scen))
            return {"status": st_, "detail": "x"}, ran_

        got_["why"] = sweep_.drain(queue_, run_cell, lambda p, s, d, took: got_["failed"].append((p, s, d, took)),
                                   got_["uninstalled"].append)
        return got_

    q_cells = [(p, s) for p in ("flutter", "cordova") for s in ("a", "b", "c", "d")]
    q_ = sweep_.Queue(q_cells, 2)
    meet = threading.Barrier(2)
    lanes_ = [None, None]
    threads_ = [threading.Thread(target=lambda i=i: lanes_.__setitem__(i, fake_lane(q_, wait=meet))) for i in (0, 1)]
    for t_ in threads_:
        t_.start()
    for t_ in threads_:
        t_.join()
    taken = lanes_[0]["ran"] + lanes_[1]["ran"]
    check("(WO-106) two devices draining one queue take every cell exactly once, each device in board order, and "
          "neither stops early",
          (sorted(taken) == sorted(q_cells), len(taken),
           [l_["ran"] == sorted(l_["ran"], key=q_cells.index) for l_ in lanes_], [l_["why"] for l_ in lanes_]),
          (True, 8, [True, True], [None, None]))
    q_ = sweep_.Queue([("cordova", s) for s in "abcdef"], 2)
    meet = threading.Barrier(2)
    threads_ = [threading.Thread(target=lambda i=i: lanes_.__setitem__(i, fake_lane(q_, wait=meet))) for i in (0, 1)]
    for t_ in threads_:
        t_.start()
    for t_ in threads_:
        t_.join()
    check("(WO-106) one platform alone runs on both devices, and each uninstalls it once, at its end",
          ([bool(l_["ran"]) for l_ in lanes_], len(lanes_[0]["ran"]) + len(lanes_[1]["ran"]),
           [l_["uninstalled"] for l_ in lanes_]), ([True, True], 6, [["cordova"], ["cordova"]]))
    solo = fake_lane(sweep_.Queue([("flutter", "a"), ("flutter", "b"), ("cordova", "a"), ("react-native", "a"),
                                   ("react-native", "b")], 1))
    check("(WO-106) a device uninstalls the previous platform's app at every platform switch, and the last at the end",
          solo["uninstalled"], ["flutter", "cordova", "react-native"])
    q_ = sweep_.Queue([("flutter", s) for s in "abcdefg"], 2)
    wedged = fake_lane(q_, [("ERROR", True)] * 7)
    healthy = fake_lane(q_)
    check("(WO-106) a lane whose runs end in ERROR 3 times in a row stops taking cells, says why, and does not "
          "uninstall; the other device runs every cell it left",
          (wedged["ran"], wedged["why"], wedged["uninstalled"], healthy["ran"], wedged["failed"] + healthy["failed"]),
          ([("flutter", s) for s in "abc"], "stopped: 3 consecutive errors: x", [], [("flutter", s) for s in "defg"],
           []))
    check("(WO-106) an n/a cell does not break the row, a failed build does not count toward it, and any other "
          "result (a TIMEOUT) breaks it",
          [len(fake_lane(sweep_.Queue([("flutter", s) for s in "abcdefgh"], 1), seq_)["ran"]) for seq_ in (
              [("ERROR", True), ("ERROR", True), ("n/a", False), ("ERROR", True)],
              [("ERROR", True), ("ERROR", False), ("ERROR", True), ("PASS", True)],
              [("ERROR", True), ("ERROR", True), ("TIMEOUT", True), ("ERROR", True), ("ERROR", True),
               ("ERROR", True)])], [4, 8, 6])
    q_ =sweep_.Queue([("flutter", s) for s in "abcdefgh"], 2)
    first = fake_lane(q_, [("ERROR", True)] * 3)
    second = fake_lane(q_, [("ERROR", True)] * 3)
    check("(WO-106) both lanes stopped: the cells neither took are ERROR, `no device left`, recorded once, by the "
          "last lane to leave",
          (first["failed"], second["failed"]), ([], [("flutter", s, "no device left", False) for s in "gh"]))
    q_ = sweep_.Queue([("flutter", s) for s in "abcd"], 2)
    died = fake_lane(q_, raise_at=("flutter", "b"))
    rest = fake_lane(q_)
    check("(WO-106) a lane that dies marks only the cell it had taken; the other device runs the rest",
          (died["why"], died["failed"], died["uninstalled"], rest["ran"]),
          ("lane died: device call timed out", [("flutter", "b", "lane died: device call timed out", True)], [],
           [("flutter", "c"), ("flutter", "d")]))
    try:
        sweep_.refuse_unusable("ios", ["a", "b", "c"])
        three = "accepted"
    except SystemExit as e:
        three = str(e)
    check("(WO-106) GROUPS is gone: a sweep's devices are 1 or 2, and refuse_unusable still refuses 3",
          (hasattr(sweep_, "GROUPS"), three), (False, "--ios a,b,c: 3 devices, and a sweep runs on 1 or 2"))

    # ------------------------------------------------------------ (Tier C, R) the one rebuild's host side (item 11)
    r_loc = {"subscribe": "onLocation", "returns": {"$.uuid": {"type": "string"}}}
    r_geo = {"subscribe": "onGeofence", "returns": {"$.action": {"equals": "EXIT"}}}
    r_err = dict(r_loc, errors=True, returns={"$.uuid": {"type": "string"}, "$err": {"record": True}})
    for title, step_, want in [
            ("`delivery: 2` loads", events_step(dict(r_geo, delivery=2)), "loaded"),
            ("`delivery: 0` refuses", events_step(dict(r_geo, delivery=0)), "a whole number from 1"),
            ("`delivery: true` refuses", events_step(dict(r_geo, delivery=True)), "a whole number from 1"),
            ("`delivery` beside a `max` below it refuses", events_step(dict(r_geo, delivery=2, max=1)),
             "`max: 1` says never comes"),
            ("`delivery` on an `arm_only` refuses",
             events_step({"subscribe": "onAuthorization", "delivery": 2, "arm_only": "2026-10-04 x"}),
             "`arm_only` says never comes"),
            ("`errors: true` with a recorded `$err` beside a delivery path loads", events_step(r_err), "loaded"),
            ("`errors: true` on watchPosition loads", events_step(dict(r_err, subscribe="watchPosition", at_s=2,
                                                                  args={"interval": 1000})), "loaded"),
            ("`errors: yes-please` refuses", events_step(dict(r_loc, errors="yes")), "`errors: true` passes"),
            ("`errors: true` on a member with one callback refuses", events_step(dict(r_geo, errors=True)),
             "the types give one"),
            ("a `$err` path without `errors: true` refuses", events_step(dict(r_loc, returns=r_err["returns"])),
             "it needs `errors: true`"),
            ("a judged `$err` path refuses", events_step(dict(r_err, returns={"$.uuid": {"type": "string"},
                                                                         "$err": {"type": "number"}})), "never judged"),
            ("a `$err` path alone refuses", events_step(dict(r_err, returns={"$err": {"record": True}})),
             "beside a path of the delivery")]:
        got = refused(lambda: _validate_events("selftest", step_))
        check("(R) events: %s" % title, want in got or got, True)
    check("(R) events: the device is told `delivery` and `errors`, and neither where the declaration has none",
          wire_events([dict(r_geo, delivery=2), r_err, r_loc]),
          [{"on": "onGeofence", "id": "onGeofence@0", "paths": ["$.action"], "delivery": 2},
           {"on": "onLocation", "id": "onLocation@1", "paths": ["$.uuid", "$err"], "errors": True},
           {"on": "onLocation", "id": "onLocation@2", "paths": ["$.uuid"]}])
    check("(R) events: `delivery` and `errors` are what a declaration asks, so a capture taken without them is a "
          "spec change",
          (_asked(r_geo) == _asked(dict(r_geo, delivery=2)), _asked(r_loc) == _asked(dict(r_loc, errors=True))),
          (False, False))
    geo_arm = ev("event-arm id=onGeofence@0 outcome=ok", "events-armed n=1")
    enter = ev("event id=onGeofence@0 n=1", "ret id=onGeofence@0 n=1 path=$.action type=string value=ENTER parent=1")
    exit_ = ev("event id=onGeofence@0 n=2", "ret id=onGeofence@0 n=2 path=$.action type=string value=EXIT parent=1")
    check("(R4) scored: `delivery: 2` holds `returns` to the second delivery's own lines — never the first's; one "
          "delivery of the two reads `fired-1/2`; and without `delivery`, lines that name one are not its reading",
          [eventAssertions(capture(geo_arm + lines_, [], decls_)) for lines_, decls_ in (
              (enter + exit_, [dict(r_geo, delivery=2)]),
              (enter + exit_, [dict(r_geo, delivery=1)]),
              (enter, [dict(r_geo, delivery=2)]),
              (enter + ev("event id=onGeofence@0 n=2"), [dict(r_geo, delivery=2)]),
              (enter + exit_, [r_geo]))],
          ["ok", "onGeofence@0.$.action=want-EXIT-got-ENTER", "onGeofence@0=fired-1/2",
           "onGeofence@0.$.action=unreported", "onGeofence@0.$.action=unreported"])
    err_arm = ev("event-arm id=onLocation@0 outcome=ok", "events-armed n=1", "event id=onLocation@0 n=1",
                 "ret id=onLocation@0 path=$.uuid type=string value=u parent=1")
    err_cap = capture(err_arm + ev("event-error id=onLocation@0 n=1", "ret id=onLocation@0 path=$err type=number "
                                   "value=408 parent=1", "event-error id=onLocation@0 n=2"), [], [r_err])
    check("(R6) scored: an error is never a red, with or without one; the `recorded` row carries how many came and "
          "the first one's reading",
          (eventAssertions(err_cap), METRICS["recorded"](err_cap), eventAssertions(capture(err_arm, [], [r_err])),
           METRICS["recorded"](capture(err_arm, [], [r_err]))),
          ("ok", "onLocation@0.delivered=1, onLocation@0.errors=2, onLocation@0.$err=number:408",
           "ok", "onLocation@0.delivered=1, onLocation@0.errors=0, onLocation@0.$err=unreported"))
    rec_geo = dict(r_geo, delivery=2, returns={"$.action": {"record": True}})
    check("(R4) recorded: a recorded path of a `delivery` declaration reads that delivery",
          METRICS["recorded"](capture(geo_arm + enter + exit_, [], [rec_geo])),
          "onGeofence@0.delivered=2, onGeofence@0.$.action=string:EXIT")
    stop_ = {"api": "stop", "returns": {"$.enabled": {"type": "boolean"}}}
    gcp_ = {"api": "getCurrentPosition", "returns": {"$err": {"equals": 1}}}

    def r_api(hook, outcome, *rets_):
        ident = "%s#0" % hook["api"]
        said_ = ["api id=%s outcome=%s" % (ident, outcome)] + list(rets_) + ["api-done n=1"]
        return apiAssertions(capture(ev(*said_), [hook]))
    err1 = "ret id=getCurrentPosition#0 path=$err type=number value=1 parent=1"
    check("(R5) scored: a call that threw is its own side — red where the call should resolve, red where it should "
          "reject (a `.catch()` never sees it), green only for `outcome: threw`, whose `$err` paths read the thrown "
          "value; and a rejection is not a throw",
          [r_api(stop_, "threw"), r_api(gcp_, "threw", err1), r_api(dict(gcp_, outcome="rejected"), "threw", err1),
           r_api(dict(gcp_, outcome="threw"), "threw", err1), r_api(dict(gcp_, outcome="threw"), "rejected", err1),
           r_api(dict(gcp_, outcome="threw"), "ok"), r_api(gcp_, "rejected", err1)],
          ["stop#0=threw", "getCurrentPosition#0=threw", "getCurrentPosition#0=want-rejected-got-threw", "ok",
           "getCurrentPosition#0=want-threw-got-rejected", "getCurrentPosition#0=want-threw-got-resolved", "ok"])
    check("(R5) `outcome: threw` loads with `$err` paths and refuses `$` ones",
          (load([dict(gcp_, outcome="threw")]),
           "the side `outcome` rules out" in load([dict(stop_, outcome="threw")])), ("loaded", True))
    settled_ = {"api": "getGeofence", "args": "x", "outcome": "settled 2026-10-04 the types are silent"}
    check("(R5) recorded: a `settled` call that threw says so before its `$err` reading",
          METRICS["recorded"](capture(ev("api id=getGeofence#0 outcome=threw",
                                         "ret id=getGeofence#0 path=$err type=string value=404 parent=1",
                                         "api-done n=1"), [settled_])),
          "getGeofence#0=threw, getGeofence#0.$err=string:404")
    check("(R3) a dotted `api:` name is a member of the interface a property returns: logger.getLog and "
          "deviceSettings.isIgnoringBatteryOptimizations load, held to their own arguments and returns",
          (load([{"api": "logger.getLog", "args": {"limit": 5}, "returns": {"$": {"type": "string"}}},
                 {"api": "deviceSettings.isIgnoringBatteryOptimizations", "returns": {"$": {"type": "boolean"}}}]),
           [api_member(n_) is None for n_ in ("logger.getLog", "logger.nope", "getState.enabled", "nothing.getLog",
                                              "logger.getLog.deeper", "getState")]),
          ("loaded", [False, True, True, True, True, False]))
    for title, hook_, want in [
            ("a member the interface lacks refuses", {"api": "logger.nope", "returns": {"$": {"type": "string"}}},
             "is not in the SDK vocabulary"),
            ("a dotted name below a method refuses", {"api": "getState.enabled", "returns": {"$": {"type": "boolean"}}},
             "is not in the SDK vocabulary"),
            ("a dotted property cannot be invoked", {"api": "logger.ORDER_ASC", "returns": {"$": {"equals": 1}}},
             "is a property"),
            ("a dotted name is never probed: the property itself is", {"api": "logger.ORDER_ASC", "probe": True,
                                                                       "returns": {"$": {"equals": 1}}},
             "probe `logger` and assert `$.ORDER_ASC`"),
            ("a dotted call is held to its own return (getLog is a string)",
             {"api": "logger.getLog", "returns": {"$": {"type": "number"}}}, "launder a divergence"),
            ("a dotted call is held to its own arguments (debug takes a message)",
             {"api": "logger.debug", "returns": {"$": {"type": "null|undefined"}}}, "requires message")]:
        got = load([hook_])
        check("(R3) %s" % title, want in got or got, True)
    # Only a PROPERTY's interface is reached through a dot: were the vocabulary ever to list a method's return among
    # `members:`, `getState.enabled` would still be no member — a call's result is not an owner.
    vocabulary()["members"]["State"] = {"enabled": {"returns": "boolean"}}
    try:
        below_ = api_member("getState.enabled")
    finally:
        del vocabulary()["members"]["State"]
    check("(R3) a dot reaches a property's interface only, never a method's return, whatever `members:` lists",
          (below_, sorted(vocabulary()["members"])), (None, ["DeviceSettings", "Logger"]))
    check("(R3) coverage: a dotted call invokes the member of its property's interface; an undotted hook, a probe "
          "and an undeclared name invoke none",
          [summarize.called_member(vocabulary(), h_) for h_ in (
              {"api": "logger.getLog"}, {"api": "deviceSettings.showPowerManager"}, {"api": "getState"},
              {"api": "logger", "probe": True}, {"api": "logger.nope"})],
          ["Logger.getLog", "DeviceSettings.showPowerManager", None, None, None])
    check("(R3) the device is told the dotted name as written",
          wire_hook({"api": "logger.getLog", "args": {"limit": 5}, "returns": {"$": {"type": "string"}}}),
          {"api": "logger.getLog", "paths": ["$"], "args": {"limit": 5}})

    # ---- (Tier C, R) what the adversarial review found (workflow wf_dd022618-4b9), each held here
    check("(R review) the end rule counts as the scorer does: a `delivery: 2` declaration is not complete on its "
          "first delivery, whatever `min` says, and is on its second",
          [ev_state(["event-arm id=onGeofence@0 outcome=ok"] + ["event id=onGeofence@0 n=%d" % k for k in range(1, n_ + 1)],
                    {"events": [d_]})
           for d_, n_ in ((dict(r_geo, delivery=2), 1), (dict(r_geo, delivery=2), 2), (r_geo, 1),
                          (dict(r_geo, delivery=2, min=3), 2), (dict(r_geo, delivery=2, min=3), 3))],
          [False, True, True, False, True])
    check("(R review) min_deliveries: `min`, 1 by default, and the `delivery` where that is later",
          [min_deliveries(d_) for d_ in ({}, {"min": 3}, {"delivery": 2}, {"min": 3, "delivery": 2},
                                         {"min": 0, "max": 0})], [1, 3, 2, 3, 0])
    tagged = ev("event id=onGeofence@0 n=1", "ret id=onGeofence@0 n=1 path=$.action type=string value=ENTER parent=1")
    check("(R review) after_unmet: the `onGeofence ENTER` sugar reads a ret line that names its delivery, of any "
          "delivery; another action's, another id's, and a look-alike id's do not satisfy it",
          [after_unmet([{"event": "onGeofence@0", "action": "ENTER"}], norm_(ls), {}) is None for ls in (
              tagged, enter + exit_, tagged[:1] + exit_[1:],
              tagged[:1] + [tagged[1].replace("onGeofence@0 n=1", "onGeofence@1 n=1")],
              tagged[:1] + [tagged[1].replace("id=onGeofence@0 n=1", "id=onGeofence@00 n=1")])],
          [True, True, False, False, False])
    old_driver = ev("event id=onGeofence@0 n=1", "ret id=onGeofence@0 path=$.action type=string value=ENTER parent=1",
                    "event id=onGeofence@0 n=2")
    check("(R review) a `delivery` declaration reads only lines that name a delivery: a driver from before the "
          "rebuild, which reports the first delivery untagged, reads `unreported`, never that delivery's value",
          [eventAssertions(capture(geo_arm + old_driver, [], [d_])) for d_ in (
              dict(r_geo, delivery=2), dict(r_geo, delivery=2, returns={"$.action": {"equals": "ENTER"}}), r_geo)],
          ["onGeofence@0.$.action=unreported", "onGeofence@0.$.action=unreported",
           "onGeofence@0.$.action=want-EXIT-got-ENTER"])
    check("(R review) ... while its `$err` line, which names none, is still its reading, and another declaration's "
          "untagged line is untouched",
          sorted(k for k in _delivery({("onLocation@0", "$err"): 1, ("onLocation@0", "$.uuid"): 2,
                                       ("onLocation@1", "$.uuid"): 3, ("onLocation@0", "$.uuid", "2"): 4},
                                      {"delivery": 2}, "onLocation@0").items()),
          [(("onLocation@0", "$.uuid"), 4), (("onLocation@0", "$err"), 1), (("onLocation@1", "$.uuid"), 3)])
    dotted_ = [{"api": "logger.debug", "args": "x", "returns": {"$": {"type": "null|undefined"}}},
               {"api": "logger.getLog", "returns": {"$": {"type": "string"}}},
               {"api": "logger.getLog", "returns": {"$": {"same": "logger.getLog#1"}}}]
    check("(R review) a dotted hook's id is a reference like any other: `same: logger.getLog#1`, a `none_after` "
          "anchor, a move's `after: {api: logger.debug#0}`; two dots are none",
          (load(dotted_), refused(lambda: _validate_events("selftest", events_step(
              dict(r_loc, none_after="logger.debug#0"), hooks=dotted_))),
           [bool(REF_ID.match(i_)) for i_ in ("logger.getLog#0", "getState#12", "a.b.c#0", "logger.#0", ".getLog#0",
                                              "logger.getLog#01")]),
          ("loaded", "loaded", [True, True, False, False, False, False]))
    check("(R review) a union of void and primitives is each of them: `void | boolean` (Logger's emailLog and "
          "uploadLog) reads null, undefined or boolean, so its `$` can be asserted; any other union stays an object",
          ([sorted(wire_of({"returns": t_})) for t_ in ("void | boolean", "void", "boolean", "State | null",
                                                        "string | number", "Geofence[]")],
           load([{"api": "logger.uploadLog", "args": "http://127.0.0.1:9", "returns": {"$": {"type": "boolean"}}}]),
           "launder a divergence" in load([{"api": "logger.uploadLog", "args": "http://127.0.0.1:9",
                                            "returns": {"$": {"type": "string"}}}])),
          ([["boolean", "null", "undefined"], ["null", "undefined"], ["boolean"], ["object"], ["number", "string"],
            ["array"]], "loaded", True))

    cov_ = covered(summarize.coverage_args, [{"name": "run", "expect": {"crashes": 0}, "hooks": [
        {"api": "logger.getLog", "args": {"limit": 5}, "returns": {"$": {"type": "string"}}},
        {"api": "logger.debug", "args": "x", "returns": {"$": {"type": "null|undefined"}}},
        {"api": "stop", "returns": {"$.enabled": {"type": "boolean"}}}]}])
    check("(R review) coverage --args lists a dotted call's argument form, under the name the hook writes",
          sorted(" ".join(l.split()[:3]) for l in cov_ if l.strip().startswith("logger.") and " asked " in l),
          ["logger.debug asked string", "logger.getLog asked {limit}"])
    cov_ = covered(summarize.coverage_leaves, [{"name": "run", "expect": {"crashes": 0}, "hooks": [
        {"api": "deviceSettings.showPowerManager", "declared": "both"}]}])
    dsr = {l.split()[0]: l.split()[1:] for l in cov_ if l.strip().startswith("DeviceSettingsRequest.")}
    check("(R review) coverage --leaves lists the payload only a dotted call returns, and types what a `declared:` "
          "on one expands: DeviceSettingsRequest's five, not lastSeenAt, whose `Date` no wire carries (`-`)",
          (sorted(k.split(".")[1] for k, v in dsr.items() if v[-1].startswith("selftest-c40:deviceSettings.showPowerManager#0")),
           dsr.get("DeviceSettingsRequest.lastSeenAt")),
          (["action", "manufacturer", "model", "seen", "version"], ["Date", "-", "-"]))
    omit_ = summarize.labkit.vocabulary()["types"]["omit"]       # the report's own labkit, as HARNESS above
    dated_ = list(omit_.items())
    del omit_["DeviceSettingsRequest.lastSeenAt"]
    try:
        loud_ = covered(summarize.coverage_leaves, [{"name": "run", "expect": {"crashes": 0}, "hooks": [
            {"api": "getState", "returns": {"$.enabled": {"type": "boolean"}}}]}])
    finally:
        omit_.clear()
        omit_.update(dated_)
    check("(R review) ... and only where `omit:` dates it: the same leaf undated, the report refuses the type",
          (len(loud_), "'Date'" in loud_[0]), (1, True))
    box_ = summarize.labkit.vocabulary()["types"]["interfaces"]["DeviceSettingsRequest"]
    declared_ = list(box_.items())
    box_.clear()
    box_.update((k + "?" if k == "lastSeenAt" else k, t) for k, t in declared_)
    try:
        opt_ = covered(summarize.coverage_leaves, [{"name": "run", "expect": {"crashes": 0}, "hooks": [
            {"api": "getState", "returns": {"$.enabled": {"type": "boolean"}}}]}])
    finally:
        box_.clear()
        box_.update(declared_)
    check("(R review) ... and a `?` leaf is dated under its bare name, as `omit:` spells it",
          [l.split()[:3] for l in opt_ if l.strip().startswith("DeviceSettingsRequest.lastSeenAt")],
          [["DeviceSettingsRequest.lastSeenAt?", "Date", "-"]])
    scan_h, scan_c = tempfile.mkdtemp(prefix="labkit-r-scan-h-"), tempfile.mkdtemp(prefix="labkit-r-scan-c-")
    scan_was = summarize.labkit.HARNESS
    try:
        os.makedirs(os.path.join(scan_h, "scenarios"))
        with open(os.path.join(scan_h, "scenarios", "selftest-rscan.yaml"), "w") as f_:
            yaml.safe_dump({"platforms": platforms(), "trigger": "none", "steps": [{
                "name": "run", "expect": {"crashes": 0},
                "events": [{"subscribe": "onGeofence", "delivery": 2, "declared": "both"}]}]}, f_)
        summarize.labkit.HARNESS = scan_h
        scan_events = summarize.labkit.scenario("selftest-rscan")["steps"][0]["events"]
        scan_dir = os.path.join(scan_c, "cordova", "selftest-rscan", "FIX", "run")
        os.makedirs(scan_dir)
        with open(os.path.join(scan_dir, "logcat.txt"), "w") as f_:
            f_.write("\n".join(["09-21 10:00:00.000 I/LAB     ( 1): point=launch label=x"] + [
                "09-21 10:00:05.%03d I/LAB     ( 1): app=%s" % (k_, l_) for k_, l_ in enumerate([
                    "event-arm id=onGeofence@0 outcome=ok", "event id=onGeofence@0 n=1",
                    "ret id=onGeofence@0 n=1 path=$.action type=string value=ENTER parent=1",
                    "event id=onGeofence@0 n=2",
                    "ret id=onGeofence@0 n=2 path=$.action type=string value=NOPE parent=1"])] + [
                "09-21 10:00:20.000 I/LAB     ( 1): point=trigger none",
                "09-21 10:00:30.000 I/LAB     ( 1): point=capture"]) + "\n")
        with open(os.path.join(scan_dir, "run.json"), "w") as f_:
            json.dump({"scenario": "selftest-rscan", "step": "run", "hooks": [], "events": scan_events, "year": 2026}, f_)
        scan_out = io.StringIO()
        with contextlib.redirect_stdout(scan_out):
            scan_rc = summarize.enum_scan(scan_c)
    finally:
        summarize.labkit.HARNESS = scan_was
        shutil.rmtree(scan_h)
        shutil.rmtree(scan_c)
    scan_said = [l.strip() for l in scan_out.getvalue().splitlines()]
    check("(R review) in-scan: a `delivery` declaration's reading is its own delivery's line, held to the value set "
          "(the second, outside it, is red; the first is not its reading)",
          (scan_rc, len(scan_said), "onGeofence@0.$.action=want-in-" in scan_said[0] and scan_said[0].endswith("-got-NOPE"),
           scan_said[-1]),
          (1, 2, True, "== in-scan: 1 readings held to their value sets, 1 red"))

    # ---- (Tier C, R; addendum A1) the Dart surface: `declared: dart`, and the rows generated from docforge/db
    import dart_members as dm_
    d_acc = {"api": "DesiredAccuracy", "probe": True, "declared": "dart"}
    _validate_hooks("selftest", {"hooks": [d_acc]})
    d_ev = {"api": "Event", "probe": True, "declared": "dart"}
    _validate_hooks("selftest", {"hooks": [d_ev]})
    check("(A1) declared: dart — the driver's map, then each member the docs give a Dart spelling, held to the "
          "TYPES' value and in their order; a member the db marks `ignore: [dart]` is not asked",
          (d_acc["returns"], len(d_ev["returns"]), "$.Notification" in d_ev["returns"], d_ev["returns"]["$.Boot"]),
          ({"$": {"type": "object"}, "$.Navigation": {"type": "number", "equals": -2},
            "$.High": {"type": "number", "equals": -1}, "$.Medium": {"type": "number", "equals": 10},
            "$.Low": {"type": "number", "equals": 100}, "$.VeryLow": {"type": "number", "equals": 1000},
            "$.Lowest": {"type": "number", "equals": 3000}}, 18, False, {"type": "string", "equals": "boot"}))
    for title, hook_, want in [
            ("without `probe` refuses", {"api": "DesiredAccuracy", "declared": "dart"}, "is a probe's"),
            ("a family the docs give no Dart members refuses", {"api": "AuthorizationStatus", "probe": True,
                                                                 "declared": "dart"}, "the docs give Dart members"),
            ("`logger` is no enum namespace", {"api": "logger", "probe": True, "declared": "dart"},
             "the docs give Dart members"),
            ("an unknown `declared` names every property form", {"api": "DesiredAccuracy", "probe": True,
                                                                  "declared": "java"},
             "`members`, `dart`, `kotlin` or `swift`")]:
        got = load([hook_])
        check("(A1) declared: dart — %s" % title, want in got or got, True)
    dart_members()["LogLevel"]["members"]["Loud"] = "LogLevel.loud"
    try:
        stale_ = load([{"api": "LogLevel", "probe": True, "declared": "dart"}])
    finally:
        del dart_members()["LogLevel"]["members"]["Loud"]
    check("(A1) declared: dart — a Dart spelling for a member the types do not declare refuses, naming it",
          "LogLevel.Loud, which the types do not declare" in stale_ or stale_, True)
    apid = scenario("api-properties-dart")
    check("(A1) api-properties-dart: nine probes, every one `declared: dart`, 60 paths (9 roots, 51 members); "
          "Flutter's alone, the three JavaScript bridges excused in writing, and (WO-143) the native platform, whose "
          "are api-properties-kotlin and -swift; in SUITE since it read green",
          (len(apid["steps"][0]["hooks"]), all(h.get("declared") == "dart" for h in apid["steps"][0]["hooks"]),
           sum(len(h["returns"]) for h in apid["steps"][0]["hooks"]), apid["platforms"],
           sorted(apid.get("not_applicable") or {}),
           ("api-properties-dart" in sweep_.SUITE, "api-properties-dart" in sweep_.LISTS["OPTIN"])),
          (9, True, 60, ["flutter"], ["capacitor", "cordova", "native", "react-native"], (True, False)))
    table_ = dm_.read()
    check("(A1) the generated table and the Flutter driver's rows are what docforge/db gives today: 51 members in "
          "nine families (`dart_members.py --diff`)",
          (open(dm_.TABLE, encoding="utf-8").read() == dm_.as_yaml(table_),
           open(dm_.DART, encoding="utf-8").read() == dm_.as_dart(table_),
           len(table_), sum(len(e["members"]) for e in table_.values()), dart_members() == table_),
          (True, True, 9, 51, True))
    check("(A1) every family's wire field is one the plugin's own toMap was read for, and the rows put it after the "
          "spelling: an enum's `.id`, a plain enum's `.index`, Permission's `.value`, an Event constant itself",
          ([l.strip() for l in dm_.as_dart(table_).splitlines() if "'High': bg.DesiredAccuracy" in l
            or "'Adjust'" in l or "'Motion'" in l or "'Boot'" in l], sorted(set(dm_.WIRE) - set(table_))),
          (["'High': bg.DesiredAccuracy.high.id,", "'Boot': bg.Event.BOOT,",
            "'Adjust': bg.LocationFilterPolicy.adjust.index,", "'Motion': bg.Permission.motion.value,"], []))
    fake_db = tempfile.mkdtemp(prefix="labkit-dart-")

    def db_entry(name, **entry):
        with open(os.path.join(fake_db, name + ".yaml"), "w") as f_:
            yaml.safe_dump(dict({"id": name}, **entry), f_)

    def dart_read():
        try:
            return dm_.read(fake_db)
        except SystemExit as e:
            return str(e)
    try:
        db_entry("Event", kind="enum", signatures={"dart": "class Event"})
        db_entry("Event.Boot", kind="enum_member", signatures={"dart": 'static const BOOT = "boot"'})
        db_entry("Event.Notification", kind="enum_member", ignore=["dart"], signatures={"dart": "Event.nope"})
        db_entry("LogLevel", kind="enum", signatures={"dart": "enum LogLevel"})
        db_entry("LogLevel.Off", kind="enum_member", signatures={"dart": "LogLevel.off", "kotlin": "x"})
        db_entry("LogLevel.Info", kind="enum_member", signatures={"kotlin": "val INFO"})
        # Its text passes the generator's cheap filter; its kind does not.
        db_entry("Config.http", kind="property", description="not of kind: enum", signatures={"dart": "HttpConfig http"})
        first_ = dart_read()
        db_entry("LogLevel.Odd", kind="enum_member", signatures={"dart": "LogLevel.off()"})
        odd_ = dart_read()
        os.remove(os.path.join(fake_db, "LogLevel.Odd.yaml"))
        db_entry("TrackingMode.Location", kind="enum_member", signatures={"dart": "TrackingMode.location"})
        unread_ = dart_read()
        os.remove(os.path.join(fake_db, "TrackingMode.Location.yaml"))
        db_entry("LogLevel.Borrowed", kind="enum_member", signatures={"dart": "Event.boot"})
        borrowed_ = dart_read()
        os.remove(os.path.join(fake_db, "LogLevel.Borrowed.yaml"))
        db_entry("Event", kind="enum", signatures={})
        classless_ = dart_read()
    finally:
        shutil.rmtree(fake_db)
    check("(A1) the generator: an expression as written, a constant on its family's class; a member marked "
          "`ignore: [dart]`, one with no Dart spelling and an entry that is no enum member are left out",
          first_, {"Event": {"wire": "", "members": {"Boot": "Event.BOOT"}},
                   "LogLevel": {"wire": ".id", "members": {"Off": "LogLevel.off"}}})
    check("(A1) the generator refuses a spelling of neither shape, a family whose wire field nobody read, and a "
          "constant whose family names no class",
          ["neither an expression" in odd_, "records no wire field for TrackingMode" in unread_,
           "names no class" in classless_], [True, True, True])
    check("(R review) the generator holds an expression to its family's own class: one borrowed from another "
          "family would compile, and could read green on a coinciding value",
          "`Event.boot` is a member of Event" in borrowed_ and "names LogLevel" in borrowed_ or borrowed_, True)

    # ---- (WO-143) `absent_on`: a member one platform's API does not have, by decision
    ao_hook_ = {"api": "registerHeadlessTask", "probe": True, "returns": {"$": {"type": "function"}},
                "absent_on": {"native": "2026-10-14 — not required for the typed API"}}
    ao_absent_ = "I/LAB     ( 1): app=api id=registerHeadlessTask#0 outcome=absent"
    ao_ok_ = ["I/LAB     ( 1): app=api id=registerHeadlessTask#0 outcome=ok",
              "I/LAB     ( 1): app=ret id=registerHeadlessTask#0 path=$ type=function parent=1"]
    check("(WO-143) absent_on: `absent` holds on the platform it names and anything else there is red; every other "
          "platform reads the hook as before; it is no part of the question, so an earlier capture still answers",
          (apiAssertions(capture([ao_absent_, "I/LAB     ( 1): app=api-done n=1"], [ao_hook_], platform="native")),
           apiAssertions(capture(ao_ok_ + ["I/LAB     ( 1): app=api-done n=1"], [ao_hook_], platform="native")),
           apiAssertions(capture(ao_ok_ + ["I/LAB     ( 1): app=api-done n=1"], [ao_hook_], platform="flutter")),
           apiAssertions(capture([ao_absent_, "I/LAB     ( 1): app=api-done n=1"], [ao_hook_], platform="flutter")),
           _asked(ao_hook_) == _asked({k_: v_ for k_, v_ in ao_hook_.items() if k_ != "absent_on"})),
          ("ok", "registerHeadlessTask#0=want-absent-got-ok", "ok", "registerHeadlessTask#0=absent", True))
    check("(WO-143) absent_on refuses an undated reason, a platform the lab does not have, and an empty map",
          ["`absent_on:` is" in load([dict(ao_hook_, absent_on=v_)]) for v_ in
           ({"native": "not required"}, {"java": "2026-10-14 — x"}, {})], [True, True, True])

    # ---- (WO-143 stage 4) the Kotlin and Swift surface: `declared: kotlin|swift`, from rows generated from docforge/db
    import native_members as nm_
    nm_table_ = nm_.read()
    check("(WO-143) the generated native members are what docforge/db gives today: Kotlin 52 in 15 families, Swift 35 "
          "in 11 (`native_members.py --diff`; TriggerActivity and Kotlin's Navigation and VeryLow since "
          "4.8.0-master-f353ee9), and the host reads the same table",
          (open(nm_.TABLE, encoding="utf-8").read() == nm_.as_yaml(nm_table_),
           open(nm_.KOTLIN, encoding="utf-8").read() == nm_.as_kotlin(nm_table_),
           open(nm_.SWIFT, encoding="utf-8").read() == nm_.as_swift(nm_table_),
           [(len(f_), sum(len(e_["members"]) for e_ in f_.values())) for f_ in nm_table_.values()],
           native_members() == nm_table_),
          (True, True, True, [(15, 52), (11, 35)], True))
    k_acc_ = {"api": "DesiredAccuracy", "probe": True, "declared": "kotlin"}
    _validate_hooks("selftest", {"hooks": [k_acc_]})
    s_acc_ = {"api": "DesiredAccuracy", "probe": True, "declared": "swift"}
    check("(WO-143) declared: kotlin — the driver's map, then each member the docs give a Kotlin spelling, held to "
          "the TYPES' value, in the types' order (Navigation and VeryLow since WO-169); and a family the docs give Swift "
          "no members of refuses `declared: swift`, naming the table",
          (k_acc_["returns"], "the docs give Swift members (labs/harness/native_members.yaml" in load([s_acc_])),
          ({"$": {"type": "object"}, "$.Navigation": {"type": "number", "equals": -2},
            "$.High": {"type": "number", "equals": -1}, "$.Medium": {"type": "number", "equals": 10},
            "$.Low": {"type": "number", "equals": 100}, "$.VeryLow": {"type": "number", "equals": 1000},
            "$.Lowest": {"type": "number", "equals": 3000}}, True))
    native_members()["kotlin"]["LogLevel"]["members"]["Loud"] = "LogLevel.LOUD"
    try:
        stale_k_ = load([{"api": "LogLevel", "probe": True, "declared": "kotlin"}])
    finally:
        del native_members()["kotlin"]["LogLevel"]["members"]["Loud"]
    check("(WO-143) declared: kotlin — a Kotlin spelling for a member the types do not declare refuses, naming it",
          "a Kotlin spelling to LogLevel.Loud" in stale_k_ or stale_k_, True)
    check("(WO-143) the rows put each class's value member after the spelling: a Kotlin enum's `.value`, the Java "
          "filter enums' `.id`, a Swift raw value; every class the db spells has a wire field somebody read",
          ([l_.strip() for l_ in nm_.as_kotlin(nm_table_).splitlines()
            if '"High" to DesiredAccuracy' in l_ or '"Adjust" to' in l_],
           [l_.strip() for l_ in nm_.as_swift(nm_table_).splitlines() if '"Debug": BGGeo' in l_],
           sorted({e_["class"] for f_ in nm_table_.values() for e_ in f_.values()}
                  - {c_ for w_ in nm_.WIRE.values() for c_ in w_})),
          (['"High" to DesiredAccuracy.HIGH.value,', '"Adjust" to LocationFilterPolicy.Adjust.id,'],
           ['"Debug": BGGeo.LogLevel.debug.rawValue,'], []))
    apik_, apis_ = scenario("api-properties-kotlin"), scenario("api-properties-swift")
    check("(WO-143) api-properties-kotlin and -swift: every probe `declared:` its language, each on the native "
          "platform's one OS (13 families and 61 paths, 9 and 40), the other OS and the four bridges excused in "
          "writing; opt-in, a measurement",
          [(len(a_["steps"][0]["hooks"]), {h_.get("declared") for h_ in a_["steps"][0]["hooks"]},
            sum(len(h_["returns"]) for h_ in a_["steps"][0]["hooks"]), a_["platforms"], sorted(a_["not_applicable"]),
            a_["name"] in sweep_.LISTS["OPTIN"]) for a_ in (apik_, apis_)],
          [(13, {"kotlin"}, 61, ["native"], ["capacitor", "cordova", "flutter", "ios", "react-native"], True),
           (9, {"swift"}, 40, ["native"], ["android", "capacitor", "cordova", "flutter", "react-native"], True)])

    # ---- (WO-143) the typed config: every config path's Kotlin and Swift member, generated from docforge/db
    import typed_config as tc_
    typed_ = tc_.read()
    check("(WO-143) the generated table and the native driver's Kotlin are what docforge/db gives today "
          "(`typed_config.py --diff`), and the table reads back as written.  Kotlin: 88 members carry 91 of the "
          "vocabulary's 99 leaves, 1 is a method, 7 are absent; Swift: 72, 1 and 26",
          (open(tc_.TABLE, encoding="utf-8").read() == tc_.as_yaml(typed_),
           open(tc_.KOTLIN, encoding="utf-8").read() == tc_.as_kotlin(typed_),
           _load(tc_.TABLE) == tc_.plain(typed_), tc_.counts(typed_)),
          (True, True, True, {"kotlin": (88, 91, 1, 7, 1, 1), "swift": (72, 72, 1, 26, 0, 0)}))

    def config_leaves_(tree, prefix=""):
        for k_, v_ in tree.items():
            yield from (config_leaves_(v_, prefix + k_ + ".") if isinstance(v_, dict) else [prefix + k_])
    for lang_ in tc_.LANGUAGES:
        t_ = typed_[lang_]
        held_ = (sorted(p_ for p_, leaf_ in t_["leaves"].items() if "carries" not in leaf_)
                 + sorted("%s.%s" % (p_, k_) for p_, leaf_ in t_["leaves"].items() for k_ in leaf_.get("carries", []))
                 + sorted(t_["methods"]) + sorted(t_["absent"]))
        check("(WO-143) %s: every config leaf of the vocabulary is carried by a member, is a method or is marked "
              "absent — once, and nothing else is" % lang_,
              sorted(held_) == sorted(config_leaves_(vocabulary()["config"])) and len(held_) == len(set(held_)), True)
    kt_, sw_ = typed_["kotlin"]["leaves"], typed_["swift"]["leaves"]
    check("(WO-143) an enum-typed member takes the docs' own correspondence: the types' value of a member, spelt as "
          "the db spells that member; a value the language has no member for is `unspelt` (Kotlin's DesiredAccuracy had "
          "two until WO-169 gave it Navigation and VeryLow)",
          (kt_["geolocation.desiredAccuracy"]["values"], kt_["geolocation.desiredAccuracy"]["unspelt"],
           sw_["logger.logLevel"]["values"][4], sw_["geolocation.filter.kalmanProfile"]["values"][0]),
          ({-2: "DesiredAccuracy.NAVIGATION", -1: "DesiredAccuracy.HIGH", 10: "DesiredAccuracy.MEDIUM",
            100: "DesiredAccuracy.LOW", 1000: "DesiredAccuracy.VERY_LOW", 3000: "DesiredAccuracy.LOWEST"}, [],
           "LogLevel.debug", "KalmanProfile.default"))
    check("(WO-143) a leaf the types declare as a union of a family's own values takes them; one they declare as "
          "something else takes the member's raw value — Swift's from its signature, Kotlin's from RAW",
          (kt_["geolocation.locationAuthorizationRequest"]["values"]["WhenInUse"],
           kt_["persistence.locationsOrderDirection"]["values"], sw_["persistence.locationsOrderDirection"]["values"],
           kt_["authorization.strategy"]["values"], sw_["authorization.strategy"]["values"],
           sorted(tc_.RAW["kotlin"])),
          ("LocationAuthorizationRequest.WHEN_IN_USE",
           {"ASC": "LocationsOrderDirection.ASC", "DESC": "LocationsOrderDirection.DESC"},
           {"ASC": "LocationsOrderDirection.ascending", "DESC": "LocationsOrderDirection.descending"},
           {"JWT": "AuthorizationStrategy.JWT", "SAS": "AuthorizationStrategy.SAS"},
           {"JWT": "AuthorizationStrategy.jwt", "SAS": "AuthorizationStrategy.sas"},
           ["AuthorizationStrategy", "LocationsOrderDirection"]))
    check("(WO-143) an interface the language has no class for is carried whole by its member, and a group the "
          "language lacks takes its leaves with it: Kotlin's rationale is one Map, Swift has no notification",
          (kt_["app.backgroundPermissionRationale"]["carries"], kt_["app.backgroundPermissionRationale"]["type"],
           sorted(k_ for k_ in typed_["swift"]["absent"] if k_.startswith("app.notification."))[:2],
           {v_ for k_, v_ in typed_["swift"]["absent"].items() if k_.startswith("app.notification.")},
           "app.notification" in typed_["kotlin"]["groups"], "app.notification" in typed_["swift"]["groups"]),
          (["message", "negativeAction", "positiveAction", "title"], "Map<String, String>",
           ["app.notification.actions", "app.notification.channelId"], {"AppConfig.notification"}, True, False))
    kotlin_ = tc_.as_kotlin(typed_)
    check("(WO-143) the Kotlin is one typed assignment and one typed read per leaf, through the member's own "
          "spelling: the value converted at once and the assignment bound for the configure block; an unassignable "
          "leaf's refuses by name and its read stands; an undocumented enum member's read refuses by name, so a NEW "
          "one still fails the build",
          [l_.strip() for l_ in kotlin_.splitlines()
           if '"geolocation.distanceFilter" ->' in l_ or '"persistence.timestampFormat" ->' in l_
           or "KalmanProfile.Custom" in l_ or "DesiredAccuracy.LOWEST" in l_],
          ['3000 -> DesiredAccuracy.LOWEST',
           '"geolocation.distanceFilter" -> typed(Wire.float(path, v)) { geolocation.distanceFilter = it }',
           '"persistence.timestampFormat" -> throw Wire.Unassignable(path, UNASSIGNABLE.getValue(path))',
           'DesiredAccuracy.LOWEST -> 3000',
           '"geolocation.distanceFilter" -> c.geolocation.distanceFilter',
           'KalmanProfile.Custom -> throw Wire.Undocumented(path, "KalmanProfile.Custom")',
           '"persistence.timestampFormat" -> c.persistence.timestampFormat'])
    wire_kt_ = open(os.path.join(os.path.dirname(tc_.KOTLIN), "Wire.kt"), encoding="utf-8").read()
    check("(WO-143) every converter the Kotlin calls is one the driver's Wire.kt declares",
          sorted(c_ for c_ in set(tc_.CONVERT["kotlin"].values()) | {"int", "string"}
                 if "    fun %s(path: String, v: Any?)" % c_ not in wire_kt_), [])
    fake_db = tempfile.mkdtemp(prefix="labkit-typed-")
    fake_vocab_ = os.path.join(fake_db, "vocabulary")           # no .yaml: the generator reads the db's *.yaml
    held_tables_ = (tc_.UNASSIGNABLE, tc_.UNDOCUMENTED, tc_.RAW)

    def typed_entry(name, **entry):
        with open(os.path.join(fake_db, name + ".yaml"), "w") as f_:
            yaml.safe_dump(dict({"id": name}, **entry), f_)

    def typed_vocab(config, **enums):
        with open(fake_vocab_, "w") as f_:
            yaml.safe_dump({"config": config, "enums": enums}, f_)

    def typed_read():
        try:
            return tc_.read(fake_db, fake_vocab_)
        except SystemExit as e:
            return str(e)

    def member_(owner, name, kotlin, swift, **more):
        typed_entry("%s.%s" % (owner, name), kind="property",
                    signatures={k_: v_ for k_, v_ in (("kotlin", kotlin), ("swift", swift)) if v_},
                    targets={k_: "%s.%s" % ({"GeoConfig": "GeolocationConfig"}.get(owner, owner), name)
                             for k_, v_ in (("kotlin", kotlin), ("swift", swift)) if v_},
                    ignore=[k_ for k_, v_ in (("kotlin", kotlin), ("swift", swift)) if not v_], **more)
    try:
        tc_.UNASSIGNABLE, tc_.UNDOCUMENTED, tc_.RAW = ({"kotlin": {}, "swift": {}}, {"kotlin": {}, "swift": {}},
                                                       {"kotlin": {}, "swift": {}})
        typed_vocab({"geolocation": {"distanceFilter": "number", "desiredAccuracy": "DesiredAccuracy",
                                     "activityType": "ActivityType"}, "reset": "boolean"},
                    DesiredAccuracy={"wire": "number", "values": [-1, 10], "members": {"High": -1, "Medium": 10}})
        typed_entry("Config", kind="interface", targets={"kotlin": "Config", "swift": "Config"})
        typed_entry("GeoConfig", kind="interface", targets={"kotlin": "GeolocationConfig", "swift": "GeolocationConfig"})
        member_("Config", "geolocation", "val geolocation", "public let geolocation: BGGeo.GeolocationConfig",
                ts_type="GeoConfig")
        member_("Config", "reset", "fun reset()", "public func reset()", ts_type="boolean")
        member_("GeoConfig", "distanceFilter", "var distanceFilter: Float", "public var distanceFilter: CLLocationDistance")
        member_("GeoConfig", "desiredAccuracy", "var desiredAccuracy: DesiredAccuracy",
                "public var desiredAccuracy: CLLocationAccuracy")
        member_("GeoConfig", "activityType", None, "public var activityType: CLActivityType")
        typed_entry("DesiredAccuracy", kind="enum", targets={"kotlin": "DesiredAccuracy"}, ignore=["swift"])
        typed_entry("DesiredAccuracy.High", kind="enum_member", targets={"kotlin": "DesiredAccuracy.HIGH"}, ignore=["swift"])
        typed_entry("DesiredAccuracy.Medium", kind="enum_member", ignore=["kotlin", "swift"])
        typed_entry("BackgroundGeolocation.ready", kind="method", description="no config option")
        first_ = typed_read()
        member_("GeoConfig", "distanceFilter", "var distanceFilter: BigDecimal", "public var distanceFilter: CLLocationDistance")
        untyped_ = typed_read()
        member_("GeoConfig", "distanceFilter", "var distanceFilter: Float", "public var distanceFilter: CLLocationDistance")
        typed_entry("GeoConfig.distanceFilter", kind="property", signatures={"kotlin": "var distanceFilter: Float"},
                    targets={"kotlin": "GeolocationConfig.distanceFilter"})
        silent_ = typed_read()
        typed_entry("GeoConfig.distanceFilter", kind="property", signatures={"kotlin": "var distanceFilter: Float"},
                    targets={"kotlin": "GeolocationConfig.distanceFilter"}, ignore=["kotlin", "swift"])
        both_ = typed_read()
        typed_entry("GeoConfig.distanceFilter", kind="property", ignore=["swift"],
                    signatures={"kotlin": "var distanceFilter: Float"}, targets={"kotlin": "HttpConfig.distanceFilter"})
        elsewhere_ = typed_read()
        member_("GeoConfig", "distanceFilter", "var distanceFilter: Float", "public var distanceFilter: CLLocationDistance")
        member_("GeoConfig", "stopTimeout", "var stopTimeout: Long", "public var stopTimeout: TimeInterval")
        undeclared_ = typed_read()
        os.remove(os.path.join(fake_db, "GeoConfig.stopTimeout.yaml"))
        os.remove(os.path.join(fake_db, "GeoConfig.activityType.yaml"))
        missing_ = typed_read()
        member_("GeoConfig", "activityType", None, "public var activityType: CLActivityType")
        typed_entry("DesiredAccuracy.High", kind="enum_member", targets={"kotlin": "LogLevel.HIGH"}, ignore=["swift"])
        borrowed_ = typed_read()
        typed_entry("DesiredAccuracy.High", kind="enum_member", targets={"kotlin": "DesiredAccuracy.HIGH"}, ignore=["swift"])
        typed_vocab({"geolocation": {"distanceFilter": "number", "desiredAccuracy": "string",
                                     "activityType": "ActivityType"}, "reset": "boolean"},
                    DesiredAccuracy={"wire": "number", "values": [-1, 10], "members": {"High": -1, "Medium": 10}})
        unread_ = typed_read()
        tc_.RAW = {"kotlin": {"DesiredAccuracy": {"HIGH": "high"}}, "swift": {}}
        raw_ = typed_read()
        tc_.UNASSIGNABLE = {"kotlin": {"geolocation.stopTimeout": "2026-10-10 selftest"}, "swift": {}}
        stale_ = typed_read()
        tc_.UNASSIGNABLE = {"kotlin": {}, "swift": {}}
        tc_.UNDOCUMENTED = {"kotlin": {"DesiredAccuracy.HIGH": "2026-10-10 selftest"}, "swift": {}}
        caught_up_ = typed_read()
        tc_.UNDOCUMENTED = {"kotlin": {"LogLevel.TRACE": "2026-10-10 selftest"}, "swift": {}}
        unused_ = typed_read()
    finally:
        tc_.UNASSIGNABLE, tc_.UNDOCUMENTED, tc_.RAW = held_tables_
        shutil.rmtree(fake_db)
    check("(WO-143) the generator: a path is the vocabulary's, each step's member the db's; a group is walked, a "
          "method listed, a leaf typed by its declaration; an entry marked `ignore` is absent in that language "
          "alone; an entry that is no config option is not read",
          first_ if not isinstance(first_, dict) else {l_: {k_: v_ for k_, v_ in t_.items() if v_} for l_, t_ in first_.items()},
          {"kotlin": {"groups": {"geolocation": "GeolocationConfig"}, "methods": {"reset": "Config.reset"},
                      "absent": {"geolocation.activityType": "GeoConfig.activityType"},
                      "leaves": {"geolocation.distanceFilter": {"member": "GeolocationConfig.distanceFilter", "type": "Float",
                                                                "access": "geolocation.distanceFilter", "as": "float"},
                                 "geolocation.desiredAccuracy": {"member": "GeolocationConfig.desiredAccuracy",
                                                                 "type": "DesiredAccuracy", "access": "geolocation.desiredAccuracy",
                                                                 "values": {-1: "DesiredAccuracy.HIGH"}, "unspelt": [10]}}},
           "swift": {"groups": {"geolocation": "GeolocationConfig"}, "methods": {"reset": "Config.reset"},
                     "leaves": {"geolocation.distanceFilter": {"member": "GeolocationConfig.distanceFilter",
                                                               "type": "CLLocationDistance", "access": "geolocation.distanceFilter",
                                                               "as": "double"},
                                "geolocation.desiredAccuracy": {"member": "GeolocationConfig.desiredAccuracy",
                                                                "type": "CLLocationAccuracy", "access": "geolocation.desiredAccuracy",
                                                                "as": "double"},
                                "geolocation.activityType": {"member": "GeolocationConfig.activityType", "type": "CLActivityType",
                                                             "access": "geolocation.activityType", "as": "clActivityType"}}}})
    check("(WO-143) the generator refuses, naming the entry: a declared type it has no conversion for; an entry "
          "with neither a spelling nor an `ignore`; one with both; a target on another class",
          ["GeoConfig.distanceFilter.yaml: declared `var distanceFilter: BigDecimal`" in untyped_
           and "no conversion for BigDecimal" in untyped_,
           "GeoConfig.distanceFilter.yaml: no `signatures.swift`, and no `ignore: [swift]`" in silent_,
           "GeoConfig.distanceFilter.yaml: marked `ignore: [kotlin]` and given a kotlin signature too" in both_,
           "`targets.kotlin` is 'HttpConfig.distanceFilter'" in elsewhere_ and "GeolocationConfig" in elsewhere_],
          [True, True, True, True])
    check("(WO-143) closed on both sides: an option the db carries and the vocabulary does not refuses, and so does "
          "a vocabulary path the db has no entry for",
          ["docforge/db has GeoConfig.stopTimeout, which the vocabulary's config does not declare" in undeclared_,
           "docforge/db has no property GeoConfig.activityType.yaml" in missing_ and "geolocation.activityType" in missing_],
          [True, True])
    check("(WO-143) an enum member is held to its family's own class; in a leaf the types do not declare as that "
          "enum, Kotlin's value is RAW's or a refusal, never a guess",
          ["`targets.kotlin` is 'LogLevel.HIGH', and DesiredAccuracy.yaml names the class DesiredAccuracy" in borrowed_,
           "RAW records no value for it" in unread_ and "DesiredAccuracy.HIGH" in unread_,
           raw_ if not isinstance(raw_, dict) else raw_["kotlin"]["leaves"]["geolocation.desiredAccuracy"]["values"]],
          [True, True, {"high": "DesiredAccuracy.HIGH"}])
    check("(WO-143) a dated entry that no longer applies refuses as stale: an unassignable path that is no leaf, an "
          "undocumented member the docs spell now, one of no enum a leaf declares",
          ["UNASSIGNABLE names geolocation.stopTimeout, which is no kotlin leaf" in stale_,
           "UNDOCUMENTED names DesiredAccuracy.HIGH, which the docs spell now" in caught_up_,
           "UNDOCUMENTED names LogLevel.TRACE, which is of no enum a kotlin leaf declares" in unused_],
          [True, True, True])
    check("(WO-143) every dated entry carries its date and the work order that owns it, which exists",
          sorted(k_ for table__ in (tc_.UNASSIGNABLE, tc_.UNDOCUMENTED) for l_ in table__.values()
                 for k_, why_ in l_.items()
                 if not re.match(r"\d{4}-\d\d-\d\d \S", why_) or not re.findall(r"\bWO-\d{3}\b", why_)
                 or any(not glob.glob(os.path.join(os.path.dirname(LABS), "workorders", id_ + "-*.md"))
                        for id_ in re.findall(r"\bWO-\d{3}\b", why_))), [])

    # ---- (WO-143) one source for the platform list: the manifests, in board order, each on the OSes it has a host on
    four_ = ["flutter", "react-native", "capacitor", "cordova"]
    bridges_ = [p_ for p_ in board_order() if p_ in four_]
    check("(WO-143) board_order() is the manifests' `column`s, and the four bridges keep the order sweep.py and "
          "lab_setup.py each spelt as a list until WO-143: no cell of theirs moves on a board or in a queue",
          (bridges_, lab_cells(bridges_, ["android", "ios"]), lab_cells(bridges_, ["ios"])[:2]),
          (four_, [(p_, o_) for p_ in four_ for o_ in ("android", "ios")], [("flutter", "ios"), ("react-native", "ios")]))
    check("(WO-143) neither sweep.py nor lab_setup.py keeps a platform list of its own, and a scenario that names no "
          "platform is every platform's",
          ([f_ for f_ in ("sweep.py", "lab_setup.py")
            if re.search(r'PLATFORMS|"flutter", "react-native"', open(os.path.join(HARNESS, f_), encoding="utf-8").read())],
           scenario("recreate-bold")["platforms"] == platforms()), ([], True))
    order_tmp_ = tempfile.mkdtemp(prefix="labkit-order-")
    labs_was_ = globals()["LABS"]

    def manifest_(name_, **spec_):
        os.makedirs(os.path.join(order_tmp_, "platforms", name_), exist_ok=True)
        with open(os.path.join(order_tmp_, "platforms", name_, "lab.yaml"), "w") as f_:
            yaml.safe_dump(dict({"name": name_}, **spec_), f_)

    def ordered_():
        try:
            return board_order()
        except SystemExit as e:
            return str(e)
    try:
        globals()["LABS"] = order_tmp_
        manifest_("alpha", column=2, ios={"package": "x"})
        manifest_("beta", column=1, ios={"package": "x"})
        manifest_("gamma", column=3)                       # a platform with an Android half only
        os.makedirs(os.path.join(order_tmp_, "platforms", "_shared"))     # no lab.yaml: not a platform
        fifth_ = (ordered_(), hosts("gamma"), hosts("alpha"), lab_cells(ordered_(), ["android", "ios"]),
                  lab_cells(["gamma"], ["ios"]))
        manifest_("gamma", column=3, short="gam")
        manifest_("alpha", column=2, ios={"package": "x"}, short="alpha")
        shorts_ = []
        for name_ in ("gamma", "alpha", "beta"):
            try:
                shorts_.append(board_name(name_))
            except SystemExit as e:
                shorts_.append(str(e).split(" — ")[0])
        manifest_("gamma", column=2)
        twice_ = ordered_()
        manifest_("gamma")
        none_ = ordered_()
        manifest_("gamma", column="3")
        text_ = ordered_()
    finally:
        globals()["LABS"] = labs_was_
        shutil.rmtree(order_tmp_)
    check("(WO-143) a fifth manifest is a fifth platform, in its column, with a cell only on the OS it has a host on",
          fifth_, (["beta", "alpha", "gamma"], ["android"], ["android", "ios"],
                   [("beta", "android"), ("beta", "ios"), ("alpha", "android"), ("alpha", "ios"), ("gamma", "android")],
                   []))
    check("(WO-143) a board's column names are the manifests' `short`: the four bridges' as the board spelt them, a "
          "fifth platform's its own, and one that is missing or would overrun the column refused",
          ([board_name(p_) for p_ in four_], shorts_),
          (["flt", "rn", "cap", "cdv"], ["gam", "alpha: lab.yaml names no `short`", "beta: lab.yaml names no `short`"]))
    check("(WO-143) a manifest with no `column`, one that is not a whole number, and two alike are refused by name",
          ["alpha and gamma: both name `column: 2`" in twice_, "gamma: lab.yaml names no `column`" in none_,
           "gamma: lab.yaml names no `column`" in text_], [True, True, True])

    # ---- (WO-143) the fifth platform: the typed native API, a manifest like the others
    import run as run_
    nat_ = platform("native")
    nat_ios_ = platform("native", "ios")
    check("(WO-143) native: the last column, `nat` on a board, a host on both OSes (the Swift app is stage 3's), no "
          "plugin (so no path, and a null record), the triggers a plain Activity recreates on, and on the Simulator "
          "no trigger and no tick call",
          (board_order(), board_name("native"), hosts("native"), run_.plugin_path(nat_, variant("FIX")),
           run_.plugin_source(nat_, variant("FIX")), nat_["supports"], nat_["package"], nat_ios_["supports"],
           nat_ios_["package"], run_.plugin_path(nat_ios_, variant("FIX"))),
          (four_ + ["native"], "nat", ["android", "ios"], None, None,
           {"triggers": ["bold", "night", "fontscale", "finish", "none"], "calls": ["startSchedule"]},
           "com.transistorsoft.labs.nativeapi", {"triggers": ["none"], "calls": []},
           "com.transistorsoft.labs.nativeapi", None))
    # (WO-143 stage 3) The two halves are two apps that share no source: each OS's stamp is its own app's, so a
    # Swift edit leaves the Android stamp alone, and back.  The four bridges name no `sources` and hash all of app/.
    with tempfile.TemporaryDirectory() as tmp_:
        labs_was_, run_.labkit.LABS = run_.labkit.LABS, tmp_
        try:
            for os_dir_ in ("kotlin", "swift"):
                os.makedirs(os.path.join(tmp_, "platforms", "native", "app", os_dir_))
                with open(os.path.join(tmp_, "platforms", "native", "app", os_dir_, "main"), "w") as f_:
                    f_.write("1")
            stamps_ = [(run_.source_stamp(nat_, variant("FIX")), run_.source_stamp(nat_ios_, variant("FIX")))]
            for os_dir_ in ("swift", "kotlin"):
                with open(os.path.join(tmp_, "platforms", "native", "app", os_dir_, "main"), "w") as f_:
                    f_.write("2")
                stamps_.append((run_.source_stamp(nat_, variant("FIX")), run_.source_stamp(nat_ios_, variant("FIX"))))
        finally:
            run_.labkit.LABS = labs_was_
    check("(WO-143) stamp: a platform whose manifest names `build.sources` per OS stamps each OS from its own app: "
          "a Swift edit moves the iOS stamp alone, a Kotlin edit the Android one alone; the four bridges name none",
          (stamps_[1][0] == stamps_[0][0], stamps_[1][1] != stamps_[0][1], stamps_[2][0] != stamps_[1][0],
           stamps_[2][1] == stamps_[1][1], nat_["build"]["sources"], nat_ios_["build"]["sources"],
           [p_ for p_ in four_ for o_ in ("android", "ios") if "sources" in platform(p_, o_)["build"]]),
          (True, True, True, True, ["app/kotlin"], ["app/swift"], []))
    native_applies_, native_excused_ = [], {}
    for f_ in sorted(os.listdir(os.path.join(HARNESS, "scenarios"))):
        if not f_.endswith(".yaml"):
            continue
        spec_ = scenario(f_[:-len(".yaml")])
        if applicability(spec_, nat_) is None:
            native_applies_.append(spec_["name"])
        if "native" in (spec_.get("not_applicable") or {}):
            native_excused_[spec_["name"]] = spec_["not_applicable"]["native"]
    native_ios_applies_ = [f_[:-len(".yaml")] for f_ in sorted(os.listdir(os.path.join(HARNESS, "scenarios")))
                           if f_.endswith(".yaml") and applicability(scenario(f_[:-len(".yaml")]), nat_ios_) is None]
    check("(WO-143) every scenario accounts for native: 52 apply on its Android cell and 36 on its iOS one (stage 4's "
          "api-properties-kotlin and -swift, one each), 13 excuse it in writing, each dated; and no STAGING excusal "
          "remains",
          (len(native_applies_), len(native_ios_applies_), [n_ for n_ in ("api-roundtrip", "api-roundtrip-ios",
           "api-types", "events", "token-survives-launch") if n_ not in native_ios_applies_],
           sorted(native_excused_), all(DATED.match(r_) for r_ in native_excused_.values()),
           sorted(n_ for n_, r_ in native_excused_.items() if "WO-143 stage" in r_),
           [n_ for n_ in ("api-roundtrip", "api-types", "events", "recreate-bold", "scheduler-survives-launch")
            if n_ not in native_applies_],
           [n_ for n_ in ("runtime-reset", "reset-no-args", "recreate-dark-mode") if n_ in native_applies_]),
          (52, 36, [], ["api-config-invalid", "api-config-invalid-android", "api-config-invalid-ios", "api-config-values",
                "api-config-values-android", "api-config-values-ios", "api-forbidden", "api-properties-dart",
                "api-properties-typescript", "geofences-bulk-add", "headless-android", "headless-android-causes",
                "headless-android-kill"], True, [], [], []))
    kotlin_dir_ = os.path.dirname(tc_.KOTLIN)
    api_kt_ = open(os.path.join(kotlin_dir_, "Api.kt"), encoding="utf-8").read()
    results_kt_ = open(os.path.join(kotlin_dir_, "Results.kt"), encoding="utf-8").read()
    check("(WO-143) the six mutators the types resolve `true` and Kotlin returns nothing from read as `true`, and "
          "say so in their alias (Chris, 2026-10-10); no other row does",
          sorted(re.findall(r'"(\w+)" to (?:settles|plain)\("[\w.]+\+true"', api_kt_)),
          ["addGeofence", "addGeofences", "destroyLocation", "destroyLocations", "removeGeofence", "removeGeofences"])
    code_ = {f_: re.sub(r"(?m)^\s*(//|\*|/\*).*$", "", open(os.path.join(kotlin_dir_, f_), encoding="utf-8").read())
             for f_ in sorted(os.listdir(kotlin_dir_)) if f_.endswith(".kt")}
    check("(WO-143) the Kotlin driver takes neither shortcut the work order rejects: no file of it calls the core's "
          "config map or a serialiser (toMap, toJson, toDictionary), outside its comments, and only the probe names "
          "the core's TSConfig, by reflection as the four bridge labs' does; config goes through TypedConfig.bind and "
          "State is read through TypedConfig.read",
          ([f_ for f_, text_ in code_.items()
            if re.search(r"\.toMap\(\)|\.toJson\(\)|\.toDictionary\(\)|updateWithJSONObject", text_)],
           [f_ for f_, text_ in code_.items() if "TSConfig" in text_],
           "TypedConfig.bind(path, value)" in api_kt_, "TypedConfig.read(bgGeo.config, path)" in results_kt_),
          ([], ["Probe.kt"], True, True))

    # (WO-143 stage 3) The Swift driver, held as the Kotlin one is.
    swift_dir_ = os.path.dirname(tc_.SWIFT)
    swift_ = {f_: re.sub(r"(?m)^\s*//.*$", "", open(os.path.join(swift_dir_, f_), encoding="utf-8").read())
              for f_ in sorted(os.listdir(swift_dir_)) if f_.endswith(".swift")}
    check("(WO-143) the Swift driver takes neither shortcut the work order rejects: no file of it calls the core's "
          "config dictionary or a serialiser (toDictionary, toJson), or reads an event's `data`, outside its "
          "comments; only the probe imports the core's module, as the four bridge labs' shims do; config goes "
          "through TypedConfig.bind and State is read through TypedConfig.read",
          ([f_ for f_, text_ in swift_.items()
            if re.search(r"\.toDictionary\(\)|\.toJson\(\)|updateWithDictionary|\.data\b(?!\()", text_)],
           [f_ for f_, text_ in swift_.items() if re.search(r"TSConfig|import TSLocationManager", text_)],
           "TypedConfig.bind(path, value)" in swift_["Api.swift"],
           "TypedConfig.read(bgGeo.config, path)" in swift_["Results.swift"]),
          ([], ["Probe.swift"], True, True))
    check("(WO-143) the mutators the types resolve `true` and Swift returns nothing from read as `true` and say so "
          "in their alias, the same six as Kotlin's (Chris's ruling of 2026-10-10, carried over); no other row does",
          sorted(re.findall(r'"(\w+)": (?:settles|plain)\("[\w.]+\+true"', swift_["Api.swift"])),
          ["addGeofence", "addGeofences", "destroyLocation", "destroyLocations", "removeGeofence", "removeGeofences"])
    check("(WO-143) every converter the Swift calls is one the driver's Wire.swift declares",
          sorted(set(re.findall(r"Wire\.(\w+)\(path, v\)", swift_["TypedConfig.g.swift"]))
                 - set(re.findall(r"static func (\w+)\(_ path: String, _ v: Any\?\)", swift_["Wire.swift"]))), [])
    # (WO-143 stage 0) The Swift API beside a label is its own question: a label published before the copy existed
    # is still published for the four bridge apps.
    import lab_setup as setup_
    with tempfile.TemporaryDirectory() as tmp_:
        labs_was_ = setup_.LABS
        try:
            setup_.LABS = tmp_
            root_ = os.path.join(tmp_, "xcframeworks", "L")
            os.makedirs(os.path.join(root_, "TSLocationManager.xcframework"))
            os.makedirs(os.path.join(tmp_, "xcframeworks", ".provenance"))
            open(os.path.join(tmp_, "xcframeworks", ".provenance", "L.txt"), "w").close()
            before_ = (setup_.published("L", "ios"), setup_.swift_api_published("L"))
            os.makedirs(os.path.join(root_, "Sources", "BackgroundGeolocation", "SwiftInterface"))
            for rel_ in ("Package.swift", os.path.join("Sources", "BackgroundGeolocation", "SwiftInterface", "BGGeo.swift")):
                open(os.path.join(root_, rel_), "w").close()
            after_ = (setup_.published("L", "ios"), setup_.swift_api_published("L"))
        finally:
            setup_.LABS = labs_was_
    publish_ = open(os.path.join(HARNESS, "publish_native_ios.sh"), encoding="utf-8").read()
    check("(WO-143) stage 0: a label with no Swift API beside it is published for the bridges and not for the "
          "Swift app; publish_native_ios.sh copies it on a publish and can add it to a published label, only from "
          "the commit the binary was recorded at; setup adds it without a rebuild",
          (before_, after_, publish_.count('publish_swift_api "$OUT" "$NATIVE"'), '[ "$want" = "$got" ]' in publish_,
           '"--swift-api", label' in open(os.path.join(HARNESS, "lab_setup.py"), encoding="utf-8").read()),
          ((True, False), (True, True), 2, True, True))

    # ---- WO-191 Part 5: the native descriptor (labkit.NATIVE), read by lab_setup, publish_native.sh and inventory.
    # lab_setup and inventory import run.py's `labkit`, not this __main__: its NATIVE is the one patched here.
    geo_native_ = {"android": {"submodule": "native/android", "build_root": "BackgroundGeolocation",
                               "gradle_task": ":tslocationmanager:publishToMavenLocal",
                               "version_property": "TS_LOCATIONMANAGER_VERSION_NAME",
                               "artifacts": ("tslocationmanager", "tslocationmanager-gms20"), "build": None},
                   "ios": {"artifact": "TSLocationManager.xcframework", "build": None}}
    fetch_android_ = {"submodule": "native/background-fetch", "build_root": "android",
                      "gradle_task": ":tsbackgroundfetch:publishToMavenLocal",
                      "version_property": "TS_BACKGROUND_FETCH_VERSION_NAME", "artifacts": ("tsbackgroundfetch",)}
    import inventory
    check("(WO-191, Part 5) labkit.NATIVE defaults to background-geolocation's native SDK, in both labkit modules; "
          "inventory's ANDROID_ARTIFACTS is its artifacts",
          (NATIVE, run.labkit.NATIVE, inventory.ANDROID_ARTIFACTS), (geo_native_, geo_native_, geo_native_["android"]["artifacts"]))
    native_was_ = json.loads(json.dumps({o_: {k_: v_ for k_, v_ in d_.items() if k_ != "build"}
                                         for o_, d_ in run.labkit.NATIVE.items()}))
    sh_was_ = (setup_.LABS, setup_.sh, setup_.native_checkout, setup_.gradle_defines, setup_.published, setup_.HARNESS,
               setup_.swift_api_published, setup_.native_slot_android)
    tmp_ = tempfile.mkdtemp(prefix="labkit-native-")
    wt_, slots_, cmds_ = os.path.join(tmp_, "wt"), [], []
    try:
        # Every command a slot would run is recorded, never run: a slot reached by mistake (its own instead of the
        # product's) shows in cmds_, and no git or Gradle starts.
        setup_.sh = lambda cmd, cwd=None, env=None, log=None: cmds_.append([os.path.basename(cmd[0])] + cmd[1:])
        setup_.native_checkout = lambda sub, sha, name: (cmds_.append(["checkout", sub, name]), wt_)[1]
        setup_.gradle_defines = lambda key: True
        # published(): the descriptor's artifact names, under labs/m2 and labs/xcframeworks
        setup_.LABS = tmp_
        for rel_ in (os.path.join("m2", "com", "transistorsoft", "tsbackgroundfetch", "L"),
                     os.path.join("xcframeworks", "L", "TSBackgroundFetch.xcframework")):
            os.makedirs(os.path.join(tmp_, rel_))
        for rel_ in (os.path.join("m2", ".provenance"), os.path.join("xcframeworks", ".provenance")):
            os.makedirs(os.path.join(tmp_, rel_), exist_ok=True)
            open(os.path.join(tmp_, rel_, "L.txt"), "w").close()
        as_geo_ = (setup_.published("L", "android"), setup_.published("L", "ios"))
        run.labkit.NATIVE["android"].update(fetch_android_)
        run.labkit.NATIVE["ios"]["artifact"] = "TSBackgroundFetch.xcframework"
        as_fetch_ = (setup_.published("L", "android"), setup_.published("L", "ios"))
        # ensure_native: the shared lines, then the OS's slot (the product's here), published or not
        for o_ in ("android", "ios"):
            run.labkit.NATIVE[o_]["build"] = lambda var, done, logs, setup, o_=o_: slots_.append(
                (o_, var["native"], done, logs, setup is setup_))
        setup_.published = lambda label, o: o == "android"
        out_ = io.StringIO()
        with contextlib.redirect_stdout(out_):
            setup_.ensure_native({"name": "V", "native": "L", "native_source": {"ios": "b" * 40}}, ["android", "ios"],
                                 "/logs")
            refusal_ = refused(lambda: setup_.ensure_native({"name": "V", "native": "L", "native_source": {}}, ["ios"],
                                                            "/logs"))
        # with no product slot, this file's own, looked up by name at the call: a rebind of native_slot_android takes
        for o_ in ("android", "ios"):
            run.labkit.NATIVE[o_]["build"] = None
        setup_.native_slot_android = lambda var, done, logs, setup=None: slots_.append(("rebound", done))
        with contextlib.redirect_stdout(io.StringIO()):
            setup_.ensure_native({"name": "V", "native": "L", "native_source": {}}, ["android"], "/logs")
        setup_.native_slot_android = sh_was_[7]
        # the default Android slot: publish_native.sh handed the descriptor's root, task, property and artifact; and,
        # where publish_native.sh is not (fetch-forge vendors none), a refusal before any checkout
        setup_.HARNESS = tmp_
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                setup_.native_slot_android({"name": "V", "native": "L", "native_source": {"android": "a" * 40}}, False,
                                           "/logs")
            missing_ = "ran"
        except (Exception, SystemExit) as e:   # a FAIL below, not a crash here
            missing_ = str(e)
        setup_.HARNESS = sh_was_[5]
        os.makedirs(os.path.join(wt_, "android"))
        for done_ in (False, True):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    setup_.native_slot_android({"name": "V", "native": "L", "native_source": {"android": "a" * 40}},
                                               done_, "/logs")
            except (Exception, SystemExit) as e:
                cmds_.append(["raised", type(e).__name__])
        props_ = os.path.exists(os.path.join(wt_, "android", "local.properties"))
    finally:
        for o_, d_ in native_was_.items():
            run.labkit.NATIVE[o_].clear()
            run.labkit.NATIVE[o_].update(d_, build=None)
            if "artifacts" in d_:
                run.labkit.NATIVE[o_]["artifacts"] = tuple(d_["artifacts"])
        (setup_.LABS, setup_.sh, setup_.native_checkout, setup_.gradle_defines, setup_.published, setup_.HARNESS,
         setup_.swift_api_published, setup_.native_slot_android) = sh_was_
        while tmp_ in sys.path:              # native() put the patched HARNESS on sys.path
            sys.path.remove(tmp_)
        shutil.rmtree(tmp_)
    check("(WO-191, Part 5) lab_setup reads the descriptor: published() looks for its artifact names; ensure_native "
          "prints a published label, refuses an unpublished one with no native_source before any slot, and hands each "
          "OS's slot (the product's, else its own, by name) the variant, whether it is published, the log dir and the "
          "running lab_setup; the default Android slot refuses where publish_native.sh is not, before any checkout, and "
          "hands it the descriptor's build root, task, property and artifact, nothing for a published label; and the "
          "descriptor is put back",
          (as_geo_, as_fetch_, slots_, "ok: android L is published" in out_.getvalue(),
           refusal_.startswith("setup stopped: L is not published for ios"),
           missing_.startswith("setup stopped: %s is not here" % os.path.join(tmp_, "publish_native.sh")), cmds_, props_,
           run.labkit.NATIVE),
          ((False, False), (True, True), [("android", "L", True, "/logs", True), ("ios", "L", False, "/logs", True),
                                          ("rebound", True)], True, True, True,
           [["checkout", "native/background-fetch", "native-android"],
            ["publish_native.sh", "L", wt_, "android", ":tsbackgroundfetch:publishToMavenLocal",
             "TS_BACKGROUND_FETCH_VERSION_NAME", "tsbackgroundfetch"]], True, geo_native_))

    # The default iOS slot, its code before Part 5: driven with the same recording fakes.
    ios_cmds_, wt_ios_ = [], "/wt"
    was_ = (setup_.sh, setup_.native_checkout, setup_.swift_api_published)
    try:
        setup_.sh = lambda cmd, cwd=None, env=None, log=None: ios_cmds_.append(
            [os.path.basename(cmd[0])] + cmd[1:] + ([os.path.basename(log)] if log else []))
        setup_.native_checkout = lambda sub, sha, name: (ios_cmds_.append(["checkout", sub, sha[:1], name]), wt_ios_)[1]
        runs_ = []
        for done_, swift_, src_ in ((True, False, {"ios": "b" * 40}), (True, True, {"ios": "b" * 40}),
                                    (True, False, {}),
                                    (False, False, {"ios": "b" * 40, "ios_version": "4.2.0", "ios_build": 4200}),
                                    (False, False, {"ios": "b" * 40})):
            setup_.swift_api_published = lambda label, s_=swift_: s_
            del ios_cmds_[:]
            with contextlib.redirect_stdout(io.StringIO()):
                setup_.native_slot_ios({"name": "V", "native": "L", "native_source": src_}, done_, "/logs")
            runs_.append(list(ios_cmds_))
    finally:
        setup_.sh, setup_.native_checkout, setup_.swift_api_published = was_
    xcfw_ = os.path.join(wt_ios_, "build", "Lab", "TSLocationManager_XCFramework_L")
    check("(WO-191, Part 5) the default iOS slot is its code before Part 5: a published label without its Swift API gets "
          "it added from its native_source, with one or with no native_source nothing; an unpublished one is built (its "
          "ios_version and ios_build passed on) and published",
          runs_,
          [[["checkout", "native/ios", "b", "native-ios"], ["publish_native_ios.sh", "--swift-api", "L", wt_ios_]],
           [], [],
           [["checkout", "native/ios", "b", "native-ios"],
            ["build_native_ios.sh", "L", wt_ios_, "4.2.0", "4200", "native-ios.log"],
            ["publish_native_ios.sh", "L", xcfw_, wt_ios_]],
           [["checkout", "native/ios", "b", "native-ios"], ["build_native_ios.sh", "L", wt_ios_, "native-ios.log"],
            ["publish_native_ios.sh", "L", xcfw_, wt_ios_]]])

    # publish_native.sh, run: a copy in a temp lab tree, against a stub gradlew that records what it was asked.
    def published_by(*args):
        root_ = tempfile.mkdtemp(prefix="labkit-publish-")
        try:
            os.makedirs(os.path.join(root_, "labs", "harness"))
            for f_ in ("publish_native.sh", "lab-publish.init.gradle"):
                shutil.copy2(os.path.join(HARNESS, f_), os.path.join(root_, "labs", "harness"))
            gradle_root_ = os.path.join(root_, "native", args[2] if len(args) > 2 else "BackgroundGeolocation")
            os.makedirs(gradle_root_)
            with open(os.path.join(gradle_root_, "gradlew"), "w") as f:
                f.write('#!/bin/sh\nprintf "%s\\n" "$@" > "$(dirname "$0")/asked.txt"\n')
            os.chmod(os.path.join(gradle_root_, "gradlew"), 0o755)
            r = sp.run(["bash", os.path.join(root_, "labs", "harness", "publish_native.sh"), args[0],
                        os.path.join(root_, "native")] + list(args[2:]), capture_output=True, text=True, timeout=60,
                       stdin=sp.DEVNULL)
            asked_ = open(os.path.join(gradle_root_, "asked.txt")).read().split("\n") if r.returncode == 0 else r.stderr
            return (r.returncode, [a_ for a_ in asked_ if a_.startswith((":", "-P"))],
                    r.stdout.strip().splitlines()[-1].split(" -> ")[0] if r.returncode == 0 else "",
                    os.path.exists(os.path.join(root_, "labs", "m2", ".provenance", args[0] + ".txt")))
        finally:
            shutil.rmtree(root_, ignore_errors=True)
    check("(WO-191, Part 5) publish_native.sh: its two positional arguments publish tslocationmanager as before; four "
          "more name another product's build root, task, version property and artifact",
          [published_by("4.7.9-lab", None), published_by("4.1.1-lab", None, "android",
                                                          ":tsbackgroundfetch:publishToMavenLocal",
                                                          "TS_BACKGROUND_FETCH_VERSION_NAME", "tsbackgroundfetch")],
          [(0, [":tslocationmanager:publishToMavenLocal", "-PTS_LOCATIONMANAGER_VERSION_NAME=4.7.9-lab"],
            "published com.transistorsoft:tslocationmanager:4.7.9-lab", True),
           (0, [":tsbackgroundfetch:publishToMavenLocal", "-PTS_BACKGROUND_FETCH_VERSION_NAME=4.1.1-lab"],
            "published com.transistorsoft:tsbackgroundfetch:4.1.1-lab", True)])

    # ---- WO-027: what loaded the machine ----
    ps_ = ("  812 612.5 00:41 /usr/bin/java\n"
           "35272 301,0 02-13:44:48 /Users/x/Library/Android/sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64\n"
           "   77  95.0 03:10 /Users/x/Library/Developer/CoreSimulator/Devices/9A1B2C3D-0000-0000-0000-000000000000"
           "/data/Containers/Bundle/Application/AB/Runner.app/Runner\n"
           "garbage\n"
           "  683   3.0 09-15:48:36 /Applications/Android Studio.app/Contents/MacOS/studio\n"
           "  999   x.y 00:01 /bin/sh\n"
           "    1   0.1 09-15:48:47 /sbin/launchd\n")
    busy_ = sweep_.busiest(ps_, top=4)
    check("(WO-027) busiest: ps's rows in ps's order, a path with spaces whole, a comma decimal read, a malformed row "
          "skipped, and no more than `top`",
          [(b_["pid"], b_["pcpu"], b_["etime"], sweep_.busy_name(b_["comm"])) for b_ in busy_],
          [(812, 612.5, "00:41", "java"), (35272, 301.0, "02-13:44:48", "qemu-system-aarch64"),
           (77, 95.0, "03:10", "9A1B2C3D/Runner"), (683, 3.0, "09-15:48:36", "studio")])
    asked_ = []
    check("(WO-027) a run's busiest processes are sampled only above LOAD_SUSPECT (40): at it, ps is not asked; and "
          "a sampler that raises costs the list, not the run",
          (sweep_.LOAD_SUSPECT,
           sweep_.sample_load(lambda: (40.0, 1.0, 1.0), lambda: asked_.append(1) or ["x"]),
           sweep_.sample_load(lambda: (40.1, 1.0, 1.0), lambda: asked_.append(1) or ["x"]), len(asked_),
           sweep_.sample_load(lambda: (99.0, 1.0, 1.0), lambda: [][0])),
          (40, (40.0, None), (40.1, ["x"]), 1, (99.0, [])))
    check("(WO-027) the LOAD line under a loaded run's RESULT: the device, the load, the five busiest by name; and "
          "an empty sample says so",
          (sweep_.load_note("ios", "0402E685", 575.04, busy_ + busy_),
           sweep_.load_note("android", "emulator-5582", 41, [])),
          ("LOAD    ios     0402E685      load=575.0 busiest: java 612%, qemu-system-aarch64 301%, "
           "9A1B2C3D/Runner 95%, studio 3%, java 612%",
           "LOAD    android emulator-5582 load=41.0 busiest: ps gave nothing"))
    runs_ = [{"status": "n/a"}, {"status": "ERROR", "detail": "no device left"},
             {"os": "ios", "plat": "cordova", "scenario": "events", "load": 12.5, "end": 0},
             {"os": "android", "plat": "flutter", "scenario": "api-smoke", "load": 28.25, "end": 0}]
    check("(WO-027) the board's load line on a quiet board: the highest load a run ended at, and nothing more; a "
          "board with no run says nothing",
          (sweep_.load_lines(runs_), sweep_.load_lines(runs_[:2])), (["load: at most 28.2"], []))
    runs_ += [{"os": "ios", "plat": "cordova", "scenario": "hello-world", "load": 575.0, "end": 0, "busy": busy_},
              {"os": "android", "plat": "capacitor", "scenario": "api-config", "load": 60.0, "end": 0,
               "busy": busy_[:2] + [dict(busy_[1], pid=35271, pcpu=120.0)]},
              {"os": "ios", "plat": "flutter", "scenario": "events", "load": 41.0, "end": 0}]
    loud_ = sweep_.load_lines(runs_)
    check("(WO-027) a loaded board: how many runs ended above 40, the peak's run, and the processes on top across "
          "them by the CPU they held: each with the runs it was seen in (once a run, however many processes share the "
          "name) and its highest figure; a run from before the record, with no `busy`, still counts",
          (len(loud_), loud_[0].split(" at ")[0], loud_[0].split(" (")[1], loud_[1]),
          (2, "load: 3 of 5 runs ended above 40; peak 575.0", "ios cordova hello-world)",
           "  busiest then: java in 2 (to 612%), qemu-system-aarch64 in 2 (to 301%), 9A1B2C3D/Runner in 1 (to 95%), "
           "studio in 1 (to 3%)"))

    # ------------------------------------------------------------ (WO-027, PREFLIGHT) the host's memory, the emulators' pace
    # The lost sweep's own figures (20261005-113336): 1,882,756 pages of 16 KiB in the compressor, 6,201 MB of swap.
    sysctl_ = ("kern.memorystatus_vm_pressure_level: %d\nhw.memsize: 68719476736\n"
               "vm.swapusage: total = 7168.00M  used = 6201.00M  free = 967.00M  (encrypted)\n")
    vmstat_ = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:                    7625.\n"
               "Pages occupied by compressor:                1882756.\n")
    lost_ = sweep_.host_memory(sysctl_ % 4, vmstat_)
    blank_ = {"pressure": None, "compressed_gb": None, "ram_gb": None, "swap_used_mb": None}
    check("(PREFLIGHT) host_memory: macOS's pressure level, the memory the compressor occupies, RAM and swap used; a "
          "level macOS does not define, or nothing readable at all, is None and never a guess",
          (lost_, sweep_.host_memory("", ""), sweep_.host_memory(sysctl_ % 3, "")["pressure"]),
          ({"pressure": 4, "compressed_gb": 28.7, "ram_gb": 64.0, "swap_used_mb": 6201}, blank_, None))
    check("(PREFLIGHT) the MEMORY line: the kernel's verdict first, then the figures; what was not read is said so",
          (sweep_.memory_line(lost_), sweep_.memory_line(dict(lost_, pressure=2)), sweep_.memory_line(blank_)),
          ("MEMORY  pressure critical, 28.7 of 64 GiB compressed (45%), swap 6201 MB used",
           "MEMORY  pressure warning, 28.7 of 64 GiB compressed (45%), swap 6201 MB used", "MEMORY  pressure not read"))
    started_ = ("Starting: Intent { cmp=com.android.settings/.Settings }\nStatus: ok\nLaunchState: COLD\n"
                "Activity: com.android.settings/.Settings\nTotalTime: 212\nWaitTime: 215\nComplete\n")
    check("(PREFLIGHT) launch_probe reads ActivityManager's TotalTime; a launch that reported none is None",
          (sweep_.launch_probe("emulator-5580", started_), sweep_.launch_probe("emulator-5580", ""),
           sweep_.launch_probe("emulator-5580", "Error type 3\nError: Activity class does not exist.\n")), (212, None, None))
    def pre_(level, rest_=None, **ms):
        return sweep_.preflight([k.replace("_", "-") for k in ms], lambda: dict(lost_, pressure=level),
                                lambda serial: ms[serial.replace("-", "_")], lambda serial: (rest_ or {}).get(serial))
    two_ = {"emulator_5580": 250, "emulator_5582": 260}
    check("(PREFLIGHT) refused: macOS's critical pressure; an emulator slower than 3 s to launch Settings, named with "
          "its time; one that never launched.  Not refused: normal; macOS's warning (the lab ran at it, launches of "
          "0.8 and 1.4 s, 2026-10-05); a level that could not be read; a sweep with no Android device",
          [pre_(1, **two_)[2], pre_(2, emulator_5580=813, emulator_5582=1399)[2], pre_(None, **two_)[2], pre_(1)[2],
           pre_(4, **two_)[2], pre_(1, emulator_5580=250, emulator_5582=12400)[2], pre_(1, emulator_5580=None)[2],
           pre_(1, emulator_5580=3000)[2], pre_(1, emulator_5580=3001)[2] is not None],
          [None, None, None, None, "macOS reports critical memory pressure",
           "emulator-5582 took 12.4 s to launch Settings (a healthy Lab emulator takes 0.2 to 0.3 s)",
           "emulator-5580 did not launch Settings in 30 s", None, True])
    check("(PREFLIGHT) the lines a sweep prints: MEMORY, then one PROBE per Android emulator; the reading kept for "
          "board.json is the memory and each launch time",
          (pre_(2, emulator_5580=813, emulator_5582=None)[1], pre_(1)[1], pre_(1, emulator_5580=250)[0]),
          (["MEMORY  pressure warning, 28.7 of 64 GiB compressed (45%), swap 6201 MB used",
            "PROBE   android emulator-5580 Settings launched in 813 ms",
            "PROBE   android emulator-5582 Settings did not launch in 30 s"],
           ["MEMORY  pressure normal, 28.7 of 64 GiB compressed (45%), swap 6201 MB used"],
           {"memory": dict(lost_, pressure=1), "probes": {"emulator-5580": 250}, "priority": {"emulator-5580": None}}))
    top_ = ("Processes: 812 total\n\nCOMMAND          MEM  \nqemu-system-aarc 13G  \nstudio           3868M\n"
            "Google Chrome He 1111M+\njava             1636M-\n")
    holders_ = sweep_.memory_holders(top_)
    said_ = sweep_.refusal("macOS reports critical memory pressure", " after its builds", lambda: holders_)
    check("(PREFLIGHT) a refusal names why, who holds the most memory (top's names and figures, a name with spaces "
          "whole), what to do, and the switch that runs anyway; with nothing from top it still says the rest",
          (holders_, said_.splitlines()[0], said_.splitlines()[1], "`./labkit down`" in said_ and "--no-preflight" in said_,
           len(sweep_.refusal("x", "", lambda: "").splitlines())),
          ("qemu-system-aarc 13G, studio 3868M, Google Chrome He 1111M, java 1636M",
           "sweep not started after its builds: macOS reports critical memory pressure.",
           "Holding the most memory: qemu-system-aarc 13G, studio 3868M, Google Chrome He 1111M, java 1636M.", True, 2))
    check("(PREFLIGHT) the board's last line on memory: the highest pressure a run ended at, and how many ended above "
          "normal; a board from before the record says nothing",
          (sweep_.memory_lines([{"pressure": 1}, {"pressure": 1}, {}]),
           sweep_.memory_lines([{"pressure": 1}, {"pressure": 2}, {"pressure": 4}, {"load": 3.0}]), sweep_.memory_lines(runs_)),
          (["memory: pressure at most normal"], ["memory: pressure at most critical; 2 of 3 runs ended above normal"], []))
    main_ = inspect.getsource(sweep_.main)
    check("(PREFLIGHT) main(): the preflight runs before the sweep's record is made (a refused sweep leaves nothing), "
          "and again after the builds only when one ran; each refuses only while --no-preflight is not given, and an "
          "ignored one is said; each run's record carries the reading",
          (main_.index('preflight(devices.get("android", []))') < main_.index("os.makedirs(log_dir)"),
           main_.count('preflight(devices.get("android", []))'), main_.count("if why and args.preflight:"),
           'if any(b.get("status") != "current" for b in _board["builds"]):' in main_,
           main_.count('say("PREFLIGHT ignored (--no-preflight): %s" % why)'),
           '**{k: mem[k] for k in ("pressure", "compressed_gb") if mem.get(k) is not None}' in inspect.getsource(sweep_.lane)),
          (True, 2, 2, True, 2, True))

    # ------------------------------------------------------------ (WO-027, PRIORITY) an emulator in the background
    qemu_ = "/Users/chris/Library/Android/sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64"
    ps_ = ("  611 /System/Library/CoreServices/Finder.app/Contents/MacOS/Finder\n"
           "43718 %s -avd Lab1 -port 5580 -no-snapshot\n43719 %s -port 5582 -avd Lab2\n"
           "50001 /usr/bin/python3 server.py -port 5584\n50002 %s -avd Big -port 55860\n" % (qemu_, qemu_, qemu_))
    check("(PRIORITY) emulator_pid: the qemu process started with that serial's `-port`, wherever the switch stands; "
          "not another process with the same port (5584), not a port that merely begins with it (5586 against "
          "55860), and nothing for a serial that is not an emulator's, a device over the network at that port included",
          [sweep_.emulator_pid(s_, ps_) for s_ in ("emulator-5580", "emulator-5582", "emulator-5584", "emulator-5586",
                                                  "R3CY908SNSW", "0402E685", "192.168.1.20:5582")],
          [43718, 43719, None, None, None, None, None])
    def threads_(*pris):
        return ("USER    PID   TT   %%CPU STAT PRI     STIME     UTIME COMMAND\n"
                "chris 43718   ??    0.0 S    %s   1:27.19   1:09.31 /Users/chris/Library/Android/sdk/emulator/qemu\n"
                % pris[0] + "".join("      43718         2.1 %s %5s   0:00.00 123:45.67 \n" % (st_, p_)
                                    for st_, p_ in zip(("S", "R", "Ss", "U", "S+") * 40, pris[1:])))
    rest_r = sweep_.emulator_priority("emulator-5580", ps_, threads_(*["4T"] * 127))
    work_r = sweep_.emulator_priority("emulator-5580", ps_, threads_("46T", "4T", "4T", "19T", "20T", *["31T"] * 120))
    check("(PRIORITY) emulator_priority: one priority a thread from `ps -M`, the main thread's line and the others', "
          "whatever the state and the policy's letter; the count and how many are at 4 or below.  None when the "
          "process is not found, or ps said nothing of its threads",
          (rest_r, work_r, sweep_.emulator_priority("emulator-5586", ps_, threads_("31T")),
           sweep_.emulator_priority("emulator-5580", ps_, ""),
           sweep_.emulator_priority("emulator-5580", ps_, threads_("31T").splitlines()[0] + "\n")),
          ({"pid": 43718, "threads": 127, "background": 127}, {"pid": 43718, "threads": 125, "background": 2},
           None, None, None))
    check("(PRIORITY) in the background is most of its threads in that band: all 127 of a resting emulator's "
          "(2026-10-07), not the two of a working one's, not exactly half, and never an emulator that was not read",
          [sweep_.resting(r_) for r_ in (rest_r, work_r, {"pid": 1, "threads": 4, "background": 2},
                                         {"pid": 1, "threads": 4, "background": 3}, None)],
          [True, False, False, True, False])
    napped_ = pre_(1, {"emulator-5580": rest_r, "emulator-5582": work_r}, emulator_5580=806, emulator_5582=198)
    check("(PRIORITY) the preflight refuses an emulator in the background, whose Settings probe passed (0.8 s), "
          "naming the cure, and prints its PRIORITY line after the probes; a working one, and one whose priority "
          "could not be read, are neither refused nor mentioned; the reading is kept for board.json",
          (napped_[2], napped_[1][1:], napped_[0]["priority"],
           pre_(1, {"emulator-5580": work_r}, emulator_5580=250)[1:], pre_(1, emulator_5580=250)[2]),
          ("macOS runs emulator-5580 in the background, where it launches apps at a third of its pace and starves on "
           "a busy host: its window is hidden, minimized or covered.  Bring it into view, or restart it (`./labkit "
           "down`, `./labkit up`): `up` opts the emulator out of that",
           ["PROBE   android emulator-5580 Settings launched in 806 ms",
            "PROBE   android emulator-5582 Settings launched in 198 ms",
            "PRIORITY android emulator-5580 in the background: 127 of 127 threads at priority 4 or below"],
           {"emulator-5580": rest_r, "emulator-5582": work_r},
           (["MEMORY  pressure normal, 28.7 of 64 GiB compressed (45%), swap 6201 MB used",
             "PROBE   android emulator-5580 Settings launched in 250 ms"], None), None))
    import lab_devices as nap_ld_
    import types as nap_types_
    def nap_(read, write_rc=0, raises=None, platform="darwin"):
        sent_ = []
        def call_(argv):
            sent_.append(argv)
            if raises and argv[1] == "write":
                raise raises
            return nap_types_.SimpleNamespace(stdout=read if argv[1] == "read" else "", returncode=write_rc,
                                              stderr="" if write_rc == 0 else "Could not write domain")
        return nap_ld_.keep_awake(call_, platform), sent_
    read_, write_ = (["defaults", "read", "qemu-system-aarch64", "NSAppSleepDisabled"],
                     ["defaults", "write", "qemu-system-aarch64", "NSAppSleepDisabled", "-bool", "YES"])
    up_ = inspect.getsource(nap_ld_.up_android)
    check("(PRIORITY) `up` opts the emulator out of App Nap before it starts one: nothing is written or said where "
          "the preference is set already, or off macOS; where it is missing it is written and said; a write that "
          "fails, or a `defaults` that cannot run, is said and does not stop the start",
          (nap_("1\n"), nap_(""), nap_("0\n")[0], nap_("", write_rc=1)[0], nap_("", raises=OSError("no defaults"))[0],
           nap_("", platform="linux"), up_.index("note += keep_awake()") < up_.index("subprocess.Popen(argv")),
          (("", [read_]), (", App Nap opt-out set (`defaults write qemu-system-aarch64 NSAppSleepDisabled`)",
                           [read_, write_]),
           ", App Nap opt-out set (`defaults write qemu-system-aarch64 NSAppSleepDisabled`)",
           ", App Nap opt-out NOT set (Could not write domain)", ", App Nap opt-out NOT set (no defaults)",
           ("", []), True))
    lane_ = inspect.getsource(sweep_.lane)
    check("(PRIORITY) each Android run reads the priorities as it ends: a PRIORITY line follows a run whose emulator "
          "was in the background and board.json marks it; the board's last lines count such runs by emulator, and "
          "say nothing on a board without one",
          ('rest = emulator_priority(serial) if os_name == "android" else None' in lane_,
           "        if resting(rest):" in lane_ and "            say(priority_line(serial, rest))" in lane_,
           '**({"background": [rest["background"], rest["threads"]]} if resting(rest) else {}),' in lane_,
           "for line in priority_lines(_board.get(\"runs\") or []):" in inspect.getsource(sweep_.board),
           sweep_.priority_lines([{"serial": "emulator-5580", "background": [127, 127]}, {"serial": "emulator-5582"},
                                  {"serial": "emulator-5580", "background": [126, 127]},
                                  {"serial": "emulator-5582", "background": [128, 128]}, {"status": "n/a"}]),
           sweep_.priority_lines([{"serial": "emulator-5580", "background": [127, 127]}]),
           sweep_.priority_lines(runs_)),
          (True, True, True, True,
           ["priority: 3 runs ended with the emulator in the background (emulator-5580 2, emulator-5582 1): its window "
            "was hidden or covered"],
           ["priority: 1 run ended with the emulator in the background (emulator-5580 1): its window was hidden or "
            "covered"], []))

    # ------------------------------------------------------------ (WO-027, Play Store) off, and Play services said
    import lab_devices as ps_ld_
    def store_(disabled, enabled, after=None, raises=None, versions=""):
        """A fake adb: what `pm list packages -d` and `-e` answer before the disable, and -d after it."""
        sent_, state_ = [], {"d": disabled}
        def adb_(serial, *args, **kw):
            sent_.append(args[1:])
            if args[1:4] == ("pm", "disable-user", "--user"):
                if raises:
                    raise SystemExit(raises)
                state_["d"] = disabled if after is None else after
                return "Package com.android.vending new state: disabled-user\n"
            if "--show-versioncode" in args:
                return versions
            return state_["d"] if "-d" in args else "" if state_["d"] == after and after else enabled
        return adb_, sent_
    on_, off_ = "package:com.android.vending\n", "package:com.android.vending\n"
    look_ = lambda adb_: ps_ld_.play_store("emulator-5580", adb_[0])
    check("(Play Store) play_store: off when the package is listed as disabled, on when as enabled, and nothing "
          "where the image has none; another package whose name merely contains it does not count",
          [look_(store_(off_, "")), look_(store_("", on_)), look_(store_("", "")),
           look_(store_("package:com.android.vending.extra\n", "")),
           look_(store_("", "package:x.com.android.vending\n"))],
          ["off", "on", None, None, None])
    did_ = store_("", on_, after=off_)
    kept_ps = store_(off_, "")
    stuck_ps = store_("", on_, after="")
    check("(Play Store) `up` turns it off where it is on, with `pm disable-user --user 0`, and says so; where it is "
          "off already, or the image has none, it sends nothing more than the look and says nothing; a disable that "
          "did not take, or an adb that gave up, is said and does not fail the `up`",
          (ps_ld_.store_off("emulator-5580", did_[0]),
           ("pm", "disable-user", "--user", "0", "com.android.vending") in did_[1],
           ps_ld_.store_off("emulator-5580", kept_ps[0]), any("disable-user" in a for a in kept_ps[1]),
           ps_ld_.store_off("emulator-5580", store_("", "")[0]),
           ps_ld_.store_off("emulator-5580", stuck_ps[0]),
           ps_ld_.store_off("emulator-5580", store_("", on_, raises="adb shell timed out after 30 s")[0])),
          (", Play Store turned off", True, "", False, "",
           ", Play Store NOT turned off (Package com.android.vending new state: disabled-user)",
           ", Play Store NOT turned off (adb shell timed out after 30 s)"))
    vers_ = ("package:com.google.android.gms.supervision versionCode:1\n"
             "package:com.google.android.gms versionCode:263635035\n")
    up_ps = inspect.getsource(ps_ld_.up_android)
    st_ps = inspect.getsource(ps_ld_.status_android)
    check("(Play Store) play_services reads Play services' own versionCode, not a package named after it; `up` adds "
          "the Store's line once the emulator is ready, and `status` says the version and the Store's state, ON in "
          "capitals while it is on",
          (ps_ld_.play_services("emulator-5580", store_("", "", versions=vers_)[0]),
           ps_ld_.play_services("emulator-5580", store_("", "", versions=vers_.splitlines()[0] + "\n")[0]),
           'note += record_network(name, v["serial"]) + store_off(v["serial"])' in up_ps,
           '"" if gms is None else ", Play services %d" % gms,' in st_ps,
           '"" if store is None else ", Play Store %s" % ("off" if store == "off" else "ON"))' in st_ps),
          (263635035, None, True, True, True))

    # ------------------------------------------------------------ (Tier C, C8.0) the permission cases, Location Accuracy
    def c80_load(**spec):
        fake = dict({"platforms": ["cordova"], "steps": [{"name": "run"}]}, **spec)
        globals()["_load"] = lambda path: (json.loads(json.dumps(fake))
                                           if path.endswith(os.sep + "selftest-c80.yaml") else real_load(path))
        try:
            got = []
            why = refused(lambda: got.append(scenario("selftest-c80")))
            return got[0] if got else why
        finally:
            globals()["_load"] = real_load
    na_ios = {"ios": "2026-10-02 x"}
    check("(C8.0) locationAccuracy: on by default on every step; scenario-level off reaches a step, which may override it",
          ([s_["locationAccuracy"] for s_ in c80_load()["steps"]],
           [s_["locationAccuracy"] for s_ in c80_load(locationAccuracy=False, not_applicable=na_ios, steps=[
               {"name": "a"}, {"name": "b", "locationAccuracy": True}])["steps"]]), ([True], [False, True]))
    check("(C8.0) locationAccuracy: a quoted \"off\" is a string, refused rather than read as truthy",
          "is true or false" in c80_load(locationAccuracy="off", not_applicable=na_ios), True)
    check("(C8.0) locationAccuracy: off needs a DATED `not_applicable: ios` (Play services' setting; iOS has none); on "
          "needs nothing",
          ("needs a dated" in c80_load(locationAccuracy=False),
           "needs a dated" in c80_load(locationAccuracy=False, not_applicable={"ios": "no date"}),
           isinstance(c80_load(locationAccuracy=True), dict)), (True, True, True))
    check("(C8.0) dialog: turn-on, no-thanks and approximate load on Android, and refuse on iOS, naming why",
          (plan({"android": ["no-thanks"], "ios": []}), plan({"android": ["approximate"]}),
           ["is Android-only" in str(plan(a_)) for a_ in ("turn-on", "no-thanks", "approximate")],
           "Google Play services setting" in str(plan({"android": ["turn-on"], "ios": ["turn-on"]}))),
          ({"android": ["no-thanks"], "ios": []}, {"android": ["approximate"], "ios": []}, [True, True, True], True))
    gms = "com.google.android.gms"
    consent = ui(node("For a better experience, your device will need to use Location Accuracy ", rid=gms + ":id/message"),
                 node("No thanks", "Button", "android:id/button2"), node("Turn on", "Button", "android:id/button1"),
                 pkg=gms)
    upgrade = ui(node("Change Lab’s location access from approximate to precise?", rid=pc + "permission_message"),
                 node("Change to precise location", "Button", pc + "permission_allow_foreground_only_button"),
                 node("Only this time", "Button", pc + "permission_allow_one_time_button"),
                 node("Keep approximate location", "Button", pc + "permission_deny_button"))
    check("(C8.0) dialog_signature: Google's consent dialog and Android's precise upgrade, by their MEASURED buttons — "
          "so undeclaredDialogs sees both",
          (sig(labels(consent)), sig(labels(upgrade))), ("no_thanks|turn_on", "keep_approximate_location|only_this_time"))
    app_window = ui(node("requestPermission"), node("Settings", "Button"), node("OK", "Button"), pkg=app)
    real_sleep = backends.time.sleep
    backends.time.sleep = lambda s_: None
    try:
        gms_ok, in_app, perm_ = Scripted(consent, app_window), Scripted(ui(node("No thanks", "Button"), pkg=app), None), \
            Scripted(ui(node("Turn on", "Button"), node("No thanks", "Button")), None)
        up_ok = Scripted(upgrade, app_window)
        up_gms = Scripted(ui(node("Keep approximate location", "Button"), pkg=gms), None)
        check("(C8.0) answer_dialog: no-thanks taps Play services' window; the same labels in the app or the permission "
              "controller are not it; approximate taps the OS's upgrade, never a Play services window",
              (gms_ok.answer_dialog("no-thanks", {}), gms_ok.sent, in_app.answer_dialog("no-thanks", {}), in_app.sent,
               perm_.answer_dialog("turn-on", {}), perm_.sent, up_ok.answer_dialog("approximate", {}), up_ok.sent,
               up_gms.answer_dialog("approximate", {}), up_gms.sent),
              ("No thanks", ["tap"], None, [], None, [], "Keep approximate location", ["tap"], None, []))
    finally:
        backends.time.sleep = real_sleep

    class Focused(backends.Adb):
        def __init__(self, *focus):
            backends.Adb.__init__(self, "emulator-0")
            self.focus, self.sent = list(focus), []

        def _adb(self, *args, **kw):
            if args[:2] == ("shell", backends.Adb.FOCUS):     # the last reading stands once the script runs out
                return self.focus.pop(0) if len(self.focus) > 1 else self.focus[0]
            self.sent.append(args)
            return ""
    launcher = "mCurrentFocus=Window{2 u0 com.google.android.apps.nexuslauncher/x.NexusLauncherActivity}"
    stale = Focused("mCurrentFocus=Window{1 u0 com.google.android.gms/"
                    "com.google.android.location.settings.LocationSettingsCheckerActivity}", launcher)
    clean = Focused(launcher)
    check("(C8.0) Adb.dismiss_stale_dialog: Play services' consent dialog outlives a force-stop — BACK while it has "
          "focus, named for `swept=`; anything else focused, nothing sent",
          (stale.dismiss_stale_dialog({}), stale.sent, clean.dismiss_stale_dialog({}), clean.sent),
          ("LocationSettingsCheckerActivity", [("shell", "input", "keyevent", "KEYCODE_BACK")], None, []))
    stuck = Focused("mCurrentFocus=Window{1 u0 com.google.android.gms/"
                    "com.google.android.location.settings.LocationSettingsCheckerActivity}")
    check("(C8.0 review) Adb.dismiss_stale_dialog: still focused after the BACK, the step is refused — it would launch "
          "under a window nothing waits on",
          ("still focused after BACK" in refused(lambda: stuck.dismiss_stale_dialog({})), stuck.sent),
          (True, [("shell", "input", "keyevent", "KEYCODE_BACK")]))
    real_which, real_call = backends.shutil.which, backends.labkit.device_call
    backends.shutil.which = lambda name_: "/usr/local/bin/" + name_
    backends.labkit.device_call = lambda argv, **kw: type("P", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
    try:
        sim_ = backends.Simctl("SIM-0")
        sim_refusals = [refused(lambda a_=a_: sim_.answer_dialog(a_, {"package": "x"}))
                        for a_ in ("turn-on", "no-thanks", "approximate")]
    finally:
        backends.shutil.which, backends.labkit.device_call = real_which, real_call
    check("(C8.0) Simctl.answer_dialog refuses the Android-only actions — a backstop behind scenario()'s refusal",
          ["is Android-only" in r_ for r_ in sim_refusals], [True, True, True])

    class Acc:
        """A device's Location Accuracy for run.set_accuracy: `reads`, what each dumpsys shows in turn (the last one
        repeats; None is a read with no network provider); a set that succeeds turns it to its value."""
        def __init__(self, *reads, why=None, avd="Lab1", raises=None):
            self.reads, self.why, self.raises, self.set = list(reads), why, raises, []
            self.info, self.serial = {"avd": avd}, "emulator-0"

        def location_accuracy(self):
            return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

        def set_location_accuracy(self, on):
            self.set.append(on)
            if self.raises:
                raise SystemExit(self.raises)
            if self.why is None:
                self.reads = [on]
            return self.why
    def acc(dev_, want_):
        """set_accuracy's record, or its refusal as text: a refusal a check did not expect is a FAIL, not an exit."""
        try:
            return run.set_accuracy(dev_, {"locationAccuracy": want_}, "l")
        except SystemExit as e:
            return "refused: %s" % e
    same_, flip_, fail_ = Acc(True), Acc(True), Acc(True, why="no switch on X")
    check("(C8.0) run.set_accuracy: every Android step reads it; a step that wants the other value flips it and reads "
          "it again; a flip that did not take refuses the step; iOS has none",
          (acc(same_, True), same_.set, acc(flip_, False), flip_.set,
           "could not be turned off (no switch on X)" in acc(fail_, False),
           acc(backends.Simctl("SIM-0"), True)),
          ({"want": True, "was": True, "now": True}, [], {"want": False, "was": True, "now": False}, [False], True,
           None))
    once_, twice_, slip_ = Acc(None, True), Acc(None), Acc(True, False)
    check("(C8.0 review) run.set_accuracy: a read with no network provider is read again, never taken for off; unread "
          "twice, or a value that slips before the launch, refuses the step",
          (acc(once_, True), once_.set,
           "could not be read" in acc(twice_, False), twice_.set,
           "reads off right before the launch, not on" in acc(slip_, True)),
          ({"want": True, "was": True, "now": True}, [], True, [], True))
    foreign_off, foreign_on = Acc(True, avd="Pixel_9_API_36"), Acc(False, avd="Pixel_9_API_36")
    check("(C8.0 review) run.set_accuracy flips only a Lab emulator: elsewhere a step that wants it off is refused, and "
          "one that wants it on runs as the device is, recorded so",
          ("is not a Lab emulator" in acc(foreign_off, False), foreign_off.set,
           acc(foreign_on, True), foreign_on.set),
          (True, [], {"want": True, "was": False, "now": False}, []))
    off_scen = {"steps": [{"locationAccuracy": False}, {"locationAccuracy": True}]}
    on_scen = {"steps": [{"locationAccuracy": True}]}
    lab_, other_, all_on, broken_ = Acc(False), Acc(False, avd="Pixel_9_API_36"), Acc(False), Acc(False, raises="x")
    check("(C8.0 review) run.restore_accuracy: a scenario that turned it off puts it back on, on a Lab emulator only, "
          "and a failed restore is a LAB DEFECT line, never a refusal",
          (run.restore_accuracy(lab_, off_scen), lab_.set, run.restore_accuracy(other_, off_scen), other_.set,
           run.restore_accuracy(all_on, on_scen), all_on.set, refused(lambda: run.restore_accuracy(broken_, off_scen)),
           run.restore_accuracy(backends.Simctl("SIM-0"), off_scen)),
          (True, [True], False, [], False, [], "loaded", False))
    with open(run.__file__) as f_:
        run_src = f_.read()
    step_src = run_src.split("\ndef run_step(", 1)[1].split("\ndef ", 1)[0]
    at_ = lambda text: step_src.find(text)
    check("(C8.0) run_step sets Location Accuracy on EVERY step, unconditionally, after neutral() and before the log "
          "begins; the launch mark says what was read right before the launch, and run.json records it; main puts it "
          "back after the steps",
          (-1 < at_("dev.neutral()") < at_("set_accuracy(dev, step, label)") < at_("dev.begin_log()"),
           re.search(r"^    accuracy = set_accuracy\(dev, step, label\)$", step_src, re.M) is not None,
           ' accuracy=%s" % ("on" if accuracy["now"] else "off")' in step_src,
           '"location_accuracy": accuracy' in step_src,
           "\n        restore_accuracy(dev, scen)\n    finally:" in run_src), (True, True, True, True, True))
    check("(C8.0 review) locationAccuracy: YAML's 0 or 1 is a number, refused like a quoted \"off\"",
          ("is true or false" in c80_load(locationAccuracy=0, not_applicable=na_ios),
           "is true or false" in c80_load(locationAccuracy=1)), (True, True))

    class Dumpsys(backends.Adb):
        def __init__(self, out):
            backends.Adb.__init__(self, "emulator-0")
            self.out = out

        def _adb(self, *args, **kw):
            return self.out
    block_ = "  gps provider:\n    enabled=true\n  network provider:\n    enabled=%s\n  fused provider:\n"
    check("(C8.0 review) Adb.location_accuracy: the network provider's own block decides on or off; a read with no such "
          "block (adb failed, output cut short) is None, never off",
          [Dumpsys(o_).location_accuracy() for o_ in (block_ % "true", block_ % "false", "",
                                                      "  gps provider:\n    enabled=true\n")],
          [True, False, None, None])

    class Preflight(backends.Adb):
        def __init__(self, gms_line):
            backends.Adb.__init__(self, "emulator-0")
            self.gms_line = gms_line

        def _adb(self, *args, **kw):
            if args[:2] == ("shell", "getprop"):
                return "1"
            if args[:2] == ("shell", "df"):
                return "Filesystem 1K-blocks Used Available Use% Mounted\n/dev/block 9000000 1000000 4000000 20% /data"
            if args[:2] == ("shell", "dumpsys"):
                return "mWakefulness=Awake"
            if args[:1] == ("emu",):
                return "Lab1\nOK"
            if args[:3] == ("shell", "pm", "list"):
                return self.gms_line
            return ""
    check("(C8.0 review) Adb.preflight records Google Play services' versionCode (the consent dialog and Location "
          "Accuracy are its, and it updates itself), None when pm lists none",
          [Preflight(l_).preflight()["play_services"] for l_ in (
              "package:com.google.android.gms.policy_sidecar_aps versionCode:1\n"
              "package:com.google.android.gms versionCode:263436035\n", "")], [263436035, None])

    class Switch(backends.Adb):
        """Play services' Location Accuracy screen, scripted: `takes`, whether a tap flips the setting."""
        def __init__(self, on, takes=True, shown=True):
            backends.Adb.__init__(self, "emulator-0")
            self.on, self.takes, self.shown, self.sent = on, takes, shown, []

        def _adb(self, *args, **kw):
            if args[:3] == ("shell", "dumpsys", "location"):
                return ("  gps provider:\n    enabled=true\n  network provider:\n    enabled=%s\n  fused provider:\n"
                        "    enabled=true\n" % ("true" if self.on else "false"))
            self.sent.append(" ".join(args[1:3]))
            if args[1:3] == ("input", "tap") and self.takes:
                self.on = not self.on
            return ""

        def _dump(self):
            return ('<hierarchy><node resource-id="android:id/switch_widget" checked="%s" bounds="[900,400][1000,460]"/>'
                    '</hierarchy>' % ("true" if self.on else "false")) if self.shown else "<hierarchy></hierarchy>"
    real_sleep = backends.time.sleep
    backends.time.sleep = lambda s_: None
    try:
        flips = []
        for sw_, want_ in ((Switch(True), True), (Switch(False), True), (Switch(True), False),
                           (Switch(False, takes=False), True), (Switch(False, shown=False), True)):
            flips.append((sw_.set_location_accuracy(want_), sw_.sent))
    finally:
        backends.time.sleep = real_sleep
    check("(C8.0) Adb.set_location_accuracy, which run.py now calls before every Android launch: a no-op sends nothing; "
          "a flip opens Play services' screen, taps its switch once and goes Home; a tap that does not take and a "
          "missing switch say why",
          flips, [(None, []), (None, ["am start", "input tap", "input keyevent"]),
                  (None, ["am start", "input tap", "input keyevent"]),
                  ("not on 10 s after tapping its switch", ["am start", "input tap", "input keyevent"]),
                  ("no switch on " + backends.Adb.ACCURACY_ACTION, ["am start", "input keyevent"])])

    import lab_devices as ld_
    stubbed = ("adb_devices", "avd_name", "android_view", "windows", "screens", "blank", "accuracy_on", "accuracy_off")
    saved_ = {n_: getattr(ld_, n_) for n_ in stubbed}
    saved_adb, saved_Adb = ld_.labkit.adb, ld_.backends.Adb
    calls_ = []

    class Preflight:
        def __init__(self, serial):
            pass

        def preflight(self):
            return {}
    try:
        ld_.adb_devices = lambda: {"emulator-0": "device"}
        ld_.avd_name = lambda serial: "Lab1"
        ld_.android_view = lambda name: {"pid": 7}
        ld_.windows = lambda pids: {7: [(1080, 2400, True)]}
        ld_.screens = lambda wins: wins
        ld_.blank = lambda serial: None
        ld_.accuracy_off = lambda serial: calls_.append("off") or "Google Location Accuracy is off"
        ld_.accuracy_on = lambda serial: calls_.append("on") or "not on 10 s after tapping its switch"
        ld_.labkit.adb = lambda *a, **k: "1"
        ld_.backends.Adb = Preflight
        v_ = {"serial": "emulator-0", "name": "Lab1"}
        looked = (ld_.ready_android(v_, 0), list(calls_))
        del calls_[:]
        set_up = (ld_.ready_android(v_, 0, setup=True), list(calls_))
    finally:
        for n_, f_ in saved_.items():
            setattr(ld_, n_, f_)
        ld_.labkit.adb, ld_.backends.Adb = saved_adb, saved_Adb
    check("(C8.0) lab_devices.ready_android: resolve()'s look (setup=False) never reads Location Accuracy, so an "
          "emulator left off is dealt and run.py's next step turns it on; `up` (setup=True) still sets it, naming a "
          "flip that failed",
          (looked, set_up), ((None, []), ("location accuracy: not on 10 s after tapping its switch", ["on"])))

    def asked(name_):
        """Each step's name, its asserted permission and accuracy calls, and the dialogs it answers per OS."""
        return [(st["name"], [(h["api"], h.get("args"), p_, c_.get("equals")) for h in st["hooks"]
                              if h.get("api") in ("requestPermission", "getCurrentPosition", "requestTemporaryFullAccuracy")
                              for p_, c_ in sorted(h.get("returns", {}).items())],
                 st["dialog"]["android"] if st["dialog"] else None, st["dialog"]["ios"] if st["dialog"] else None)
                for st in scenario(name_)["steps"]]
    rp, gcp_, rtfa = "requestPermission", "getCurrentPosition", "requestTemporaryFullAccuracy"
    gcp_args = {"samples": 1, "persist": False, "timeout": 10}
    check("(C8.0) permission-request-forms: granted 3, deny 2, motion-silent 4 (Android's motion window denied), "
          "motion-disabled 4 with one window",
          asked("permission-request-forms"),
          [("granted", [(rp, None, "$", 3)], None, None), ("deny", [(rp, None, "$err", 2)], ["deny"], ["deny"]),
           ("motion-silent", [(rp, None, "$", 4)], ["allow", "deny"], ["allow"]),
           ("motion-disabled", [(rp, None, "$", 4)], ["allow"], ["allow"])])
    check("(C8.0) permission-request-motion-android: 3, 3, 2, 5, 5, then location 4 and motion 3; iOS excused, dated",
          (asked("permission-request-motion-android"),
           DATED.match(scenario("permission-request-motion-android")["not_applicable"]["ios"]) is not None),
          ([("grant", [(rp, "motion", "$", 3)], ["allow"], ["allow"]), ("held", [(rp, "motion", "$", 3)], None, None),
            ("deny", [(rp, "motion", "$err", 2)], ["deny"], ["deny"]),
            ("deny-again", [(rp, "motion", "$err", 5)], ["deny"], ["deny"]),
            ("denied-always", [(rp, "motion", "$err", 5)], [], []),
            ("two-step", [(rp, "location", "$", 4), (rp, "motion", "$", 3)], ["allow", "allow"], ["allow", "allow"])],
           True))
    check("(C8.0) permission-request-motion-ios: 3, 5, 1, then location 4 and motion 1, with no window but the "
          "location alert; Android excused, dated",
          (asked("permission-request-motion-ios"),
           DATED.match(scenario("permission-request-motion-ios")["not_applicable"]["android"]) is not None),
          ([("granted", [(rp, "motion", "$", 3)], None, None), ("denied", [(rp, "motion", "$err", 5)], None, None),
            ("never-asked", [(rp, "motion", "$err", 1)], None, None),
            ("two-step", [(rp, "location", "$", 4), (rp, "motion", "$err", 1)], ["allow"], ["allow"])], True))
    check("(C8.0) permission-location-error: the types' LocationError 1 after a denial, both times; Flutter excused",
          (asked("permission-location-error"), sorted(scenario("permission-location-error")["not_applicable"])),
          ([("first-denial", [(gcp_, gcp_args, "$err", 1)], ["deny"], ["deny"]),
            ("denied-again", [(gcp_, gcp_args, "$err", 1)], ["deny"], ["cancel"])], ["flutter"]))
    check("(C8.0) temporary-full-accuracy-android: Reduced (1) after keeping approximate, and after a denial",
          asked("temporary-full-accuracy-android"),
          [("coarse-only", [(rtfa, "DemoPurpose", "$", 1)], ["approximate"], []),
           ("never-asked-deny", [(rtfa, "DemoPurpose", "$", 1)], ["deny"], [])])
    off_ = scenario("location-accuracy-off")
    check("(C8.0) location-accuracy-off: six fresh steps with Location Accuracy off, each answer Android's; iOS "
          "excused, dated; slc measured",
          ([(st["name"], st["locationAccuracy"], st["fresh"], st["noStart"], (st["dialog"] or {}).get("android"),
             st["expect"].get("dialogAnswered"), st["expect"]["undeclaredDialogs"]) for st in off_["steps"]],
           DATED.match(off_["not_applicable"]["ios"]) is not None),
          ([("no-thanks", False, True, False, ["no-thanks"], "no-thanks", 0),
            ("turn-on", False, True, False, ["turn-on"], "turn-on", 0),
            ("alert-disabled", False, True, False, None, None, 0),
            ("request-permission", False, True, True, ["no-thanks"], "no-thanks", 0),
            ("ready-only", False, True, True, None, None, 0),
            ("slc", False, True, False, None, None, "any")], True))
    unknown_ = scenario("api-forbidden")["steps"][-1]
    check("(C8.0) api-forbidden's permission-unknown: requestPermission('bogus'), invalid and settled — recorded",
          (unknown_["name"], [(h["api"], h["args"], DATED.match(h["invalid"]) is not None, h["outcome"][:7])
                              for h in unknown_["hooks"]]),
          ("permission-unknown", [("requestPermission", "bogus", True, "settled")]))
    check("(C8.0) five C8.0 scenarios are on PERMISSIONS, and permission-request-forms is in SUITE (decision 27 B)",
          ([n_ for n_ in ("permission-request-motion-android", "permission-request-motion-ios",
                          "permission-location-error", "temporary-full-accuracy-android", "location-accuracy-off")
            if n_ not in sweep_.LISTS["PERMISSIONS"]], "permission-request-forms" in sweep_.SUITE), ([], True))

    # ------------------------------------------------------------ (Tier C, C5.3) what needed the rebuild
    # A clipped string and `contains` (R's review, ruled A): end to end, through apiAssertions.
    log_hook = [{"api": "logger.getLog", "args": {"limit": 5, "order": 1},
                 "returns": {"$": {"type": "string", "contains": "LAB-C5-3 debug"}}}]
    def read_log(ret):
        return apiAssertions(capture(["I/LAB     ( 1): app=api id=logger.getLog#0 outcome=ok",
                                      "I/LAB     ( 1): app=ret id=logger.getLog#0 path=$ type=string " + ret,
                                      "I/LAB     ( 1): app=api-done n=1"], log_hook))
    check("(C5.3) `contains` on a string the driver clipped holds on what arrived: a pass when its first characters "
          "hold the needle, `truncated` when they do not, and `truncated` on a line the transport cut, needle or not",
          [read_log("value=10-05%20DEBUG%20LAB-C5-3%20debug%0A10-05%20INFO trunc=1 parent=1"),
           read_log("value=10-05%20INFO%20something%20else trunc=1 parent=1"),
           read_log("value=10-05%20DEBUG%20LAB-C5-3%20debug%0A10-05%20IN")],
          ["ok", "logger.getLog#0.$=truncated", "logger.getLog#0.$=truncated"])
    clip_ = {"type": "string", "value": "10-05 DEBUG LAB-C5-3 debug", "parent": True, "trunc": True, "cut": False}
    check("(C5.3) ... and only `contains`, alone: beside another comparator, or for any other one, a clipped string "
          "is still `truncated`; a whole string that lacks the needle is red as before",
          [_check(clip_, {"contains": "LAB-C5-3", "startsWith": "10-05"}), _check(clip_, {"startsWith": "10-05"}),
           _check(clip_, {"equals": "10-05 DEBUG LAB-C5-3 debug"}),
           (_check(dict(clip_, trunc=False), {"contains": "nothing like it"}) or "").startswith("want-contains-")],
          ["truncated", "truncated", "truncated", True])

    # `lab_http: {revoked: true}`: validated, and the server refuses from the first upload.
    with_token = {"config": {"http": {"url": LAB_HTTP + "/locations"}, "authorization": {"accessToken": "lab-access-1"}},
                  "hooks": []}
    check("(C5.3) lab_http: {revoked: true} loads on a step that configures an access token; without one, as "
          "anything but true, or beside `locations`, it refuses",
          (_validate_lab_http("selftest", dict(with_token, lab_http={"revoked": True})),
           "authorization.accessToken" in refused(lambda: _validate_lab_http("selftest", {
               "config": {"http": {"url": LAB_HTTP}}, "hooks": [], "lab_http": {"revoked": True}})),
           "authorization.accessToken" in refused(lambda: _validate_lab_http("selftest", dict(
               with_token, lab_http={"revoked": "yes"}))),
           "one of them" in refused(lambda: _validate_lab_http("selftest", dict(
               with_token, lab_http={"revoked": True, "locations": 503})))),
          (None, True, True, True))
    mdev3 = MarkDev()
    lab3 = run.LabHttp(mdev3, "lab-access-1", None, True)
    def post3(path, body, token=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (lab3.port, path), data=body.encode(), method="POST")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        answers3 = [post3("/locations", one, "lab-access-1"), post3("/refresh", "refresh_token=lab-refresh"),
                    post3("/locations", one, "lab-access-2")]
    finally:
        lab3.close()
    check("(C5.3) LabHttp started revoked: the step's own token is refused 401 at once, /refresh issues the next, "
          "and that one is accepted",
          (answers3, labHttpRefreshedUploads(capture(mdev3.lines, [], None, measure_from="launch"))), ([401, 200, 200], 1))

    # `notification_tap`: validated, dispatched with the app's package, and done on a scripted shade.
    tap_ = dict(base_, actions=[act(notification_tap="notificationButtonPause")])
    _validate_moves("selftest", tap_, ("ios",))
    check("(C5.3) notification_tap names a button's resource id: true, a spaced name, or a second thing to do refuses",
          ["resource id" in refused(lambda: _validate_moves("selftest", dict(base_, actions=[act(notification_tap=True)]),
                                                             ("ios",))),
           "resource id" in refused(lambda: _validate_moves("selftest", dict(base_, actions=[
               act(notification_tap="the pause button")]), ("ios",))),
           "exactly one of" in refused(lambda: _validate_moves("selftest", dict(base_, actions=[
               act(notification_tap="notificationButtonPause", power_save=True)]), ("ios",)))],
          [True, True, True])
    check("(C5.3 review) notification_tap reads the screen with the dialog watcher's `uiautomator dump`: it refuses "
          "beside `dialog:` and beside `undeclaredDialogs`, in `expect` or one platform's; and the window holds its "
          "own 10 s (the 6 s wait and the look that was running), which is more than the backend can take",
          (["uiautomator dump" in refused(lambda extra=extra: _validate_moves("selftest", dict(tap_, **extra), ("ios",)))
            for extra in ({"dialog": {"android": ["allow"]}}, {"expect": {"undeclaredDialogs": 0}},
                          {"platform_expect": {"flutter": {"undeclaredDialogs": 0}}}, {"expect": {"crashes": 0}})],
           ["need %d s" % n in refused(lambda after=after: _validate_moves("selftest", dict(tap_, settle=0, after=after),
                                                                          ("ios",))) for n, after in ((10, 25), (10, 26))],
           NOTIFICATION_TAP_S >= backends.Adb.NOTIFICATION_WAIT_S + 3.5),
          ([True, True, True, False], [True, False], True))
    class TapDev(FakeDev):
        missed = None

        def notification_tap(self, package, button):
            self.calls.append(("notification_tap", package, button))
            return self.missed
    class NoButton(TapDev):
        missed = "absent"
    tap_lines = ["09-21 10:00:01.000 I/LAB     ( 7): app=boot n=1",
                 "09-21 10:00:02.000 I/LAB     ( 7): point=onDestroy isChangingConfigurations=false"]
    tdev, ndev = TapDev(tap_lines), NoButton(tap_lines)
    thost, nhost = ({"error": None, "held_s": {}, "sent_s": {}} for _ in range(2))
    run.run_host(tdev, tap_, time.time(), time.time() + 3, threading.Event(), thost, None, {"package": "com.lab.app"})
    run.run_host(ndev, tap_, time.time(), time.time() + 3, threading.Event(), nhost, None, {"package": "com.lab.app"})
    check("(C5.3) run_host: a notification tap goes to the device with the app's package and the button, then is marked",
          (tdev.calls, thost["error"]),
          ([("notification_tap", "com.lab.app", "notificationButtonPause"),
            ("mark", "host i=0 action=notification_tap notificationButtonPause sent_ms=T done_ms=T mark_ms=T")], None))
    tap_meta = [dict(tap_["actions"][0])]
    check("(C5.3 review) a button that was not in the shade is the SDK's doing: the host marks the miss and the step "
          "is scored `action0=missed(absent)`, where a SystemExit left it unscored with no log; a tap that landed is ok",
          (ndev.calls[-1], nhost["error"],
           [actionsDone(capture(["I/LAB     ( 2): point=onDestroy isChangingConfigurations=false",
                                 "I/LAB     ( 2): point=" + d.calls[-1][1]], [], None, actions=tap_meta))
            for d in (ndev, tdev)]),
          (("mark", "host i=0 action=notification_tap notificationButtonPause missed=absent sent_ms=T done_ms=T mark_ms=T"),
           None, ["action0=missed(absent)", "ok"]))
    class ShadeAdb(backends.Adb):
        NOTIFICATION_WAIT_S = 1.2

        def __init__(self, dumps):
            backends.Adb.__init__(self, "emulator-5580")
            self.dumps, self.sent = list(dumps), []

        def _adb(self, *args, **kw):
            self.sent.append(" ".join(args[1:]))
            return ""

        def _dump(self):
            return self.dumps.pop(0) if self.dumps else None
    # (C5.3 review) A shade as a dump gives it: the button nested under a root and after a sibling, each with bounds
    # of its own.  The tap is the matched node's centre, not the first `bounds=` in the dump.
    button_ = ('<hierarchy><node index="0" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">'
               '<node text="12:00" resource-id="com.android.systemui:id/clock" bounds="[40,60][200,120]" />'
               '<node text="PAUSE" resource-id="%s:id/notificationButtonPause" class="android.widget.Button" '
               'bounds="%s" /></node></hierarchy>')
    shade_ = ShadeAdb([None, "<hierarchy></hierarchy>", button_ % ("com.lab.app", "[900,300][1040,380]")])
    tapped_ = shade_.notification_tap("com.lab.app", "notificationButtonPause")
    stranger_ = ShadeAdb([button_ % ("com.other.app", "[900,300][1040,380]")] * 20)
    flat_ = ShadeAdb([button_ % ("com.lab.app", "[0,0][0,0]")] * 20)
    missed_ = [d.notification_tap("com.lab.app", "notificationButtonPause") for d in (stranger_, flat_)]
    check("(C5.3) Adb.notification_tap: opens the shade, waits for the app's own button through a dump that could not "
          "be read and one without it, taps its centre (not the root's) and closes the shade; (C5.3 review) another "
          "app's button of that id is `absent` and one with no area `no-area`, each returned with nothing tapped and "
          "the shade closed",
          (shade_.sent, tapped_, missed_, [(d.sent[0], d.sent[-1], any(c.startswith("input tap") for c in d.sent))
                                           for d in (stranger_, flat_)]),
          (["cmd statusbar expand-notifications", "input tap 970 340", "cmd statusbar collapse"], None,
           ["absent", "no-area"], [("cmd statusbar expand-notifications", "cmd statusbar collapse", False)] * 2))

    # (C5.3 review) run_host marks a move before its first hop when a declaration counts deliveries after it, and
    # only then: the (C5.0) run_host check above, a step with no such declaration, sends the same calls as before.
    anch_ = {"settle": 20, "after": 70, "hooks": [],
             "events": [{"subscribe": "watchPosition", "none_after": "move0", "returns": {"$.coords.latitude": {"type": "number"}}}],
             "moves": [{"after": [{"armed": "watchPosition@0"}], "to": {"offset_m": 20, "bearing": 90}, "hops": 2, "every_s": 1}]}
    _validate_moves("selftest", anch_)
    adev = FakeDev(["09-21 10:00:01.000 I/LAB     ( 7): app=boot n=1",
                    "09-21 10:00:02.000 I/LAB     ( 7): app=event-arm id=watchPosition@0 outcome=ok"])
    ahost = {"error": None, "held_s": {}, "sent_s": {}}
    run.run_host(adev, anch_, time.time(), time.time() + 4, threading.Event(), ahost)
    check("(C5.3 review) run_host: a move that `none_after: move0` counts from is marked `move-begin` before its "
          "first hop, once, and `sent_ms` is taken after that mark",
          ([c[:2] if c[0] == "mark" else c[0] for c in adev.calls], ahost["error"]),
          ([("mark", "move-begin i=0"), "move", ("mark", "move i=0 hop=1/2 to=49.2827,-123.120562 sent_ms=T mark_ms=T"),
            "move", ("mark", "move i=0 hop=2/2 to=49.2827,-123.120424 sent_ms=T mark_ms=T")], None))

    # (C5.3 review) A windowed metric on a step that ends on its evidence: from launch, or refused.
    win_ = lambda **kw: dict({"settle": 0, "after": 10, "trigger": "none", "end": "evidence", "hooks": [{"api": "getCount"}],
                              "events": [], "expect": {"labHttpRecords": 1, "crashes": 0}}, **kw)
    check("(C5.3 review) a step that ends on evidence and expects a windowed metric (the lab server's counts, "
          "deferredExitDropped) measures from launch: its trigger mark is written after the app is running.  "
          "Refused otherwise; a step that keeps its window, or has a trigger, is not asked",
          [refused(lambda st=st: _validate_windowed("selftest", st, ["react-native", "flutter"])).split(" is counted")[0]
           for st in (
              win_(), win_(measure_from="launch"), win_(end="window"), win_(trigger="finish"),
              win_(expect={"deferredExitDropped": 1}), win_(expect={"crashes": 0}),
              win_(expect={"crashes": 0}, platform_expect={"flutter": {"labHttp.locations.401": ">=1"}}),
              win_(measure_from="trigger"))],          # as scenario() fills it in: the default is no exemption
          ["selftest: labHttpRecords", "loaded", "loaded", "loaded", "selftest: deferredExitDropped", "loaded",
           "selftest: labHttp.locations.401", "selftest: labHttpRecords"])

    # The scenarios, each held to the point its design turns on.
    rem_ = scenario("events-remove")["steps"][0]
    rem_ev = {"%s@%d" % (e["subscribe"], i): e for i, e in enumerate(rem_["events"])}
    check("(C5.3) events-remove: each removed listener is silent after its own removal with a never-removed twin "
          "beside it; the one watch is held to the move five seconds on, never to its own removal (Capacitor asks "
          "its native side after remove() returns).  (C5.3 review) It is removed at 8 s, not 6 (Android's first "
          "watch delivery came up to 5.38 s after start()), and the move is two hops two seconds apart, so a watch "
          "that survived speaks again clear of the anchor; stop() follows the second hop by three seconds",
          ([(k, e.get("remove_at_s"), e.get("none_after"), e.get("min_after"), e.get("min")) for k, e in rem_ev.items()],
           [(m["after"], m["delay_s"], m["hops"], m["every_s"]) for m in rem_["moves"]],
           sum(e["subscribe"] == "watchPosition" for e in rem_["events"]), move_anchors(rem_),
           [h["at_s"] for h in rem_["hooks"] if h["api"] == "stop" and "at_s" in h]),
          ([("onLocation@0", 8, "remove", None, None), ("onLocation@1", None, None, {"ref": "onLocation@0", "n": 1}, None),
            ("onEnabledChange@2", 8, "remove", None, None), ("onEnabledChange@3", None, None, None, 2),
            ("watchPosition@4", 8, "move0", None, None)], [([{"armed": "watchPosition@4"}], 12, 2, 2)], 1, {0}, [18]))
    http_ = scenario("events-http")["steps"]
    check("(C5.3) events-http: a closed port, the lab server's 200, and a server that starts revoked; only the last "
          "configures a token; its onHttp is read, not judged (Android posts the 401, iOS only the retry), and the "
          "server's own counts carry the refresh",
          [(s_["name"], uses_lab_http(s_), s_.get("lab_http"), "authorization" in s_["config"],
            [(e["subscribe"], sorted(k for k, c in e["returns"].items() if c.get(RECORD))) for e in s_["events"]],
            {k: v for k, v in s_["expect"].items() if k.startswith("labHttp") and k != "labHttp"}) for s_ in http_],
          [("refused", False, None, False, [("onHttp", [])], {}),
           ("accepted", True, None, False, [("onHttp", [])], {"labHttpRecords": 1, "labHttp.locations.200": 1}),
           ("unauthorized", True, {"revoked": True}, True, [("onAuthorization", []), ("onHttp", ["$.status", "$.success"])],
            {"labHttp.locations.401": ">=1", "labHttp.refresh.200": ">=1", "labHttpRefreshedUploads": ">=1",
             "labHttpRecords": 1})])
    check("(C5.3) ... and each step ends on its evidence, the server's counts read after it: getCount follows sync() "
          "— 1 after a refused upload, 0 after an accepted one, and 0 three seconds after a 401, by when the retry "
          "has answered",
          [(end_policy(s_, "react-native"), [(h.get("at_s", 0), h["returns"]["$"]["equals"]) for h in s_["hooks"]
                                              if h["api"] == "getCount"]) for s_ in http_],
          [("evidence", [(0, 1)]), ("evidence", [(0, 0)]), ("evidence", [(3, 0)])])
    check("(C5.3 review) ... and each measures from launch: the server marked the first upload 140 ms after the "
          "trigger mark on Flutter Android, and a slower launch would have put it before the window",
          [s_["measure_from"] for s_ in http_], ["launch"] * 3)
    logger_ = scenario("api-logger")["steps"]
    check("(C5.3) api-logger: two seconds after ready() (the SDK's boot records are in by then: the pilot's "
          "300-character NotificationChannel line) the log is emptied and the marker written, then read back "
          "oldest-first in a later slot, so its record is at or near the head whatever came after (a clipped string "
          "holds only its first characters); destroyLog alone in the last step, its return typed there",
          ([(h["api"], h.get("at_s", 0)) for h in logger_[0]["hooks"]][1:3] +
           [(h["api"], h.get("at_s", 0), h["args"].get("order"), h["returns"]["$"].get("contains"))
            for h in logger_[0]["hooks"] if h["api"] == "logger.getLog"],
           [(h["api"], h.get("outcome"), (h.get("returns") or {}).get("$")) for h in logger_[1]["hooks"]],
           logger_[0]["hooks"][1].get("outcome")),
          ([("logger.destroyLog", 2), ("logger.debug", 2), ("logger.getLog", 4, 1, "LAB-C5-3 debug")],
           [("logger.destroyLog", None, {"type": "null|undefined"})], "resolved"))
    host_sc = scenario("events-android-host")
    check("(C5.3) events-android-host: battery saver on then off read as two deliveries of one member; the "
          "notification's Pause button tapped by the host; iOS excused, dated",
          ([[(e["subscribe"], e.get("delivery"), e["returns"]["$"].get("equals")) for e in s_["events"]]
            for s_ in host_sc["steps"]],
           [[next(k for k in ACTION_HANDLERS if k in a) for a in s_["actions"]] for s_ in host_sc["steps"]],
           DATED.match(host_sc["not_applicable"]["ios"]) is not None),
          ([[("onPowerSaveChange", 1, True), ("onPowerSaveChange", 2, False)],
            [("onNotificationAction", None, "notificationButtonPause")]],
           [["power_save", "power_save"], ["notification_tap"]], True))
    check("(C5.3 review) ... the notification is configured by its layout alone: the SDK wires that layout's Pause "
          "button whatever `notification.actions` holds, so the step does not pretend to read it",
          next(h["args"]["app"]["notification"] for h in host_sc["steps"][1]["hooks"] if h["api"] == "ready"),
          {"layout": "tslocationmanager_notification_layout"})
    beats_ = [scenario("events-heartbeat-android"), scenario("events-heartbeat-ios")]
    check("(C5.3) the two heartbeat scenarios each excuse the other OS, dated, and iOS's sets preventSuspend and "
          "waits out its 60 s floor",
          ([sorted(b["not_applicable"]) for b in beats_],
           [DATED.match(list(b["not_applicable"].values())[0]) is not None for b in beats_],
           [next(h["args"]["app"] for h in b["steps"][0]["hooks"] if h["api"] == "ready") for b in beats_],
           [b["steps"][0]["after"] for b in beats_]),
          ([["ios"], ["android"]], [True, True],
           [{"stopOnTerminate": True, "heartbeatInterval": 10},
            {"stopOnTerminate": True, "heartbeatInterval": 60, "preventSuspend": True}], [30, 90]))
    fence_ev = scenario("events-geofence")["steps"][0]["events"]
    check("(C5.3) events-geofence: onActivityChange appended arm-only, dated, so @0-@3 keep their ids; "
          "events-location-filter and geofence-exit-far-android run on Flutter now",
          ([e["subscribe"] for e in fence_ev], DATED.match(fence_ev[4]["arm_only"]) is not None,
           "flutter" in scenario("events-location-filter")["platforms"],
           sorted(scenario("geofence-exit-far-android")["not_applicable"])),
          (["onGeofencesChange", "onGeofence", "onProviderChange", "onConnectivityChange", "onActivityChange"], True, True,
           ["ios"]))
    check("(C5.3) six of its seven scenarios are on OPTIN and not in SUITE: events-http until a green board (red on "
          "two cells), the rest to stay; events-remove is in SUITE",
          ([n_ for n_ in ("events-http", "events-heartbeat-android", "events-heartbeat-ios",
                          "events-android-host", "api-logger", "api-device-settings-android")
            if n_ not in sweep_.LISTS["OPTIN"] or n_ in sweep_.SUITE], "events-remove" in sweep_.SUITE), ([], True))

    # ------------------------------------------------------------ (Tier C, C8.1) the failure callbacks, measured
    errs_ = scenario("events-errors")
    check("(C8.1) events-errors: each failure callback is passed (`errors: true`) on a declaration that is armed only "
          "and judges no path; the denials are real answers carried forward; the watch has a step of its own with no "
          "dialog, so a driver that cannot call it waits for none; on PERMISSIONS, not in SUITE",
          ([(s_["name"], s_.get("permissions"), s_.get("dialog"),
             [(e["subscribe"], e.get("errors"), DATED.match(e.get("arm_only") or "") is not None,
               all(checks == {RECORD: True} for checks in e["returns"].values()), "$err" in e["returns"])
              for e in s_["events"]]) for s_ in errs_["steps"]],
           "events-errors" in sweep_.LISTS["PERMISSIONS"], "events-errors" in sweep_.SUITE),
          ([("first-denial", "grant", {"android": ["deny"], "ios": ["deny"]}, [("onLocation", True, True, True, True)]),
            ("denied-again", "keep", {"android": ["deny"], "ios": ["cancel"]}, [("onLocation", True, True, True, True)]),
            ("watch-denied", "keep", None, [("watchPosition", True, True, True, True)]),
            ("timeout-behind-dialog", "grant", {"android": ["deny"], "ios": ["deny"]},
             [("onLocation", True, True, True, True)])], True, False))
    check("(C8.1) ... and its last step is what raises the listener's error on Android: never asked again (a fresh "
          "install, the location permissions un-answered), and getCurrentPosition's timeout one second, under the "
          "two the host needs to answer a dialog; the denial steps keep ten",
          ([(s_.get("fresh"), bool(s_.get("prompt")),
             [h["args"]["timeout"] for h in s_["hooks"] if h.get("api") == "getCurrentPosition"])
            for s_ in errs_["steps"]]),
          [(False, True, [10]), (False, False, [10]), (False, False, []), (True, True, [1])])
    errs_none_ = ("watchPosition@0.delivered=0, watchPosition@0.errors=0, "
                  "watchPosition@0.$.coords.latitude=unreported, watchPosition@0.$err=unreported")

    def errs_cap(*said):
        return capture(["I/LAB     ( 1): app=" + s_ for s_ in said + ("events-armed n=1",)], [],
                       errs_["steps"][2]["events"])
    check("(C8.1) scored: a watch error passed on and none at all are both green, with the count and the first value "
          "on the `recorded` row; a declaration the driver refused (Dart's watchPosition has no failure callback) is red",
          [(eventAssertions(c_), METRICS["recorded"](c_)) for c_ in (
              errs_cap("event-arm id=watchPosition@0 outcome=ok", "event-error id=watchPosition@0 n=1",
                       "ret id=watchPosition@0 path=$err type=number value=1 parent=1"),
              errs_cap("event-arm id=watchPosition@0 outcome=ok"),
              errs_cap("event-arm id=watchPosition@0 outcome=unexpressible"))],
          [("ok", "watchPosition@0.delivered=0, watchPosition@0.errors=1, "
                  "watchPosition@0.$.coords.latitude=unreported, watchPosition@0.$err=number:1"),
           ("ok", errs_none_), ("watchPosition@0=unexpressible", errs_none_)])

    # ---- WO-191, D3: the product hook (lab_product.py beside labkit.py) ----
    check("(WO-191, D3) no lab_product.py beside labkit (bg-forge has none): no product, and every hook is "
          "background-geolocation's",
          (PRODUCT, os.path.exists(PRODUCT_PATH), HEADLESS_NAMES is _geo_headless_names, SDK_LOG is _geo_sdk_log,
           EVIDENCE, CAPTURE_EXTRAS, IMPLICIT),
          (None, False, True, True, None, None, {"configDropped": "-"}))
    # (WO-191 review) CAPTURE_EXTRAS, driven: run_step needs a device, so its end-of-log lines are run.end_capture_log.
    class LogDev:
        def end_log(self, plat):
            return "09-21 10:00:01.000 I/LAB     ( 7): app=boot n=1\n"
    seen_ = []

    def extras_(dev, plat, out):
        seen_.append((type(dev).__name__, plat["package"], os.path.exists(os.path.join(out, "logcat.txt"))))

    def failing_(dev, plat, out):
        raise SystemExit("selftest extras failed")

    def ended_(hook):
        out_, phases_ = tempfile.mkdtemp(prefix="labkit-extras-"), {}
        saved_ = run.labkit.CAPTURE_EXTRAS
        try:
            run.labkit.CAPTURE_EXTRAS = hook
            try:
                got_ = "ok" if run.end_capture_log(LogDev(), {"package": "com.lab.app"}, out_, phases_) else "no log"
            except (Exception, SystemExit) as e:
                got_ = "%s: %s" % (type(e).__name__, e)
            return got_, list(phases_), os.path.exists(os.path.join(out_, "logcat.txt"))
        finally:
            run.labkit.CAPTURE_EXTRAS = saved_
            shutil.rmtree(out_)
    check("(WO-191) end_capture_log: with no CAPTURE_EXTRAS the log is ended and written as before; a product's is "
          "handed the device, the platform and the capture dir once logcat.txt is on disk, and one that fails costs no "
          "log; run_step ends its log through it",
          ([ended_(h) for h in (None, extras_, failing_)], seen_,
           "log_text = end_capture_log(dev, plat, out, phases)" in inspect.getsource(run.run_step)),
          ([("ok", ["log_ended", "log_written"], True), ("ok", ["log_ended", "log_written", "capture_extras"], True),
            ("SystemExit: selftest extras failed", ["log_ended", "log_written"], True)],
           [("LogDev", "com.lab.app", True)], True))

    def product_probe(source, probe):
        """A copy of this harness's top-level files in a temp dir (never its .claims, scenarios or variants: the probes
        only import), `source` as its lab_product.py (None: none; ("symlink", target): a link), and a fresh interpreter
        running `probe` there: (exit code, last stdout line, stderr)."""
        root_ = tempfile.mkdtemp(prefix="labkit-product-")
        try:
            harness_ = os.path.join(root_, "harness")
            os.makedirs(harness_)
            for name_ in os.listdir(HARNESS):
                if name_ != "lab_product.py" and os.path.isfile(os.path.join(HARNESS, name_)):
                    shutil.copy2(os.path.join(HARNESS, name_), harness_)
            if isinstance(source, tuple):
                os.symlink(source[1], os.path.join(harness_, "lab_product.py"))
            elif source is not None:
                with open(os.path.join(harness_, "lab_product.py"), "w") as f:
                    f.write(source)
            r = sp.run([sys.executable, "-B", "-c", probe], cwd=harness_, capture_output=True, text=True, timeout=120,
                       stdin=sp.DEVNULL, env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
            return r.returncode, (r.stdout.strip().splitlines() or [""])[-1], r.stderr
        finally:
            shutil.rmtree(root_, ignore_errors=True)
    product_src_ = "\n".join([
        "CALLS = []",
        "def register(labkit):",
        "    CALLS.append(labkit.__name__)",
        "    labkit.HEADLESS_NAMES = lambda: ['fetch', 'timeout']",
        "    labkit.METRICS['headlessFired.fetch'] = lambda c: 'own'",
        "    labkit.IMPLICIT['selftestImplicit'] = 'ok'",
        "    labkit.EARLY_METRICS.add('selftestImplicit')",
        "    labkit.EVIDENCE = lambda lines, step, os_name: any('selftest-settled' in l for l in lines)",
        "    labkit.SDK_LOG = lambda c, like: ['sdk ' + like]",
        "    def extras(dev, plat, out):",
        "        with open(out + '/extras.txt', 'w') as f:",
        "            f.write(plat['package'])",
        "    labkit.CAPTURE_EXTRAS = extras",
        "    def selftestImplicit(c):",
        "        return 'ok'",
        "    labkit.metric(selftestImplicit)",
        "    labkit.ACTION_HANDLERS['selftest_job'] = labkit.ActionHandler(lambda at, item, step: 0,",
        "                                                                 lambda dev, item, plat, server: 'selftest_job')",
        ""])
    # `import run` first: run imports backends, which imports labkit — the order a bare `python3 run.py` has.  The
    # settle line is not a LAB line, so it proves EVIDENCE is handed the lines as given, not evidence_state's filter.
    product_probe_ = "\n".join([
        "import json, os, tempfile",
        "import run, labkit, lab_product",
        "c = labkit.Capture.live([], {'os': 'android'})",
        "ls = ['09-21 10:00:01.000 I/LAB     ( 7): app=boot n=1', '09-21 10:00:02.000 I/LAB     ( 7): app=tick n=1']",
        "settled = ['09-21 10:00:03.000 I/ActivityManager(  674): selftest-settled']",
        "out = tempfile.mkdtemp()",
        "run.end_capture_log(type('D', (), {'end_log': lambda self, plat: ''})(), {'package': 'com.lab.app'}, out, {})",
        "print(json.dumps([os.path.relpath(labkit.PRODUCT.__file__, labkit.HARNESS), lab_product.CALLS,",
        "    run.labkit is labkit, labkit.headless_names(),",
        "    sorted(k for k in labkit.METRICS if k.startswith(('headlessEvents.', 'headlessFired.'))),",
        "    labkit.expectations({'expect': {'crashes': 0}}, 'react').get('selftestImplicit'),",
        "    'selftestImplicit' in labkit.EARLY_METRICS and 'selftestImplicit' in labkit.METRICS,",
        "    [labkit.evidence_state(ls, {}, 'android')[0], labkit.evidence_state(ls + settled, {}, 'android')[0]],",
        "    [c.sdk('%x%'), labkit.METRICS['headlessFired.timeout'](c), labkit.METRICS['headlessFired.fetch'](c)],",
        "    open(os.path.join(out, 'extras.txt')).read(), 'selftest_job' in run.labkit.ACTION_HANDLERS]))",
        ""])
    rc_, line_, err_ = product_probe(product_src_, product_probe_)
    check("(WO-191, D3) a lab_product.py beside labkit is loaded by labkit itself, registered once per labkit module, "
          "and every hook takes effect: the headless names (the metrics generated from them after register, and the "
          "product's own under a generated name kept), an implicit key that is early, the evidence predicate (handed "
          "the raw lines), the SDK log adapter (which the generated metrics read too), the capture extras through "
          "end_capture_log, a metric and an action kind",
          (rc_, json.loads(line_) if rc_ == 0 else err_[-600:]),
          (0, ["lab_product.py", ["labkit"], True, ["fetch", "timeout"],
               ["headlessEvents.fetch", "headlessEvents.timeout", "headlessFired.fetch", "headlessFired.timeout"],
               "ok", True, [False, True], [["sdk %x%"], 1, "own"], "com.lab.app", True]))
    none_ = product_probe(None, "import run, labkit; print(labkit.PRODUCT, 'headlessEvents.location' in labkit.METRICS)")
    broken_ = [(product_probe(src, probe), cause) for src, probe, cause in (
        ("raise RuntimeError('selftest product import')\n", "import run", "RuntimeError: selftest product import"),
        ("CALLS = []\n", "import run", "has no attribute 'register'"),
        ("def register(labkit):\n    raise SystemExit('selftest refuses')\n", "import run", "SystemExit: selftest refuses"),
        ("def register(labkit):\n    labkit.HEADLESS_NAMES = lambda: 1 / 0\n", "import run", "ZeroDivisionError"),
        ("import labkit\n\ndef register(labkit):\n    pass\n", "import lab_product",
         "lab_product imports labkit at module level"),
        (("symlink", "nowhere/lab_product.py"), "import run", "FileNotFoundError"))]
    check("(WO-191, D3) with none, the copy runs background-geolocation's seams; a product that does not load — its "
          "import raises, it has no register(), register() refuses, its HEADLESS_NAMES fails as the metrics are "
          "generated, it imports labkit at module level, or lab_product.py is a dangling link — stops the process with "
          "exit 2, naming the file and the cause",
          [none_[:2]] + [(rc, "did not load (WO-191)" in err and "lab_product.py" in err, cause in err)
                         for (rc, _, err), cause in broken_],
          [(0, "None True")] + [(2, True, True)] * 6)
    native_src_ = "\n".join([
        "def register(labkit):",
        "    labkit.NATIVE['android'].update(artifacts=('tsbackgroundfetch',),",
        "                                    gradle_task=':tsbackgroundfetch:publishToMavenLocal')",
        "    labkit.NATIVE['ios']['artifact'] = 'TSBackgroundFetch.xcframework'",
        ""])
    check("(WO-191, Part 5) a product's native descriptor, set in register(), is what inventory and lab_setup read in a "
          "fresh process",
          product_probe(native_src_, "import inventory, lab_setup; print(inventory.ANDROID_ARTIFACTS, "
                                     "lab_setup.native('ios')['artifact'], lab_setup.native('android')['gradle_task'])")[:2],
          (0, "('tsbackgroundfetch',) TSBackgroundFetch.xcframework :tsbackgroundfetch:publishToMavenLocal"))

    print("\nselftest: %s" % ("PASS" if not failures else "FAIL (%d)" % failures))
    return 0 if not failures else 1


# (WO-191, D3) The end of labkit's definitions: the product (none for background-geolocation), and the headless metrics
# generated from its names.
PRODUCT = _load_product(sys.modules[__name__])


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(_selftest())
    sys.exit("usage: labkit.py --selftest")
