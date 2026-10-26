"""End-to-end runs of bin/ingest.py against a fake `claude` CLI (tests/fake-engine/claude)."""
import json, sqlite3, time, unittest
from pathlib import Path

from helpers import TempWiki, git

DEMO = [("user", "The staging server is reachable as demo-staging over SSH, port 2222."),
        ("assistant", "Noted: demo-staging, SSH port 2222."),
        ("user", "Always run the linter before committing.")]


class IngestZhTest(unittest.TestCase):
    """Runs the pipeline with language "zh": Chinese SOP prompts, Chinese page sections."""
    language, key_section, task_word = "zh", "## 关键事实", "任务"

    def setUp(self):
        self.w = TempWiki(config={"language": self.language})
        self.addCleanup(self.w.cleanup)
        self.sid = self.w.session(DEMO)
        self.sid8 = self.sid[:8]

    def db(self, table):
        con = sqlite3.connect(self.w.wiki / ".ingest.sqlite")
        try:
            return dict(con.execute(f"select id, offset from {table}"))
        finally:
            con.close()

    def size(self):
        return (self.w.transcripts / "-home-demo-project" / f"{self.sid}.jsonl").stat().st_size

    def test_session_ingest_is_accepted_and_delivered(self):
        r = self.w.ingest("--session", self.sid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"ingest {self.sid8}", self.w.commits())
        page = (self.w.wiki / "misc" / "demo-facts.md").read_text()
        self.assertIn("demo-staging", page)
        self.assertIn(self.key_section, page)
        self.assertIn(f"ingest | {self.sid8} Demo session", (self.w.wiki / "log.md").read_text())
        self.assertEqual(self.w.lint().returncode, 0)
        self.assertEqual(git(self.w.wiki, "status", "--porcelain"), "")  # delivered as a commit, tree clean
        self.assertEqual(self.db("ingested")[self.sid], self.size())
        self.assertNotIn("REVERTED", self.w.log_text())
        self.assertEqual(self.w.calls_made(), ["haiku INGEST"])
        prompt = (self.w.tmp / "calls.txt.prompts").read_text()
        self.assertTrue(prompt.startswith(f"INGEST {self.task_word}"), prompt)
        # nothing new: a second run makes no model call
        self.w.ingest("--pending")
        self.assertEqual(self.w.calls_made(), ["haiku INGEST"])

    def test_touching_schema_is_rejected(self):
        self.w.ingest("--session", self.sid, mode="touch-schema")
        self.assertEqual(self.w.commits(), ["skeleton"])
        self.assertIn("R9: touched files outside the wiki pages: CLAUDE.md", self.w.log_text())
        self.assertFalse((self.w.wiki / "misc" / "demo-facts.md").exists())
        self.assertNotIn(self.sid, self.db("ingested"))
        con = sqlite3.connect(self.w.wiki / ".ingest.sqlite")
        self.assertEqual(con.execute("select n from fails where id=?", (self.sid,)).fetchone()[0], 1)
        con.close()

    def test_missing_log_entry_is_rejected(self):
        self.w.ingest("--session", self.sid, mode="no-log")
        self.assertEqual(self.w.commits(), ["skeleton"])
        self.assertIn(f"A2: no new log.md entry", self.w.log_text())

    def test_fallback_engine_redoes_failed_batch(self):
        self.w.ingest("--session", self.sid, mode="fail-haiku")
        self.assertEqual(self.w.calls_made(), ["haiku INGEST", "sonnet INGEST"])
        self.assertIn(f"ingest {self.sid8}", self.w.commits())

    def test_nightly_reviews_and_dreams(self):
        self.w.ingest("--session", self.sid)
        r = self.w.ingest("--nightly")
        self.assertEqual(r.returncode, 0, r.stderr)
        commits = self.w.commits()
        self.assertIn(f"review {self.sid8}", commits)
        self.assertIn("dream 1 sessions", commits)
        cand = (self.w.wiki / "misc" / "rule-candidates.md").read_text()
        self.assertIn(self.sid8, cand)
        self.assertEqual(self.db("reviewed")[self.sid], self.size())
        self.assertEqual(self.w.lint().returncode, 0)

    def test_nightly_writes_machine_readable_diff_report(self):
        self.w.ingest("--session", self.sid)
        r = self.w.ingest("--nightly")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads((self.w.wiki / "state" / f"diff-{time.strftime('%Y-%m-%d')}.json").read_text())
        self.assertEqual(report["schema"], 1)
        self.assertGreaterEqual(report["summary"]["total"], 1)
        self.assertIn("review diff report", " ".join(self.w.commits()))

    def test_diff_engine_fixes_fabricated_attribution(self):
        self.w.ingest("--session", self.sid, mode="bad-write")
        r = self.w.ingest("--nightly", mode="bad-write")
        self.assertEqual(r.returncode, 0, r.stderr)
        page = (self.w.wiki / "misc" / "demo-facts.md").read_text()
        self.assertNotIn("用户定：", page)
        report = json.loads((self.w.wiki / "state" / f"diff-{time.strftime('%Y-%m-%d')}.json").read_text())
        bad = [c for c in report["changes"] if "用户定" in c["text"]]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["verdict"], "misattributed")
        self.assertTrue(bad[0]["fixed"])
        self.assertGreaterEqual(report["summary"]["fixed"], 1)

    def test_proposals_cli_accept_and_reject(self):
        self.w.ingest("--session", self.sid)
        # a second session with the same preference gives the candidate the two occurrences
        # the Proposal Engine requires before it can be accepted
        self.w.ingest("--session", self.w.session(DEMO))
        self.w.ingest("--nightly")
        records = json.loads((self.w.wiki / "misc" / "rule-candidates.json").read_text())["proposals"]
        self.assertTrue(records)
        listed = self.w.ingest("proposals", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(records[0]["id"], listed.stdout)
        target = self.w.home / ".claude" / "CLAUDE.md"
        self.assertFalse(target.exists())
        accepted = self.w.ingest("proposals", "accept", records[0]["id"])
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn("resident rule file not modified", accepted.stdout)
        self.assertFalse(target.exists())  # accept only writes a patch
        self.assertEqual(json.loads((self.w.wiki / "misc" / "rule-candidates.json").read_text())["proposals"][0]["status"],
                         "accepted")
        second = records[1]["id"]
        rejected = self.w.ingest("proposals", "reject", second, "不适用")
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        word = "已否决" if self.language == "zh" else "Rejected"
        self.assertIn(word, (self.w.wiki / "misc" / "rule-candidates.md").read_text())

    def test_new_engines_can_be_disabled(self):
        w = TempWiki(config={"language": self.language, "diff_engine": False, "proposal_engine": False})
        self.addCleanup(w.cleanup)
        sid = w.session(DEMO)
        w.ingest("--session", sid)
        r = w.ingest("--nightly")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"review {sid[:8]}", w.commits())
        self.assertIn("dream 1 sessions", w.commits())
        self.assertFalse((w.wiki / "state" / f"diff-{time.strftime('%Y-%m-%d')}.json").exists())
        self.assertFalse((w.wiki / "misc" / "rule-candidates.json").exists())

    def test_dream_misquote_is_rejected(self):
        self.w.ingest("--session", self.sid)
        self.w.ingest("--nightly", mode="dream-misquote")
        self.assertNotIn("dream 1 sessions", self.w.commits())
        self.assertIn("quotes not found among the USER lines", self.w.log_text())

    def test_weekly_lint_runs(self):
        r = self.w.ingest("--lint")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sonnet LINT", self.w.calls_made())

    def test_hook_runs_session_in_background(self):
        r = self.w.ingest("--hook", stdin=json.dumps({"session_id": self.sid}))
        self.assertEqual(r.returncode, 0, r.stderr)
        deadline = time.time() + 60
        while time.time() < deadline and f"ingest {self.sid8}" not in self.w.commits():
            time.sleep(0.5)
        self.assertIn(f"ingest {self.sid8}", self.w.commits())

    def test_status_lists_pending(self):
        r = self.w.ingest("--status")
        self.assertIn(self.sid, r.stdout)


class IngestEnTest(IngestZhTest):
    """Runs the pipeline with language "en": English SOP prompts, English page sections."""
    language, key_section, task_word = "en", "## Key facts", "task"


class RegressionTest(unittest.TestCase):
    """Focused end-to-end regressions for the PR review findings."""

    def setUp(self):
        self.w = TempWiki(config={"language": "en"})
        self.addCleanup(self.w.cleanup)

    def db(self, table):
        con = sqlite3.connect(self.w.wiki / ".ingest.sqlite")
        try:
            return dict(con.execute(f"select id, offset from {table}"))
        finally:
            con.close()

    def proposal_fixture(self):
        """Make one accepted-eligible proposal and return its record."""
        s1 = self.w.session([("user", "Always run the linter before committing.")])
        s2 = self.w.session([("user", "Always run the linter before committing.")])
        self.w.ingest("--session", s1)
        self.w.ingest("--session", s2)
        self.w.ingest("--nightly")
        records = json.loads((self.w.wiki / "misc" / "rule-candidates.json").read_text())["proposals"]
        return next(r for r in records if len(r["occurrences"]) >= 2)

    def proposal_status(self, proposal_id):
        records = json.loads((self.w.wiki / "misc" / "rule-candidates.json").read_text())["proposals"]
        return next(r for r in records if r["id"] == proposal_id)["status"]

    def test_nightly_survives_two_ingests_rewriting_the_same_page(self):
        s1 = self.w.session([("user", "The staging server SSH port is 2222.")])
        self.assertEqual(self.w.ingest("--session", s1).returncode, 0)
        s2 = self.w.session([("user", "Correction: the staging server SSH port is 2223.")])
        self.assertEqual(self.w.ingest("--session", s2).returncode, 0)
        r = self.w.ingest("--nightly")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REVERTED", self.w.log_text())
        reviewed = self.db("reviewed")
        self.assertIn(s1, reviewed)
        self.assertIn(s2, reviewed)
        page = (self.w.wiki / "misc" / "demo-facts.md").read_text()
        self.assertIn("2223", page)
        self.assertNotIn("2222", page)

    def test_nightly_survives_uncommitted_repo_edit(self):
        s1 = self.w.session([("user", "The staging server SSH port is 2222.")])
        self.assertEqual(self.w.ingest("--session", s1).returncode, 0)
        s2 = self.w.session([("user", "Correction: the staging server SSH port is 2223.")])
        self.assertEqual(self.w.ingest("--session", s2).returncode, 0)
        page = self.w.wiki / "misc" / "demo-facts.md"
        page.write_text(page.read_text().rstrip("\n") + "\n- The staging server SSH port is 2222.\n")
        r = self.w.ingest("--nightly")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("REVERTED", self.w.log_text())
        reviewed = self.db("reviewed")
        self.assertIn(s1, reviewed)
        self.assertIn(s2, reviewed)
        text = page.read_text()
        self.assertIn("2223", text)
        self.assertIn("- The staging server SSH port is 2222.", text)

    def test_proposals_accept_does_not_commit_unrelated_staged_file(self):
        s1 = self.w.session([("user", "Always run the linter before committing.")])
        s2 = self.w.session([("user", "Always run the linter before committing.")])
        self.w.ingest("--session", s1)
        self.w.ingest("--session", s2)
        self.w.ingest("--nightly")
        records = json.loads((self.w.wiki / "misc" / "rule-candidates.json").read_text())["proposals"]
        record = next(r for r in records if len(r["occurrences"]) >= 2)
        (self.w.wiki / "misc" / "user-notes.md").write_text("# user notes\n- not for this commit\n")
        git(self.w.wiki, "add", "misc/user-notes.md")
        r = self.w.ingest("proposals", "accept", record["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        stat = git(self.w.wiki, "show", "--stat", "--oneline", "HEAD")
        self.assertNotIn("user-notes.md", stat)
        self.assertIn("A  misc/user-notes.md", git(self.w.wiki, "status", "--porcelain"))

    def test_proposals_accept_does_not_commit_readme_user_change(self):
        s1 = self.w.session([("user", "Always run the linter before committing.")])
        s2 = self.w.session([("user", "Always run the linter before committing.")])
        self.w.ingest("--session", s1)
        self.w.ingest("--session", s2)
        self.w.ingest("--nightly")
        records = json.loads((self.w.wiki / "misc" / "rule-candidates.json").read_text())["proposals"]
        record = next(r for r in records if len(r["occurrences"]) >= 2)
        readme = self.w.wiki / "misc" / "README.md"
        readme.write_text(readme.read_text() + "- user note in README\n")
        git(self.w.wiki, "add", "misc/README.md")
        r = self.w.ingest("proposals", "accept", record["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        stat = git(self.w.wiki, "show", "--stat", "--oneline", "HEAD")
        self.assertNotIn("misc/README.md", stat)
        self.assertIn("Signed-off-by: test <test@example.com>", git(self.w.wiki, "show", "-s", "--format=%B", "HEAD"))
        self.assertIn("M  misc/README.md", git(self.w.wiki, "status", "--porcelain"))
        self.assertIn("user note in README", readme.read_text())

    def test_proposals_accept_generates_only_the_readme_line(self):
        s1 = self.w.session([("user", "Always run the linter before committing.")])
        s2 = self.w.session([("user", "Always run the linter before committing.")])
        self.w.ingest("--session", s1)
        self.w.ingest("--session", s2)
        self.w.ingest("--nightly")
        records = json.loads((self.w.wiki / "misc" / "rule-candidates.json").read_text())["proposals"]
        record = next(r for r in records if len(r["occurrences"]) >= 2)
        readme = self.w.wiki / "misc" / "README.md"
        readme.write_text("# misc\n")
        git(self.w.wiki, "add", "misc/README.md")
        git(self.w.wiki, "commit", "-qm", "test: reset README")
        readme.write_text(readme.read_text() + "- user note in README\n")
        r = self.w.ingest("proposals", "accept", record["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        committed = git(self.w.wiki, "show", "HEAD:misc/README.md")
        self.assertIn("- [rule-candidates](rule-candidates.md)", committed)
        self.assertNotIn("user note in README", committed)
        self.assertIn("user note in README", readme.read_text())
        self.assertIn("M misc/README.md", git(self.w.wiki, "status", "--porcelain"))

    def test_proposals_accept_preserves_staged_readme_with_generated_link(self):
        s1 = self.w.session([("user", "Always run the linter before committing.")])
        s2 = self.w.session([("user", "Always run the linter before committing.")])
        self.w.ingest("--session", s1)
        self.w.ingest("--session", s2)
        self.w.ingest("--nightly")
        records = json.loads((self.w.wiki / "misc" / "rule-candidates.json").read_text())["proposals"]
        record = next(r for r in records if len(r["occurrences"]) >= 2)
        readme = self.w.wiki / "misc" / "README.md"
        readme.write_text("# misc\n")
        git(self.w.wiki, "add", "misc/README.md")
        git(self.w.wiki, "commit", "-qm", "test: reset README")
        readme.write_text(readme.read_text() + "- user note in README\n")
        git(self.w.wiki, "add", "misc/README.md")
        r = self.w.ingest("proposals", "accept", record["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        committed = git(self.w.wiki, "show", "HEAD:misc/README.md")
        self.assertIn("- [rule-candidates](rule-candidates.md)", committed)
        self.assertNotIn("user note in README", committed)
        self.assertIn("M  misc/README.md", git(self.w.wiki, "status", "--porcelain"))
        self.assertIn("user note in README", readme.read_text())

    def test_proposals_reject_does_not_commit_readme_user_change(self):
        s1 = self.w.session([("user", "Always run the linter before committing.")])
        s2 = self.w.session([("user", "Always run the linter before committing.")])
        self.w.ingest("--session", s1)
        self.w.ingest("--session", s2)
        self.w.ingest("--nightly")
        records = json.loads((self.w.wiki / "misc" / "rule-candidates.json").read_text())["proposals"]
        record = next(r for r in records if len(r["occurrences"]) >= 2)
        readme = self.w.wiki / "misc" / "README.md"
        readme.write_text(readme.read_text() + "- user note in README\n")
        git(self.w.wiki, "add", "misc/README.md")
        r = self.w.ingest("proposals", "reject", record["id"], "not applicable")
        self.assertEqual(r.returncode, 0, r.stderr)
        stat = git(self.w.wiki, "show", "--stat", "--oneline", "HEAD")
        self.assertNotIn("misc/README.md", stat)
        self.assertIn("Signed-off-by: test <test@example.com>", git(self.w.wiki, "show", "-s", "--format=%B", "HEAD"))
        self.assertIn("M  misc/README.md", git(self.w.wiki, "status", "--porcelain"))
        self.assertIn("user note in README", readme.read_text())

    def test_proposals_accept_refuses_unstaged_readme_deletion(self):
        record = self.proposal_fixture()
        readme = self.w.wiki / "misc" / "README.md"
        self.assertTrue(readme.exists())
        readme.unlink()
        r = self.w.ingest("proposals", "accept", record["id"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("misc/README.md", r.stderr)
        self.assertIn("deleted", r.stderr)
        self.assertFalse(readme.exists())
        self.assertIn("D misc/README.md", git(self.w.wiki, "status", "--porcelain"))
        self.assertEqual(self.proposal_status(record["id"]), "open")

    def test_proposals_accept_refuses_staged_readme_deletion(self):
        record = self.proposal_fixture()
        readme = self.w.wiki / "misc" / "README.md"
        self.assertTrue(readme.exists())
        git(self.w.wiki, "rm", "-q", "misc/README.md")
        r = self.w.ingest("proposals", "accept", record["id"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("misc/README.md", r.stderr)
        self.assertIn("deleted", r.stderr)
        self.assertFalse(readme.exists())
        self.assertIn("D  misc/README.md", git(self.w.wiki, "status", "--porcelain"))
        self.assertEqual(self.proposal_status(record["id"]), "open")

    def test_proposals_reject_refuses_unstaged_readme_deletion(self):
        record = self.proposal_fixture()
        readme = self.w.wiki / "misc" / "README.md"
        readme.unlink()
        r = self.w.ingest("proposals", "reject", record["id"], "not applicable")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("deleted", r.stderr)
        self.assertFalse(readme.exists())
        self.assertIn("D misc/README.md", git(self.w.wiki, "status", "--porcelain"))
        self.assertEqual(self.proposal_status(record["id"]), "open")

    def test_proposals_reject_refuses_staged_readme_deletion(self):
        record = self.proposal_fixture()
        readme = self.w.wiki / "misc" / "README.md"
        git(self.w.wiki, "rm", "-q", "misc/README.md")
        r = self.w.ingest("proposals", "reject", record["id"], "not applicable")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("deleted", r.stderr)
        self.assertFalse(readme.exists())
        self.assertIn("D  misc/README.md", git(self.w.wiki, "status", "--porcelain"))
        self.assertEqual(self.proposal_status(record["id"]), "open")

    def committed_readme_deletion(self):
        record = self.proposal_fixture()
        readme = self.w.wiki / "misc" / "README.md"
        self.assertTrue(readme.exists())
        git(self.w.wiki, "rm", "-q", "misc/README.md")
        git(self.w.wiki, "commit", "-qm", "user deletes readme")
        return record, readme

    def test_proposals_accept_refuses_committed_readme_deletion(self):
        record, readme = self.committed_readme_deletion()
        head = git(self.w.wiki, "rev-parse", "HEAD")
        r = self.w.ingest("proposals", "accept", record["id"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("misc/README.md", r.stderr)
        self.assertIn("deleted", r.stderr)
        self.assertFalse(readme.exists())
        self.assertEqual(git(self.w.wiki, "status", "--porcelain"), "")
        self.assertEqual(git(self.w.wiki, "rev-parse", "HEAD"), head)
        self.assertEqual(self.proposal_status(record["id"]), "open")

    def test_proposals_reject_refuses_committed_readme_deletion(self):
        record, readme = self.committed_readme_deletion()
        head = git(self.w.wiki, "rev-parse", "HEAD")
        r = self.w.ingest("proposals", "reject", record["id"], "not applicable")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("misc/README.md", r.stderr)
        self.assertIn("deleted", r.stderr)
        self.assertFalse(readme.exists())
        self.assertEqual(git(self.w.wiki, "status", "--porcelain"), "")
        self.assertEqual(git(self.w.wiki, "rev-parse", "HEAD"), head)
        self.assertEqual(self.proposal_status(record["id"]), "open")

    def test_review_cannot_delete_a_protected_page(self):
        sid = self.w.session([("user", "The staging server SSH port is 2222.")])
        self.w.ingest("--session", sid)
        r = self.w.ingest("--nightly", mode="delete-page")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.w.wiki / "misc" / "demo-facts.md").exists())
        self.assertIn("diff scope", self.w.log_text())
        self.assertNotIn(sid, self.db("reviewed"))

    def test_review_cannot_extend_a_protected_line(self):
        sid = self.w.session([("user", "The staging server SSH port is 2222.")])
        self.w.ingest("--session", sid)
        r = self.w.ingest("--nightly", mode="extend-line")
        self.assertEqual(r.returncode, 0, r.stderr)
        page = (self.w.wiki / "misc" / "demo-facts.md").read_text()
        self.assertIn("- The staging server SSH port is 2222.\n", page)
        self.assertNotIn("2222.2", page)
        self.assertIn("diff scope", self.w.log_text())

    def test_proposals_list_json_is_parseable(self):
        sid = self.w.session([("user", "Always run the linter before committing.")])
        self.w.ingest("--session", sid)
        self.w.ingest("--nightly")
        r = self.w.ingest("proposals", "list", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsInstance(json.loads(r.stdout), list)

    def test_invalid_dream_candidates_are_retried_not_marked_done(self):
        sid = self.w.session([("user", "Always run the linter before committing.")])
        self.w.ingest("--session", sid)
        r = self.w.ingest("--nightly", mode="dream-invalid")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(sid, self.db("dreamed"))
        self.assertIn("candidate without a draft", self.w.log_text())

    def test_short_dream_quote_is_not_accepted(self):
        sid = self.w.session([("user", "Always run the linter before committing.")])
        self.w.ingest("--session", sid)
        r = self.w.ingest("--nightly", mode="dream-short")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("dream 1 sessions", self.w.commits())
        self.assertIn("quotes not found among the USER lines", self.w.log_text())
        self.assertNotIn(sid, self.db("dreamed"))

    def test_nightly_log_lists_todos(self):
        sid = self.w.session([("user", "The staging server SSH port is 2222.")])
        self.w.ingest("--session", sid, mode="todo-write")
        r = self.w.ingest("--nightly", mode="todo-write")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Diff todos", (self.w.wiki / "log.md").read_text())


class RemoteTest(unittest.TestCase):
    def test_first_run_pushes_to_an_empty_remote(self):
        w = TempWiki(config={"remote": "origin"})
        self.addCleanup(w.cleanup)
        bare = w.tmp / "remote.git"
        git(w.tmp, "init", "-q", "--bare", str(bare))
        git(w.wiki, "remote", "add", "origin", str(bare))
        sid = w.session(DEMO)
        r = w.ingest("--session", sid)
        self.assertEqual(r.returncode, 0, r.stderr)
        head = git(w.wiki, "rev-parse", "HEAD")
        self.assertIn(head, git(w.tmp, "ls-remote", str(bare)))
        self.assertNotIn("FAILED", w.log_text())
        # second run: the branch now exists on the remote; pull and push again
        sid2 = w.session(DEMO)
        w.ingest("--session", sid2)
        self.assertIn(f"ingest {sid2[:8]}", w.commits())
        self.assertIn(git(w.wiki, "rev-parse", "HEAD"), git(w.tmp, "ls-remote", str(bare)))
        self.assertNotIn("FAILED", w.log_text())


class ConfigTest(unittest.TestCase):
    def test_unknown_language_is_refused(self):
        w = TempWiki(config={"language": "fr"})
        self.addCleanup(w.cleanup)
        r = w.ingest("--status")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("language must be", r.stderr)

    def test_unknown_key_is_refused(self):
        w = TempWiki(config={"no_such_key": 1})
        self.addCleanup(w.cleanup)
        r = w.ingest("--status")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown key(s) no_such_key", r.stderr)


if __name__ == "__main__":
    unittest.main()
