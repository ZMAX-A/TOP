"""Execute the migrated home-search case against a local deterministic target."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[1]
for relative in ("packages/contracts/src", "runners/web_playwright/src"):
    sys.path.insert(0, str(ROOT / relative))

from testops.contracts import RunSnapshot, RunStatus, canonical_json_bytes  # noqa: E402
from testops.runners.web import load_run_snapshot  # noqa: E402
from testops.runners.web.playwright_adapter import PlaywrightWebAdapter  # noqa: E402
from testops.runners.web.variables import MappingSecretProvider  # noqa: E402

LOGIN_HTML = b"""<!doctype html>
<html lang="zh-CN"><body>
<form id="login-form">
  <input id="username" />
  <input id="password" type="password" />
  <button type="submit">login</button>
</form>
<p id="error" hidden>login failed</p>
<script>
document.querySelector('#login-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const username = document.querySelector('#username').value;
  const password = document.querySelector('#password').value;
  if (username === 'smoke-user' && password === 'smoke-password') {
    window.location.assign('/dashboard');
  } else {
    document.querySelector('#error').hidden = false;
  }
});
</script>
</body></html>"""

DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><body>
<main>
  <input placeholder="请输入关键字" />
  <button id="search">搜 索</button>
  <p id="result"></p>
</main>
<script>
document.querySelector('#search').addEventListener('click', () => {
  const keyword = document.querySelector('input').value;
  document.querySelector('#result').textContent =
    keyword === '不存在的用户xyz123' ? '暂无内容' : keyword;
});
</script>
</body></html>""".encode()


class SmokeTargetHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP method name
        if self.path == "/login":
            self._respond(LOGIN_HTML)
            return
        if self.path == "/dashboard":
            self._respond(DASHBOARD_HTML)
            return
        self.send_error(404)

    def _respond(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _config_hash(job: RunSnapshot) -> str:
    payload = canonical_json_bytes(
        {
            "web_config": job.web_config.model_dump(mode="json"),
            "variables": [item.model_dump(mode="json") for item in job.variables],
            "secret_bindings": [item.model_dump(mode="json") for item in job.secret_bindings],
        }
    )
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), SmokeTargetHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/login"
    try:
        source = load_run_snapshot(ROOT / "examples/jobs/yanjia-home-search-smoke.json")
        if source.web_config is None:
            raise RuntimeError("home-search example has no web_config")
        web_config = source.web_config.model_copy(
            update={"base_url": base_url, "capture_trace": False}
        )
        job = source.model_copy(
            update={
                "run_id": uuid4(),
                "environment_id": uuid5(NAMESPACE_URL, base_url),
                "web_config": web_config,
                "created_at": datetime.now(UTC),
            }
        )
        job = job.model_copy(update={"config_hash": _config_hash(job)})
        with tempfile.TemporaryDirectory() as workspace_root:
            adapter = PlaywrightWebAdapter(
                workspace_root=workspace_root,
                secret_provider=MappingSecretProvider(
                    {
                        "secret://yanjia/staging/test-username": "smoke-user",
                        "secret://yanjia/staging/test-password": "smoke-password",
                    }
                ),
            )
            result = adapter.execute(job, lambda _event: None)
        print(
            json.dumps(
                {
                    "status": result.status.value,
                    "case_codes": [item.case_code for item in result.case_results],
                    "runner_version": result.runner_version,
                    "target": "local-loopback",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result.status == RunStatus.PASSED else 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
