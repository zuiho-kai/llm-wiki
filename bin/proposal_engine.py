#!/usr/bin/env python3
"""Proposal Engine: structured rule candidates behind ``misc/rule-candidates.md``.

A candidate is one record with a stable id, a normalized dedup key, the rule
draft, its target (a resident rule file + section, or a hook/test), every
occurrence (date, sid8, verbatim quote) and a status (``open`` / ``accepted`` /
``rejected``).  ``misc/rule-candidates.md`` is rendered from these records, so
the human page and the machine state cannot drift apart.

Rules from Issue #1:
- synonymous corrections merge on ``normalize_key``;
- a candidate is shown to the user only after ``MIN_OCCURRENCES`` occurrences;
- a rejected candidate is never proposed again;
- ``accept`` writes a patch under ``state/proposals/`` and never edits the
  resident rule file itself.

No model calls, no git commands: ``bin/ingest.py`` feeds this module and tests
exercise it directly.
"""
import difflib, hashlib, json, os, re, tempfile, time, unicodedata
from collections import Counter
from pathlib import Path

SCHEMA = 1
STATUSES = ("open", "accepted", "rejected")
# source: existing rule R5 and Issue #1 ("≥2 次进待办")
MIN_OCCURRENCES = 2
# source: lint.py BODY_LINE (300) minus the prefix/evidence budget; keeps generated lines valid.
DRAFT_CLIP = 120
# source: measured on the PR review's paraphrase pair ("先跑 lint 再提交" vs
# "提交前先跑 lint": character-bigram Dice 0.75); the nearest distinct pair
# measured 0.545, and numeric-only variants are excluded below.
SYNONYM_DICE = 0.75
# source: shorter drafts have too few character bigrams for a stable comparison.
MIN_SYNONYM_CHARS = 6
# source: PR review P2 (opposite rules such as 要/不要 and Always/Never merged).
# If either draft carries a negation, only an exact canonical key may merge it;
# the approximate fallback must stay conservative because it cannot understand
# which side of the rule a negation changes.
# source: PR review P2 — single-character 不/未/别 misread positive words such as
# 不同/未来/特别/不错 as negation.  Match negation words or phrases; 别 only at a
# clause start, so 特别/别人/个别 stay positive.
NEGATION_ZH = re.compile(
    r"(?:不要|不能|不得|不用|不必|不需|不准|不许|不可|不该|不应|不建议|不推荐|不允许|"
    r"无需|禁止|切勿|勿|严禁|避免|尚未|并未|从未|还未|未能|没有|千万别|别忘记)"
    r"|(?:^|[，。；：、！？\s])别(?!人|的|处|致|墅|称|名)"
    # source: PR review P2 — bare 不/未 before a verb or adjective is a negation
    # ("不合并", "未完成"); the listed lexicalized positive words (不同/不错/未来/…)
    # stay positive.
    r"|(?:不|未)(?!(?:同|错|少|断|久|仅|但|愧|亚|丹|菲|二|妨|免|惜|无|日|列|来|然|几))"
    r"(?=[\u4e00-\u9fff])")
NEGATION_EN = re.compile(
    r"\b(?:not|never|no|none|nothing|nobody|neither|nor|nowhere|without|cannot|"
    r"can't|don't|doesn't|didn't|shouldn't|mustn't|won't|wouldn't|isn't|aren't|avoid)\b",
    re.IGNORECASE)
# source: PR review P2 — HTTPS/HTTP differ by one technical token but are opposite
# rules; the approximate fallback must require such key tokens to match exactly.
KEY_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*(?::\/\/[^\s]+)?")
PROTOCOL_TOKEN = re.compile(r"^(?:https?|ftps?|sftp|ssh|wss?)$", re.IGNORECASE)
# source: PR review P2 — Chinese structural particles do not change a rule's meaning
# (不同项目使用不同的端口 / 不同项目使用不同端口; 特别小心地备份 / 特别小心备份).
SIMILARITY_PARTICLES = re.compile(r"[的地得了]")
PAGE = "misc/rule-candidates.md"
SCOPE = "rule candidates from dream, the user's accept/reject decisions, and proposal patches"


def normalize_key(text):
    """Normalized dedup key: NFKC, case-folded, punctuation and whitespace removed."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\W_]+", "", t, flags=re.UNICODE)


def _bigrams(text):
    return [text[i:i + 2] for i in range(len(text) - 1)] or ([text] if text else [])


def has_negation(text):
    """True when a draft carries a negation (Chinese or English).

    The approximate fallback cannot tell which side a negation flips, so the
    caller must not merge such drafts unless their canonical keys are equal."""
    raw = str(text or "").lower()
    return bool(NEGATION_ZH.search(raw) or NEGATION_EN.search(raw))


def _key_tokens(text):
    """Technical tokens (acronyms, numbers, URLs) that must match exactly."""
    tokens = set()
    for raw in KEY_TOKEN.findall(unicodedata.normalize("NFKC", str(text or ""))):
        token = raw.rstrip(".,;:!?)]}\"'")
        if "://" in token:
            tokens.add(token.lower())
        elif PROTOCOL_TOKEN.match(token):
            tokens.add(token.lower())
        elif token.isupper() and len(token) >= 2:
            tokens.add(token)
        elif any(ch.isdigit() for ch in token):
            tokens.add(token.lower())
    return tokens


def similar_draft(a, b):
    """Conservative fallback for paraphrases the model gave different keys.

    Character-bigram Dice similarity above ``SYNONYM_DICE``.  Drafts that
    differ only in a number ("备份保留 7 天" / "备份保留 30 天") are rule
    variants, not synonyms, so they never merge.  Negated drafts (要/不要,
    Always/Never) never merge by similarity: opposite rules look deceptively
    alike and the fallback cannot infer the intended polarity.  Technical key
    tokens (HTTPS/HTTP, URLs, numbers) must match exactly, and Chinese
    structural particles are ignored before the Dice comparison."""
    raw_a, raw_b = str(a or ""), str(b or "")
    a, b = normalize_key(a), normalize_key(b)
    if not a or not b or a == b:
        return a == b
    if has_negation(raw_a) or has_negation(raw_b):
        return False
    if _key_tokens(raw_a) != _key_tokens(raw_b):
        return False
    a = SIMILARITY_PARTICLES.sub("", a)
    b = SIMILARITY_PARTICLES.sub("", b)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < MIN_SYNONYM_CHARS or len(b) < MIN_SYNONYM_CHARS:
        return False
    if re.search(r"\d", a + b) and re.sub(r"\d+", "", a) == re.sub(r"\d+", "", b):
        return False
    ca, cb = Counter(_bigrams(a)), Counter(_bigrams(b))
    total = sum(ca.values()) + sum(cb.values())
    return total > 0 and 2 * sum((ca & cb).values()) / total >= SYNONYM_DICE


def candidate_id(key):
    return "p-" + hashlib.sha1((key or "").encode("utf-8")).hexdigest()[:8]


def clip(text, limit=DRAFT_CLIP):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def normalize_target(target):
    """Accept a dict or a compact string (``file#section``, ``hook``, ``test``)."""
    if isinstance(target, dict):
        kind = str(target.get("kind") or "rule").lower()
        file = str(target.get("file") or "").strip()
        section = str(target.get("section") or "").strip()
    else:
        raw = str(target or "").strip()
        if raw.lower() in ("hook", "test"):
            kind, file, section = raw.lower(), "", ""
        elif "#" in raw:
            kind, (file, section) = "rule", raw.split("#", 1)
        else:
            kind, file, section = "rule", raw, ""
    if kind not in ("rule", "hook", "test"):
        kind = "rule"
    return {"kind": kind, "file": file or "~/.claude/CLAUDE.md", "section": section}


def target_label(target):
    target = normalize_target(target)
    if target["kind"] != "rule":
        return target["kind"]
    base = f"`{target['file']}`"
    return base + (f" › {target['section']}" if target["section"] else "")


def _occ(o):
    quote = str(o.get("quote") or "").strip()
    if quote.startswith("「") and quote.endswith("」"):
        quote = quote[1:-1]
    return {"date": str(o.get("date") or "").strip(),
            "sid8": str(o.get("sid8") or "").strip()[:8],
            "quote": quote}


def _unique_occurrences(items):
    out, seen = [], set()
    for o in items:
        o = _occ(o)
        k = (o["date"], o["sid8"], o["quote"])
        if o["sid8"] and o["quote"] and k not in seen:
            seen.add(k)
            out.append(o)
    return out


class ProposalStore:
    """JSON-backed candidate store.  One store per wiki, normally
    ``<wiki>/misc/rule-candidates.json`` (tracked with the page)."""

    def __init__(self, path):
        self.path = Path(path)
        self.records = self._load()

    def _load(self):
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
        except ValueError:
            return []
        records = data.get("proposals", []) if isinstance(data, dict) else data
        out = []
        for r in records:
            if not isinstance(r, dict):
                continue
            r.setdefault("key", normalize_key(r.get("draft", "")))
            r.setdefault("id", candidate_id(r["key"]))
            r.setdefault("status", "open")
            r.setdefault("occurrences", [])
            r.setdefault("target", normalize_target(r.get("target")))
            r.setdefault("patch", "")
            r.setdefault("reason", "")
            r.setdefault("created", "")
            r.setdefault("updated", "")
            r["occurrences"] = _unique_occurrences(r["occurrences"])
            out.append(r)
        return out

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": SCHEMA, "proposals": self.records}
        fd, tmp = tempfile.mkstemp(prefix=".proposals-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def get(self, proposal_id):
        return next((r for r in self.records if r["id"] == proposal_id), None)

    def list(self, status=None):
        rows = [r for r in self.records if status is None or r["status"] == status]
        return sorted(rows, key=lambda r: (r["status"], r["id"]))

    def proposed(self, record):
        return record["status"] == "open" and len(record["occurrences"]) >= MIN_OCCURRENCES

    def add(self, draft, target=None, occurrences=(), key=None, today=None, save=True):
        """Add/merge one candidate.  Synonymous drafts share ``key``."""
        key = normalize_key(key or draft)
        if not key:
            raise ValueError("candidate needs a draft or key")
        today = today or time.strftime("%Y-%m-%d")
        existing = next((r for r in self.records if r["key"] == key), None)
        if not existing and draft:
            # A model that changes the canonical key between nights must not split
            # the same rule into two records; the verbatim draft is a safe fallback.
            draft_key = normalize_key(draft)
            existing = next((r for r in self.records if normalize_key(r.get("draft", "")) == draft_key), None)
            if not existing:
                wanted = normalize_target(target)
                existing = next((r for r in self.records
                                 if normalize_target(r.get("target")) == wanted
                                 and similar_draft(draft, r.get("draft", ""))), None)
        if existing:
            existing["occurrences"] = _unique_occurrences(list(existing["occurrences"]) + list(occurrences))
            existing["updated"] = today
            if draft and not existing.get("draft"):
                existing["draft"] = draft
            if target:
                existing["target"] = normalize_target(target)
            record = existing
        else:
            record = {"id": candidate_id(key), "key": key, "draft": str(draft or "").strip(),
                      "target": normalize_target(target), "occurrences": _unique_occurrences(list(occurrences)),
                      "status": "open", "patch": "", "reason": "",
                      "created": today, "updated": today}
            self.records.append(record)
        if save:
            self.save()
        return record

    def reject(self, proposal_id, reason="", today=None):
        record = self.get(proposal_id)
        if not record:
            raise KeyError(proposal_id)
        record["status"] = "rejected"
        record["reason"] = str(reason or "").strip()
        record["updated"] = today or time.strftime("%Y-%m-%d")
        self.save()
        return record

    def accept(self, proposal_id, patch_dir, patch_prefix="state/proposals", today=None):
        """Mark accepted and write a patch; never touch the target file."""
        record = self.get(proposal_id)
        if not record:
            raise KeyError(proposal_id)
        if record["status"] == "rejected":
            raise ValueError(f"{proposal_id} was rejected; reject is final")
        if record["status"] == "open" and len(record["occurrences"]) < MIN_OCCURRENCES:
            raise ValueError(f"{proposal_id} has {len(record['occurrences'])} occurrence(s); "
                             f"at least {MIN_OCCURRENCES} are needed before it can be accepted")
        patch_dir = Path(patch_dir)
        patch_dir.mkdir(parents=True, exist_ok=True)
        target = normalize_target(record["target"])
        if target["kind"] == "rule":
            name = f"{proposal_id}.patch"
            content = rule_patch(target, record["draft"])
        else:
            name = f"{proposal_id}.proposal.md"
            content = (f"# Proposal {proposal_id} ({target['kind']})\n\n"
                       f"- rule draft: {record['draft']}\n"
                       f"- target: {target['kind']}\n"
                       f"- evidence: " + "; ".join(f"{o['date']} {o['sid8']}「{o['quote']}」"
                                                   for o in record["occurrences"]) + "\n\n"
                       "Generated by the Proposal Engine; nothing was applied.  A hook or test "
                       "needs a human-designed check before it can be added.\n")
        path = patch_dir / name
        path.write_text(content)
        record["status"] = "accepted"
        record["patch"] = f"{patch_prefix}/{name}"
        record["updated"] = today or time.strftime("%Y-%m-%d")
        self.save()
        return record, path


def insert_rule(text, section, draft, path="~/.claude/CLAUDE.md"):
    """Return new file text with ``- draft`` added under ``section`` (never writes)."""
    line = f"- {draft}".rstrip()
    if text and re.search(r"(?m)^" + re.escape(line) + r"\s*$", text):
        return text  # already present: an empty patch
    if not text:
        head = f"## {section}\n" if section else "## Rules\n"
        return f"# Rules\n\n{head}{line}\n"
    lines = text.splitlines()
    idx = None
    if section:
        for i, l in enumerate(lines):
            if re.match(r"^#{1,3}\s+" + re.escape(section) + r"\s*$", l):
                idx = i
                break
    if idx is None and section:
        return text.rstrip("\n") + f"\n\n## {section}\n{line}\n"
    if idx is None:
        return text.rstrip("\n") + f"\n{line}\n"
    # insert as the first bullet of the section, after the heading and one blank
    at = idx + 1
    while at < len(lines) and not lines[at].strip():
        at += 1
    lines.insert(at, line)
    return "\n".join(lines).rstrip("\n") + "\n"


def patch_base(target):
    """The directory the generated relative-path rule patch is applied from."""
    return Path(os.path.expanduser(normalize_target(target)["file"])).parent


def rule_patch(target, draft):
    """Unified diff that adds one rule line; the resident file is only read.

    Paths are relative to the target file's directory (``a/NAME``, ``b/NAME``),
    so ``git apply --check`` works from the base printed by the CLI instead of
    receiving an absolute path (PR review P2)."""
    target = normalize_target(target)
    path = Path(os.path.expanduser(target["file"]))
    old = Path(path).read_text() if os.path.exists(path) else ""
    new = insert_rule(old, target["section"], draft, target["file"])
    if old == new:
        return (f"# no change: the rule is already present in {target['file']}\n")
    name = path.name
    diff = difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                fromfile="/dev/null" if not old else f"a/{name}",
                                tofile=f"b/{name}", n=3)
    return "".join(diff)


def registered_text(text, language="zh"):
    """Return README text with the generated-page link, without touching disk.

    ``text=None`` means the file does not exist; the caller keeps its existing
    ``# misc`` default.  Keeping this pure lets the commit path compute the
    generated line from HEAD instead of from a user's dirty working tree."""
    if text is None:
        text = "# misc\n"
    if "(rule-candidates.md)" in text:
        return text
    desc = "规则候选（由结构化记录生成）" if language == "zh" else "rule candidates (generated from structured records)"
    if not text.endswith("\n"):
        text += "\n"
    return text + f"- [rule-candidates](rule-candidates.md) — {desc}\n"


def ensure_registered(wiki, language="zh"):
    """Put the generated page into misc/README.md once."""
    wiki = Path(wiki)
    readme = wiki / "misc" / "README.md"
    before = readme.read_text() if readme.exists() else None
    after = registered_text(before, language)
    if after == (before if before is not None else "# misc\n"):
        return False
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text(after)
    return True


def render_page(records, language="zh"):
    """Render the human page from the records; deterministic for a given store."""
    zh = language == "zh"
    description = "规则候选：来自 dream 的结构化记录，由用户决定接受或否决" if zh else \
        "rule candidates from dream, for the user to accept or reject"
    scope = ("dream 归纳的规则候选、用户的接受/否决记录和补丁路径；由 bin/ingest.py proposals 生成，不手改"
             if zh else
             "rule candidates, the user's accept/reject decisions and patch paths; generated by "
             "bin/ingest.py proposals, do not edit by hand")
    out = ["---", "name: rule-candidates", f"description: {description}", "metadata:", f"  scope: {scope}",
           "---", "# 规则候选" if zh else "# Rule candidates", ""]
    open_records = [r for r in records if r.get("status") == "open"]
    once = [r for r in open_records if 0 < len(r["occurrences"]) < MIN_OCCURRENCES]
    proposed = [r for r in open_records if len(r["occurrences"]) >= MIN_OCCURRENCES]
    accepted = [r for r in records if r.get("status") == "accepted"]
    rejected = [r for r in records if r.get("status") == "rejected"]

    if once:
        out.append("## 关键事实" if zh else "## Key facts")
        for r in once:
            o = r["occurrences"][0]
            pattern = clip(r.get("draft") or r["key"], 80)
            out.append(f"- {o['date']} {o['sid8']} {pattern}：「{clip(o['quote'], 100)}」")
        out.append("")
    if proposed:
        out.append("## 待办" if zh else "## Open items")
        for r in proposed:
            target = target_label(r["target"])
            line = (f"- 是否把「{clip(r['draft'])}」加进 {target}（或做成 hook/测试）："
                    f"出现 {len(r['occurrences'])} 次；dream 建议，用户未答复"
                    if zh else
                    f"- Whether to add 「{clip(r['draft'])}」 to {target} (or make it a hook/test): "
                    f"seen {len(r['occurrences'])} times; dream suggestion, no answer yet")
            out.append(line)
            for o in r["occurrences"]:
                out.append(f"  - {o['date']} {o['sid8']}「{clip(o['quote'], 100)}」")
        out.append("")
    if accepted or rejected:
        out.append("## 决策与踩坑" if zh else "## Decisions & pitfalls")
        for r in accepted:
            line = (f"- 已接受 {r['id']}：「{clip(r['draft'])}」→ 补丁 `{r['patch']}`（规则文件未改）"
                    if zh else
                    f"- Accepted {r['id']}: 「{clip(r['draft'])}」 → patch `{r['patch']}` (rule file untouched)")
            out.append(line)
        for r in rejected:
            line = (f"- 已否决 {r['id']}：「{clip(r['draft'])}」" + (f" — {r['reason']}" if r.get("reason") else "")
                    if zh else
                    f"- Rejected {r['id']}: 「{clip(r['draft'])}」" + (f" — {r['reason']}" if r.get("reason") else ""))
            out.append(line)
        out.append("")
    if not (once or proposed or accepted or rejected):
        out.append("## 待办" if zh else "## Open items")
        out.append("- 暂无候选" if zh else "- no candidates yet")
        out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def publish(store, wiki, language="zh", meta=False):
    """Write the generated page and register it.

    Returns ``(page_path, changed)``; with ``meta=True`` also returns whether
    the README changed, so the CLI can commit only files it actually wrote."""
    wiki = Path(wiki)
    page = wiki / PAGE
    page.parent.mkdir(parents=True, exist_ok=True)
    text = render_page(store.records, language)
    changed = not page.exists() or page.read_text() != text
    if changed:
        page.write_text(text)
    readme_changed = ensure_registered(wiki, language)
    if meta:
        return page, changed, readme_changed
    return page, changed


def list_text(store):
    rows = store.list()
    if not rows:
        return "no proposals"
    lines = [f"{'id':10} {'status':9} {'n':>2}  target / draft"]
    for r in rows:
        lines.append(f"{r['id']:10} {r['status']:9} {len(r['occurrences']):>2}  "
                     f"{target_label(r['target'])} — {clip(r['draft'], 60)}")
    return "\n".join(lines)
