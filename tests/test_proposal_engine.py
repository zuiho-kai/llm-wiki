"""Unit tests for bin/proposal_engine.py: dedup, threshold, reject and accept."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import proposal_engine as pe  # noqa: E402


def occ(day, sid, quote):
    return {"date": day, "sid8": sid, "quote": quote}


class KeyTest(unittest.TestCase):
    def test_normalize_key_merges_punctuation_and_case(self):
        self.assertEqual(pe.normalize_key("先跑 Lint！再提交"), pe.normalize_key("先跑 lint 再提交"))
        self.assertEqual(pe.normalize_key("Run the linter, then commit."), pe.normalize_key("run the linter then commit"))

    def test_normalize_target_strings(self):
        self.assertEqual(pe.normalize_target("hook")["kind"], "hook")
        self.assertEqual(pe.normalize_target("test")["kind"], "test")
        t = pe.normalize_target("~/.claude/CLAUDE.md#工作规矩")
        self.assertEqual((t["kind"], t["file"], t["section"]), ("rule", "~/.claude/CLAUDE.md", "工作规矩"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = pe.ProposalStore(self.root / "state" / "proposals.json")

    def test_dedup_and_two_occurrence_threshold(self):
        r = self.store.add("先跑 lint 再提交", {"kind": "rule", "file": "~/.claude/CLAUDE.md", "section": "工作规矩"},
                           [occ("10-25", "12345678", "先跑 lint 再提交")])
        self.assertFalse(self.store.proposed(r))
        r2 = self.store.add("先跑 lint 再提交", None,
                            [occ("10-26", "87654321", "记住先跑 lint 再提交")])
        self.assertIs(r, r2)
        self.assertEqual(len(r["occurrences"]), 2)
        self.assertTrue(self.store.proposed(r))
        self.assertEqual(len(self.store.records), 1)
        page = pe.render_page(self.store.records, "zh")
        self.assertIn("## 待办", page)
        self.assertIn("12345678", page)
        self.assertIn("87654321", page)

    def test_one_occurrence_is_only_recorded_not_proposed(self):
        self.store.add("回答先给结论", None, [occ("10-25", "12345678", "回答先给结论")])
        page = pe.render_page(self.store.records, "zh")
        self.assertIn("## 关键事实", page)
        self.assertNotIn("## 待办", page)
        self.assertFalse(self.store.proposed(self.store.records[0]))

    def test_rejected_is_never_proposed_again(self):
        r = self.store.add("不要自动删文件", None, [occ("10-25", "12345678", "别自动删文件"),
                                                    occ("10-26", "87654321", "不要自动删除")])
        self.store.reject(r["id"], "误报")
        self.store.add("不要自动删文件", None, [occ("10-27", "abcdef12", "别删文件")])
        self.assertEqual(r["status"], "rejected")
        self.assertFalse(self.store.proposed(r))
        self.assertEqual(len(r["occurrences"]), 3)
        page = pe.render_page(self.store.records, "zh")
        self.assertIn("## 决策与踩坑", page)
        self.assertIn("已否决", page)
        self.assertIn("误报", page)

    def test_store_roundtrip_dedupes_occurrences(self):
        self.store.add("同一句", None, [occ("10-25", "12345678", "同一句"), occ("10-25", "12345678", "同一句")])
        self.assertEqual(len(self.store.records[0]["occurrences"]), 1)
        again = pe.ProposalStore(self.store.path)
        self.assertEqual(len(again.records), 1)
        self.assertEqual(again.records[0]["id"], self.store.records[0]["id"])

    def test_accept_only_writes_a_patch(self):
        target = self.root / "home" / ".claude" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Rules\n\n## 工作规矩\n- 已有规则\n")
        r = self.store.add("先跑 lint 再提交", {"kind": "rule", "file": str(target), "section": "工作规矩"},
                           [occ("10-25", "12345678", "先跑 lint"), occ("10-26", "87654321", "先跑 lint 再提交")])
        before = target.read_text()
        rec, patch = self.store.accept(r["id"], self.root / "state" / "proposals")
        self.assertEqual(rec["status"], "accepted")
        self.assertEqual(target.read_text(), before)  # the resident rule file is untouched
        text = patch.read_text()
        self.assertIn("+- 先跑 lint 再提交", text)
        self.assertIn("state/proposals/", rec["patch"])
        self.assertEqual(pe.ProposalStore(self.store.path).get(r["id"])["status"], "accepted")

    def test_accept_hook_writes_a_proposal_not_a_rule_patch(self):
        r = self.store.add("提交前必须跑 lint", {"kind": "hook"}, [occ("10-25", "12345678", "先跑 lint"),
                                                                  occ("10-26", "87654321", "提交前必须跑 lint")])
        rec, path = self.store.accept(r["id"], self.root / "state" / "proposals")
        self.assertTrue(path.name.endswith(".proposal.md"))
        self.assertIn("hook", path.read_text())
        self.assertEqual(rec["status"], "accepted")

    def test_accept_requires_two_occurrences(self):
        r = self.store.add("只出现一次", None, [occ("10-25", "12345678", "只出现一次")])
        with self.assertRaises(ValueError):
            self.store.accept(r["id"], self.root / "state" / "proposals")
        self.assertEqual(r["status"], "open")
        self.store.add("只出现一次", None, [occ("10-26", "87654321", "只出现一次")])
        rec, _ = self.store.accept(r["id"], self.root / "state" / "proposals")
        self.assertEqual(rec["status"], "accepted")

    def test_same_draft_with_different_keys_merges(self):
        r1 = self.store.add("先跑 lint 再提交", None, [occ("10-25", "12345678", "先跑 lint 再提交")],
                            key="lint-first")
        r2 = self.store.add("先跑 lint 再提交", None, [occ("10-26", "87654321", "先跑 lint 再提交")],
                            key="lint-before-commit")
        self.assertIs(r1, r2)
        self.assertEqual(len(r2["occurrences"]), 2)

    def test_same_canonical_key_merges_different_wording(self):
        r1 = self.store.add("先跑 lint 再提交", None, [occ("10-25", "12345678", "先跑 lint 再提交")],
                            key="lint-first")
        r2 = self.store.add("提交前先跑 lint", None, [occ("10-26", "87654321", "提交前先跑 lint")],
                            key="lint-first")
        self.assertIs(r1, r2)
        self.assertEqual(len(r2["occurrences"]), 2)

    def test_different_wording_without_shared_key_merges(self):
        r1 = self.store.add("先跑 lint 再提交", None, [occ("10-25", "12345678", "先跑 lint 再提交")],
                            key="lint-first")
        r2 = self.store.add("提交前先跑 lint", None, [occ("10-26", "87654321", "提交前先跑 lint")],
                            key="lint-before-commit")
        self.assertIs(r1, r2)
        self.assertEqual(len(r2["occurrences"]), 2)

    def test_numeric_rule_variants_do_not_merge(self):
        r1 = self.store.add("备份保留 7 天", None, [occ("10-25", "12345678", "备份保留 7 天")], key="backup-7")
        r2 = self.store.add("备份保留 30 天", None, [occ("10-26", "87654321", "备份保留 30 天")], key="backup-30")
        self.assertIsNot(r1, r2)
        self.assertEqual(len(self.store.records), 2)

    def test_keyword_variants_do_not_merge(self):
        self.assertFalse(pe.similar_draft("Use HTTPS for all external URLs",
                                          "Use HTTP for all external URLs"))
        r1 = self.store.add("Use HTTPS for all external URLs", None,
                            [occ("10-25", "12345678", "Use HTTPS for all external URLs")], key="https-urls")
        r2 = self.store.add("Use HTTP for all external URLs", None,
                            [occ("10-26", "87654321", "Use HTTP for all external URLs")], key="http-urls")
        self.assertIsNot(r1, r2)
        self.assertEqual(len(self.store.records), 2)

    def test_url_and_lowercase_protocol_variants_do_not_merge(self):
        self.assertEqual(pe._key_tokens("访问 https://example.com"),
                         {"https://example.com"})
        cases = [
            ("访问 https://example.com", "访问 http://example.com"),
            ("Deploy to https://prod.example.com", "Deploy to https://staging.example.com"),
            ("Use https for all external URLs", "Use http for all external URLs"),
        ]
        for a, b in cases:
            with self.subTest(a=a):
                self.assertFalse(pe.similar_draft(a, b))
        r1 = self.store.add("访问 https://example.com", None,
                            [occ("10-25", "12345678", "访问 https://example.com")],
                            key="url-https")
        r2 = self.store.add("访问 http://example.com", None,
                            [occ("10-26", "87654321", "访问 http://example.com")],
                            key="url-http")
        self.assertIsNot(r1, r2)
        self.assertEqual(len(self.store.records), 2)

    def test_bare_bu_wei_negation_blocks_merge(self):
        cases = [
            ("不合并到主分支", "合并到主分支"),
            ("不提交未完成的代码", "提交未完成的代码"),
            ("未完成迁移前先备份", "完成迁移前先备份"),
            ("不明显的问题先记录", "明显的问题先记录"),
            ("Nothing should be deleted", "Something should be deleted"),
        ]
        for a, b in cases:
            with self.subTest(a=a):
                self.assertTrue(pe.has_negation(a))
                self.assertFalse(pe.similar_draft(a, b))
        r1 = self.store.add("不合并到主分支", None,
                            [occ("10-25", "12345678", "不合并到主分支")],
                            key="no-merge")
        r2 = self.store.add("合并到主分支", None,
                            [occ("10-26", "87654321", "合并到主分支")],
                            key="merge")
        self.assertIsNot(r1, r2)
        self.assertEqual(len(self.store.records), 2)

    def test_positive_words_with_negation_characters_still_merge(self):
        cases = [
            ("不同项目使用不同端口", "不同项目使用不同的端口"),
            ("特别小心地备份", "特别小心备份"),
            ("未来的部署使用容器", "未来部署使用容器"),
            ("不错的做法是先备份", "好的做法是先备份"),
        ]
        for a, b in cases:
            with self.subTest(a=a):
                self.assertFalse(pe.has_negation(a))
                self.assertFalse(pe.has_negation(b))
                self.assertTrue(pe.similar_draft(a, b))
        self.assertFalse(pe.has_negation("别人提交前先跑 lint"))
        self.assertFalse(pe.has_negation("个别项目使用不同端口"))
        self.assertTrue(pe.has_negation("回答先给结论，不要解释原因"))
        self.assertTrue(pe.has_negation("尚未运行迁移"))
        self.assertTrue(pe.has_negation("别解释原因"))
        self.assertTrue(pe.has_negation("Never run migrations at night"))

    def test_negated_rule_variants_do_not_merge(self):
        r1 = self.store.add("回答先给结论，再解释原因", None,
                            [occ("10-25", "12345678", "回答先给结论，再解释原因")], key="answer-then-explain")
        r2 = self.store.add("回答先给结论，不要解释原因", None,
                            [occ("10-26", "87654321", "回答先给结论，不要解释原因")], key="answer-no-explain")
        self.assertIsNot(r1, r2)
        self.assertEqual(len(self.store.records), 2)
        r3 = self.store.add("Always run the linter before committing", None,
                            [occ("10-27", "abcdef12", "Always run the linter before committing")], key="always-lint")
        r4 = self.store.add("Never run the linter before committing", None,
                            [occ("10-28", "1234abcd", "Never run the linter before committing")], key="never-lint")
        self.assertIsNot(r3, r4)
        self.assertEqual(len(self.store.records), 4)

    def test_render_page_skips_zero_occurrence_record(self):
        self.store.add("空候选", None, [])
        page = pe.render_page(self.store.records, "zh")
        self.assertNotIn("空候选", page)

    def test_rule_patch_applies_from_its_base(self):
        target = self.root / "home" / ".claude" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Rules\n\n## 工作规矩\n- 已有规则\n")
        r = self.store.add("先跑 lint 再提交", {"kind": "rule", "file": str(target), "section": "工作规矩"},
                           [occ("10-25", "12345678", "先跑 lint"), occ("10-26", "87654321", "先跑 lint 再提交")])
        _rec, patch = self.store.accept(r["id"], self.root / "state" / "proposals")
        text = patch.read_text()
        self.assertIn("--- a/CLAUDE.md", text)
        self.assertIn("+++ b/CLAUDE.md", text)
        check = subprocess.run(["git", "apply", "--check", str(patch)], cwd=target.parent,
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_rejected_cannot_be_accepted(self):
        r = self.store.add("x", None, [occ("10-25", "12345678", "x")])
        self.store.reject(r["id"], "no")
        with self.assertRaises(ValueError):
            self.store.accept(r["id"], self.root / "state" / "proposals")

    def test_publish_writes_page_and_registers_it_once(self):
        wiki = self.root / "wiki"
        (wiki / "misc").mkdir(parents=True)
        (wiki / "misc" / "README.md").write_text("# misc\n")
        self.store.add("先跑 lint", None, [occ("10-25", "12345678", "先跑 lint"),
                                           occ("10-26", "87654321", "先跑 lint")])
        page, changed = pe.publish(self.store, wiki, "zh")
        self.assertTrue(changed)
        text = page.read_text()
        self.assertIn("## 待办", text)
        self.assertIn("(rule-candidates.md)", (wiki / "misc" / "README.md").read_text())
        _, changed = pe.publish(self.store, wiki, "zh")
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
