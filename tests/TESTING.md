# Test recipes

## `ringer.py ask`

Status: tested

Purpose: test context-packet selection, one-worker execution, optional request
redaction, run state, artifact-library metadata, and the one-attempt contract.

Safe actions:

- Run the unit suite. Worker tests use temporary directories and local Python
  fixture workers.
- Run `ask --dry-run` against temporary text or Markdown sources.

Unsafe actions:

- Do not omit `--dry-run` from a smoke command unless a real model call is
  intended.

Verification steps:

1. Run `RINGER_NO_SELF_UPDATE=1 python3 -m unittest discover -s tests`.
2. Create a temporary Markdown source with a distinctive answer passage.
3. Run `RINGER_NO_SELF_UPDATE=1 python3 ./ringer.py ask "<question>" --source <temp-file> --dry-run`.
4. Make sure that the packet report names the source passage and stdout says
   `No model call was made.`
5. For a headless execution smoke test, use the mock engine and temporary
   configuration. Do not pass `--dashboard` or `--browser`.

Cleanup:

- Remove the temporary source and generated request directory when one was
  supplied explicitly.
- Keep all state and evidence paths inside the temporary test directory.

Known test-environment constraint:

- Some worker tests mock the dashboard socket bind because restricted test
  sandboxes can reject local listeners. Tests assert run state and artifact
  library metadata without requiring generated HTML. The production defaults
  for `ask` do not start a listener or browser. See [Evidence storage](../docs/EVIDENCE.md)
  for local evidence behavior.
