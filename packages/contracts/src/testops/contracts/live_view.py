"""Ephemeral contracts for authenticated, read-only Run live viewing."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

LIVE_VIEW_SESSION_KEY_PREFIX = "testops:live-view:session:"
LIVE_VIEW_TICKET_KEY_PREFIX = "testops:live-view:ticket:"
LIVE_VIEW_RFB_PORT = 5900
LIVE_VIEW_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

LiveViewSecret = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_-]{32,128}$"),
]
LiveViewPassword = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9]{8}$"),
]
LiveViewContainerHost = Annotated[
    str,
    StringConstraints(pattern=r"^testops-run-[0-9a-f]{32}$"),
]


class LiveViewRegistration(BaseModel):
    """Worker-published location of one active, isolated VNC display."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    session_id: LiveViewSecret
    host: LiveViewContainerHost
    port: Literal[LIVE_VIEW_RFB_PORT] = LIVE_VIEW_RFB_PORT
    password: LiveViewPassword = Field(repr=False)


class LiveViewTicketRecord(BaseModel):
    """Single-use browser ticket stored without embedding the VNC password."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    session_id: LiveViewSecret


def live_view_session_key(run_id: UUID) -> str:
    return f"{LIVE_VIEW_SESSION_KEY_PREFIX}{run_id}"


def live_view_ticket_key(ticket: str) -> str:
    if LIVE_VIEW_TOKEN_PATTERN.fullmatch(ticket) is None:
        raise ValueError("live-view ticket has an invalid format")
    return f"{LIVE_VIEW_TICKET_KEY_PREFIX}{sha256(ticket.encode('ascii')).hexdigest()}"
