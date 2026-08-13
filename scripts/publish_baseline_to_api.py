"""Register a checked-in immutable baseline through the control-plane API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

import httpx

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="publish-baseline-to-api")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--project-id", required=True, type=UUID)
    parser.add_argument("--actor-id", required=True, type=UUID)
    parser.add_argument("--version", default="case-v1.0.1")
    return parser


def main() -> int:
    args = _parser().parse_args()
    directory = ROOT / "baselines/yanjia-ai-web" / args.version
    baseline = json.loads((directory / "case-baseline.json").read_text("utf-8"))
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    url = f"{args.api_url.rstrip('/')}/api/v1/projects/{args.project_id}/baselines"
    response = httpx.post(
        url,
        headers={"X-Actor-Id": str(args.actor_id)},
        json={"baseline": baseline, "digest": manifest["baseline"]["digest"]},
        timeout=30,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
