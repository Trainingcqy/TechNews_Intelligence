from __future__ import annotations

import importlib
import os
import re
import sys
import types
import uuid
from contextlib import contextmanager
from collections.abc import Callable, Iterator
from pathlib import Path
from shutil import rmtree
from unittest.mock import AsyncMock, MagicMock

import pytest


# Add the project root directory to sys.path so tests can import local packages.
project_root = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import common.script_env  # noqa: E402

# Tests never read the developer's .env, which holds real keys and the production database address.
common.script_env.load_env = lambda path=None: None

import psycopg2  # noqa: E402
import psycopg2.pool  # noqa: E402


def _no_database(*args, **kwargs):
    raise RuntimeError("unit tests must not connect to the database")


# The configured database is the production one; a test that tries to connect fails instead of reaching it.
psycopg2.connect = _no_database
psycopg2.pool.SimpleConnectionPool = _no_database
psycopg2.pool.ThreadedConnectionPool = _no_database

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_PATH_COMPAT_ROOT = PROJECT_ROOT / "tests" / ".tmp_pytest_compat"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _remove_tree(path: Path, *, root: Path) -> None:
    """Best-effort cleanup for transient pytest artifacts."""
    if not path.exists():
        return
    if not _is_within(path, root):
        return
    if not path.is_dir():
        return
    try:
        rmtree(path, ignore_errors=True)
    except Exception:
        # Keep test exit robust: cleanup errors should not fail test runs.
        return


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG001
    _remove_tree(TMP_PATH_COMPAT_ROOT, root=PROJECT_ROOT)


def _sanitize_tmp_node_name(node_name: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_.-]+", "_", node_name).strip("_")
    return candidate[:64] or "tmp_case"


@pytest.fixture()
def tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    """Compatibility tmp_path fixture for restricted Windows sandboxes."""
    TMP_PATH_COMPAT_ROOT.mkdir(parents=True, exist_ok=True)
    case_name = _sanitize_tmp_node_name(request.node.name)
    case_dir = TMP_PATH_COMPAT_ROOT / f"{case_name}_{uuid.uuid4().hex[:8]}"
    case_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield case_dir
    finally:
        _remove_tree(case_dir, root=PROJECT_ROOT)


@pytest.fixture()
def models_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Replace config/models.toml with the given sections plus two providers."""
    from common import model_settings

    def write(sections: str) -> None:
        path = tmp_path / "models.toml"
        providers = '[providers.deepseek]\ndefault_model = "d"\n[providers.vertex]\ndefault_model = "v"\n'
        path.write_text(sections + providers, encoding="utf-8")
        monkeypatch.setattr(model_settings, "MODELS_FILE", path)
        model_settings._settings.cache_clear()

    yield write
    model_settings._settings.cache_clear()


@pytest.fixture()
def agent_dependency_stubs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide lightweight stubs for DB-related optional dependencies."""
    if "common" not in sys.modules:
        try:
            importlib.import_module("common")
        except Exception:
            common_mod = types.ModuleType("common")
            common_mod.__path__ = []
            monkeypatch.setitem(sys.modules, "common", common_mod)

    db_mod = types.ModuleType("common.db")
    db_mod.get_conn = MagicMock()
    db_mod.put_conn = MagicMock()
    db_mod.init_db_pool = MagicMock()
    db_mod.close_db_pool = MagicMock()

    @contextmanager
    def _unconfigured_db_context():
        raise RuntimeError("common.db test stub is not configured for DB access")
        yield  # pragma: no cover

    db_mod.db_cursor = _unconfigured_db_context
    db_mod.db_transaction = _unconfigured_db_context
    monkeypatch.setitem(sys.modules, "common.db", db_mod)

    psycopg2_mod = types.ModuleType("psycopg2")
    psycopg2_extras = types.ModuleType("psycopg2.extras")
    psycopg2_pool = types.ModuleType("psycopg2.pool")

    psycopg2_extras.Json = lambda value: value
    psycopg2_pool.SimpleConnectionPool = MagicMock()
    psycopg2_pool.ThreadedConnectionPool = MagicMock()
    psycopg2_mod.pool = psycopg2_pool
    psycopg2_mod.extras = psycopg2_extras
    psycopg2_mod.extensions = types.SimpleNamespace(
        TRANSACTION_STATUS_IDLE=0,
        connection=object,
        cursor=object,
    )

    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2_mod)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", psycopg2_extras)
    monkeypatch.setitem(sys.modules, "psycopg2.pool", psycopg2_pool)
    yield


@pytest.fixture()
def email_validator_stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub email_validator and pydantic metadata lookup for EmailStr tests."""
    email_validator_mod = types.ModuleType("email_validator")

    class EmailNotValidError(ValueError):
        pass

    def _fake_validate_email(email: str, *_args, **_kwargs):
        return types.SimpleNamespace(email=email, normalized=email, local_part=email.split("@", 1)[0])

    email_validator_mod.EmailNotValidError = EmailNotValidError
    email_validator_mod.validate_email = _fake_validate_email
    monkeypatch.setitem(sys.modules, "email_validator", email_validator_mod)

    import pydantic.networks as pydantic_networks

    monkeypatch.setattr(pydantic_networks, "version", lambda _name: "2.0.0", raising=False)
    yield


@pytest.fixture()
def telegram_import_stubs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub python-telegram-bot imports for transport-layer bot tests."""
    telegram_mod = types.ModuleType("telegram")

    class Update:  # pragma: no cover - structure-only stub
        pass

    class BotCommand:  # pragma: no cover - structure-only stub
        def __init__(self, command: str, description: str):
            self.command = command
            self.description = description

    telegram_mod.Update = Update
    telegram_mod.BotCommand = BotCommand

    ext_mod = types.ModuleType("telegram.ext")

    class _Builder:  # pragma: no cover - structure-only stub
        def token(self, *_args, **_kwargs):
            return self

        def post_init(self, *_args, **_kwargs):
            return self

        def post_shutdown(self, *_args, **_kwargs):
            return self

        def build(self):
            return types.SimpleNamespace(
                add_handler=lambda *_a, **_k: None,
                run_polling=lambda *_a, **_k: None,
                bot=types.SimpleNamespace(set_my_commands=AsyncMock()),
            )

    class _ContextTypes:  # pragma: no cover - structure-only stub
        DEFAULT_TYPE = object

    ext_mod.ApplicationBuilder = _Builder
    ext_mod.CommandHandler = lambda *_a, **_k: None
    ext_mod.MessageHandler = lambda *_a, **_k: None
    ext_mod.ContextTypes = _ContextTypes
    ext_mod.filters = types.SimpleNamespace(TEXT=1, COMMAND=2)

    monkeypatch.setitem(sys.modules, "telegram", telegram_mod)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext_mod)
    yield
