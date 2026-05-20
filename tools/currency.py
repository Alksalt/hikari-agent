"""Phase 10: currency conversion via frankfurter.app (free, no key)."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from claude_agent_sdk import tool

from agents import config as cfg
from tools._response import ok as _ok

logger = logging.getLogger(__name__)


@tool(
    "currency_convert",
    "Convert an amount between two ISO currency codes via frankfurter.app "
    "(ECB daily rates). Example: amount=100, from_ccy='USD', to_ccy='NOK'.",
    {"amount": float, "from_ccy": str, "to_ccy": str},
)
async def currency_convert(args: dict[str, Any]) -> dict[str, Any]:
    amount = float(args.get("amount") or 0)
    from_ccy = (args.get("from_ccy") or "").strip().upper()
    to_ccy = (args.get("to_ccy") or "").strip().upper()
    if not from_ccy or not to_ccy:
        return _ok("refused: missing from_ccy or to_ccy")
    if amount <= 0:
        return _ok("refused: amount must be positive")
    endpoint = str(cfg.get("currency.endpoint", "https://api.frankfurter.app/latest"))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(endpoint, params={
                "amount": amount, "from": from_ccy, "to": to_ccy,
            })
            r.raise_for_status()
            data = r.json() or {}
            rates = data.get("rates") or {}
            converted = rates.get(to_ccy)
            if converted is None:
                return _ok(f"error: no rate returned for {from_ccy}->{to_ccy}",
                           data={"error": "no rate"})
            return _ok(
                f"{amount} {from_ccy} = {converted} {to_ccy} (as of {data.get('date')})",
                data={
                    "amount": amount, "from": from_ccy, "to": to_ccy,
                    "converted": converted, "as_of_date": data.get("date"),
                },
            )
    except Exception as e:
        logger.exception("currency convert failed")
        return _ok(f"error: {e}", data={"error": str(e)})


ALL_TOOLS = [currency_convert]
