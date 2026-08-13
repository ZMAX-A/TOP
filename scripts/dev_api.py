"""Run the FastAPI control plane from the monorepo source directories."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in ("packages/contracts/src", "apps/api/src"):
    sys.path.insert(0, str(ROOT / relative))


def main() -> int:
    import uvicorn

    uvicorn.run(
        "testops.api.main:app",
        host=os.getenv("TESTOPS_API_HOST", "127.0.0.1"),
        port=int(os.getenv("TESTOPS_API_PORT", "8000")),
        reload=os.getenv("TESTOPS_API_RELOAD", "1") == "1",
        reload_dirs=[str(ROOT / "apps/api/src"), str(ROOT / "packages/contracts/src")],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
