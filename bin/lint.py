#!/usr/bin/env python3 -I
"""Structural checks for the wiki (budgets from CLAUDE.md). Prints violations; exit 1 if any.

  lint.py          check
  lint.py --stats  also print sizes per layer
  lint.py --warn   print only soft warnings (≥ WARN of a budget: split early, in the weekly LINT pass)
  lint.py --language en|zh   page language; default: "language" in config.json, else en
"""
import json, re, sys
from pathlib import Path

WIKI = Path(__file__).resolve().parent.parent
MAX_DOMAINS, INDEX_LINE = 8, 80
MAX_TOPICS, README_LINE = 15, 120
PAGE_LINES, PAGE_CHARS = 60, 4000
WARN = 0.8
BODY_LINE = 300  # one bullet = one fact; longer lines dodge PAGE_LINES and are unreadable
# per page language: the four allowed sections, verification bookkeeping that leaked onto a page instead of
# fixing the fact, and the session-list heading that belongs in log.md
LANGUAGES = {
    "zh": {"sections": ("现状", "关键事实", "决策与踩坑", "待办"),
           "meta": re.compile(r"不声称|未见用户确认|不记作现行|（用户告知）|\(用户告知\)|本会话中"),
           "sessions": "## 相关会话", "todo": "待办"},
    "en": {"sections": ("Current state", "Key facts", "Decisions & pitfalls", "Open items"),
           "meta": re.compile(r"not claiming|does not claim|not confirmed by the user|unconfirmed by the user|"
                              r"not recorded as current|\((?:per the user|as the user said|user said)\)|in this session", re.I),
           "sessions": "## Related sessions", "todo": "Open items"},
}
SPECIAL = {"misc"}
TOOL_DIRS = {"bin", "launchd", "schema", "tests", "state"}  # the tool's own directories in the repo, not wiki domains
TOP_FILES = {"MEMORY.md", "CLAUDE.md", "log.md", "README.md", "README.zh-CN.md"}  # README*: the repo's own readme
LINK = re.compile(r"\]\(([^)]+)\)")
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


def entries(path):
    return [l for l in path.read_text().splitlines() if l.startswith("- [")]


def frontmatter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return dict(re.findall(r"^[ \t]*(\w+):[ \t]*(.+)$", m.group(1), re.M)) if m else None


def language():
    if "--language" in sys.argv[:-1]:
        return sys.argv[sys.argv.index("--language") + 1]
    cfg = WIKI / "config.json"
    return json.loads(cfg.read_text()).get("language", "en") if cfg.exists() else "en"


def check(lang="en"):
    SECTIONS, META = LANGUAGES[lang]["sections"], LANGUAGES[lang]["meta"]
    errs, stats, warns = [], [], []
    links = set()
    index = WIKI / "MEMORY.md"
    lines = index.read_text().splitlines()
    doms = entries(index)
    if len(lines) != len(doms):
        errs.append(f"MEMORY.md: only domain entry lines allowed ({len(lines) - len(doms)} other lines)")
    linked = {LINK.search(l).group(1).split("/")[0] for l in doms if LINK.search(l)}
    if len(linked - SPECIAL) > MAX_DOMAINS:
        errs.append(f"MEMORY.md: {len(linked - SPECIAL)} domains > {MAX_DOMAINS}; merge domains")
    for l in doms:
        if len(l) > INDEX_LINE:
            errs.append(f"MEMORY.md: line over {INDEX_LINE} chars ({len(l)}): {l[:50]}…")
    stats.append(f"L0 MEMORY.md: {len(doms)} domains, {len(index.read_text())} chars")

    dirs = {p.name for p in WIKI.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name not in TOOL_DIRS}
    for d in sorted(dirs - linked):
        errs.append(f"{d}/: domain not listed in MEMORY.md")
    for d in sorted(linked):
        readme = WIKI / d / "README.md"
        if not readme.exists():
            errs.append(f"{d}/README.md missing")
            continue
        tops = entries(readme)
        if len(tops) > MAX_TOPICS:
            errs.append(f"{d}/README.md: {len(tops)} topics > {MAX_TOPICS}; split the domain")
        if MAX_TOPICS * WARN <= len(tops) <= MAX_TOPICS:
            warns.append(f"{d}/README.md: {len(tops)}/{MAX_TOPICS} topics; consider splitting the domain")
        for l in tops:
            if len(l) > README_LINE:
                errs.append(f"{d}/README.md: line over {README_LINE} chars ({len(l)}): {l[:50]}…")
        listed = {LINK.search(l).group(1) for l in tops if LINK.search(l)}
        pages = {p.name for p in (WIKI / d).glob("*.md") if p.name != "README.md"}
        for p in sorted(pages - listed):
            errs.append(f"{d}/{p}: orphan page, not listed in {d}/README.md")
        for p in sorted(listed - pages):
            errs.append(f"{d}/README.md: links missing page {p}")
        for name in sorted(pages):
            text = (WIKI / d / name).read_text()
            n_lines, n_chars = len(text.splitlines()), len(text)
            fm = frontmatter(text)
            if not fm or not {"name", "description", "scope"} <= fm.keys():
                errs.append(f"{d}/{name}: frontmatter needs name, description, scope")
            if n_lines > PAGE_LINES or n_chars > PAGE_CHARS:
                errs.append(f"{d}/{name}: {n_lines} lines / {n_chars} chars > {PAGE_LINES}/{PAGE_CHARS}; split into subtopics or compress")
            elif max(n_lines / PAGE_LINES, n_chars / PAGE_CHARS) >= WARN:
                warns.append(f"{d}/{name}: {n_lines}/{PAGE_LINES} lines, {n_chars}/{PAGE_CHARS} chars; split into subtopics")
            if LANGUAGES[lang]["sessions"] in text:
                errs.append(f"{d}/{name}: session lists belong in log.md, not on topic pages")
            body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
            heads = re.findall(r"^## (.+)$", body, re.M)
            for h in heads:
                if not h.startswith(SECTIONS):
                    errs.append(f"{d}/{name}: section '## {h}' not in {'/'.join(SECTIONS)}; fold it into one of them")
            if not heads and len(body.splitlines()) > 8:
                errs.append(f"{d}/{name}: no ## sections; use {'/'.join(SECTIONS)}")
            for i, l in enumerate(body.splitlines(), 1):
                if len(l) > BODY_LINE:
                    errs.append(f"{d}/{name}: body line {i} is {len(l)} chars > {BODY_LINE}; split into bullets or move detail to log.md")
                if META.search(l):
                    errs.append(f"{d}/{name}: body line {i} has verification bookkeeping ('{META.search(l).group()}'); state the fact or make it a {LANGUAGES[lang]['todo']}")
            links.update((f"{d}/{name}", n) for n in WIKILINK.findall(body))
            stats.append(f"L2 {d}/{name}: {n_lines} lines, {n_chars} chars")
        stats.append(f"L1 {d}/README.md: {len(tops)} topics")
    names = {p.stem for p in WIKI.glob("*/*.md") if p.name != "README.md"}
    for src, n in sorted(links):
        if n not in names:
            warns.append(f"{src}: [[{n}]] has no page yet")
    stray = [p.name for p in WIKI.glob("*.md") if p.name not in TOP_FILES]
    for p in stray:
        errs.append(f"{p}: top-level page; move it into a domain")
    return errs, stats, warns


if __name__ == "__main__":
    lang = language()
    if lang not in LANGUAGES:
        sys.exit(f"unknown language {lang!r}: use {' or '.join(LANGUAGES)}")
    errs, stats, warns = check(lang)
    if "--warn" in sys.argv:
        print("\n".join(warns))
        sys.exit(0)
    if "--stats" in sys.argv:
        print("\n".join(stats))
    print("\n".join(errs) if errs else "lint ok")
    sys.exit(1 if errs else 0)
