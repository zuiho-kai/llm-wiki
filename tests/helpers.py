"""Shared fixtures: a throwaway copy of this repository as a wiki, and synthetic Claude Code transcripts."""
import json, os, shutil, subprocess, sys, tempfile, time, uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "fake-engine"
SKIP = shutil.ignore_patterns(".git", ".ingest*", "__pycache__", "config.json", "state")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class TempWiki:
    """A copy of the repository in a temp dir, committed, with its own transcripts dir and HOME."""

    def __init__(self, config=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="llm-wiki-test-"))
        self.wiki = self.tmp / "wiki"
        shutil.copytree(REPO, self.wiki, ignore=SKIP)
        self.transcripts = self.tmp / "transcripts"
        self.home = self.tmp / "home"
        self.transcripts.mkdir()
        self.home.mkdir()
        self.calls = self.tmp / "calls.txt"
        cfg = {"transcripts": str(self.transcripts), **(config or {})}
        (self.wiki / "config.json").write_text(json.dumps(cfg))
        git(self.wiki, "init", "-q")
        git(self.wiki, "config", "user.name", "test")
        git(self.wiki, "config", "user.email", "test@example.com")
        git(self.wiki, "add", "-A")
        git(self.wiki, "commit", "-qm", "skeleton")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, mode="good"):
        return {"PATH": f"{FAKE}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.home), "FAKE_MODE": mode,
                "FAKE_CALLS": str(self.calls), "LANG": "en_US.UTF-8"}

    def ingest(self, *args, mode="good", stdin=None):
        return subprocess.run([sys.executable, "-I", str(self.wiki / "bin" / "ingest.py"), *args],
                              env=self.env(mode), input=stdin, capture_output=True, text=True, timeout=120)

    def lint(self, *args):
        return subprocess.run([sys.executable, "-I", str(self.wiki / "bin" / "lint.py"), *args],
                              capture_output=True, text=True)

    def session(self, lines, project="-home-demo-project"):
        """Write a synthetic transcript: `lines` is a list of (role, text); returns the session id."""
        sid = str(uuid.uuid4())
        path = self.transcripts / project / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        with open(path, "w") as f:
            for role, text in lines:
                f.write(json.dumps({"type": role, "cwd": "/home/demo/project", "timestamp": ts, "sessionId": sid,
                                    "message": {"role": role, "content": text}}) + "\n")
            f.write(json.dumps({"type": "ai-title", "aiTitle": "Demo session", "sessionId": sid}) + "\n")
        return sid

    def log_text(self):
        p = self.wiki / ".ingest.log"
        return p.read_text() if p.exists() else ""

    def commits(self):
        return git(self.wiki, "log", "--format=%s").splitlines()

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []
