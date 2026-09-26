from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "dashboard" / "ringside.html"


class RingsideLibraryViewTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to exercise the dashboard view helpers")
    def test_metadata_only_and_mixed_history_expose_only_existing_html_views(self) -> None:
        html = PAGE.read_text(encoding="utf-8")
        start = html.index("    function artifactViewChoices(")
        end = html.index("    function syncArtifactSelection()", start)
        helpers = html[start:end]
        target_start = html.index("    function selectedArtifactTarget()", end)
        target_end = html.index("    function renderArtifacts()", target_start)
        target_function = html[target_start:target_end]
        checks = r"""
const assert = require('node:assert/strict');
let state = {artifactVersion: 'live'};
let selected = null;
function selectedArtifact() { return selected; }
function artifactPathHref(path) { return path; }
const metadataOnly = {
  live_path: '',
  versions: [{key: 'current', path: ''}]
};
assert.deepEqual(artifactViewChoices(metadataOnly), []);
assert.equal(artifactViewPath(metadataOnly, 'live'), '');
selected = metadataOnly;
assert.equal(selectedArtifactTarget(), null);
const mixedHistory = {
  live_path: '',
  versions: [
    {key: 'current', path: ''},
    {key: 'prior', path: '/state/artifacts/prior.html'}
  ]
};
assert.deepEqual(artifactViewChoices(mixedHistory).map(view => view.key), ['prior']);
assert.equal(artifactViewPath(mixedHistory, 'live'), '');
assert.equal(artifactViewPath(mixedHistory, 'prior'), '/state/artifacts/prior.html');
selected = mixedHistory;
syncArtifactVersion(mixedHistory);
assert.equal(state.artifactVersion, 'prior');
assert.equal(selectedArtifactTarget().src, '/state/artifacts/prior.html');
const presented = {
  live_path: '/state/artifacts/live/Run.html',
  versions: [{key: 'prior', path: '/state/artifacts/prior.html'}]
};
assert.deepEqual(artifactViewChoices(presented).map(view => view.key), ['live', 'prior']);
"""
        proc = subprocess.run(
            ["node", "-e", helpers + "\n" + target_function + "\n" + checks],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
