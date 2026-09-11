"""Preview and stage a GitHub release as governed TestOps drafts."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from testops.contracts import CaseBaseline, CaseDefinition, TargetType, canonical_sha256

from .case_change_policy import (
    changed_fields,
    comparable_case_document,
    requires_execution_validation,
)
from .github_release import FetchedGitRelease
from .persistence import (
    AutomationPackageRecord,
    CaseBaselineRecord,
    ChangeItemRecord,
    ChangeRequestRecord,
    TestTargetRecord,
)
from .schemas import (
    AutomationPackageResponse,
    GitReleaseApplyRequest,
    GitReleaseApplyResponse,
    GitReleaseCaseChange,
    GitReleasePreviewResponse,
)
from .services import InvalidRequest, ResourceConflict, ResourceNotFound, _audit, _project


@dataclass(frozen=True, slots=True)
class _CaseDiff:
    documents: tuple[dict[str, object], ...]
    unchanged_count: int


def _case_document(case: CaseDefinition) -> dict[str, object]:
    return comparable_case_document(case)


def _diff_cases(base: CaseBaseline, candidate: CaseBaseline) -> _CaseDiff:
    base_by_code = {case.case_code: case for case in base.cases}
    candidate_by_code = {case.case_code: case for case in candidate.cases}
    documents: list[dict[str, object]] = []
    unchanged_count = 0

    for case in candidate.cases:
        before_case = base_by_code.get(case.case_code)
        if before_case is None:
            before = None
            after = _case_document(case)
            change_type = "ADD"
        else:
            if before_case.case_id != case.case_id:
                raise InvalidRequest(
                    f"stable case_id changed for existing case_code: {case.case_code}"
                )
            before = _case_document(before_case)
            after = _case_document(case)
            if before == after:
                unchanged_count += 1
                continue
            change_type = "MODIFY"
        documents.append(
            {
                "sequence": len(documents) + 1,
                "change_type": change_type,
                "case_id": case.case_id,
                "case_code": case.case_code,
                "before_document": before,
                "after_document": after,
                "changed_fields": changed_fields(before, after),
            }
        )

    for case in base.cases:
        if case.case_code in candidate_by_code:
            continue
        before = _case_document(case)
        documents.append(
            {
                "sequence": len(documents) + 1,
                "change_type": "DELETE",
                "case_id": case.case_id,
                "case_code": case.case_code,
                "before_document": before,
                "after_document": None,
                "changed_fields": ["$case"],
            }
        )
    if len(documents) > 500:
        raise InvalidRequest("a Git release may stage at most 500 case changes")
    return _CaseDiff(documents=tuple(documents), unchanged_count=unchanged_count)


async def _release_context(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    fetched: FetchedGitRelease,
) -> tuple[CaseBaselineRecord, CaseBaseline, _CaseDiff, AutomationPackageRecord | None]:
    project = await _project(session, project_id)
    target = await session.get(TestTargetRecord, target_id)
    if target is None or target.project_id != project_id:
        raise ResourceNotFound("target not found in project")
    if target.target_type != TargetType.WEB.value:
        raise InvalidRequest("Git Web release sync requires a WEB target")

    manifest = fetched.manifest
    candidate = manifest.baseline
    if candidate.project_key != project.key:
        raise InvalidRequest("release baseline project_key does not match the selected project")
    if canonical_sha256(candidate) != manifest.baseline_digest:
        raise InvalidRequest("release baseline digest does not match its canonical document")

    base_record = await session.scalar(
        select(CaseBaselineRecord)
        .where(
            CaseBaselineRecord.project_id == project_id,
            CaseBaselineRecord.status == "RELEASED",
        )
        .order_by(CaseBaselineRecord.created_at.desc())
        .limit(1)
    )
    if base_record is None:
        raise ResourceConflict(
            "Git release sync requires an initial RELEASED baseline for comparison"
        )
    base = CaseBaseline.model_validate(base_record.document)
    diff = _diff_cases(base, candidate)
    if diff.documents and candidate.version == base.version:
        raise ResourceConflict("a changed release baseline must use a new case baseline version")
    if diff.documents and candidate.baseline_id == base.baseline_id:
        raise ResourceConflict("a changed release baseline must use a new baseline_id")

    package = manifest.automation_package
    supersedes = await session.scalar(
        select(AutomationPackageRecord)
        .where(
            AutomationPackageRecord.project_id == project_id,
            AutomationPackageRecord.target_id == target_id,
            AutomationPackageRecord.name == package.name,
            AutomationPackageRecord.version != package.version,
            AutomationPackageRecord.status != "REVOKED",
        )
        .order_by(AutomationPackageRecord.created_at.desc())
        .limit(1)
    )
    return base_record, base, diff, supersedes


async def preview_git_release(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    fetched: FetchedGitRelease,
) -> GitReleasePreviewResponse:
    base_record, _base, diff, supersedes = await _release_context(
        session,
        project_id,
        target_id,
        fetched,
    )
    manifest = fetched.manifest
    candidate = manifest.baseline
    counts = {"ADD": 0, "MODIFY": 0, "DELETE": 0}
    changes: list[GitReleaseCaseChange] = []
    for document in diff.documents:
        change_type = str(document["change_type"])
        counts[change_type] += 1
        changes.append(
            GitReleaseCaseChange(
                change_type=change_type,
                case_id=document["case_id"],
                case_code=str(document["case_code"]),
                changed_fields=tuple(document["changed_fields"]),
            )
        )
    return GitReleasePreviewResponse(
        repository_url=fetched.repository_url,
        tag=fetched.tag,
        revision=fetched.revision,
        base_baseline_id=base_record.baseline_id,
        base_baseline_version=base_record.version,
        base_baseline_digest=base_record.digest,
        candidate_baseline_id=candidate.baseline_id,
        candidate_baseline_version=candidate.version,
        candidate_baseline_digest=manifest.baseline_digest,
        case_count=len(candidate.cases),
        enabled_case_count=sum(case.enabled for case in candidate.cases),
        added_count=counts["ADD"],
        modified_count=counts["MODIFY"],
        deleted_count=counts["DELETE"],
        unchanged_count=diff.unchanged_count,
        baseline_changed=bool(diff.documents),
        changes=tuple(changes),
        automation_package=manifest.automation_package,
        supersedes_id=supersedes.id if supersedes is not None else None,
    )


def _same_package(
    record: AutomationPackageRecord,
    fetched: FetchedGitRelease,
) -> bool:
    package = fetched.manifest.automation_package
    return (
        record.digest == package.digest
        and record.runner_type == package.runner_type
        and record.image_repository == package.image_repository
    )


async def apply_git_release(
    session: AsyncSession,
    project_id: UUID,
    target_id: UUID,
    fetched: FetchedGitRelease,
    payload: GitReleaseApplyRequest,
    actor_id: UUID,
) -> GitReleaseApplyResponse:
    manifest = fetched.manifest
    if fetched.revision != payload.expected_revision:
        raise ResourceConflict("Git tag revision changed after preview; preview the release again")
    if manifest.baseline_digest != payload.expected_baseline_digest:
        raise ResourceConflict("baseline digest changed after preview; preview the release again")
    if manifest.automation_package.digest != payload.expected_package_digest:
        raise ResourceConflict("automation package digest changed after preview; preview again")

    base_record, _base, diff, supersedes = await _release_context(
        session,
        project_id,
        target_id,
        fetched,
    )
    candidate = manifest.baseline
    candidate_document = candidate.model_dump(mode="json", exclude_none=True)
    change_request: ChangeRequestRecord | None = None

    if diff.documents:
        change_request = await session.scalar(
            select(ChangeRequestRecord).where(
                ChangeRequestRecord.project_id == project_id,
                ChangeRequestRecord.candidate_version == candidate.version,
            )
        )
        if change_request is not None:
            if (
                change_request.candidate_baseline_id != candidate.baseline_id
                or change_request.candidate_digest != manifest.baseline_digest
                or change_request.candidate_document != candidate_document
            ):
                raise ResourceConflict(
                    "candidate baseline version already exists with other content"
                )
        else:
            existing_baseline = await session.get(CaseBaselineRecord, candidate.baseline_id)
            if existing_baseline is not None:
                raise ResourceConflict("candidate baseline_id already exists")
            change_request = ChangeRequestRecord(
                id=uuid4(),
                project_id=project_id,
                base_baseline_id=base_record.baseline_id,
                candidate_baseline_id=candidate.baseline_id,
                candidate_version=candidate.version,
                candidate_digest=manifest.baseline_digest,
                candidate_document=candidate_document,
                title=f"Sync {fetched.repository_url}@{fetched.tag}"[:200],
                reason=(
                    "Imported from an immutable GitHub Release manifest. "
                    f"repository={fetched.repository_url}; tag={fetched.tag}; "
                    f"revision={fetched.revision}"
                ),
                created_by=actor_id,
                validation_status=(
                    "PENDING_EXECUTION"
                    if any(
                        requires_execution_validation(
                            change_type=str(item["change_type"]),
                            after_document=item["after_document"],
                            fields=item["changed_fields"],
                        )
                        for item in diff.documents
                    )
                    else "NOT_REQUIRED"
                ),
            )
            session.add(change_request)
            try:
                await session.flush((change_request,))
            except IntegrityError as exc:
                await session.rollback()
                raise ResourceConflict("candidate baseline version already exists") from exc
            session.add_all(
                [
                    ChangeItemRecord(
                        id=uuid4(),
                        change_request_id=change_request.id,
                        **document,
                    )
                    for document in diff.documents
                ]
            )
            session.add(
                CaseBaselineRecord(
                    baseline_id=candidate.baseline_id,
                    project_id=project_id,
                    version=candidate.version,
                    digest=manifest.baseline_digest,
                    case_count=len(candidate.cases),
                    enabled_case_count=sum(case.enabled for case in candidate.cases),
                    source_kind=candidate.source.kind,
                    status="DRAFT",
                    document=candidate_document,
                )
            )
            session.add(
                _audit(
                    actor_id=actor_id,
                    action="change_request.git_release_imported",
                    resource_type="change_request",
                    resource_id=change_request.id,
                    project_id=project_id,
                    details={
                        "repository_url": fetched.repository_url,
                        "tag": fetched.tag,
                        "revision": fetched.revision,
                        "candidate_digest": manifest.baseline_digest,
                        "change_count": len(diff.documents),
                    },
                )
            )

    package_manifest = manifest.automation_package
    package_record = await session.scalar(
        select(AutomationPackageRecord).where(
            AutomationPackageRecord.target_id == target_id,
            AutomationPackageRecord.name == package_manifest.name,
            AutomationPackageRecord.version == package_manifest.version,
        )
    )
    if package_record is not None:
        if not _same_package(package_record, fetched):
            raise ResourceConflict("automation package version already exists with other content")
    else:
        package_record = AutomationPackageRecord(
            id=uuid4(),
            project_id=project_id,
            target_id=target_id,
            name=package_manifest.name,
            version=package_manifest.version,
            digest=package_manifest.digest,
            runner_type=package_manifest.runner_type,
            image_repository=package_manifest.image_repository,
            status="DRAFT",
            supersedes_id=supersedes.id if supersedes is not None else None,
            supply_chain_status="PENDING",
        )
        session.add(package_record)
        session.add(
            _audit(
                actor_id=actor_id,
                action="automation_package.git_release_imported",
                resource_type="automation_package",
                resource_id=package_record.id,
                project_id=project_id,
                details={
                    "repository_url": fetched.repository_url,
                    "tag": fetched.tag,
                    "revision": fetched.revision,
                    "digest": package_record.digest,
                    "candidate_baseline_id": (
                        str(candidate.baseline_id) if diff.documents else None
                    ),
                },
            )
        )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ResourceConflict("Git release has already been imported with other content") from exc
    await session.refresh(package_record)
    if change_request is not None:
        await session.refresh(change_request)
    return GitReleaseApplyResponse(
        repository_url=fetched.repository_url,
        tag=fetched.tag,
        revision=fetched.revision,
        baseline_changed=bool(diff.documents),
        baseline_id=(candidate.baseline_id if diff.documents else base_record.baseline_id),
        change_request_id=change_request.id if change_request is not None else None,
        automation_package=AutomationPackageResponse.model_validate(package_record),
    )
