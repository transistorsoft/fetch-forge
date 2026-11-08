# The fetch pack's activation shim (WO-002; labs/harness/fetch_pack.py, labs/README.md "The pack").
#
# `./labkit` puts this directory first on PYTHONPATH and sets FETCH_LABKIT_PACK=1, so every Python process the harness
# starts — `run.py` from sweep.py:644, lab_setup.py's re-exec, its `lab_devices.py status` probe — runs this at
# startup, and the vendored modules are patched before any of them is used.  Subprocesses inherit both variables.
#
# Python imports ONE sitecustomize, the first on sys.path, and this one is first: so it runs the interpreter's own
# next (Homebrew's sets sys.executable and its site-packages; a venv's base has the same file), then the pack.
# A pack that was asked for and does not load stops the process (exit 2, "could not be judged"): the harness must
# never run with bg-geo's seams under a fetch lab.
import importlib.util as _util
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.realpath(__file__))

# The interpreter's own is the first sitecustomize.py on sys.path that is not a fetch pack's.  Another fetch pack's shim
# is skipped, never chained: another checkout's (PYTHONPATH still holding it after `./labkit env` there, with `./labkit`
# here putting this one first), or this one reached through a symlink.  Chained, it chained this one back, and every
# harness process died of RecursionError at startup.  Real paths, so a symlinked spelling is still this one.
for _p in list(_sys.path):
    _candidate = _os.path.join(_p or _os.getcwd(), "sitecustomize.py")
    _dir = _os.path.dirname(_os.path.realpath(_candidate))
    if (_dir == _HERE or not _os.path.isfile(_candidate)
            or _os.path.isfile(_os.path.join(_os.path.dirname(_dir), "fetch_pack.py"))):   # a fetch pack's own shim
        continue
    try:
        _spec = _util.spec_from_file_location("_fetch_chained_sitecustomize", _candidate)
        _chained = _util.module_from_spec(_spec)
        _spec.loader.exec_module(_chained)
    except Exception as _e:                # the interpreter's own: reported, as site.py would, and the pack still loads
        _sys.stderr.write("Error in %s (chained by the fetch pack): %s: %s\n" % (_candidate, type(_e).__name__, _e))
    break

if _os.environ.get("FETCH_LABKIT_PACK") == "1":
    try:
        _spec = _util.spec_from_file_location("fetch_pack", _os.path.join(_os.path.dirname(_HERE), "fetch_pack.py"))
        _pack = _util.module_from_spec(_spec)
        _sys.modules["fetch_pack"] = _pack
        _spec.loader.exec_module(_pack)
        _pack.activate()
    except Exception as _e:
        _sys.stderr.write("fetch pack: failed to load (%s: %s) — refusing to run the harness without it\n"
                          % (type(_e).__name__, _e))
        _sys.stderr.flush()
        _os._exit(2)
