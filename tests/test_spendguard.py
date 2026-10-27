import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "skill" / "spend-guard"))

import spendguard as S  # noqa: E402

PLAN = {
    "project": "Billing API", "main_name": "Billing", "summary": "REST API that issues invoices",
    "routine": ["running tests", "docs", "git"], "test_command": "python -m pytest",
    "avoid_files": ["data/rates.csv"], "rules": ["Never read or print `.env`."],
    "specialists": [
        {"name": "Billing Developer", "role": "developer", "when": "Code changes beyond a one-line fix"},
        {"name": "Billing Security Reviewer", "role": "reviewer", "when": "After a change to auth/"},
        {"name": "Invoice Reader", "role": "extractor", "double_check": True, "when": "Reading scanned invoices"},
    ],
}


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "no frontmatter at the top"
    out = {}
    for line in m.group(1).splitlines():
        assert "\t" not in line
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name) / "proj"
        (self.dir / "src" / "auth").mkdir(parents=True)
        (self.dir / "src" / "auth" / "login.py").write_text("x = 1\n")
        (self.dir / "src" / "app.py").write_text("print(1)\n")
        (self.dir / "tests").mkdir()
        (self.dir / "data").mkdir()
        (self.dir / "data" / "rates.csv").write_text("a," * 150_000)
        (self.dir / ".env").write_text("TOKEN=secret\n")
        (self.dir / "README.md").write_text("# Billing\n")
        self._old = S.TIERS_OVERRIDE
        S.TIERS_OVERRIDE = Path(self.tmp.name) / "no-tiers.json"

    def tearDown(self):
        S.TIERS_OVERRIDE = self._old
        self.tmp.cleanup()

    def read(self, rel):
        return (self.dir / rel).read_text(encoding="utf-8")


class AnalyzeTests(Base):
    def test_facts(self):
        a = S.analyze(self.dir)
        self.assertEqual(a["languages"], {"Python": 2})
        self.assertIn("python -m unittest discover -s tests", a["test_commands"])
        self.assertEqual(a["big_files"][0]["path"], "data/rates.csv")
        self.assertEqual(a["secret_files"], [".env"])
        self.assertEqual(a["sensitive_code"], ["src/auth/login.py"])
        self.assertFalse(a["existing_customizations"]["AGENTS.md"])

    def test_skips_envs_and_bundled_tools_and_finds_secret_values(self):
        venv = self.dir / ".venv-cad" / "Lib" / "site-packages"
        venv.mkdir(parents=True)
        (self.dir / ".venv-cad" / "pyvenv.cfg").write_text("home = x\n")
        (venv / "auth_lib.py").write_text("x\n")
        (self.dir / "tools-env").mkdir()
        (self.dir / "tools-env" / "pyvenv.cfg").write_text("home = x\n")
        (self.dir / "tools-env" / "token.py").write_text("x\n")
        (self.dir / ".cad-engines").mkdir()
        (self.dir / ".cad-engines" / "migration.py").write_text("x\n")
        (self.dir / "config.json").write_text('{"telegram": {"bot_token": "123456:ABCdefGHIjklMNOpqr"}}')
        (self.dir / "settings.json").write_text('{"token": ""}')
        (self.dir / "src" / "big.py").write_text("# x\n" * 20_000)
        a = S.analyze(self.dir)
        self.assertEqual(a["languages"], {"Python": 3})
        self.assertEqual(sorted(a["secret_files"]), [".env", "config.json"])
        self.assertEqual(a["sensitive_code"], ["src/auth/login.py"])
        self.assertEqual(a["big_code_files"][0]["path"], "src/big.py")


class ApplyTests(Base):
    def test_generates_agents_routing_hook_and_settings(self):
        S.apply(self.dir, PLAN)
        agents = sorted(p.name for p in (self.dir / ".github" / "agents").glob("*.agent.md"))
        self.assertEqual(agents, ["billing-developer.agent.md", "billing-security-reviewer.agent.md",
                                  "billing.agent.md", "invoice-reader-checker.agent.md", "invoice-reader.agent.md"])
        main = self.read(".github/agents/billing.agent.md")
        fm = frontmatter(main)
        self.assertIn("GPT-5.6 Luna", fm["model"])
        for name in ("Billing Developer", "Billing Security Reviewer", "Invoice Reader", "Invoice Reader Checker"):
            self.assertIn(f"'{name}'", fm["agents"])
        self.assertIn("Two-pass check for Invoice Reader", main)
        self.assertIn("`data/rates.csv`", main)
        rev = frontmatter(self.read(".github/agents/billing-security-reviewer.agent.md"))
        self.assertIn("Claude Opus", rev["model"])
        self.assertEqual((rev["tools"], rev["user-invocable"], rev["reasoning-effort"]),
                         ("[read, search, execute]", "false", "high"))
        chk = self.read(".github/agents/invoice-reader-checker.agent.md")
        self.assertIn("GPT-6 Astra", frontmatter(chk)["model"])
        self.assertIn("DO NOT read Invoice Reader's results", chk)
        self.assertIn("Claude Sonnet", frontmatter(self.read(".github/agents/billing-developer.agent.md"))["model"])
        agents_md = self.read("AGENTS.md")
        self.assertIn(S.SECTION_START, agents_md)
        self.assertIn("independent second pass", agents_md)
        self.assertIn("`.github/notes.md`", agents_md)
        self.assertIn("`.github/notes.md`", main)
        self.assertTrue((self.dir / ".github" / "notes.md").exists())
        provider_policy = json.loads(self.read(".github/spend-guard-providers.json"))
        self.assertEqual(provider_policy["allowed"], list(S.TRUSTED_PROVIDERS))
        self.assertIn("one primary provider", main)
        self.assertIn('"chat.defaultModel": "gpt-5.6-luna"', self.read(".vscode/settings.json"))
        hook = json.loads(self.read(".github/hooks/spend-guard.json"))
        self.assertIn("userPromptTransformed", hook["hooks"])
        self.assertIn("sessionStart", hook["hooks"])
        self.assertTrue((self.dir / ".github" / "hooks" / "session_start.py").exists())
        saved = json.loads(self.read(".github/spend-guard.json"))
        self.assertEqual(len(saved["specialists"]), 3)            # the checker is derived, not saved

    def test_dispatcher_routes_compatible_tasks_and_restricts_sensitive_providers(self):
        S.apply(self.dir, PLAN)
        routine = S.dispatch_task(self.dir, "update the README")
        self.assertEqual((routine["target"], routine["model"], routine["tier"]),
                         ("copilot", "GPT-5.6 Luna (copilot)", "cheap"))
        self.assertIn("free quota", routine["external_blocked"])
        routine_external = S.dispatch_task(self.dir, "update the README", free_quota_confirmed=True)
        self.assertEqual((routine_external["target"], routine_external["provider"], routine_external["tier"]),
                         ("external", "google", "cheap"))
        security = S.dispatch_task(self.dir, "review authentication and token signing")
        self.assertEqual((security["target"], security["model"], security["tier"]),
                         ("copilot", "Claude Opus 5.5 (copilot)", "accurate"))
        security_external = S.dispatch_task(self.dir, "review authentication and token signing",
                                             free_quota_confirmed=True)
        self.assertEqual((security_external["target"], security_external["provider"], security_external["tier"]),
                         ("external", "anthropic", "accurate"))
        self.assertEqual(security_external["allowed_providers"], ["anthropic", "openai"])
        self.assertEqual(security_external["fallback"]["target"], "copilot")
        self.assertEqual(security_external["cost_mode"], "free_only")
        self.assertEqual(security_external["model_calls"], 0)

    def test_notes_are_seeded_once_and_preserved(self):
        S.apply(self.dir, PLAN)
        notes = self.dir / ".github" / "notes.md"
        seeded = notes.read_text(encoding="utf-8")
        self.assertIn("Append 1-3 lines", seeded)
        notes.write_text(seeded + "\nDecision: keep the handoff short.\n", encoding="utf-8")
        S.apply(self.dir, S.load_plan(self.dir))
        self.assertIn("Decision: keep the handoff short.", notes.read_text(encoding="utf-8"))
        S.remove(self.dir)
        self.assertTrue(notes.exists())
        self.assertIn("Decision: keep the handoff short.", notes.read_text(encoding="utf-8"))

    def test_dry_run_does_not_seed_notes(self):
        S.apply(self.dir, PLAN, dry=True)
        self.assertFalse((self.dir / ".github" / "notes.md").exists())

    def test_generates_claude_skills_and_preserves_claude_md(self):
        plan = dict(PLAN, providers=["copilot", "claude"])
        (self.dir / "CLAUDE.md").write_text("# Local Claude rules\n", encoding="utf-8")
        S.apply(self.dir, plan)
        main = self.read(".claude/skills/spend-guard/SKILL.md")
        developer = self.read(".claude/skills/billing-developer/SKILL.md")
        claude_md = self.read("CLAUDE.md")
        self.assertIn("generated for Claude Code", main)
        self.assertIn("standard-tier developer", developer)
        self.assertIn("# Local Claude rules", claude_md)
        self.assertIn(S.CLAUDE_SECTION_START, claude_md)
        self.assertIn(".claude/skills/spend-guard/SKILL.md", claude_md)
        S.remove(self.dir)
        self.assertEqual(self.read("CLAUDE.md"), "# Local Claude rules\n")
        self.assertFalse((self.dir / ".claude").exists())

    def test_claude_files_are_not_created_by_default(self):
        S.apply(self.dir, PLAN)
        self.assertFalse((self.dir / ".claude").exists())

    def test_idempotent_and_preserves_existing_agents_md(self):
        (self.dir / "AGENTS.md").write_text("# Guide\n\nKeep this.\n", encoding="utf-8")
        S.apply(self.dir, PLAN)
        first = {p: p.read_text(encoding="utf-8") for p in self.dir.rglob("*") if p.is_file()}
        log = S.apply(self.dir, json.loads(self.read(".github/spend-guard.json")))
        self.assertFalse([l for l in log if l.startswith("wrote")], log)
        second = {p: p.read_text(encoding="utf-8") for p in self.dir.rglob("*") if p.is_file()}
        self.assertEqual(first, second)
        text = self.read("AGENTS.md")
        self.assertTrue(text.startswith("# Guide\n\nKeep this.\n"))
        self.assertEqual(text.count(S.SECTION_START), 1)

    def test_never_overwrites_files_it_did_not_create(self):
        (self.dir / ".github" / "agents").mkdir(parents=True)
        mine = self.dir / ".github" / "agents" / "billing.agent.md"
        mine.write_text("my own agent\n", encoding="utf-8")
        log = S.apply(self.dir, PLAN)
        self.assertEqual(mine.read_text(encoding="utf-8"), "my own agent\n")
        self.assertTrue(any(l.startswith("skipped") and "billing.agent.md" in l for l in log))

    def test_settings_merge(self):
        (self.dir / ".vscode").mkdir()
        (self.dir / ".vscode" / "settings.json").write_text('{"editor.tabSize": 2}', encoding="utf-8")
        S.apply(self.dir, PLAN)
        self.assertEqual(json.loads(self.read(".vscode/settings.json")),
                         {"editor.tabSize": 2, "chat.defaultModel": "gpt-5.6-luna"})
        S.remove(self.dir)
        self.assertEqual(json.loads(self.read(".vscode/settings.json")), {"editor.tabSize": 2})

    def test_settings_with_comments_or_own_choice_left_alone(self):
        (self.dir / ".vscode").mkdir()
        p = self.dir / ".vscode" / "settings.json"
        p.write_text('{\n  // mine\n  "chat.defaultModel": "opus"\n}\n', encoding="utf-8")
        log = S.apply(self.dir, PLAN)
        self.assertIn('"opus"', p.read_text(encoding="utf-8"))
        self.assertTrue(any("left" in l and "settings.json" in l for l in log))

    def test_dropped_specialist_is_removed_on_update(self):
        S.apply(self.dir, PLAN)
        plan = json.loads(self.read(".github/spend-guard.json"))
        plan["specialists"] = plan["specialists"][:1]
        S.apply(self.dir, plan)
        agents = sorted(p.name for p in (self.dir / ".github" / "agents").glob("*.agent.md"))
        self.assertEqual(agents, ["billing-developer.agent.md", "billing.agent.md"])

    def test_tier_override_and_update(self):
        S.apply(self.dir, PLAN)
        S.TIERS_OVERRIDE.write_text(json.dumps({"standard": {"models": ["Claude Sonnet 6 (copilot)"]}}))
        S.apply(self.dir, S.load_plan(self.dir))
        self.assertIn("Claude Sonnet 6", self.read(".github/agents/billing-developer.agent.md"))

    def test_remove_cleans_up_only_its_files(self):
        (self.dir / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
        (self.dir / ".github" / "agents").mkdir(parents=True)
        (self.dir / ".github" / "agents" / "mine.agent.md").write_text("keep\n", encoding="utf-8")
        S.apply(self.dir, PLAN)
        S.remove(self.dir)
        self.assertEqual(self.read("AGENTS.md"), "# Guide\n")
        self.assertEqual([p.name for p in (self.dir / ".github" / "agents").glob("*")], ["mine.agent.md"])
        self.assertFalse((self.dir / ".github" / "hooks").exists())
        self.assertFalse((self.dir / ".vscode").exists())
        self.assertFalse((self.dir / ".github" / "spend-guard.json").exists())
        self.assertFalse((self.dir / ".github" / "spend-guard-providers.json").exists())

    def test_dry_run_writes_nothing(self):
        S.apply(self.dir, PLAN, dry=True)
        self.assertFalse((self.dir / ".github").exists())
        self.assertFalse((self.dir / "AGENTS.md").exists())

    def test_bad_plans_rejected(self):
        for bad in ({"specialists": [{"name": "X"}]},
                    {"specialists": [{"name": "X", "when": "w", "role": "wizard"}]},
                    {"specialists": [{"name": "X", "when": "w", "tier": "gold"}]},
                    {"providers": ["copilot", "unknown"]},
                    {"provider_policy": {"allowed": ["unknown"]}},
                    {"provider_policy": {"routing": {"cheap": ["unknown"]}}},
                    {"provider_policy": {"dispatcher": {"external_mode": "always"}}},
                    {"provider_policy": {"dispatcher": {"external_cost_mode": "paid"}}},
                    {"main_name": "X", "specialists": [{"name": "X", "when": "w"}]}):
            with self.assertRaises(ValueError):
                S.apply(self.dir, bad, dry=True)

    def test_quotes_in_text_keep_frontmatter_valid(self):
        plan = dict(PLAN, specialists=[{"name": "Dev", "role": "developer", "when": 'Edits to "core" module'}])
        S.apply(self.dir, plan)
        fm = frontmatter(self.read(".github/agents/dev.agent.md"))
        self.assertEqual(fm["description"], '"Edits to \\"core\\" module. Used by the main Billing agent."')


class GuardTests(Base):
    def run_guard(self, payload, tokens, home):
        sid = "s1"
        d = home / "session-state" / sid
        d.mkdir(parents=True, exist_ok=True)
        rec = {"type": "session.usage_record", "data": {"usage": {"inputTokens": tokens, "initiator": "user"}}}
        (d / "events.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
        env = dict(os.environ, COPILOT_HOME=str(home))
        out = subprocess.run([sys.executable, str(self.dir / ".github" / "hooks" / "spend_guard.py")],
                             input=json.dumps({"sessionId": sid, **payload}).encode(), capture_output=True,
                             env=env, timeout=20)
        return out.stdout.decode()

    def test_guard_quiet_then_notes_then_fails_open(self):
        S.apply(self.dir, PLAN)
        home = Path(self.tmp.name) / "home"
        self.assertEqual(self.run_guard({"transformedPrompt": "hi"}, 5_000, home), "{}")
        out = json.loads(self.run_guard({"transformedPrompt": "add a feature"}, 250_000, home))
        self.assertTrue(out["modifiedTransformedPrompt"].startswith("add a feature\n\n[Spend-Guard"))
        self.assertIn("~250k", out["modifiedTransformedPrompt"])
        self.assertIn('"gpt-5.6-luna"', out["modifiedTransformedPrompt"])
        env = dict(os.environ, COPILOT_HOME=str(home))
        bad = subprocess.run([sys.executable, str(self.dir / ".github" / "hooks" / "spend_guard.py")],
                             input=b"not json", capture_output=True, env=env, timeout=20)
        self.assertEqual(bad.stdout, b"{}")

    def run_session_start(self, payload, home):
        out = subprocess.run([sys.executable, str(self.dir / ".github" / "hooks" / "session_start.py")],
                             input=json.dumps(payload).encode(), capture_output=True,
                             env=dict(os.environ, COPILOT_HOME=str(home)), timeout=20)
        return out.stdout.decode()

    def test_session_start_uses_previous_same_workspace_and_caps(self):
        S.apply(self.dir, PLAN)
        home = Path(self.tmp.name) / "home"
        previous = home / "session-state" / "previous"
        previous.mkdir(parents=True)
        (previous / "workspace.yaml").write_text(f"cwd: {self.dir}\n", encoding="utf-8")
        events = [
            {"type": "user.message", "content": f"work item {i} " + ("x" * 1000)} for i in range(10)
        ]
        events.append({"type": "assistant.message", "content": "Finished the relevant feature."})
        (previous / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        other = home / "session-state" / "other"
        other.mkdir(parents=True)
        (other / "workspace.yaml").write_text(f"cwd: {self.tmp.name}\\other\n", encoding="utf-8")
        (other / "events.jsonl").write_text(json.dumps({"type": "user.message", "content": "wrong workspace"}),
                                            encoding="utf-8")
        out = json.loads(self.run_session_start({"sessionId": "current", "cwd": str(self.dir)}, home))
        self.assertLessEqual(len(out["additionalContext"]), 6000)
        self.assertIn("Previous chat: previous", out["additionalContext"])
        self.assertIn("Finished the relevant feature.", out["additionalContext"])
        self.assertNotIn("wrong workspace", out["additionalContext"])

    def test_session_start_fails_open_on_bad_input_or_missing_logs(self):
        S.apply(self.dir, PLAN)
        home = Path(self.tmp.name) / "home"
        self.assertEqual(self.run_session_start({}, home), "{}")
        out = json.loads(self.run_session_start({"sessionId": "current", "cwd": str(self.dir)}, home))
        self.assertIn("Project notes", out["additionalContext"])

    def test_session_start_includes_capped_local_graph_report(self):
        S.apply(self.dir, PLAN)
        S.build_graph(self.dir)
        home = Path(self.tmp.name) / "home"
        out = json.loads(self.run_session_start({"sessionId": "current", "cwd": str(self.dir)}, home))
        self.assertIn("Local code graph summary", out["additionalContext"])
        self.assertLessEqual(len(out["additionalContext"]), 6000)


class GraphTests(Base):
    def test_graph_is_local_deterministic_and_excludes_secrets_and_large_files(self):
        (self.dir / "src" / "app.py").write_text(
            "from auth.login import check\n\nclass Service:\n    pass\n", encoding="utf-8")
        (self.dir / "src" / "ignored.py").write_text("password = 'not-a-real-secret'\n", encoding="utf-8")
        result = S.build_graph(self.dir)
        graph = json.loads(self.read(".github/spend-guard-graph/graph.json"))
        self.assertEqual(result["files"], 3)
        self.assertIn("file:src/app.py", {n["id"] for n in graph["nodes"]})
        self.assertIn("file:src/auth/login.py", {n["id"] for n in graph["nodes"]})
        self.assertNotIn("file:.env", {n["id"] for n in graph["nodes"]})
        self.assertTrue(all(e["evidence"] == "EXTRACTED" for e in graph["edges"]))
        self.assertIn("no network or model calls", self.read(".github/spend-guard-graph/GRAPH_REPORT.md"))

    def test_graph_reuses_unchanged_file_cache_and_removes_deleted_files(self):
        first = S.build_graph(self.dir)
        graph_path = self.dir / ".github" / "spend-guard-graph" / "graph.json"
        before = graph_path.read_text(encoding="utf-8")
        second = S.build_graph(self.dir)
        self.assertEqual(first, second)
        self.assertEqual(before, graph_path.read_text(encoding="utf-8"))
        (self.dir / "src" / "app.py").unlink()
        S.build_graph(self.dir)
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        self.assertNotIn("src/app.py", graph["files"])


class ReportTests(Base):
    def test_report_compares_periods(self):
        root = Path(self.tmp.name) / "ss"
        now = dt.datetime(2026, 11, 2, tzinfo=dt.timezone.utc)

        def add(sid, cwd, when, credits, tokens, model="Claude Opus 5.5"):
            d = root / sid
            d.mkdir(parents=True, exist_ok=True)
            (d / "workspace.yaml").write_text(f"cwd: {cwd}\n")
            rec = {"type": "session.usage_record", "timestamp": when.isoformat().replace("+00:00", "Z"),
                   "data": {"usage": {"modelDisplayName": model, "inputTokens": tokens, "initiator": "user",
                                      "copilotUsage": {"totalNanoAiu": credits * 1e9}}}}
            with open(d / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")

        add("a1", "C:/p/Alpha", now - dt.timedelta(days=10), 1000, 300_000)
        add("a2", "C:/p/Alpha", now - dt.timedelta(days=2), 200, 20_000, "GPT-5.6 Luna")
        add("b1", "C:/p/Beta.worktrees/x", now - dt.timedelta(days=1), 50, 150_000)
        text = S.report(7, root=root, now=now)
        self.assertIn("Total: 250 credits", text)
        self.assertIn("previous 7 days: 1,000", text)
        self.assertIn("change: -75%", text)
        self.assertIn("Beta", text)
        self.assertIn("Chats past 100k tokens of context: 1", text)


class MemoryTests(Base):
    def test_memory_search_is_workspace_scoped_redacted_and_local(self):
        home = Path(self.tmp.name) / "home"

        def add(sid, cwd, records, automated=False):
            d = home / "session-state" / sid
            d.mkdir(parents=True)
            (d / "workspace.yaml").write_text(f"cwd: {cwd}\n", encoding="utf-8")
            if automated:
                records = [{"type": "session.usage_record",
                            "data": {"usage": {"initiator": "sub-agent"}}}] + records
            (d / "events.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8")

        add("same", self.dir, [
            {"type": "user.message", "content": "Decided to keep the auth handoff local."},
            {"type": "assistant.message", "content": "token=abc1234567890 decision recorded"},
        ])
        add("wrong", self.tmp.name, [{"type": "user.message", "content": "auth handoff wrong workspace"}])
        add("bot", self.dir, [{"type": "user.message", "content": "auth handoff automation"}], automated=True)
        result = S.memory_search(self.dir, "auth handoff", home=home)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual([s["session_id"] for s in result["sessions"]], ["same"])
        text = json.dumps(result)
        self.assertIn("keep the auth handoff local", text)
        self.assertNotIn("abc1234567890", text)
        self.assertNotIn("wrong workspace", text)
        self.assertNotIn("automation", text)

    def test_memory_search_caps_and_rejects_empty_query(self):
        home = Path(self.tmp.name) / "home"
        d = home / "session-state" / "long"
        d.mkdir(parents=True)
        (d / "workspace.yaml").write_text(f"cwd: {self.dir}\n", encoding="utf-8")
        records = [{"type": "user.message", "content": "decision " + ("x" * 1000)}
                   for _ in range(30)]
        (d / "events.jsonl").write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
        result = S.memory_search(self.dir, "decision", home=home)
        self.assertLessEqual(len(json.dumps(result, separators=(",", ":"))), S.MEMORY_MAX_OUTPUT)
        with self.assertRaises(ValueError):
            S.memory_search(self.dir, "   ", home=home)

    def test_memory_search_can_explicitly_cross_workspace(self):
        home = Path(self.tmp.name) / "home"
        other = Path(self.tmp.name) / "other-project"
        d = home / "session-state" / "other-session"
        d.mkdir(parents=True)
        (d / "workspace.yaml").write_text(f"cwd: {other}\n", encoding="utf-8")
        (d / "events.jsonl").write_text(
            json.dumps({"type": "user.message", "content": "shared routing decision"}), encoding="utf-8")
        local = S.memory_search(self.dir, "shared routing", home=home)
        self.assertEqual(local["sessions"], [])
        cross = S.memory_search(self.dir, "shared routing", home=home, all_workspaces=True)
        self.assertTrue(cross["all_workspaces"])
        self.assertEqual(cross["sessions"][0]["workspace"], os.path.normcase(os.path.abspath(str(other))))
        self.assertIn("shared routing decision", json.dumps(cross))


class CliTests(Base):
    def test_cli_round_trip(self):
        script = HERE / "skill" / "spend-guard" / "spendguard.py"
        plan = Path(self.tmp.name) / "plan.json"
        plan.write_bytes(b"\xef\xbb\xbf" + json.dumps(PLAN).encode())   # BOM, as PowerShell writes it
        env = dict(os.environ, COPILOT_HOME=str(Path(self.tmp.name) / "home"))
        run = lambda *a: subprocess.run([sys.executable, str(script), *a], capture_output=True, text=True,  # noqa: E731
                                        env=env, timeout=60)
        a = run("analyze", str(self.dir))
        self.assertEqual(json.loads(a.stdout)["name"], "proj")
        self.assertEqual(run("apply", str(self.dir), "--plan", str(plan)).returncode, 0)
        self.assertEqual(run("update", str(self.dir)).returncode, 0)
        bad = run("update", str(Path(self.tmp.name)))
        self.assertEqual(bad.returncode, 2)
        self.assertIn("no Spend-Guard plan", bad.stderr)
        self.assertEqual(run("remove", str(self.dir)).returncode, 0)
        self.assertEqual(run("tiers").returncode, 0)


if __name__ == "__main__":
    unittest.main()
