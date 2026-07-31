"""The cron: a launchd agent that runs `session-recall metadocs run` daily.

launchd rather than cron on purpose — a Mac that was asleep at the scheduled
minute runs the job once on wake (StartCalendarInterval semantics), which is
exactly right for a daily distill on a laptop. `enable` writes the plist and
loads it; `disable` unloads and removes it. Nothing else in the codebase
starts the job: turning it on is a human act, like everything scheduled.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .. import config as app_config

LABEL = "tech.absolutemode.session-recall.metadocs"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _cli_binary() -> str:
    """The console script that lives next to the running interpreter — the
    same environment the user installed; PATH at launchd time is not ours."""
    candidate = Path(sys.executable).with_name("session-recall")
    return str(candidate) if candidate.exists() else "session-recall"


def build_plist(daily_at: str, log_path: Path) -> dict:
    hour, minute = (int(x) for x in daily_at.split(":"))
    return {
        "Label": LABEL,
        "ProgramArguments": [_cli_binary(), "metadocs", "run"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        # launchd starts jobs with a bare PATH; the distiller shells out to
        # `claude`, so freeze the enabling user's PATH into the agent
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def enable(daily_at: str) -> Path:
    log_path = app_config.DATA_DIR / "metadocs.log"
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(build_plist(daily_at, log_path), f)
    # bootout first so a re-enable with a new time actually takes effect
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    done = subprocess.run(["launchctl", "load", str(path)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"launchctl load failed: {done.stderr.strip()}")
    return path


def disable() -> bool:
    path = plist_path()
    if not path.exists():
        return False
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    path.unlink()
    return True


def is_enabled() -> bool:
    return plist_path().exists()
