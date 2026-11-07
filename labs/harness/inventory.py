#!/usr/bin/env python3
"""What the lab holds, what still names it, and what can go.

    python3 labs/harness/inventory.py           # report: snapshot labels and variants
    python3 labs/harness/inventory.py --prune   # delete snapshot artifacts nothing names

A snapshot label (publish_native.sh -> m2/, publish_native_ios.sh -> xcframeworks/) is in use
while a variant names it or a capture on disk records it in run.json `native`.  Only a label
that NOTHING names is pruned, and its `.provenance/<label>.txt` is kept regardless: a few
hundred bytes that are the only record of which commit the label was — a capture that names
a pruned label can still say what it ran, and publish_native*.sh can rebuild it from there.

A variant is runnable when its label exists for the OS and its plugin checkout exists for the
platform.  A dead one is reported, never deleted: summarize.py re-scores a capture through its
variant (must_fail inverts the verdict), so removing the YAML would orphan that evidence.  Mark
it `retired: "<date> — why"` instead, which run.py refuses with the reason.
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys

import labkit
from run import expand

M2 = os.path.join(labkit.LABS, "m2", "com", "transistorsoft")
ANDROID_ARTIFACTS = tuple(labkit.NATIVE["android"]["artifacts"])     # (WO-191) the product's, from its descriptor
XCFW = os.path.join(labkit.LABS, "xcframeworks")


def android_labels():
    root = os.path.join(M2, ANDROID_ARTIFACTS[0])
    return {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))} if os.path.isdir(root) else set()


def ios_labels():
    return {d for d in os.listdir(XCFW) if not d.startswith(".") and os.path.isdir(os.path.join(XCFW, d))} \
        if os.path.isdir(XCFW) else set()


def size(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        total += sum(os.path.getsize(os.path.join(dirpath, f)) for f in files)
    return total


def variants():
    out = {}
    for path in sorted(glob.glob(os.path.join(labkit.HARNESS, "variants", "*.yaml"))):
        name = os.path.basename(path)[:-5]
        try:
            out[name] = labkit.variant(name)
        except SystemExit as e:
            # (Tier C, C3.1) One that no longer loads (its must_fail names a scenario or step since renamed)
            # is dead, and reported here, not a reason to stop: its YAML as written still names its label,
            # so --prune keeps that.
            out[name] = dict(labkit._load(path), name=name, broken=str(e))
    return out


def captures():
    """label -> number of captures ON DISK naming it (committed or not: never prune under a run)."""
    counts = {}
    for path in glob.glob(os.path.join(labkit.LABS, "captures", "**", "run.json"), recursive=True):
        try:
            label = json.load(open(path)).get("native")
        except (OSError, ValueError):
            continue
        if label:
            counts[label] = counts.get(label, 0) + 1
    return counts


def plugin_status(var):
    """Per platform: the checkout the variant's `plugin` resolves to, and whether it is there."""
    out = {}
    choice = var.get("plugin", "worktree")
    for name in labkit.platforms():
        plat = labkit.platform(name)
        path = (plat.get("plugins") or {}).get(choice)
        if path is None:
            out[name] = "unmapped"
            continue
        path = expand(path)
        if path == "default":
            path = expand(plat.get("default_checkout", ""))
        out[name] = "ok" if path and os.path.isdir(path) else "missing"
    return out


def prune_android(label):
    for artifact in ANDROID_ARTIFACTS:
        shutil.rmtree(os.path.join(M2, artifact, label), ignore_errors=True)
        meta = os.path.join(M2, artifact, "maven-metadata-local.xml")
        if os.path.exists(meta):
            text = open(meta).read()
            text = re.sub(r"\s*<version>%s</version>" % re.escape(label), "", text)
            open(meta, "w").write(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prune", action="store_true", help="delete snapshot artifacts nothing names")
    args = ap.parse_args()

    vs = variants()
    named = {}
    for vname, var in vs.items():
        named.setdefault(var.get("native"), []).append(vname)
    caps = captures()
    andr, ios = android_labels(), ios_labels()

    print("== snapshot labels")
    print("   %-28s %-14s %-9s %s" % ("label", "on disk", "captures", "variants"))
    doomed = []
    for label in sorted(andr | ios | set(named) | set(caps)):
        where = "+".join(o for o, s in (("android", andr), ("ios", ios)) if label in s) or "MISSING"
        mb = (size(os.path.join(M2, ANDROID_ARTIFACTS[0], label)) * len(ANDROID_ARTIFACTS) if label in andr else 0) \
            + (size(os.path.join(XCFW, label)) if label in ios else 0)
        users = ", ".join(named.get(label, [])) or "-"
        unused = label not in named and not caps.get(label) and where != "MISSING"
        if unused:
            doomed.append(label)
        print("   %-28s %-14s %-9s %-40s %5.1f MB%s" % (label, where, caps.get(label, 0), users,
                                                       mb / 1e6, "   <- nothing names it" if unused else ""))

    print("\n== variants")
    for vname, var in vs.items():
        label = var.get("native")
        plugins = plugin_status(var)
        problems = []
        if var.get("broken"):
            print("   %-20s %-26s %s" % (vname, label, var["broken"]))
            continue
        if var.get("retired"):              # the reason already names what is missing
            print("   %-20s %-26s retired: %s" % (vname, label, var["retired"]))
            continue
        if label not in andr:
            problems.append("no android snapshot %s" % label)
        if label not in ios:
            problems.append("no ios snapshot %s" % label)
        missing = sorted(p for p, s in plugins.items() if s == "missing")
        if missing:
            problems.append("plugin '%s' checkout missing for %s" % (var.get("plugin", "worktree"), ", ".join(missing)))
        # A mutation is a source edit made BY HAND before the build (run.py never applies one):
        # unapplied, the variant builds exactly what FIX builds and its must_fail goes red.
        if var.get("mutation") and not problems:
            problems.append("runnable once applied by hand: %s" % var["mutation"])
        print("   %-20s %-26s %s" % (vname, label, "; ".join(problems) or "runnable"))

    if not doomed:
        print("\nnothing to prune")
        return 0
    if not args.prune:
        print("\n%d label(s) nothing names: %s — run with --prune to delete (provenance is kept)"
              % (len(doomed), ", ".join(doomed)))
        return 0
    for label in doomed:
        if label in andr:
            prune_android(label)
        if label in ios:
            shutil.rmtree(os.path.join(XCFW, label))
        print("pruned %s (provenance kept)" % label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
