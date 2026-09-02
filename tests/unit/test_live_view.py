from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from testops.api.live_view import issue_live_view_ticket, live_view_status
from testops.contracts import (
    LiveViewRegistration,
    LiveViewTicketRecord,
    live_view_session_key,
    live_view_ticket_key,
)
from testops.worker.viewer_gateway import LiveViewTicketConsumer

RUN_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT = Path(__file__).resolve().parents[2]


class FakeRedis:
    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)


class FakeLiveViewStore:
    def __init__(self, registration: LiveViewRegistration | None):
        self.registration = registration

    async def get_registration(self, run_id: UUID) -> LiveViewRegistration | None:
        assert run_id == RUN_ID
        return self.registration

    async def issue_ticket(
        self,
        registration: LiveViewRegistration,
        *,
        ttl_seconds: int,
    ) -> tuple[str, datetime]:
        assert registration == self.registration
        assert ttl_seconds == 30
        return "A" * 43, datetime(2026, 8, 28, 8, 0, tzinfo=UTC)

    async def aclose(self) -> None:
        return None


def registration() -> LiveViewRegistration:
    return LiveViewRegistration(
        run_id=RUN_ID,
        session_id="S" * 43,
        host=f"testops-run-{RUN_ID.hex}",
        password="Ab12Cd34",
    )


def headed_run(status: str = "RUNNING") -> SimpleNamespace:
    snapshot = json.loads(
        (ROOT / "tests/fixtures/run_snapshot.valid.json").read_text("utf-8")
    )
    snapshot["run_id"] = str(RUN_ID)
    snapshot["web_config"]["headless"] = False
    return SimpleNamespace(
        id=RUN_ID,
        status=status,
        snapshot=snapshot,
    )


def test_live_view_registration_rejects_arbitrary_network_targets() -> None:
    with pytest.raises(ValidationError):
        LiveViewRegistration(
            run_id=RUN_ID,
            session_id="S" * 43,
            host="evil.example.com",
            password="Ab12Cd34",
        )


def test_ticket_consumer_is_single_use_and_binds_current_session() -> None:
    ticket = "T" * 43
    active = registration()
    record = LiveViewTicketRecord(run_id=RUN_ID, session_id=active.session_id)
    client = FakeRedis(
        {
            live_view_ticket_key(ticket): record.model_dump_json(),
            live_view_session_key(RUN_ID): active.model_dump_json(),
        }
    )
    consumer = LiveViewTicketConsumer(client)

    assert asyncio.run(consumer.consume(ticket)) == active
    assert asyncio.run(consumer.consume(ticket)) is None


def test_ticket_consumer_rejects_a_replaced_run_session() -> None:
    ticket = "T" * 43
    active = registration()
    stale = LiveViewTicketRecord(run_id=RUN_ID, session_id="X" * 43)
    client = FakeRedis(
        {
            live_view_ticket_key(ticket): stale.model_dump_json(),
            live_view_session_key(RUN_ID): active.model_dump_json(),
        }
    )
    assert asyncio.run(LiveViewTicketConsumer(client).consume(ticket)) is None


def test_status_and_ticket_require_an_active_headed_run() -> None:
    store = FakeLiveViewStore(registration())
    ready = asyncio.run(live_view_status(headed_run(), store))
    assert ready.status == "READY"
    access = asyncio.run(issue_live_view_ticket(headed_run(), store, ttl_seconds=30))
    assert access.viewer_path == "/viewer/session.html"
    assert access.password == "Ab12Cd34"
    assert access.read_only is True

    ended = asyncio.run(live_view_status(headed_run("PASSED"), store))
    assert ended.status == "ENDED"
