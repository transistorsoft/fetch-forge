"""fetch-forge's product hook (bg-forge WO-191, D3): the vendored labkit.py loads this file itself, by path, at the end
of its own definitions, and calls register(labkit).  Not vendored: bg-forge has no lab_product.py (labs/VENDORED.md).

register() puts fetch's labkit seams in place: fetch_pack.register(), the WO-191 hooks.  It refuses (labkit then exits
2, "did not load") a process the fetch pack is not active in.  The pack's other overrides — backends, run, sweep,
lab_setup — are applied by its activation (labs/harness/pack/sitecustomize.py, which ./labkit sets up with
FETCH_LABKIT_PACK=1), not here: register() runs inside labkit's import, while backends.py may still be loading (bg-forge
WO-191 Part 3, narrowed).  A process without the activation would run fetch's labkit seams beside bg-geo's backends,
run and sweep: never a result.  Run the harness through ./labkit, or `eval "$(./labkit env)"` first.

It never imports labkit (labkit is loading while this runs): register() is handed it.
"""
import os
import sys


def register(labkit):
    pack = sys.modules.get("fetch_pack")
    if os.environ.get("FETCH_LABKIT_PACK") != "1" or pack is None or not pack.active():
        raise SystemExit("the fetch pack is not active in this process (FETCH_LABKIT_PACK=%s): its backends, run, sweep "
                         "and lab_setup seams would be bg-geo's — run the harness through ./labkit, or "
                         "`eval \"$(./labkit env)\"` first" % (os.environ.get("FETCH_LABKIT_PACK") or "unset"))
    # The active pack must be THIS harness's: its import hook patches only modules beside its own fetch_pack.py, so
    # another checkout's (a shell that ran `./labkit env` elsewhere) would leave this one's backends and run bg-geo's.
    here = os.path.dirname(os.path.realpath(__file__))
    if os.path.realpath(getattr(pack, "HARNESS", "") or "") != here:
        raise SystemExit("the active fetch pack is another checkout's (%s), not this harness's (%s): run this "
                         "checkout's ./labkit, or `eval \"$(./labkit env)\"` here" % (pack.HARNESS, here))
    pack.register(labkit)
