from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from testops.api.config import Settings
from testops.api.main import create_app
from testops.api.persistence import AuthSessionRecord, SystemSettingRecord, UserRecord

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_TOKEN = "governance-bootstrap-token"
ADMIN_PASSWORD = "governance-admin-password"
USER_PASSWORD = "governance-user-password"
RUNNER_HEADERS = {"X-Runner-Token": "governance-runner-token"}
PACKAGE_DIGEST = "sha256:" + "e" * 64


class IdentityAndGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "governance.sqlite3"
        self.app = create_app(
            Settings(
                database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
                auto_create_schema=True,
                runner_callback_token=RUNNER_HEADERS["X-Runner-Token"],
                bootstrap_admin_token=BOOTSTRAP_TOKEN,
            )
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        bootstrap = self.client.post(
            "/api/v1/auth/bootstrap",
            headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
            json={
                "username": "admin",
                "display_name": "System Admin",
                "password": ADMIN_PASSWORD,
            },
        )
        self.assertEqual(bootstrap.status_code, 201, bootstrap.text)
        self.admin = bootstrap.json()
        self.admin_headers, self.admin_token = self._login("admin", ADMIN_PASSWORD)

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def _login(self, username: str, password: str) -> tuple[dict[str, str], str]:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}, token

    def _create_user(self, username: str, display_name: str) -> dict[str, object]:
        response = self.client.post(
            "/api/v1/users",
            headers=self.admin_headers,
            json={
                "username": username,
                "display_name": display_name,
                "password": USER_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _add_member(self, project_id: str, user_id: str, role: str) -> None:
        response = self.client.put(
            f"/api/v1/projects/{project_id}/members",
            headers=self.admin_headers,
            json={"user_id": user_id, "role": role},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _bootstrap_project(self) -> dict[str, str]:
        project = self.client.post(
            "/api/v1/projects",
            headers=self.admin_headers,
            json={"key": "yanjia-ai-web", "name": "颜佳 AI Web"},
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        target = self.client.post(
            f"/api/v1/projects/{project_id}/targets",
            headers=self.admin_headers,
            json={
                "key": "web",
                "name": "Web 管理端",
                "target_type": "WEB",
                "browser": "chromium",
            },
        )
        self.assertEqual(target.status_code, 201, target.text)
        target_id = target.json()["id"]
        environment = self.client.post(
            f"/api/v1/projects/{project_id}/targets/{target_id}/environments",
            headers=self.admin_headers,
            json={
                "key": "staging",
                "name": "测试环境",
                "web_config": {"base_url": "https://example.invalid/login"},
                "secret_bindings": [
                    {
                        "name": "TEST_USERNAME",
                        "ref": "secret://yanjia/staging/test-username",
                    },
                    {
                        "name": "TEST_PASSWORD",
                        "ref": "secret://yanjia/staging/test-password",
                    },
                ],
            },
        )
        self.assertEqual(environment.status_code, 201, environment.text)

        baseline_directory = ROOT / "baselines/yanjia-ai-web/case-v1.0.1"
        baseline_document = json.loads(
            (baseline_directory / "case-baseline.json").read_text("utf-8")
        )
        manifest = json.loads((baseline_directory / "manifest.json").read_text("utf-8"))
        baseline = self.client.post(
            f"/api/v1/projects/{project_id}/baselines",
            headers=self.admin_headers,
            json={
                "baseline": baseline_document,
                "digest": manifest["baseline"]["digest"],
            },
        )
        self.assertEqual(baseline.status_code, 201, baseline.text)
        package = self.client.post(
            f"/api/v1/projects/{project_id}/targets/{target_id}/automation-packages",
            headers=self.admin_headers,
            json={"name": "yanjia-web", "version": "0.1.0", "digest": PACKAGE_DIGEST},
        )
        self.assertEqual(package.status_code, 201, package.text)
        return {
            "project_id": project_id,
            "target_id": target_id,
            "environment_id": environment.json()["id"],
            "baseline_id": baseline.json()["baseline_id"],
            "package_id": package.json()["id"],
        }

    def _complete_run(self, run_id: str, viewer_headers: dict[str, str]) -> dict[str, object]:
        preparing = self.client.post(
            f"/api/v1/internal/runs/{run_id}/status",
            headers=RUNNER_HEADERS,
            json={"status": "PREPARING"},
        )
        self.assertEqual(preparing.status_code, 200, preparing.text)
        running = self.client.post(
            f"/api/v1/internal/runs/{run_id}/status",
            headers=RUNNER_HEADERS,
            json={"status": "RUNNING"},
        )
        self.assertEqual(running.status_code, 200, running.text)
        snapshot_response = self.client.get(f"/api/v1/runs/{run_id}", headers=viewer_headers)
        self.assertEqual(snapshot_response.status_code, 200, snapshot_response.text)
        snapshot = snapshot_response.json()["snapshot"]
        started = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
        finished = started + timedelta(seconds=1)
        case_results = [
            {
                "case_id": case["case_id"],
                "case_code": case["case_code"],
                "status": "PASSED",
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "duration_ms": 1000,
            }
            for case in snapshot["cases"]
        ]
        result = {
            "run_id": run_id,
            "status": "PASSED",
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "runner_version": "0.1.0",
            "case_results": case_results,
            "artifacts": [],
        }
        accepted = self.client.post(
            f"/api/v1/internal/runs/{run_id}/result",
            headers=RUNNER_HEADERS,
            json=result,
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        return snapshot

    def test_bootstrap_login_session_hashing_and_project_rbac(self) -> None:
        repeated = self.client.post(
            "/api/v1/auth/bootstrap",
            headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
            json={
                "username": "second-admin",
                "display_name": "Second Admin",
                "password": ADMIN_PASSWORD,
            },
        )
        self.assertEqual(repeated.status_code, 409, repeated.text)
        bad_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "incorrect-password"},
        )
        self.assertEqual(bad_login.status_code, 401, bad_login.text)
        me = self.client.get("/api/v1/auth/me", headers=self.admin_headers)
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["system_role"], "SYSTEM_ADMIN")
        lowercase_scheme = self.client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"bearer {self.admin_token}"},
        )
        self.assertEqual(lowercase_scheme.status_code, 200, lowercase_scheme.text)

        resources = self._bootstrap_project()
        viewer = self._create_user("viewer", "Read Only")
        self._add_member(resources["project_id"], str(viewer["id"]), "VIEWER")
        viewer_headers, viewer_token = self._login("viewer", USER_PASSWORD)
        projects = self.client.get("/api/v1/projects", headers=viewer_headers)
        self.assertEqual([item["id"] for item in projects.json()], [resources["project_id"]])
        denied = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/targets",
            headers=viewer_headers,
            json={"key": "denied", "name": "Denied", "target_type": "WEB"},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        policy_url = f"/api/v1/projects/{resources['project_id']}/execution-policy"
        readable_policy = self.client.get(policy_url, headers=viewer_headers)
        self.assertEqual(readable_policy.status_code, 200, readable_policy.text)
        denied_policy_update = self.client.patch(
            policy_url,
            headers=viewer_headers,
            json={"max_in_flight_runs": 5},
        )
        self.assertEqual(denied_policy_update.status_code, 403, denied_policy_update.text)
        pool = self.client.post(
            "/api/v1/admin/runner-pools",
            headers=self.admin_headers,
            json={
                "key": "governance-web",
                "name": "Governance Web Pool",
                "target_types": ["WEB"],
                "max_concurrency": 2,
            },
        )
        self.assertEqual(pool.status_code, 201, pool.text)
        readable_pool_catalog = self.client.get(
            "/api/v1/runner-pools/catalog",
            headers=viewer_headers,
        )
        self.assertEqual(readable_pool_catalog.status_code, 200, readable_pool_catalog.text)
        self.assertEqual(readable_pool_catalog.json()[0]["key"], "governance-web")
        denied_pool_admin = self.client.get(
            "/api/v1/admin/runner-pools",
            headers=viewer_headers,
        )
        self.assertEqual(denied_pool_admin.status_code, 403, denied_pool_admin.text)
        denied_worker_heartbeat = self.client.put(
            "/api/v1/internal/runner-workers/governance-worker/heartbeat",
            json={
                "pool_key": "governance-web",
                "display_name": "Governance Worker",
                "runner_version": "0.10.0",
                "capabilities": {"target_types": ["WEB"], "browsers": ["chromium"]},
            },
        )
        self.assertEqual(denied_worker_heartbeat.status_code, 401, denied_worker_heartbeat.text)
        unauthenticated = self.client.get("/api/v1/projects")
        self.assertEqual(unauthenticated.status_code, 401, unauthenticated.text)

        async def stored_secrets() -> tuple[str, str, dict[str, str]]:
            async with self.app.state.session_factory() as session:
                user = await session.scalar(
                    select(UserRecord).where(UserRecord.username == "admin")
                )
                viewer_user = await session.scalar(
                    select(UserRecord).where(UserRecord.username == "viewer")
                )
                assert viewer_user is not None
                auth_session = await session.scalar(
                    select(AuthSessionRecord).where(AuthSessionRecord.user_id == viewer_user.id)
                )
                bootstrap_setting = await session.get(
                    SystemSettingRecord,
                    "identity.bootstrapped",
                )
                assert user is not None and auth_session is not None
                assert bootstrap_setting is not None
                return user.password_hash, auth_session.token_hash, bootstrap_setting.value

        password_hash, token_hash, bootstrap_setting = asyncio.run(stored_secrets())
        self.assertTrue(password_hash.startswith("scrypt$"))
        self.assertNotIn(ADMIN_PASSWORD, password_hash)
        self.assertNotEqual(token_hash, viewer_token)
        self.assertEqual(bootstrap_setting["username"], "admin")

        logged_out = self.client.post("/api/v1/auth/logout", headers=viewer_headers)
        self.assertEqual(logged_out.status_code, 204, logged_out.text)
        expired = self.client.get("/api/v1/auth/me", headers=viewer_headers)
        self.assertEqual(expired.status_code, 401, expired.text)

    def test_system_user_session_and_audit_management(self) -> None:
        managed_user = self._create_user("managed-user", "Managed User")
        managed_headers, _ = self._login("managed-user", USER_PASSWORD)

        denied = self.client.get("/api/v1/admin/users", headers=managed_headers)
        self.assertEqual(denied.status_code, 403, denied.text)
        users = self.client.get(
            "/api/v1/admin/users?query=managed&status=ACTIVE&limit=10",
            headers=self.admin_headers,
        )
        self.assertEqual(users.status_code, 200, users.text)
        self.assertEqual(users.json()["total"], 1)
        self.assertEqual(users.json()["items"][0]["username"], "managed-user")

        sessions = self.client.get(
            f"/api/v1/admin/sessions?user_id={managed_user['id']}",
            headers=self.admin_headers,
        )
        self.assertEqual(sessions.status_code, 200, sessions.text)
        self.assertEqual(sessions.json()["total"], 1)
        session_id = sessions.json()["items"][0]["id"]
        revoked = self.client.post(
            f"/api/v1/admin/sessions/{session_id}/revoke",
            headers=self.admin_headers,
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertFalse(revoked.json()["active"])
        self.assertEqual(
            self.client.get("/api/v1/auth/me", headers=managed_headers).status_code,
            401,
        )

        active_headers, _ = self._login("managed-user", USER_PASSWORD)
        disabled = self.client.patch(
            f"/api/v1/admin/users/{managed_user['id']}",
            headers=self.admin_headers,
            json={"display_name": "Disabled User", "status": "DISABLED"},
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(disabled.json()["status"], "DISABLED")
        self.assertEqual(
            self.client.get("/api/v1/auth/me", headers=active_headers).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/login",
                json={"username": "managed-user", "password": USER_PASSWORD},
            ).status_code,
            401,
        )

        self_protection = self.client.patch(
            f"/api/v1/admin/users/{self.admin['id']}",
            headers=self.admin_headers,
            json={"system_role": "USER"},
        )
        self.assertEqual(self_protection.status_code, 409, self_protection.text)

        enabled = self.client.patch(
            f"/api/v1/admin/users/{managed_user['id']}",
            headers=self.admin_headers,
            json={"status": "ACTIVE"},
        )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        before_reset_headers, _ = self._login("managed-user", USER_PASSWORD)
        replacement_password = "replacement-user-password"
        reset = self.client.post(
            f"/api/v1/admin/users/{managed_user['id']}/password-reset",
            headers=self.admin_headers,
            json={"password": replacement_password},
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(
            self.client.get("/api/v1/auth/me", headers=before_reset_headers).status_code,
            401,
        )
        old_password = self.client.post(
            "/api/v1/auth/login",
            json={"username": "managed-user", "password": USER_PASSWORD},
        )
        self.assertEqual(old_password.status_code, 401, old_password.text)
        self._login("managed-user", replacement_password)

        audit = self.client.get(
            "/api/v1/admin/audit-logs?action=identity.&limit=100",
            headers=self.admin_headers,
        )
        self.assertEqual(audit.status_code, 200, audit.text)
        actions = {item["action"] for item in audit.json()["items"]}
        self.assertTrue(
            {
                "identity.user_updated",
                "identity.password_reset",
                "identity.session_revoked_by_admin",
            }.issubset(actions)
        )
        self.assertNotIn(replacement_password, audit.text)

    def test_project_member_and_environment_resource_management(self) -> None:
        project = self.client.post(
            "/api/v1/projects",
            headers=self.admin_headers,
            json={"key": "managed-project", "name": "Managed Project"},
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        updated_project = self.client.patch(
            f"/api/v1/projects/{project_id}",
            headers=self.admin_headers,
            json={"name": "Managed Project Updated", "status": "ARCHIVED"},
        )
        self.assertEqual(updated_project.status_code, 200, updated_project.text)
        self.assertEqual(updated_project.json()["name"], "Managed Project Updated")
        self.assertEqual(updated_project.json()["status"], "ARCHIVED")
        member = self._create_user("project-admin", "Project Admin")

        only_admin = self.client.delete(
            f"/api/v1/projects/{project_id}/members/{self.admin['id']}",
            headers=self.admin_headers,
        )
        self.assertEqual(only_admin.status_code, 409, only_admin.text)
        self._add_member(project_id, str(member["id"]), "PROJECT_ADMIN")
        removed_creator = self.client.delete(
            f"/api/v1/projects/{project_id}/members/{self.admin['id']}",
            headers=self.admin_headers,
        )
        self.assertEqual(removed_creator.status_code, 204, removed_creator.text)
        last_admin = self.client.delete(
            f"/api/v1/projects/{project_id}/members/{member['id']}",
            headers=self.admin_headers,
        )
        self.assertEqual(last_admin.status_code, 409, last_admin.text)

        project_admin_headers, _ = self._login("project-admin", USER_PASSWORD)
        candidates = self.client.get(
            f"/api/v1/projects/{project_id}/member-candidates?query=project",
            headers=project_admin_headers,
        )
        self.assertEqual(candidates.status_code, 200, candidates.text)
        self.assertEqual(candidates.json()[0]["id"], member["id"])

        target = self.client.post(
            f"/api/v1/projects/{project_id}/targets",
            headers=project_admin_headers,
            json={"key": "web", "name": "Web", "target_type": "WEB"},
        )
        self.assertEqual(target.status_code, 201, target.text)
        target_id = target.json()["id"]
        environment = self.client.post(
            f"/api/v1/projects/{project_id}/targets/{target_id}/environments",
            headers=project_admin_headers,
            json={
                "key": "staging",
                "name": "Staging",
                "web_config": {"base_url": "https://example.invalid/login"},
                "secret_bindings": [
                    {"name": "TEST_PASSWORD", "ref": "secret://managed/staging/password"}
                ],
            },
        )
        self.assertEqual(environment.status_code, 201, environment.text)

        updated_target = self.client.patch(
            f"/api/v1/projects/{project_id}/targets/{target_id}",
            headers=project_admin_headers,
            json={"name": "Web Console", "browser": "firefox", "status": "ARCHIVED"},
        )
        self.assertEqual(updated_target.status_code, 200, updated_target.text)
        self.assertEqual(updated_target.json()["browser"], "firefox")
        self.assertEqual(updated_target.json()["status"], "ARCHIVED")

        environment_id = environment.json()["id"]
        updated_environment = self.client.patch(
            f"/api/v1/projects/{project_id}/targets/{target_id}/environments/{environment_id}",
            headers=project_admin_headers,
            json={
                "name": "Staging Archived",
                "status": "ARCHIVED",
                "secret_bindings": [
                    {"name": "TEST_PASSWORD", "ref": "secret://managed/staging/password-v2"}
                ],
            },
        )
        self.assertEqual(updated_environment.status_code, 200, updated_environment.text)
        self.assertEqual(updated_environment.json()["name"], "Staging Archived")
        self.assertEqual(updated_environment.json()["status"], "ARCHIVED")
        self.assertEqual(
            updated_environment.json()["secret_bindings"][0]["ref"],
            "secret://managed/staging/password-v2",
        )

        project_audit = self.client.get(
            f"/api/v1/admin/audit-logs?project_id={project_id}&action=updated",
            headers=self.admin_headers,
        )
        self.assertEqual(project_audit.status_code, 200, project_audit.text)
        self.assertEqual(
            {item["action"] for item in project_audit.json()["items"]},
            {"project.updated", "target.updated", "environment.updated"},
        )

    def test_draft_validation_review_full_regression_and_release(self) -> None:
        resources = self._bootstrap_project()
        tester = self._create_user("tester", "Test Engineer")
        reviewer = self._create_user("reviewer", "Case Reviewer")
        self._add_member(resources["project_id"], str(tester["id"]), "TESTER")
        self._add_member(resources["project_id"], str(reviewer["id"]), "REVIEWER")
        tester_headers, _ = self._login("tester", USER_PASSWORD)
        reviewer_headers, _ = self._login("reviewer", USER_PASSWORD)

        base = self.client.get(
            f"/api/v1/projects/{resources['project_id']}/baselines/{resources['baseline_id']}",
            headers=tester_headers,
        )
        self.assertEqual(base.status_code, 200, base.text)
        changed_case = next(
            case for case in base.json()["cases"] if case["case_code"] == "TC-LOGIN-007"
        )
        changed_case = {**changed_case, "title": "登录成功后离开登录页（治理验证）"}
        draft = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/change-requests",
            headers=tester_headers,
            json={
                "base_baseline_id": resources["baseline_id"],
                "candidate_version": "case-v1.0.2",
                "title": "强化登录成功用例描述",
                "reason": "使标准用例标题准确表达断言目标",
                "changes": [
                    {
                        "change_type": "MODIFY",
                        "case_id": changed_case["case_id"],
                        "case": changed_case,
                    }
                ],
            },
        )
        self.assertEqual(draft.status_code, 201, draft.text)
        draft_document = draft.json()
        request_id = draft_document["id"]
        self.assertEqual(draft_document["status"], "DRAFT")
        self.assertEqual(draft_document["validation_status"], "PENDING_EXECUTION")
        self.assertEqual(draft_document["changes"][0]["changed_fields"], ["title"])

        premature = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/change-requests/{request_id}/submit",
            headers=tester_headers,
        )
        self.assertEqual(premature.status_code, 409, premature.text)
        run_request = {
            "target_id": resources["target_id"],
            "environment_id": resources["environment_id"],
            "automation_package_id": resources["package_id"],
        }
        validation_run = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/change-requests/"
            f"{request_id}/validation-runs",
            headers={**tester_headers, "Idempotency-Key": "draft-validation-0001"},
            json=run_request,
        )
        self.assertEqual(validation_run.status_code, 201, validation_run.text)
        self.assertEqual(validation_run.json()["case_count"], 1)
        self._complete_run(validation_run.json()["id"], tester_headers)

        validated = self.client.get(
            f"/api/v1/projects/{resources['project_id']}/change-requests/{request_id}",
            headers=tester_headers,
        )
        self.assertEqual(validated.json()["validation_status"], "PASSED")
        submitted = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/change-requests/{request_id}/submit",
            headers=tester_headers,
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["status"], "IN_REVIEW")

        self._add_member(resources["project_id"], str(tester["id"]), "PROJECT_ADMIN")
        self_review = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/change-requests/{request_id}/decision",
            headers=tester_headers,
            json={"decision": "APPROVE"},
        )
        self.assertEqual(self_review.status_code, 409, self_review.text)
        approved = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/change-requests/{request_id}/decision",
            headers=reviewer_headers,
            json={"decision": "APPROVE", "comment": "字段 Diff 与验证结果符合预期"},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "CANDIDATE")
        self.assertEqual(len(approved.json()["approvals"]), 1)

        released_before = self.client.get(
            f"/api/v1/projects/{resources['project_id']}/baselines",
            headers=tester_headers,
        )
        self.assertEqual(
            [baseline["version"] for baseline in released_before.json()], ["case-v1.0.1"]
        )
        regression = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/change-requests/"
            f"{request_id}/regression-runs",
            headers={**tester_headers, "Idempotency-Key": "full-regression-0001"},
            json=run_request,
        )
        self.assertEqual(regression.status_code, 201, regression.text)
        self.assertEqual(regression.json()["case_count"], 89)
        self._complete_run(regression.json()["id"], tester_headers)

        published = self.client.post(
            f"/api/v1/projects/{resources['project_id']}/change-requests/{request_id}/publish",
            headers=self.admin_headers,
            json={"regression_run_id": regression.json()["id"], "confirmation": "PUBLISH"},
        )
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["status"], "PUBLISHED")
        self.assertEqual(
            published.json()["published_baseline_id"],
            published.json()["candidate_baseline_id"],
        )
        released_after = self.client.get(
            f"/api/v1/projects/{resources['project_id']}/baselines",
            headers=tester_headers,
        )
        self.assertEqual(
            [baseline["version"] for baseline in released_after.json()],
            ["case-v1.0.1", "case-v1.0.2"],
        )
        immutable = self.client.put(
            f"/api/v1/projects/{resources['project_id']}/change-requests/{request_id}",
            headers=tester_headers,
            json={
                "candidate_version": "case-v1.0.3",
                "title": "不可修改",
                "reason": "发布后必须创建新的变更申请",
                "changes": [
                    {
                        "change_type": "MODIFY",
                        "case_id": changed_case["case_id"],
                        "case": changed_case,
                    }
                ],
            },
        )
        self.assertEqual(immutable.status_code, 409, immutable.text)


if __name__ == "__main__":
    unittest.main()
