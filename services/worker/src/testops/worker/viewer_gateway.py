"""Authenticated WebSocket-to-VNC gateway for read-only Run live viewing."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from testops.contracts import (
    LiveViewRegistration,
    LiveViewTicketRecord,
    live_view_session_key,
    live_view_ticket_key,
)


class LiveViewTicketConsumer:
    def __init__(self, client: Any):
        self._client = client

    async def consume(self, ticket: str) -> LiveViewRegistration | None:
        try:
            ticket_key = live_view_ticket_key(ticket)
        except ValueError:
            return None
        ticket_document = await self._client.getdel(ticket_key)
        if not ticket_document:
            return None
        try:
            record = LiveViewTicketRecord.model_validate_json(ticket_document)
        except ValueError:
            return None
        session_document = await self._client.get(live_view_session_key(record.run_id))
        if not session_document:
            return None
        try:
            registration = LiveViewRegistration.model_validate_json(session_document)
        except ValueError:
            return None
        if registration.session_id != record.session_id:
            return None
        return registration


async def _proxy_vnc(websocket: WebSocket, registration: LiveViewRegistration) -> None:
    reader, writer = await asyncio.open_connection(registration.host, registration.port)

    async def browser_to_vnc() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None:
                continue
            writer.write(data)
            await writer.drain()

    async def vnc_to_browser() -> None:
        while data := await reader.read(64 * 1024):
            await websocket.send_bytes(data)

    tasks = {
        asyncio.create_task(browser_to_vnc()),
        asyncio.create_task(vnc_to_browser()),
    }
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


def create_viewer_gateway(
    *,
    redis_client: Any | None = None,
    novnc_root: str | Path | None = None,
) -> FastAPI:
    owned_client = redis_client is None
    client = redis_client or Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )
    consumer = LiveViewTicketConsumer(client)
    static_root = Path(novnc_root or os.getenv("NOVNC_STATIC_ROOT", "/usr/share/novnc"))
    viewer_page = Path(__file__).with_name("viewer.html")
    viewer_script = Path(__file__).with_name("viewer.js")

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        yield
        if owned_client:
            await client.aclose()

    application = FastAPI(
        title="TestOps Live View Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.get("/healthz")
    async def healthz() -> JSONResponse:
        try:
            ready = bool(await client.ping())
        except Exception:
            ready = False
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready"},
        )

    @application.get("/session.html", include_in_schema=False)
    async def session_page() -> FileResponse:
        return FileResponse(
            viewer_page,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
                    "connect-src 'self' ws: wss:; img-src 'self' data:; font-src 'self'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "SAMEORIGIN",
            },
        )

    @application.get("/viewer.js", include_in_schema=False)
    async def session_script() -> FileResponse:
        return FileResponse(
            viewer_script,
            media_type="text/javascript",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.websocket("/ws")
    async def live_view_socket(websocket: WebSocket) -> None:
        ticket = websocket.query_params.get("ticket", "")
        registration = await consumer.consume(ticket)
        offered_protocols = {
            value.strip()
            for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        }
        await websocket.accept(subprotocol="binary" if "binary" in offered_protocols else None)
        if registration is None:
            await websocket.close(code=4403, reason="live-view ticket is invalid or expired")
            return
        try:
            await _proxy_vnc(websocket, registration)
        except (OSError, WebSocketDisconnect):
            pass
        finally:
            with suppress(Exception):
                await websocket.close()

    application.mount(
        "/novnc",
        StaticFiles(directory=static_root, check_dir=False),
        name="novnc",
    )
    application.state.redis = client
    application.state.ticket_consumer = consumer
    return application


app = create_viewer_gateway()
