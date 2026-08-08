import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, UploadFile, status

from app.api.deps import CurrentUser, get_resume_service
from app.core.rate_limit import limit_by_user
from app.schemas.common import Page, PageParams
from app.schemas.resume import ResumeRead
from app.services.resume_service import ResumeService

router = APIRouter(prefix="/resumes", tags=["resumes"])

ResumeSvc = Annotated[ResumeService, Depends(get_resume_service)]


# Upload costs storage plus an embedding call per chunk.
@router.post(
    "",
    response_model=ResumeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_user("upload"))],
)
async def upload_resume(file: UploadFile, current_user: CurrentUser, resumes: ResumeSvc) -> ResumeRead:
    content = await file.read()
    resume = await resumes.upload(
        user_id=current_user.id,
        file_name=file.filename or "resume",
        content=content,
        content_type=file.content_type or "application/octet-stream",
    )
    return ResumeRead.model_validate(resume)


@router.get("", response_model=Page[ResumeRead])
async def list_resumes(
    current_user: CurrentUser,
    resumes: ResumeSvc,
    params: Annotated[PageParams, Depends()],
) -> Page[ResumeRead]:
    items, total = await resumes.list(current_user.id, offset=params.offset, limit=params.size)
    return Page(
        items=[ResumeRead.model_validate(r) for r in items],
        total=total,
        page=params.page,
        size=params.size,
    )


@router.get("/{resume_id}", response_model=ResumeRead)
async def get_resume(
    resume_id: uuid.UUID, current_user: CurrentUser, resumes: ResumeSvc
) -> ResumeRead:
    resume = await resumes.get(resume_id, current_user.id)
    return ResumeRead.model_validate(resume)


@router.get("/{resume_id}/download")
async def download_resume(
    resume_id: uuid.UUID, current_user: CurrentUser, resumes: ResumeSvc
) -> Response:
    resume, content = await resumes.download(resume_id, current_user.id)
    return Response(
        content=content,
        media_type=resume.content_type,
        headers={"Content-Disposition": f'attachment; filename="{resume.file_name}"'},
    )


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_resume(
    resume_id: uuid.UUID, current_user: CurrentUser, resumes: ResumeSvc
) -> None:
    await resumes.delete(resume_id, current_user.id)
