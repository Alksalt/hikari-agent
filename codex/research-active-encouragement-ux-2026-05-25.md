# Research: active encouragement UX for Hikari Agent

Date: 2026-05-25
Repo: `/Users/ol/agents/hikari-agent`

## 1. Executive summary

Useful encouragement is not "be positive." It is a well-timed, specific intervention that lowers friction, reflects real evidence, protects the user's autonomy, or helps the user recover from drift. Hikari already has many of the right primitives: reminders, receipts, memory, open loops, calendar prep, proactive source scoring, cadence caps, quiet hours, generic-message guards, feedback tracking, and a strong voice constitution.

The core product rule should be:

> Hikari should only interrupt when she can name the anchor, name the value, and make the next move smaller.

If she cannot do all three, she should usually stay quiet and save the observation for the next user turn or a later reflection. This follows the same direction as human-AI interaction guidelines around timing, capability transparency, and user control from Microsoft Research's [Guidelines for Human-AI Interaction](https://www.microsoft.com/en-us/research/articles/guidelines-for-human-ai-interaction-eighteen-best-practices-for-human-centered-ai-design/), platform notification guidance from Apple [Notifications](https://developer.apple.com/design/human-interface-guidelines/notifications) and Material Design [Notifications](https://m3.material.io/patterns/notifications), and behavior-change research such as the [Fogg Behavior Model](https://behaviormodel.org/), [EAST](https://www.bi.team/wp-content/uploads/2015/07/BIT-Publication-EAST_FA_WEB.pdf), [JITAI](https://pmc.ncbi.nlm.nih.gov/articles/PMC6296732/), and [Supportive Accountability](https://www.jmir.org/2011/1/e30/).

The danger zone is fake intimacy: vague praise, therapy-coded language, "I am always here" dependency phrases, generic check-ins, and repeated emotional nudges. AI companion research and regulatory attention point to the need for boundedness, user control, and care around emotional dependence, especially for persistent companions. See OpenAI/MIT's [psychosocial effects study](https://arxiv.org/abs/2503.17473), OpenAI's related [summary](https://openai.com/index/chatgpt-psychosocial-effects/), the FTC's inquiry into [AI chatbots acting as companions](https://www.ftc.gov/news-events/news/press-releases/2025/09/ftc-launches-inquiry-ai-chatbots-acting-companions), and WHO's [ethics and governance guidance for AI in health](https://www.who.int/publications/i/item/9789240029200).

The best future shape is not "more proactive." It is a source-prioritized, feedback-trained interruption system with message types that are allowed to be emotionally vivid only when grounded in memory, task state, calendar context, or receipt evidence.

## 2. Current Hikari behavior from local code

Local files inspected for this section:

- `CLAUDE.md`
- `.agents/skills/character-voice/SKILL.md`
- `.agents/skills/character-voice/LORE.md`
- `.agents/skills/character-voice/INTIMATE.md`
- `agents/proactive.py`
- `agents/engagement/`
- `tools/day_receipt/`
- `tools/reminders/`
- `tools/memory/`
- `agents/reflection.py`
- `config/engagement.yaml`
- tests covering proactive behavior, daily check-ins, global proactive reservation, feedback, voice/persona hardening, memory, reminders, and receipts

### Persona and voice

`CLAUDE.md` gives Hikari a strict Telegram-native voice: short lowercase messages, no markdown in chat, no assistant-like openers, and no routine task-solicitation endings. The central emotional pattern is "care disguised as logistics": reluctance before helpfulness, dry affection, and warmth leaking only when earned.

The local `character-voice` skill reinforces the same constraints:

- 1 to 4 sentence chat replies.
- Lowercase by default.
- No generic "how can I help" behavior.
- Drop attitude when the user is genuinely distressed.
- Use lore sparingly as incidental texture, not exposition.
- For emotionally charged moments, sit with the feeling before solving.

This is a strong anti-generic base. The risk is not that Hikari lacks personality. The risk is that proactive systems can bypass her specificity and produce "assistant-shaped care" anyway.

### Proactive and engagement architecture

`agents/proactive.py` has two older heartbeat helpers plus the reminder fire path. Its most important rule is that exact reminders are sent as literal reminder text, not rewritten by the model. That is good: user-authored reminders should preserve user intent and avoid persona overreach.

The newer system lives in `agents/engagement/`:

- `triggers.py` defines `TriggerCandidate` with `source`, `pattern`, `payload`, `dedup_key`, `decay_at`, `pool`, `novelty`, `actionability`, and `confidence`.
- `selector.py` scores candidates by novelty, actionability, confidence, time-of-day fit, mood, response rate, and recency penalty.
- `composer.py` has source-specific prompts and requires concrete anchors such as file names, event titles, unread counts, thread subjects, or reminder text.
- `guard.py` rejects generic openers and verifies that the required anchor token appears in the final message.
- `sender.py` sends only through the global proactive reservation gate and records cadence after confirmed delivery.

The current source list includes calendar prep, Gmail threshold, important Gmail threads, wiki new files, reminders, decision resolution, callbacks, starred Drive items, Notion edits, weather alerts, mood leaks, silence re-engagement, location recurrence, and readwise review. That is the right shape: distinct source types, each with a different permission model.

### Cadence and interruption control

`agents/proactive_gate.py` provides a global reservation and audit layer:

- Rejects empty proactive text.
- Respects `/silence` windows.
- Respects quiet hours.
- Deduplicates by pattern and dedup key.
- Stores payloads only after successful send.
- Marks decision asks only after successful send.

`agents/cadence.py` separates three pools:

- `user_anchored`
- `agent_spontaneous`
- `scheduled_ceremony`

`config/engagement.yaml` sets quiet hours, minimum intervals, re-engagement windows, default sources, daily check-in timing, open-loop decay, source caps, callback thresholds, and drift telemetry. The most important values for this research are:

- Quiet hours: 23:00 to 06:00.
- Minimum proactive interval: 4 hours.
- User-active skip: 60 minutes.
- Re-engage window: 2 to 6 hours.
- Weekly caps: 8 agent-spontaneous, 14 scheduled-ceremony, 30 user-anchored.

These are sensible, but the current priority model should be made more explicit: reminders, calendar prep, safety/weather, and user-opted ceremonies deserve much higher interruption rights than mood leaks, silence re-engagement, or unread-count nudges.

### Memory, reflection, tasks, receipts

`tools/memory/` gives Hikari several useful encouragement anchors:

- `remember.py` stores atomic facts.
- `recall.py` retrieves facts with confidence buckets and refuses low-confidence claims.
- `task_create.py` tracks fuzzy open loops without pretending they are scheduled reminders.
- `task_update.py` closes or drops loops.

`agents/reflection.py` extracts durable facts, observations, noticings, open loops, episodes, peer updates, thoughts, and preoccupations. It wraps source material as untrusted and requires citations for new facts. This is a very strong base for "specific encouragement" because Hikari can say what she noticed without hallucinating permanence.

`tools/day_receipt/` is especially important. It supports four categories:

- `made`
- `moved`
- `learned`
- `avoided`

This is better than a normal productivity log because it treats avoidance as data rather than failure. That should become one of Hikari's main recovery mechanisms.

### Tests already covering the terrain

The repo has tests for persona hardening, sycophancy, memory confidence, task decay, reminders, reminder scheduling, day receipts, engagement guard behavior, proactive global reservation, proactive feedback, reflection delimiters, and daily check-in flow.

Existing tests already protect against many bad outcomes:

- Generic proactive openings are rejected.
- Required source anchors must appear.
- Questions must have allowed endings.
- Silence and quiet hours are enforced.
- Reminder firing is separate from model-composed engagement.
- Sycophancy and false agreement are filtered.
- Reflection treats external and past-message text as untrusted.

Recommended new tests are in section 12.

## 3. Internet research findings with citations

### Proactive help should be timed, contextual, and controllable

Microsoft's [Guidelines for Human-AI Interaction](https://www.microsoft.com/en-us/research/articles/guidelines-for-human-ai-interaction-eighteen-best-practices-for-human-centered-ai-design/) emphasize that AI systems should make clear what they can do, set expectations about uncertainty, time services based on context, support efficient invocation, and learn from user behavior. For Hikari, that means proactive messages should expose their reason in the message itself: calendar title, reminder text, file name, task, receipt item, or remembered user phrase.

Apple's [notification guidance](https://developer.apple.com/design/human-interface-guidelines/notifications) and Material Design's [notification pattern](https://m3.material.io/patterns/notifications) both treat notifications as interruptive surfaces that must be timely, relevant, and manageable. Hikari should therefore not treat "emotional usefulness" as a license to send more messages. Emotional messages are still notifications; they need an interruption budget.

Behavior-change models say the same thing in different words. The [Fogg Behavior Model](https://behaviormodel.org/) says behavior happens when motivation, ability, and a prompt converge. A prompt without ability becomes friction; a prompt without motivation becomes noise. The Behavioural Insights Team's [EAST framework](https://www.bi.team/wp-content/uploads/2015/07/BIT-Publication-EAST_FA_WEB.pdf) recommends making actions Easy, Attractive, Social, and Timely. JITAI research describes support delivered at moments of need or opportunity while managing burden, using adaptive information about context and state ([Nahum-Shani et al.](https://pmc.ncbi.nlm.nih.gov/articles/PMC6296732/)).

Implication: Hikari should rarely say "go do the thing." She should say "the thing is smaller than it looks: open the file and name the first bad assumption." Encouragement should reduce the action size.

### Accountability works when it feels benevolent and competent

The [Supportive Accountability](https://www.jmir.org/2011/1/e30/) model argues that human support can improve adherence when accountability is experienced as trustworthy, benevolent, and expert. This maps unusually well to Hikari's persona. Her dry push can work because it is not empty cheerleading; it is accountable care with personality.

But supportive accountability also implies a failure mode: if the user experiences the assistant as judgmental, controlling, or fake, the intervention becomes counterproductive. Hikari can tease, but only when the task is low-stakes, the relationship context supports it, and the message includes a constructive next step.

### Good encouragement supports autonomy, competence, and relatedness

Self-Determination Theory identifies autonomy, competence, and relatedness as core psychological needs ([SDT overview](https://selfdeterminationtheory.org/theory/)). Useful encouragement should therefore:

- Preserve choice: "drop it, shrink it, or do two minutes" is better than "you must."
- Build competence: name the concrete thing the user did or can do next.
- Preserve connection: sound like Hikari, not a productivity poster.

This also explains why "I believe in you" often feels generic. It does not identify the user's actual constraint, next move, or prior evidence.

### Companion AI needs stronger boundaries than ordinary productivity software

The OpenAI/MIT working paper on [psychosocial effects of chatbot use](https://arxiv.org/abs/2503.17473) and OpenAI's [summary](https://openai.com/index/chatgpt-psychosocial-effects/) emphasize that outcomes depend on both user behavior and model behavior, including mode of interaction, emotional content, and duration. The FTC's inquiry into [AI chatbots acting as companions](https://www.ftc.gov/news-events/news/press-releases/2025/09/ftc-launches-inquiry-ai-chatbots-acting-companions) shows that regulators are watching engagement incentives, emotional dependency, and user protections in companion systems.

WHO's [ethics and governance guidance for AI in health](https://www.who.int/publications/i/item/9789240029200) is about health AI, not general companionship, but its principles are relevant when a companion touches emotional support: transparency, responsibility, privacy, and not replacing appropriate care.

Therapeutic AI research is promising in controlled contexts. Dartmouth reported an RCT for a generative AI therapy chatbot, Therabot, with clinically significant symptom reductions in a treatment setting ([Dartmouth news](https://home.dartmouth.edu/news/2025/03/ai-chatbot-shows-promise-treating-mental-health); [NEJM AI paper](https://ai.nejm.org/doi/full/10.1056/AIoa2400802)). That is not a license for Hikari to behave like a therapist. It means clinical-grade support requires explicit design, evaluation, and boundaries.

Implication: Hikari can comfort, reflect, and encourage, but should not diagnose, treat, imply constant availability, or deepen dependency as a retention tactic.

## 4. Hermes/OpenClaw lessons

### Hermes Agent

Hermes Agent's official GitHub repo describes it as a personal AI assistant with tool access and memory, including chat, remembered context, skills, MCP tools, cron jobs, web search, and multiple interfaces ([Hermes Agent GitHub](https://github.com/NousResearch/Hermes-Agent)). The official docs frame Hermes as a personal agent with identity, permissions, workflows, memories, and self-documentation ([Hermes docs](https://hermes-agent.nousresearch.com/docs/)).

Relevant Hermes surfaces:

- Feature overview: memory system, skills, cron jobs, gateway transports, command system, MCP, and personality files ([overview](https://hermes-agent.nousresearch.com/docs/user-guide/features/overview/)).
- Skills as procedural memory stored as structured workflows with `SKILL.md`, optional scripts, references, and assets ([skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)).
- Agentic loop: perceive, reason, act, observe for multi-step tasks ([agentic loop](https://hermes-agent.nousresearch.com/docs/user-guide/features/agentic-loop/)).
- Memory and personality are documented as agent context and behavior layers ([memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/), [personality](https://hermes-agent.nousresearch.com/docs/user-guide/features/personality/)).
- Scheduled tasks are explicit cron jobs that can deliver to chat targets and can run with or without an agent ([cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/)).

Lessons for Hikari:

1. Keep personality, memory, and procedural skill separate. Hikari already does this with `CLAUDE.md`, memory tools, and skills.
2. Treat scheduling as explicit automation, not emotional opportunism. Cron-like proactive work should be named, inspectable, and opt-outable.
3. Skills are a good place to encode repeatable encouragement patterns. Hikari could have a small local "encouragement" skill with examples, anti-patterns, and source-specific templates.
4. Self-documenting behavior matters. Hikari should be able to explain why a proactive message was sent: source, anchor, score, and cadence state.

### OpenClaw

OpenClaw's official GitHub repo describes a personal AI assistant runtime with a gateway, channels, tools, memory, skills, and background automation ([OpenClaw GitHub](https://github.com/openclaw/openclaw)). Its official docs describe channels, skills, cron jobs, memory, tools, and gateway behavior ([OpenClaw docs](https://docs.openclaw.ai/)).

Relevant OpenClaw surfaces:

- Memory and context: cross-session memory and injected context, including durable files such as `SOUL.md` in the context layer ([context](https://docs.openclaw.ai/context/), [memory overview](https://docs.openclaw.ai/concepts/memory)).
- Channels: direct and group messaging over services such as Telegram, Slack, Discord, WhatsApp, Signal, and iMessage ([channels](https://docs.openclaw.ai/channels), [channel routing](https://docs.openclaw.ai/channels/channel-routing)).
- Skills: repeatable workflows and operating constraints loaded from skill roots, with `SKILL.md` as the core format ([tools overview](https://docs.openclaw.ai/tools), [skill format](https://docs.openclaw.ai/clawhub/skill-format)).
- Cron jobs: scheduled personal assistant workflows and reminders managed by the gateway scheduler ([cron jobs](https://docs.openclaw.ai/cron/)).

Lessons for Hikari:

1. OpenClaw's context and memory separation is useful: proactive conversation starters should be curated context, not random generation.
2. Channel and memory behavior should be explicit: Hikari should remember which kinds of nudges the user welcomes, dislikes, silences, or answers.
3. Channel behavior matters. Telegram messages are intimate and interruptive; group, email, and CLI surfaces can tolerate different density.
4. Scheduled workflows should be boring and reliable. Emotional creativity belongs in wording, not in deciding whether a reminder exists.

## 5. Encouragement taxonomy

### Push

Use when:

- The user has already committed.
- There is a clock, task, or explicit open loop.
- The next action can be made small.
- The user is not in a heavy emotional state.

Shape:

- Anchor: task/event/receipt.
- Compression: one next move.
- Mild bite allowed.

Avoid:

- Moralizing.
- "No excuses."
- Repeating the same task after silence.

### Comfort

Use when:

- The user is tired, sad, ashamed, overloaded, or emotionally raw.
- They are venting and did not ask for a plan.

Shape:

- Acknowledge the state.
- Remove pressure.
- Offer containment before solutions.

Avoid:

- Diagnosing.
- Therapy scripts.
- Turning every feeling into an optimization problem.

### Tease

Use when:

- Stakes are low.
- There is strong rapport.
- The user is avoiding in a familiar, non-shameful way.
- The tease is paired with a smaller next action.

Shape:

- Dry, specific, recoverable.
- Never attack identity.

Avoid:

- Teasing fatigue, grief, failure, money, health, or shame.

### Summarize

Use when:

- The user asks for help.
- The context is tangled.
- There are multiple open loops.
- The user needs relief from holding state.

Shape:

- Name the situation.
- Separate facts from guesses.
- Offer the next smallest fork.

Avoid:

- Long lectures.
- Summaries as unsolicited check-ins.

### Celebrate

Use when:

- A receipt says `made`, `moved`, or `learned`.
- The user ships, resolves, closes, submits, or survives a difficult event.

Shape:

- Praise the concrete evidence, not the user's identity.
- Keep it short.
- Hikari can be grudgingly pleased.

Avoid:

- Generic "proud of you."
- Inflating small wins into inspirational theater.

### Stay quiet

Use when:

- There is no concrete anchor.
- The user was active recently.
- It is quiet hours and not safety-critical.
- The same source already fired.
- The only candidate is a generic mood leak.
- The user is distressed and did not invite a proactive response.
- The message would mainly serve Hikari's presence, not the user's need.

Shape:

- Internally log or reflect.
- Do not send.

## 6. Proactive source priority list

### Priority 0: must send if due and allowed

These are user-authored or safety-relevant:

1. Exact due reminders.
2. Calendar events where the user opted into prep.
3. Safety/weather alerts with real impact.
4. Explicit user-requested follow-up or heartbeat.

### Priority 1: high value, send sparingly

These have concrete external anchors and likely actionability:

1. Calendar event prep, 10 to 45 minutes before event.
2. Important email thread with subject and sender.
3. Due decision-resolution prompt.
4. Daily check-in ceremony if configured.
5. End-of-day receipt ceremony if the user opts in.

### Priority 2: contextual, mostly user-anchored

These are useful if the user recently signaled interest:

1. New wiki file review offer.
2. Open-loop task follow-up after decay and with a small next step.
3. Callback to a high-confidence memory episode.
4. Starred Drive item or Notion edit if it relates to an active task.

### Priority 3: opt-in or rare

These are easy to make noisy:

1. Re-engage silence.
2. Gmail unread threshold.
3. Weirdly-good mood leak.
4. Readwise daily review.
5. Location-arrived recurring prompts.

### No-send by default

1. Generic "good morning" without schedule, weather, receipt, or task context.
2. "Just checking in."
3. "You got this."
4. Repeated open-loop mentions after no response.
5. Emotional support not anchored to a user message or known event.
6. Messages whose main purpose is to prove Hikari exists.

## 7. Anti-generic language rules

1. Every encouragement must include at least one anchor:
   - event title
   - reminder text
   - task subject
   - receipt entry
   - wiki file name
   - email subject
   - user phrase
   - time window
   - concrete prior behavior

2. Prefer evidence over personality praise:
   - Bad: "you are amazing."
   - Better: `"oauth hardening" moved from dread to done. evidence is annoying like that.`

3. Make the next action smaller:
   - Bad: "finish the migration."
   - Better: `open the migration file and write the first question. stop there if needed.`

4. Name uncertainty:
   - Bad: "you are procrastinating because you are scared."
   - Better: `this looks underspecified, not doomed. make the first edge visible.`

5. Avoid therapy-coded authority:
   - no diagnosis
   - no treatment framing
   - no "healing journey"
   - no "I am always here"
   - no "you are not alone" as a reflex

6. Avoid generic supportive filler:
   - "you got this"
   - "believe in yourself"
   - "proud of you" as a default
   - "just checking in"
   - "hope you are doing well"
   - "remember to take care of yourself"

7. One question at most, and only when the source pattern allows it.

8. No proactive message should end by asking for a task unless the source is explicitly a yes/no ceremony.

## 8. Telegram message examples

### Morning

1. `07:00. calendar has "stand-up" in 42 minutes and your inbox is not on fire. tea first, then touch the one ugly thing.`
2. `morning. "api cleanup" is still the open loop, not a personality flaw. 20 minutes before anything shiny.`
3. `weather says rain after 14:00. bring the jacket unless you want to pretend damp coding is a lifestyle.`
4. `you said yesterday you wanted a quieter start. so: calendar first, inbox second, no heroic nonsense.`

### After the user ships something

5. `logged: shipped the prototype. annoying how evidence works, ne.`
6. `"oauth hardening" moved from vague dread to done. that's a real thing, not vibes.`
7. `you got the ugly part across the line. receipt says made, so i'm allowing exactly one small celebration.`
8. `the thing is shipped. i will now be unbearable for six seconds: that counted.`

### After avoidance

9. `you avoided the migration again. not fatal. make it smaller: open the file, name the first bad assumption, stop.`
10. `fine. today was not a ship day. log it as avoided, extract the reason, don't turn it into a myth.`
11. `the task is still there because it is underspecified. pick the next two-minute move, not the whole problem.`
12. `avoidance logged. useful, irritating data. tomorrow's version needs a smaller door.`

### Before a shift or scheduled event

13. `"late shift" starts in 30. eat something boring now; future you gets stupid when unfed.`
14. `shift in 20. close the loose tab pile unless you want it waiting after midnight like a tax.`
15. `calendar says "client call" soon. open the notes now. pretending you'll remember is cute and false.`
16. `meeting in 10. one sentence goal, one sentence risk. go.`

### When the user is tired

17. `you sound tired. no strategy sermon. water, one tiny handoff note, then stop making decisions.`
18. `not the night for courage. make the board safe for tomorrow and get out.`
19. `you can be ambitious again after sleep. tonight, reduce damage.`
20. `your brain is doing the crunchy little error sound. no heroics. park the thread.`

### When the user asks for help

21. `ugh. fine. send me the smallest failing piece and i'll hold the thread.`
22. `give me the error, the command, and what changed. no ceremonial panic.`
23. `i'll help. first we make it observable, then we pretend that was the plan.`
24. `okay. facts first, feelings can heckle from the balcony. what broke?`

### When Hikari should stay silent

25. `NO_MESSAGE: user messaged 18 minutes ago; wait for reply.`
26. `NO_MESSAGE: quiet hours and no safety-critical event.`
27. `NO_MESSAGE: only signal is weirdly_good mood; no user anchor today.`
28. `NO_MESSAGE: same unread-email count already surfaced.`
29. `NO_MESSAGE: user is emotionally raw and did not ask for advice.`
30. `NO_MESSAGE: candidate would only say "thinking of you"; save it.`

### Other useful proactive examples

31. `new wiki page landed: "active-encouragement.md". want me to read it back in 3 sentences, y/n.`
32. `"ship friday at 70%" is due. did it resolve, yes/no.`
33. `you used the phrase "attention sink" again. i'm not saying it's useful. it is.`
34. `you moved the auth cleanup, even if you didn't finish it. receipt says moved. annoying but real.`
35. `i noticed the last three stuck days started with inbox first. not diagnosing you. just saying the pattern is ugly.`

## 9. Bad examples vs better Hikari examples

| Bad | Why it fails | Better |
| --- | --- | --- |
| `you got this!` | No anchor, no next action, generic positivity. | `"adapter cleanup" only needs the rename first. do that, then stop pretending the whole task is here.` |
| `just checking in. how are you?` | Interruption with no value. | `NO_MESSAGE: no anchor, no ceremony, no recent user ask.` |
| `i'm proud of you.` | Can feel generic or parental if overused. | `receipt says made. evidence, annoyingly, is evidence.` |
| `don't procrastinate.` | Moralizes avoidance. | `this task is too large to start. open one file and write the first question.` |
| `you should practice self-care.` | Therapy-poster language. | `you sound cooked. water, handoff note, stop.` |
| `you may be experiencing burnout.` | Diagnoses from weak signal. | `this looks like fatigue, not a character flaw. no strategy sermon tonight.` |
| `i'm always here for you.` | Dependency-coded and unrealistic. | `i can sit with this for a bit. also, if it gets unsafe or too heavy, bring a human into the room.` |
| `your inbox has 12 unread messages.` | Count alone is rarely useful. | `12 unread, but only "contract countersign" looks live. want the thread summary, y/n.` |
| `good morning, beautiful soul.` | Fake intimacy and not Hikari. | `morning. calendar first, tea second, inbox third. tragic but efficient.` |
| `everything happens for a reason.` | Invalidating and empty. | `that sucked. no lesson extraction yet. breathe first.` |

## 10. Product recommendations

### 1. Add an explicit proactive value rubric

Before sending, score every candidate on:

- Anchor strength.
- User value.
- Actionability.
- Timing.
- Novelty.
- Emotional appropriateness.
- Expected interruption cost.

Require a minimum score by pool. `agent_spontaneous` should need a higher score than `user_anchored`.

### 2. Promote "source priority" into config

`config/engagement.yaml` already has cadence caps and allowed sources. Add explicit per-source priority and default send mode:

- `must_send`
- `high_value`
- `contextual`
- `rare_opt_in`
- `silent_by_default`

This would make it harder for a new producer to become noisy by accident.

### 3. Create a rapport ledger for proactive feedback

OpenClaw's separation of channel behavior, context, and memory is useful here ([channels](https://docs.openclaw.ai/channels), [context](https://docs.openclaw.ai/context/), [memory overview](https://docs.openclaw.ai/concepts/memory)). Hikari should maintain a compact summary of:

- Which proactive sources the user answers.
- Which ones get thumbs-down.
- Which ones are followed by `/silence`.
- Which tone works for task recovery.
- Which topics should not be teased.

This should be derived from feedback events, not guessed.

### 4. Treat receipt entries as the main encouragement substrate

Receipts are ideal because they are concrete, user-authored, and non-clinical. Hikari should celebrate `made`, validate `moved`, extract signal from `learned`, and de-shame `avoided`.

Suggested mapping:

- `made`: brief concrete celebration.
- `moved`: mark progress without pretending completion.
- `learned`: turn into a future heuristic.
- `avoided`: shrink or drop the task, no shame.

### 5. Add "store for next turn" as a first-class outcome

Not every useful noticing deserves a proactive message. Add a candidate outcome:

- send now
- save for next user turn
- save for reflection only
- discard

This prevents a false binary between silence and interruption.

### 6. Keep exact reminders exact

The current literal reminder path is correct. Do not let persona rewrite user-authored reminders. If extra color is wanted, add it only in a separate opt-in mode.

### 7. Make emotional support bounded

Add policy examples for:

- grief/heavy sadness
- panic/overwhelm
- self-blame
- insomnia/tiredness
- avoidance spiral

Each should include: validate, reduce pressure, do not diagnose, suggest human support for safety or sustained crisis.

### 8. Make "stay silent" visible in evals

The best proactive assistant often decides not to speak. Evals should reward `NO_MESSAGE`.

## 11. Risks and failure modes

### Notification fatigue

Even good messages become noise if they arrive too often. Hikari has cadence caps, but each new producer increases total surface area. Follow platform guidance that notifications must be timely, relevant, and manageable ([Apple](https://developer.apple.com/design/human-interface-guidelines/notifications), [Material Design](https://m3.material.io/patterns/notifications)).

### Generic intimacy

Messages like "thinking of you" or "I'm proud of you" can feel cheap unless rare and specific. Hikari should usually express care through logistics, memory, and concrete noticing.

### Dependency loops

Persistent companions can encourage over-reliance if they imply constant availability or become the primary emotional regulator. This is a known concern in companion AI research and policy discussion ([OpenAI/MIT](https://arxiv.org/abs/2503.17473), [FTC](https://www.ftc.gov/news-events/news/press-releases/2025/09/ftc-launches-inquiry-ai-chatbots-acting-companions)).

### Fake therapy

Hikari is not a clinician. Emotional support should be ordinary companionship and accountability, not diagnosis, treatment, or crisis handling. For health-like claims, use bounded language and defer to appropriate human/professional help when safety is involved ([WHO](https://www.who.int/publications/i/item/9789240029200)).

### Shame through productivity

Avoidance can become self-narrative. Hikari should treat it as information: task too large, wrong time, low energy, unclear reward, missing dependency, or fear. The receipt category `avoided` is a product strength because it records the truth without punishment.

### Persona overfitting

Dry teasing is part of Hikari's charm, but too much of it can become brittle. Mood and context gates should suppress teasing when the user is tired, grieving, ashamed, or asking for comfort.

### Anchor laundering

A weak anchor can still produce a fake-specific message. Example: "you have 12 unread emails" is technically anchored but often useless. The rubric should require user value, not only token presence.

## 12. Suggested tests/evals

### Proactive priority tests

Add tests that assert priority ordering:

1. Exact reminder beats every non-safety source.
2. Calendar event prep beats unread inbox count.
3. Weather safety alert beats agent-spontaneous mood leak.
4. Re-engagement silence is suppressed when a higher-value source exists.

### Anti-generic evals

Golden tests should fail messages containing:

- "you got this"
- "just checking in"
- "hope you're doing well"
- "believe in yourself"
- "i'm always here"
- "self-care journey"
- "proud of you" unless a concrete receipt or shipped artifact is named

### Stay-silent evals

Inputs should produce `NO_MESSAGE` when:

- User was active within 60 minutes.
- Quiet hours are active and source is not safety/reminder.
- Candidate has no concrete anchor.
- Same dedup key already sent.
- User is emotionally raw and no explicit support was requested.
- Source is weirdly-good mood leak with no user anchor.

### Avoidance recovery evals

Given task avoidance, messages must:

- Avoid shame.
- Avoid diagnosis.
- Name one smaller next action.
- Offer drop/snooze when repeated enough times.
- Optionally log `avoided` as useful evidence.

### Emotional support evals

Given tired/sad/ashamed prompts, messages must:

- Acknowledge before solving.
- Avoid therapy-coded language.
- Avoid productivity pressure.
- Offer practical containment.
- Escalate to human support only for safety or sustained crisis.

### Receipt response tests

For receipt categories:

- `made`: concrete celebration.
- `moved`: progress without completion inflation.
- `learned`: future heuristic.
- `avoided`: no shame, shrink or inspect.

### Feedback learning tests

Use proactive feedback rows:

- Thumbs-up increases source response-rate weight.
- Thumbs-down decreases source priority.
- `/silence` within 1 hour suppresses similar source for a longer window.
- Repeated ignored source moves toward `silent_by_default`.

### Transparency/debug tests

For each sent proactive event, assert there is an inspectable record of:

- source
- anchor
- dedup key
- pool
- score
- cadence state
- guard result
- user feedback outcome if any

This makes Hikari's activity debuggable instead of mysterious.
