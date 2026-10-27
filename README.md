<div align="center">

<img src="docs/assets/spend-guard-logo.svg" alt="Spend-Guard logo" width="128">

# Spend-Guard

**Stop paying top-model prices for routine AI coding work.**
Spend-Guard sets up any project so GitHub Copilot uses a cheap model by default and calls in stronger
models only for the work that needs them.

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Copilot](https://img.shields.io/badge/GitHub%20Copilot-agents-000000?logo=githubcopilot&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-2EA44F)
![Tests](https://img.shields.io/badge/tests-25%20passing-2EA44F)
![License](https://img.shields.io/badge/license-MIT-yellow)

</div>

---

## The problem

Coding agents make it easy to burn money without noticing:

- **One expensive model for everything.** Running a test or editing a README costs as much as designing a
  feature, because the top model is selected once and never changed.
- **Endless chats.** Every request resends the whole conversation. A chat with 300k tokens of history makes
  even a one-line question expensive.
- **Big files in context.** Reference PDFs, data dumps and huge source files get read in full, again and again.

In the logs that inspired this tool, **81% of a month's spend went to the top model**, and a single
long-running chat cost more than half the total.

## What Spend-Guard does

| | What | How |
|---|---|---|
| 💸 | **Cheap by default** | New chats in the project start on a low-cost model (`chat.defaultModel`) |
| 🧭 | **Automatic hand-off** | A main agent does routine work itself and delegates to specialists on stronger models: a developer for code, a read-only reviewer for security-sensitive changes, an accurate reader for critical documents |
| 🧩 | **Claude Skills support** | Optionally generates Claude Code `SKILL.md` files and a marked `CLAUDE.md` routing section from the same plan |
| 🔁 | **Independent second pass** | For accuracy-critical extraction (handwriting, scans, invoices), a checker on a *different model family* re-reads the same locations without seeing the first answers; disagreements go to you |
| ✂️ | **Short chats** | A hook watches the chat's size. Past ~100k tokens it asks the agent to move a *new* task to a fresh chat with a short handoff |
| 📝 | **Project handoff notes** | Each project gets a small `.github/notes.md` file for durable decisions and half-done work, so new chats read context without carrying full history |
| 🚀 | **Automatic startup context** | A fast `sessionStart` hook adds capped notes and the previous same-workspace chat's recent user messages and final answer, with no model call |
| 🕸️ | **Local code graph** | An optional standard-library graph extracts code files, symbols and imports locally, caches unchanged files by SHA-256, and adds a small report to startup context |
| 🎯 | **Trusted provider policy** | Optional routing guidance for Claude, GPT, Grok, NVIDIA and Gemini, with one primary provider and failure-only fallback |
| 🚦 | **Route-only dispatcher** | Deterministically chooses Copilot or an approved external provider by task risk, always retaining a Copilot fallback |
| 📵 | **No whole big files** | Large reference files and huge source files are searched, never read whole |
| 📊 | **Spend report** | Cost per project and model from your local Copilot logs, compared with the previous period |

You don't pick agents or models: open a chat and ask. The routing happens on its own.

## Benefits of integrating Spend-Guard into a tool

### Lower AI usage costs

- Uses a cheap model for routine work.
- Escalates only coding, security, architecture or accuracy-critical tasks.
- Avoids sending every task to the most expensive model.
- Can use approved external providers only while their free quota is confirmed.
- Blocks paid external fallback by default.

### Better model selection

Different work can use different model tiers:

- **Cheap:** tests, documentation, searches, logs and git.
- **Standard:** coding and bug fixes.
- **Accurate:** security reviews, architecture and sensitive decisions.
- **Checker:** independent second review when accuracy matters.

### Preserves output quality

- Stronger models are reserved for difficult tasks.
- Security-sensitive work can be restricted to trusted providers.
- Accuracy-critical work can use independent checking.
- Tests, validation and review gates remain part of the workflow.
- Routine work does not waste expensive model capacity.

### Prevents oversized chats

- The cost guard detects long conversations.
- It recommends moving new work to a fresh chat.
- It creates a short handoff instead of carrying the entire transcript.
- This reduces repeated context-token costs.

### Automatic context continuity

New chats can automatically receive project notes, a bounded previous-chat digest, recent decisions
and task state, plus optional local code-graph context. This avoids manually copying large
conversation histories.

### Cross-session memory without model cost

On-demand local memory search can find decisions in the current workspace, explicitly selected other
workspaces and existing local session logs. It uses no embeddings, model summarization or network
service.

### Free-only external-provider routing

The route-only dispatcher can prefer Copilot models, route compatible work to approved external
providers, require explicit free-quota confirmation, restrict sensitive tasks to approved providers,
fall back to Copilot when quota is unknown or exhausted, and prevent paid external fallback.

### Provider flexibility

A project can define approved providers such as Claude, GPT, Gemini, Grok and NVIDIA for compatible
external tools. Existing Copilot model routing continues independently.

### Security protections

- Credentials are never stored in generated project files.
- Secret-looking files are excluded from graph indexing.
- `.env`, config and credential-like values are protected during memory search.
- Subagent and automation sessions can be excluded.
- Sensitive work can stay on Copilot or use restricted trusted providers.
- Generated files do not overwrite user-owned customization files.

### Local-first operation

- Standard-library Python implementation.
- No required hosted memory service, background worker or mandatory MCP server.
- Notes, graphing, memory search and dispatch decisions make no network calls.
- These features make no additional model calls.

### Easier project onboarding

Spend-Guard can generate a main routing agent, developer specialist, security reviewer when needed,
accuracy reader and independent checker when needed, automatic session-start and long-chat hooks,
provider policy, project notes, low-cost VS Code settings and optional Claude Code skills.

### Usage visibility

The local report helps identify cost by project and model, recent versus previous usage, long chats,
models consuming the most usage and opportunities to move work to cheaper tiers.

### Important limitations

Spend-Guard does not:

- Reduce the fixed Copilot subscription price.
- Automatically redirect Copilot traffic to an external provider.
- Guarantee identical output across models.
- Verify external free quotas unless an integrating client provides that information.
- Make external API calls by itself.
- Replace tests, security review or human approval.

The main benefit is **controlled model selection**: use the least expensive safe option for each task
while reserving stronger models and external providers for cases where they provide real value.

## Cost-saving strategy

Spend-Guard uses a simple policy: use the cheapest suitable model for predictable work,
reserve stronger models for code, security and accuracy-critical decisions, and carry only
a capped local handoff into a new chat instead of the full conversation history.

The optional graph feature deliberately uses only local code structure. It does not call a
model, access the network, process PDFs or images, run an MCP server, or start a watcher.
It excludes secret-looking files, dependency/build directories, symlinks and code files over
200 KB. Generated graph data is kept under `.github/spend-guard-graph/`; review it before
committing it because paths and symbol names can still reveal project structure.

### Model tiers

| Tier | Default models | Used for |
|---|---|---|
| cheap | GPT-5.6 Luna, GPT-5.4 nano | running tools and tests, small edits, docs, git |
| standard | Claude Sonnet 5.5, GPT-5.6 Sol, Grok 4.7 | writing and fixing code |
| accurate | Claude Opus 5.5 | security reviews, accuracy-critical reading |
| checker | GPT-6 Astra, Grok 4.7, GPT-5.6 Sol | independent second pass |

Model names change often. Run `spendguard.py tiers --init`, edit `~/.copilot/spend-guard/tiers.json`, then
`spendguard.py update <project>` to switch every project at once.

## How Spend-Guard works

Spend-Guard is a project-level AI cost-control system. It keeps routine work cheap, escalates only
when the task needs more reasoning, and carries a small amount of durable context into new chats
instead of replaying the entire conversation.

```mermaid
flowchart LR
    U[Your request] --> M{What kind of work?}
    M -->|Tests, tools, docs, git| C[💸 Cheap<br/>GPT-5.6 Luna]
    M -->|Code change or bug fix| S[🛠️ Standard<br/>Sonnet / Sol]
    M -->|Security or accuracy-critical| A[🛡️ Accurate<br/>Opus]
    M -->|Independent second pass| K[🔎 Checker<br/>GPT-6 Astra]
    C --> R[Main agent]
    S --> R
    A --> R
    K --> R
    R --> V[Verified result]
```

### The seven cost-saving mechanisms

| # | Mechanism | What happens automatically |
|---:|---|---|
| 1 | **Cheap model by default** | New chats use the configured low-cost model, currently GPT-5.6 Luna, through `chat.defaultModel`. |
| 2 | **Automatic model routing** | The main agent sends routine work to cheap, code changes to standard, and security or accuracy-critical work to accurate tiers. |
| 3 | **Specialist agents** | Projects receive developer, read-only reviewer, extractor and independent-checker agents only when their codebase needs them. |
| 4 | **Long-chat guard** | Follow-ups continue in place, but new tasks are directed to a fresh low-cost chat once context becomes expensive. |
| 5 | **Automatic startup context** | `sessionStart` adds capped notes and a digest of the previous same-workspace chat, with no model call. |
| 6 | **Secure local code graph** | An optional standard-library graph records local symbols, imports and relationships without network access or model calls. |
| 7 | **Spend reporting** | Local Copilot logs are compared by project, model, credits, estimated cost and long-chat count. |

### Context without the history bill

```mermaid
sequenceDiagram
    participant N as New chat
    participant H as sessionStart hook
    participant L as Local session logs
    participant P as Project notes
    participant G as Local graph report
    participant A as Agent

    N->>H: Start workspace session
    H->>L: Find latest previous chat in same workspace
    H->>P: Read last ~60 note lines
    H->>G: Read bounded graph summary if present
    H-->>N: Add context (maximum ~6,000 characters)
    N->>A: User request + compact startup context
```

The startup hook never loads the full previous transcript. It includes the previous session ID,
up to eight recent user messages, the final assistant response, project notes and an optional
bounded graph report. Missing or malformed logs fail open.

### Secure local code graph

```mermaid
flowchart TD
    F[Local source files] --> X{Strict exclusions}
    X -->|Secrets, symlinks, dependencies,<br/>build output, files over 200 KB| D[Do not index]
    X -->|Allowed source code| H[SHA-256 fingerprint]
    H --> P{Changed?}
    P -->|No| K[Reuse cached extraction]
    P -->|Yes| E[Extract symbols and imports locally]
    K --> O[graph.json + GRAPH_REPORT.md]
    E --> O
    O --> S[Bounded startup summary]
```

The graph stores only relative paths, symbol names, imports, relationships and fingerprints. It
does not store source text, call a model, access the network, process PDFs or images, run an MCP
server, or start a watcher.

### Expected savings

Projects using this strategy are expected to reduce **variable AI usage cost** by approximately
**60–85%** from Spend-Guard alone. With compatible free external-provider routing integrated and
available for about 25% of the remaining eligible work, the combined variable-cost reduction is
approximately **70–89%**. These are workload-dependent estimates, not guarantees:

```text
Original usage cost:                 100
Spend-Guard reduction:               60–85%
Remaining cost:                     15–40
Free external routing removes 25%:  11.25–30
Combined reduction:                 70–89%
```

The free-provider estimate applies only while a provider's free quota is available and the task is
eligible for external routing. It does not reduce the fixed Copilot Pro subscription price; it reduces
variable usage or separate API costs. Measure actual results with the local report and provider billing:

```mermaid
pie title Where the savings come from
    "Cheap routine work" : 35
    "Targeted specialist hand-offs" : 25
    "Shorter chats and compact handoffs" : 25
    "Large-file and graph context control" : 15
```

The graph itself has no model cost. Its value is reducing repeated context reading. Measure actual
results with:

```powershell
python .\skill\spend-guard\spendguard.py report --days 7
```

That report is based on local Copilot logs; GitHub billing remains authoritative.

### Trusted provider routing

Spend-Guard generates `.github/spend-guard-providers.json` for compatible external API tools. The
default allowlist contains only Anthropic (Claude), OpenAI (GPT), xAI (Grok), NVIDIA and Google (Gemini).
The policy uses one primary provider per task and permits fallback only for an unavailable provider, timeout
or failed validation. It does not reroute GitHub Copilot's own endpoint or billing, and it stores no
credentials. Keep sensitive tasks on the `sensitive_allowed` list and review provider terms before use.

The route-only dispatcher can be queried with:

```powershell
python .\skill\spend-guard\spendguard.py dispatch C:\path\to\project "review authentication and token signing"
```

It classifies the request locally, returns a JSON route, and makes zero model or network calls. Copilot is
the default route, including Copilot models such as Grok 4.7. An external route requires the explicit
`--free-quota-confirmed` flag and is marked `free_only`; otherwise it falls back to Copilot. Sensitive
external routes are limited to Anthropic/OpenAI and retain a Copilot fallback. The dispatcher never
verifies quota, invokes an API or reads credentials; the calling client must confirm the provider's free
quota and execute the returned route. Paid external fallback is blocked.

## Quick start

> **You need:** Python 3.10+ and VS Code with GitHub Copilot (agent sessions).

```powershell
git clone https://github.com/PrashantAHD/spend-guard.git
cd spend-guard
powershell -ExecutionPolicy Bypass -File .\install.ps1     # macOS/Linux: ./install.sh
```

Then open a project, start a **new chat** and type **`/spend-guard`**. The agent:

1. analyses the project (languages, tests, big files, secrets, security-sensitive code);
2. proposes the specialists it needs: usually just a developer, plus a reviewer or a double-checked
   reader only where the project calls for it;
3. asks you to confirm, then writes the setup.

Commit the new files and start new chats as usual.

## Using Spend-Guard in a new tool

### Interactive setup

1. Install Spend-Guard once:

   ```powershell
   git clone https://github.com/PrashantAHD/spend-guard.git
   cd spend-guard
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   On macOS/Linux, run `./install.sh` instead. The skill is installed under
   `~/.copilot/skills/spend-guard/`.

2. Open the new tool's project folder in VS Code and start a **new** Copilot chat.
3. Type `/spend-guard`.
4. Review the project analysis and proposed specialists, then confirm the setup.
5. Run the project's normal tests and review the generated files before committing them.

Before applying Spend-Guard to company or client code, confirm that your organization's AI policy
allows changing model routing. Spend-Guard does not send code anywhere new.

The setup normally creates:

```text
AGENTS.md
.github/agents/
.github/hooks/spend-guard.json
.github/hooks/spend_guard.py
.github/hooks/session_start.py
.github/notes.md
.github/spend-guard.json
.vscode/settings.json
```

When Claude support is enabled in the plan, it also creates:

```text
CLAUDE.md                         # a marked Spend-Guard routing section
.claude/skills/spend-guard/SKILL.md
.claude/skills/<specialist>/SKILL.md
```

`.github/notes.md` is created only when missing and is never overwritten or deleted by
Spend-Guard. It contains short handoffs, never secrets or client data.

### Manual setup

Analyze a project without changing it:

```powershell
python "$env:USERPROFILE\.copilot\skills\spend-guard\spendguard.py" analyze .
```

Create a plan using the analysis, then preview and apply it:

```powershell
python "$env:USERPROFILE\.copilot\skills\spend-guard\spendguard.py" apply . --plan .\plan.json --dry-run
python "$env:USERPROFILE\.copilot\skills\spend-guard\spendguard.py" apply . --plan .\plan.json
```

The plan format is documented in [SKILL.md](skill/spend-guard/SKILL.md#plan-schema). Review
`.github/spend-guard.json` after applying.

To generate Claude Code skills as well as Copilot agents, add this to the plan:

```json
{
  "providers": ["copilot", "claude"]
}
```

The default is `["copilot"]`, so existing setups are unchanged. Claude skill files are local,
marked as managed, and are never created by default or used to overwrite an existing
`CLAUDE.md` file.

### Optional secure local code graph

For a codebase where relationship context is useful, build the local graph explicitly:

```powershell
python "$env:USERPROFILE\.copilot\skills\spend-guard\spendguard.py" graph .
```

This writes `.github/spend-guard-graph/graph.json` and `GRAPH_REPORT.md`. It uses no model calls
or network access, excludes secret-looking files and other strict-risk paths, and adds only the
bounded report to new-chat startup context. Review the generated graph before committing it.

### Commit and measure

After reviewing the generated configuration:

```powershell
git add AGENTS.md .github .vscode
git commit -m "Add Spend-Guard model routing"
git push
```

Measure actual usage after several days:

```powershell
python "$env:USERPROFILE\.copilot\skills\spend-guard\spendguard.py" report --days 7
```

The report compares local Copilot usage by project and model. The expected 60–85% reduction is
workload-dependent; your GitHub billing page remains authoritative.

## What it creates in a project

```
AGENTS.md                         # a marked "Model routing" section (the rest of the file is untouched)
.github/agents/<main>.agent.md    # the cheap main agent and its routing table
.github/agents/<specialist>.agent.md
.github/hooks/spend-guard.json    # the long-chat guard hook
.github/hooks/spend_guard.py
.github/hooks/session_start.py  # automatic capped startup context from notes + previous chat
.github/hooks/spend-guard.json   # hook registration
.github/notes.md                 # short user-maintained handoff notes (created once, never overwritten)
.github/spend-guard.json          # the plan, so it can be updated or removed
.vscode/settings.json             # chat.defaultModel = the cheap model
```

Files are marked *managed by spend-guard*. It **never overwrites a file it didn't create**: it skips it and
tells you. `.github/notes.md` is deliberately user-maintained: Spend-Guard creates it only when missing and
never overwrites or deletes it. Append 1-3 lines after a task with the decision, current state, half-done work
or a gotcha; condense older entries around 60 lines. Never put secrets or client data there. `remove` deletes
only its own files and its section of `AGENTS.md`.

## Commands

The skill runs these for you; you can also run them yourself:

| Command | What it does |
|---|---|
| `python spendguard.py analyze <folder>` | Project facts as JSON |
| `python spendguard.py apply <folder> --plan plan.json [--dry-run]` | Generate the setup from a plan |
| `python spendguard.py update <folder>` | Re-render from the saved plan (e.g. after changing tiers) |
| `python spendguard.py remove <folder>` | Remove everything Spend-Guard created |
| `python spendguard.py report [--days 7]` | Cost per project and model, vs the previous period |
| `python spendguard.py tiers [--init]` | Show or customise the model tiers |
| `python spendguard.py graph <folder>` | Build a local deterministic code graph and bounded `GRAPH_REPORT.md` |
| `python spendguard.py memory search <folder> <query> [--all-workspaces]` | Search matching messages in local chats |
| `python spendguard.py dispatch <folder> <task> [--free-quota-confirmed]` | Return a free-only Copilot/external route without calling a model |

`memory search` is on-demand only: it reads local Copilot session logs, excludes subagent and automation
sessions, ignores messages mentioning `config.json` or `.env`, redacts credential-like values, and caps its
JSON output at about 6,000 characters. It makes zero model calls and does not add context to new chats unless
the agent explicitly runs it. By default it searches the current workspace; pass `--all-workspaces` when
cross-workspace lookup is intentional. Results include the workspace path so the source is explicit. This
provides a cheap way to recover a decision from another existing session without loading that session's full
transcript.

The script is in `~/.copilot/skills/spend-guard/` after installing. Plan format: see
[SKILL.md](skill/spend-guard/SKILL.md#plan-schema).

## What to expect

- **Cost:** in the projects it was built on, the estimate is a **60–85% lower spend**, depending on how much
  of the work needs the stronger tiers. Check your own with `report` after a week.
- **Quality:** unchanged for routine work and for anything your own code computes. Coding on the standard
  tier may take an extra round on hard problems; the developer reports back when it needs the accurate tier.
  Accuracy-critical reading stays on the top model, with the optional second pass.

## Good to know

- **Company or client code:** check your employer's AI policy before changing how AI tools run in their
  repositories. Spend-Guard sends nothing anywhere new; it only changes which Copilot model handles a task.
- **The guard is advice, not a lock.** The agent decides whether your message is a follow-up or a new task.
  If it carries on with a new task in a long chat, just say "new chat".
- **Cost figures are estimates** from your local Copilot logs (about $0.01 per AI credit). Only your GitHub
  billing page is authoritative.
- **Built on Copilot features that change often:** custom agents, subagents, hooks and `chat.defaultModel`.
  If routing stops working after an update, open an issue.

## Project layout

```
spend-guard/
├── skill/spend-guard/SKILL.md      # the /spend-guard skill (the judgement part)
├── skill/spend-guard/spendguard.py # analyse, apply, update, remove, report (standard library only)
├── install.ps1 / install.sh        # copy the skill to ~/.copilot/skills/spend-guard
├── tests/                          # python -m unittest discover -s tests
└── LICENSE                         # MIT
```

## License

[MIT](LICENSE) © 2026 Prashant Kumar

---

<div align="center">
<sub>Spend-Guard is an independent project and isn't affiliated with GitHub, Microsoft, Anthropic or OpenAI.</sub>
</div>
