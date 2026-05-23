"""Sprint 4 Phase 4C — python_run sandbox deny-default + input_files allowlist."""
import importlib
import pathlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    """Match the isolation fixture from test_calc.py."""
    from agents import config
    from storage import db

    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


@pytest.mark.asyncio
async def test_deny_home_env():
    import os as _os

    from tools import calc
    home = _os.path.expanduser("~")
    code = f"print(open('{home}/.env').read()[:50])"
    result = await calc.python_run.handler({"code": code})
    text = result["content"][0]["text"]
    lower = text.lower()
    assert (
        "operation not permitted" in lower
        or "permissionerror" in lower
        or "errno 1" in lower
        or "errno 13" in lower
        or "no such file" in lower      # acceptable: blocked → path not found
        or "filenotfounderror" in lower
    ), f"expected sandbox-deny error, got: {text[:300]}"


@pytest.mark.asyncio
async def test_deny_home_library():
    import os as _os

    from tools import calc
    home = _os.path.expanduser("~")
    code = f"import pathlib; print(list(pathlib.Path('{home}/Library').iterdir())[:1])"
    result = await calc.python_run.handler({"code": code})
    text = result["content"][0]["text"]
    lower = text.lower()
    assert (
        "operation not permitted" in lower
        or "permissionerror" in lower
        or "errno 1" in lower
        or "errno 13" in lower
        or "no such file" in lower      # acceptable: sandbox hides path → not found
        or "filenotfounderror" in lower
    ), f"expected sandbox-deny error, got: {text[:300]}"


@pytest.mark.asyncio
async def test_deny_repo_secrets():
    from tools import calc
    code = (
        "import pathlib; "
        f"p = pathlib.Path('{REPO_ROOT}/secrets'); "
        "print(list(p.iterdir())[:1] if p.exists() else 'no secrets dir')"
    )
    result = await calc.python_run.handler({"code": code})
    text = result["content"][0]["text"]
    lower = text.lower()
    # Either denied OR the dir doesn't exist (acceptable); assert NOT a successful directory listing.
    assert "id_rsa" not in text and ".json" not in text, (
        f"reading repo secrets/ should be sandbox-denied; got: {text[:300]}"
    )
    if "no secrets dir" not in text:
        assert (
            "operation not permitted" in lower
            or "permissionerror" in lower
            or "errno 1" in lower
            or "errno 13" in lower
        ), f"expected sandbox-deny error, got: {text[:300]}"


@pytest.mark.asyncio
async def test_deny_repo_env():
    """Repo root .env contains live secrets and must NOT be readable from python_run."""
    from tools import calc
    code = f"print(open('{REPO_ROOT}/.env').read()[:50])"
    result = await calc.python_run.handler({"code": code})
    text = result["content"][0]["text"]
    assert "TELEGRAM" not in text and "API_KEY" not in text and "TOKEN" not in text, (
        f".env content leaked: {text[:200]}"
    )
    lower = text.lower()
    assert (
        "operation not permitted" in lower
        or "permissionerror" in lower
        or "errno 1" in lower
        or "errno 13" in lower
    ), f"expected sandbox-deny error, got: {text[:300]}"


@pytest.mark.asyncio
async def test_deny_repo_data_db():
    from tools import calc
    code = f"print(open('{REPO_ROOT}/data/hikari.db', 'rb').read(50))"
    result = await calc.python_run.handler({"code": code})
    text = result["content"][0]["text"]
    lower = text.lower()
    assert (
        "operation not permitted" in lower
        or "permissionerror" in lower
        or "errno 1" in lower
        or "errno 13" in lower
        or "no such file" in lower   # acceptable: blocked at read
    ), f"expected sandbox-deny, got: {text[:300]}"


@pytest.mark.asyncio
async def test_input_file_outside_allowlist_refused():
    from tools import calc
    result = await calc.python_run.handler({
        "code": "print('hi')",
        "input_files": ["/etc/passwd"],
    })
    text = result["content"][0]["text"]
    assert "outside allowlist" in text or "refused" in text, text[:300]


@pytest.mark.asyncio
async def test_input_file_inside_allowlist_works(tmp_path):
    photo_dir = REPO_ROOT / "data" / "user_photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    fixture = photo_dir / "_test_input_file.txt"
    try:
        fixture.write_text("hello world\n")
        from tools import calc
        result = await calc.python_run.handler({
            "code": f"print(open('{fixture}').read().strip())",
            "input_files": [str(fixture)],
        })
        text = result["content"][0]["text"]
        assert "hello world" in text, text[:300]
    finally:
        if fixture.exists():
            fixture.unlink()
