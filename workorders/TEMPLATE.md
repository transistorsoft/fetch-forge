---
id: WO-NNN
title: <one line, declarative — state the defect or the change, not "fix X">
status: draft
created: YYYY-MM-DD
platforms: [ios, android]          # or [android], [ios], [docs], [tooling]
submodules: [native/ios, flutter/background_geolocation]
branch: fix/WO-NNN-<slug>
issues: []                         # transistorsoft/flutter#1652 — customer-facing, if any
supersedes: []                     # WO ids this replaces
---

## Diagnosis

<The root cause. The specific code path that fails and why. Cite file:line. If this
came from a customer log or issue, cite it. State what is PROVEN vs INFERRED —
mark inferences explicitly, they are where the next session gets misled.>

## Proposed fix

<Plain English. Which files, which methods, what the change does. No code here
unless a specific expression is the point of the change.>

## Scope check

<What else calls this code path? Could the fix break existing behaviour or other
callers? Name them with file:line. "Nothing else calls it" is an acceptable answer
only if you looked.>

## Platform parity

- **iOS:** <file:line — affected the same way / not affected because …>
- **Android:** <file:line — affected the same way / not affected because …>

<Citation required on both sides, or an explicit "N/A because". An unsupported
"Android is fine" is the single most common way a half-fix ships.>

## Test plan

<Existing tests, new tests, and the on-device protocol if it needs one. Name the
test or card file that will carry the WO id.>

## Out of scope

<What this change must not grow into. Adjacent defects get their own work order.>

## Not an acceptable shortcut

<The plausible-but-wrong routes, named in advance, with why they fail. Examples of
the genre from past work:
- "Just call reEvaluate()" — rejected: reEvaluate is itself racy.
- Retry-after-reset on GEOFENCE_NOT_AVAILABLE — infinite-loop risk; do not retry
  that shape.
- The callback reports onSuccess deliberately (88ba211f, 2023) — do not "fix" it.
Write these even when they feel obvious. They are what stop a fresh session from
confidently doing the wrong thing.>

## Verification

- [ ] Unit/integration tests pass
- [ ] On-device: <device, protocol, expected observation>
- [ ] Platform parity confirmed on the second platform
- [ ] Customer issue replied to (if `issues:` is non-empty)

## Log

- YYYY-MM-DD — created
