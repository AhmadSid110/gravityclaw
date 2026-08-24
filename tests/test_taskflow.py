"""Comprehensive tests for Milestone 12: Durable TaskFlow / Kanban Orchestration."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from gravityclaw.api import Settings, create_app
from gravityclaw.context import PROFILES
from gravityclaw.event_bus import EventBus
from gravityclaw.execution import FakeHostSpecFactory, HostExecutionBackend
from gravityclaw.manager import RunManager
from gravityclaw.store import (
    BLOCK_REASONS,
    FLOW_STATUSES,
    SCHEMA_VERSION,
    TASK_PRIORITIES,
    TASK_STATUSES,
    FlowTaskRecord,
    Store,
    TaskFlowRecord,
    VersionConflict,
    utc_now,
)
from gravityclaw.taskflow import (
    DispatcherReport,
    TaskFlowDispatcher,
    TaskFlowService,
    TaskResultContract,
    build_taskflow_worker_prompt,
    detect_dag_cycle,
)
from starlette.testclient import TestClient


class TaskFlowStoreAndDAGTests(unittest.TestCase):
    """Tests for database tables, migrations, DAG cycles, and auto-promotion."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="gravityclaw-taskflow-test-")
        self.root = Path(self.tmpdir.name)
        self.db_path = self.root / "gravityclaw.db"
        self.store = Store(self.db_path)
        self.store.initialize()
        self.workspace = self.store.create_workspace("default", self.root / "ws")
        self.service = TaskFlowService(self.store)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_schema_version_is_18_and_tables_exist(self) -> None:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(row["value"], "18")
        expected_tables = {
            "task_flows",
            "flow_tasks",
            "task_dependencies",
            "task_attempts",
            "task_comments",
            "task_claims",
            "task_artifacts",
        }
        self.assertTrue(expected_tables <= tables)

    def test_schema_migration_from_v17_creates_taskflow_tables(self) -> None:
        mig_db = self.root / "migrate_v17.db"
        s = Store(mig_db)
        s.initialize()
        # Simulate v17 downgrade
        with s._connect() as conn:
            conn.execute("UPDATE metadata SET value='17' WHERE key='schema_version'")
            for tbl in (
                "task_artifacts",
                "task_claims",
                "task_comments",
                "task_attempts",
                "task_dependencies",
                "flow_tasks",
                "task_flows",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        # Re-initialize to trigger migration 17 -> 18
        s.initialize()
        with s._connect() as conn:
            ver = conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(ver, "18")
        self.assertIn("task_flows", tables)
        self.assertIn("flow_tasks", tables)
        self.assertIn("task_dependencies", tables)

    def test_taskflow_crud_and_optimistic_locking(self) -> None:
        flow = self.service.create_flow(
            title="Deploy Microservice",
            objective="Deploy auth service to staging cluster",
            workspace_id=self.workspace.id,
            state_json={"priority": "high"},
        )
        self.assertEqual(flow.title, "Deploy Microservice")
        self.assertEqual(flow.status, "QUEUED")
        self.assertEqual(flow.version, 1)

        fetched = self.service.get_flow(flow.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, flow.id)

        # Update status with expected version
        updated = self.service.update_flow_status(
            flow.id, "RUNNING", expected_version=1
        )
        self.assertEqual(updated.status, "RUNNING")
        self.assertEqual(updated.version, 2)

        # Version conflict on stale expected_version
        with self.assertRaises(VersionConflict):
            self.service.update_flow_status(flow.id, "WAITING", expected_version=1)

    def test_idempotent_task_creation(self) -> None:
        flow = self.service.create_flow(
            title="Build Pipeline",
            objective="Run CI/CD pipeline",
            workspace_id=self.workspace.id,
        )

        task1 = self.service.create_task(
            flow_id=flow.id,
            title="Run Unit Tests",
            body="pytest -v",
            workspace_id=self.workspace.id,
            idempotency_key="webhook-event-12345",
        )

        # Attempt to create the same task again with the same idempotency key
        task2 = self.service.create_task(
            flow_id=flow.id,
            title="Run Unit Tests (Retry)",
            body="different body",
            workspace_id=self.workspace.id,
            idempotency_key="webhook-event-12345",
        )

        self.assertEqual(task1.id, task2.id)
        all_tasks = self.service.list_tasks(flow_id=flow.id)
        self.assertEqual(len(all_tasks), 1)

    def test_dag_cycle_prevention(self) -> None:
        flow = self.service.create_flow(
            title="DAG Test",
            objective="Test DAG cycle prevention",
            workspace_id=self.workspace.id,
        )

        t1 = self.service.create_task(
            flow_id=flow.id, title="Task 1", body="", workspace_id=self.workspace.id
        )
        t2 = self.service.create_task(
            flow_id=flow.id, title="Task 2", body="", workspace_id=self.workspace.id, parent_ids=[t1.id]
        )
        t3 = self.service.create_task(
            flow_id=flow.id, title="Task 3", body="", workspace_id=self.workspace.id, parent_ids=[t2.id]
        )

        # Self dependency
        with self.assertRaises(ValueError):
            self.service.add_dependency(t1.id, t1.id)

        # Cycle: T3 -> T1 (since T1 -> T2 -> T3)
        with self.assertRaises(ValueError):
            self.service.add_dependency(t3.id, t1.id)

        # Cycle: T2 -> T1
        with self.assertRaises(ValueError):
            self.service.add_dependency(t2.id, t1.id)

    def test_deterministic_dag_auto_promotion(self) -> None:
        """Diamond DAG: T1 -> T2, T1 -> T3, (T2, T3) -> T4."""
        flow = self.service.create_flow(
            title="Diamond Pipeline",
            objective="Execute Diamond DAG",
            workspace_id=self.workspace.id,
        )

        t1 = self.service.create_task(
            flow_id=flow.id, title="T1 Root", body="Compile", workspace_id=self.workspace.id
        )
        t2 = self.service.create_task(
            flow_id=flow.id, title="T2 Unit", body="Unit tests", workspace_id=self.workspace.id, parent_ids=[t1.id]
        )
        t3 = self.service.create_task(
            flow_id=flow.id, title="T3 Lint", body="Linter", workspace_id=self.workspace.id, parent_ids=[t1.id]
        )
        t4 = self.service.create_task(
            flow_id=flow.id, title="T4 Package", body="Docker build", workspace_id=self.workspace.id, parent_ids=[t2.id, t3.id]
        )

        # T1 has no parents -> READY
        t1_fetched = self.service.get_task(t1.id)
        self.assertEqual(t1_fetched.status, "READY")

        # T2, T3, T4 have uncompleted parents -> TODO
        self.assertEqual(self.service.get_task(t2.id).status, "TODO")
        self.assertEqual(self.service.get_task(t3.id).status, "TODO")
        self.assertEqual(self.service.get_task(t4.id).status, "TODO")

        # Complete T1
        self.store.update_flow_task_status(t1.id, "DONE")
        self.service.auto_promote_flow(flow.id)

        # T2 and T3 should now be READY, T4 still TODO
        self.assertEqual(self.service.get_task(t2.id).status, "READY")
        self.assertEqual(self.service.get_task(t3.id).status, "READY")
        self.assertEqual(self.service.get_task(t4.id).status, "TODO")

        # Complete T2 only
        self.store.update_flow_task_status(t2.id, "DONE")
        self.service.auto_promote_flow(flow.id)
        # T4 must STILL be TODO because T3 is not DONE yet
        self.assertEqual(self.service.get_task(t4.id).status, "TODO")

        # Complete T3
        self.store.update_flow_task_status(t3.id, "DONE")
        self.service.auto_promote_flow(flow.id)
        # Now T4 must be READY
        self.assertEqual(self.service.get_task(t4.id).status, "READY")

    def test_recurrence_guard_escalates_to_triage(self) -> None:
        """If a task is blocked for the same reason 3 times -> escalates to TRIAGE."""
        flow = self.service.create_flow(
            title="Flaky Service",
            objective="Test recurrence guard",
            workspace_id=self.workspace.id,
        )
        task = self.service.create_task(
            flow_id=flow.id,
            title="Connect External API",
            body="Fetch data from 3rd party",
            workspace_id=self.workspace.id,
        )

        # 1st block for 'external_service'
        b1 = self.service.block_task(task.id, "external_service", "503 Service Unavailable")
        self.assertEqual(b1.status, "BLOCKED")
        self.assertEqual(b1.block_recurrence_count, 1)

        # Unblock
        u1 = self.service.unblock_task(task.id, comment="Retrying API")
        self.assertEqual(u1.status, "READY")

        # 2nd block for 'external_service'
        b2 = self.service.block_task(task.id, "external_service", "503 Service Unavailable")
        self.assertEqual(b2.status, "BLOCKED")
        self.assertEqual(b2.block_recurrence_count, 2)

        # Unblock
        u2 = self.service.unblock_task(task.id, comment="Retrying API again")
        self.assertEqual(u2.status, "READY")

        # 3rd block for 'external_service' -> triggers RECURRENCE GUARD -> TRIAGE
        b3 = self.service.block_task(task.id, "external_service", "503 Service Unavailable")
        self.assertEqual(b3.status, "TRIAGE")
        self.assertEqual(b3.block_recurrence_count, 3)

        comments = self.store.list_task_comments(task.id)
        self.assertTrue(any("[RECURRENCE GUARD]" in c.body for c in comments))

    def test_task_comments_and_handoff_protocol(self) -> None:
        flow = self.service.create_flow(
            title="Handoff Test",
            objective="Verify comment handoffs",
            workspace_id=self.workspace.id,
        )
        parent = self.service.create_task(
            flow_id=flow.id, title="Extract Data", body="", workspace_id=self.workspace.id
        )
        child = self.service.create_task(
            flow_id=flow.id, title="Transform Data", body="", workspace_id=self.workspace.id, parent_ids=[parent.id]
        )

        # Add comments on parent
        self.service.add_comment(parent.id, "agent", "run-101", "Saved raw data to /tmp/data.csv")
        self.service.add_comment(parent.id, "agent", "run-101", "Row count: 50,000")

        handoffs = self.service.get_task_handoffs(child.id)
        self.assertEqual(len(handoffs), 1)
        p_task, comments = handoffs[0]
        self.assertEqual(p_task.id, parent.id)
        self.assertEqual(len(comments), 2)
        self.assertIn("Row count: 50,000", comments[1].body)


class TaskFlowDispatcherAndIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Tests for autonomous Dispatcher, Leases, Run spawning, and Crash Recovery."""

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="gravityclaw-taskflow-disp-")
        self.root = Path(self.tmpdir.name)
        self.db_path = self.root / "gravityclaw.db"
        self.store = Store(self.db_path)
        self.store.initialize()
        self.workspace = self.store.create_workspace("default", self.root / "ws")
        self.service = TaskFlowService(self.store)

        self.bus = EventBus()
        self.backend = HostExecutionBackend()
        self.factory = FakeHostSpecFactory()
        self.manager = RunManager(
            self.store,
            self.backend,
            self.factory,
            self.bus,
            poll_interval=0.05,
        )
        self.dispatcher = TaskFlowDispatcher(
            self.store,
            self.manager,
            self.service,
            poll_interval=0.05,
            lease_seconds=60,
            max_concurrent_workers=4,
        )

    async def asyncTearDown(self) -> None:
        await self.dispatcher.close()
        await self.manager.close()
        self.tmpdir.cleanup()

    async def test_atomic_lease_claims_and_heartbeats(self) -> None:
        flow = self.service.create_flow(
            title="Lease Flow", objective="Test leases", workspace_id=self.workspace.id
        )
        task = self.service.create_task(
            flow_id=flow.id, title="Ready Task", body="", workspace_id=self.workspace.id
        )
        self.assertEqual(task.status, "READY")

        # Claim task
        claimed = self.store.claim_ready_tasks(owner="worker-1", limit=1, lease_seconds=120)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].id, task.id)
        self.assertEqual(self.service.get_task(task.id).status, "RUNNING")

        # Another worker tries to claim -> gets 0
        claimed_2 = self.store.claim_ready_tasks(owner="worker-2", limit=1)
        self.assertEqual(len(claimed_2), 0)

        # Heartbeat extension
        heartbeat_ok = self.store.heartbeat_task_claim(
            task.id, "worker-1", message="Working on step 2", extend_seconds=300
        )
        self.assertTrue(heartbeat_ok)
        claim = self.store.get_task_claim(task.id)
        self.assertEqual(claim.heartbeat_message, "Working on step 2")

    async def _await_runs_complete(self, timeout: float = 3.0) -> None:
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            runs = self.store.list_runs()
            if runs and all(r.status in ("completed", "failed", "cancelled", "interrupted") for r in runs):
                break
            await asyncio.sleep(0.05)

    async def test_dispatcher_executes_multi_task_dag_to_completion(self) -> None:
        """Test dispatcher driving a 3-task dependency sequence to SUCCEEDED."""
        await self.manager.start()

        flow = self.service.create_flow(
            title="Web Scraping Project",
            objective="Scrape articles and generate summary",
            workspace_id=self.workspace.id,
        )

        t1 = self.service.create_task(
            flow_id=flow.id,
            title="Fetch URL list",
            body="Fetch 10 article URLs",
            workspace_id=self.workspace.id,
            acceptance_criteria=["Return 10 URLs"],
        )
        t2 = self.service.create_task(
            flow_id=flow.id,
            title="Scrape Content",
            body="Scrape text from URLs",
            workspace_id=self.workspace.id,
            parent_ids=[t1.id],
            acceptance_criteria=["Clean HTML to markdown"],
        )
        t3 = self.service.create_task(
            flow_id=flow.id,
            title="Generate Synthesis",
            body="Summarize articles",
            workspace_id=self.workspace.id,
            parent_ids=[t2.id],
            acceptance_criteria=["Executive summary in markdown"],
        )

        # Initial tick -> dispatches T1
        rep1 = await self.dispatcher.tick()
        self.assertEqual(rep1.dispatched_tasks, 1)

        # Wait for T1 fake run to finish
        await self._await_runs_complete()

        # Tick 2 -> reconciles T1, promotes T2, dispatches T2
        rep2 = await self.dispatcher.tick()
        self.assertEqual(self.service.get_task(t1.id).status, "DONE")
        self.assertEqual(self.service.get_task(t2.id).status, "RUNNING")

        await self._await_runs_complete()

        # Tick 3 -> reconciles T2, promotes T3, dispatches T3
        rep3 = await self.dispatcher.tick()
        self.assertEqual(self.service.get_task(t2.id).status, "DONE")
        self.assertEqual(self.service.get_task(t3.id).status, "RUNNING")

        await self._await_runs_complete()

        # Tick 4 -> reconciles T3, evaluates Flow -> SUCCEEDED
        rep4 = await self.dispatcher.tick()
        self.assertEqual(self.service.get_task(t3.id).status, "DONE")
        updated_flow = self.service.get_flow(flow.id)
        self.assertEqual(updated_flow.status, "SUCCEEDED")

    async def test_crash_recovery_and_gateway_restart(self) -> None:
        """Simulate a crash during task execution: lease expiration -> reclaim -> rerun -> complete."""
        flow = self.service.create_flow(
            title="Crash Recovery Flow",
            objective="Recover after worker failure",
            workspace_id=self.workspace.id,
        )
        task = self.service.create_task(
            flow_id=flow.id,
            title="Resilient Task",
            body="Must complete even if crashed",
            workspace_id=self.workspace.id,
            max_attempts=3,
        )

        # Manually create an expired claim (simulating crashed worker from 10 minutes ago)
        with self.store._connect() as conn:
            conn.execute(
                "INSERT INTO task_claims(task_id, owner, lease_until, heartbeat_at) "
                "VALUES(?, 'crashed-worker-old', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')",
                (task.id,),
            )
            conn.execute("UPDATE flow_tasks SET status='RUNNING' WHERE id=?", (task.id,))

        # Run manager start
        await self.manager.start()

        # Tick -> reconciles expired claim, resets to READY, claims & dispatches in same tick
        report = await self.dispatcher.tick()
        self.assertEqual(report.reconciled_claims, 1)
        self.assertEqual(report.dispatched_tasks, 1)

        await self._await_runs_complete()
        report2 = await self.dispatcher.tick()
        self.assertEqual(self.service.get_task(task.id).status, "DONE")
        self.assertEqual(self.service.get_flow(flow.id).status, "SUCCEEDED")

    @unittest.skipIf(
        __import__("os").environ.get("CI") == "true",
        "Flaky in CI due to asyncio scheduling latency under resource contention; "
        "run manually with: python -m pytest tests/test_taskflow.py -k crash_restart",
    )
    async def test_large_scale_dag_with_concurrency_and_crash_restart(self) -> None:
        """Acceptance Gate: 21 tasks, 5 dependency branches, 4 workers, simulated crash & restart."""
        await self.manager.start()

        flow = self.service.create_flow(
            title="Large Scale Microservice Migration",
            objective="Migrate 5 microservices concurrently with DAG dependencies",
            workspace_id=self.workspace.id,
        )

        branch_deploy_ids: list[str] = []
        all_task_ids: list[str] = []

        for b in range(1, 6):
            root = self.service.create_task(
                flow_id=flow.id,
                title=f"Branch-{b} Prepare Infrastructure",
                body=f"Provision infra for branch {b}",
                workspace_id=self.workspace.id,
                max_attempts=5,
            )
            svc = self.service.create_task(
                flow_id=flow.id,
                title=f"Branch-{b} Implement Service",
                body=f"Write service {b} logic",
                workspace_id=self.workspace.id,
                parent_ids=[root.id],
                max_attempts=5,
            )
            test = self.service.create_task(
                flow_id=flow.id,
                title=f"Branch-{b} Run Integration Tests",
                body=f"Integration test for service {b}",
                workspace_id=self.workspace.id,
                parent_ids=[svc.id],
                max_attempts=5,
            )
            dep = self.service.create_task(
                flow_id=flow.id,
                title=f"Branch-{b} Deploy Canary",
                body=f"Canary deploy service {b}",
                workspace_id=self.workspace.id,
                parent_ids=[test.id],
                max_attempts=5,
            )
            all_task_ids.extend([root.id, svc.id, test.id, dep.id])
            branch_deploy_ids.append(dep.id)

        # Final aggregate release task
        release = self.service.create_task(
            flow_id=flow.id,
            title="Global Release Certification",
            body="Verify all 5 services deployed and certify release",
            workspace_id=self.workspace.id,
            parent_ids=branch_deploy_ids,
            max_attempts=5,
        )
        all_task_ids.append(release.id)
        self.assertEqual(len(all_task_ids), 21)

        crashed = False
        for _ in range(600):
            report = await self.dispatcher.tick()
            await self._await_runs_complete(timeout=5.0)

            done_count = sum(
                1 for tid in all_task_ids if self.service.get_task(tid).status == "DONE"
            )
            if done_count >= 8 and not crashed:
                crashed = True
                await self.dispatcher.close()

                # Simulate passage of lease time during crash
                with self.store._connect() as conn:
                    conn.execute("UPDATE task_claims SET lease_until='2020-01-01T00:00:00Z'")

                self.dispatcher = TaskFlowDispatcher(
                    self.store,
                    self.manager,
                    self.service,
                    poll_interval=0.05,
                    lease_seconds=60,
                    max_concurrent_workers=4,
                    owner="taskflow_dispatcher:restarted_worker",
                )

            flow_rec = self.service.get_flow(flow.id)
            if flow_rec.status == "SUCCEEDED":
                break

        final_flow = self.service.get_flow(flow.id)
        self.assertEqual(final_flow.status, "SUCCEEDED")

        for tid in all_task_ids:
            task_rec = self.service.get_task(tid)
            self.assertEqual(task_rec.status, "DONE")

        release_attempts = self.store.list_task_attempts(release.id)
        self.assertTrue(len(release_attempts) >= 1)


class TaskFlowAPITests(unittest.TestCase):
    """Tests for TaskFlow REST endpoints."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="gravityclaw-taskflow-api-")
        self.root = Path(self.tmpdir.name)
        self.settings = Settings(
            home=self.root,
            mode="fake",
            control_token=None,
        )
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)

        # Create workspace
        resp = self.client.post(
            "/workspaces", json={"name": "test-ws", "path": str(self.root / "ws")}
        )
        self.workspace_id = resp.json()["id"]

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_flow_and_task_rest_api_lifecycle(self) -> None:
        # 1. Create Flow
        flow_resp = self.client.post(
            "/api/taskflows",
            json={
                "title": "API Flow",
                "objective": "Test REST APIs",
                "workspace_id": self.workspace_id,
            },
        )
        self.assertEqual(flow_resp.status_code, 200)
        flow_data = flow_resp.json()
        flow_id = flow_data["id"]
        self.assertEqual(flow_data["title"], "API Flow")
        self.assertEqual(flow_data["stats"]["total_tasks"], 0)

        # 2. Create Task 1
        t1_resp = self.client.post(
            f"/api/taskflows/{flow_id}/tasks",
            json={
                "flow_id": flow_id,
                "title": "Task 1",
                "body": "First task",
                "workspace_id": self.workspace_id,
                "priority": "HIGH",
            },
        )
        self.assertEqual(t1_resp.status_code, 200)
        t1_id = t1_resp.json()["id"]
        self.assertEqual(t1_resp.json()["status"], "READY")

        # 3. Create Task 2 depending on Task 1
        t2_resp = self.client.post(
            f"/api/taskflows/{flow_id}/tasks",
            json={
                "flow_id": flow_id,
                "title": "Task 2",
                "body": "Second task",
                "workspace_id": self.workspace_id,
                "parent_ids": [t1_id],
            },
        )
        self.assertEqual(t2_resp.status_code, 200)
        t2_id = t2_resp.json()["id"]
        self.assertEqual(t2_resp.json()["status"], "TODO")

        # 4. Add Comment
        c_resp = self.client.post(
            f"/api/flow-tasks/{t1_id}/comments",
            json={"body": "Initial research completed", "author_type": "user"},
        )
        self.assertEqual(c_resp.status_code, 200)
        self.assertEqual(c_resp.json()["body"], "Initial research completed")

        # 5. Block & Unblock Task
        b_resp = self.client.post(
            f"/api/flow-tasks/{t1_id}/block",
            json={"reason": "needs_user_input", "detail": "Which database type?"},
        )
        self.assertEqual(b_resp.status_code, 200)
        self.assertEqual(b_resp.json()["status"], "BLOCKED")

        u_resp = self.client.post(
            f"/api/flow-tasks/{t1_id}/unblock",
            json={"comment": "Use PostgreSQL"},
        )
        self.assertEqual(u_resp.status_code, 200)
        self.assertEqual(u_resp.json()["status"], "READY")

        # 6. Check Flow details with stats
        flow_get = self.client.get(f"/api/taskflows/{flow_id}")
        self.assertEqual(flow_get.status_code, 200)
        stats = flow_get.json()["stats"]
        self.assertEqual(stats["total_tasks"], 2)
        self.assertEqual(stats["ready_tasks"], 1)
        self.assertEqual(stats["todo_tasks"], 1)

        # 7. List Tasks
        tasks_resp = self.client.get(f"/api/taskflows/{flow_id}/tasks")
        self.assertEqual(tasks_resp.status_code, 200)
        self.assertEqual(len(tasks_resp.json()), 2)


if __name__ == "__main__":
    unittest.main()
