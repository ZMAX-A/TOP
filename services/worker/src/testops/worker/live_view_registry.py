"""Redis-backed publication of active per-Run live-view displays."""

from __future__ import annotations

from typing import Any

from redis import Redis

from testops.contracts import LiveViewRegistration, live_view_session_key

UNPUBLISH_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class LiveViewPublisher:
    def __init__(self, redis_url: str, *, client: Any | None = None):
        self._owns_client = client is None
        self._client = client or Redis.from_url(redis_url, decode_responses=True)

    def publish(self, registration: LiveViewRegistration, *, ttl_seconds: int) -> str:
        document = registration.model_dump_json()
        stored = self._client.set(
            live_view_session_key(registration.run_id),
            document,
            ex=ttl_seconds,
        )
        if not stored:
            raise RuntimeError("live-view registration could not be stored")
        return document

    def unpublish(self, registration: LiveViewRegistration, document: str) -> None:
        self._client.eval(
            UNPUBLISH_SCRIPT,
            1,
            live_view_session_key(registration.run_id),
            document,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
