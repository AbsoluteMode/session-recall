"""The cron: a daily `session-recall metadocs run`, scheduled the native way.

macOS gets a launchd agent, Linux a systemd user timer — chosen over plain
cron on both because of the same laptop reality: a machine that was asleep at
the scheduled minute must run the job once on wake (StartCalendarInterval
semantics on launchd, `Persistent=true` on systemd). Anything else — say,
Windows — gets an honest error instead of a unit file nothing will read.
`enable` writes the schedule and arms it; `disable` disarms and removes it.
Nothing else in the codebase starts the job: turning it on is a human act,
like everything scheduled.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .. import config as app_config

LABEL = "tech.absolutemode.session-recall.metadocs"
UNIT = "session-recall-metadocs"          # systemd user unit basename


def _cli_binary() -> str:
    """The console script that lives next to the running interpreter — the
    same environment the user installed; PATH at scheduler time is not ours."""
    candidate = Path(sys.executable).with_name("session-recall")
    return str(candidate) if candidate.exists() else "session-recall"


# -- launchd (macOS) ----------------------------------------------------------

def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


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


def _launchd_enable(daily_at: str, log_path: Path, runner) -> Path:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(build_plist(daily_at, log_path), f)
    # bootout first so a re-enable with a new time actually takes effect
    runner(["launchctl", "unload", str(path)])
    done = runner(["launchctl", "load", str(path)])
    if done.returncode != 0:
        raise RuntimeError(f"launchctl load failed: {done.stderr.strip()}")
    return path


def _launchd_disable(runner) -> bool:
    path = plist_path()
    if not path.exists():
        return False
    runner(["launchctl", "unload", str(path)])
    path.unlink()
    return True


# -- systemd user timer (Linux) -----------------------------------------------

def systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def build_units(daily_at: str, log_path: Path) -> dict[str, str]:
    """The two unit files, as text — pure so tests read them without systemd.
    `Persistent=true` is the launchd wake-up semantics: a missed run fires
    once when the machine is back."""
    hour, minute = (int(x) for x in daily_at.split(":"))
    path_env = os.environ.get("PATH", "/usr/bin:/bin")
    service = f"""\
[Unit]
Description=session-recall meta docs — daily distill

[Service]
Type=oneshot
ExecStart={_cli_binary()} metadocs run
Environment=PATH={path_env}
StandardOutput=append:{log_path}
StandardError=append:{log_path}
"""
    timer = f"""\
[Unit]
Description=session-recall meta docs — daily schedule

[Timer]
OnCalendar=*-*-* {hour:02d}:{minute:02d}:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    return {f"{UNIT}.service": service, f"{UNIT}.timer": timer}


def _systemd_enable(daily_at: str, log_path: Path, runner) -> Path:
    d = systemd_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, text in build_units(daily_at, log_path).items():
        (d / name).write_text(text, encoding="utf-8")
    runner(["systemctl", "--user", "daemon-reload"])
    done = runner(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"])
    if done.returncode != 0:
        raise RuntimeError(f"systemctl enable failed: {done.stderr.strip()}")
    return d / f"{UNIT}.timer"


def _systemd_disable(runner) -> bool:
    timer = systemd_dir() / f"{UNIT}.timer"
    if not timer.exists():
        return False
    runner(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"])
    for name in (f"{UNIT}.timer", f"{UNIT}.service"):
        (systemd_dir() / name).unlink(missing_ok=True)
    runner(["systemctl", "--user", "daemon-reload"])
    return True


# -- the platform switch ------------------------------------------------------

def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True)


def enable(daily_at: str, platform: str | None = None, runner=_run) -> Path:
    platform = platform or sys.platform
    log_path = app_config.DATA_DIR / "metadocs.log"
    if platform == "darwin":
        return _launchd_enable(daily_at, log_path, runner)
    if platform.startswith("linux"):
        return _systemd_enable(daily_at, log_path, runner)
    raise RuntimeError(
        f"no scheduler backend for {platform!r} — run `session-recall metadocs "
        "run` from your own scheduler instead")


def disable(platform: str | None = None, runner=_run) -> bool:
    platform = platform or sys.platform
    if platform == "darwin":
        return _launchd_disable(runner)
    if platform.startswith("linux"):
        return _systemd_disable(runner)
    return False


def is_enabled(platform: str | None = None) -> bool:
    platform = platform or sys.platform
    if platform == "darwin":
        return plist_path().exists()
    if platform.startswith("linux"):
        return (systemd_dir() / f"{UNIT}.timer").exists()
    return False
