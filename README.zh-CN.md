# llm-wiki

[English](README.md) | 简体中文

给 [Claude Code](https://docs.claude.com/en/docs/claude-code) 用的分层记忆，存在 git 里，由 LLM 维护（「LLM wiki」模式）。
原始资料是你的 Claude Code 会话。一条由多轮模型处理组成的流水线把会话提炼成一个小 wiki，Claude 自上而下读它：
一个域索引、每个域一个 README、每个主题一页短文。每一轮模型的产出都先由脚本验收，再进 wiki。

## 包含什么

- **Schema**（`CLAUDE.md`）：分层、大小预算、页面格式和写作规则，所有模型都按它来。
- **Linter**（`bin/lint.py`）：检查预算、页面格式和索引一致性。
- **写入流水线**（`bin/ingest.py`）：把结束的会话写成 wiki 改动，夜里复核，每周整理整个 wiki，
  还从你反复对 Claude 说的话里整理出「规则候选」。
- **Claude Code 接入**：SessionEnd hook、`autoMemoryDirectory` 设置，以及一段放进全局 `~/.claude/CLAUDE.md`
  的指令，告诉 Claude 怎么读写这个 wiki。
- **launchd 模板**（macOS），用于定时任务。

## 分层

```
MEMORY.md                 L0  每个域一行                       （≤ 8 个域 + misc，每行 ≤ 80 字符）
  └─ <domain>/README.md   L1  每个主题一行                     （≤ 15 个主题，每行 ≤ 120 字符）
       └─ <topic>.md      L2  现状 + 关键事实                  （≤ 60 行，≤ 4000 字符）
log.md                    L3  带日期的历史、会话 id            （只追加，默认不加载）
```

Claude 只读到问题需要的深度：`MEMORY.md` → `infra/README.md` → `infra/server.md`。
每个主题页有一个 `scope:`，写明哪些内容属于这一页，所以每个事实只有一个归属。页面固定用四节：
`Current state`、`Key facts`、`Decisions & pitfalls`、`Open items`（设 `"language": "zh"` 时是 `现状`、`关键事实`、
`决策与踩坑`、`待办`）。见[语言](#语言)。

## 流水线

```
 会话结束 ──SessionEnd hook──┐
 每 30 分钟（--pending）─────┤
                             v
            ┌──────────── 白天写入 ────────────┐
            │ 白天引擎(haiku) → 兜底引擎(sonnet) │
            └────────────────┬─────────────────┘
                             v
   ┌──────── 每一轮：git worktree .ingest-work ────────┐
   │ 模型改页面 → 脚本验收 → 最多 1 轮修复             │
   │   通过：commit，cherry-pick 到 wiki，push*        │
   │   不通过：重置 worktree，下次运行再试             │
   └───────────────────────────────────────────────────┘
                             ^
 周一至周六 03:00（--nightly）：补写剩余 → 拆分满页 → dream → 复核白天写入
 周日       03:00（--lint）：   整理整个 wiki → dream
                                                    * 只有配置了 `remote` 才 push
```

- **白天。** 会话关闭时（hook），或空闲满 30 分钟后（`--pending`），快模型写入会话记录里新增的部分，不做核对。
  模型失败时由兜底引擎重做这一批。Claude 额度拒绝时，这一批留到晚上。
- **夜间（`--nightly`）。** 夜间引擎写入白天剩下的。然后 **Diff Engine** 读白天写入过的每段会话记录，连同白天那一轮
  写出的 git diff，按一个新增事实行一条来核对。先跑确定性检查：每个 `「引文」`、每条「用户说/用户定」都必须对上
  所引会话的 USER 行，并列出跨页面的冲突（只是提示，不是结论；不同页面上的相似模板可能误报）；再由模型给每行一个结论：
  `supported` / `contradicted` / `stale` / `misattributed` / `unsupported`，附 sid8 + 原话证据。只有
  `misattributed` 和证据经脚本核实过的 `contradicted` 会自动修；其余写成待办，不擅自删改。报告在
  `state/diff-YYYY-MM-DD.json`，`log.md` 记一行汇总和可读的待办行。之后 review 引擎只补漏、去重，不碰待办行。
- **每周（`--lint`）。** lint 引擎检查整个 wiki：导航、重复、矛盾、过时、范围、格式、预算。它也读你的常驻规则文件
  （`rule_files`），建议哪些规则该移出、删除或合并。它从不改这些文件。
- **Dream / Proposal Engine（每晚）。** 模型只读上次 dream 以来你说的话，每句连同它前面那条助手回复。它输出结构化
  候选：稳定的去重键、规则草稿、目标（哪个规则文件的哪一节，或 hook/测试），以及每次出现（日期、sid8、逐字原话）。
  脚本规范化 key，并合并 key 相同、草稿逐字相同或近似相同的记录（只有数字不同的规则变体不合并）；dream 提示要求
  模型给同义候选同一个 canonical key（脚本不会只靠措辞猜语义）。出现不少于 2 次的才作为待办出现在
  `misc/rule-candidates.md`，你否决过的候选永远不再提。`ingest.py proposals list` 查看记录；`accept <id>`
  拒绝不足 2 次的候选，只在 `state/proposals/` 生成相对路径补丁、不改规则文件，并打印应从哪里应用补丁；
  `reject <id> [原因]` 记录否决。页面由 `misc/rule-candidates.json`（纳入 git）生成，人看的页面和机器状态保持一致。
- **验收（每一轮）。** 脚本不信模型的自述。它检查：`bin/lint.py` 通过；每个会话在 `log.md` 有一条新条目；
  只改了 wiki 页面（没改 `CLAUDE.md`、`bin/`、`schema/` 或隐藏文件）；`log.md` 只追加；没有大量删除。
- **隔离。** 模型在单独的 git worktree（`.ingest-work`）里改，从不碰你的工作区。只有验收通过的提交才 cherry-pick
  到 wiki，所以你自己没提交的改动不会被动到。
- **进度。** 每个会话的字节偏移存在 `.ingest.sqlite`，续接的会话只处理新增的部分。

## 环境要求

- macOS（定时用 launchd）。脚本在 Linux 上也能跑，定时任务需要自己配。
- Python ≥ 3.9（只用标准库）和 git。
- 已登录的 [`claude` CLI](https://docs.claude.com/en/docs/claude-code)，这是默认引擎。
- 可选引擎：`codex` CLI（OpenAI Codex）和 `agy` CLI（Google Antigravity）。

## 安装

1. **Clone** 仓库。clone 下来的目录就是你的 wiki，放在你想放 wiki 的位置：
   ```sh
   git clone https://github.com/zuiho-kai/llm-wiki ~/wiki && cd ~/wiki
   git remote rename origin upstream   # wiki 里是个人数据：绝不要 push 到公开仓库
   ```
2. **配置。** 每一项都有默认值，用英文时这一步可以跳过。复制示例再编辑：
   ```sh
   cp config.example.json config.json   # 已被 git 忽略
   ```
   见[配置项](#配置项)。
3. **选语言**，要在第一次运行之前选。默认是英文（`"language": "en"`），不用做别的。用中文：在 `config.json`
   里设 `"language": "zh"`，并换成中文 schema：
   ```sh
   cp schema/CLAUDE.zh-CN.md CLAUDE.md && git commit -qam "schema: Chinese"
   ```
   选完会改变什么，见[语言](#语言)。
4. **接入 Claude Code。** 把下面几项加进 `~/.claude/settings.json`。和已有内容合并，不要覆盖整个文件：
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
5. **告诉 Claude 怎么用 wiki。** 把记忆指令追加到全局 `~/.claude/CLAUDE.md`，语言和第 3 步一致：
   ```sh
   cat schema/global-memory.zh-CN.md >> ~/.claude/CLAUDE.md   # 中文
   cat schema/global-memory.md >> ~/.claude/CLAUDE.md         # English
   ```
6. **配置定时任务（macOS）。** 下面的命令生成三个填好路径的 LaunchAgent，并打印加载它们的命令：
   ```sh
   launchd/install.sh
   ```
   | Agent | 运行 | 时间 |
   |---|---|---|
   | `local.llm-wiki.ingest` | `ingest.py --pending` | 每 30 分钟 |
   | `local.llm-wiki.nightly` | `ingest.py --nightly` | 周一至周六 03:00 |
   | `local.llm-wiki.weekly` | `ingest.py --lint` | 周日 03:00 |

   其他平台用自己的定时工具配同样三项。

   **第一次运行会处理全部历史。** 第一次 `--pending`（hook 或每 30 分钟的定时任务）会写入 `transcripts` 下所有
   空闲满 30 分钟的已有会话，不只是新会话。历史长的话可能连跑好几个小时，消耗一大块模型额度，期间夜间任务要等它跑完。
   加载定时任务前，先用 `python3 bin/ingest.py --status` 看看有多少会话要处理。
7. **备份到私有远端（可选）。** 加一个私有仓库，并在 `config.json` 里设 `"remote"`。之后每次运行前先 rebase 拉取，
   验收通过的提交会 push 上去。新建的空仓库也可以，第一次运行会在远端建好分支：
   ```sh
   git remote add origin <你的私有仓库地址>
   ```
8. **检查：**
   ```sh
   python3 bin/lint.py              # lint ok
   python3 bin/ingest.py --status   # 有新内容的会话
   ```

如果换了 wiki 路径，三处都要改：`autoMemoryDirectory` 设置、hook 命令、全局记忆指令。launchd 模板的路径取自
`install.sh` 所在位置。

## 语言

`config.json` 里的 `language` 取 `"en"`（默认）或 `"zh"`。它决定：

- 发给模型的分步提示（`bin/ingest.py` 里的 `SOP_*_EN` 或 `SOP_*_ZH`），页面也就用这种语言写；
- `bin/lint.py` 接受的四个节名：`Current state` / `Key facts` / `Decisions & pitfalls` / `Open items`，
  或 `现状` / `关键事实` / `决策与踩坑` / `待办`；
- `bin/lint.py` 拦截的批注用语（比如 "(per the user)" 或「（用户告知）」）。

schema 和记忆指令要用同一种语言：英文用 `CLAUDE.md`（= `schema/CLAUDE.md`），中文用 `schema/CLAUDE.zh-CN.md`；
记忆指令用对应的 `schema/global-memory*.md`。`bin/lint.py --language en|zh` 可以临时覆盖一次。以后再换语言，
已有页面还是旧节名，要手动改名才能过 lint。

## 配置项

`config.json` 和 `MEMORY.md` 放在同一目录。所有键都可选。写了未知的键，脚本报错退出。

| 键 | 默认值 | 含义 |
|---|---|---|
| `transcripts` | `~/.claude/projects` | Claude Code 会话记录（`<project>/<session>.jsonl`） |
| `ingest_engine` | `haiku` | 白天引擎 |
| `fallback_engine` | `sonnet` | 白天引擎失败时重做这一批 |
| `night_engine` | `sonnet` | 夜间：写入白天剩下的 |
| `review_engine` | `sonnet` | 夜间：复核白天写入 |
| `lint_engine` | `sonnet` | 每周整理 |
| `dream_engine` | `sonnet` | 每晚 dream |
| `split_engine` | `sonnet` | 页面达到预算 90% 时，在下次写入前拆分 |
| `claude_model` | `sonnet` | 引擎名 `claude` 对应的模型 |
| `codex_models` | `[]` | `codex` 引擎：遇到容量错误时依次换用的模型（`[]` = Codex 默认模型） |
| `codex_effort` | `max` | 引擎名没带 `:effort` 后缀时 `codex` 的推理强度 |
| `codex_tier` | `""` | `codex` 服务档位（`"fast"` = Fast 模式） |
| `after_codex` | `["haiku"]` | 所有 `codex_models` 都失败后依次尝试的引擎 |
| `agy_model` | `gemini-3.8-flash-high` | `agy` 引擎的模型 |
| `night_budget` | `18000` | 夜间一次运行最多用的秒数；剩下的等下一晚 |
| `rule_files` | `["~/.claude/CLAUDE.md"]` | dream 和每周 lint 读取的常驻规则文件（只读不改） |
| `remote` | `""` | pull/push 用的 git 远端（`""` = 只在本地） |
| `proxy` | `""` | pull/push 先试的 HTTP(S) 代理，失败再直连 |
| `language` | `"en"` | `"en"` 或 `"zh"`：模型提示、页面节名、lint 用语（见[语言](#语言)） |
| `diff_engine` | `true` | `true`：夜间 Diff Engine + 逐行判定 + `state/diff-*.json`；`false`：沿用旧 review |
| `proposal_engine` | `true` | `true`：结构化 dream 候选 + proposals 命令 + 生成页面；`false`：沿用旧 dream |

引擎名：`haiku`、`sonnet`、`opus`（或 `claude` CLI 接受的任何模型名）、`claude`（= `claude_model`）、
`codex` 或 `codex:<effort>`（如 `codex:high`）、`agy`。所有引擎用同一套分步提示（`bin/ingest.py` 里的 `SOP_*`），
脚本用同一套标准验收每个结果。

## 命令

```
ingest.py --pending        写入所有空闲 ≥ 30 分钟且有新内容的会话
ingest.py --session ID     立刻写入一个会话（已有运行时进队列）
ingest.py --hook           SessionEnd hook 入口：从 stdin 读 hook JSON，后台运行 --session
ingest.py --nightly        夜间运行：补写、拆分、dream、复核
ingest.py --lint           每周整理，然后 dream
ingest.py --status         列出有新内容的会话
ingest.py --usage [DAYS]   按天、按模型统计 token 用量
ingest.py proposals list [--json]  结构化规则候选（open / accepted / rejected）
ingest.py proposals accept ID           在 state/proposals/ 生成补丁，不改规则文件；
                                        打印应用目录；少于 2 次的候选会被拒绝
ingest.py proposals reject ID [原因]    记录否决，以后不再提
lint.py [--stats|--warn] [--language en|zh]   结构检查 / 各层大小 / 接近预算的警告
```

运行时文件都在 wiki 根目录，都已被 git 忽略：`.ingest.log`、`.ingest.sqlite`、`.ingest-usage.tsv`、`.ingest.lock`、
`.ingest.queue`、`state/`（Diff Engine 报告、`proposals/*.patch`）和 `.ingest-work/` worktree。结构化的候选记录
`misc/rule-candidates.json` 和页面一样纳入 git。

## 找回原始对话

主题页只留结论，`log.md` 记着每次改动来自哪个会话（`sid8`）。需要完整讨论时，在 Claude Code 里打开那个会话
（`claude --resume <session-id>`）。也可以用会话搜索工具（比如「resume session」类 skill）按主题找。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

写入测试用一个假的 `claude` CLI（`tests/fake-engine/claude`）跑真实流水线。假引擎像模型一样写页面，
测试在两种语言下覆盖验收通过、拒收、兜底引擎、hook、夜间 Diff Engine（报告 + 伪造归因自动修复）、结构化 dream、
proposals 命令和第一次 push 到空远端，不调用任何模型。`tests/test_diff_engine.py` 和
`tests/test_proposal_engine.py` 用小样例直接测确定性检查、去重/门槛/否决逻辑，以及 accept 只出补丁不改文件。

## 许可证

[MIT](LICENSE)
