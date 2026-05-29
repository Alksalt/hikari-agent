"""Token storage backends.

TokenStore is the abstract interface. Two concrete implementations:
  - KeychainStore: delegates to python-keyring (OS keychain / SecretService).
    Service name: ``hikari.<provider>``. Key name as-is.
  - MemoryStore: in-process dict, used in tests and as a fallback when keyring
    is unavailable.

``default_store()`` tries KeychainStore first; on ImportError or
keyring.errors.KeyringError it checks HIKARI_REQUIRE_KEYCHAIN=1
(raises if set), then falls back to MemoryStore with a warning.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class TokenStore(ABC):
    """Abstract token store."""

    @abstractmethod
    def get(self, provider: str, key: str) -> str | None:
        """Return stored value or None."""

    @abstractmethod
    def set(self, provider: str, key: str, value: str) -> None:
        """Persist value."""

    @abstractmethod
    def clear(self, provider: str) -> None:
        """Remove all keys for a provider."""


class MemoryStore(TokenStore):
    """Process-local dict — for tests and fallback."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def get(self, provider: str, key: str) -> str | None:
        return self._data.get((provider, key))

    def set(self, provider: str, key: str, value: str) -> None:
        self._data[(provider, key)] = value

    def clear(self, provider: str) -> None:
        keys = [k for k in self._data if k[0] == provider]
        for k in keys:
            del self._data[k]


class KeychainStore(TokenStore):
    """OS keychain via python-keyring.

    Service name: ``hikari.<provider>``; username = key.
    Raises ImportError on import if keyring is not installed.
    Raises keyring.errors.KeyringError on backend failures.
    """

    def __init__(self) -> None:
        import keyring  # noqa: F401 — validate available at construction time
        self._keyring = keyring

    def _service(self, provider: str) -> str:
        return f"hikari.{provider}"

    def get(self, provider: str, key: str) -> str | None:
        return self._keyring.get_password(self._service(provider), key)

    def set(self, provider: str, key: str, value: str) -> None:
        self._keyring.set_password(self._service(provider), key, value)

    def clear(self, provider: str) -> None:
        # 'grant' is the keychain item written by write_grant_to_keychain() via _GRANT_KEY.
        for key in ("client_id", "client_secret", "refresh_token", "access_token", "grant"):
            try:
                self._keyring.delete_password(self._service(provider), key)
            except Exception:
                pass  # key may not exist; best-effort


# Module-level singleton — created once.
_store: TokenStore | None = None


def default_store() -> TokenStore:
    """Return (or create) the process-wide default TokenStore.

    Tries KeychainStore; falls back to MemoryStore unless
    HIKARI_REQUIRE_KEYCHAIN=1 is set (raises in that case).
    """
    global _store
    if _store is not None:
        return _store
    try:
        _store = KeychainStore()
        logger.debug("auth.store: using KeychainStore")
    except (ImportError, Exception) as exc:
        if os.environ.get("HIKARI_REQUIRE_KEYCHAIN") == "1":
            raise RuntimeError(
                "HIKARI_REQUIRE_KEYCHAIN=1 but keyring is unavailable: "
                f"{exc!r}"
            ) from exc
        logger.warning(
            "auth.store: keyring unavailable (%r), falling back to MemoryStore",
            exc,
        )
        _store = MemoryStore()
    return _store


def _reset_store() -> None:
    """For tests only — reset the singleton so the next call re-initialises."""
    global _store
    _store = None
