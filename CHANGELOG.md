# Changelog

## 0.1.0

- Split Seedance 2.5 from Seedance 2.0 into an independent custom node package.
- Added `VergoraSeedance25` without exposing Seedance 2.0 model choices.
- Added 30-second generation, 30/10/10 media limits, MOV request support,
  reference/edit/extend task types, and the two-stage Draft workflow.
- Added local validation for keyframes, references, editing, extension, Draft,
  and final-from-Draft requests.
- Added flat and native `content[]` request formats with safe one-time schema
  fallback only.
- Added asynchronous polling, interruption, verified downloads, last-frame
  download, and ComfyUI video preview.
- Detect gateway Draft loss (`draft=false`) and stop before mislabeling a normal
  480p result.
- Added specific diagnostics for current gateway limitations affecting Draft,
  MOV, first-frame, and edit/extend tasks.
- Added offline tests and example workflows.
