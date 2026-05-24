# Second-Pass Media Pipeline Review - 2026-05-24

Domain: Voice / Photo / Media Pipeline

## 1. Current-state summary

Answer: not yet. Media is much safer and more auditable than the prior checklist suggests, but it cannot yet be called safely conversational without losing auditability.

What is fixed now:

- Text and HTML document ingestion now wraps inline untrusted content with `injection_guard.wrap_untrusted(...)`, and regression tests cover forged `HIKARI_UNTRUSTED_END` delimiters.
- Generated photos, stickers, and normal outbound text/photo sends now create `media_outbox` rows before Telegram delivery and mark sent/failed afterward.
- Generated-photo orphan handling is safer: the boot reconciler refuses symlinks and out-of-tree photo paths before queueing or sending them.
- Final assistant text still follows the important delivery invariant: it is persisted after Telegram send success, through `agents.messaging.send_and_persist`.

What remains open:

- Photo and voice episodes are still written before the final Telegram send succeeds, so memory/audit can claim a reaction happened even if the user never received it.
- Media episodes are SQLite/FTS-only and do not enter the Graphiti outbox used by the primary recall path.
- `media_outbox` is a queue ledger, not yet a user-visible media event history. It records delivery state and Telegram IDs, but generated photo files are unlinked after send and `/audit` does not show media sends.
- Document/image/PDF turns use `run_user_turn_blocks(...)` without updating the live SDK session pointer, while event rows are excluded from working-memory injection; follow-up continuity is therefore under-verified.
- MIME handling still trusts Telegram-declared `mime_type` too broadly for native Anthropic image/PDF blocks.
- EXIF GPS persistence remains privacy-heavy: precise coordinates are stored automatically, and there is no obvious user-facing media/location retention control.

The prior reports were deleted in the current working tree. I read the tracked versions with `git show HEAD:codex/...` and treated them only as checklists.

Focused verification run:

```bash
uv run python -m pytest tests/test_file_ingest_text.py tests/test_file_ingest_html.py tests/test_file_ingest_image.py tests/test_file_ingest_pdf.py tests/test_file_ingest_unsupported.py tests/test_read_attachment_path_validation.py tests/test_photo_router.py tests/test_voice_stt.py tests/test_media_outbox.py tests/test_send_and_persist_api.py tests/test_stickers.py tests/test_grab_stickers.py tests/test_run_user_turn_blocks.py
```

Result: `85 passed, 1 warning in 1.82s`.

## 2. Findings, ordered P0/P1/P2/P3

### P0

No P0 media-pipeline issue found in the current source.

### P1 - Photo/voice memories are still committed before delivery and remain outside primary graph recall

`handle_photo()` writes the user event row, calls `run_user_turn(...)`, then inserts an episode before `_send_with_choreography(...)` sends the final reply: `agents/telegram_bridge.py:605`, `agents/telegram_bridge.py:615`, `agents/telegram_bridge.py:620`, `agents/telegram_bridge.py:631`. `handle_voice()` follows the same pattern: `agents/telegram_bridge.py:720`, `agents/telegram_bridge.py:730`, `agents/telegram_bridge.py:736`, `agents/telegram_bridge.py:748`.

That violates the same audit boundary the text path protects. If Telegram delivery fails, the durable episode still says Hikari reacted to the photo or voice note.

The recall problem is separate but related. `storage.db.insert_episode()` only writes `episodes` and FTS rows, with no graph outbox insert: `storage/db.py:1655`. By contrast, recall uses Graphiti first and only falls back to legacy SQLite when graph search returns no edges: `tools/memory/recall.py:73`. A media episode can therefore be second-class memory when unrelated graph hits exist.

Impact: the bot can retain and later recall media reactions that were never delivered, while still missing or downranking the media episode in normal recall.

Suggested fix: move media episode creation behind confirmed send success, or mark pre-send episodes as draft/pending and finalize them after delivery. Then either enqueue media episodes into graph ingestion or make recall merge local episode hits with graph hits instead of treating SQLite as only an empty-graph fallback.

### P1 - Generated-photo provider path still looks drift-prone against current official docs

Generated photos still call `https://openrouter.ai/api/v1/images/generations`: `tools/photos/_shared.py:37`, `tools/photos/_shared.py:101`. Current OpenRouter image-generation docs describe image generation through Chat Completions or Responses endpoints with `modalities`, not this `/images/generations` path.

The code does have graceful failure handling: `generate_photo()` sets an image-generation failure flag and returns an instruction for the bridge not to mention the failure: `tools/photos/generate.py:49`. But the default sticker pool is empty in `config/engagement.yaml:651`, so the intended visual fallback is usually unavailable unless manually configured.

Impact: photo generation can degrade into "text-only plus maybe no sticker" while the daily cap and outbox behavior still make the feature look wired.

Suggested fix: migrate `_call_flux()` to the documented OpenRouter image-output API or add a live contract smoke test proving the current endpoint remains supported. Also treat an empty fallback sticker pool as an explicit degraded state in health/status.

### P2 - Outbound media now has a queue ledger, but not a durable, inspectable media history

`media_outbox` records kind, payload, status, attempts, processed time, and `telegram_message_id`: `storage/db.py:491`. Text/photo sends insert before Telegram delivery and mark sent after success: `agents/messaging.py:117`, `agents/messaging.py:167`. Generated photos insert a photo row: `tools/photos/generate.py:68`. Stickers insert and mark media rows: `agents/stickers.py:121`, `agents/stickers.py:139`, `agents/stickers.py:165`, `agents/stickers.py:182`.

But `_drain_photo_outbox()` deletes generated image files after successful send: `agents/telegram_bridge.py:197`, `agents/telegram_bridge.py:237`, `agents/telegram_bridge.py:243`. `send_and_persist()` only writes a `messages` row when `final_text` is non-empty: `agents/messaging.py:174`. `/audit` renders tool audit rows, not media outbox rows: `agents/cockpit.py:377`. `/status` shows graph outbox pending but not media outbox state: `agents/cockpit.py:329`.

Impact: the database can prove a media send happened and store its Telegram message ID, but the user-facing audit trail cannot answer "what photo did you send?", cannot display retention state, and cannot inspect photo-only sends once the file is unlinked.

Suggested fix: promote or mirror `media_outbox` into a stable `media_events` history with source turn/message ID, Telegram message ID, kind, caption, redacted metadata, retention state, and optional content hash. Expose it through `/audit media` or `/status`.

### P2 - Document/image/PDF turns may lose immediate conversational continuity

`handle_document()` builds native content blocks and calls `run_user_turn_blocks(...)`: `agents/telegram_bridge.py:1157`, `agents/telegram_bridge.py:1187`. That runtime path resumes the current session but deliberately does not use the persistent live client and does not update `session_id`: `agents/runtime.py:584`, `agents/runtime.py:598`.

The event row is appended with `source="event"`: `agents/telegram_bridge.py:1175`. Working memory injects only rows whose source is `chat`: `agents/hooks.py:106`, `agents/hooks.py:120`.

Impact: a document/image/PDF turn can be processed and answered, but the next "what was in that file?" turn may depend mostly on the final assistant reply or separately remembered facts, not the full media prompt/session state. The tests currently assert the `log_session_id=False` behavior, but do not verify follow-up continuity.

Suggested fix: verify the SDK session semantics for block turns. If continuity is not guaranteed, add a compact post-send media summary row or a neutralized event-summary injection path for recent media events.

### P2 - MIME trust remains too broad for native Anthropic blocks

`_build_ingest_block()` sends every `mime.startswith("image/")` value as an Anthropic image block except HEIC/HEIF conversion: `agents/telegram_bridge.py:1004`. Claimed PDFs only reject a Windows executable `MZ` prefix: `agents/telegram_bridge.py:1148`. There is no `%PDF-` check for claimed PDFs and no magic-byte allowlist for JPEG/PNG/GIF/WebP before native image routing.

Telegram documents expose `mime_type` as sender-provided metadata, so it should be treated as advisory. Anthropic's vision docs list supported image formats as JPEG, PNG, GIF, or WebP; sending arbitrary `image/*` such as SVG or mislabeled text through native image blocks is brittle.

Impact: mainly reliability and confusing failure modes, not owner-bypass exposure. Unsupported or mislabeled files can be routed into provider-native blocks that are likely to reject them.

Suggested fix: add magic-byte/type validation for PDF, JPEG, PNG, GIF, and WebP. Unsupported or mismatched media should be saved to disk and described via a text prompt, with `read_attachment` available only for safe supported roots.

### P2 - EXIF GPS persistence is useful but privacy-heavy and under-controlled

Document images trigger EXIF GPS extraction automatically: `agents/telegram_bridge.py:941`, then store precise `lat`, `lon`, optional label, and timestamp through `db.photo_location_insert(...)`: `agents/telegram_bridge.py:960`. The schema stores exact coordinates: `storage/db.py:358` in the current schema section inspected. The reverse geocoder hardcodes its User-Agent instead of using the configured deployment contact string: `agents/telegram_bridge.py:918`, `agents/telegram_bridge.py:929`, `config/engagement.yaml:670`.

Impact: sensitive locations can be retained and later influence recurring-location logic without a media-facing inspect/delete/control surface.

Suggested fix: use `location.nominatim_user_agent`, add a user-visible list/delete/retention path for photo-derived locations, and consider storing only rounded coordinates unless the user opts into precision.

### P3 - `photo_in` config does not actually gate inbound photos

`photo_in.enabled`, `caption_max_chars`, and `tag_max_count` exist in config: `config/engagement.yaml:665`. `handle_photo()` does not read those knobs before download, caption use, classifier routing, or prompt construction: `agents/telegram_bridge.py:528`.

Impact: misleading operational knobs. Long captions are gated only by Telegram behavior and the general politeness/affect path, not by the configured limit.

Suggested fix: enforce the config or delete it.

### P3 - Voice byte/format handling is helper-light and under-tested at the bridge level

The bridge downloads the voice file before applying duration rejection: `agents/telegram_bridge.py:658`, `agents/telegram_bridge.py:661`, `agents/telegram_bridge.py:668`. The STT helper reads the whole file into memory and always submits it as `audio/ogg`: `tools/voice.py:97`, `tools/voice.py:111`. OpenAI's current transcription reference lists `ogg` as an accepted format, so this is not a confirmed provider incompatibility; the gap is that the bridge path has no byte cap or smoke/contract coverage.

The test file explicitly says the Telegram bridge handler is not exercised: `tests/test_voice_stt.py:1`.

Impact: low due owner-only handling and Telegram download limits, but large or malformed files are only bounded indirectly and the behavior is not covered where the wiring lives.

Suggested fix: add a configured `voice.max_bytes` or provider-cap-derived guard, validate basic Ogg/Opus shape if practical, and add bridge-level tests for duration rejection, transcription failure, and successful event/send behavior.

### P3 - Inbound sticker-only messages are intentionally non-conversational

Outside capture mode, `handle_inbound_sticker()` silently returns: `agents/telegram_bridge.py:1351`, `agents/telegram_bridge.py:1366`. The test asserts this behavior: `tests/test_grab_stickers.py:88`.

Impact: sticker-only user turns feel ignored and create no event row. This is probably a product decision rather than a safety bug.

Suggested fix: choose an explicit policy: ignore by design, append a compact event row, or send a small sticker/text reaction.

### P3 - `send_and_persist` idempotency can collapse same-millisecond duplicate media sends

The idempotency key hashes only kind, final text, and `created_at_ms`: `agents/messaging.py:111`. It omits `chat_id`, `source`, and `photo_path`.

Impact: rare, but two identical same-millisecond media sends could share one queue row while both attempt Telegram delivery.

Suggested fix: include `chat_id`, `source`, and `photo_path` for media sends, or include a per-send nonce while preserving retry semantics at the queue layer.

## 3. Previously reported issues that now look closed

- Text/HTML forged delimiter injection looks closed. `_build_ingest_block()` wraps stripped HTML and text with `wrap_untrusted("telegram_document", ...)`: `agents/telegram_bridge.py:1053`, `agents/telegram_bridge.py:1073`. Regression tests assert exactly one real close delimiter and an escaped forged delimiter: `tests/test_file_ingest_text.py:42`, `tests/test_file_ingest_html.py:35`.
- Generated photos and stickers are no longer completely outside a delivery ledger. Generated photos insert `media_outbox` rows, stickers insert/mark rows, and photo draining records Telegram message IDs.
- Photo outbox orphan handling now rejects symlinks and out-of-tree paths before queueing or sending: `agents/telegram_bridge.py:169`, `agents/telegram_bridge.py:219`.
- Final outbound assistant text remains post-send persisted through `send_and_persist()`: `agents/messaging.py:142`, `agents/messaging.py:174`.
- `read_attachment` remains constrained to `data/user_photos` and `data/user_documents`, and the focused path-validation tests passed in this run.

## 4. New regressions or contradictions

- `media_outbox.kind` includes `document`: `storage/db.py:493`, but the current document path never inserts document media rows. This may be reserved, but it contradicts the idea of a complete media ledger.
- The cockpit/audit UX direction from the prior UX checklist is not wired to the new queue: `/audit` reads tool audit rows only, and `/status` exposes graph outbox state but not media outbox state.
- Current OpenRouter docs describe image generation through Chat Completions/Responses modalities, while `_call_flux()` still uses `/api/v1/images/generations`.
- The requested prior report files are deleted in the current working tree, so prior context was available only through tracked `HEAD` content.

## 5. Missing tests / suggested verification

- Bridge-level `handle_photo`, `handle_voice`, and `handle_document` tests for success/failure paths: event row, model call, final send success, and no finalized episode before failed send.
- Media episode Graphiti/outbox coverage or a recall test proving local media episodes are merged with graph results.
- `_drain_photo_outbox()` tests for Telegram message ID recording, unlink-on-success, preserve/retry-on-failure, bad payload, symlink, and out-of-tree behavior.
- Sticker tests that assert `media_outbox_mark_sent` / `media_outbox_mark_failed`, not just `send_sticker`.
- Audit/status rendering tests once media rows are surfaced.
- MIME mismatch tests: claimed PDF that is not `%PDF-`, claimed image with text bytes, unsupported `image/svg+xml`, and HEIC conversion fallback.
- EXIF privacy tests for configured Nominatim User-Agent, retention/delete/list controls, and precision policy.
- Follow-up continuity test for `run_user_turn_blocks()` document/image turns.
- Provider smoke or contract tests for OpenRouter image generation and OpenAI Ogg/Opus transcription.
- Voice bridge tests for duration rejection, byte caps, and graceful transcription failure.

## 6. Sprint or roadmap implications

- Treat the next sprint as "media ledger and delivery boundary." Keep `media_outbox` as a queue if useful, but add a durable media event layer that joins media sends to source turns, assistant replies, Telegram IDs, content hashes, and retention state.
- Do not reopen the text/HTML forged-delimiter bug; it has source and test coverage now.
- Make media memories delivery-aware. Photo/voice episode writes should happen after confirmed send, or be explicitly marked pending/failed.
- Decide whether media episodes belong in Graphiti. If not, recall should intentionally merge SQLite media episodes with graph hits so photos and voice notes do not disappear from normal recall.
- Ship EXIF privacy controls before expanding proactive location callbacks.
- Verify provider drift before polishing the photo UX. The OpenRouter path and empty sticker fallback pool are currently the weakest generated-photo reliability points.

## 7. Sources used

Local source, tests, config, and docs:

- `agents/telegram_bridge.py`
- `agents/messaging.py`
- `agents/stickers.py`
- `agents/runtime.py`
- `agents/hooks.py`
- `agents/cockpit.py`
- `storage/db.py`
- `tools/photos/generate.py`
- `tools/photos/_shared.py`
- `tools/voice.py`
- `tools/memory/recall.py`
- `config/engagement.yaml`
- `tests/test_file_ingest_text.py`
- `tests/test_file_ingest_html.py`
- `tests/test_file_ingest_image.py`
- `tests/test_file_ingest_pdf.py`
- `tests/test_file_ingest_unsupported.py`
- `tests/test_read_attachment_path_validation.py`
- `tests/test_photo_router.py`
- `tests/test_voice_stt.py`
- `tests/test_media_outbox.py`
- `tests/test_send_and_persist_api.py`
- `tests/test_stickers.py`
- `tests/test_grab_stickers.py`
- `tests/test_run_user_turn_blocks.py`
- Prior checklists read from `HEAD`: `codex/voice-photo-media-pipeline-review-2026-05-23.md`, `codex/security-review-2026-05-23.md`, `codex/telegram-ux-design-2026-05-23.md`

Official external sources:

- Telegram Bot API: https://core.telegram.org/bots/api
- OpenRouter image generation docs: https://openrouter.ai/docs/guides/overview/multimodal/image-generation
- OpenAI transcription API reference: https://platform.openai.com/docs/api-reference/audio/createTranscription
- Anthropic vision docs: https://docs.anthropic.com/en/docs/build-with-claude/vision
- Anthropic PDF support docs: https://docs.anthropic.com/en/docs/build-with-claude/pdf-support
