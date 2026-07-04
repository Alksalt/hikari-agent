"""Per-turn effort is cfg-driven, default high (sonnet-5 respects effort
strictly; medium under-reaches for tools — official migration guidance)."""
from unittest.mock import patch

from agents import config as cfg
from agents.runtime import _MODEL_RATES_USD_PER_1M, _build_options


def test_effort_defaults_to_high():
    opts = _build_options(resume=None)
    assert opts.effort == "high"


def test_effort_reads_cfg():
    # agents.runtime.cfg IS this same agents.config module object, so
    # patching "agents.runtime.cfg.get" replaces get() on the one shared
    # module. Capture the original callable before patching — calling
    # cfg.get(...) from inside the fake would recurse into the mock.
    original_get = cfg.get

    def fake_get(key, default=None):
        if key == "runtime.effort":
            return "xhigh"
        return original_get(key, default)

    with patch("agents.runtime.cfg.get", side_effect=fake_get):
        opts = _build_options(resume=None)
    assert opts.effort == "xhigh"


def test_sonnet5_in_cost_map():
    assert _MODEL_RATES_USD_PER_1M["claude-sonnet-5"] == (3.00, 15.00)
