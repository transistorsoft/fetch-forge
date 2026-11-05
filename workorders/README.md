# Work orders

A work order is one unit of intended change, written down before it is built, and
given a number that everything else hangs off.

The problem it solves: investigation happens in a Claude Code session, the session
ends, and what survives is a memory note reading *"fix COMMITTED 9212901 (unpushed),
field-test + issue reply pending."* The reasoning, the rejected approaches, and the
verification plan are gone. The next session re-derives them, or worse, doesn't.

## The anchor rule

Every work order gets an id — `WO-007` — and that id appears in **five** places:

| Place | Form |
|---|---|
| The work order file | `workorders/WO-007-stationary-nil-region.md` |
| The branch | `fix/WO-007-stationary-nil-region` |
| The code, at the lines changed | `// (WO-007)` |
| The test or demo-app card | `test_wo_007_...` / `WO007Card` |
| The changelog entry | `... (WO-007)` |

That is the whole discipline. It means any of the five gets you to the other four —
six months later, from a stack trace, from a customer log, from a changelog line.

**Work orders are private.** They live in this repo, not in the public
`transistorsoft/*` issue trackers, where self-filed work orders would read to paying
customers as a defect count. When a work order corresponds to a customer-reported
GitHub issue, cite the issue in the work order (`Fixes: transistorsoft/flutter#1652`)
— never the reverse.

## Status lifecycle

`draft` → `approved` → `in-progress` → `verify` → `done`, or `wont-fix`

`approved` means Chris said go. Per CLAUDE.md, no code is written before that.
`verify` means the code is committed but the on-device proof is still outstanding —
this is the state the memory notes keep getting stuck in, and now it is visible.
`wont-fix` means Chris decided the behaviour stays. The work order is kept, not deleted: its
Diagnosis and the reason in its Log are what stop the next session re-opening it.

## Anatomy

The template mirrors the CLAUDE.md bug-fix workflow one-to-one, because that ritual
is already right; it just needed somewhere to live:

1. **Diagnosis** — the failing code path, and why it fails
2. **Proposed fix** — plain English, which files, which methods
3. **Scope check** — what else calls this
4. **Platform parity** — file:line on *both* platforms, or an explicit "N/A because"
5. **Test plan** — how it gets verified
6. *(approval gate)*

Plus three sections the Tracelet forensics showed are what actually keep an agent
on the rails:

- **Out of scope** — what this change must not grow into
- **Not an acceptable shortcut** — the plausible-but-wrong routes, named in advance.
  This is the highest-value section. Every "ABANDONED: …", "PROVEN unsurvivable",
  "that is ON PURPOSE — do not fix it" currently scattered through memory belongs
  here, where the next session reads it *before* re-attempting the dead end.
- **Log** — dated one-liners; what actually happened, including reversals

## Usage

```
/workorder                 # write one from the current investigation
/workorder-execute WO-007  # implement an approved one
```

Numbering is sequential. `ls workorders/` and take the next.
