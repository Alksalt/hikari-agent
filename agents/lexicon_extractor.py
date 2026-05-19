"""Lexicon extractor — pulls candidate phrases out of recent user messages
and promotes the ones that repeat into the ``lexicon`` table.

A phrase is a candidate if:
  - it has between ``lexicon.min_phrase_word_count`` and
    ``lexicon.max_phrase_word_count`` whitespace-delimited tokens,
  - it does NOT match any of ``lexicon.exclusion_patterns`` at its head,
  - the user has used it organically at least
    ``lexicon.promote_after_n_organic_uses`` times in the look-back window.

This is intentionally conservative — the lexicon is meant to surface idiosyncratic
phrases ("attention sinks", "the cabbage thing"), not generic n-grams ("a lot of",
"i think that"). Heavy lifting is left to the daily reflection LLM pass; this
extractor is a cheap, deterministic first cut.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

from storage import db

from . import config as cfg

logger = logging.getLogger(__name__)


def _excludes() -> list[re.Pattern[str]]:
    raw = cfg.get("lexicon.exclusion_patterns") or []
    return [re.compile(p) for p in raw]


def _word_count_range() -> tuple[int, int]:
    return (
        int(cfg.get("lexicon.min_phrase_word_count", 2)),
        int(cfg.get("lexicon.max_phrase_word_count", 6)),
    )


def _promote_threshold() -> int:
    return int(cfg.get("lexicon.promote_after_n_organic_uses", 2))


def _enabled() -> bool:
    return bool(cfg.get("lexicon.enabled", True))


_DEFAULT_STOP_TOKENS = frozenset({
    "the", "a", "an", "i", "you", "we", "they", "it", "is", "are", "was",
    "were", "to", "and", "or", "but", "of", "in", "on", "at", "for", "with",
    "this", "that", "these", "those", "be", "been", "being", "do", "did",
    "does", "have", "has", "had", "will", "would", "should", "could",
})


def _stop_tokens() -> frozenset[str]:
    """Config-driven, with sane fallback if the key is missing."""
    raw = cfg.get("lexicon.stop_tokens")
    if not raw:
        return _DEFAULT_STOP_TOKENS
    return frozenset(str(t).lower() for t in raw)


def _candidate_phrases(text: str) -> list[str]:
    """Slice text into candidate n-grams matching the configured length range.

    Pre-filters at token level (skipping windows that start with a stop word)
    before generating n-grams, so we don't quadratically expand candidates that
    we'd just throw away. Then applies the configured regex excludes as a final
    pass for anything the token filter misses.
    """
    lo, hi = _word_count_range()
    excludes = _excludes()
    max_tokens_per_msg = int(cfg.get("lexicon.max_tokens_per_message", 200))
    # Tokenize on whitespace + simple punctuation strip. Keep apostrophes.
    tokens = [t for t in re.split(r"[\s,.!?;:\"]+", text.lower()) if t]
    if max_tokens_per_msg > 0:
        tokens = tokens[:max_tokens_per_msg]
    stop_tokens = _stop_tokens()
    candidates: list[str] = []
    for n in range(lo, hi + 1):
        for i in range(len(tokens) - n + 1):
            head = tokens[i]
            if head in stop_tokens:
                continue
            phrase = " ".join(tokens[i:i + n])
            if len(phrase) < 4:  # absolute floor
                continue
            if any(pat.search(phrase) for pat in excludes):
                continue
            candidates.append(phrase)
    return candidates


def extract_and_promote(lookback_days: int = 7) -> int:
    """Public entry. Implementation in _do_extract; this wrapper enforces the
    config-enabled gate and the message-count cap."""
    return _do_extract(lookback_days)


def _do_extract(lookback_days: int) -> int:
    """Scan user messages from the last ``lookback_days`` for repeated phrases.

    Phrases meeting the promotion threshold are upserted into the ``lexicon``
    table (which dedupes via UNIQUE constraint). Returns the number of phrases
    newly promoted (vs already present).
    """
    if not _enabled():
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).isoformat()
    try:
        texts = db.all_messages_text_since(cutoff, role="user")
    except Exception:
        logger.exception("lexicon extractor: db read failed")
        return 0
    if not texts:
        return 0
    # Hard cap on messages scanned per pass to bound the O(M × N²) blow-up on
    # heavy days. Most-recent messages first so we keep what matters.
    max_msgs = int(cfg.get("lexicon.max_messages_per_extract", 200))
    if max_msgs > 0 and len(texts) > max_msgs:
        texts = texts[-max_msgs:]

    counter: Counter[str] = Counter()
    for text in texts:
        for phrase in _candidate_phrases(text):
            counter[phrase] += 1

    threshold = _promote_threshold()
    promoted = 0
    for phrase, count in counter.items():
        if count < threshold:
            continue
        try:
            existing = db.lexicon_get(phrase)
            db.lexicon_record(phrase, source="user_coined")
            if existing is None:
                promoted += 1
        except Exception:
            logger.exception("lexicon extractor: lexicon_record failed for %r", phrase)

    if promoted:
        logger.info("lexicon: promoted %d new phrases", promoted)
    return promoted
