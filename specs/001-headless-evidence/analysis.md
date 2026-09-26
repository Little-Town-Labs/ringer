# Pre-implementation analysis

headless-review (fresh standalone openai-codex/gpt-6-astra medium) found no material specification/plan/task contradictions. Lead accepts T002 with these clarifications:

- Cover run/demo and ask paths.
- Explicit opt-in: --browser selects per-run browser presentation; add --dashboard for normal Ringside opt-in where necessary. Explicit disable flags win. Existing artifact configuration may suppress HTML; opting into a dashboard must not override explicit artifact disable.
- HTML disabled by default does not disable durable JSON library updates or deliverable harvesting. JSON metadata must not claim nonexistent HTML pages.
- Worker-produced HTML is a deliverable, not Ringer-generated presentation, and must still be preserved.
- Regression tests cover defaults and explicit presentation, including worktree rescue.

## Graph evidence update
Although graph tools are not exposed as native tools, codebase-memory-mcp CLI is installed. Indexing the task worktree failed in a contained worker crash. The existing home-powerbox2-src-ringer index reports ready at the exact base a8c47d153320c2c87af68b49e543e16045daca4b (2,387 nodes / 10,032 edges). search_graph returns StateWriter and run_one_request, consistent with targeted source reads at ringer.py:2194 and :10300. Code is unchanged from that base at analysis acceptance. This satisfies the evidence gate without claiming a new worktree index succeeded.

Classification: brownfield/system/routine. Selected T002 only, prerequisite T001 accepted. Route: mutable-delegation via local ringer-delivery skill using Herdr, one sequential writer in the existing isolated task worktree. No duplicate dispatch. Full extraction stays out of this wave.
