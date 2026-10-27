"""Spend-Guard: keeps coding-agent AI spend down by giving every task the right model for the job.

Sets up a project so routine work runs on a low-cost model and only the work that needs it
(writing code, security reviews, accuracy-critical reading) goes to stronger models, and keeps
chats short. Standard library only, Python 3.10+.

Commands:
    python spendguard.py analyze <folder>                 facts about a project (JSON)
    python spendguard.py apply <folder> --plan plan.json  generate/refresh the setup from a plan
    python spendguard.py update <folder>                  re-render from the saved plan (e.g. new model tiers)
    python spendguard.py remove <folder>                  remove everything Spend-Guard generated
    python spendguard.py report [--days N]                AI cost per project/model from local Copilot logs
    python spendguard.py tiers [--init]                   show model tiers (--init writes an editable copy)
    python spendguard.py memory search <folder> <query> [--all-workspaces]
                                                            search approved local session messages
    python spendguard.py dispatch <folder> <task>           choose a trusted route without making a model call
    python spendguard.py validate <folder> [--strict]      audit the local Spend-Guard setup
"""

from __future__ import annotations

import argparse
import ast
import collections
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

VERSION = "1.0.0"
MARK = "managed by spend-guard"
SECTION_START = "<!-- spend-guard:start -->"
SECTION_END = "<!-- spend-guard:end -->"
CLAUDE_SECTION_START = "<!-- spend-guard:claude:start -->"
CLAUDE_SECTION_END = "<!-- spend-guard:claude:end -->"
PLAN_FILE = Path(".github") / "spend-guard.json"
PROVIDER_POLICY_FILE = Path(".github") / "spend-guard-providers.json"
NOTES_FILE = Path(".github") / "notes.md"
GRAPH_DIR = Path(".github") / "spend-guard-graph"
GRAPH_FILE = GRAPH_DIR / "graph.json"
GRAPH_REPORT = GRAPH_DIR / "GRAPH_REPORT.md"
MEMORY_MAX_OUTPUT = 6000
MEMORY_MAX_MATCHES = 20
MEMORY_MAX_MESSAGE = 700
COPILOT_HOME = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))
TIERS_OVERRIDE = COPILOT_HOME / "spend-guard" / "tiers.json"

DEFAULT_TIERS: dict[str, dict] = {
    "cheap": {"models": ["GPT-5.6 Luna (copilot)", "GPT-5.4 nano (copilot)"], "default_model_id": "gpt-5.6-luna",
              "use": "routine work: running tools and tests, small edits, docs, git"},
    "standard": {"models": ["Claude Sonnet 5.5 (copilot)", "GPT-5.6 Sol (copilot)", "Grok 4.7 (copilot)"],
                 "use": "writing and fixing code"},
    "accurate": {"models": ["Claude Opus 5.5 (copilot)"], "reasoning_effort": "high",
                 "use": "accuracy- or security-critical work"},
    "checker": {"models": ["GPT-6 Astra (copilot)", "Grok 4.7 (copilot)", "GPT-5.6 Sol (copilot)"],
                "reasoning_effort": "high",
                "use": "independent second pass from a different model family"},
}

ROLES = ("developer", "reviewer", "extractor", "custom")
TRUSTED_PROVIDERS = ("anthropic", "openai", "xai", "nvidia", "google")
DEFAULT_PROVIDER_POLICY = {
    "allowed": list(TRUSTED_PROVIDERS),
    "routing": {
        "cheap": ["google", "openai", "nvidia"],
        "standard": ["anthropic", "openai", "google"],
        "accurate": ["anthropic", "openai"],
        "checker": ["google", "xai"],
    },
    "fallback_on": ["unavailable", "timeout", "validation_failure"],
    "sensitive_allowed": ["anthropic", "openai"],
    "dispatcher": {
        "external_mode": "compatible_tasks",
        "sensitive_external": "trusted_external",
        "external_cost_mode": "free_only",
        "require_free_quota_confirmation": True,
    },
}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target", "bin", "obj",
             ".next", ".tox", ".mypy_cache", ".pytest_cache", "vendor", "coverage", ".idea"}
LANG_EXT = {".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
            ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".cs": "C#", ".cpp": "C++", ".c": "C",
            ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".ps1": "PowerShell", ".sh": "Shell", ".sql": "SQL",
            ".tf": "Terraform", ".bicep": "Bicep", ".scala": "Scala", ".dart": "Dart"}
SENSITIVE = re.compile(r"(auth|login|password|secret|token|crypto|sign|security|permission|acl|payment|billing|"
                       r"invoice|terraform|bicep|infra|deploy|migration|\.github[\\/]workflows)", re.I)
SECRET_FILES = re.compile(r"(^|[\\/])(\.env(\..*)?|.*\.pem|.*\.key|.*\.pfx|id_rsa.*|secrets?\.(json|ya?ml)|"
                          r"credentials.*)$", re.I)
SECRET_VALUE = re.compile(r"""["']?(?:[a-z_]*token|secret|password|passwd|api[_-]?key|client[_-]?secret|"""
                          r"""private[_-]?key|access[_-]?key)["']?\s*[:=]\s*["']?[A-Za-z0-9_\-./+=:]{12,}""", re.I)
GRAPH_MAX_BYTES = 200_000
GRAPH_EXTENSIONS = set(LANG_EXT)
GRAPH_IMPORT = re.compile(r"^\s*(?:import\s+([A-Za-z_][\w.]*)|from\s+([A-Za-z_][\w.]*)\s+import)", re.M)
GRAPH_DEFINITION = re.compile(
    r"^\s*(?:export\s+)?(?:(?:async\s+)?function|class|func|type|def|interface|struct|"
    r"module|trait)\s+([A-Za-z_][\w]*)", re.M)


def _has_secret(path: Path) -> bool:
    """A small config file that holds what looks like a real credential value."""
    try:
        return bool(SECRET_VALUE.search(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------

def load_tiers() -> dict[str, dict]:
    tiers = json.loads(json.dumps(DEFAULT_TIERS))
    if TIERS_OVERRIDE.exists():
        for name, val in json.loads(TIERS_OVERRIDE.read_text(encoding="utf-8-sig")).items():
            tiers[name] = {**tiers.get(name, {}), **val}
    return tiers


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

def analyze(folder: Path) -> dict:
    folder = folder.resolve()
    langs: collections.Counter = collections.Counter()
    big, big_code, sensitive, secrets_found, docs = [], [], [], [], []
    files = 0
    for root, dirs, names in os.walk(folder):
        # Skip dependencies, build output, virtual environments, bundled tools and hidden folders.
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".worktrees")
                   and (not d.startswith(".") or d in (".github", ".vscode"))
                   and not (Path(root) / d / "pyvenv.cfg").exists() and d != "site-packages"]
        for n in names:
            p = Path(root) / n
            rel = p.relative_to(folder).as_posix()
            files += 1
            ext = p.suffix.lower()
            if ext in LANG_EXT:
                langs[LANG_EXT[ext]] += 1
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > 200_000 and ext in (".txt", ".md", ".json", ".csv", ".log", ".xml", ".html", ".jsonl", ".yaml",
                                           ".yml", ".sql", ".pdf"):
                big.append({"path": rel, "kb": size // 1024})
            elif size > 60_000 and ext in LANG_EXT:
                big_code.append({"path": rel, "kb": size // 1024})
            if SECRET_FILES.search(rel) or (size < 50_000 and ext in (".json", ".yaml", ".yml", ".ini", ".toml", ".cfg")
                                            and _has_secret(p)):
                secrets_found.append(rel)
            elif ext in LANG_EXT and SENSITIVE.search(rel):
                sensitive.append(rel)
            if ext in (".md",) and rel.count("/") <= 1:
                docs.append(rel)

    def exists(*parts: str) -> bool:
        return (folder.joinpath(*parts)).exists()

    tests = []
    if exists("pyproject.toml") or exists("pytest.ini") or exists("setup.cfg"):
        tests.append("python -m pytest")
    if exists("tests") and langs.get("Python"):
        tests.append("python -m unittest discover -s tests")
    if exists("package.json"):
        try:
            if "test" in (json.loads((folder / "package.json").read_text(encoding="utf-8")).get("scripts") or {}):
                tests.append("npm test")
        except (ValueError, OSError):
            pass
    if exists("go.mod"):
        tests.append("go test ./...")
    if exists("Cargo.toml"):
        tests.append("cargo test")
    if list(folder.glob("*.sln")) or list(folder.glob("*.csproj")):
        tests.append("dotnet test")

    existing = {
        "AGENTS.md": exists("AGENTS.md"),
        ".github/copilot-instructions.md": exists(".github", "copilot-instructions.md"),
        ".github/agents": sorted(p.name for p in (folder / ".github" / "agents").glob("*.agent.md")),
        ".github/instructions": sorted(p.name for p in (folder / ".github" / "instructions").glob("*.instructions.md")),
        ".github/skills": sorted(p.name for p in (folder / ".github" / "skills").glob("*") if p.is_dir()),
        ".github/hooks": sorted(p.name for p in (folder / ".github" / "hooks").glob("*.json")),
        ".vscode/settings.json": exists(".vscode", "settings.json"),
        "spend-guard plan": exists(*PLAN_FILE.parts),
    }
    always_on = [p.name for p in (folder / ".github" / "instructions").glob("*.instructions.md")
                 if re.search(r"^applyTo:\s*['\"]?\*\*['\"]?\s*$", p.read_text(encoding="utf-8", errors="replace"), re.M)]
    return {
        "folder": str(folder), "name": folder.name, "files": files,
        "languages": dict(langs.most_common(6)),
        "test_commands": tests,
        "big_files": sorted(big, key=lambda b: -b["kb"])[:20],
        "big_code_files": sorted(big_code, key=lambda b: -b["kb"])[:10],
        "secret_files": secrets_found[:20],
        "sensitive_code": sensitive[:30],
        "docs": sorted(docs)[:10],
        "existing_customizations": existing,
        "always_loaded_instructions": always_on,
    }


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------

def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"


def validate_plan(plan: dict, tiers: dict) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    p = dict(plan)
    p.setdefault("project", "Project")
    p.setdefault("main_name", p["project"])
    p.setdefault("summary", "")
    for key in ("routine", "avoid_files", "rules", "specialists"):
        p.setdefault(key, [])
        if not isinstance(p[key], list):
            raise ValueError(f"'{key}' must be a list")
    p.setdefault("test_command", "")
    p.setdefault("providers", ["copilot"])
    if not isinstance(p["providers"], list) or not p["providers"] or any(
            provider not in ("copilot", "claude") for provider in p["providers"]):
        raise ValueError("'providers' must be a non-empty list containing only 'copilot' or 'claude'")
    p["providers"] = list(dict.fromkeys(p["providers"]))
    policy = p.get("provider_policy") or {}
    if not isinstance(policy, dict):
        raise ValueError("'provider_policy' must be an object")
    merged = json.loads(json.dumps(DEFAULT_PROVIDER_POLICY))
    merged.update({key: value for key, value in policy.items() if key != "routing"})
    routing = dict(merged["routing"])
    if policy.get("routing") is not None and not isinstance(policy.get("routing"), dict):
        raise ValueError("'provider_policy.routing' must be an object")
    if isinstance(policy.get("routing"), dict):
        routing.update(policy["routing"])
    merged["routing"] = routing
    allowed = merged["allowed"]
    if not isinstance(allowed, list) or not allowed or any(provider not in TRUSTED_PROVIDERS for provider in allowed):
        raise ValueError(f"'provider_policy.allowed' must contain only trusted providers: {', '.join(TRUSTED_PROVIDERS)}")
    merged["allowed"] = list(dict.fromkeys(allowed))
    for tier, providers in merged["routing"].items():
        if tier not in ("cheap", "standard", "accurate", "checker") or not isinstance(providers, list) or \
                not providers or any(provider not in merged["allowed"] for provider in providers):
            raise ValueError(f"provider routing for {tier!r} must be a non-empty list from 'allowed'")
        merged["routing"][tier] = list(dict.fromkeys(providers))
    for key in ("fallback_on", "sensitive_allowed"):
        if not isinstance(merged[key], list) or (key == "sensitive_allowed" and
                                                   any(provider not in merged["allowed"] for provider in merged[key])):
            raise ValueError(f"'provider_policy.{key}' is invalid")
    dispatcher = merged["dispatcher"]
    if not isinstance(dispatcher, dict) or dispatcher.get("external_mode") not in ("explicit_only", "compatible_tasks"):
        raise ValueError("'provider_policy.dispatcher.external_mode' must be 'explicit_only' or 'compatible_tasks'")
    if dispatcher.get("sensitive_external") not in ("copilot_only", "trusted_external"):
        raise ValueError("'provider_policy.dispatcher.sensitive_external' is invalid")
    if dispatcher.get("external_cost_mode") != "free_only" or \
            dispatcher.get("require_free_quota_confirmation") is not True:
        raise ValueError("external routing must be free-only and require free-quota confirmation")
    p["provider_policy"] = merged
    p["guard_tokens"] = int(p.get("guard_tokens") or 100_000)
    names = {p["main_name"]}
    out = []
    for s in p["specialists"]:
        if not isinstance(s, dict) or not s.get("name") or not s.get("when"):
            raise ValueError("each specialist needs 'name' and 'when'")
        s = dict(s)
        s.setdefault("role", "custom")
        if s["role"] not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {s['role']!r}")
        s.setdefault("tier", {"developer": "standard", "reviewer": "accurate", "extractor": "accurate"}.get(s["role"], "standard"))
        if s["tier"] not in tiers:
            raise ValueError(f"unknown tier {s['tier']!r} (known: {', '.join(tiers)})")
        s.setdefault("instructions", "")
        s["read_only"] = bool(s.get("read_only", s["role"] == "reviewer"))
        s["double_check"] = bool(s.get("double_check", False)) and s["role"] == "extractor"
        if s["name"] in names:
            raise ValueError(f"duplicate agent name {s['name']!r}")
        names.add(s["name"])
        out.append(s)
        if s["double_check"]:
            checker = {"name": s.get("checker_name") or f"{s['name']} Checker", "role": "checker", "tier": "checker",
                       "for": s["name"], "when": f"independent second pass after every {s['name']} run",
                       "instructions": "", "read_only": True, "double_check": False}
            if checker["name"] in names:
                raise ValueError(f"duplicate agent name {checker['name']!r}")
            names.add(checker["name"])
            out.append(checker)
    p["specialists"] = out
    return p


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _models(tier: dict) -> str:
    return "[" + ", ".join(f"'{m}'" for m in tier["models"]) + "]"


def _yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


def _common_constraints(plan: dict) -> list[str]:
    c = list(plan["rules"])
    if plan["avoid_files"]:
        c.append("DO NOT open these large files in full; search them for the exact term and read only the "
                 "matching lines: " + ", ".join(f"`{f}`" for f in plan["avoid_files"]) + ".")
    return c


def render_main(plan: dict, tiers: dict) -> str:
    cheap = tiers["cheap"]
    specs = plan["specialists"]
    delegable = [s for s in specs if s["role"] != "checker"]
    agents = "[" + ", ".join(f"'{s['name']}'" for s in specs) + "]"
    rows = []
    for s in delegable:
        action = f"Run the **two-pass check** below (starts with **{s['name']}**)" if s["double_check"] \
            else f"Delegate to **{s['name']}**"
        rows.append(f"| {s['when']} | {action} |")
    routine = ", ".join(plan["routine"]) or "running tools and tests, small edits, docs, git"
    rows.append(f"| {routine} | Do it yourself |")
    two_pass = ""
    for s in delegable:
        if not s["double_check"]:
            continue
        chk = next(c for c in specs if c.get("for") == s["name"])
        two_pass += f"""
## Two-pass check for {s['name']} (accuracy matters most)
1. Delegate the work to **{s['name']}**. It returns each extracted item with an ID and its exact
   location in the source, plus the items it is unsure of.
2. Delegate to **{chk['name']}** with ONLY the source path and, per item, its ID, location and what
   it is (never the first values): the second pass must be independent.
3. Compare the two passes item by item (normalise units and number formats first).
   Same value: accept it as agreed. Different, unreadable or low confidence on either side:
   keep it unconfirmed, record both readings, and ask the user.
4. Show the user a table: ID | item | first pass | second pass | agreed / needs confirmation.
   Agreement is not confirmation; confirmation stays with the user.
"""
    constraints = _bullets(_common_constraints(plan) + [
        "Never do a specialist's work yourself, even if it looks easy."])
    tests = f"\nRun tests with `{plan['test_command']}`.\n" if plan["test_command"] else ""
    desc = (f"Main {plan['project']} agent for all work in this project. Does routine work itself on a low-cost "
            f"model and automatically delegates to: " + "; ".join(f"{s['name']} ({s['when']})" for s in delegable) + ".")
    return f"""---
name: {_yaml_str(plan['main_name'])}
description: {_yaml_str(desc)}
model: {_models(cheap)}
agents: {agents}
---
<!-- {MARK} {VERSION}: edit .github/spend-guard.json and run `spendguard.py update` instead of this file -->
You are the entry point for {plan['project']}{(': ' + plan['summary']) if plan['summary'] else ''}. You run on a
low-cost model: do routine work yourself and hand the expensive kinds of work to specialist
subagents on stronger models. Decide this for every request; the user does not pick agents.
At the start of a new task, read `.github/notes.md` for the short handoff from previous chats. When
the task finishes, append 1-3 lines with the decision, current state, half-done work or a gotcha.
{tests}
## Routing (decide before doing anything)
| The request needs... | Do this |
|---|---|
{chr(10).join(rows)}
{two_pass}
## Delegating
- Give the subagent everything in one message: the goal, files and paths, expected result and
  what to return. It does not see this conversation.
- Use its result as given; do not redo its work.

## Trusted provider policy
- For compatible external API tools only, use providers from `.github/spend-guard-providers.json`.
- Use one primary provider per task. Fall back only for an unavailable provider, timeout or failed validation;
  never ask every provider to answer the same request.
- Keep authentication, payments, secrets and security reviews on the `sensitive_allowed` list.
- This policy does not reroute GitHub Copilot's own endpoint or billing.

## Constraints
{constraints}

## Output Format
A short summary: what was delegated (and its result), commands run, results, and anything
waiting on the user.
"""


def render_specialist(s: dict, plan: dict, tiers: dict) -> str:
    tier = tiers[s["tier"]]
    effort = f"\nreasoning-effort: {tier['reasoning_effort']}" if tier.get("reasoning_effort") else ""
    tools = "\ntools: [read, search, execute]" if s["read_only"] else ""
    hidden = "\nuser-invocable: false" if s["role"] in ("reviewer", "checker") else ""
    role = s["role"]
    extra = s["instructions"].strip()
    constraints = _common_constraints(plan)
    if s["read_only"]:
        constraints.append("Read-only: do not edit, create or delete files.")
    if role == "developer":
        intro = f"You write and fix code for {plan['project']}."
        approach = [
            "Search for the relevant code; read only the ranges you need.",
            "Handle every variant of the input the bug or feature involves, not only the example given, "
            "and add a test built from the real example.",
            f"Make the change and run the tests{(' (`' + plan['test_command'] + '`)') if plan['test_command'] else ''}.",
            "If the same problem survives two attempts, or it is a hard design question, stop and report that "
            "it needs a stronger model, with what you tried."]
        out = "Files changed and what each change does, test results, and whether the change touches " \
              "security-sensitive code."
    elif role == "reviewer":
        intro = f"You review {plan['project']} changes. Mistakes here matter, so be thorough and concrete."
        approach = ["Read the diff (`git --no-pager diff`) and only the code around it.",
                    "For each concern, construct a concrete failure scenario; drop anything you can't make concrete."]
        out = "Findings ordered by severity, each with file/line, the concrete scenario and a suggested fix; " \
              "or \"No issues found\"."
    elif role == "extractor":
        intro = f"You extract information from source material for {plan['project']}. Accuracy is the top " \
                "requirement; take the time you need."
        approach = ["Work from the source at full resolution, one item at a time. Record the literal text exactly "
                    "as written before its interpretation.",
                    "Give every item an ID and its exact location in the source (page and region), because an "
                    "independent second pass is made from those locations." if s["double_check"]
                    else "Give every item an ID and its exact location in the source.",
                    "Watch for look-alike characters, decimal separators and units.",
                    "If you are not sure, say so and explain why. Never guess to fill a gap."]
        out = "Each item with ID, location, literal text and interpretation, and the list of items you are " \
              "unsure of."
    elif role == "checker":
        intro = (f"You make an independent second pass of work done by {s['for']}. You are deliberately not told "
                 "its results.")
        approach = ["For each item you are given (ID, location, what it is), go to that location in the source "
                    "and read it yourself.",
                    "Record the literal text, your interpretation and a confidence: high, medium or low.",
                    "If the location doesn't contain the item, say so."]
        constraints.append(f"DO NOT read {s['for']}'s results, notes or any earlier output: your pass must be "
                           "independent. Use only the source and the IDs/locations you were given.")
        out = "One line per ID: `ID | literal text | interpretation | confidence | note`."
    else:
        intro = f"You are a specialist for {plan['project']}."
        approach = []
        out = "A concise result the main agent can act on."
    body_extra = f"\n{extra}\n" if extra else ""
    approach_md = ("\n## Approach\n" + "\n".join(f"{i}. {a}" for i, a in enumerate(approach, 1)) + "\n") if approach else ""
    return f"""---
name: {_yaml_str(s['name'])}
description: {_yaml_str(f"{s['when']}. Used by the main {plan['main_name']} agent.")}
model: {_models(tier)}{effort}{tools}{hidden}
---
<!-- {MARK} {VERSION}: edit .github/spend-guard.json and run `spendguard.py update` instead of this file -->
{intro}
{body_extra}{approach_md}
## Constraints
{_bullets(constraints)}
Read `.github/notes.md` when you need the project's current handoff context; never put secrets or client
data there.

## Output Format
{out} This is returned to the main {plan['main_name']} agent.
"""


def render_claude_main(plan: dict) -> str:
    rows = []
    for s in plan["specialists"]:
        if s["role"] == "checker":
            continue
        action = f"invoke `{slug(s['name'])}`" + (" with an independent checker" if s["double_check"] else "")
        rows.append(f"| {s['when']} | {action} |")
    rows.append(f"| {', '.join(plan['routine']) or 'Routine work'} | Do it yourself |")
    return f"""---
name: spend-guard
description: {_yaml_str(f"Route {plan['project']} work to the appropriate cost and accuracy tier.")}
---
<!-- {MARK} {VERSION}: generated for Claude Code; edit the plan and run `spendguard.py update` -->
# Spend-Guard routing for {plan['project']}

This project uses Spend-Guard to keep routine work inexpensive while reserving stronger Claude
models for code, security and accuracy-critical work. Read `.github/notes.md` at the start of
a new task and append a 1-3 line durable handoff when the task finishes. Never put secrets or
client data in the notes.

## Routing
| The request needs... | Do this |
|---|---|
{chr(10).join(rows)}

## Constraints
{_bullets(_common_constraints(plan) + ["Do not read a specialist's results and redo its work yourself."])}
"""


def render_claude_specialist(s: dict, plan: dict) -> str:
    role = s["role"]
    if role == "checker":
        intro = f"Independently check the work of `{slug(s['for'])}` without reading its results."
    elif role == "developer":
        intro = f"Implement code changes for {plan['project']} as the standard-tier developer."
    elif role == "reviewer":
        intro = f"Perform a read-only accurate security review for {plan['project']}."
    elif role == "extractor":
        intro = f"Extract information accurately for {plan['project']}."
    else:
        intro = f"Act as a specialist for {plan['project']}."
    extra = s["instructions"].strip()
    return f"""---
name: {_yaml_str(slug(s['name']))}
description: {_yaml_str(s['when'])}
---
<!-- {MARK} {VERSION}: generated for Claude Code; edit the plan and run `spendguard.py update` -->
{intro}

{extra}

## Constraints
{_bullets(_common_constraints(plan) + (["Read-only: do not edit, create or delete files."]
                                         if s["read_only"] else []))}
"""


def render_section(plan: dict, tiers: dict) -> str:
    lines = [SECTION_START, "## Model routing (Spend-Guard)", "",
             f"New chats here start on a low-cost model (`.vscode/settings.json`). Whichever agent you are, "
             f"route every request yourself; the user does not pick agents. The full routing table is in "
             f"`.github/agents/{slug(plan['main_name'])}.agent.md`:"]
    lines.append("- At the start of a new task, read `.github/notes.md` for the short handoff from previous chats; "
                 "append 1-3 lines when the task finishes.")
    lines.append("- For compatible external API tools, use only the trusted providers in "
                 "`.github/spend-guard-providers.json`; use one primary and fallback only on failure.")
    for s in plan["specialists"]:
        if s["role"] == "checker":
            continue
        tail = " (with an independent second pass by the checker; disagreements go to the user)" \
            if s["double_check"] else ""
        lines.append(f"- {s['when']}: delegate to the `{s['name']}` subagent{tail}.")
    lines.append(f"- {', '.join(plan['routine']) or 'Routine work'}: do it yourself.")
    if plan["avoid_files"]:
        lines.append("- Never open " + ", ".join(f"`{f}`" for f in plan["avoid_files"]) + " in full; search them.")
    lines += ["", "### Long chats",
              f"`.github/hooks/spend_guard.py` adds a \"[Spend-Guard …]\" note to the message once this chat "
              f"passes ~{plan['guard_tokens'] // 1000}k tokens of context. Continue follow-ups here; move a new task to "
              "a fresh chat with a short handoff. Never put secrets or client data in `.github/notes.md`.", SECTION_END]
    return "\n".join(lines) + "\n"


GUARD_JSON = """{
  "version": 1,
  "hooks": {
    "sessionStart": [
      {
        "type": "command",
        "powershell": "python .github/hooks/session_start.py",
        "bash": "python3 .github/hooks/session_start.py",
        "timeoutSec": 10
      }
    ],
    "userPromptTransformed": [
      {
        "type": "command",
        "powershell": "python .github/hooks/spend_guard.py",
        "bash": "python3 .github/hooks/spend_guard.py",
        "timeoutSec": 10
      }
    ]
  }
}
"""

GUARD_PY = '''"""Spend-Guard hook ({mark} {version}): a Copilot userPromptTransformed hook.

Every model call resends the whole chat. When this chat's context passes a threshold, append
a note asking the agent to move a NEW task to a fresh low-cost chat with a short handoff.
Follow-ups continue normally. Fails open: on any problem the message is left unchanged.
"""

import json
import os
import re
import sys
from pathlib import Path

SOFT_TOKENS = int(os.environ.get("SPEND_GUARD_TOKENS", "{tokens}"))
MODEL = "{model}"


def context_tokens(session_id):
    home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))
    with open(home / "session-state" / session_id / "events.jsonl", "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4_000_000))
        lines = f.read().split(b"\\n")
    for raw in reversed(lines):
        if b'"session.usage_record"' in raw[:80]:
            try:
                usage = json.loads(raw)["data"]["usage"]
            except (ValueError, KeyError):
                continue
            if usage.get("initiator") != "sub-agent":
                return int(usage.get("inputTokens") or 0)
    return 0


def append_handoff(data, tokens):
    cwd = data.get("cwd") or data.get("workspace") or data.get("workspaceFolder")
    if isinstance(cwd, dict):
        cwd = cwd.get("cwd") or cwd.get("path")
    if not cwd:
        return
    path = Path(str(cwd)) / ".github" / "notes.md"
    prompt = data.get("transformedPrompt")
    if not isinstance(prompt, str) or not prompt.strip() or not path.exists():
        return
    line = " ".join(prompt.strip().split())[:500]
    if re.search(r"(^|[\\\\/])(?:config\\.json|\\.env(?:\\b|[.]))", line, re.I):
        return
    with path.open("a", encoding="utf-8", newline="\\n") as f:
        f.write(f"\\n- Cost-guard handoff (~{{tokens // 1000}}k): {{line}}\\n")


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{{}}")
    sid = data.get("sessionId") or data.get("session_id") or ""
    content = data.get("transformedPrompt")
    if not sid or not isinstance(content, str) or not content.strip():
        return "{{}}"
    tokens = context_tokens(sid)
    if tokens < SOFT_TOKENS:
        return "{{}}"
    append_handoff(data, tokens)
    note = (
        f"\\n\\n[Spend-Guard: this chat is long (~{{tokens // 1000}}k tokens of history are resent with every "
        "request). Decide whether the request above is a NEW task or a follow-up. Follow-up: ignore this note. "
        "New task: do not start it here. If you have the create_session tool, call it with model "
        f"\\"{{MODEL}}\\", this workspace, a short title, and a prompt containing a handoff (goal, key files, "
        "decisions and state that matter, max ~15 lines). Also append the handoff to `.github/notes.md` before "
        "moving when the task has a durable decision or half-done state. Never put secrets or client data there. "
        "Then call create_session with the user's request verbatim; reply in "
        "one line with the link. Otherwise tell the user in one line to start a new chat and give a 3-line "
        "handoff to paste.]"
    )
    return json.dumps({{"modifiedTransformedPrompt": content + note}})


if __name__ == "__main__":
    try:
        out = main()
    except Exception:
        out = "{{}}"
    sys.stdout.write(out)
    sys.stdout.flush()
'''


SESSION_START_PY = r'''"""Spend-Guard sessionStart hook (managed by spend-guard): inexpensive deterministic project context."""

import json
import os
import re
import sys
from pathlib import Path

MAX_CONTEXT = 6000
MAX_NOTES_LINES = 60
MAX_MESSAGE = 700


def _cwd(data):
    value = data.get("cwd") or data.get("workspace") or data.get("workspaceFolder")
    if isinstance(value, dict):
        value = value.get("cwd") or value.get("path")
    return os.path.normcase(os.path.abspath(str(value))) if value else ""


def _workspace(path):
    try:
        for line in (path / "workspace.yaml").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("cwd:"):
                return os.path.normcase(os.path.abspath(line[4:].strip().strip('"')))
    except OSError:
        pass
    return ""


def _message(record, wanted):
    found = []
    def walk(value, key=""):
        if isinstance(value, dict):
            for name, child in value.items():
                low = str(name).lower()
                if isinstance(child, str) and (low in wanted or low.endswith("content")):
                    found.append(child)
                else:
                    walk(child, low)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
    walk(record)
    text = "\n".join(found).strip()
    safe = [line for line in text.splitlines()
            if not re.search(r"(^|[\\/])(?:config\.json|\.env(?:\b|[.]))", line, re.I)]
    return "\n".join(safe).strip()


def _previous(home, current, cwd):
    candidates = []
    for path in home.glob("session-state/*"):
        if not path.is_dir() or path.name == current or _workspace(path) != cwd:
            continue
        log = path / "events.jsonl"
        if not log.exists():
            continue
        try:
            head = log.read_bytes()[:200000].lower()
        except OSError:
            continue
        if b'"initiator":"sub-agent"' in head or b'"automation"' in head:
            continue
        candidates.append((log.stat().st_mtime, path.name, log))
    if not candidates:
        return ""
    _, sid, log = max(candidates)
    users, final_assistant = [], ""
    try:
        for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            kind = str(record.get("type", "")).lower()
            if "user.message" in kind:
                text = _message(record, {"content", "text", "message", "prompt"})
                if text:
                    users.append(text)
            elif "assistant.message" in kind:
                text = _message(record, {"content", "text", "message", "response"})
                if text:
                    final_assistant = text
    except OSError:
        return ""
    parts = [f"Previous chat: {sid}"]
    parts.extend(f"User: {text[-MAX_MESSAGE:]}" for text in users[-8:])
    if final_assistant:
        parts.append(f"Final assistant: {final_assistant[-MAX_MESSAGE:]}")
    return "\n".join(parts)


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        current = data.get("sessionId") or data.get("session_id") or ""
        cwd = _cwd(data)
        if not current or not cwd:
            return "{}"
        home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))
        notes_path = Path(cwd) / ".github" / "notes.md"
        notes = ""
        if notes_path.exists():
            notes = "\n".join(notes_path.read_text(encoding="utf-8", errors="replace")
                               .splitlines()[-MAX_NOTES_LINES:])
        graph = ""
        graph_path = Path(cwd) / ".github" / "spend-guard-graph" / "GRAPH_REPORT.md"
        if graph_path.exists():
            graph = "\n".join(graph_path.read_text(encoding="utf-8", errors="replace")
                              .splitlines()[:40])
        previous = _previous(home, current, cwd)
        context = "Spend-Guard startup context\n"
        if notes:
            context += "\nProject notes:\n" + notes
        if graph:
            context += "\n\nLocal code graph summary:\n" + graph
        if previous:
            context += "\n\n" + previous
        return json.dumps({"additionalContext": context[-MAX_CONTEXT:]}) if context.strip() else "{}"
    except Exception:
        return "{}"


if __name__ == "__main__":
    sys.stdout.write(main())
'''


# ---------------------------------------------------------------------------
# apply / update / remove
# ---------------------------------------------------------------------------

def _write_managed(path: Path, text: str, log: list[str], dry: bool) -> None:
    if path.exists():
        old = path.read_text(encoding="utf-8", errors="replace")
        if MARK not in old:
            log.append(f"skipped {path.as_posix()}: exists and wasn't created by Spend-Guard (rename it or remove it)")
            return
        if old == text:
            log.append(f"unchanged {path.as_posix()}")
            return
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    log.append(f"wrote {path.as_posix()}")


def _seed_notes(folder: Path, log: list[str], dry: bool) -> None:
    """Create the user-maintained handoff file once; never rewrite or remove it."""
    path = folder / NOTES_FILE
    if path.exists():
        log.append(f"left {path.as_posix()} alone (user-maintained project notes)")
        return
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {folder.name} project notes\n\n"
            "Short handoff from previous chats. Append 1-3 lines when a task finishes: a decision, "
            "current state, half-done work or a gotcha. Condense older entries when this reaches about "
            "60 lines. Never put secrets or client data here.\n",
            encoding="utf-8", newline="\n")
    log.append(f"wrote {path.as_posix()} (initial project notes)")


def _provider_policy_json(policy: dict) -> str:
    return json.dumps({
        "description": f"{MARK}: opt-in provider policy; no credentials or provider SDKs",
        "version": VERSION,
        **policy,
    }, indent=2) + "\n"


DISPATCH_KEYWORDS = {
    "sensitive": ("security", "authentication", "authorization", "permission", "secret", "credential",
                  "password", "token", "signing", "cryptograph", "payment", "billing"),
    "accuracy": ("invoice", "scan", "ocr", "document", "handwriting", "compliance", "legal", "extract"),
    "code": ("implement", "feature", "fix", "bug", "refactor", "code", "test", "debug"),
}


def _task_kind(task: str) -> str:
    text = task.lower()
    for kind in ("sensitive", "accuracy", "code"):
        if any(word in text for word in DISPATCH_KEYWORDS[kind]):
            return kind
    return "routine"


def dispatch_task(folder: Path, task: str, free_quota_confirmed: bool = False) -> dict:
    """Return a deterministic route for a compatible client; never invokes a provider."""
    plan = validate_plan(load_plan(folder), load_tiers())
    kind = _task_kind(task)
    tier = {"routine": "cheap", "code": "standard", "accuracy": "accurate", "sensitive": "accurate"}[kind]
    policy = plan["provider_policy"]
    copilot = {"target": "copilot", "tier": tier, "model": load_tiers()[tier]["models"][0],
               "model_calls": 0}
    if not free_quota_confirmed:
        return {**copilot, "kind": kind, "external_blocked": "free quota was not explicitly confirmed",
                "reason": "external routing is free-only; use Copilot until quota is confirmed"}
    fallback = {"target": "copilot", "tier": tier, "reason": "trusted local fallback"}
    external_allowed = policy["dispatcher"]["external_mode"] == "compatible_tasks"
    if kind == "sensitive":
        external_allowed = external_allowed and policy["dispatcher"]["sensitive_external"] == "trusted_external"
    candidates = policy["routing"][tier]
    if kind == "sensitive":
        candidates = [p for p in candidates if p in policy["sensitive_allowed"]]
    if not external_allowed or not candidates:
        return {**copilot, "kind": kind, "fallback": None,
                "reason": "Copilot is the configured primary for this task"}
    return {"kind": kind, "tier": tier, "target": "external", "provider": candidates[0],
            "allowed_providers": candidates, "fallback": fallback, "model_calls": 0,
            "cost_mode": "free_only",
            "reason": "route-only decision; caller confirmed free quota; the calling client must execute "
                       "the approved provider"}


def validate_project(folder: Path, strict: bool = False) -> dict:
    """Audit generated setup files and policy without executing project code."""
    folder = folder.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []

    def require(path: Path, label: str) -> str:
        if not path.exists():
            errors.append(f"missing {label}: {path.relative_to(folder).as_posix()}")
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        if MARK not in text and path.name != "notes.md":
            errors.append(f"{label} is not marked as managed by Spend-Guard: {path.relative_to(folder).as_posix()}")
        return text

    try:
        plan = validate_plan(load_plan(folder), load_tiers())
        checks.append("saved plan is valid")
    except (ValueError, FileNotFoundError) as exc:
        errors.append(str(exc))
        return {"ok": False, "strict": strict, "errors": errors, "warnings": warnings, "checks": checks,
                "model_calls": 0}

    main_path = folder / ".github" / "agents" / f"{slug(plan['main_name'])}.agent.md"
    require(main_path, "main agent")
    for specialist in plan["specialists"]:
        require(folder / ".github" / "agents" / f"{slug(specialist['name'])}.agent.md",
                f"specialist {specialist['name']}")
    hook_json_text = require(folder / ".github" / "hooks" / "spend-guard.json", "hook registration")
    hook_text = require(folder / ".github" / "hooks" / "spend_guard.py", "cost guard hook")
    session_text = require(folder / ".github" / "hooks" / "session_start.py", "session-start hook")
    notes_path = folder / NOTES_FILE
    if not notes_path.exists():
        errors.append(f"missing project notes: {NOTES_FILE.as_posix()}")
    else:
        checks.append("project notes exist")
    provider_path = folder / PROVIDER_POLICY_FILE
    provider_text = require(provider_path, "provider policy")
    try:
        hook = json.loads(hook_json_text)
        if not {"sessionStart", "userPromptTransformed"}.issubset(hook.get("hooks", {})):
            errors.append("hook registration must include sessionStart and userPromptTransformed")
        else:
            checks.append("hook registration is valid")
    except ValueError:
        errors.append("hook registration is not valid JSON")
    try:
        provider = json.loads(provider_text)
        if provider.get("allowed") != plan["provider_policy"]["allowed"]:
            errors.append("generated provider allowlist differs from the saved plan")
        dispatcher = provider.get("dispatcher", {})
        if dispatcher.get("external_cost_mode") != "free_only" or \
                dispatcher.get("require_free_quota_confirmation") is not True:
            errors.append("provider policy does not enforce free-only external routing")
        else:
            checks.append("provider policy is trusted and free-only")
    except ValueError:
        errors.append("provider policy is not valid JSON")
    for label, text in (("cost guard hook", hook_text), ("session-start hook", session_text)):
        if text:
            try:
                compile(text, label, "exec")
            except SyntaxError as exc:
                errors.append(f"{label} has a syntax error: line {exc.lineno}")
            else:
                checks.append(f"{label} compiles")
    if not plan["test_command"]:
        warnings.append("no test command is configured")
    sensitive_paths = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in names:
            rel = (Path(root) / name).relative_to(folder).as_posix()
            if SENSITIVE.search(rel):
                sensitive_paths.append(rel)
    if sensitive_paths and not any(s["role"] == "reviewer" for s in plan["specialists"]):
        warnings.append("sensitive-looking paths exist without a generated reviewer: " +
                        ", ".join(sensitive_paths[:5]))
    else:
        checks.append("specialist coverage is configured for detected sensitive paths")
    if strict and warnings:
        errors.extend(f"strict: {warning}" for warning in warnings)
    return {"ok": not errors, "strict": strict, "errors": errors, "warnings": warnings,
            "checks": checks, "model_calls": 0}


def _strip_jsonc(text: str) -> str:
    return re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", text, flags=re.S)


def _settings(folder: Path, model_id: str, log: list[str], dry: bool, remove: bool = False) -> bool:
    """Set (or unset) chat.defaultModel in .vscode/settings.json. Returns True if Spend-Guard created the file."""
    path = folder / ".vscode" / "settings.json"
    key = "chat.defaultModel"
    if not path.exists():
        if remove:
            return False
        if not dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{\n  // " + MARK + ": new chats start on the low-cost model\n"
                            f'  "{key}": "{model_id}"\n}}\n', encoding="utf-8", newline="\n")
        log.append(f"wrote {path.as_posix()} ({key} = {model_id})")
        return True
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if MARK in text and text.count("\n") <= 4:
        if remove:
            if not dry:
                path.unlink()
            log.append(f"removed {path.as_posix()}")
            return False
        new = re.sub(rf'"{re.escape(key)}"\s*:\s*"[^"]*"', f'"{key}": "{model_id}"', text)
        if new != text and not dry:
            path.write_text(new, encoding="utf-8", newline="\n")
        log.append(f"{'updated' if new != text else 'unchanged'} {path.as_posix()}")
        return True
    try:
        data = json.loads(_strip_jsonc(text) or "{}")
    except ValueError:
        data = None
    if remove:
        if isinstance(data, dict) and data.get(key) == model_id and _strip_jsonc(text) == text:
            data.pop(key)
            if not dry:
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
            log.append(f"removed {key} from {path.as_posix()}")
        return False
    if isinstance(data, dict) and data.get(key) == model_id:
        log.append(f"unchanged {path.as_posix()}")
    elif isinstance(data, dict) and key not in data and _strip_jsonc(text) == text:
        data[key] = model_id
        if not dry:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
        log.append(f"added {key} = {model_id} to {path.as_posix()}")
    else:
        log.append(f"left {path.as_posix()} alone (has comments or its own {key}): add \"{key}\": \"{model_id}\" "
                   "yourself if you want new chats to start on the low-cost model")
    return False


def _section(folder: Path, section: str | None, log: list[str], dry: bool) -> None:
    path = folder / "AGENTS.md"
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    pattern = re.compile(re.escape(SECTION_START) + r".*?" + re.escape(SECTION_END) + r"\n?", re.S)
    if section is None:
        if not pattern.search(text):
            return
        new = pattern.sub("", text).rstrip() + "\n"
        if not dry:
            if new.strip():
                path.write_text(new, encoding="utf-8", newline="\n")
            else:
                path.unlink()
        log.append(f"removed the Spend-Guard section from {path.as_posix()}")
        return
    if pattern.search(text):
        new = pattern.sub(lambda _m: section, text)
    elif text.strip():
        new = text.rstrip() + "\n\n" + section
    else:
        new = f"# {folder.name}: agent guide\n\n" + section
    if new == text:
        log.append(f"unchanged {path.as_posix()}")
        return
    if not dry:
        path.write_text(new, encoding="utf-8", newline="\n")
    log.append(f"wrote the Spend-Guard section in {path.as_posix()}")


def _claude_section(folder: Path, section: str | None, log: list[str], dry: bool) -> None:
    path = folder / "CLAUDE.md"
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    pattern = re.compile(re.escape(CLAUDE_SECTION_START) + r".*?" + re.escape(CLAUDE_SECTION_END) + r"\n?", re.S)
    if section is None:
        if not pattern.search(text):
            return
        new = pattern.sub("", text).rstrip() + "\n"
        if not dry:
            if new.strip():
                path.write_text(new, encoding="utf-8", newline="\n")
            else:
                path.unlink()
        log.append(f"removed the Spend-Guard section from {path.as_posix()}")
        return
    if pattern.search(text):
        new = pattern.sub(lambda _m: section, text)
    elif text.strip():
        new = text.rstrip() + "\n\n" + section
    else:
        new = section
    if new == text:
        log.append(f"unchanged {path.as_posix()}")
        return
    if not dry:
        path.write_text(new, encoding="utf-8", newline="\n")
    log.append(f"wrote the Spend-Guard section in {path.as_posix()}")


def render_claude_section(plan: dict) -> str:
    lines = [CLAUDE_SECTION_START, "## Spend-Guard routing", "",
             "Use the Spend-Guard skills under `.claude/skills/` to route work by cost and risk. "
             "Read `.github/notes.md` at the start of a new task and append a short durable handoff "
             "when it finishes. Never put secrets or client data in the notes.",
             f"Main routing skill: `.claude/skills/spend-guard/SKILL.md`."]
    for s in plan["specialists"]:
        if s["role"] != "checker":
            lines.append(f"- {s['when']}: invoke `.claude/skills/{slug(s['name'])}/SKILL.md`.")
    lines += [CLAUDE_SECTION_END]
    return "\n".join(lines) + "\n"


def apply(folder: Path, plan: dict, dry: bool = False) -> list[str]:
    folder = folder.resolve()
    tiers = load_tiers()
    plan = validate_plan(plan, tiers)
    log: list[str] = []
    agents_dir = folder / ".github" / "agents"
    wanted = {slug(plan["main_name"]) + ".agent.md"}
    _write_managed(agents_dir / f"{slug(plan['main_name'])}.agent.md", render_main(plan, tiers), log, dry)
    for s in plan["specialists"]:
        fname = f"{slug(s['name'])}.agent.md"
        wanted.add(fname)
        _write_managed(agents_dir / fname, render_specialist(s, plan, tiers), log, dry)
    for old in agents_dir.glob("*.agent.md") if agents_dir.exists() else []:
        if old.name not in wanted and MARK in old.read_text(encoding="utf-8", errors="replace"):
            if not dry:
                old.unlink()
            log.append(f"removed {old.as_posix()} (no longer in the plan)")
    hooks = folder / ".github" / "hooks"
    _write_managed(hooks / "spend_guard.py", GUARD_PY.format(
        mark=MARK, version=VERSION, tokens=plan["guard_tokens"], model=tiers["cheap"]["default_model_id"]), log, dry)
    _write_managed(hooks / "session_start.py", SESSION_START_PY, log, dry)
    guard_json = GUARD_JSON.replace('"version": 1,', f'"version": 1,\n  "description": "{MARK}",')
    _write_managed(hooks / "spend-guard.json", guard_json, log, dry)
    _write_managed(folder / PROVIDER_POLICY_FILE, _provider_policy_json(plan["provider_policy"]), log, dry)
    _section(folder, render_section(plan, tiers), log, dry)
    if "claude" in plan["providers"]:
        claude_skills = folder / ".claude" / "skills"
        _write_managed(claude_skills / "spend-guard" / "SKILL.md",
                       render_claude_main(plan), log, dry)
        wanted_claude = {"spend-guard"}
        for s in plan["specialists"]:
            name = slug(s["name"])
            wanted_claude.add(name)
            _write_managed(claude_skills / name / "SKILL.md",
                           render_claude_specialist(s, plan), log, dry)
        if claude_skills.exists():
            for skill_dir in claude_skills.iterdir():
                skill_file = skill_dir / "SKILL.md"
                if skill_dir.is_dir() and skill_dir.name not in wanted_claude and skill_file.exists() and \
                        MARK in skill_file.read_text(encoding="utf-8", errors="replace"):
                    if not dry:
                        skill_file.unlink()
                        skill_dir.rmdir()
                    log.append(f"removed {skill_file.as_posix()} (no longer in the plan)")
        _claude_section(folder, render_claude_section(plan), log, dry)
    else:
        _claude_section(folder, None, log, dry)
    _seed_notes(folder, log, dry)
    created = _settings(folder, tiers["cheap"]["default_model_id"], log, dry)
    saved = {**plan, "spend_guard_version": VERSION}
    saved["specialists"] = [s for s in plan["specialists"] if s["role"] != "checker"]
    prev = {}
    if (folder / PLAN_FILE).exists():
        try:
            prev = json.loads((folder / PLAN_FILE).read_text(encoding="utf-8-sig"))
        except ValueError:
            prev = {}
    saved["applied"] = prev.get("applied") or dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    saved["created_settings"] = created or bool(prev.get("created_settings"))
    if not dry:
        (folder / PLAN_FILE).parent.mkdir(parents=True, exist_ok=True)
        (folder / PLAN_FILE).write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8", newline="\n")
    log.append(f"saved the plan to {(folder / PLAN_FILE).as_posix()}")
    return log


def load_plan(folder: Path) -> dict:
    path = folder.resolve() / PLAN_FILE
    if not path.exists():
        raise FileNotFoundError(f"no Spend-Guard plan in {folder} (run apply first)")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def remove(folder: Path, dry: bool = False) -> list[str]:
    folder = folder.resolve()
    log: list[str] = []
    provider_policy = folder / PROVIDER_POLICY_FILE
    if provider_policy.exists() and MARK in provider_policy.read_text(encoding="utf-8", errors="replace"):
        if not dry:
            provider_policy.unlink()
        log.append(f"removed {provider_policy.as_posix()}")
    for d in (folder / ".github" / "agents", folder / ".github" / "hooks"):
        for p in list(d.glob("*")) if d.exists() else []:
            if p.is_file() and MARK in p.read_text(encoding="utf-8", errors="replace"):
                if not dry:
                    p.unlink()
                log.append(f"removed {p.as_posix()}")
    _section(folder, None, log, dry)
    for skill_dir in (folder / ".claude" / "skills").glob("*") if (folder / ".claude" / "skills").exists() else []:
        skill_file = skill_dir / "SKILL.md"
        if skill_file.is_file() and MARK in skill_file.read_text(encoding="utf-8", errors="replace"):
            if not dry:
                skill_file.unlink()
                if skill_dir.exists() and not any(skill_dir.iterdir()):
                    skill_dir.rmdir()
            log.append(f"removed {skill_file.as_posix()}")
    _claude_section(folder, None, log, dry)
    plan = {}
    if (folder / PLAN_FILE).exists():
        plan = json.loads((folder / PLAN_FILE).read_text(encoding="utf-8-sig"))
    _settings(folder, load_tiers()["cheap"]["default_model_id"], log, dry, remove=True)
    if (folder / PLAN_FILE).exists():
        if not dry:
            (folder / PLAN_FILE).unlink()
        log.append(f"removed {(folder / PLAN_FILE).as_posix()}")
    for d in (folder / ".github" / "agents", folder / ".github" / "hooks", folder / ".vscode", folder / ".github",
              folder / ".claude" / "skills", folder / ".claude"):
        if d.exists() and not any(d.iterdir()) and not dry:
            d.rmdir()
    _ = plan
    return log


# ---------------------------------------------------------------------------
# local code graph
# ---------------------------------------------------------------------------

def _graph_files(folder: Path):
    for root, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".worktrees")
                   and (not d.startswith(".") or d in (".github", ".vscode"))]
        for name in names:
            path = Path(root) / name
            if path.is_symlink() or path.suffix.lower() not in GRAPH_EXTENSIONS:
                continue
            rel = path.relative_to(folder).as_posix()
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > GRAPH_MAX_BYTES or SECRET_FILES.search(rel):
                continue
            yield path, rel


def _graph_extract(path: Path, rel: str) -> tuple[list[dict], list[dict]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    module = "file:" + rel
    nodes = [{"id": module, "kind": "file", "path": rel, "evidence": "EXTRACTED"}]
    edges = []
    if path.suffix.lower() == ".py":
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError:
            tree = None
        if tree is not None:
            for item in ast.walk(tree):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    ident = module + ":" + item.name
                    nodes.append({"id": ident, "kind": "symbol", "name": item.name, "path": rel,
                                  "evidence": "EXTRACTED"})
                    edges.append({"from": module, "to": ident, "kind": "defines", "evidence": "EXTRACTED"})
                elif isinstance(item, ast.Import):
                    imports = [alias.name for alias in item.names]
                    edges.extend({"from": module, "to": "module:" + name, "kind": "imports",
                                  "evidence": "EXTRACTED"} for name in imports)
                elif isinstance(item, ast.ImportFrom) and item.module:
                    edges.append({"from": module, "to": "module:" + item.module, "kind": "imports",
                                  "evidence": "EXTRACTED"})
            return nodes, edges
    for match in GRAPH_DEFINITION.finditer(text):
        ident = module + ":" + match.group(1)
        nodes.append({"id": ident, "kind": "symbol", "name": match.group(1), "path": rel,
                      "evidence": "EXTRACTED"})
        edges.append({"from": module, "to": ident, "kind": "defines", "evidence": "EXTRACTED"})
    for match in GRAPH_IMPORT.finditer(text):
        name = match.group(1) or match.group(2)
        edges.append({"from": module, "to": "module:" + name, "kind": "imports",
                      "evidence": "EXTRACTED"})
    return nodes, edges


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def build_graph(folder: Path) -> dict:
    """Build a local, deterministic code graph without network or model calls."""
    folder = folder.resolve()
    old = {}
    graph_path = folder / GRAPH_FILE
    if graph_path.exists():
        try:
            saved = json.loads(graph_path.read_text(encoding="utf-8"))
            old = saved.get("files") or {}
        except (OSError, ValueError):
            old = {}
    files, all_nodes, all_edges = {}, {}, {}
    for path, rel in _graph_files(folder):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        cached = old.get(rel)
        if cached and cached.get("sha256") == digest:
            nodes, edges = cached.get("nodes", []), cached.get("edges", [])
        else:
            nodes, edges = _graph_extract(path, rel)
        files[rel] = {"sha256": digest, "nodes": nodes, "edges": edges}
        for node in nodes:
            all_nodes[node["id"]] = node
        for edge in edges:
            all_edges[json.dumps(edge, sort_keys=True)] = edge
    graph = {
        "version": 1,
        "description": "local deterministic code graph; no network or model calls",
        "files": files,
        "nodes": sorted(all_nodes.values(), key=lambda n: n["id"]),
        "edges": sorted(all_edges.values(), key=lambda e: (e["from"], e["to"], e["kind"])),
    }
    _write_json_atomic(graph_path, graph)
    report_lines = [
        "<!-- managed by spend-guard -->",
        "# Spend-Guard local code graph",
        "",
        "This report is generated locally from code structure only. It contains no source text, "
        "secrets, network data or model-generated inferences. It makes no network or model calls.",
        "",
        f"- Files indexed: {len(files)}",
        f"- Nodes: {len(graph['nodes'])}",
        f"- Edges: {len(graph['edges'])}",
        "",
        "## Relationships",
    ]
    for edge in graph["edges"][:100]:
        report_lines.append(f"- `{edge['from']}` --{edge['kind']}--> `{edge['to']}` ({edge['evidence']})")
    report_path = folder / GRAPH_REPORT
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8", newline="\n")
    return {"files": len(files), "nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
            "path": str(graph_path)}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _usage_records(root: Path):
    for d in root.iterdir() if root.exists() else []:
        f = d / "events.jsonl"
        if not f.exists():
            continue
        proj = d.name[:8]
        ws = d / "workspace.yaml"
        if ws.exists():
            for line in ws.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("cwd:"):
                    cwd = Path(line[4:].strip().strip("'\""))
                    proj = cwd.parent.name.removesuffix(".worktrees") if cwd.parent.name.endswith(".worktrees") \
                        else cwd.name
        with open(f, "rb") as fh:
            for raw in fh:
                if b'"session.usage_record"' not in raw[:80]:
                    continue
                try:
                    e = json.loads(raw)
                    u = e["data"]["usage"]
                    t = dt.datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                except (ValueError, KeyError):
                    continue
                yield (t, d.name, proj, u.get("modelDisplayName") or u.get("model") or "?",
                       ((u.get("copilotUsage") or {}).get("totalNanoAiu") or 0) / 1e9,
                       int(u.get("inputTokens") or 0), u.get("initiator"))


def report(days: int = 7, root: Path | None = None, now: dt.datetime | None = None) -> str:
    root = root or COPILOT_HOME / "session-state"
    now = now or dt.datetime.now(dt.timezone.utc)
    start, prev_start = now - dt.timedelta(days=days), now - dt.timedelta(days=2 * days)
    cur, prev = collections.defaultdict(float), collections.defaultdict(float)
    models, calls = collections.defaultdict(float), collections.Counter()
    peak: dict[tuple, int] = {}
    for t, sid, proj, model, credits, tokens, initiator in _usage_records(root):
        if t >= start:
            cur[proj] += credits
            models[model] += credits
            calls[model] += 1
            if initiator != "sub-agent":
                peak[(proj, sid)] = max(peak.get((proj, sid), 0), tokens)
        elif t >= prev_start:
            prev[proj] += credits
    tc, tp = sum(cur.values()), sum(prev.values())
    out = [f"AI usage, last {days} days (estimate from local Copilot logs; 1 credit is about $0.01)",
           f"  Total: {tc:,.0f} credits (~${tc / 100:,.2f})   previous {days} days: {tp:,.0f} (~${tp / 100:,.2f})"
           + (f"   change: {100 * (tc - tp) / tp:+.0f}%" if tp else ""), "By project:"]
    for p in sorted(set(cur) | set(prev), key=lambda k: -cur.get(k, 0)):
        c, pv = cur.get(p, 0.0), prev.get(p, 0.0)
        out.append(f"  {p:28} ~${c / 100:8,.2f}   (before: ~${pv / 100:,.2f}"
                   + (f", {100 * (c - pv) / pv:+.0f}%" if pv else "") + ")")
    out.append("By model:")
    for m, v in sorted(models.items(), key=lambda x: -x[1]):
        out.append(f"  {m:22} ~${v / 100:8,.2f}   {calls[m]} calls, {v / max(calls[m], 1):.2f} credits/call")
    long_chats = sorted(((k, v) for k, v in peak.items() if v >= 100_000), key=lambda x: -x[1])
    out.append(f"Chats past 100k tokens of context: {len(long_chats)}")
    out += [f"  {k[0]} ({k[1][:8]}): peak {v // 1000}k" for k, v in long_chats[:5]]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# local memory search
# ---------------------------------------------------------------------------

def _session_workspace(path: Path) -> str:
    try:
        for line in (path / "workspace.yaml").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("cwd:"):
                return os.path.normcase(os.path.abspath(line[4:].strip().strip('"')))
    except OSError:
        pass
    return ""


def _automated_session(log: Path) -> bool:
    try:
        head = log.read_bytes()[:200_000].lower()
    except OSError:
        return True
    return b"sub-agent" in head or b'"automation"' in head


def _memory_message(record: object) -> str:
    found: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                low = str(key).lower()
                if isinstance(child, str) and (low in {"content", "text", "message", "prompt", "response"}
                                               or low.endswith("content")):
                    found.append(child)
                elif isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(record)
    return "\n".join(found).strip()


def _safe_memory_text(text: str) -> str:
    if re.search(r"(^|[\\/])(?:config\.json|\.env(?:\b|[.]))", text, re.I):
        return ""
    text = SECRET_VALUE.sub("[REDACTED]", text)
    return "\n".join(text.splitlines()).strip()[:MEMORY_MAX_MESSAGE]


def memory_search(folder: Path, query: str, home: Path | None = None,
                  all_workspaces: bool = False) -> dict:
    """Search local user/assistant messages without a model or network call."""
    folder_key = os.path.normcase(os.path.abspath(str(folder)))
    terms = [term.lower() for term in re.findall(r"\w+", query or "", re.UNICODE)]
    if not terms:
        raise ValueError("memory search query must contain text")
    root = home or COPILOT_HOME
    matches: list[dict] = []
    sessions = []
    for session in root.glob("session-state/*"):
        workspace = _session_workspace(session)
        if not session.is_dir() or (not all_workspaces and workspace != folder_key):
            continue
        log = session / "events.jsonl"
        if not log.exists() or _automated_session(log):
            continue
        try:
            sessions.append((log.stat().st_mtime, session.name, workspace, log))
        except OSError:
            continue
    for _, sid, workspace, log in sorted(sessions, reverse=True):
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        session_matches = []
        for raw in lines:
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            kind = str(record.get("type", "")).lower() if isinstance(record, dict) else ""
            if "user.message" not in kind and "assistant.message" not in kind:
                continue
            text = _safe_memory_text(_memory_message(record))
            if text and all(term in text.lower() for term in terms):
                session_matches.append({"type": "user" if "user.message" in kind else "assistant",
                                        "text": text})
        if session_matches:
            matches.append({"session_id": sid, "workspace": workspace, "matches": session_matches[-5:]})
        if len(matches) >= MEMORY_MAX_MATCHES:
            break
    result = {
        "workspace": str(folder),
        "all_workspaces": all_workspaces,
        "query": query,
        "model_calls": 0,
        "sessions": matches,
    }
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    while len(encoded) > MEMORY_MAX_OUTPUT and result["sessions"]:
        last = result["sessions"][-1]
        if last["matches"]:
            last["matches"].pop(0)
        if not last["matches"]:
            result["sessions"].pop()
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="spendguard", description="Keeps coding-agent AI spend down.")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="facts about a project (JSON)")
    a.add_argument("folder", type=Path)
    a = sub.add_parser("apply", help="generate or refresh the setup from a plan")
    a.add_argument("folder", type=Path)
    a.add_argument("--plan", type=Path, required=True)
    a.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("update", help="re-render from the saved plan")
    a.add_argument("folder", type=Path)
    a.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("remove", help="remove everything Spend-Guard generated")
    a.add_argument("folder", type=Path)
    a.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("report", help="AI cost per project and model")
    a.add_argument("--days", type=int, default=7)
    a = sub.add_parser("tiers", help="show model tiers")
    a.add_argument("--init", action="store_true", help=f"write an editable copy to {TIERS_OVERRIDE}")
    a = sub.add_parser("graph", help="build a local deterministic code graph")
    a.add_argument("folder", type=Path)
    a = sub.add_parser("memory", help="search local same-workspace session memory")
    memory_sub = a.add_subparsers(dest="memory_cmd", required=True)
    s = memory_sub.add_parser("search", help="search approved user/assistant messages without model calls")
    s.add_argument("folder", type=Path)
    s.add_argument("query")
    s.add_argument("--all-workspaces", action="store_true",
                   help="search every identifiable local workspace session")
    d = sub.add_parser("dispatch", help="choose a trusted route without making a model call")
    d.add_argument("folder", type=Path)
    d.add_argument("task")
    d.add_argument("--free-quota-confirmed", action="store_true",
                   help="allow an external route only when its free quota is confirmed")
    v = sub.add_parser("validate", help="audit the local Spend-Guard setup without running project code")
    v.add_argument("folder", type=Path)
    v.add_argument("--strict", action="store_true", help="treat warnings as validation errors")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "analyze":
            print(json.dumps(analyze(args.folder), indent=2))
        elif args.cmd == "apply":
            print("\n".join(apply(args.folder, json.loads(args.plan.read_text(encoding="utf-8-sig")), args.dry_run)))
        elif args.cmd == "update":
            print("\n".join(apply(args.folder, load_plan(args.folder), args.dry_run)))
        elif args.cmd == "remove":
            print("\n".join(remove(args.folder, args.dry_run)) or "nothing to remove")
        elif args.cmd == "report":
            print(report(args.days))
        elif args.cmd == "tiers":
            if args.init:
                TIERS_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
                if not TIERS_OVERRIDE.exists():
                    TIERS_OVERRIDE.write_text(json.dumps(DEFAULT_TIERS, indent=2) + "\n", encoding="utf-8")
                print(f"edit {TIERS_OVERRIDE}, then run `spendguard.py update <folder>` in each project")
            print(json.dumps(load_tiers(), indent=2))
        elif args.cmd == "graph":
            print(json.dumps(build_graph(args.folder), indent=2))
        elif args.cmd == "memory":
            print(json.dumps(memory_search(args.folder, args.query, all_workspaces=args.all_workspaces), indent=2))
        elif args.cmd == "dispatch":
            print(json.dumps(dispatch_task(args.folder, args.task, args.free_quota_confirmed), indent=2))
        elif args.cmd == "validate":
            result = validate_project(args.folder, args.strict)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
