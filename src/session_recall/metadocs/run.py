"""One meta docs run: collect → distill → scan → write → commit → advance.

Order is the safety property. Watermarks move only after the batch's documents
are safely in the repo, so a crash or a garbled model answer re-processes the
same dialogue next run instead of silently swallowing it. A project whose
distillation fails is skipped without advancing; the others still run.
"""

from dataclasses import dataclass, field
from pathlib import Path

from . import collect
from .config import MetaConfig, Watermarks, state_path
from .distill import Distiller
from .repo import commit, current_docs, ensure_repo, write_docs


@dataclass
class RunReport:
    projects: list = field(default_factory=list)   # (project, files_written, note)
    blocked: list = field(default_factory=list)    # (path, kinds) scanner stops
    committed: str = ""
    spillover: bool = False    # some dialogue waits for the next run

    def summary(self) -> str:
        if not self.projects and not self.blocked:
            return "meta docs: nothing new"
        lines = []
        for project, files, note in self.projects:
            what = ", ".join(files) if files else "no doc changes"
            lines.append(f"{project}: {what}{f'  ({note})' if note else ''}")
        for path, kinds in self.blocked:
            lines.append(f"BLOCKED by secret scanner: {path} [{', '.join(kinds)}] "
                         "— fix the source, the entry will retry next run")
        if self.committed:
            lines.append(f"committed {self.committed}")
        if self.spillover:
            lines.append("more dialogue than one run's budget — the rest runs next time")
        return "\n".join(lines)


def run_once(cfg: MetaConfig, db, distiller: Distiller,
             data_dir: Path | None = None) -> RunReport:
    repo = Path(cfg.repo).expanduser()
    ensure_repo(repo)
    marks = Watermarks(state_path(data_dir))
    report = RunReport()

    for project in collect.select_projects(db, cfg.projects):
        batch = collect.new_dialogue(db, project, marks)
        if not batch.turns:
            continue
        report.spillover = report.spillover or batch.spillover
        updates = distiller(project, batch.turns, current_docs(repo, project))
        if updates is None:
            # engine down or unparseable output: do not advance, try again later
            report.projects.append((project, [], "distill failed, will retry"))
            continue
        wr = write_docs(repo, project, updates)
        report.blocked.extend(wr.blocked)
        if wr.blocked:
            # something in this batch trips the scanner; keep the marks put so
            # a human can fix the doc source and the next run reprocesses
            report.projects.append((project, wr.written, "partially blocked"))
            continue
        collect.advance_marks(marks, batch)
        marks.save()
        report.projects.append((project, wr.written, ""))

    if any(files for _, files, _ in report.projects):
        report.committed = commit(
            repo, "meta docs: distill new sessions", push=cfg.push)
    return report
