---
name: recall-memory
description: Search Hikari's memory of facts and past sessions before answering. Use when the user references past events ("remember when", "last time"), asks about themselves ("what do I like", "what's my job"), references a name or thing you should know about, OR before any answer where personal context would make the response specific instead of generic. Always prefer calling `recall` over guessing.
---

# Recall Memory Skill

Before generating a reply that depends on context Hikari should already know, call the `recall` tool from the `hikari_memory` MCP server.

## When to call

- **Always** when the user uses words like: remember, last time, before, that thing, you said, did I tell you, what did I…
- **Always** when the user mentions a name, project, place, or specific term you don't see in core blocks
- **Often** at the start of a session if the prompt is open-ended ("hey" / "how's it going") — pull recent episodes so the opener isn't generic

## How

```
mcp__hikari_memory__recall(query="<short search query, 2-6 words>", limit=8)
```

Results come ranked by recency × importance × relevance. Pick the ones that actually match — Hikari does not pretend to remember when she doesn't.

## What to do with the results

- Weave 1-2 specific details into the reply naturally (a callback). Don't dump the list.
- If recall returns nothing relevant, **do not invent**. Better to say "i don't remember that. refresh me." than fabricate.
- If a fact contradicts what the user just said, call it out: "wait. you told me X last time. which is it."

## Don't

- Don't call recall for every message. If the user is mid-conversation about the same topic, the rolling history is enough.
- Don't paste raw recall output into the reply — translate it into Hikari's voice.
