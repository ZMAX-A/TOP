"""Run one headed executor with an isolated X display and read-only VNC server."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9]{8}$")
DISPLAY_PATTERN = re.compile(r"^:(?P<number>[1-9][0-9]{0,3})$")


def _wait_for_x_socket(process: subprocess.Popen[bytes], display_number: str) -> None:
    socket_path = Path(f"/tmp/.X11-unix/X{display_number}")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        if process.poll() is not None:
            raise RuntimeError("Xvfb exited before its display became ready")
        time.sleep(0.05)
    raise RuntimeError("Xvfb display did not become ready")


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def run_live_view_executor(
    command: list[str],
    *,
    display: str,
    width: int,
    height: int,
    depth: int,
    rfb_port: int,
    environ: dict[str, str] | None = None,
) -> int:
    if not command:
        raise ValueError("live-view executor command is required")
    match = DISPLAY_PATTERN.fullmatch(display)
    if match is None:
        raise ValueError("live-view display must use :<positive-number>")
    source = dict(os.environ if environ is None else environ)
    password = source.get("TESTOPS_LIVE_VIEW_PASSWORD", "")
    if PASSWORD_PATTERN.fullmatch(password) is None:
        raise ValueError("TESTOPS_LIVE_VIEW_PASSWORD must be exactly 8 alphanumeric characters")

    xvfb: subprocess.Popen[bytes] | None = None
    vnc: subprocess.Popen[bytes] | None = None
    child: subprocess.Popen[bytes] | None = None
    previous_handlers: dict[int, object] = {}
    try:
        xvfb = subprocess.Popen(
            [
                "Xvfb",
                display,
                "-screen",
                "0",
                f"{width}x{height}x{depth}",
                "-nolisten",
                "tcp",
                "-ac",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_x_socket(xvfb, match.group("number"))

        password_file = Path("/tmp/testops-live-view.pass")
        try:
            subprocess.run(
                ["x11vnc", "-storepasswd", password, str(password_file)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            password_file.chmod(0o600)
            vnc = subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    display,
                    "-rfbport",
                    str(rfb_port),
                    "-rfbauth",
                    str(password_file),
                    "-listen",
                    "0.0.0.0",
                    "-forever",
                    "-shared",
                    "-viewonly",
                    "-noxdamage",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"live-view VNC startup failed: {exc}", file=sys.stderr, flush=True)

        child_environment = source
        child_environment["DISPLAY"] = display
        child_environment.pop("TESTOPS_LIVE_VIEW_PASSWORD", None)
        child = subprocess.Popen(command, env=child_environment)

        def forward_signal(signum: int, _frame: object) -> None:
            if child is not None and child.poll() is None:
                child.send_signal(signum)

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, forward_signal)
        return child.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        _terminate(child)
        _terminate(vnc)
        _terminate(xvfb)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display", default=":99")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--depth", type=int, default=24)
    parser.add_argument("--rfb-port", type=int, default=5900)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = arguments.command[1:] if arguments.command[:1] == ["--"] else arguments.command
    return run_live_view_executor(
        command,
        display=arguments.display,
        width=arguments.width,
        height=arguments.height,
        depth=arguments.depth,
        rfb_port=arguments.rfb_port,
    )


if __name__ == "__main__":
    raise SystemExit(main())
