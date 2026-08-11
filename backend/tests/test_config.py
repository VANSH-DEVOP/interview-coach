"""Where configuration comes from, and from where it is read.

`env_file=".env"` is resolved against the process working directory. That made
`uvicorn app.main:app` from `backend/` read a different file than the same
command from the repository root -- and reading *no* file was silent, because
every setting has a default. The application started cleanly with no Gemini key
and Postgres on 5432, which looks exactly like a revoked key and a stopped
database.

These pin the fix: the paths are absolute and derived from the module, so every
entry point reads the same configuration from any directory.
"""

from pathlib import Path

from app.core import config
from app.core.config import Settings


def test_env_files_are_absolute() -> None:
    """A relative entry here is the bug returning: it would resolve against
    whatever directory the process happens to be started from."""
    env_files = Settings.model_config["env_file"]

    assert all(Path(path).is_absolute() for path in env_files)


def test_the_env_files_are_the_repo_root_and_the_backend_directory() -> None:
    root_env, backend_env = (Path(p) for p in Settings.model_config["env_file"])

    assert backend_env.parent.name == "backend"
    assert backend_env.parent.parent == root_env.parent
    assert root_env.name == backend_env.name == ".env"


def test_the_backend_file_wins_where_the_two_disagree(tmp_path, monkeypatch) -> None:
    """Ordered least- to most-specific, which is what lets a repository-root
    `.env` hold the shared settings while `backend/.env` overrides only what
    genuinely differs between a container and a host process -- in practice
    POSTGRES_PORT, since a host reaches the database on the published port."""
    root = tmp_path / ".env"
    backend = tmp_path / "backend" / ".env"
    backend.parent.mkdir()
    root.write_text("POSTGRES_PORT=5432\nPOSTGRES_DB=shared\n")
    backend.write_text("POSTGRES_PORT=5434\n")

    class Scoped(Settings):
        model_config = {**Settings.model_config, "env_file": (root, backend)}

    settings = Scoped()

    assert settings.POSTGRES_PORT == 5434
    # And the root file still supplies what the specific one does not set.
    assert settings.POSTGRES_DB == "shared"


def test_a_missing_env_file_is_not_an_error(tmp_path) -> None:
    """The Docker image copies only the backend directory, so the
    repository-root path does not exist inside a container. Containers are
    configured from the environment, and a missing file must be ignored rather
    than fatal."""

    class Scoped(Settings):
        model_config = {**Settings.model_config, "env_file": (tmp_path / "nope.env",)}

    assert Scoped().POSTGRES_USER == "interviewpilot"


def test_the_module_locates_its_own_directories() -> None:
    assert config._BACKEND_DIR.name == "backend"
    assert (config._BACKEND_DIR / "app" / "core" / "config.py").exists()
    assert config._REPO_ROOT == config._BACKEND_DIR.parent
