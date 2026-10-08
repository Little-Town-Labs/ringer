from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "scripts" / "central-evidence" / "grafana-dashboard.json"


class GrafanaDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))

    def test_panel_ids_are_unique(self) -> None:
        ids = [panel["id"] for panel in self.dashboard["panels"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_panel_uses_the_datasource_variable(self) -> None:
        for panel in self.dashboard["panels"]:
            self.assertEqual("${ds}", panel["datasource"]["uid"], panel["title"])
            for target in panel["targets"]:
                self.assertEqual("${ds}", target["datasource"]["uid"], panel["title"])

    def test_queries_read_only_the_attempts_table(self) -> None:
        for panel in self.dashboard["panels"]:
            for target in panel["targets"]:
                sql = target["rawSql"]
                self.assertIn("ringer.attempts", sql, panel["title"])
                self.assertIsNone(re.search(r"\b(insert|update|delete|drop|alter|truncate)\b", sql, re.I), panel["title"])

    def test_filter_variables_exist_for_every_filter_used(self) -> None:
        names = {variable["name"] for variable in self.dashboard["templating"]["list"]}
        used = {name for panel in self.dashboard["panels"] for target in panel["targets"]
                for name in re.findall(r"\$(host|model|task_type)\b", target["rawSql"])}
        self.assertTrue(used <= names, used - names)

    def test_contains_no_environment_specific_values(self) -> None:
        text = DASHBOARD.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\.ts\.net|/home/", text))


if __name__ == "__main__":
    unittest.main()
