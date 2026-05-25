# Competitive Teardown: Hermes Agent and OpenClaw for Hikari

Date: 2026-05-25
Repository inspected: `/Users/ol/agents/hikari-agent`

## 1. Executive summary

Hermes Agent and OpenClaw are converging on the same product shape: a local-first personal agent runtime with messaging channels, tool registries, memory, skills, scheduling, background work, and safety controls. Hermes emphasizes a "self-improving" loop, skill creation and skill marketplaces, many terminal backends, and a broad messaging gateway. OpenClaw emphasizes channel routing, a gateway/control UI, per-agent workspaces, multi-agent background tasks, explicit sandbox/tool-policy/elevated controls, and a very wide provider/plugin surface.

Hikari is not behind in the core agent idea. Locally, it already has a durable Telegram bridge, Claude Agent SDK session management, a split between visible turns and internal control turns, a gatekeeper approval state machine, untrusted-output wrapping, rich personal tools, proactive scheduling, memory with validity/provenance, and a test suite around those invariants. Hikari's edge is intimacy plus operational discipline: it is built as one person's companion with careful final-sent persistence, not as a generic agent OS.

The strongest competitive gaps are product visibility and operator UX. Hermes and OpenClaw make their capabilities, commands, tools, jobs, skills, models, channels, and sandboxes visible to the user through commands, dashboards, and setup flows. Hikari has much of the machinery, but it is mostly implicit or developer-facing. The most valuable backlog is therefore not "more tools"; it is a small Hikari-native cockpit for capabilities, approvals, background jobs, proactive sources, memory review, and tool health.

Be careful copying hype. Hermes' official README and docs make expansive claims about self-improvement, agent-curated memory, autonomous skill creation, many messaging platforms, and terminal backends. The official docs substantiate a real skills/memory/tooling/control-plane system, but independent evidence for durable autonomous improvement quality is limited. OpenClaw's official docs substantiate a serious gateway, channel, skills, automation, and sandbox-policy design, but secondary coverage focuses heavily on security risks around skills, system access, and prompt injection. Hikari should copy the inspectability and policy clarity, not the sprawl.

## 2. Source list with official vs secondary labels

Official / primary sources:

- Hermes Agent GitHub README: [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent). Used for product positioning, install commands, messaging platforms, model/provider claims, terminal backends, self-improvement claims, skill loop claims, scheduling claims, delegation claims, and repository shape.
- Hermes Agent docs home: [Hermes Agent Documentation](https://hermes-agent.nousresearch.com/docs). Used for official feature list and quick links to tools, memory, skills, MCP, messaging, security, and architecture.
- Hermes tools/toolsets docs: [Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools). Used for tool categories, toolsets, terminal backends, background process management, and container-hardening claims.
- Hermes memory docs: [Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory). Used for MEMORY.md / USER.md, capacity limits, prompt injection model, and memory tool semantics.
- Hermes skills docs: [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills). Used for skill locations, skill bundles, skill hub sources, install/audit/update commands, and marketplace integrations.
- Hermes messaging docs: [Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging). Used for gateway architecture, cron tick inside gateway, setup commands, and chat commands.
- Hermes cron docs: [Scheduled Tasks (Cron)](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron). Used for one-shot/recurring jobs, skill-backed cron, lifecycle actions, delivery targets, isolated sessions, and no-agent mode.
- Hermes delegation docs: [Subagent Delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation). Used for child agents, isolated context, restricted toolsets, parallel batches, monitoring, durability limits, and delegation-vs-code-execution tradeoffs.
- Hermes security docs: [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security). Used for command approval, gateway authorization, pairing, and container isolation.
- OpenClaw GitHub README: [openclaw/openclaw](https://github.com/openclaw/openclaw). Used for product positioning, security model, operator quick refs, optional apps, workspace/skills layout, and development setup.
- OpenClaw getting started docs: [Getting started](https://docs.openclaw.ai/start/getting-started). Used for install, onboarding, gateway status, dashboard, first message, model provider setup, and Telegram-first path.
- OpenClaw channels docs: [Chat channels](https://docs.openclaw.ai/channels). Used for supported channels, gateway connection model, delivery notes, ambient room events, and bot-loop protection.
- OpenClaw tools overview: [Capabilities overview](https://docs.openclaw.ai/tools). Used for distinctions between tools, skills, plugins, automation, sub-agents, and tool visibility filtering.
- OpenClaw skills docs: [Skills](https://docs.openclaw.ai/tools/skills). Used for AgentSkills-compatible folders, skill roots and precedence, per-agent allowlists, Skill Workshop, ClawHub installs, and admin upload path.
- OpenClaw sub-agents docs: [Sub-agents](https://docs.openclaw.ai/tools/subagents). Used for background sub-agent runs, session isolation, tracking as background tasks, model/cost settings, and `sessions_spawn` behavior.
- OpenClaw automation docs: [Automation](https://docs.openclaw.ai/automation). Used for cron, heartbeat, tasks, inferred commitments, task flow, standing orders, hooks, and auditability.
- OpenClaw sandbox/policy/elevated docs: [Sandbox vs tool policy vs elevated](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated). Used for the three-layer security model, sandbox modes, bind mount cautions, tool policy precedence, and `/exec` limits.
- OpenClaw provider docs: [Provider directory](https://docs.openclaw.ai/providers). Used for provider breadth and provider/model configuration.
- OpenClaw memory docs: [Memory overview](https://docs.openclaw.ai/concepts/memory). Used for Markdown memory files, daily notes, DREAMS.md, bootstrap truncation, and memory search.
- OpenClaw slash commands docs: [Slash commands](https://docs.openclaw.ai/tools/slash-commands). Used for command/directive distinction, authorization behavior, and command categories.

Secondary sources:

- Tom's Guide, hands-on OpenClaw article: [I let a viral AI agent take over my PC](https://www.tomsguide.com/ai/i-tested-the-viral-ai-agent-that-could-replace-apps-and-it-made-me-appreciate-my-computer-without-it). Secondary product/UX framing; used only as outside perception, not as proof of OpenClaw internals.
- TechRadar, OpenClaw skills explainer: [What are OpenClaw Skills? A detailed guide](https://www.techradar.com/pro/what-are-openclaw-skills-a-detailed-guide). Secondary framing of skills and risk; security claims from this article are marked secondary unless verified in OpenClaw docs.
- TechRadar, OpenClaw security overview: [Here are the OpenClaw security risks you should know about](https://www.techradar.com/pro/here-are-the-openclaw-security-risks-you-should-know-about). Secondary security framing; used to understand market concern around prompt injection and skill supply chain, not as official fact.
- McAfee, OpenClaw safety guide: [Is OpenClaw Safe? 2026 Guide to AI Agent Security Risks](https://www.mcafee.com/learn/is-openclaw-safe-to-install/). Secondary security framing; used only to triangulate risk perception around deep system access and third-party skills.
- PCWorld, OpenClaw warning article: [OpenClaw AI is going viral. Don't install it](https://www.pcworld.com/article/3064874/openclaw-ai-is-going-viral-dont-install-it.html). Secondary security/consumer framing; used only to capture external risk perception.
- arXiv, ClawGuard: [ClawGuard: A Runtime Security Framework for Tool-Augmented LLM Agents Against Indirect Prompt Injection](https://arxiv.org/abs/2604.11790). Secondary academic context for prompt-injection risk in tool-augmented agents.
- arXiv, SkillJect: [SkillJect: Automating Stealthy Skill-Based Prompt Injection for Coding Agents](https://arxiv.org/abs/2602.14211). Secondary academic context for malicious agent skills.
- Reddit / LocalLLaMA thread: [Nous Research Releases Hermes Agent](https://www.reddit.com/r/LocalLLaMA/comments/1rf5mvu/nous_research_releases_hermes_agent/). Weak secondary/community source for early Hermes reaction only.

Local Hikari sources inspected:

- `README.md`
- `CLAUDE.md`
- `AGENTS.md`
- `agents/runtime.py`
- `agents/telegram_bridge.py`
- `agents/scheduler.py`
- `agents/proactive.py`
- `agents/hooks.py`
- `agents/external_wrap_hook.py`
- `agents/engagement/*`
- `agents/subagents/prompts/*`
- `tools/_registry.py`
- `tools/gatekeeper.py`
- `tools/memory/recall.py`
- `tools/README.md`
- `config/tools.yaml`
- `.agents/skills/*`
- `tests/`

## 3. Hermes Agent teardown

### What Hermes actually does

Hermes is an open-source personal agent runtime from Nous Research. Its official README positions it as a self-improving AI agent that can run on a local machine, VPS, GPU cluster, or serverless infrastructure, and can be reached through CLI and messaging gateways including Telegram, Discord, Slack, WhatsApp, Signal, and more [Hermes GitHub](https://github.com/nousresearch/hermes-agent). The docs home repeats the "self-improving" framing and lists tools, memory, skills, MCP integration, voice, personality files, context files, security, and architecture as first-class surfaces [Hermes docs](https://hermes-agent.nousresearch.com/docs).

The concrete product is a multi-surface agent with:

- A CLI/TUI plus messaging gateway. The messaging docs describe platform adapters that receive messages, route them through a per-chat session store, dispatch to the agent, and run a cron scheduler every 60 seconds [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging).
- Toolsets. Hermes tools are organized into logical toolsets that can be enabled or disabled per platform. Official categories include web search, terminal/files, browser automation, media, agent orchestration, memory/recall, automation/delivery, integrations, and MCP tools [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools).
- Multiple terminal backends. Official docs list local, Docker, SSH, Singularity/Apptainer, Modal, Daytona, and Vercel Sandbox style execution backends, with Docker described as a persistent sandbox container and cloud backends for remote/serverless work [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools).
- Persistent memory. Hermes currently documents two bounded prompt-injected files, `MEMORY.md` and `USER.md`, stored under `~/.hermes/memories/`, with strict character limits and an agent-managed memory tool for add/replace/remove [Hermes memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory).
- Skills. Hermes uses `SKILL.md` style skills, local/external skill directories, skill bundles, hub sources, GitHub installs, skill audit/update/reset commands, and marketplace/source integrations including official optional skills, skills.sh, well-known endpoints, GitHub repositories, ClawHub, LobeHub, and browse.sh [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills).
- Scheduling and background work. Hermes cron can create one-shot or recurring jobs, attach skills, deliver results to chat/local/platform targets, run fresh isolated agent sessions, and run no-agent script-only jobs [Hermes cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron). The tools docs also document background process management through terminal/process actions [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools), and the messaging docs show a `/background <prompt>` chat command for a separate background session [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging).
- Delegation. Hermes' `delegate_task` spawns child AIAgent instances with isolated context, restricted toolsets, separate terminal sessions, parallel batch execution, optional model overrides, and monitoring through `/agents` or `/tasks`; the docs also state that `delegate_task` is synchronous and not durable, recommending cron or background terminal processes for work that must survive interrupts [Hermes delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation).
- Security controls. Hermes documents dangerous-command approval, messaging approval, gateway allowlists, pairing, MCP credential filtering, context file scanning, and container isolation [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security).

### Real vs hype

Real, documented:

- Multi-platform gateway, gateway setup/service commands, and a large set of in-chat commands are documented [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging).
- Toolsets and execution backends are documented with concrete categories and configuration examples [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools).
- Persistent memory has an explicit on-disk format and prompt-injection behavior [Hermes memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory).
- Skills, skill bundles, skill installs, skill audit/update commands, and multiple skill discovery sources are documented [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills).
- Cron scheduling and subagent delegation are documented as explicit automation surfaces [Hermes cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron), [Hermes delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation).
- Security approval and gateway authorization are documented in detail [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security).

Hype or not independently proven:

- The README and docs use "self-improving" and "built-in learning loop" language [Hermes GitHub](https://github.com/nousresearch/hermes-agent), [Hermes docs](https://hermes-agent.nousresearch.com/docs). The primitives are real: memory, skills, skill creation/update, session search, and nudges are documented. But I found no official evaluation proving that the resulting agent reliably improves user outcomes over time across domains.
- "Use any model" and broad provider claims are official README positioning [Hermes GitHub](https://github.com/nousresearch/hermes-agent). The useful takeaway is provider flexibility, not that every model will produce good agent behavior.
- Community Reddit posts describe excitement and skepticism around Hermes [Reddit / LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/comments/1rf5mvu/nous_research_releases_hermes_agent/). Treat that as weak sentiment, not evidence.

### Competitive takeaways for Hikari

Hermes' strongest lesson is packaging. It turns internal capabilities into visible commands: `hermes tools`, `hermes model`, `hermes gateway`, `/status`, `/usage`, `/background`, `/approve`, `/deny`, `/<skill-name>`, and skill/bundle management [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging), [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills). Hikari has comparable subsystems but fewer user-facing affordances.

Hermes' second lesson is skill lifecycle. Hikari has `.agents/skills/` and character/memory/photo/drive/heartbeat skills, but Hermes treats skills as installable, auditable, updateable, bundleable, and invokable objects [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills). Hikari should copy that lifecycle shape in a smaller, trusted form.

Hermes' third lesson is terminal isolation vocabulary. Hikari has sandboxed `python_run`, gatekeeper, and Codex-side approvals, but Hermes exposes backend choice and container hardening as product concepts [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools), [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security). Hikari should make risk levels understandable to the user.

## 4. OpenClaw teardown

### What OpenClaw actually does

OpenClaw is a self-hostable personal agent platform centered around a Gateway, channels, agents, tools, skills, plugins, models, memory, automation, sandboxing, and optional companion apps. The getting-started docs say setup gets the Gateway running, configures auth/model provider, opens a Control UI dashboard, and then lets the user chat via web or phone channels like Telegram [OpenClaw getting started](https://docs.openclaw.ai/start/getting-started).

The official product surface includes:

- Gateway and Control UI. Getting started verifies `openclaw gateway status`, opens `openclaw dashboard`, and uses Control UI chat for the first message [OpenClaw getting started](https://docs.openclaw.ai/start/getting-started).
- Channels. OpenClaw docs say channels connect through the Gateway and support many chat apps. The channel index includes Discord, Slack, Telegram, WhatsApp, Signal, Microsoft Teams, Google Chat, iMessage, Matrix, IRC, Mattermost, Nextcloud Talk, Nostr, Twitch, LINE, WeChat, QQ bot, Feishu, Yuanbao, and Zalo [OpenClaw channels](https://docs.openclaw.ai/channels).
- Tools, skills, plugins. OpenClaw explicitly distinguishes tools as typed callable actions, skills as prompt instruction packs, and plugins as runtime capability bundles such as tools, providers, channels, hooks, and packaged skills [OpenClaw tools](https://docs.openclaw.ai/tools).
- AgentSkills-compatible skills. OpenClaw loads skills from workspace, project, personal, managed/local, bundled, and extra directories with clear precedence, and it supports per-agent skill allowlists [OpenClaw skills](https://docs.openclaw.ai/tools/skills).
- ClawHub and skill installation. OpenClaw documents ClawHub installs, Git skill installs, local skill installs, global installs, updates, and an admin upload path for private trusted clients [OpenClaw skills](https://docs.openclaw.ai/tools/skills).
- Skill Workshop. The optional experimental Skill Workshop can create or update workspace skills from observed reusable procedures, writing only to workspace skills and supporting pending approval or safe automatic writes [OpenClaw skills](https://docs.openclaw.ai/tools/skills).
- Sub-agents. OpenClaw sub-agents are background agent runs in their own session, tracked as background tasks, used for parallel research/long tasks, optionally sandboxed, and able to report completion back to the requester channel [OpenClaw sub-agents](https://docs.openclaw.ai/tools/subagents).
- Automation. OpenClaw has scheduled tasks, heartbeat, background task ledger, inferred commitments, task flow, standing orders, and hooks. The docs distinguish exact cron, approximate heartbeat, detached task tracking, durable multi-step flows, and persistent instructions [OpenClaw automation](https://docs.openclaw.ai/automation).
- Memory. OpenClaw writes memory as plain Markdown files in the agent workspace, including `MEMORY.md`, daily `memory/YYYY-MM-DD.md` notes, and optional `DREAMS.md`; the docs emphasize that the model remembers what gets saved to disk and that bootstrap prompt copies can be truncated [OpenClaw memory](https://docs.openclaw.ai/concepts/memory).
- Providers. OpenClaw lists a large provider directory and configures models as `provider/model`, with docs for Anthropic, OpenAI, Google, OpenRouter, Ollama, LM Studio, Bedrock, DeepSeek, xAI, and many others [OpenClaw providers](https://docs.openclaw.ai/providers).
- Commands/directives. OpenClaw Gateway handles slash commands and directives, with authorization, owner-gated commands, command allowlists, and documented controls such as `/new`, `/reset`, `/compact`, `/stop`, `/think`, `/verbose`, `/trace`, `/elevated`, `/exec`, and `/model` [OpenClaw slash commands](https://docs.openclaw.ai/tools/slash-commands).
- Sandbox/tool policy/elevated controls. OpenClaw documents three layers: sandbox decides where tools run, tool policy decides which tools are available, and elevated is an exec-only escape hatch. It also documents sandbox modes `off`, `non-main`, and `all`, bind-mount risks, and deny-wins tool policy [OpenClaw sandbox](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated).

### Real vs hype

Real, documented:

- Gateway, dashboard, onboarding, and chat workflow are documented [OpenClaw getting started](https://docs.openclaw.ai/start/getting-started).
- Broad channel support is documented as a channel index, although per-channel maturity likely varies [OpenClaw channels](https://docs.openclaw.ai/channels).
- The tools/skills/plugins separation is a useful, explicit architectural vocabulary [OpenClaw tools](https://docs.openclaw.ai/tools).
- Skill precedence, per-agent skill allowlists, ClawHub installs, and Skill Workshop are documented [OpenClaw skills](https://docs.openclaw.ai/tools/skills).
- Sub-agent sessions, background task tracking, and session-spawn controls are documented [OpenClaw sub-agents](https://docs.openclaw.ai/tools/subagents).
- Automation taxonomy is documented unusually clearly: cron, heartbeat, tasks, inferred commitments, task flow, standing orders, and hooks [OpenClaw automation](https://docs.openclaw.ai/automation).
- Sandbox/tool-policy/elevated controls are documented with threat-relevant caveats like bind-mounts and `exec` side effects [OpenClaw sandbox](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated).

Hype or not independently proven:

- The OpenClaw README positions it as a broad personal AI assistant ecosystem with optional companion apps and a strong local-first identity [OpenClaw GitHub](https://github.com/openclaw/openclaw). The architecture exists in docs, but the quality of every channel, plugin, provider, and app cannot be inferred from docs alone.
- Secondary hands-on coverage describes OpenClaw as viral and potentially transformative [Tom's Guide](https://www.tomsguide.com/ai/i-tested-the-viral-ai-agent-that-could-replace-apps-and-it-made-me-appreciate-my-computer-without-it). That is product perception, not verification of reliability.
- Secondary security coverage raises concerns about malicious skills, prompt injection, and deep system access [TechRadar skills](https://www.techradar.com/pro/what-are-openclaw-skills-a-detailed-guide), [TechRadar security](https://www.techradar.com/pro/here-are-the-openclaw-security-risks-you-should-know-about), [McAfee](https://www.mcafee.com/learn/is-openclaw-safe-to-install/), [PCWorld](https://www.pcworld.com/article/3064874/openclaw-ai-is-going-viral-dont-install-it.html). OpenClaw's own docs do document sandbox/policy controls, so the honest read is "powerful and risky unless configured carefully," not "unsafe by definition."

### Competitive takeaways for Hikari

OpenClaw's best product idea is not any single tool. It is the layered control model: channels, agents, skills, tools, plugins, provider profiles, tasks, sandbox, policy, elevated mode, and commands all have names the operator can inspect [OpenClaw tools](https://docs.openclaw.ai/tools), [OpenClaw sandbox](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated).

OpenClaw's second strong idea is the background-work taxonomy. Hikari already has scheduler jobs, reminders, proactive engagement, Graphiti outbox drain, media outbox drain, and internal control calls, but OpenClaw gives the user a language for exact schedules, heartbeat checks, inferred commitments, task ledgers, durable flows, and hooks [OpenClaw automation](https://docs.openclaw.ai/automation).

OpenClaw's third strong idea is skill/workflow installability with explicit roots and precedence [OpenClaw skills](https://docs.openclaw.ai/tools/skills). Hikari should adopt a narrower trusted-skill lifecycle rather than a public marketplace posture.

## 5. Hikari current-state comparison

Local inspection shows Hikari is already a mature single-user personal agent, not a toy bot.

Runtime:

- `agents/runtime.py` exposes three entrypoints: `run_user_turn`, `run_visible_proactive`, and `run_internal_control`, matching the `AGENTS.md` contract. Live turns resume the stored Claude SDK session and are serialized with `_RUN_LOCK`; internal control calls are stateless and do not mutate the live SDK session.
- `respond()` persists raw user input, while the Telegram bridge owns appending final assistant text after successful delivery. This is a strong invariant: final-sent text is what gets persisted.
- `_build_options()` attaches in-process MCP servers from `config/tools.yaml`, hooks, gatekeeper `can_use_tool`, allowed tools, subagent registry, and skill support.

Messaging:

- `agents/telegram_bridge.py` is owner-gated and single-user, with typing heartbeat, media/photo outbox, post-filtering, reaction feedback, and cockpit-style commands.
- Hikari is less channel-general than Hermes/OpenClaw, but it has higher fidelity on one channel.

Scheduling and proactive behavior:

- `agents/scheduler.py` wires APScheduler jobs for reminders, Apple/GCal sync, daily reflection, morning brief, memory pruning, daily/evening check-ins, drift canary, future letters, decision resolver, weekly/monthly consolidation, engagement ticks, MCP warm-pool eviction, Graphiti outbox drain, and media outbox drain.
- `agents/engagement/*` includes trigger candidates, novelty/actionability/confidence scoring, mood/time multipliers, snooze and pool caps, source-specific templates, payload-anchor requirements, and generic-message guards.
- `agents/proactive.py` separates literal reminder delivery from LLM-composed proactive turns.

Memory:

- `storage/db.py` schema includes facts, messages, episodes, tasks, core blocks, runtime state, vector tables, background tasks, approvals, audit log, lexicon, observations, noticings, peer representation, user feedback, reminders, relation tables, weekly archives, session scratch, OAuth audit, future letters, decisions, and media outbox.
- `tools/memory/recall.py` uses Graphiti primary search with SQLite legacy fallback, confidence buckets, and fact-validity checks.
- Hikari has stronger provenance and invalidation semantics than Hermes' currently documented two-file memory and OpenClaw's Markdown memory, though those systems are simpler and more user-editable.

Tools and safety:

- `config/tools.yaml` defines MCP servers, tool gates, untrusted-output status, wrapping patterns, allow/deny policy, and subagent prompts.
- `agents/external_wrap_hook.py` wraps untrusted tool outputs based on the registry, including MCP envelopes and bare strings.
- `tools/gatekeeper.py` implements durable async approvals with Telegram prompts, deadlines, restart recovery, nudges/expiration, and hash-chained audit rows on approval.
- `read_attachment` is hard-scoped to user upload roots. `python_run` is sandboxed with macOS sandbox-exec, no network, timeout, and ephemeral writes.

Skills and delegation:

- `.agents/skills/` includes character voice, recall-memory, drive-search, generate-photo, and schedule-heartbeat.
- `AGENTS.md` defines delegated subagents for wiki, research, drive_gmail, notion, github, and codex reports. Hikari rewrites specialist output in voice, preserving the illusion of one person.
- Hikari currently lacks a polished skill catalog/install/update/audit lifecycle like Hermes/OpenClaw.

Tests:

- `tests/` covers gatekeeper, prompt injection, proactive behavior, memory, reminders, Telegram bridge, runtime behavior, and many utility tools. This is one of Hikari's clearest advantages over competitor "demo surface" energy.

## 6. Comparison table: Hermes vs OpenClaw vs Hikari

| Dimension | Hermes Agent | OpenClaw | Hikari |
|---|---|---|---|
| Product posture | Self-improving personal agent that runs anywhere and reaches many platforms [Hermes GitHub](https://github.com/nousresearch/hermes-agent) | Self-hosted Gateway with channels, agents, tools, skills, plugins, control UI [OpenClaw getting started](https://docs.openclaw.ai/start/getting-started) | Single-user Telegram companion/agent with high persona fidelity and operational guardrails |
| Primary UX | CLI/TUI plus messaging gateway; many chat commands [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging) | Dashboard/Control UI plus many chat channels and slash commands [OpenClaw slash commands](https://docs.openclaw.ai/tools/slash-commands) | Telegram conversation plus cockpit commands; mostly implicit operator UX |
| Channels | Broad gateway: Telegram, Discord, Slack, WhatsApp, Signal, and more [Hermes docs](https://hermes-agent.nousresearch.com/docs) | Very broad channel index through Gateway [OpenClaw channels](https://docs.openclaw.ai/channels) | Telegram-first, owner-gated |
| Tools | Toolsets for web, terminal/files, browser, media, memory, automation, messaging, MCP, etc. [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools) | Tools are typed actions filtered by profile, policy, provider, sandbox, channel, plugin availability [OpenClaw tools](https://docs.openclaw.ai/tools) | Rich utility/MCP registry in `config/tools.yaml`; gates and untrusted wrappers |
| Skills | Skills, bundles, hubs, installs, audit/update/reset [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills) | AgentSkills-compatible skills, roots/precedence, allowlists, Skill Workshop, ClawHub [OpenClaw skills](https://docs.openclaw.ai/tools/skills) | `.agents/skills/` exists; lifecycle is local/developer-facing |
| Memory | Bounded `MEMORY.md` and `USER.md` injected at session start [Hermes memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) | Plain Markdown `MEMORY.md`, daily notes, optional `DREAMS.md` [OpenClaw memory](https://docs.openclaw.ai/concepts/memory) | Graphiti plus SQLite facts/episodes/tasks/provenance/invalidation; stronger but less user-editable |
| Background work | Cron, skill-backed scheduled jobs, no-agent scripts, process management, and `/background` [Hermes cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron), [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools), [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging) | Cron, heartbeat, task ledger, inferred commitments, task flow, hooks [OpenClaw automation](https://docs.openclaw.ai/automation) | APScheduler jobs, proactive engagement, reminders, sync, reflection, outbox drains; limited user-facing job cockpit |
| Delegation | `delegate_task` child agents with isolated context, restricted toolsets, parallel batches, and synchronous durability limits [Hermes delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation) | Sub-agents are tracked background sessions with controls [OpenClaw sub-agents](https://docs.openclaw.ai/tools/subagents) | Specialist subagents by prompt plus direct MCP/utility tools |
| Execution isolation | Local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools) | Sandbox/tool-policy/elevated layers [OpenClaw sandbox](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated) | Tool-specific sandboxing and approvals; no broad user-visible execution-mode model |
| Commands/control | `hermes tools`, `hermes model`, gateway commands, chat commands [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging) | Rich command/directive system with authorization [OpenClaw slash commands](https://docs.openclaw.ai/tools/slash-commands) | Some cockpit commands; less comprehensive capability/status UX |
| Safety model | Command approval, allowlists, pairing, credential filtering, context scanning, containers [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security) | Sandbox, tool policy, elevated mode, owner commands, command allowlists [OpenClaw sandbox](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated) | Durable gatekeeper, untrusted wrappers, owner gate, scoped readers, audit log, tests |
| Differentiator | Self-improvement and skill ecosystem packaging | Control-plane clarity and multi-channel/multi-agent ops | Personal continuity, voice, trust boundary, proactive emotional/productive intelligence |

## 7. Product lessons

1. Hikari needs a capability map the user can ask for. Hermes and OpenClaw both let users inspect tools, models, commands, and status through first-class commands [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging), [OpenClaw slash commands](https://docs.openclaw.ai/tools/slash-commands). Hikari should expose "what can you do right now?" from `config/tools.yaml`, skill metadata, and MCP health.

2. Make background work visible. OpenClaw's task ledger is a product feature, not just an implementation detail [OpenClaw automation](https://docs.openclaw.ai/automation). Hikari has many scheduled/proactive jobs but lacks a simple `/jobs` or `/activity` surface that explains what is pending, running, skipped, snoozed, or failed.

3. Treat skills as a user's library. Hermes' skill bundles and hub commands make skills feel like reusable workflows [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills). Hikari should offer a trusted personal skill catalog before considering marketplace-style installs.

4. Keep Hikari's opinionated single-user identity. Hermes and OpenClaw broaden into many channels and many operators. Hikari's moat is that it feels like one relationship, not a generic gateway.

5. Add memory review rather than more hidden memory. OpenClaw's Markdown memory is legible [OpenClaw memory](https://docs.openclaw.ai/concepts/memory). Hikari's memory is more structured, but the user should be able to inspect, correct, invalidate, and pin it without touching SQLite.

## 8. Architecture lessons

1. Separate concepts with product names. OpenClaw's distinction between tools, skills, plugins, agents, tasks, standing orders, heartbeat, and cron is clean [OpenClaw tools](https://docs.openclaw.ai/tools), [OpenClaw automation](https://docs.openclaw.ai/automation). Hikari has equivalent internal concepts, but naming them would reduce operator confusion.

2. Add a "control plane" without becoming generic. Hermes has CLI/gateway commands and OpenClaw has a dashboard [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging), [OpenClaw getting started](https://docs.openclaw.ai/start/getting-started). Hikari could add a small local web or Telegram cockpit for health, approvals, jobs, memory, skills, and proactive settings.

3. Formalize background work records. Hikari's `background_tasks`, scheduler jobs, proactive events, reminders, and media outbox could be projected into a unified task/activity ledger similar to OpenClaw's task concept [OpenClaw automation](https://docs.openclaw.ai/automation).

4. Adopt explicit execution policy vocabulary. OpenClaw's sandbox/tool-policy/elevated split is worth adapting [OpenClaw sandbox](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated). Hikari can map existing gatekeeper and sandbox behavior to user-facing "safe", "asks first", "blocked", and "elevated" labels.

5. Keep final-sent persistence as a non-negotiable invariant. Neither Hermes nor OpenClaw docs foreground this exact guarantee. Hikari's split between SDK output and Telegram post-send persistence is product-grade reliability.

## 9. UX lessons

1. Commands should answer the operator's obvious questions: "what are you doing?", "what can you access?", "what is pending?", "what did you remember?", "what failed?", "what will you do later?"

2. Hikari should avoid command sprawl in normal conversation. Hermes and OpenClaw expose many commands [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging), [OpenClaw slash commands](https://docs.openclaw.ai/tools/slash-commands). Hikari should keep commands mostly hidden behind natural language, but provide exact commands for recovery, audits, and settings.

3. Give proactive behavior a visible "why". OpenClaw distinguishes heartbeat, inferred commitments, and cron [OpenClaw automation](https://docs.openclaw.ai/automation). Hikari's proactive messages already require anchors internally; surface those anchors in audit/history UI.

4. A dashboard does not have to become a product shell. OpenClaw's Control UI is useful for setup and health [OpenClaw getting started](https://docs.openclaw.ai/start/getting-started). Hikari can keep Telegram as the soul of the product while adding a local operator panel for things that are awkward in chat.

5. Skill discovery should be intimate, not marketplace-y. Hermes/OpenClaw public skill ecosystems are powerful [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills), [OpenClaw skills](https://docs.openclaw.ai/tools/skills), but Hikari should frame skills as "rituals", "workflows", or "ways I know how to help you", not an app store.

## 10. Tool/safety lessons

1. Keep untrusted data visibly separate from instructions. Academic work on tool-augmented agents continues to identify indirect prompt injection as a serious risk [ClawGuard](https://arxiv.org/abs/2604.11790). Hikari's untrusted-output wrapper is exactly the right direction.

2. Treat third-party skills as executable trust bundles. Skill-based prompt injection is a live research concern [SkillJect](https://arxiv.org/abs/2602.14211). Secondary coverage of OpenClaw also focuses on third-party skill risk [TechRadar skills](https://www.techradar.com/pro/what-are-openclaw-skills-a-detailed-guide), [McAfee](https://www.mcafee.com/learn/is-openclaw-safe-to-install/). Hikari should not auto-install public skills without scanning, approval, source pinning, and provenance.

3. Copy the explicit policy model, not the risky defaults. OpenClaw's docs warn that sandboxing, tool policy, and elevated execution are different controls [OpenClaw sandbox](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated). Hikari should show the user this distinction for any tool that reads files, sends messages, spends money, calls external APIs, or writes outside ephemeral storage.

4. Make approvals revocable and inspectable. Hermes has once/session/always/deny style approvals and persistent allowlists [Hermes security](https://hermes-agent.nousresearch.com/docs/user-guide/security). Hikari's durable gatekeeper should add a user-facing history and revoke UI before broadening privileges.

5. Keep one strong default: deny or ask for risky writes. PCWorld, TechRadar, and McAfee all frame OpenClaw's power as risky when local access and third-party skills are casually enabled [PCWorld](https://www.pcworld.com/article/3064874/openclaw-ai-is-going-viral-dont-install-it.html), [TechRadar security](https://www.techradar.com/pro/here-are-the-openclaw-security-risks-you-should-know-about), [McAfee](https://www.mcafee.com/learn/is-openclaw-safe-to-install/). Hikari's existing gatekeeper culture is a competitive advantage.

## 11. 15 ranked Hikari backlog items

1. Capability cockpit: add `/tools` or `/capabilities` that renders enabled tools, gated tools, untrusted-output tools, MCP server health, and skill availability from `config/tools.yaml`.

2. Approval center: add `/approvals` to list pending approvals, recent approvals, expirations, denials, and permanent grants; include revoke for any persistent allow/ask pattern.

3. Background job/activity ledger: unify reminders, scheduler jobs, proactive ticks, media outbox, Graphiti outbox, internal control jobs, and subagent dispatches into `/jobs` with status, last run, next run, failure reason, and cancel/snooze where applicable.

4. Memory review/edit UX: add `/memory` for facts, confidence, provenance, validity, open tasks, core blocks, and "forget/correct this" flows. Keep Graphiti/SQLite internals hidden.

5. Proactive source controls: add `/proactive` to show enabled sources, quiet hours, snoozes, pool caps, recent anchors, reaction stats, and why the last proactive message fired or skipped.

6. Skill catalog v1: index `.agents/skills/` with name, trigger, status, risk, last used, and whether it can call external tools. Do not support public installs yet.

7. Trusted workflow bundles: create Hikari-native bundles for research sprint, inbox triage, code review, day receipt, memory cleanup, travel prep, and weekly review, inspired by Hermes skill bundles [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills).

8. Tool policy labels: expose each tool as `safe`, `untrusted read`, `asks first`, `writes`, `external send`, or `blocked`, adapting OpenClaw's sandbox/policy vocabulary [OpenClaw sandbox](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated).

9. Risk previews for approvals: before approving a tool, show recipient/file/path/API, irreversible side effects, credential exposure, and whether output will be public or private.

10. Local operator panel: a minimal local web page for status, logs, tools, approvals, jobs, memory, and proactive settings. Keep Telegram as the primary experience.

11. Subagent handoff ledger: record each research/wiki/drive/github/codex-report delegation with prompt, sources touched, result summary, and whether its output was rewritten before user delivery.

12. Skill safety scanner: before adding or updating any skill, scan for network instructions, secret exfiltration language, shell commands, file writes, hidden prompt injection, and unclear provenance.

13. Channel abstraction lite: keep Telegram first, but separate inbound message, delivery, media, reaction, and command handling enough to support a future local web chat or Signal/iMessage experiment without rewriting personality/runtime semantics.

14. Model/cost dashboard: expose recent Claude SDK usage, internal control budgets, Max/OpenRouter routing choices, failure counts, and "why this model" decisions.

15. Operator runbook generator: generate a current `status.md` or `/doctor` summary from README, launchd state, env/credential checks, MCP server health, scheduler health, database migrations, and recent errors.

## 12. What not to copy

- Do not copy marketplace sprawl before trust infrastructure. Hermes and OpenClaw both integrate broad skill ecosystems [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills), [OpenClaw skills](https://docs.openclaw.ai/tools/skills). Hikari should stay curated until scanning, pinning, review, provenance, and rollback are mature.

- Do not copy generic multi-user/channel-first identity. Hermes and OpenClaw are competing as broad agent platforms [Hermes docs](https://hermes-agent.nousresearch.com/docs), [OpenClaw channels](https://docs.openclaw.ai/channels). Hikari's value is one relationship, one owner, one continuity model.

- Do not copy "self-improving" as marketing without measurable loops. Hikari can improve through memory, receipts, reactions, tests, and workflow extraction, but should not promise autonomous self-improvement unless there are evaluations and rollback paths.

- Do not expose `/exec`-style power casually. OpenClaw's docs are refreshingly honest that `exec` side effects are not made safe just by denying file tools [OpenClaw sandbox](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated). Hikari should keep shell/code execution narrow and gated.

- Do not turn every product affordance into a slash command. Commands are good for recovery and inspection, but Hikari's daily UX should remain conversational and characterful.

- Do not weaken final-sent persistence for richer streaming. If an assistant message is persisted before delivery, the user history can diverge from reality. Hikari's bridge-owned post-send append should stay sacred.

## 13. Open questions / claims that could not be verified

- Hermes' "self-improving" quality: official sources document the primitives, but I did not find independent evaluations showing durable cross-domain improvement from autonomous skill creation or memory curation [Hermes GitHub](https://github.com/nousresearch/hermes-agent), [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills).

- Hermes platform maturity per channel: official docs list many platforms and gateway commands, but I did not verify each adapter's production maturity or media parity [Hermes messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging).

- Hermes provider/model breadth in real use: official README claims broad provider flexibility [Hermes GitHub](https://github.com/nousresearch/hermes-agent). I did not verify latency, tool-call compatibility, cost, or reliability across providers.

- OpenClaw channel maturity per platform: official channel docs list many platforms [OpenClaw channels](https://docs.openclaw.ai/channels). I did not verify setup success, rate limits, media parity, or bot policy compliance for each.

- OpenClaw Skill Workshop safety: official docs say it scans generated content, supports pending approval, and quarantines unsafe proposals [OpenClaw skills](https://docs.openclaw.ai/tools/skills). I did not audit the scanner implementation or bypass resistance.

- Secondary security claims about specific OpenClaw incidents: TechRadar, McAfee, and PCWorld report serious concerns and incidents around skills/prompt injection/system access [TechRadar security](https://www.techradar.com/pro/here-are-the-openclaw-security-risks-you-should-know-about), [McAfee](https://www.mcafee.com/learn/is-openclaw-safe-to-install/), [PCWorld](https://www.pcworld.com/article/3064874/openclaw-ai-is-going-viral-dont-install-it.html). I treated those as secondary risk perception unless matched by official OpenClaw documentation.

- Hikari local runtime health today: I inspected repo files but did not start services, run tests, or exercise Telegram/MCP integrations. This report is architectural/product research, not a live health audit.
