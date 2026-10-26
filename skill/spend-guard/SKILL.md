---
name: spend-guard
description: 'Cut AI (Copilot/agent) spend in a project: a cheap default model for routine work, automatic hand-off of coding, security reviews and accuracy-critical work to stronger models, an optional independent second pass, and a guard that moves new tasks out of long chats. Use when asked to reduce AI costs, set up model routing, add Spend-Guard to a project, update or remove it, or report AI spend.'
argument-hint: 'setup | update | remove | report'
---

# Spend-Guard

Sets a project up so every task gets the model it actually needs:

| Tier | Used for |
|---|---|
| cheap (default) | routine work: running tools and tests, small edits, docs, git |
| standard | writing and fixing code |
| accurate | security reviews and accuracy-critical work |
| checker | an independent second pass from a different model family |

Spend-Guard can generate the same routing policy for Copilot and Claude Code. Copilot remains the
default provider for backwards compatibility; add `"providers": ["copilot", "claude"]` to a plan
to generate `.claude/skills/` and a marked `CLAUDE.md` section as well. Existing Claude files are
never overwritten.

The mechanical part is [spendguard.py](./spendguard.py) (standard library only); your job is the judgement:
understanding the project and proposing the right specialists. Never overwrite files Spend-Guard didn't
create; the script already refuses to.

Each applied project also gets `.github/notes.md` when that file is missing. It is a short, user-maintained
handoff: append 1-3 lines at task completion with a decision, current state, half-done work or a gotcha.
Condense older entries around 60 lines. Never put secrets or client data there. Update and remove leave it
untouched.

The generated `sessionStart` hook automatically adds the capped notes and a deterministic digest of the
most recently modified previous chat in the same workspace to startup context. It reads local session logs
only, makes no model call, and fails open if the input or logs are unavailable.

An optional `spendguard.py graph <folder>` command builds a local deterministic code graph from supported
source files using the Python standard library. It records only relative paths, symbol names, imports and
SHA-256 fingerprints; it excludes secret-looking files, symlinks, dependency/build directories and files
over 200 KB. It makes no network or model calls and writes `GRAPH_REPORT.md` under
`.github/spend-guard-graph/`. The startup hook includes only the bounded report, never the graph or source
contents.

Run the script as `python "<this skill folder>/spendguard.py" <command>` (on macOS/Linux `python3`).

## Setup in a project (`/spend-guard` or "set up spend-guard")

1. **Check it's allowed.** If the repository looks like company or client work, ask the user to confirm
   their employer allows changing how AI tools are used there. Never send code anywhere new.
2. **Analyse:** `spendguard.py analyze <workspace folder>`. Then skim the README (first ~60 lines) and any
   existing `AGENTS.md`/`.github/copilot-instructions.md`. Don't read big files.
3. **Draft a plan** (JSON, schema below):
   - `project`, `main_name` (short, e.g. the project name), one-line `summary`.
   - `routine`: what the cheap main agent does itself (run tests, regenerate outputs, docs, git, ...).
   - `test_command` from the analysis.
   - `avoid_files`: the analysis `big_files` (and any large reference data). Agents will search them, never open them whole.
   - `rules`: safety rules every agent follows, e.g. "Never read or print `config.json`" for each
     `secret_files` entry; "Search `<file>` and read only the ranges you need" for each `big_code_files`
     entry; and any project rule from existing docs that must not be broken (keep them short).
   - If `always_loaded_instructions` lists files with `applyTo: '**'`, mention to the user that they load on
     every request and cost tokens each time; trimming them or narrowing `applyTo` saves more. Don't edit them yourself.
   - `specialists`:
     - a **developer** (`role: developer`, tier standard) for any project with code;
     - a **reviewer** (`role: reviewer`, tier accurate, read-only) only if there is security-sensitive code
       (`sensitive_code` in the analysis, or auth, payments, signing, infra, permissions). Make `when`
       name the exact areas;
     - an **extractor** (`role: extractor`, tier accurate) when the project reads documents, images,
       handwriting, scans or data where a misread is costly. Set `double_check: true` to add an
       independent checker on a different model family;
     - `custom` specialists only for a clearly separate expensive task type.
     Keep it minimal: every specialist adds a hand-off cost. Most projects need just the developer.
4. **Confirm with the user** in one question form: the specialists (name, what they handle, tier), whether
   anything needs the accuracy double-check, and the files to avoid. Adjust the plan to their answers.
5. **Apply:** write the plan to a temporary file, run `spendguard.py apply <folder> --plan <file> --dry-run`,
   check the output, then run it without `--dry-run`. If it skipped a file that already exists (not created by
   Spend-Guard), tell the user; don't work around it.
6. **Report back** in a few lines: what each agent does and which model, what was skipped, and that new chats
   in this project start on the cheap model. Suggest committing `.github/agents/`, `.github/hooks/`,
   `.github/spend-guard.json`, `.github/notes.md`, `AGENTS.md` and `.vscode/settings.json`.

## Other commands

- **update** (`spendguard.py update <folder>`): re-render everything from the saved plan, e.g. after
  editing `.github/spend-guard.json` or the model tiers.
- **remove** (`spendguard.py remove <folder>`): removes only what Spend-Guard created.
- **report** (`spendguard.py report --days 7`): AI cost per project and model from the local Copilot logs,
  compared with the previous period, plus chats that grew past 100k tokens. Summarise it in a few lines with
  one or two concrete suggestions.
- **tiers** (`spendguard.py tiers [--init]`): show the model tiers; `--init` writes an editable copy to
  `~/.copilot/spend-guard/tiers.json`. After editing, run `update` in each project.
- **graph** (`spendguard.py graph <folder>`): build or incrementally update the local code graph and report.

## Plan schema

```json
{
  "project": "Billing API",
  "main_name": "Billing",
  "summary": "REST API that issues invoices",
  "routine": ["running tests", "small edits", "docs", "git"],
  "test_command": "python -m pytest",
  "providers": ["copilot", "claude"],
  "avoid_files": ["data/rates.csv"],
  "rules": ["Never read or print `.env`."],
  "guard_tokens": 100000,
  "specialists": [
    {"name": "Billing Developer", "role": "developer", "when": "Code changes beyond a one-line fix"},
    {"name": "Billing Security Reviewer", "role": "reviewer",
     "when": "After a change to auth/, payments/ or webhook signing"},
    {"name": "Invoice Reader", "role": "extractor", "double_check": true,
     "when": "Reading scanned invoices or PDFs",
     "instructions": "Amounts use a comma as the decimal separator."}
  ]
}
```

`role`: developer | reviewer | extractor | custom. `tier` defaults by role (developer: standard,
reviewer and extractor: accurate, custom: standard). `read_only` defaults to true for reviewers.
`instructions` adds project-specific guidance to that agent.
