"""Unit tests for reading USER messages out of a transcript (bin/ingest.py).

A user message spanning several physical lines carries the ``USER: `` transcript
prefix only on its first line.  ``user_text_of`` is the source for the
deterministic quote check, so it must keep every line of the message (G哥's P1
reproduction: the continuation line "演示库用 SQLite。" used to be dropped and a
real quote was then reported as missing).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))

import diff_engine as de  # noqa: E402
import ingest  # noqa: E402


def change(text, sids=("12345678",), file="misc/demo.md", line=3, sha="abc1234"):
    return {"sha": sha, "subject": f"ingest {' '.join(sids)} demo", "file": file,
            "line": line, "text": text, "sids": list(sids)}


class MultilineUserTextTest(unittest.TestCase):
    def item(self, *messages):
        """Extract one transcript made of ``(role, text)`` messages."""
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        self.addCleanup(Path(f.name).unlink)
        try:
            for role, text in messages:
                f.write(json.dumps({"type": role, "message": {"role": role, "content": text}}) + "\n")
            f.write(json.dumps({"type": "ai-title", "aiTitle": "Demo session"}) + "\n")
        finally:
            f.close()
        return ingest.extract(Path(f.name), 0)

    def test_multiline_user_message_quote_is_found(self):
        """G哥's sample: the quote lives on the user message's second line."""
        item = self.item(("user", "项目设置如下：\n演示库用 SQLite。"), ("assistant", "收到。"))
        self.assertEqual(item["text"], "USER: 项目设置如下：\n演示库用 SQLite。\n\nCLAUDE: 收到。")
        self.assertEqual(ingest.user_text_of(item), "项目设置如下：\n演示库用 SQLite。")
        checked = de.check_changes(
            [change("- 用户定：演示库用 SQLite「演示库用 SQLite」")],
            {"12345678": ingest.user_text_of(item)})[0]
        self.assertTrue(checked["deterministic_ok"], checked["checks"])

    def test_adjacent_assistant_message_is_not_user_text(self):
        """A multi-line user message must not swallow the assistant reply after it."""
        item = self.item(("user", "第一行\n第二行"), ("assistant", "演示库用 MySQL"))
        user = ingest.user_text_of(item)
        self.assertNotIn("MySQL", user)
        checked = de.check_changes([change("- 用户说「演示库用 MySQL」")], {"12345678": user})[0]
        self.assertFalse(checked["deterministic_ok"])
        self.assertIn("quote not found", checked["checks"][0]["detail"])

    def test_user_line_that_looks_like_a_speaker_label_is_kept(self):
        """Role comes from the record, not from whether a physical line starts with ``CLAUDE: ``."""
        item = self.item(("user", "请记住这行引用：\nCLAUDE: 这是用户贴的原文"), ("assistant", "好的"))
        user = ingest.user_text_of(item)
        self.assertIn("CLAUDE: 这是用户贴的原文", user)
        checked = de.check_changes(
            [change("- 用户定：这是用户贴的原文「这是用户贴的原文」")],
            {"12345678": user})[0]
        self.assertTrue(checked["deterministic_ok"], checked["checks"])


if __name__ == "__main__":
    unittest.main()
