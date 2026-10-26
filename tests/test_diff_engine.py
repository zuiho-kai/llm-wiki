"""Unit tests for bin/diff_engine.py: deterministic checks on small diffs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import diff_engine as de  # noqa: E402


def change(text, sids=("12345678",), file="misc/demo.md", line=3, sha="abc1234"):
    return {"sha": sha, "subject": f"ingest {' '.join(sids)} demo", "file": file,
            "line": line, "text": text, "sids": list(sids)}


class ParseTest(unittest.TestCase):
    def test_parse_git_show_keeps_file_line_and_sids(self):
        text = """=== abc1234 ingest 12345678 Demo session
diff --git a/misc/demo.md b/misc/demo.md
index 111..222 100644
--- a/misc/demo.md
+++ b/misc/demo.md
@@ -2,0 +3,2 @@
+- 用户定：演示库用 MySQL「demo uses MySQL」
+- 端口 2222
=== def5678 ingest 87654321 Other
diff --git a/infra/server.md b/infra/server.md
--- a/infra/server.md
+++ b/infra/server.md
@@ -10 +10 @@
-old line
+SSH port 2222
"""
        changes = de.parse_git_show(text)
        self.assertEqual(len(changes), 3)
        self.assertEqual((changes[0]["file"], changes[0]["line"], changes[0]["sids"]),
                         ("misc/demo.md", 3, ["12345678"]))
        self.assertEqual((changes[1]["line"], changes[2]["file"], changes[2]["line"]),
                         (4, "infra/server.md", 10))

    def test_parse_skips_headings_and_frontmatter(self):
        text = """=== abc1234 ingest 12345678 Demo
diff --git a/misc/demo.md b/misc/demo.md
--- a/misc/demo.md
+++ b/misc/demo.md
@@ -1,0 +2,4 @@
+## Key facts
+name: demo
+- a real fact
+---
"""
        self.assertEqual([c["text"] for c in de.parse_git_show(text)], ["- a real fact"])


class DeterministicTest(unittest.TestCase):
    TEXTS = {"12345678": "用户说 demo 用 SQLite。\nCLAUDE: 好的。\n用户说先跑 lint 再提交。"}

    def test_fabricated_quote_is_caught(self):
        c = de.check_changes([change("- 用户说「demo 用 MySQL」")], self.TEXTS)[0]
        self.assertFalse(c["deterministic_ok"])
        self.assertIn("quote not found", c["checks"][0]["detail"])

    def test_attribution_without_quote_is_caught(self):
        c = de.check_changes([change("- 用户定：演示库用 MySQL")], self.TEXTS)[0]
        checks = {x["check"]: x for x in c["checks"]}
        self.assertFalse(checks["attribution"]["ok"])
        self.assertIn("no 「」 quote", checks["attribution"]["detail"])

    def test_matching_quote_passes(self):
        c = de.check_changes([change("- 用户说「先跑 lint 再提交」")], self.TEXTS)[0]
        self.assertTrue(c["deterministic_ok"])

    def test_ellipsis_quote_passes(self):
        c = de.check_changes([change("- 用户说「demo 用 … SQLite」")], self.TEXTS)[0]
        self.assertTrue(c["deterministic_ok"])

    def test_single_character_quote_is_too_short_to_verify(self):
        # "用" is in the transcript, but one common character proves nothing.
        c = de.check_changes([change("- 用户说「用」")], self.TEXTS)[0]
        self.assertFalse(c["deterministic_ok"])
        self.assertTrue(any("too short" in x["detail"] for x in c["checks"]))
        self.assertFalse(de.quote_matches("用", self.TEXTS["12345678"]))
        self.assertTrue(de.quote_matches("demo 用 SQLite", self.TEXTS["12345678"]))

    def test_explicit_missing_sid_does_not_fall_back_to_another_session(self):
        c = de.check_changes([change("- 用户说 1234abcd「demo 用 SQLite」")], self.TEXTS)[0]
        self.assertFalse(c["deterministic_ok"])
        self.assertIn("quote not found", c["checks"][0]["detail"])

    def test_conflicting_numbers_on_two_pages(self):
        changes = [change("- 演示服务器 SSH 端口 2222")]
        conflicts = de.find_conflicts(changes, {"infra/server.md": "## Key facts\n- 演示服务器 SSH 端口 8080\n"})
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["kind"], "similar")

    def test_conflicting_key_values(self):
        changes = [change("- SSH port: 2222")]
        conflicts = de.find_conflicts(changes, {"infra/server.md": "- SSH port: 8080\n"})
        self.assertTrue(any(c["kind"] == "key" for c in conflicts))

    def test_same_number_is_not_a_conflict(self):
        changes = [change("- SSH port: 2222")]
        self.assertEqual(de.find_conflicts(changes, {"infra/server.md": "- SSH port: 2222\n"}), [])


class VerdictTest(unittest.TestCase):
    TEXTS = {"12345678": "用户说 demo 用 SQLite。"}

    def setUp(self):
        self.changes = de.check_changes([
            change("- 用户说「demo 用 SQLite」", line=2),
            change("- 用户说「demo 用 MySQL」", line=3),
            change("- 用户说「demo 用 MySQL」", line=4),
            change("- 旧版本 1.0", line=5),
            change("- 无依据的说法", line=6),
        ], self.TEXTS)

    def test_actions_follow_the_policy(self):
        out = de.apply_verdicts(self.changes, {"verdicts": [
            {"index": 1, "verdict": "supported"},
            {"index": 2, "verdict": "contradicted",
             "evidence": [{"sid8": "12345678", "quote": "demo 用 SQLite"}]},
            {"index": 3, "verdict": "contradicted",
             "evidence": [{"sid8": "12345678", "quote": "demo 用 Oracle"}]},
            {"index": 4, "verdict": "stale"},
            {"index": 5, "verdict": "misattributed"},
        ]}, self.TEXTS)
        self.assertEqual([c["action"] for c in out], ["none", "auto-fix", "todo", "todo", "auto-fix"])
        self.assertEqual([c["verdict"] for c in out], ["supported", "contradicted", "contradicted", "stale",
                                                       "misattributed"])
        self.assertTrue(out[1]["evidence"][0]["verified"])
        self.assertFalse(out[2]["evidence"][0]["verified"])

    def test_missing_or_unknown_verdict_is_unjudged_todo(self):
        out = de.apply_verdicts(self.changes[:1], {"verdicts": [{"index": 1, "verdict": "maybe"}]}, self.TEXTS)
        self.assertEqual((out[0]["verdict"], out[0]["action"]), (de.UNJUDGED, "todo"))
        out = de.apply_verdicts(self.changes[:1], {"verdicts": []}, self.TEXTS)
        self.assertEqual(out[0]["verdict"], de.UNJUDGED)

    def test_single_character_evidence_does_not_verify(self):
        out = de.apply_verdicts([change("- 用户说「用」")], {"verdicts": [
            {"index": 1, "verdict": "contradicted", "evidence": [{"sid8": "12345678", "quote": "用"}]},
        ]}, self.TEXTS)
        self.assertFalse(out[0]["evidence"][0]["verified"])
        self.assertEqual(out[0]["action"], "todo")

    def test_report_summary_and_log_line(self):
        out = de.apply_verdicts(self.changes, {"verdicts": [
            {"index": 1, "verdict": "supported"}, {"index": 2, "verdict": "contradicted",
             "evidence": [{"sid8": "12345678", "quote": "demo 用 SQLite"}]},
            {"index": 3, "verdict": "unsupported"}, {"index": 4, "verdict": "stale"},
            {"index": 5, "verdict": "misattributed"},
        ]}, self.TEXTS)
        report = de.build_report(out, [{"kind": "key"}])
        self.assertEqual(report["summary"]["total"], 5)
        self.assertEqual(report["summary"]["supported"], 1)
        self.assertEqual(report["summary"]["fixed"], 0)
        self.assertEqual(report["summary"]["todos"], 2)
        self.assertIn("Diff Engine", de.summary_line(report, "zh"))
        self.assertIn("Diff Engine", de.summary_line(report, "en"))

    def test_report_merges_same_day_runs(self):
        a = de.build_report(self.changes[:2], [], date="2026-10-26")
        b = de.build_report(self.changes[2:], [], date="2026-10-26")
        merged = de.merge_report(a, b)
        self.assertEqual(merged["summary"]["total"], 5)
        self.assertEqual(len(merged["changes"]), 5)

    def test_write_report_is_json(self):
        report = de.build_report(self.changes[:1], [], date="2026-10-26")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state" / "diff.json"
            de.write_report(path, report)
            self.assertEqual(json.loads(path.read_text())["schema"], 1)


if __name__ == "__main__":
    unittest.main()
