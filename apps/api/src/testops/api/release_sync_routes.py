"""HTTP entrypoints for GitHub Release preview and governed draft import."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .github_release import GitReleaseFetcher
from .identity import CurrentPrincipal, authorize_project
from .release_sync import apply_git_release, preview_git_release
from .schemas import (
    GitReleaseApplyRequest,
    GitReleaseApplyResponse,
    GitReleasePreviewResponse,
    GitReleaseSyncRequest,
)

Session = Annotated[AsyncSession, Depends(get_session)]
router = APIRouter(prefix="/api/v1")


def _release_fetcher(request: Request) -> GitReleaseFetcher:
    return request.app.state.git_release_fetcher


ReleaseFetcher = Annotated[GitReleaseFetcher, Depends(_release_fetcher)]


@router.post(
    "/projects/{project_id}/targets/{target_id}/automation-packages/git-release/preview",
    response_model=GitReleasePreviewResponse,
    tags=["automation-packages"],
)
async def post_git_release_preview(
    project_id: UUID,
    target_id: UUID,
    payload: GitReleaseSyncRequest,
    principal: CurrentPrincipal,
    session: Session,
    fetcher: ReleaseFetcher,
) -> GitReleasePreviewResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    fetched = await fetcher.fetch(payload)
    return await preview_git_release(session, project_id, target_id, fetched)


@router.post(
    "/projects/{project_id}/targets/{target_id}/automation-packages/git-release/apply",
    response_model=GitReleaseApplyResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["automation-packages"],
)
async def post_git_release_apply(
    project_id: UUID,
    target_id: UUID,
    payload: GitReleaseApplyRequest,
    principal: CurrentPrincipal,
    session: Session,
    fetcher: ReleaseFetcher,
) -> GitReleaseApplyResponse:
    await authorize_project(session, principal, project_id, "project:manage")
    fetched = await fetcher.fetch(payload)
    return await apply_git_release(
        session,
        project_id,
        target_id,
        fetched,
        payload,
        principal.user_id,
    )
