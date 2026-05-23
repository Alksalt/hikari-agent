# Voice, Photo, Document, Attachment, Sticker, and Media Pipeline Review - 2026-05-23

Scope: Telegram voice notes, transcription, user photos/images, generated photos, document/image attachments, sticker handling, media persistence, media safety and prompt injection, final-message persistence around media turns, and Graphiti/memory/episode writes from media events.

This review is local-first. Internet research was used only for current Telegram, transcription, Claude media-block, and OpenRouter image-generation behavior.

## Executive Summary

The current media pipeline is much stronger than the older May 23 reviews on one critical point: visible assistant text is now persisted after Telegram send success, not before. `_send_with_choreography()` filters/rewrite-checks the draft, sends the final Telegram message, and then appends that exact final text with the Telegram message id. Text replies that fail to send are not persisted. That closes the worst "phantom assistant message" failure for normal text output and for the final text of media turns.

The media side effects are still uneven. Photo and voice turns write compact user event rows, call the live Claude session, then insert SQLite episodes before the final Telegram reply is delivered. Documents write compact user event rows but do not write media episodes. Generated photos and stickers can be sent as visible assistant-side media without any durable assistant media row, Telegram media id, or Graphiti/episode trace. The result is a split-brain history: text is post-send accurate; media artifacts and some media memories are not.

The highest-risk open safety issue is still document text/HTML prompt wrapping. `read_attachment` tool output is hard-scoped and wrapped by the generic untrusted-output hook, but `_build_ingest_block()` manually injects text and stripped HTML into the model prompt using raw `<<<HIKARI_UNTRUSTED_BEGIN>>>` / `<<<HIKARI_UNTRUSTED_END>>>` sentinels without escaping forged delimiters. Prior security reviews identified this issue; it is still present.

The second major architectural risk is memory consistency. Facts created through the normal `remember` path dual-write toward Graphiti, but media episodes inserted directly through `db.insert_episode()` do not schedule Graphiti writes. Since recall now prefers Graphiti, media episodes can become less visible to future recall than facts, depending on fallback behavior.

The generated-photo provider path also needs verification. The repo calls `https://openrouter.ai/api/v1/images/generations`, but OpenRouter's current image-generation documentation describes image generation through Chat Completions and Responses with image modalities. This may still work as a compatibility path, but it should be treated as provider drift risk and covered by a smoke test or updated client.

Recommended fix order:

1. Replace manual document text/HTML wrappers with the existing escaping wrapper, and add forged-delimiter tests.
2. Add a single media-event ledger for inbound and outbound media, including Telegram message ids and send state.
3. Move photo/voice episode writes behind successful final send, and make document/sticker/generated-photo episode policy explicit.
4. Route media episodes through a Graphiti outbox or stop making Graphiti the primary recall surface for episode-like media context until backfill is reliable.
5. Add bridge-level tests for voice/photo/document media turns and generated-photo/sticker sends.
6. Verify or update the OpenRouter image-generation endpoint against current docs.

## Pipeline Map By Media Type

### Telegram voice notes

Entry point: `agents/telegram_bridge.py::handle_voice()`.

Flow:

1. The bridge only accepts voice notes from `OWNER_CHAT_ID`.
2. It fetches the Telegram voice file with `voice.file_id` and downloads it to `data/user_voice/{millis}.ogg`.
3. It rejects notes whose Telegram-provided duration exceeds `voice.max_duration_sec`.
4. It calls `tools.voice.transcribe_voice()` with the local file path.
5. It applies rude/affect gates to the transcript.
6. It appends a compact user event row: `[voice note Ns] transcript: ...`, with `source='event'`.
7. It bumps `last_user_message`.
8. It sends a synthetic live-session prompt: `[voice note]: {transcript!r}` plus a request to respond naturally.
9. It inserts a SQLite episode summarizing the voice note and draft reply.
10. It sends the final visible reply through `_send_with_choreography()`.

Positive controls:

- Owner-only media handling.
- Local voice persistence before STT.
- Duration cap before STT.
- STT failures are caught and turned into a visible apology instead of crashing the bridge.
- Final visible text is persisted only after Telegram send success.

Gaps:

- Duration is trusted from Telegram metadata; the downloaded file size is not capped before reading and uploading to STT.
- `tools.voice._max_duration_sec()` exists but is not used by the STT helper; all duration enforcement lives in the bridge.
- The transcript is user-originated, model-derived text. It is inserted into a synthetic prompt and an episode without untrusted delimiters. Because the sender is the owner this is lower risk than third-party web content, but an audio prompt-injection recording can still become first-class prompt text.
- The SQLite episode is written before final Telegram delivery, so a failed send can leave a memory of a reply the user never saw.
- There is unit coverage for `tools.voice`, but no bridge-level voice test that exercises download, duration rejection, event row, session prompt, episode timing, and final-message persistence together.

### Transcription

Entry point: `tools/voice.py::transcribe_voice()`.

Flow:

1. Reads config from `config/tools.yaml`, including endpoint, model, API key env var, timeout, and language.
2. Reads the whole audio file into memory.
3. POSTs multipart form data to the transcription endpoint with `file`, `model`, and optional `language`.
4. Parses JSON and returns `text.strip()`.

Positive controls:

- Configurable endpoint/model/key/language.
- Explicit API key failure.
- HTTP errors are logged with a bounded preview.
- Missing `text` in response is treated as failure.

Gaps:

- The code uses `audio/ogg` in the multipart upload. OpenAI's current API reference includes OGG as accepted for transcriptions, while some guide text still highlights common formats such as mp3, mp4, mpeg, mpga, m4a, wav, and webm. This is worth preserving in tests because Telegram voice notes are OGG/Opus.
- The configured model is historically `whisper-1`; OpenAI's current speech-to-text docs list newer `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, and diarization-capable options. This is not a bug, but it is an upgrade choice.
- No byte-size check before loading the audio file.

### User photos/images

Entry point for Telegram compressed photos: `agents/telegram_bridge.py::handle_photo()`.

Flow:

1. Owner-only check.
2. Selects the largest `message.photo` size.
3. Downloads it to `data/user_photos/{millis}.jpg`.
4. Runs caption rude/affect gates.
5. Builds a synthetic prompt telling the model to call `mcp__hikari_utility__read_attachment` with the relative path.
6. Runs `tools.photos.classify.classify_user_photo()` on the saved image and caption.
7. Appends a compact event row: `[photo: data/user_photos/...jpg] caption: ...`, with `source='event'`.
8. Bumps `last_user_message`.
9. Calls `runtime.run_user_turn(prompt)` against the live session.
10. Inserts a SQLite episode before final delivery.
11. Sends final visible text through `_send_with_choreography()`.
12. Drains any generated-photo outbox files.

Entry point for image documents: `agents/telegram_bridge.py::handle_document()` plus `_build_ingest_block()`.

Flow:

1. Owner-only check.
2. MIME and size checks.
3. If `mime_type.startswith("image/")`, attempts EXIF GPS/timestamp extraction before saving.
4. Saves under `data/user_documents/{timestamp}_{safe_filename}`.
5. Builds an Anthropic image content block when the MIME is supported.
6. Falls back to a prompt that tells the model to use `read_attachment` for unsupported or HEIC/HEIF images.
7. Appends a compact document event row.
8. Calls `runtime.run_user_turn_blocks(prompt_blocks)`.
9. Sends final visible text through `_send_with_choreography()`.

Positive controls:

- Owner-only.
- User photos are saved under a dedicated allowed root.
- The read path is hard-scoped by `tools.attachments.read` to `data/user_photos` and `data/user_documents`.
- The generic tool hook wraps `read_attachment` outputs as untrusted.
- The classifier sanitizes OCR/details text before inserting router hints into the model prompt.
- Image documents can be passed as native image blocks instead of making the model tool-read base64 text.

Gaps:

- `photo_in.enabled`, `caption_max_chars`, and `tag_max_count` appear in config but are not enforced in `handle_photo()`.
- Compressed Telegram photos do not have explicit byte-size caps before classifier/read.
- Photo classifier sends the full image to Anthropic directly as a routing helper. That may be acceptable, but it is a privacy-relevant external call and should be documented as such.
- Photo episodes are inserted before final message delivery.
- Image document EXIF location extraction silently stores precise GPS data in `photo_locations` if present. There is no user-visible retention/delete control.
- `_reverse_geocode_label()` hardcodes `User-Agent: hikari-agent/0.1` instead of using the configured `location.nominatim_user_agent`.

### Generated photos

Entry points: `tools/photos/generate.py::generate_photo()`, `tools/photos/_shared.py::_call_flux()`, and `agents/telegram_bridge.py::_drain_photo_outbox()`.

Flow:

1. The model calls the `generate_photo` tool.
2. The tool refuses in irritable mood and enforces a daily cap.
3. It builds a prompt from `assets/APPEARANCE.md` plus a mood-specific scene.
4. It calls OpenRouter image generation through `_call_flux()`.
5. On success, it writes a PNG to `data/photo_outbox/{millis}.png`, increments the daily count, and returns a queued message to the model.
6. `_send_with_choreography()` sends the final text reply.
7. `handle_photo()` and the text send choreography drain `PHOTO_OUTBOX` after the visible text by sending each image with `bot.send_photo()`.
8. Successfully sent outbox files are unlinked.
9. On image generation failure, the tool sets `runtime_state["image_gen_last_failure_ts"]`; the send choreography may force a sticker fallback.

Positive controls:

- Mood gate.
- Daily cap.
- Outbox files are retained when Telegram send fails.
- The model does not directly send media; the bridge controls the actual Telegram send.

Gaps:

- Outbound generated photos are not recorded as assistant media rows and Telegram media message ids are not persisted.
- If the assistant sends only a generated photo or the text is empty/filtered away, history can lose the visible media act.
- Deleting the outbox file after success removes the local generated artifact unless some other artifact store exists.
- OpenRouter's current image-generation docs describe Chat Completions/Responses image modalities, while `_call_flux()` calls `/api/v1/images/generations`. This should be verified or migrated.
- Daily cap and outbox drain are not locked; concurrent sends could race.
- There is failure-path testing for `generate_photo`, but no outbox success-drain persistence test.

### Document/image attachments

Entry point: `agents/telegram_bridge.py::handle_document()`.

Flow:

1. Owner-only check.
2. Filename, MIME, and size extraction from Telegram `Document`.
3. Hard cap of 32 MiB, matching Anthropic PDF request limits.
4. Optional EXIF ingest for image documents.
5. Download and save to `data/user_documents/{timestamp}_{safe_filename}`.
6. Caption rude/affect gates.
7. Magic-byte guard for claimed PDFs that start with `MZ`.
8. `_build_ingest_block()` chooses one of:
   - PDF `document` content block with base64 source and citations enabled.
   - image content block for supported image MIME types.
   - stripped HTML text block wrapped in manual untrusted sentinels.
   - text/code/XML/JSON block wrapped in manual untrusted sentinels.
   - fallback prompt asking the model to use `read_attachment`.
9. Appends a compact event row: `[document: filename (mime, size bytes)] ...`, with `source='event'`.
10. Bumps `last_user_message`.
11. Calls `run_user_turn_blocks(prompt_blocks)`.
12. Sends final visible text through `_send_with_choreography()`.

Positive controls:

- Owner-only.
- Filename sanitization.
- Size cap before inline Anthropic processing.
- Native PDF/image blocks for supported types.
- HEIC/HEIF conversion fallback.
- `read_attachment` path containment for later tool reads.
- Claimed-PDF Windows executable guard.

Gaps:

- Manual text/HTML untrusted wrappers do not escape forged end delimiters. This is the most direct injection bug in the reviewed media surface.
- Unsupported or large files are saved locally but may be unreadable by `read_attachment` if they exceed the tool's 8 MiB cap.
- MIME type is largely trusted from Telegram, with only a narrow `MZ` check for PDFs.
- Documents do not get the same SQLite episode treatment as photos and voice notes. That may be intentional, but the policy is not explicit.
- `run_user_turn_blocks()` uses an ephemeral client path because the persistent client expects string stdin. It resumes the live session id and updates it, but this is a second runtime path with separate failure behavior.

### Sticker handling

Entry points: `agents/telegram_bridge.py::handle_inbound_sticker()`, `/grab_stickers` command handlers, and `agents/stickers.py`.

Inbound flow:

1. Owner-only check.
2. If capture mode is active, store sticker `file_id`s in `runtime_state["sticker_capture"]` by bucket.
3. `/grab_stickers done` emits a YAML snippet for config.
4. If capture mode is inactive, owner stickers are silently ignored.

Outbound flow:

1. `_send_with_choreography()` can call `stickers.force_send_sticker()` when recent image generation failed.
2. It can call `stickers.maybe_send_sticker()` probabilistically after text replies.
3. Outbound sticker sending uses Telegram `file_id`s from config.

Positive controls:

- Owner-only capture.
- Duplicate suppression during capture.
- YAML output quotes file ids safely.
- Empty sticker pools disable outbound sending.
- `maybe_send_sticker()` respects mood blocklist, probability, and cooldown.

Gaps:

- Inbound stickers outside capture are invisible to memory and conversation history.
- Outbound sticker sends are not persisted as assistant media rows.
- Forced image-generation failure stickers bypass probability, cooldown, and mood gates by design, but are also unaudited.
- `config/engagement.yaml` currently has empty configured sticker file-id pools, so fallback stickers are effectively disabled until populated.
- The config contains `solo_reply_probability`, but no code path in `agents/stickers.py` appears to use it.

## Persistence And Memory Behavior

### What is currently persisted well

- Normal user text goes through `runtime.respond()`, which appends the user message and bumps `last_user_message`.
- Final assistant text goes through `_send_with_choreography()`, which appends only the sent post-filter text with Telegram message id.
- Photo, voice, and document inbound media append compact user event rows with `source='event'`.
- Photo, voice, and document inbound media bump `last_user_message`.
- Reactions write compact event rows and feedback/proactive-event records rather than raw synthetic prompt text.

### Where persistence is uneven

Photo and voice:

- Persist a compact user event row before model execution.
- Insert a SQLite episode before final send success.
- Persist final text only after send success.

Document:

- Persists a compact user event row before model execution.
- Does not insert a SQLite episode.
- Persists final text only after send success.

Generated photo:

- Writes local outbox PNG.
- Sends Telegram photo after text reply.
- Deletes local PNG after successful send.
- Does not persist a message row, media row, Telegram photo message id, or episode.

Sticker:

- Sends Telegram sticker by file id.
- Records only transient cooldown/runtime state.
- Does not persist a message row, media row, Telegram sticker message id, or episode.

Inbound sticker:

- Capture-mode stickers are stored in runtime capture state until YAML export.
- Non-capture stickers are ignored.

### Graphiti / memory behavior

Facts remembered through the regular memory path use `storage.db.insert_fact()`, which schedules a Graphiti episode via `storage.graph.schedule_episode()`. That means a model-initiated `remember` call after viewing a user photo can reach Graphiti.

Media episodes inserted directly with `db.insert_episode()` do not schedule Graphiti writes. Voice/photo episodes therefore live in SQLite episodes/FTS but are not guaranteed to appear in the primary Graphiti recall path. Since `tools/memory/recall.py` now prefers Graphiti and only falls back to SQLite when Graphiti yields nothing, media episodes may become second-class recall material.

There is also no durable Graphiti outbox for failed async graph writes. `storage.graph.schedule_episode()` is fire-and-forget. That risk applies broadly to Graphiti writes, but media episodes are worse because they bypass the scheduler entirely.

### Final-message persistence around media turns

The final visible text is handled correctly relative to older reports: it is appended only after `bot.send_message()` succeeds, and the row contains the post-filter final text, not a draft.

The remaining inconsistency is that media side effects are not under the same delivery boundary. Photo/voice episodes can describe a reply before it is delivered. Generated photos and stickers can be delivered without any durable assistant-side record. A single media-event ledger would make this auditable:

- inbound/outbound
- media type
- local path or Telegram `file_id`
- Telegram message id after successful send
- send state
- whether it bumps `last_user_message`
- whether it creates an episode
- whether it schedules Graphiti
- retention/delete state

## Safety / Injection Risks

### P0: forged delimiters in text/HTML document ingest

`_build_ingest_block()` manually wraps stripped HTML and text-like documents with:

```text
<<<HIKARI_UNTRUSTED_BEGIN>>>
...
<<<HIKARI_UNTRUSTED_END>>>
```

The content is not escaped before insertion. A malicious text or HTML file can include `<<<HIKARI_UNTRUSTED_END>>>` and continue with instructions that appear outside the quoted untrusted region. The repo already has `wrap_untrusted()` tests that show the intended escaping behavior, and the generic PostToolUse hook wraps `read_attachment` output correctly. The inline document path should use the same helper instead of hand-written sentinels.

### P1: media episodes before final delivery

Photo and voice handlers insert episodes before the Telegram final reply is sent. If Telegram send fails, local memory can claim Hikari reacted with text that was never delivered. This is not prompt injection, but it is memory integrity risk and undermines the invariant fixed for assistant text rows.

### P1: generated photos and stickers have no assistant media ledger

Generated photos and stickers are visible assistant outputs, but they do not create message rows with Telegram ids. That weakens auditability, deletion, replay, debugging, and "what did you send me?" recall.

### P1: image/document privacy and external calls

Photo classification sends user images to Anthropic. Generated photos send Hikari appearance prompts to OpenRouter. Voice transcription sends voice-note audio to the configured transcription provider. These are reasonable product choices, but the media review should treat them as privacy boundaries:

- classify photo -> Anthropic
- transcribe voice -> OpenAI-compatible transcription endpoint
- generate photo -> OpenRouter/provider
- inline document/image/PDF -> Anthropic Claude runtime

### P1: EXIF location persistence

Image documents with GPS EXIF can create precise `photo_locations` rows. This is useful for context, but it is sensitive media-derived location data. The bridge does not currently ask the user, expose a deletion command, or document retention.

### P2: transcript prompt injection

Voice transcripts are treated like owner text. That is usually fine for a personal bot, but recorded audio can carry prompt-injection content. The transcript should be clearly framed as user-supplied content in the synthetic prompt. Do not wrap it as third-party text if that would break normal voice UX, but do prevent it from blending with system instructions.

### P2: MIME trust and file sniffing

Document routing relies on Telegram MIME metadata plus a narrow claimed-PDF `MZ` guard. This is better than nothing but not a general file-type verifier. Native image/PDF block creation should be based on a small allowlist plus magic sniffing where cheap.

## UX Gaps

- Voice transcription failure replies are generic. They do not distinguish missing API key, overlong note, provider failure, unsupported format, or network failure.
- Voice duration rejection happens after download. From the user's view this is okay, but operationally a long note still causes Telegram file work.
- Inbound stickers outside capture mode get no response and no memory row, so sticker-only turns can feel ignored.
- Generated-photo failure fallback depends on sticker file-id pools, but the checked config has empty pools. The intended visual fallback may silently do nothing.
- Generated photos are sent after text, but there is no user-facing or persisted link between the text and the image if the text is later recalled.
- Saved media has no obvious retention policy, cleanup command, or user-visible inventory.
- EXIF-derived location storage is invisible.
- Document ingest can save a file and then fall back to "use read_attachment", but `read_attachment` has a smaller 8 MiB cap than the document save path, so the user may get a confusing failure for a file that was accepted.

## Reliability / Failure Modes

- Telegram `get_file()` / download failures are handled with visible apologies for photos, voice, and documents.
- STT HTTP and JSON failures are handled in `tools.voice`.
- `run_user_turn_blocks()` is a separate runtime path from `run_user_turn()`. It resumes and updates the SDK session id, but it is not protected by the same persistent-client path. This increases the need for document-specific tests.
- Photo/voice user event rows are written before model execution. If Claude fails, the event row remains, which is probably correct: the user did send the media. But no episode/final assistant row should be written until visible response succeeds.
- Generated-photo outbox files are retained on Telegram send failure, which is good, but repeated failures can accumulate files.
- Generated-photo file names use millisecond timestamps. Collision is unlikely but not impossible under concurrency.
- Runtime state for daily photo cap, sticker cooldown, and sticker capture is process-local. Restarts can reset some behavior unless persisted elsewhere.
- `data/user_documents`, `data/user_voice`, and `data/photo_outbox` are lazy-created and absent until first use. That is fine, but operational scripts should not assume they exist.
- `read_attachment` containment is strong, but its allowed roots mean any future bug that writes sensitive files into `data/user_documents` or `data/user_photos` makes them model-readable.

## Recommended Fix Order

1. **Fix inline text/HTML document wrapping.** Replace manual sentinels in `_build_ingest_block()` with the existing `wrap_untrusted()` helper or a shared wrapper with identical escaping semantics. Add forged `<<<HIKARI_UNTRUSTED_END>>>` tests for text and HTML ingest.

2. **Add a media-event ledger.** Create a durable table or message convention for media events covering inbound photos, voice, documents, stickers, generated photos, and generated-image failures. Record Telegram ids after successful sends.

3. **Move photo/voice episode writes after successful final send.** Keep inbound user event rows before model execution, but create assistant/media episodes only after visible delivery succeeds. For send failure, record a failed-send event if useful, not a successful reaction.

4. **Make document episode policy explicit.** Either documents should create media episodes like photos/voice, or no media handler should write episodes automatically. The current asymmetry is accidental-looking.

5. **Route media episodes through Graphiti.** Add an outbox/backfill path for episodes, not only facts. If Graphiti remains primary recall, media-derived episodes should not live only in SQLite.

6. **Persist generated photos and stickers.** After successful `send_photo()` / `send_sticker()`, append an assistant media row or media-event row with Telegram message id and local/outbox identity. Decide whether generated image files are deleted, archived, or retained with TTL.

7. **Verify or update OpenRouter image generation.** Current OpenRouter docs show image generation through `/api/v1/chat/completions` or Responses with modalities. Either prove `/api/v1/images/generations` is supported for the chosen model or migrate `_call_flux()`.

8. **Add voice byte-size and transcript framing.** Cap downloaded voice bytes before reading/uploading. Frame transcripts as user-provided voice content in the synthetic prompt.

9. **Add EXIF privacy controls.** Use configured Nominatim UA, record whether GPS was extracted, expose a way to list/delete photo locations, and document retention.

10. **Tighten document MIME handling.** Keep the current `MZ` guard, but add small magic-sniff checks for PDF and common image formats before native block creation.

## Suggested Tests

### Injection and safety

- Text document containing a forged `<<<HIKARI_UNTRUSTED_END>>>` stays inside escaped untrusted content.
- HTML document containing forged sentinels after stripping stays escaped.
- `read_attachment` output remains wrapped by the PostToolUse hook.
- Photo classifier OCR/router details cannot inject brackets, newlines, or tool instructions into the router block.
- Voice transcript with prompt-injection-like text is framed as transcript text, not system/control text.

### Bridge-level media persistence

- `handle_voice()` success writes one inbound event row, bumps `last_user_message`, calls `run_user_turn()`, sends final text, and persists final sent text with Telegram id.
- `handle_voice()` final send failure does not append assistant final text and does not insert a successful reaction episode after the recommended fix.
- `handle_photo()` success writes one inbound event row, includes classifier router block, persists final sent text, and creates post-send media episode only after the recommended fix.
- `handle_document()` PDF/image/text paths append compact event rows and call `run_user_turn_blocks()` with the expected content-block shape.
- Document run failure does not append assistant text.
- Reactions and sticker-only inbound events follow an explicit event-row policy.

### Generated photos and stickers

- `generate_photo()` success writes exactly one outbox file and increments daily count.
- `_drain_photo_outbox()` sends each file, records a media event with Telegram id after the recommended fix, and deletes or archives according to policy.
- `_drain_photo_outbox()` preserves the file on Telegram send failure.
- Image generation failure sets `image_gen_last_failure_ts` and the forced fallback path is exercised with empty and populated sticker pools.
- `maybe_send_sticker()` and `force_send_sticker()` create media events after the recommended fix.

### Memory / Graphiti

- Media episode creation schedules a Graphiti write or durable outbox item.
- Graphiti unavailable does not drop media episodes silently.
- Recall can find a media-derived episode after graph ingestion/backfill.
- Photo facts remembered via `remember` still dual-write through the existing fact path.

### Reliability

- Voice file over byte cap is rejected before STT upload.
- Telegram `get_file()` with missing/expired file path is handled visibly.
- MIME mismatch cases: claimed PDF with non-PDF bytes, image MIME with non-image bytes, text file with invalid UTF-8.
- HEIC/HEIF conversion fallback does not crash and still provides a readable path.
- EXIF GPS extraction stores expected lat/lon/taken_at and uses configured Nominatim UA.
- Concurrent generated-photo calls do not exceed daily cap or collide on file names.

## Sources

### Local project sources

- `AGENTS.md`
- `CLAUDE.md`
- `codex/index.md`
- `codex/prompt_persona_deep_dive.md`
- `codex/security-review-2026-05-23.md`
- `codex/security-solo-dev-deep-dive-2026-05-23.md`
- `codex/deep-architecture-review-2026-05-23.md`
- `agents/telegram_bridge.py`
- `agents/runtime.py`
- `agents/stickers.py`
- `storage/db.py`
- `storage/graph.py`
- `tools/voice.py`
- `tools/photos/classify.py`
- `tools/photos/generate.py`
- `tools/photos/_shared.py`
- `tools/attachments/read.py`
- `tests/test_final_sent_text_is_persisted.py`
- `tests/test_voice_stt.py`
- `tests/test_photo_router.py`
- `tests/test_file_ingest_image.py`
- `tests/test_file_ingest_pdf.py`
- `tests/test_file_ingest_text.py`
- `tests/test_file_ingest_html.py`
- `tests/test_file_ingest_unsupported.py`
- `tests/test_read_attachment_path_validation.py`
- `tests/test_external_wrap.py`
- `tests/test_security.py`
- `tests/test_stickers.py`
- `tests/test_grab_stickers.py`
- `tests/test_start_and_reaction_event_rows.py`
- `tests/test_run_user_turn_blocks.py`
- `data/user_photos`
- `data/user_documents`
- `assets/stickers`

### External sources

- Telegram Bot API, `getFile`, file download limits, and file-id behavior: https://core.telegram.org/bots/api#getfile
- Telegram Bot API, voice/document/photo/sticker object reference: https://core.telegram.org/bots/api
- OpenAI Speech to Text guide, transcription models and file-upload limits: https://platform.openai.com/docs/guides/speech-to-text
- OpenAI Audio API reference, transcription endpoint and accepted model list: https://platform.openai.com/docs/api-reference/audio/createTranscription
- Anthropic Vision guide, image content blocks and supported image sources: https://docs.anthropic.com/en/docs/build-with-claude/vision
- Anthropic PDF support, 32 MB request limit, page limit, and PDF document blocks: https://docs.anthropic.com/en/docs/build-with-claude/pdf-support
- Anthropic Citations guide, document block formats and PDF/text citation support: https://docs.anthropic.com/en/docs/build-with-claude/citations
- OpenRouter image generation guide, current chat/responses image-generation path: https://openrouter.ai/docs/guides/overview/multimodal/image-generation
- OpenRouter API reference overview and OpenAPI specs: https://openrouter.ai/docs/api-reference/overview

