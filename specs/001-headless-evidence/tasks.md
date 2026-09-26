# Tasks

- [x] T001: Record baseline and approved scope (R1-R6).
- [x] T002: Headless defaults; separate HTML from durable state/artifacts; add regression tests and documentation (R1,R2,R3,R6). Prerequisite T001. Owned ringer.py, config.sample.toml, README.md, tests/.
- [x] T002a: Owner-approved follow-up for F1/F5: publish only successfully generated HTML paths and handle metadata-only runs in optional Ringside, with regression tests (R1,R2,R6). Owned ringer.py, dashboard/ringside.html, tests/. Depends on delivered T002 corrections; requires separate quality review before T002 acceptance.
- [x] T003: Extract/harden evidence storage with concurrent-write/error tests (R3,R4,R5). Prerequisite T002 accepted.
- [x] T004: Extract context, configuration/manifests, verification sequentially (R5). Prerequisite T003 accepted.
- [x] T005: Extract execution/state/artifacts and optional analytics/presentation (R5). Prerequisite T004 accepted.
  - [x] T005a: Extract 14 unchanged worker-runtime primitives into runtime.py; see t005.md frozen scope.
  - [x] T005b: Extract state-file persistence and worker logs unchanged; frozen scope in t005b.md.
  - [x] T005c: Extract artifact storage unchanged; frozen scope in t005c.md.
  - [x] T005d: Extract artifact rendering unchanged; frozen scope in t005d.md.
  - [x] T005e: Extract StateWriter unchanged; frozen scope in t005e.md.
  - [x] T005f: Extract catalog unchanged; frozen scope in t005f.md.
  - [x] T005g: Extract model analytics, preserving resource roots; scope in t005g.md.
  - [x] T005h: Extract existing SQLite read model unchanged; scope in t005h.md.
  - [x] T005i: Extract models API and scoreboard views unchanged; scope in t005i.md.
  - [x] T005j: Extract optional HTTP presentation preserving resource roots; scope in t005j.md.
  - [x] T005k: Owner approved required logger keyword and initialization order; lead-owned composition, bounded extraction in t005k.md.
- [x] T006: Full verification, final quality review, and docs (R6). Prerequisite integrated changes.

The initial T002 wave exhausted its remediation/delta-review limits. User explicitly approved a new bounded T002a follow-up for the two remaining cases and leaving baseline contributor credits documented. T002/T002a are locally accepted after independent review and owner-approved B1 baseline exception. T003 is locally accepted after integrated checks and read-only delta review; see t003.md and t003-review.md. T004 is locally accepted after exact-body comparison, integrated tests, and separate quality review; see t004.md. T005a is locally accepted after parent checks and separate review; T005b is locally accepted after independent review; T005c artifact storage is locally accepted after parent verification and separate quality review; T005d artifact views is locally accepted after checks and separate review; T005e StateWriter is locally accepted after checks and separate review; T005f catalog is locally accepted after checks and separate review; T005g model analytics is locally accepted after checks and separate review; T005h SQLite extraction is locally accepted after bounded remediation/delta review; T005i models API/scoreboard is locally accepted after checks and separate review; T005j HTTP presentation is locally accepted; T005k and aggregate T005 are locally accepted after independent review; T006 final docs and whole-change review accepted locally with documented B1 exception; all approved implementation tasks complete; remaining T005 subwaves are ordered in t005.md and need fresh bounded dispatch; T006 remains final verification. Later tasks require accepted prerequisites and a fresh bounded dispatch.
