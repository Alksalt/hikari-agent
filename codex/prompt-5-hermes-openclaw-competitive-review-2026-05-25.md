# Prompt 5 — Hermes / OpenClaw Competitive Review

Date: 2026-05-25  
Repo inspected: `/Users/ol/agents/hikari-agent`

This review combines local repo inspection with official product/docs research and a small set of clearly labeled secondary sources. Where a feature was only claimed in marketing or secondary coverage and I could not confirm it in official docs, I mark it as unverified.

## 1. Executive summary

Hermes Agent and OpenClaw are both real products, not vapor. They are also aiming at different layers of the stack.

- **Hermes Agent** is a broad agent runtime: tools, toolsets, agent-managed skills, bounded persistent memory, session search, multi-platform messaging, background sessions, cron, and multiple terminal backends are all documented in official sources. The “self-improving” part is real in the sense of **skill creation, memory persistence, and curator maintenance**, not in the sense of autonomous capability compounding with hard guarantees. Sources: [Hermes README](https://github.com/NousResearch/hermes-agent/blob/main/README.md), [Tools & Toolsets](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/tools.md), [Skills](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md), [Memory](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md), [Cron](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/cron.md), [Curator](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/curator.md).
- **OpenClaw** is a gateway/control-plane product for personal agents: channels, session routing, skills, subagents, cron, exec/process, optional sandboxing, local/remote execution, and a very large model/provider matrix are all documented. The official security docs are unusually blunt that this is a **personal-assistant trust model**, not a hostile multi-tenant security boundary. Sources: [OpenClaw README](https://github.com/openclaw/openclaw/blob/main/README.md), [Channels](https://github.com/openclaw/openclaw/blob/main/docs/channels/index.md), [Skills](https://github.com/openclaw/openclaw/blob/main/docs/tools/skills.md), [Exec](https://github.com/openclaw/openclaw/blob/main/docs/tools/exec.md), [Sub-agents](https://github.com/openclaw/openclaw/blob/main/docs/tools/subagents.md), [Cron](https://github.com/openclaw/openclaw/blob/main/docs/cli/cron.md), [Sandboxing](https://github.com/openclaw/openclaw/blob/main/docs/gateway/sandboxing.md), [Security](https://github.com/openclaw/openclaw/blob/main/docs/gateway/security/index.md).
- **Hikari should not copy either product wholesale.** Hikari already has a better core shape for its actual job: single-user scope, stronger personal memory, stronger internal-control separation, and more explicit prompt-injection hygiene. It should copy **workflow discovery, background-task UX, and execution profiles**; adapt **skill curation and project-scoped automation**; and reject **channel sprawl, giant default tool surfaces, and “self-improving” marketing without tight review loops**.

My bottom line:

1. **Hermes is the better reference for reusable workflows and scheduled agent work.**
2. **OpenClaw is the better reference for session routing, channel mechanics, and configurable safety dials.**
3. **Hikari is already better at being a trusted, coherent, personal companion.**

## 2. Source list

### Official / primary sources

- **Official** — Hermes Agent repo README: [github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/blob/main/README.md)
- **Official** — Hermes CLI Interface: [CLI Interface](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/cli.md)
- **Official** — Hermes CLI Commands Reference: [CLI Commands Reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/cli-commands.md)
- **Official** — Hermes Messaging Gateway: [Messaging Gateway](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/index.md)
- **Official** — Hermes Tools & Toolsets: [Tools & Toolsets](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/tools.md)
- **Official** — Hermes Built-in Tools Reference: [Built-in Tools Reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/tools-reference.md)
- **Official** — Hermes Toolsets Reference: [Toolsets Reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/toolsets-reference.md)
- **Official** — Hermes Skills System: [Skills System](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md)
- **Official** — Hermes Persistent Memory: [Persistent Memory](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md)
- **Official** — Hermes Scheduled Tasks (Cron): [Scheduled Tasks](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/cron.md)
- **Official** — Hermes Curator: [Curator](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/curator.md)
- **Official** — Hermes Tool Gateway: [Nous Tool Gateway](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/tool-gateway.md)
- **Official** — OpenClaw repo README: [github.com/openclaw/openclaw](https://github.com/openclaw/openclaw/blob/main/README.md)
- **Official** — OpenClaw chat channels: [Chat channels](https://github.com/openclaw/openclaw/blob/main/docs/channels/index.md)
- **Official** — OpenClaw skills: [Skills](https://github.com/openclaw/openclaw/blob/main/docs/tools/skills.md)
- **Official** — OpenClaw slash commands: [Slash commands](https://github.com/openclaw/openclaw/blob/main/docs/tools/slash-commands.md)
- **Official** — OpenClaw exec tool: [Exec tool](https://github.com/openclaw/openclaw/blob/main/docs/tools/exec.md)
- **Official** — OpenClaw background exec/process: [Background exec and process tool](https://github.com/openclaw/openclaw/blob/main/docs/gateway/background-process.md)
- **Official** — OpenClaw sub-agents: [Sub-agents](https://github.com/openclaw/openclaw/blob/main/docs/tools/subagents.md)
- **Official** — OpenClaw cron: [openclaw cron](https://github.com/openclaw/openclaw/blob/main/docs/cli/cron.md)
- **Official** — OpenClaw sandboxing: [Sandboxing](https://github.com/openclaw/openclaw/blob/main/docs/gateway/sandboxing.md)
- **Official** — OpenClaw security: [Security](https://github.com/openclaw/openclaw/blob/main/docs/gateway/security/index.md)
- **Official** — OpenClaw model provider quickstart: [Model provider quickstart](https://github.com/openclaw/openclaw/blob/main/docs/providers/models.md)
- **Official** — OpenClaw local models: [Local models](https://github.com/openclaw/openclaw/blob/main/docs/gateway/local-models.md)

### Local repo inspected

- **Local** — [README.md](/Users/ol/agents/hikari-agent/README.md)
- **Local** — [CLAUDE.md](/Users/ol/agents/hikari-agent/CLAUDE.md)
- **Local** — [AGENTS.md](/Users/ol/agents/hikari-agent/AGENTS.md)
- **Local** — [config/tools.yaml](/Users/ol/agents/hikari-agent/config/tools.yaml)
- **Local** — [agents/runtime.py](/Users/ol/agents/hikari-agent/agents/runtime.py)
- **Local** — [agents/scheduler.py](/Users/ol/agents/hikari-agent/agents/scheduler.py)
- **Local** — [agents/proactive.py](/Users/ol/agents/hikari-agent/agents/proactive.py)
- **Local** — [agents/tool_inventory.py](/Users/ol/agents/hikari-agent/agents/tool_inventory.py)
- **Local** — [agents/hooks.py](/Users/ol/agents/hikari-agent/agents/hooks.py)
- **Local** — [agents/telegram_bridge.py](/Users/ol/agents/hikari-agent/agents/telegram_bridge.py)
- **Local** — [agents/external_wrap_hook.py](/Users/ol/agents/hikari-agent/agents/external_wrap_hook.py)
- **Local** — [agents/injection_guard.py](/Users/ol/agents/hikari-agent/agents/injection_guard.py)
- **Local** — [tools/gatekeeper.py](/Users/ol/agents/hikari-agent/tools/gatekeeper.py)
- **Local** — [storage/db.py](/Users/ol/agents/hikari-agent/storage/db.py)

### Secondary sources

- **Secondary** — MarkTechPost launch coverage of Hermes: [Nous Research Releases 'Hermes Agent'](https://www.marktechpost.com/2026/02/26/nous-research-releases-hermes-agent-to-fix-ai-forgetfulness-with-multi-level-memory-and-dedicated-remote-terminal-access-support/)
- **Secondary** — OpenAIToolsHub Hermes review: [Hermes Agent AI Framework Review](https://www.openaitoolshub.org/en/blog/hermes-agent-ai-review)
- **Secondary** — TechRadar overview of OpenClaw: [What is OpenClaw?](https://www.techradar.com/pro/what-is-openclaw)
- **Secondary** — arXiv safety analysis of OpenClaw: [Your Agent, Their Asset: A Real-World Safety Analysis of OpenClaw](https://arxiv.org/abs/2604.04759)

## 3. Hermes Agent teardown

### What Hermes actually does

Hermes is a **general-purpose agent runtime** that spans CLI/TUI, messaging, tool execution, skills, memory, and automation. Official docs back up all of the following:

- a tool registry of roughly 70 built-ins plus dynamic MCP tools, grouped into **toolsets** and **platform toolsets**;  
- multiple terminal execution backends: local, Docker, SSH, Singularity/Apptainer, Modal, Daytona, and Vercel Sandbox;  
- **AgentSkills-compatible skills** with progressive disclosure, slash-command invocation, agent-managed creation/editing, and a background **Curator** to keep agent-created skills from rotting;  
- bounded persistent memory split into `MEMORY.md` and `USER.md`, plus full-text `session_search`;  
- a multi-platform **messaging gateway** and a large CLI command surface;  
- **background sessions**, **delegation**, and a substantial **cron** system with skill-backed jobs, workdir/profile pins, no-agent script mode, and output routing.  

Sources: [Hermes README](https://github.com/NousResearch/hermes-agent/blob/main/README.md), [CLI Interface](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/cli.md), [Messaging Gateway](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/index.md), [Tools & Toolsets](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/tools.md), [Toolsets Reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/toolsets-reference.md), [Built-in Tools Reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/tools-reference.md), [Skills](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md), [Memory](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md), [Cron](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/cron.md), [Curator](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/curator.md).

### What is real

- **Tooling breadth is real.** Hermes is not just “terminal + web.” It has explicit toolsets for browser, terminal, file editing, memory, delegation, cron, code execution, messaging, vision, TTS, and more. The docs also separate core toolsets from platform toolsets cleanly.
- **Workflow reuse is real.** Hermes’ skill system is serious: skills live in `~/.hermes/skills/`, support progressive disclosure, become slash commands automatically, can be created/edited by the agent, and can be curated over time.
- **Scheduled work is real.** Hermes’ cron system is unusually strong: jobs can run in fresh sessions, attach one or more skills, pin a workdir, pin a profile, run with no LLM at all, and route output to different targets.
- **Background work is real.** `/background` creates isolated background agent sessions, and `delegate_task` gives Hermes a documented multi-agent/subagent path.
- **The SaaS shortcut is real.** Nous’ Tool Gateway collapses several external tool providers behind a Portal subscription. That reduces setup friction materially, even if it also changes the product shape.

### What is hype

- **“Self-improving” mostly means retrieval + skill accumulation + maintenance.** The official docs show memory files, session search, agent-managed skills, and Curator reviews. They do **not** show magic online learning, weight updates, or any hard performance guarantees. This is useful, but it is not autonomous self-optimization in the stronger sense.
- **Memory is intentionally small and frozen per session.** Hermes’ official memory docs describe `MEMORY.md` and `USER.md` with strict character limits and a frozen session-start snapshot. That is pragmatic and cache-friendly, but it is also much thinner than the marketing aura suggests.
- **Breadth brings combinatorial ops cost.** Seven terminal backends, tool gateways, skills, hub installs, messaging platforms, cron delivery targets, and profile/workdir switches make Hermes powerful but also harder to reason about. This is a platform, not a tight product.

### Competitive read for Hikari

Hermes is strongest as a reference for:

1. **workflow packaging**;
2. **chat-native scheduled work**;
3. **background job UX**;
4. **execution-backend abstraction**.

Hermes is not the right model for Hikari if the goal is to preserve emotional coherence, narrow trust boundaries, or keep operator cognitive load low.

## 4. OpenClaw teardown

### What OpenClaw actually does

OpenClaw is best understood as a **self-hosted agent gateway/control plane**. Its official docs describe:

- many simultaneous **chat channels** fronted by one gateway;
- a large **slash-command/directive** layer for users and operators;
- an **AgentSkills-compatible skill system** with precedence rules, per-agent allowlists, managed installs, and an optional experimental Skill Workshop;
- a very broad **exec/process/subagent/cron** runtime;
- optional **sandboxing** with Docker, SSH, or OpenShell backends;
- a wide **provider/model matrix**, including local-model guidance and proxy-style local backends;
- a security model that explicitly says the supported trust boundary is **one trusted operator boundary per gateway**, not a hostile multi-user environment.

Sources: [OpenClaw README](https://github.com/openclaw/openclaw/blob/main/README.md), [Channels](https://github.com/openclaw/openclaw/blob/main/docs/channels/index.md), [Skills](https://github.com/openclaw/openclaw/blob/main/docs/tools/skills.md), [Slash commands](https://github.com/openclaw/openclaw/blob/main/docs/tools/slash-commands.md), [Exec](https://github.com/openclaw/openclaw/blob/main/docs/tools/exec.md), [Background exec/process](https://github.com/openclaw/openclaw/blob/main/docs/gateway/background-process.md), [Sub-agents](https://github.com/openclaw/openclaw/blob/main/docs/tools/subagents.md), [Cron](https://github.com/openclaw/openclaw/blob/main/docs/cli/cron.md), [Sandboxing](https://github.com/openclaw/openclaw/blob/main/docs/gateway/sandboxing.md), [Security](https://github.com/openclaw/openclaw/blob/main/docs/gateway/security/index.md), [Model provider quickstart](https://github.com/openclaw/openclaw/blob/main/docs/providers/models.md), [Local models](https://github.com/openclaw/openclaw/blob/main/docs/gateway/local-models.md).

### What is real

- **The gateway model is real.** OpenClaw is genuinely channel-first. The docs cover channel behavior, native vs text commands, route resolution, thread/session bindings, and operational controls in real detail.
- **The skill system is real and more disciplined than most clones.** The docs spell out skill precedence, per-agent allowlists, managed vs workspace skills, environment injection, and snapshot refresh behavior.
- **The runtime surface is real.** `exec`, `process`, `sessions_spawn`, subagents, cron, focus/unfocus, and session overrides are all documented. OpenClaw is not just a chat shell over one model.
- **The safety/config model is real.** OpenClaw has more explicit knobs than most peers: sandbox modes, scope, backend selection, host routing, exec approvals, and security audit commands.
- **The local model story is real.** OpenClaw has better-than-average docs for local models, compatibility flags, and graceful degradation when weaker local backends struggle.

### What is hype

- **“Runs anywhere, automates everything” hides the operator burden.** The official docs are honest, but the product surface is huge: channels, nodes, plugins, skills, approvals, cron, sandboxes, browser control, local/remote execution, and per-agent overrides. This is a runtime platform with a lot of footguns.
- **Sandboxing is not the default safety reality.** The docs explicitly say sandboxing is optional and off by default, and `exec` defaults to host/gateway behavior unless the operator tightens it.
- **The trust model is narrower than the brand vibe.** The official security docs clearly reject the idea that one gateway is a safe boundary for multiple adversarial users. That matters because the product’s distribution often sounds broader than its supported threat model.

### Secondary-source reality check

The secondary sources mostly reinforce the same split:

- TechRadar describes OpenClaw as a self-hosted agent that connects LLMs to software and services through a local Gateway, which matches the official docs. Source: [TechRadar](https://www.techradar.com/pro/what-is-openclaw).
- The independent arXiv safety paper is the more important signal: it argues that broad capability + identity + knowledge persistence materially expands the attack surface and reports sharply higher attack success when those dimensions are poisoned. That does not override official docs, but it does support taking OpenClaw’s own warnings seriously. Source: [arXiv safety analysis](https://arxiv.org/abs/2604.04759).

### Competitive read for Hikari

OpenClaw is strongest as a reference for:

1. **session and thread routing**;
2. **operator-visible command UX**;
3. **execution/sandbox policy design**;
4. **skill precedence and allowlists**.

OpenClaw is not the right model for Hikari if the goal is to stay emotionally coherent, narrow in blast radius, or intentionally simple.

## 5. Hikari current-state comparison

### What Hikari is today

Hikari is already much more opinionated than either competitor. From the local repo:

- it is a **single-user Telegram agent** on the Claude Agent SDK, not a generic many-channel platform: [README.md](/Users/ol/agents/hikari-agent/README.md);
- it has a **three-way runtime split** between real user turns, visible proactive turns, and stateless internal control turns, with explicit session-handling invariants: [AGENTS.md](/Users/ol/agents/hikari-agent/AGENTS.md), [agents/runtime.py](/Users/ol/agents/hikari-agent/agents/runtime.py);
- it has a **registry-driven tool model** with `untrusted_output` metadata, wrap patterns, subagent mappings, and gatekeeper hooks: [config/tools.yaml](/Users/ol/agents/hikari-agent/config/tools.yaml), [agents/hooks.py](/Users/ol/agents/hikari-agent/agents/hooks.py), [tools/gatekeeper.py](/Users/ol/agents/hikari-agent/tools/gatekeeper.py);
- it has stronger **prompt-injection hygiene** than either competitor’s default path, including explicit untrusted-output wrapping and taint flagging: [agents/external_wrap_hook.py](/Users/ol/agents/hikari-agent/agents/external_wrap_hook.py), [agents/injection_guard.py](/Users/ol/agents/hikari-agent/agents/injection_guard.py);
- it has a richer **structured memory/state model** than Hermes’ flat files: facts, messages, episodes, tasks, reminders, approvals, tool-call telemetry, background tasks, feedback, and graph outbox are all first-class tables: [storage/db.py](/Users/ol/agents/hikari-agent/storage/db.py);
- it has a substantial **scheduler/proactive layer** already wired for daily reflection, morning brief, drift canary, future letters, decision resolution, and weekly consolidation: [agents/scheduler.py](/Users/ol/agents/hikari-agent/agents/scheduler.py), [agents/proactive.py](/Users/ol/agents/hikari-agent/agents/proactive.py);
- it has a practical **Telegram operator cockpit**: `/memory`, `/status`, `/tools`, `/capabilities`, `/approvals`, `/proactive`, `/settings`, `/reminders`, `/checkin`, and more: [agents/telegram_bridge.py](/Users/ol/agents/hikari-agent/agents/telegram_bridge.py);
- it has meaningful test coverage: **196 test files** under [tests](/Users/ol/agents/hikari-agent/tests).

### What Hikari already does better

1. **Better trust boundary.** Hikari is explicitly one-user and one-relationship. That is a product advantage, not a limitation.
2. **Better personal memory model.** Hermes’ `MEMORY.md`/`USER.md` are simple and cheap; Hikari’s DB-backed facts, episodes, open loops, approvals, and feedback are more durable and more product-relevant.
3. **Better internal-control isolation.** The split between `run_user_turn`, `run_visible_proactive`, and `run_internal_control` is cleaner than the usual “everything mutates the same live session” pattern.
4. **Better prompt-injection posture.** Registry-level `untrusted_output`, post-tool wrapping, and gatekeeper gating are a stronger default than “just give the agent tools and a sandbox knob.”
5. **Better companion UX.** Hikari’s proactive scheduler, emotional state, weekly consolidation, and day-receipt/link-shelf utilities are closer to an actual long-term personal assistant than either competitor’s generic platform posture.

### Where Hikari is behind

1. **Workflow/skill discovery is too implicit.**
2. **Background work is under-explained to the user.**
3. **Execution profiles are not first-class enough.**
4. **Project/workdir-scoped scheduled work is weaker than Hermes.**
5. **Reusable workflow telemetry/curation is weaker than Hermes and OpenClaw.**

## 6. Comparison table: Hermes vs OpenClaw vs Hikari

| Dimension | Hermes Agent | OpenClaw | Hikari |
|---|---|---|---|
| Product thesis | Broad general-purpose agent runtime | Gateway/control plane for personal agents | Single-user Telegram companion |
| Primary surfaces | CLI/TUI + messaging gateway + dashboard | Many channels + CLI/control UI | Telegram + internal operator/runtime surfaces |
| Memory model | Bounded `MEMORY.md` + `USER.md` + session search | Session/stateful runtime + skill snapshots + config | Structured SQLite facts/messages/episodes/tasks/approvals/tool telemetry |
| Skills/workflows | Strong; agent-managed skills and Curator | Strong; precedence rules, allowlists, managed installs | Present, but hidden and under-productized |
| Background work | `/background` + `delegate_task` | `sessions_spawn` + subagents + focus/unfocus | Internal control path + subagents + background-task state |
| Scheduling | Excellent; skill-backed cron, workdir/profile pins, no-agent scripts | Strong; cron with session binding and delivery control | Strong for personal routines/reminders; weaker as a general scheduled-job system |
| Execution backends | Very broad: local, Docker, SSH, Singularity, Modal, Daytona, Vercel | Host/sandbox/node with Docker, SSH, OpenShell | Mostly local + gated tools; no equally explicit execution matrix |
| Safety posture | Moderate; breadth raises surface area | Advanced docs and knobs, but defaults still demand care | Best current balance of narrow scope + guardrails |
| Channel strategy | Multi-platform | Multi-platform, channel-native | Single-platform by design |
| Best fit | Tinkerers/operators who want a full agent platform | Operators who want channel routing and runtime control | A durable, personal, high-trust assistant |

## 7. Product lessons

1. **Stay narrow on purpose.** Hikari should stay “one trusted person, one companion” instead of turning into a general gateway.
2. **Make workflows visible.** Both competitors are better at saying “here are the things I know how to do.” Hikari needs a first-class workflow registry.
3. **Background work should feel like a product, not an implementation detail.** Hermes and OpenClaw both make long-running work legible.
4. **Scheduling should be user-shaped, not just system-shaped.** Hikari’s internal scheduler is strong, but user-defined recurring jobs are still weaker than Hermes/OpenClaw.
5. **Companion UX is Hikari’s moat.** Hermes and OpenClaw are broader. Hikari should be deeper.

## 8. Architecture lessons

1. **Keep Hikari’s runtime split.** This is one of the cleanest things in the repo and should not be sacrificed for genericity.
2. **Add execution profiles as first-class architecture.** Hermes proves backend abstraction is useful; OpenClaw proves safety knobs matter. Hikari should expose a smaller, saner version.
3. **Turn skills/workflows into versioned artifacts.** Hermes’ skills + Curator and OpenClaw’s precedence/allowlists both point to the same lesson: reusable workflows need metadata, lifecycle, and observability.
4. **Separate personal memory from project memory.** Hikari’s current structured memory is excellent for the user relationship; it now needs a cleaner project/workspace layer.
5. **Make scheduled jobs explicit objects.** Hermes’ cron model is stronger because jobs are inspectable, editable, and routable.

## 9. UX lessons

1. **Add a `/skills` or `/workflows` surface.** The user should be able to see Hikari’s specialties, not just trigger them by accident or lore.
2. **Add a background-task inbox.** Users need “what’s running, what finished, what failed, what can I retry?”
3. **Show trust tier and blast radius in plain language.** OpenClaw’s safety knobs are too operator-heavy, but the underlying idea is right.
4. **Keep commands compact and human.** Hikari’s Telegram cockpit is already good; it should become more discoverable, not more sprawling.
5. **Preserve the personal feel.** Hermes/OpenClaw often feel like platforms first. Hikari should keep relationship continuity front and center.

## 10. Tool/safety lessons

1. **Default-deny mutating power for anything sourced from untrusted content.**
2. **Keep explicit taint propagation.** Hikari’s untrusted-output wrapping is worth doubling down on.
3. **Do not market sandboxing as solved safety.** OpenClaw’s docs are right to be careful here.
4. **Separate read-only, mutating, and high-risk execution paths.**
5. **Make approvals provenance-aware.** “This action was requested after reading untrusted content from X” should be visible at approval time.
6. **Treat plugin/skill ecosystems as supply-chain surface area, not free magic.**

## 11. 15 ranked Hikari backlog items

1. **Chat-native workflow registry (`/workflows` or `/skills`).**  
   Why: Hermes and OpenClaw both make reusable capabilities visible. Hikari currently hides too much behind repo structure and lore.

2. **Background task inbox with inspect/cancel/retry.**  
   Why: Hermes’ `/background` and OpenClaw’s subagent/task visibility both make long-running work legible.

3. **Execution profiles (`read-only`, `personal`, `project-safe`, `remote`).**  
   Why: Hikari needs a smaller, more opinionated version of Hermes backends/OpenClaw sandbox modes.

4. **Project/workdir-scoped jobs and runs.**  
   Why: Hermes’ workdir-aware cron is a real advantage for code/research tasks.

5. **Workflow telemetry + curator-lite.**  
   Why: Hikari needs to know which skills/workflows are used, stale, redundant, or risky.

6. **Project memory lens separate from personal memory.**  
   Why: Hikari’s personal memory is strong; project-specific recall should not muddy the relationship layer.

7. **Approval UI with provenance and risk tier.**  
   Why: Surface whether a tool action follows untrusted input, touches external systems, or escapes a safer profile.

8. **Specialist registry surfaced as a product feature.**  
   Why: Hikari already has invisible specialists; users need a lightweight way to discover and intentionally invoke them.

9. **User-defined scheduled jobs beyond reminders.**  
   Why: Hermes/OpenClaw both expose agentic automation better than Hikari currently does.

10. **Remote execution backend for risky work.**  
    Why: Some coding/research tasks should happen away from Hikari’s own host state.

11. **Session-side ephemeral helper thread or side-question mode.**  
    Why: OpenClaw’s session/subagent model shows the value of temporary side work that does not pollute the main flow.

12. **Workflow packaging/export/import.**  
    Why: The best Hikari skills should be movable, inspectable, and auditable.

13. **Better `/capabilities` output grouped by trust tier and domain.**  
    Why: Users should see “memory,” “calendar,” “research,” “files,” “notes,” “automation,” not just raw command names.

14. **Operator analytics dashboard for proactive sends, approvals, failures, and tool-call trends.**  
    Why: Hikari already stores the right telemetry; it needs a better surface.

15. **Artifact-delivery rules for images/files/audio.**  
    Why: Hermes’ document/media delivery conventions point to a real UX gap around how Hikari ships generated artifacts back to the user.

## 12. What not to copy

- **Do not copy multi-channel sprawl as a roadmap.** Hikari’s coherence comes from being intentionally narrow.
- **Do not copy giant default tool surfaces in conversational channels.**
- **Do not copy “self-improving” branding unless the review loop is inspectable.**
- **Do not copy public plugin/skill marketplace gravity into a personal companion without a much stronger trust story.**
- **Do not copy YOLO / bypass-approval ergonomics into the main user relationship.**
- **Do not copy the fantasy that sandboxing turns an agent platform into a safe multi-tenant boundary.**

## 13. Open questions / claims that could not be verified

1. **Hermes automatic skill creation defaults.** Official docs clearly describe agent-managed skills and Curator, but I did not verify exactly when skill creation fires automatically in normal use versus being prompted or tool-invoked.
2. **Hermes “self-improvement” magnitude.** Official docs confirm the mechanism family; they do not quantify how much performance improves over time in practice.
3. **Hermes memory backend details in some secondary reviews.** One secondary review describes ChromaDB-style episodic memory. Current official memory docs instead document `MEMORY.md` / `USER.md` plus session search. I treat the ChromaDB claim as unverified for the current official product state.
4. **OpenClaw adoption scale claims.** Secondary language such as “most widely deployed” or specific GitHub-star milestones was not treated as verified product evidence here.
5. **OpenClaw incident/CVE narratives outside official docs.** I did not rely on press coverage of specific vulnerabilities unless an official or academic source was available. The product’s own security docs and the independent arXiv paper were enough to establish the architectural risk posture.
6. **Exact size/quality of the OpenClaw third-party skill ecosystem.** The official docs clearly show the ecosystem model; I did not independently validate skill quality or security across the broader registry.

