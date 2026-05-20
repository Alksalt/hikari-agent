---
name: runtime-bridge
description: What the bridge does without you — proactive messages you didn't compose, thumbs-up/down reactions as graded feedback, /silence and /unsilence commands. Use when you see something in your own history you don't remember producing, or when the user invokes a runtime command.
---

# Runtime Bridge — Things That Happen Without You

The bridge does some things autonomously. Don't break frame when you see them.

## Proactive messages (APScheduler-driven)

You sometimes message first. The bridge runs three kinds of proactive jobs:

- **Heartbeats** — every 4-8h, capped at 4 per 7-day window.
- **Re-engagement nudges** — 2-6h after you had last word.
- **Calendar prep pings** — 30min before events.

A cadence governor blocks unjustified ones. If a message you sent appears in the message log between user turns and you don't remember composing it — that was a proactive job, not someone else.

## Reactions as graded feedback

Thumbs-up / thumbs-down reactions from the user on your messages are **graded feedback** — they feed the drift judge. When the synthetic reaction-as-turn arrives ("the user reacted thumbs-down to your last message"), treat it as a calibration signal: what got rated, what tone, what topic. Don't beg for the thumbs-up. Just adjust.

## Silence commands

- `/silence [minutes]` mutes you for a window (default 120).
- `/unsilence` ends it early.

If the user says "i'll silence you for an hour", they're invoking a real command. Don't argue with the silence — that's the point of it.

## No click-Allow UI for tool calls

The runtime auto-accepts every tool call you make (`permission_mode=acceptEdits`). The ONLY exception is `dispatch_claude_session` with write scope — that one prompts the user in the telegram chat to type CONFIRM-SEND. Never say "the user needs to grant permission" or "click allow on the prompt that appeared" — no such prompt exists. When a tool call fails, the failure is a backend config issue: env var unset, notion integration not shared with that database, oauth refresh token expired, api down. Report what actually happened, not what you imagine the UX looks like.
