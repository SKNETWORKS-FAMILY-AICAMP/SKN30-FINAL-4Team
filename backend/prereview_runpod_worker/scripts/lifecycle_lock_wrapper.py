#!/usr/bin/env python3
"""Hold the lifecycle flock and forward Pod shutdown signals to its child."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--lock-fd", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command or args.command[0] != "--":
        return 2
    command = args.command[1:]
    if not command:
        return 2
    try:
        descriptor = args.lock_fd
        if descriptor < 3:
            return 1
        os.set_inheritable(descriptor, False)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            return 1
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 1

    child: subprocess.Popen[bytes] | None = None
    forwarding = False
    pending: list[int] = []

    def forward(signum: int, _frame: object) -> None:
        nonlocal forwarding
        forwarding = True
        if child is None:
            pending.append(signum)
        elif child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    for candidate in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(candidate, forward)
    try:
        child = subprocess.Popen(command, close_fds=True, start_new_session=True)
        for pending_signal in pending:
            if child.poll() is None:
                os.killpg(child.pid, pending_signal)
        status = child.wait()
    finally:
        # The shell created this inherited descriptor; closing it here releases
        # the lifetime lock only after the forwarded child has exited.
        os.close(descriptor)
    if forwarding and status == 0:
        return 0
    return status if status >= 0 else 128 + abs(status)


if __name__ == "__main__":
    raise SystemExit(main())
