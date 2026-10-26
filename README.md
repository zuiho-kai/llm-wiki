# llm-wiki

English | [简体中文](README.zh-CN.md)

A layered, git-backed memory for [Claude Code](https://docs.claude.com/en/docs/claude-code), maintained by LLMs
(the "LLM wiki" pattern). Your Claude Code sessions are the raw source. A pipeline of model passes distills
them into a small wiki that Claude reads top-down: an index of domains, one README per domain, and one short
page per topic. Every model pass is checked by scripts before it reaches the wiki.

## What you get

- **A schema** (`CLAUDE.md`): layers, size budgets, page format and writing rules that every model follows.
- **A linter** (`bin/lint.py`): enforces the budgets, page format and index consistency.
- **An ingest pipeline** (`bin/ingest.py`): turns finished sessions into wiki edits, reviews them at night,
  lints the whole wiki weekly, and collects "rule candidates" from what you keep telling Claude.
- **Claude Code integration**: a SessionEnd hook, the `autoMemoryDirectory` setting, and a block for your global
  `~/.claude/CLAUDE.md` that tells Claude how to read and write the wiki.
- **launchd templates** (macOS) for the scheduled runs.

## Layers

```
MEMORY.md                 L0  one line per domain             (≤ 8 domains + misc, ≤ 80 chars/line)
  └─ <domain>/README.md   L1  one line per topic              (≤ 15 topics, ≤ 120 chars/line)
       └─ <topic>.md      L2  current state + key facts       (≤ 60 lines, ≤ 4000 chars)
log.md                    L3  dated history, session ids      (append-only, not loaded by default)
```

Claude reads only as deep as the question needs: `MEMORY.md` → `infra/README.md` → `infra/server.md`.
Each topic page has a `scope:` that says what belongs there, so every fact has exactly one home. Pages
use four fixed sections: `Current state`, `Key facts`, `Decisions & pitfalls` and `Open items` (with
`"language": "zh"`: `现状`, `关键事实`, `决策与踩坑`, `待办`). See [Language](#language).

## Pipeline

```
 session ends ──SessionEnd hook──┐
 every 30 min (--pending) ───────┤
                                 v
                ┌───────────── day ingest ─────────────┐
                │ day engine (haiku) → fallback (sonnet)│
                └──────────────────┬───────────────────┘
                                   v
   ┌──────── each pass: git worktree .ingest-work ────────┐
   │ model edits pages → acceptance checks → 1 fix round  │
   │   ok: commit, cherry-pick onto the wiki, push*       │
   │   fail: reset the worktree, retry on a later run     │
   └──────────────────────────────────────────────────────┘
                                   ^
 Mon–Sat 03:00 (--nightly): ingest leftovers → split full pages → dream → review the day's writes
 Sun     03:00 (--lint):    lint the whole wiki → dream
                                                         * only if `remote` is configured
```

- **Day.** When a session closes (hook), or after it has been idle for 30 minutes (`--pending`), a fast model
  ingests the new part of the transcript. There is no checking pass. If the model fails, the fallback engine
  redoes the batch. If the Claude quota refuses the call, the batch waits for the night.
- **Night (`--nightly`).** The night engine ingests what the day left. Then the **Diff Engine** reads each
  transcript span that was ingested during the day, together with the diff the day pass wrote, one added fact
  line at a time. Script checks run first: every `「quote」` and every "the user said / decided" line must match a
  USER line of the cited session, and conflicts between pages are listed as hints (not verdicts: similar
  templates on different pages can be false positives). A model then gives each line one
  verdict — `supported`, `contradicted`, `stale`, `misattributed` or `unsupported` — with sid8 + quote
  evidence. Only `misattributed` and `contradicted` lines with script-verified evidence are auto-fixed; the rest
  become todos and are not deleted. The report is `state/diff-YYYY-MM-DD.json`; `log.md` gets one summary line
  plus the human-readable todo lines.
  The review engine then fills gaps and dedupes without touching the todo lines.
- **Week (`--lint`).** The lint engine checks the whole wiki: navigation, duplicates, contradictions, stale
  facts, scope, format and budgets. It also reads your resident rule files (`rule_files`) and suggests rules
  to move, delete or merge. It never edits those files.
- **Dream / Proposal Engine (every night).** A model reads only what you said since the last dream, each line
  together with the assistant reply before it. It outputs structured candidates: a stable dedup key, the rule
  draft, the target (a resident rule file + section, or a hook/test), and every occurrence with date + sid8 +
  verbatim quote. The script normalizes keys and merges records that share a normalized key, an identical draft,
  or a near-identical normalized draft (numeric variants stay separate); the dream prompt asks the model to use
  one canonical key for synonymous candidates (the script does not infer semantics from different wording). Only
  a candidate with at least 2 occurrences is shown as an open item in `misc/rule-candidates.md`, and a candidate
  you rejected is never proposed again. `ingest.py proposals list` shows the records; `accept <id>` refuses a
  candidate below that threshold, writes a relative patch under `state/proposals/` without editing the rule
  file, and prints the directory to apply it from; `reject <id> [reason]` records the decision. The page is
  generated from `misc/rule-candidates.json` (tracked), so the human page and machine state stay in sync.
- **Acceptance (every pass).** The script does not trust what the model reports. It checks that `bin/lint.py`
  passes, that each session has a new `log.md` entry, that only wiki pages changed (not `CLAUDE.md`, `bin/`,
  `schema/` or hidden files), that `log.md` is append-only, and that there is no mass deletion.
- **Isolation.** Models edit a separate git worktree (`.ingest-work`), never your working tree. Only an accepted
  commit is cherry-picked onto the wiki, so your own uncommitted edits are safe.
- **Progress.** A byte offset per session is stored in `.ingest.sqlite`, so a resumed session is processed only
  from where it stopped.

## Requirements

- macOS (launchd for scheduling). The scripts also run on Linux; there you set up the schedule yourself.
- Python ≥ 3.9 (standard library only) and git.
- The [`claude` CLI](https://docs.claude.com/en/docs/claude-code), logged in. This is the default engine.
- Optional engines: the `codex` CLI (OpenAI Codex) and the `agy` CLI (Google Antigravity).

## Install

1. **Clone** the repository. The clone becomes your wiki, so put it where you want the wiki to live:
   ```sh
   git clone https://github.com/zuiho-kai/llm-wiki ~/wiki && cd ~/wiki
   git remote rename origin upstream   # your wiki holds personal data: never push it to the public repo
   ```
2. **Configure.** Every key has a default, so this step is optional for English. Copy the example and edit it:
   ```sh
   cp config.example.json config.json   # git-ignored
   ```
   See [Configuration](#configuration).
3. **Choose the language** before the first run. English (`"language": "en"`) is the default and needs nothing
   more. For Chinese, set `"language": "zh"` in `config.json` and switch the schema:
   ```sh
   cp schema/CLAUDE.zh-CN.md CLAUDE.md && git commit -qam "schema: Chinese"
   ```
   See [Language](#language) for what this changes.
4. **Connect Claude Code.** Add these entries to `~/.claude/settings.json`. Merge them with what is already
   there; do not replace the file:
   ```json
   {
     "autoMemoryDirectory": "~/wiki",
     "hooks": {
       "SessionEnd": [
         { "hooks": [ { "type": "command", "command": "/usr/bin/python3 -I ~/wiki/bin/ingest.py --hook", "timeout": 10 } ] }
       ]
     }
   }
   ```
5. **Tell Claude how to use the wiki.** Append the memory instructions to your global `~/.claude/CLAUDE.md`,
   in the language you chose in step 3:
   ```sh
   cat schema/global-memory.md >> ~/.claude/CLAUDE.md          # English
   cat schema/global-memory.zh-CN.md >> ~/.claude/CLAUDE.md    # 中文
   ```
6. **Schedule the runs (macOS).** This writes three LaunchAgents with your paths filled in and prints the
   commands that load them:
   ```sh
   launchd/install.sh
   ```
   | Agent | Runs | When |
   |---|---|---|
   | `local.llm-wiki.ingest` | `ingest.py --pending` | every 30 minutes |
   | `local.llm-wiki.nightly` | `ingest.py --nightly` | Monday–Saturday 03:00 |
   | `local.llm-wiki.weekly` | `ingest.py --lint` | Sunday 03:00 |

   On other platforms, set up the same three schedules with your own scheduler.

   **The first run processes your whole history.** The first `--pending` run (the hook or the 30-minute
   schedule) ingests every existing session in `transcripts` that has been idle for 30 minutes, not only new
   ones. With a long history this can run for hours and use a large part of your model quota, and the nightly
   run waits for it. Check what it will take with `python3 bin/ingest.py --status` before you load the agents.
7. **Back up to a private remote (optional).** Add a private repository and set `"remote"` in `config.json`.
   Each run then pulls with rebase before it starts and pushes accepted commits. A new, empty repository is
   fine: the first run creates the branch there.
   ```sh
   git remote add origin <your-private-repo-url>
   ```
8. **Check:**
   ```sh
   python3 bin/lint.py              # lint ok
   python3 bin/ingest.py --status   # sessions with new content
   ```

If you change the wiki path, change it in all three places: the `autoMemoryDirectory` setting, the hook
command and the global memory block. The launchd templates take the path from where `install.sh` is.

## Language

`language` in `config.json` is `"en"` (default) or `"zh"`. It sets:

- the step-by-step prompts sent to the models (`SOP_*_EN` or `SOP_*_ZH` in `bin/ingest.py`), so pages are
  written in that language;
- the four section names `bin/lint.py` accepts: `Current state` / `Key facts` / `Decisions & pitfalls` /
  `Open items`, or `现状` / `关键事实` / `决策与踩坑` / `待办`;
- the bookkeeping phrases `bin/lint.py` rejects (for example "(per the user)" or "（用户告知）").

Use the matching schema (`CLAUDE.md` = `schema/CLAUDE.md` for English, `schema/CLAUDE.zh-CN.md` for Chinese) and
the matching memory block (`schema/global-memory*.md`). `bin/lint.py --language en|zh` overrides the setting for
one run. If you switch later, existing pages keep their old section names and fail lint until you rename them.

## Configuration

`config.json` sits next to `MEMORY.md`. All keys are optional. An unknown key stops the script with an error.

| Key | Default | Meaning |
|---|---|---|
| `transcripts` | `~/.claude/projects` | Claude Code transcripts (`<project>/<session>.jsonl`) |
| `ingest_engine` | `haiku` | day engine |
| `fallback_engine` | `sonnet` | redoes a day batch the day engine failed |
| `night_engine` | `sonnet` | night: ingests what the day left |
| `review_engine` | `sonnet` | night: reviews the day's writes |
| `lint_engine` | `sonnet` | weekly lint |
| `dream_engine` | `sonnet` | nightly dream |
| `split_engine` | `sonnet` | splits pages at ≥ 90 % of budget before the next ingest |
| `claude_model` | `sonnet` | model used for the engine name `claude` |
| `codex_models` | `[]` | `codex` engine: models tried in turn on capacity errors (`[]` = Codex default) |
| `codex_effort` | `max` | `codex` reasoning effort when the engine has no `:effort` suffix |
| `codex_tier` | `""` | `codex` service tier (`"fast"` = Fast mode) |
| `after_codex` | `["haiku"]` | engines tried once all `codex_models` failed |
| `agy_model` | `gemini-3.8-flash-high` | model for the `agy` engine |
| `night_budget` | `18000` | seconds a night run may spend; the rest waits for the next night |
| `rule_files` | `["~/.claude/CLAUDE.md"]` | resident rule files that dream and the weekly lint read (never edit) |
| `remote` | `""` | git remote to pull from and push to (`""` = local only) |
| `proxy` | `""` | HTTP(S) proxy tried first for pull/push, then a direct connection |
| `language` | `"en"` | `"en"` or `"zh"`: model prompts, page section names, lint phrases ([Language](#language)) |
| `diff_engine` | `true` | `true`: the nightly Diff Engine + per-line verdicts + `state/diff-*.json`; `false`: the old review pass |
| `proposal_engine` | `true` | `true`: structured dream candidates + the proposals CLI + generated page; `false`: the old dream |

Engine names: `haiku`, `sonnet`, `opus` (or any model name the `claude` CLI accepts), `claude`
(= `claude_model`), `codex` or `codex:<effort>` (for example `codex:high`), and `agy`. All engines get the
same step-by-step prompts (`SOP_*` in `bin/ingest.py`). The script checks every result the same way.

## Commands

```
ingest.py --pending        ingest every session idle ≥ 30 min with new content
ingest.py --session ID     ingest one session now (queued if a run is going)
ingest.py --hook           SessionEnd hook entry: reads the hook JSON on stdin, runs --session detached
ingest.py --nightly        night run: leftovers, split, dream, review
ingest.py --lint           weekly lint, then dream
ingest.py --status         list sessions with new content
ingest.py --usage [DAYS]   token use per day and model
ingest.py proposals list [--json]   structured rule candidates (open / accepted / rejected)
ingest.py proposals accept ID            write a patch under state/proposals/, never edit the rule file;
                                         prints the directory to apply it from (≥2 occurrences required)
ingest.py proposals reject ID [reason]   record a rejection; the candidate is never proposed again
lint.py [--stats|--warn] [--language en|zh]   structural check / sizes / near-budget warnings
```

Runtime files (all git-ignored) sit in the wiki root: `.ingest.log`, `.ingest.sqlite`, `.ingest-usage.tsv`,
`.ingest.lock`, `.ingest.queue`, `state/` (Diff Engine reports, `proposals/*.patch`) and the
`.ingest-work/` worktree. The structured proposal records in `misc/rule-candidates.json` are tracked, like the page.

## Finding the original conversation

Topic pages keep conclusions, and `log.md` keeps the session id (`sid8`) of every change. When you need the
full discussion, open that session in Claude Code (`claude --resume <session-id>`). A session-search tool,
such as a "resume session" skill, can also find it by topic.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The ingest tests run the real pipeline against a stand-in `claude` CLI (`tests/fake-engine/claude`). The fake
writes pages the way a model would, so the tests cover acceptance, rejection, fallback, the hook, the nightly
Diff Engine (report + auto-fix of a fabricated attribution), the structured dream, the proposals CLI and the
first push to an empty remote, in both languages, with no model calls. `tests/test_diff_engine.py` and
`tests/test_proposal_engine.py` exercise the deterministic checks, dedup/threshold/reject logic and the
accept-patch rule directly on small samples.

## License

[MIT](LICENSE)
