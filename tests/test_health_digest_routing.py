from agents import health


def _report(**overrides):
    base = {name: {"ok": True} for name in (
        "db_integrity", "oauth_google", "google_scopes", "google_account",
        "graphiti_reachable", "graph_outbox_pending")}
    base.update(overrides)
    return base


def test_infra_failure_is_not_chat_worthy():
    r = _report(graphiti_reachable={"ok": False, "reason": "timeout"})
    assert health.chat_worthy_failures(r) == {}


def test_auth_failure_is_chat_worthy():
    r = _report(oauth_google={"ok": False, "reason": "expired"})
    assert list(health.chat_worthy_failures(r)) == ["oauth_google"]


def test_all_green_is_not_chat_worthy():
    assert health.chat_worthy_failures(_report()) == {}


def _bridge_would_send(report, mode):
    """Mirrors the send gate in agents/telegram_bridge.py post_init.

    The condition is inline in the bridge (not unit-testable without heavy
    mocks), so we replicate the exact expression against the pure health
    functions it composes. Passing `mode` explicitly keeps this independent
    of the HIKARI_STARTUP_DIGEST env var.
    """
    return bool(
        health.should_send_digest(report, mode=mode)
        and (not health.is_degraded(report) or health.chat_worthy_failures(report))
    )


def test_always_mode_all_green_still_sends():
    assert _bridge_would_send(_report(), "always") is True


def test_always_mode_infra_only_degraded_is_suppressed():
    r = _report(graphiti_reachable={"ok": False, "reason": "timeout"})
    assert _bridge_would_send(r, "always") is False


def test_on_degrade_infra_only_is_suppressed():
    r = _report(graphiti_reachable={"ok": False, "reason": "timeout"})
    assert _bridge_would_send(r, "on_degrade") is False


def test_on_degrade_auth_failure_sends():
    r = _report(oauth_google={"ok": False, "reason": "expired"})
    assert _bridge_would_send(r, "on_degrade") is True
