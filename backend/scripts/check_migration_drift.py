"""Fail if the ORM models and the migrations have diverged.

Compares Base.metadata against the live, already-migrated database using
alembic's autogenerate machinery, and prints the differences instead of
writing a revision file.

Run this after `alembic upgrade head`. A non-empty diff means someone changed a
model without generating a migration -- which otherwise stays invisible until a
deploy fails or a query hits a column that does not exist.

    python scripts/check_migration_drift.py
"""

import asyncio
import sys
from pathlib import Path

# Allow running as a plain script from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

import app.models  # noqa: F401,E402  (registers every table on Base.metadata)
from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402


def _diff(connection) -> list:
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "target_metadata": Base.metadata}
    )
    return compare_metadata(context, Base.metadata)


async def main() -> int:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as connection:
            differences = await connection.run_sync(_diff)
    finally:
        await engine.dispose()

    if differences:
        print("Models and migrations have drifted:\n")
        for difference in differences:
            print(f"  {difference}")
        print(
            "\nGenerate a migration with:\n"
            '  alembic revision --autogenerate -m "describe the change"'
        )
        return 1

    print("No drift: the migrated schema matches the models.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
