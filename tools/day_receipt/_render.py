"""ASCII receipt rendering. Looks like a thermal printer slip.

Width and divider glyphs come from RenderOptions so the look is tunable
without hunting through string literals. Identical to the standalone
``day_receipt.render`` module — kept verbatim so the in-process and CLI
outputs match.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from datetime import date as date_cls

from tools.day_receipt._db import Receipt
from tools.day_receipt._shared import CATEGORIES, Category

_LABELS: dict[Category, str] = {
    "made": "MADE",
    "moved": "MOVED",
    "learned": "LEARNED",
    "avoided": "AVOIDED",
}

_SHORT_LABELS: dict[Category, str] = {
    "made": "Md",
    "moved": "Mv",
    "learned": "Ln",
    "avoided": "Av",
}


@dataclass(frozen=True)
class RenderOptions:
    width: int = 46
    heavy_rule: str = "="
    light_rule: str = "-"
    bullet: str = "·"
    header_title: str = "DAY RECEIPT"
    show_empty_sections: bool = False
    footer_message: str = "end of day. logged."


def _center(text: str, width: int) -> str:
    text = text[:width]
    pad = max(0, (width - len(text)) // 2)
    return " " * pad + text


def _rule(char: str, width: int) -> str:
    return (char * width)[:width]


def _wrap_entry(text: str, bullet: str, width: int) -> list[str]:
    prefix = f"  {bullet} "
    indent = " " * len(prefix)
    wrapped = textwrap.wrap(text, width=width - len(prefix)) or [""]
    return [prefix + wrapped[0]] + [indent + line for line in wrapped[1:]]


def render_receipt(receipt: Receipt, options: RenderOptions | None = None) -> str:
    opts = options or RenderOptions()
    w = opts.width
    lines: list[str] = []
    lines.append(_rule(opts.heavy_rule, w))
    lines.append(_center(f"{opts.header_title}  ·  {receipt.receipt_date.isoformat()}", w))
    if receipt.note:
        lines.append(_center(f"({receipt.note})", w))
    lines.append(_rule(opts.heavy_rule, w))
    lines.append("")

    for cat in CATEGORIES:
        bucket = receipt.by_category(cat)
        if not bucket and not opts.show_empty_sections:
            continue
        lines.append(_LABELS[cat])
        if not bucket:
            lines.append(f"  {opts.bullet} (none)")
        else:
            for entry in bucket:
                lines.extend(_wrap_entry(entry.text, opts.bullet, w))
                if entry.tags:
                    tag_line = "    [" + ", ".join(entry.tags) + "]"
                    lines.append(tag_line[:w])
        lines.append("")

    counts = receipt.counts
    summary = "  ".join(f"{k}:{counts[k]}" for k in CATEGORIES)
    lines.append(_rule(opts.light_rule, w))
    lines.append(_center(summary, w))
    lines.append(_rule(opts.light_rule, w))
    lines.append(_center(opts.footer_message, w))
    lines.append(_rule(opts.heavy_rule, w))
    return "\n".join(lines) + "\n"


def render_week(receipts: list[Receipt], options: RenderOptions | None = None) -> str:
    opts = options or RenderOptions()
    if not receipts:
        return _rule(opts.heavy_rule, opts.width) + "\n" + _center(
            "no receipts in range.", opts.width
        ) + "\n" + _rule(opts.heavy_rule, opts.width) + "\n"
    parts = [render_receipt(r, opts) for r in receipts]
    return "\n".join(parts)


def render_summary_table(
    summaries: list[tuple[date_cls, dict[Category, int], bool]],
    options: RenderOptions | None = None,
) -> str:
    opts = options or RenderOptions()
    w = opts.width
    lines: list[str] = []
    lines.append(_rule(opts.heavy_rule, w))
    lines.append(_center("RECEIPT INDEX", w))
    lines.append(_rule(opts.heavy_rule, w))
    if not summaries:
        lines.append(_center("(no entries yet)", w))
        lines.append(_rule(opts.heavy_rule, w))
        return "\n".join(lines) + "\n"
    for d, counts, has_note in summaries:
        marker = "*" if has_note else " "
        body = f"{marker} {d.isoformat()}  " + " ".join(
            f"{_SHORT_LABELS[k]}:{counts.get(k, 0)}" for k in CATEGORIES
        )
        lines.append(body[:w])
    lines.append(_rule(opts.heavy_rule, w))
    return "\n".join(lines) + "\n"
