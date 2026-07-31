"""`session-recall metadocs …` — setup, the daily run, and the switch.

    metadocs init <repo> [--projects git|all|NAME…] [--daily-at HH:MM] [--push]
    metadocs run            one distill pass now (also what the cron invokes)
    metadocs enable         start the daily launchd job
    metadocs disable        stop it
    metadocs status         config, schedule, watermark count, last log lines
"""

import argparse
import re
from pathlib import Path

from .. import config as app_config
from . import config as md_config
from . import schedule
from .collect import open_index
from .config import MetaConfig, PROJECT_ALL, PROJECT_GIT
from .distill import make_distiller
from .run import run_once

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def add_parser(sub) -> None:
    mp = sub.add_parser("metadocs",
                        help="distill sessions into living project docs (git repo)")
    msub = mp.add_subparsers(dest="metadocs_cmd", required=True)
    ip = msub.add_parser("init", help="choose the repo and what to track")
    ip.add_argument("repo", help="git repo the documents live in (created if missing)")
    ip.add_argument("--projects", nargs="+", default=[PROJECT_GIT],
                    help='"git" (default: every project that is a git checkout), '
                         '"all", or explicit project names')
    ip.add_argument("--daily-at", default="21:00", metavar="HH:MM")
    ip.add_argument("--push", action="store_true",
                    help="push after each commit (default: commit stays local)")
    msub.add_parser("run", help="distill everything new right now")
    msub.add_parser("enable", help="turn the daily job on (launchd)")
    msub.add_parser("disable", help="turn the daily job off")
    msub.add_parser("status", help="show config, schedule and progress")


def run(args: argparse.Namespace) -> int:
    cmd = args.metadocs_cmd

    if cmd == "init":
        if not _TIME_RE.match(args.daily_at):
            print(f"--daily-at must be HH:MM, got {args.daily_at!r}")
            return 1
        cfg = MetaConfig(repo=str(Path(args.repo).expanduser()),
                         projects=list(args.projects),
                         daily_at=args.daily_at, push=bool(args.push))
        md_config.save(cfg)
        sel = ("every indexed project" if PROJECT_ALL in cfg.projects else
               "every project with a git checkout" if PROJECT_GIT in cfg.projects
               else ", ".join(cfg.projects))
        print(f"meta docs configured\n  repo: {cfg.repo}\n  tracking: {sel}\n"
              f"  daily at: {cfg.daily_at}\n"
              f"  push: {'on' if cfg.push else 'off (commits stay local)'}\n"
              "next: session-recall metadocs enable   (or `run` for one pass now)")
        return 0

    cfg = md_config.load()
    if cfg is None:
        print("meta docs not configured — run: session-recall metadocs init <repo>")
        return 1

    if cmd == "enable":
        path = schedule.enable(cfg.daily_at)
        print(f"daily job on, {cfg.daily_at} every day (agent: {path})\n"
              "a missed run (mac asleep) fires once on wake")
        return 0

    if cmd == "disable":
        print("daily job off" if schedule.disable() else "daily job was not on")
        return 0

    if cmd == "status":
        marks = md_config.Watermarks(md_config.state_path())
        print(f"repo: {cfg.repo}\nprojects: {', '.join(cfg.projects)}\n"
              f"daily at: {cfg.daily_at}  "
              f"[{'enabled' if schedule.is_enabled() else 'disabled'}]\n"
              f"push: {'on' if cfg.push else 'off'}\n"
              f"sessions distilled so far: {len(marks.marks)}")
        log = app_config.DATA_DIR / "metadocs.log"
        if log.exists():
            tail = log.read_text()[-800:]
            print(f"--- log tail ---\n{tail.strip()}")
        return 0

    if cmd == "run":
        distiller = make_distiller(cfg.engine)
        if distiller is None:
            print(f"unknown engine {cfg.engine!r} in metadocs.json")
            return 1
        if not app_config.DB_PATH.exists():
            print(f"no index at {app_config.DB_PATH} — run: session-recall index")
            return 1
        db = open_index(app_config.DB_PATH)
        try:
            report = run_once(cfg, db, distiller)
        finally:
            db.close()
        print(report.summary())
        return 1 if report.blocked else 0

    return 1
