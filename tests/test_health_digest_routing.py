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


def test_always_mode_all_green_still_sends():
    assert health.should_ping_chat(_report(), mode="always") is True


def test_always_mode_infra_only_degraded_is_suppressed():
    r = _report(graphiti_reachable={"ok": False, "reason": "timeout"})
    assert health.should_ping_chat(r, mode="always") is False


def test_on_degrade_infra_only_is_suppressed():
    r = _report(graphiti_reachable={"ok": False, "reason": "timeout"})
    assert health.should_ping_chat(r, mode="on_degrade") is False


def test_on_degrade_auth_failure_sends():
    r = _report(oauth_google={"ok": False, "reason": "expired"})
    assert health.should_ping_chat(r, mode="on_degrade") is True
