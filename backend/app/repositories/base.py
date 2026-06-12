"""Generic async repository.

Repositories are the only layer that builds SQLAlchemy queries. Services
receive and return ORM entities; routes never see a session.
"""

import uuid
from typing import Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        return await self.session.get(self.model, entity_id)

    async def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        await self.session.flush()
        await self.session.refresh(entity)
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)
        await self.session.flush()

    async def count(self, *whereclauses) -> int:
        stmt = select(func.count()).select_from(self.model)
        for clause in whereclauses:
            stmt = stmt.where(clause)
        return (await self.session.execute(stmt)).scalar_one()
