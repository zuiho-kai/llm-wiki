#!/usr/bin/env python3
"""Diff Engine: turn one night's ingest diff into checked, per-line verdicts.

The engine has two layers:

1. Deterministic checks (this module, no model call): every added fact line is
   checked for quotes the cited session does not contain, for "the user said /
   decided" claims without a verbatim quote, and for conflicts between pages.
2. A model judgement pass (``bin/ingest.py``) that assigns one of
   ``supported / contradicted / stale / misattributed / unsupported`` to every
   change, with evidence.  This module applies those verdicts and builds the
   machine-readable report under ``state/``.

Only ``misattributed`` and ``contradicted`` lines with evidence that the script
can verify are marked ``auto-fix``.  Everything else is a todo: the nightly
review may not delete or rewrite it.

The module is deliberately free of git and model calls so it can be tested on
small, hand-written diffs.
"""
import difflib, json, re, time
from pathlib import Path

VERDICTS = ("supported", "contradicted", "stale", "misattributed", "unsupported")
UNJUDGED = "unjudged"
QUOTE_RE = re.compile(r"「([^」]+)」")
SID_RE = re.compile(r"\b([0-9a-f]{7,8})\b")
ATTRIBUTION_RE = re.compile(
    r"(?:用户(?:说|定|决定|讲|要求|否决|拒绝|选择|确认|原话|明确|告知|提出|提到|希望)|按用户|据用户|用户这边"
    r"|\bthe user(?:'s)?\s+(?:said|says|decided|rejected|asked|confirmed|wants|chose|requested|words)\b"
    r"|\bper the user\b|\baccording to the user\b)", re.I)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
COMMIT_RE = re.compile(r"^=== ([0-9a-f]+) (.*)$")
# source: SPEC (Issue #1 asks for conflict listing); 0.75 catches near-identical
# fact lines whose numbers disagree while ignoring unrelated pages.
CONFLICT_RATIO = 0.75
# source: PR review P2; a single character or other very short common substring
# cannot verify that the user actually said a fact. Four CJK characters is the
# shortest fragment that carries a word rather than a stray character.
MIN_QUOTE_CHARS = 4


def flatten(text):
    """Whitespace-insensitive form used for quote checks (same rule as dream)."""
    return re.sub(r"\s+", "", text or "")


def quote_fragments(quote):
    """Split a quote at ``…`` / ``...`` and flatten each non-empty fragment."""
    return [flatten(f) for f in re.split(r"…+|\.\.\.", quote or "") if flatten(f)]


def quote_matches(quote, text):
    """True when every quote fragment is long enough and occurs in ``text``."""
    frags = quote_fragments(quote)
    return bool(frags) and all(len(f) >= MIN_QUOTE_CHARS and f in flatten(text) for f in frags)


def _is_fact_line(text):
    s = text.strip()
    if not s or s.startswith(("#", "---", "+++", "@@")):
        return False
    if re.match(r"^(name|description|metadata|scope):\s*", s):
        return False
    return True


def parse_git_show(text, default_sids=()):
    """Parse ``git show --unified=0 --format=tformat:=== %h %s`` output.

    Returns one dict per added fact line:
    ``{sha, subject, file, line, text, sids}``.  Line numbers refer to the new
    file in that commit.  ``default_sids`` is used when a commit subject has no
    sid8 (for example a manually written test fixture).
    """
    changes, sha, subject, sids, file, new_line = [], "", "", list(default_sids), None, 0
    for raw in text.splitlines():
        m = COMMIT_RE.match(raw)
        if m:
            sha, subject = m.group(1), m.group(2)
            sids = [s for s in SID_RE.findall(subject)] or list(default_sids)
            file = None
            continue
        if raw.startswith("+++ b/"):
            file = raw[6:].strip()
            continue
        if raw.startswith("+++ "):
            file = None
            continue
        m = HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(1))
            continue
        if file is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            fact = raw[1:].rstrip()
            if _is_fact_line(fact):
                changes.append({"sha": sha, "subject": subject, "file": file, "line": new_line,
                                "text": fact, "sids": list(sids)})
            new_line += 1
        elif raw.startswith(" ") or raw.startswith("+"):
            new_line += 1
    return changes


def _texts_for(line, change, session_texts):
    """The session texts a quote may be checked against: an explicit 8-char sid8 on
    the line wins (and must exist); otherwise the sessions of the commit."""
    explicit = [s for s in SID_RE.findall(line) if len(s) == 8]
    if explicit:
        return [s for s in explicit if s in session_texts]
    sids = [s for s in (change.get("sids") or []) if s in session_texts]
    return sids or list(session_texts)


def quote_checks(line, change, session_texts):
    """Check every 「quote」 on a line against the cited sessions' USER lines."""
    checks = []
    for quote in QUOTE_RE.findall(line):
        frags = quote_fragments(quote)
        sids = _texts_for(line, change, session_texts)
        flat = {s: flatten(session_texts.get(s, "")) for s in sids}
        problems = []
        for f in frags:
            if len(f) < MIN_QUOTE_CHARS:
                problems.append(f"quote fragment too short to verify: {f}")
            elif not any(f in text for text in flat.values()):
                problems.append(f)
        checks.append({"check": "quote", "quote": quote, "ok": not problems,
                       "sids": sids,
                       "detail": "" if not problems else
                       "quote not found among the USER lines of the cited session(s): "
                       + ", ".join(problems)})
    return checks


def attribution_checks(line, change, session_texts):
    """A line that attributes words to the user must carry a verifiable quote."""
    if not ATTRIBUTION_RE.search(line):
        return []
    checks = quote_checks(line, change, session_texts)
    if not QUOTE_RE.search(line):
        return [{"check": "attribution", "ok": False,
                 "detail": "claims the user said/decided something but carries no 「」 quote"}]
    bad = [c for c in checks if not c["ok"]]
    return [{"check": "attribution", "ok": not bad,
             "detail": "" if not bad else "attributed quote is not in the cited session"}]


def check_changes(changes, session_texts):
    """Add deterministic checks to every change; returns the same list (copied entries)."""
    out = []
    for change in changes:
        c = dict(change)
        checks = quote_checks(c["text"], c, session_texts) + attribution_checks(c["text"], c, session_texts)
        c["checks"] = checks
        c["deterministic_ok"] = all(x["ok"] for x in checks)
        out.append(c)
    return out


def _fact_entries(changes, pages):
    entries = []
    for c in changes:
        entries.append({"file": c["file"], "line": c["line"], "text": c["text"], "source": "change"})
    for path, text in (pages or {}).items():
        if not path.endswith(".md") or path == "log.md" or path.endswith("README.md"):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _is_fact_line(line):
                entries.append({"file": path, "line": i, "text": line, "source": "page"})
    seen, out = set(), []
    for e in entries:
        key = (e["file"], e["line"], e["text"].strip())
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def _normalize_fact(text):
    s = text.strip().lstrip("-*").strip().lower()
    s = re.sub(r"\(\d{4}-\d\d-\d\d\)", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _numbers(text):
    return re.findall(r"\d+(?:\.\d+)?", text)


def find_conflicts(changes, pages):
    """List likely conflicts: similar fact lines on different pages whose numbers
    disagree, plus explicit ``key: value`` facts with different values."""
    entries = _fact_entries(changes, pages)
    conflicts = []

    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if a["file"] == b["file"]:
                continue
            na, nb = _normalize_fact(a["text"]), _normalize_fact(b["text"])
            if len(na) < 8 or len(nb) < 8:
                continue
            na_nums, nb_nums = _numbers(na), _numbers(nb)
            if not (na_nums or nb_nums) or na_nums == nb_nums:
                continue
            masked_a = re.sub(r"\d+(?:\.\d+)?", "#", na)
            masked_b = re.sub(r"\d+(?:\.\d+)?", "#", nb)
            if masked_a == masked_b or \
                    difflib.SequenceMatcher(None, na, nb).ratio() >= CONFLICT_RATIO:
                conflicts.append({"kind": "similar",
                                  "files": [a["file"], b["file"]],
                                  "lines": [a["line"], b["line"]],
                                  "texts": [a["text"], b["text"]]})

    kv_re = re.compile(r"(?P<key>[A-Za-z][A-Za-z0-9_. -]{1,24}|[\u4e00-\u9fff]{2,12})\s*[:：=]\s*"
                       r"(?P<val>[^\s，。；;,]{1,50})")
    by_key = {}
    for e in entries:
        m = kv_re.search(e["text"])
        if not m:
            continue
        key = flatten(m.group("key")).lower()
        if key in ("description", "name", "scope", "title", "section", "http", "https"):
            continue
        by_key.setdefault(key, []).append((e, m.group("val")))
    for key, items in by_key.items():
        values = {flatten(v) for _, v in items}
        if len(values) > 1 and len({e["file"] for e, _ in items}) > 1:
            conflicts.append({"kind": "key", "key": key,
                              "files": [e["file"] for e, _ in items],
                              "lines": [e["line"] for e, _ in items],
                              "texts": [e["text"] for e, _ in items]})

    seen, out = set(), []
    for c in conflicts:
        k = (c["kind"], tuple(sorted(c["files"])), tuple(sorted(c["lines"])),
             tuple(sorted(c.get("texts", []))))
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _verified(quote, sid, session_texts):
    return quote_matches(quote, session_texts.get(sid or "", ""))


def apply_verdicts(changes, data, session_texts):
    """Apply the model's JSON verdicts to the checked changes.

    ``data`` may be ``{"verdicts": [...]}`` or a bare list; each item uses a
    1-based ``index`` matching the order of ``changes``.
    """
    items = data if isinstance(data, list) else (data or {}).get("verdicts") or (data or {}).get("changes") or []
    by_index = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index") or item.get("i") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(changes):
            by_index[idx] = item
    out = []
    for i, original in enumerate(changes, 1):
        c = dict(original)
        item = by_index.get(i) or {}
        verdict = str(item.get("verdict") or "").lower()
        if verdict not in VERDICTS:
            verdict = UNJUDGED
        evidence = []
        for e in item.get("evidence") or []:
            if not isinstance(e, dict):
                continue
            quote = str(e.get("quote") or "").strip().strip("「」")
            if not quote:
                continue
            sid = str(e.get("sid8") or e.get("sid") or "").strip()[:8]
            evidence.append({"sid8": sid, "quote": quote,
                             "verified": _verified(quote, sid, session_texts)})
        c["verdict"] = verdict
        c["evidence"] = evidence
        c["reason"] = str(item.get("reason") or "")
        if verdict == "misattributed":
            c["action"] = "auto-fix"
        elif verdict == "contradicted":
            c["action"] = "auto-fix" if any(e["verified"] for e in evidence) else "todo"
        elif verdict in ("stale", "unsupported", UNJUDGED):
            c["action"] = "todo"
        else:
            c["action"] = "none"
        c["fixed"] = False
        out.append(c)
    return out


def changes_context(changes):
    """Prompt section: one numbered change per added fact line, with deterministic findings."""
    lines = []
    for i, c in enumerate(changes, 1):
        sids = ",".join(c.get("sids") or []) or "-"
        lines.append(f"=== CHANGE {i} | {c['file']}:{c['line']} | sid8={sids} ===")
        lines.append(c["text"])
        bad = [x for x in c.get("checks", []) if not x.get("ok")]
        if bad:
            lines.append("DETERMINISTIC: " + " | ".join(x.get("detail", x["check"]) for x in bad))
        else:
            lines.append("DETERMINISTIC: ok")
    return "\n".join(lines)


def summarize(changes, conflicts):
    summary = {v: 0 for v in VERDICTS}
    summary[UNJUDGED] = 0
    for c in changes:
        summary[c.get("verdict", UNJUDGED)] = summary.get(c.get("verdict", UNJUDGED), 0) + 1
    summary["total"] = len(changes)
    summary["fixed"] = sum(1 for c in changes if c.get("fixed"))
    summary["todos"] = sum(1 for c in changes if c.get("action") == "todo")
    summary["conflicts"] = len(conflicts)
    return summary


def build_report(changes, conflicts, date=None, since=None, engine=""):
    return {"schema": 1,
            "date": date or time.strftime("%Y-%m-%d"),
            "generated_at": time.strftime("%F %T"),
            "since": since,
            "engine": engine,
            "summary": summarize(changes, conflicts),
            "changes": changes,
            "conflicts": conflicts}


def merge_report(old, new):
    """Merge two same-day reports, keyed by commit + file + line + text."""
    if not old:
        return new
    key = lambda c: (c.get("sha", ""), c.get("file", ""), c.get("line", 0), c.get("text", ""))
    changes = {key(c): c for c in old.get("changes", [])}
    for c in new.get("changes", []):
        changes[key(c)] = c
    conflicts = list(old.get("conflicts", []))
    seen = {json.dumps(c, sort_keys=True, ensure_ascii=False) for c in conflicts}
    for c in new.get("conflicts", []):
        k = json.dumps(c, sort_keys=True, ensure_ascii=False)
        if k not in seen:
            seen.add(k)
            conflicts.append(c)
    merged = build_report(list(changes.values()), conflicts, new.get("date"), new.get("since"), new.get("engine", ""))
    return merged


def write_report(path, report):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def summary_line(report, language="zh"):
    s = report.get("summary", {})
    if language == "zh":
        return (f"- Diff Engine：共 {s.get('total', 0)} 行；supported {s.get('supported', 0)} / "
                f"contradicted {s.get('contradicted', 0)} / stale {s.get('stale', 0)} / "
                f"misattributed {s.get('misattributed', 0)} / unsupported {s.get('unsupported', 0)} / "
                f"unjudged {s.get('unjudged', 0)}；自动修复 {s.get('fixed', 0)}；"
                f"待办 {s.get('todos', 0)}；冲突 {s.get('conflicts', 0)}")
    return (f"- Diff Engine: {s.get('total', 0)} lines; supported {s.get('supported', 0)} / "
            f"contradicted {s.get('contradicted', 0)} / stale {s.get('stale', 0)} / "
            f"misattributed {s.get('misattributed', 0)} / unsupported {s.get('unsupported', 0)} / "
            f"unjudged {s.get('unjudged', 0)}; auto-fixed {s.get('fixed', 0)}; "
            f"todos {s.get('todos', 0)}; conflicts {s.get('conflicts', 0)}")


def todos_markdown(report, language="zh"):
    """The human-facing todo list for verdicts the review must not auto-change."""
    rows = [c for c in report.get("changes", []) if c.get("action") == "todo"]
    if not rows:
        return ""
    head = "### Diff 待办（未自动修改，留给人看）" if language == "zh" else "### Diff todos (not auto-changed)"
    out = [head]
    for c in rows:
        ev = "; ".join(f"{e.get('sid8') or '?'}「{e['quote']}」" for e in c.get("evidence", []))
        out.append(f"- [{c.get('verdict')}] {c['file']}:{c['line']} {c['text'].strip()}"
                   + (f" — {ev}" if ev else ""))
    return "\n".join(out)
