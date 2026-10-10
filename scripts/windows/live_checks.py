"""Print live-check lines for Invoke-GovConLiveChecks.ps1.

PowerShell 5.1 strips double quotes when a here-string is passed to
``python -c``, so this file is executed directly. It does not print keys,
database URLs, or request URLs.
"""

from __future__ import annotations

import sys

from govcon.envfile import run_report


def main() -> None:
    env_path = sys.argv[1] if len(sys.argv) > 1 else ""
    mode = sys.argv[2] if len(sys.argv) > 2 else "local"
    http_status = sys.argv[3] if len(sys.argv) > 3 else "unavailable"
    body = sys.stdin.read()
    for line in run_report(env_path, mode, http_status, body):
        print(line)


if __name__ == "__main__":
    main()
