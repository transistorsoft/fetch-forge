# Vendored from bg-forge (WO-002, decision D1)

The lab harness is bg-forge's LabKit, **vendored byte-identical**. Nothing listed here is edited in this repo.
When a fetch need cannot be met without changing one of these files, the change goes into bg-forge first
(a bg-forge work order), and the file is re-vendored from the new commit. Fetch's differences live in
one fetch-owned module: `labs/harness/fetch_pack.py`. It is described in `labs/README.md`, and
`python3 labs/harness/fetch_pack.py --seams` lists each vendored line it stands in for.

Commit: `76f6536ac5cb5516a5d96c72a727d76724a53ad7` (bg-forge `master`, 2026-11-10: the merge of WO-192, a variant's
`channel:` reaching builds and source stamps, for fetch WO-002's D4).  Only WO-192 changed vendored files since
the previous commit, `23f448c42` (WO-191 Parts 1–5, the seams this pack had worked around), which replaced 49d38d47f.

Each file was extracted with `git -C <bg-forge> show <commit>:<path>`, never copied from a working tree.
bg-forge's working tree has unrelated local edits.

**Check:** `labs/harness/vendor_check.sh` (also run by `./labkit selftest`). It re-extracts every row
below from bg-forge at the commit, and fails on any byte, sha256 or mode difference. It also fails on a
file that is listed in neither table, but it scans only `labkit`, `labs/harness/` and
`labs/platforms/_shared/`: the platform apps under `labs/platforms/<name>/` (fetch-owned, modelled on
bg-forge's) and the fetch-owned rows for `labs/README.md` and `labs/.gitignore` are outside that scan.
bg-forge is found at `$BG_FORGE`, or else beside the main fetch-forge checkout.

## Vendored files

| Path | sha256 | mode | lines |
|---|---|---|---|
| `labs/harness/labkit.py` | `da0764d58c83128276557e7197b8120312d129efec5ba889ba8f51e5c1fa6ef5` | 100644 | 12311 |
| `labs/harness/backends.py` | `4179d0d0654daf3bc75beb7d0b0440e944784e516fe3bbac794c0714f9d086e1` | 100644 | 1519 |
| `labs/harness/run.py` | `867eeaef264f18229e2f772f336aa0d42f3521bdf83910242bedab4a2d7860b8` | 100755 | 1494 |
| `labs/harness/sweep.py` | `825e68d93d260883e6a00e7b469df6e0843a1ed8d61e0ffd79a37ed8cf50b239` | 100644 | 959 |
| `labs/harness/summarize.py` | `d9e9b11d50cc48ff61e58528752d43e4757da3f7d3948877199e91239ddb66ed` | 100755 | 1405 |
| `labs/harness/devices.py` | `e70c5cb78b5bbc47e33568f2cc6f095f4abacde733dc88987f93e1d1145b513b` | 100644 | 231 |
| `labs/harness/lab_devices.py` | `245467c04cfd7965bea92cf812e5381793ed8beddcaf6e00bf5f6471f8867990` | 100644 | 736 |
| `labs/harness/lab_setup.py` | `75b024518a92586ee391a15a7f3028ec46e590c9792278353821c441a0b4673c` | 100644 | 559 |
| `labs/harness/inventory.py` | `6ed4f61f7040f788a5123cadec49231672021dad4f00f104995f1853ae9ae715` | 100644 | 172 |
| `labs/harness/slots_selftest.js` | `0df29055c3284069a87a58df5526c96d16439e1f865a83595fb4e6687f7023f2` | 100644 | 433 |
| `labs/harness/avd/lab.config.ini` | `5f24bf72174ae6a75b19db55897696cc9a51f097efebaeff7c7f2c13dbb48bf9` | 100644 | 67 |
| `labs/harness/avd/windows.swift` | `bdc283e5c5533b2d2c8b6b498668431f5e62cc7058e276ab8ffe9bf87eb32668` | 100644 | 22 |
| `labs/harness/lab-publish.init.gradle` | `7d1e678b531bcdcf0656f70ee90997f3a9d887ce0becaad2607e6b23c3317550` | 100644 | 11 |
| `labs/platforms/_shared/labapi.js` | `fded75628990f3d18b14fabf12512368fbd2d3a232495ba94717e9e9da39e4c2` | 100644 | 518 |
| `labs/platforms/_shared/labevents.js` | `9f787a69a4644199992396d5ba180c29d7c63add894b3ef2d6824793ea60ba8e` | 100644 | 243 |
| `labs/platforms/_shared/logo.js` | `42e7867f400f01536fd49b1c4b0a95dfdcec1c3d010c7e88c902794dd8eb0a28` | 100644 | 10 |

`lab-publish.init.gradle` was vendored by the native-pinning slice, for `publish_native_fetch.sh`. It
is listed here so that the check holds it like the rest.

## Fetch-owned files at a vendored path

These paths also exist in bg-forge at the commit, but the file here is fetch's own. They are never
compared with bg-forge.

| Path | Why fetch owns it |
|---|---|
| `labkit` | The root CLI: modelled on bg-forge's, with fetch's commands (`pack`, `env`) and its selftest |
| `labs/README.md` | Fetch's README; it points to bg-forge's for the protocol |
| `labs/.gitignore` | Modelled on bg-forge's |
| `labs/harness/vocabulary.yaml` | Fetch's API surface, seeded from `types/background-fetch`. It is never a copy of bg-geo's |
| `labs/harness/variants/FIX.yaml` | Fetch's FIX variant: the `tsbackgroundfetch` label in `labs/m2` and its native commit |
| `labs/harness/scenarios/api-smoke.yaml` | Fetch's api-smoke: the seven members of `types/background-fetch`, not bg-geo's |

## Fetch-owned files the vendored code loads

| Path | Why |
|---|---|
| `labs/harness/lab_product.py` | bg-forge WO-191 (D3): the vendored `labkit.py` loads it, when present, at the end of its own definitions and calls its `register(labkit)`, which hands labkit fetch's hooks (`fetch_pack.register`). bg-forge has none, so it is at no bg-forge path and the scan above does not see it |

## Not vendored (bg-geo data, or bg-geo builds)

- `labs/harness/vocabulary.yaml` (bg-geo's), `scenarios/`, `variants/`
- `typed_config.py` / `.yaml`, `native_members.py` / `.yaml`, `dart_members.py` / `.yaml`
- `roundtrip.yaml`, `invalid.yaml`, `seed_vocabulary.py`
- `publish_native.sh`, `publish_native_ios.sh`, `build_native_ios.sh`
- `labs/platforms/_shared/labstate.js`, which renders bg-geo's State

The vendored modules import none of these except inside `labkit._selftest()`. That selftest is bg-geo's
6,700-line offline suite of 727 checks over bg-geo's scenarios and tables, and fetch does not run it.
`./labkit selftest` runs `labs/harness/fetch_selftest.py`, the vendored `slots_selftest.js` and
`vendor_check.sh`.

## Re-vendoring

1. Land the change in bg-forge.
2. Pick the new commit.
3. For each row, run `git -C <bg-forge> show <commit>:<path> > <path>`, keeping the mode.
4. Update the commit and every sha256 above.
5. Run `./labkit selftest`.

Then check every seam that `fetch_pack.py --seams` names against the new line numbers. A seam that
bg-forge has grown is an override to delete.
