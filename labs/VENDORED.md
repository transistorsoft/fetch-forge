# Vendored from bg-forge (WO-002, decision D1)

The lab harness is bg-forge's LabKit, **vendored byte-identical**. Nothing listed here is edited in this repo.
When a fetch need cannot be met without changing one of these files, the change goes into bg-forge first
(a bg-forge work order), and the file is re-vendored from the new commit. Fetch's differences live in
one fetch-owned module: `labs/harness/fetch_pack.py`. It is described in `labs/README.md`, and
`python3 labs/harness/fetch_pack.py --seams` lists each vendored line it stands in for.

Commit: `49d38d47f8f73d45c155a24dc7802d4128216b5f` (bg-forge `master`, 2026-11-04).

Each file was extracted with `git -C <bg-forge> show 49d38d47f:<path>`, never copied from a working tree.
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
| `labs/harness/labkit.py` | `510706ff1ebcf2c24e8ff90fd611cdb9ebe7bc7452ded5a732c9682c542dce83` | 100644 | 11520 |
| `labs/harness/backends.py` | `23d3c44fcede2586d3daab5be73c2d6d45e0d8d4ce62d4c1de5929c7f75130a8` | 100644 | 1494 |
| `labs/harness/run.py` | `23054b58429403910f88ad4fa388b655859c48631ee792ff1e2a11c312c247a4` | 100755 | 1477 |
| `labs/harness/sweep.py` | `411d918230d34a63713a2c9d1848d016737ec94cb6105f33e10a7df6c0a4756f` | 100644 | 950 |
| `labs/harness/summarize.py` | `10429802d3418b52cbefe571aeeae01d2b1a3b420582b8c356e20dad7cb40782` | 100755 | 1403 |
| `labs/harness/devices.py` | `d05ba1b388365735069382884a81747f537bfd28bacff2cbe8959cf682e57b76` | 100644 | 210 |
| `labs/harness/lab_devices.py` | `c127fe6ebe18777a7123594357fff603065854d1928a55c261069026e40cd64c` | 100644 | 718 |
| `labs/harness/lab_setup.py` | `08376289c43054e28dce408171501aee15336cd4a7af072c588bcf3699bbc534` | 100644 | 514 |
| `labs/harness/inventory.py` | `7e3f852be36c6f6dc070172f5bbba216039dcfebb7befcb2d48c117283e907ef` | 100644 | 172 |
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
