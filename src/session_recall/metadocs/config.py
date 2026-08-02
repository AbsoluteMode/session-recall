"""meta docs settings + incremental state.

Settings are one JSON file a human edits through the CLI. State is a separate
file because it is machine-written on every run and must never clobber the
human's choices on a crash: per-session watermarks recording how far each
transcript has been distilled, so a run only ever reads the new tail.
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .. import config as app_config

CONFIG_FILE = "metadocs.json"
STATE_FILE = "metadocs-state.json"

PROJECT_ALL = "all"
PROJECT_GIT = "git"          # every project whose cwd is a git checkout


@dataclass
class MetaConfig:
    repo: str                          # git repo the documents live in
    projects: list = field(default_factory=lambda: [PROJECT_GIT])
    daily_at: str = "21:00"            # local wall-clock time of the daily run
    engine: str = "claude-cli"         # distiller agent; see distill.make_distiller
    push: bool = False                 # commit is always local; pushing is opt-in
    model: str = ""                    # agent model; "" = the CLI's own default.
                                       # Config-only on purpose: the distiller
                                       # must never pick a model silently.
    since: float = 0.0                 # epoch; dialogue older than this is not
                                       # distilled («с сегодняшнего дня»)


def config_path(data_dir: Path | None = None) -> Path:
    return (data_dir or app_config.DATA_DIR) / CONFIG_FILE


def state_path(data_dir: Path | None = None) -> Path:
    return (data_dir or app_config.DATA_DIR) / STATE_FILE


def load(data_dir: Path | None = None) -> MetaConfig | None:
    p = config_path(data_dir)
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    return MetaConfig(**raw)


def save(cfg: MetaConfig, data_dir: Path | None = None) -> None:
    p = config_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False))


class Watermarks:
    """How far each session has been distilled: {"source:session_id": last_ts}.

    Per-session rather than one global timestamp on purpose: transcripts get
    indexed late (an old session appearing for the first time must still be
    picked up whole) and get appended to (only the tail after the mark is new).
    """

    def __init__(self, path: Path):
        self.path = path
        self.marks: dict[str, int] = (
            json.loads(path.read_text()) if path.exists() else {})

    def key(self, source: str, session_id: str) -> str:
        return f"{source}:{session_id}"

    def last_ts(self, source: str, session_id: str) -> int:
        return int(self.marks.get(self.key(source, session_id), 0))

    def advance(self, source: str, session_id: str, ts: int) -> None:
        k = self.key(source, session_id)
        if ts > int(self.marks.get(k, 0)):
            self.marks[k] = int(ts)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.marks))
