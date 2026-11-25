"""The parity matrix: every documented feature of OpenCode and Hermes Agent, and where it stands here.

status   native   = built in, and the named test proves the mechanism
         bridged  = reached by handing the job to the installed tool (talos engine ...) or by a
                    standard hook (MCP, plugin); not built in
         untested = built in, but no test exercises it yet (treat as unproven)
         missing  = not available
proof    test     = unit test in tests/, no network (platform APIs are local stand-ins)
         live     = also run for real on a PC (see README, "What was checked")
"""
from __future__ import annotations

# (source, feature, status, how, test)
ROWS = [
    # ---- OpenCode
    ("opencode", "Terminal chat (TUI)", "native", "talos (line-based chat, slash commands; not a full-screen TUI)", "test_cli.ReplTest.test_slash_commands"),
    ("opencode", "Desktop app", "native", "talos app (own window, Start menu shortcut)", "test_cli.ShortcutTest.test_shortcut_script"),
    ("opencode", "Web UI", "native", "talos web", "test_server.ServerTest.test_page_and_state"),
    ("opencode", "run (one-shot, JSON output)", "native", "talos run", "test_cli.CliTest.test_run_json"),
    ("opencode", "serve (HTTP API + live events)", "native", "talos serve", "test_server.ServerTest.test_prompt_events_undo"),
    ("opencode", "SDK", "bridged", "plain HTTP + the OpenAI-compatible endpoint; no typed SDK package", ""),
    ("opencode", "Tools: read write edit patch", "native", "", "test_tools.FileToolsTest.test_edit_and_patch"),
    ("opencode", "Tools: grep glob list", "native", "", "test_tools.FileToolsTest.test_search_tools"),
    ("opencode", "Tool: bash (+ background processes)", "native", "PowerShell on Windows, sh elsewhere", "test_tools.ShellTest.test_bash_and_background"),
    ("opencode", "Tools: todowrite todoread", "native", "", "test_tools.MiscToolsTest.test_todos_and_question"),
    ("opencode", "Tools: webfetch websearch", "native", "search through DuckDuckGo HTML or a SearXNG URL", "test_tools.WebTest.test_fetch_and_search"),
    ("opencode", "Tool: question", "native", "", "test_tools.MiscToolsTest.test_todos_and_question"),
    ("opencode", "Tool: task (subagents)", "native", "up to 3 helpers at once", "test_agent.AgentTest.test_helpers_run_in_parallel"),
    ("opencode", "Tool: skill", "native", "", "test_skills.SkillsTest.test_discovery_across_tools"),
    ("opencode", "Tool: lsp + diagnostics after edits", "native", "built-in checks for py/json/js; any LSP server from the config", "test_lsp.LspTest.test_server_diagnostics"),
    ("opencode", "Formatters", "native", "runs the formatter found on the PC after each edit", "test_lsp.LspTest.test_formatter_runs"),
    ("opencode", "Agents: build, plan, general, explore", "native", "", "test_agent.AgentTest.test_plan_agent_cannot_edit"),
    ("opencode", "Custom agents (Markdown / JSON)", "native", "reads .opencode/agent in place", "test_plugins.PluginsTest.test_custom_agent_and_command"),
    ("opencode", "Custom commands", "native", "$ARGUMENTS, $1, !`cmd`, @file", "test_plugins.PluginsTest.test_custom_agent_and_command"),
    ("opencode", "Permissions (allow / ask / deny, patterns)", "native", "", "test_permissions.PermissionsTest.test_rules"),
    ("opencode", "Rules: AGENTS.md, CLAUDE.md, instructions", "native", "", "test_agent.AgentTest.test_rule_files_and_soul"),
    ("opencode", "MCP servers, local", "native", "", "test_mcp.McpTest.test_local_server"),
    ("opencode", "MCP servers, remote (HTTP / SSE)", "native", "headers and bearer tokens", "test_mcp.McpTest.test_remote_server"),
    ("opencode", "MCP OAuth login flow", "missing", "use a token in headers instead", ""),
    ("opencode", "Plugins and hooks", "native", "Python files in .talos/plugin; OpenCode's JS plugins are not loaded", "test_plugins.PluginsTest.test_hooks_and_custom_tool"),
    ("opencode", "Custom tools", "native", "", "test_plugins.PluginsTest.test_hooks_and_custom_tool"),
    ("opencode", "Sessions: list, continue, delete", "native", "", "test_store.StoreTest.test_sessions"),
    ("opencode", "Undo / redo", "native", "restores files changed by edit tools, not by shell commands", "test_store.StoreTest.test_undo_redo"),
    ("opencode", "Share", "native", "one self-contained HTML file, secrets blanked; no hosted link", "test_store.StoreTest.test_share_and_export"),
    ("opencode", "Export / import", "native", "", "test_store.StoreTest.test_share_and_export"),
    ("opencode", "Compaction", "native", "", "test_agent.AgentTest.test_compaction"),
    ("opencode", "Stats (tokens, cost)", "native", "", "test_store.StoreTest.test_sessions"),
    ("opencode", "Models from any provider", "native", "any OpenAI-compatible API; 13 presets", "test_config.ConfigTest.test_model_resolution"),
    ("opencode", "Reads opencode.json(c)", "native", "", "test_config.ConfigTest.test_reads_opencode_and_hermes"),
    ("opencode", "Images as input", "native", "the read tool attaches images", "test_tools.FileToolsTest.test_read_image"),
    ("opencode", "Image generation tool", "native", "any OpenAI-style images API", "test_tools.MediaTest.test_image_generate"),
    ("opencode", "ACP (editor protocol)", "native", "talos acp; core methods only", "test_acp.AcpTest.test_prompt_round_trip"),
    ("opencode", "GitHub / GitLab workflow", "native", "talos github install writes the workflow; not run on GitHub in tests", "test_cli.CliTest.test_github_workflow"),
    ("opencode", "Themes", "native", "light and dark in the app", "test_server.ServerTest.test_page_and_state"),
    ("opencode", "Keybinds (configurable)", "missing", "fixed keys in the app", ""),
    ("opencode", "IDE extensions", "bridged", "through ACP in editors that speak it", ""),
    ("opencode", "Hosted share links, Zen, pair, service", "bridged", "talos engine opencode", ""),
    # ---- Hermes Agent
    ("hermes", "Terminal chat", "native", "", "test_cli.ReplTest.test_slash_commands"),
    ("hermes", "Memory: MEMORY.md and USER.md", "native", "same limits and separator as Hermes", "test_skills.MemoryTest.test_memory_limits"),
    ("hermes", "External memory providers (Honcho, Mem0 ...)", "bridged", "through MCP or a plugin", ""),
    ("hermes", "Skills (agentskills.io format)", "native", "reads ~/.hermes/skills in place", "test_skills.SkillsTest.test_discovery_across_tools"),
    ("hermes", "Self-improving skills", "native", "skill_manage after long runs, scanned before saving", "test_agent.AgentTest.test_learns_a_skill"),
    ("hermes", "Skills hub", "bridged", "talos engine hermes; or copy a skill folder", ""),
    ("hermes", "Session search (full text)", "native", "SQLite FTS5", "test_store.StoreTest.test_search"),
    ("hermes", "Scheduled jobs (cron)", "native", "", "test_cron.CronTest.test_job_runs_and_reschedules"),
    ("hermes", "Subagent delegation", "native", "", "test_agent.AgentTest.test_helpers_run_in_parallel"),
    ("hermes", "execute_code (script that calls tools)", "native", "", "test_tools.ShellTest.test_execute_code_calls_tools"),
    ("hermes", "Gateway: Telegram", "native", "", "test_gateway.GatewayTest.test_telegram_round_trip"),
    ("hermes", "Gateway: Discord", "native", "polls channels over REST (no live socket)", "test_gateway.GatewayTest.test_other_platforms"),
    ("hermes", "Gateway: Slack", "native", "", "test_gateway.GatewayTest.test_other_platforms"),
    ("hermes", "Gateway: Mattermost", "native", "", "test_gateway.GatewayTest.test_other_platforms"),
    ("hermes", "Gateway: Matrix", "native", "", "test_gateway.GatewayTest.test_other_platforms"),
    ("hermes", "Gateway: ntfy", "native", "", "test_gateway.GatewayTest.test_other_platforms"),
    ("hermes", "Gateway: Signal", "native", "needs signal-cli's REST service", "test_gateway.GatewayTest.test_other_platforms"),
    ("hermes", "Gateway: Email", "untested", "IMAP + SMTP; written, never run against a mail server", ""),
    ("hermes", "Gateway: IRC", "untested", "written, never run against an IRC server", ""),
    ("hermes", "Gateway: any system by webhook", "native", "POST /hook/<name>", "test_server.ServerTest.test_webhook"),
    ("hermes", "Gateway: WhatsApp, SMS, Home Assistant, DingTalk, Feishu, WeCom, Weixin, BlueBubbles, QQ, Yuanbao, Teams, LINE, Raft, SimpleX, Buzz", "bridged", "talos engine hermes, or the webhook route", ""),
    ("hermes", "Approvals in chat, allowed users", "native", "", "test_gateway.GatewayTest.test_approval_in_chat"),
    ("hermes", "Terminal backends: local, Docker, SSH, Singularity", "native", "command lines are tested; Docker/SSH not run in tests", "test_backends.BackendsTest.test_command_lines"),
    ("hermes", "Terminal backends: Daytona, Modal, Vercel Sandbox", "bridged", "talos engine hermes", ""),
    ("hermes", "Browser automation", "bridged", "a browser MCP server (for example Playwright MCP)", ""),
    ("hermes", "Vision", "native", "images go to the model as attachments", "test_tools.FileToolsTest.test_read_image"),
    ("hermes", "Image generation", "native", "", "test_tools.MediaTest.test_image_generate"),
    ("hermes", "Text to speech", "native", "the system's own voice", "test_tools.MediaTest.test_speech_command"),
    ("hermes", "Voice input (speech to text)", "missing", "", ""),
    ("hermes", "MCP", "native", "reads mcp_servers from Hermes' config.yaml", "test_config.ConfigTest.test_reads_opencode_and_hermes"),
    ("hermes", "Context files: AGENTS.md, SOUL.md, .cursorrules", "native", "", "test_agent.AgentTest.test_rule_files_and_soul"),
    ("hermes", "Personalities", "native", "", "test_agent.AgentTest.test_rule_files_and_soul"),
    ("hermes", "Checkpoints and /rollback", "native", "/undo and /redo per turn", "test_store.StoreTest.test_undo_redo"),
    ("hermes", "Hooks and plugins", "native", "", "test_plugins.PluginsTest.test_hooks_and_custom_tool"),
    ("hermes", "OpenAI-compatible API server", "native", "/v1/chat/completions, streaming too", "test_server.ServerTest.test_openai_endpoint"),
    ("hermes", "Batch runs + trajectory export", "native", "talos batch", "test_cli.CliTest.test_batch"),
    ("hermes", "Atropos RL environments", "missing", "research tooling, out of scope", ""),
    ("hermes", "Migration from other agents", "native", "reads OpenCode, Hermes and Claude files where they are; talos migrate copies Hermes memory", "test_cli.CliTest.test_migrate"),
    ("hermes", "Context compression", "native", "", "test_agent.AgentTest.test_compaction"),
    # ---- Talos's own
    ("talos", "Proof gate: every run that changed files says checked / re-read only / unverified / check failed", "native", "", "test_agent.AgentTest.test_proof_levels"),
    ("talos", "Budget hard stops (steps, tokens, cost)", "native", "", "test_agent.AgentTest.test_budget_stops"),
    ("talos", "Tool calling for models that have none (text protocol, automatic)", "native", "", "test_llm.LlmTest.test_text_protocol_fallback"),
    ("talos", "Guard: skills, memory, rule files and MCP tool descriptions are scanned before use", "native", "", "test_skills.SkillsTest.test_guard_blocks_and_trust"),
    ("talos", "One setup for both: reads OpenCode and Hermes config, skills, agents, commands in place", "native", "", "test_config.ConfigTest.test_reads_opencode_and_hermes"),
    ("talos", "Engines: hand a job to OpenCode, Hermes, Claude Code, Codex or Gemini from one place", "native", "", "test_engines.EnginesTest.test_runs_an_engine"),
    ("talos", "Safe by default: risky commands and writes outside the project ask first", "native", "", "test_permissions.PermissionsTest.test_dangerous_and_outside"),
    ("talos", "Secrets blanked before a session is shared", "native", "", "test_store.StoreTest.test_share_and_export"),
    ("talos", "Loop breaker: the same failing call three times is stopped", "native", "", "test_agent.AgentTest.test_loop_breaker"),
    ("talos", "Demo with no model, no key, no network", "native", "talos demo", "test_cli.CliTest.test_demo"),
    ("talos", "Tune: measures your models on fixed tasks and recommends one", "native", "", "test_tune.TuneTest.test_scores_models"),
    ("talos", "Local server refuses other web pages (Origin and Host checks)", "native", "", "test_server.ServerTest.test_refuses_other_origins"),
    ("talos", "Fits small local models: reads the model's window size and sends a short prompt with the core tools", "native", "", "test_agent.AgentTest.test_small_window_gets_short_prompt"),
    ("talos", "Knows your notes: searches your Obsidian or Cairn folder where it is (notes tool, Knowledge page)", "native", "", "test_knowledge.KnowledgeTest.test_agent_looks_things_up"),
    ("talos", "Knows your projects: your GitHub repositories and local project folders (projects tool)", "native", "", "test_knowledge.KnowledgeTest.test_projects_from_github_and_local_folders"),
    ("talos", "Reads skills from Codex and Vector folders too, plus any folder you name; hides skills by name", "native", "", "test_knowledge.KnowledgeTest.test_more_skill_folders_and_hidden_names"),
    ("talos", "Small models are pushed to act: code shown without a tool call gets one reminder to do the work", "native", "", "test_agent.AgentTest.test_small_model_is_pushed_to_act"),
    ("talos", "App window with its own taskbar logo, and no console windows popping up behind it", "native", "", "test_cli.NoConsoleTest.test_app_does_not_open_console_windows"),
    ("talos", "Unreadable tool calls are sent back with the reason instead of ending the run", "native", "", "test_agent.AgentTest.test_unreadable_tool_call_is_sent_back"),
    ("talos", "Talos Relay: start, follow, answer and stop its chats from a phone page; the relay in between sees only locked text", "native", "its own page and relay (relay/); AES-256-GCM, key only in the QR code; several computers, a 4-digit code, photos, one list of all chats", "test_relay.RelayTest.test_phone_starts_a_chat_and_allows_a_step"),
    ("talos", "Sign in to OpenRouter in the browser: no key to copy (auth login openrouter)", "native", "", "test_relay.SignInTest.test_openrouter_sign_in"),
    ("talos", "No dependencies: Python standard library only", "native", "", "test_parity.ParityTest.test_standard_library_only"),
]

# rows that were also run for real (a real local model, the real app window, real notes), not only in tests
LIVE = {
    "run (one-shot, JSON output)", "Desktop app", "Web UI",
    "Tool calling for models that have none (text protocol, automatic)",
    "Proof gate: every run that changed files says checked / re-read only / unverified / check failed",
    "Fits small local models: reads the model's window size and sends a short prompt with the core tools",
    "Small models are pushed to act: code shown without a tool call gets one reminder to do the work",
    "Knows your notes: searches your Obsidian or Cairn folder where it is (notes tool, Knowledge page)",
    "Knows your projects: your GitHub repositories and local project folders (projects tool)",
    "App window with its own taskbar logo, and no console windows popping up behind it",
}


def rows() -> list:
    return [{"source": source, "feature": feature, "status": status, "how": how, "test": test,
             "proof": ("live + test" if feature in LIVE else "test") if status == "native" else ""}
            for source, feature, status, how, test in ROWS]


def counts() -> dict:
    result = {}
    for row in rows():
        group = result.setdefault(row["source"], {"native": 0, "bridged": 0, "untested": 0, "missing": 0})
        group[row["status"]] += 1
    return result


def markdown() -> str:
    names = {"opencode": "OpenCode", "hermes": "Hermes Agent", "talos": "Only in Talos"}
    lines = ["# Parity", "",
             "Generated by `talos parity --write` from `talos/parity.py`. A test (`tests/test_parity.py`)",
             "fails if a row marked native does not name an existing test.", "",
             "- **native**: built in. **bridged**: reached through the installed tool (`talos engine ...`),",
             "  MCP or a plugin. **untested**: built in but no test runs it. **missing**: not available.",
             "- Proof `test` means a unit test with local stand-ins (no real Telegram, Slack and so on).",
             "  `live + test` means it was also run for real; see README, \"What was checked\".", ""]
    totals = counts()
    for source in ("opencode", "hermes", "talos"):
        group = totals[source]
        lines += [f"## {names[source]}", "",
                  f"{group['native']} native, {group['bridged']} bridged, {group['untested']} untested, "
                  f"{group['missing']} missing.", "",
                  "| Feature | Status | Notes | Proof |", "|---|---|---|---|"]
        for row in rows():
            if row["source"] == source:
                proof = f"{row['proof']}: `{row['test']}`" if row["test"] else ""
                lines.append(f"| {row['feature']} | {row['status']} | {row['how']} | {proof} |")
        lines.append("")
    return "\n".join(lines)
