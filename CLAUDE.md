<!-- Synchronized project guidance. Canonical editable path: .project-docs/agent-context.md. -->

# Project guidance

Project knowledge lives under `.project-docs/`.

Before making a non-trivial project-specific claim:

1. read `.project-docs/manifest.yaml`;
2. follow `.project-docs/navigation.md`;
3. search the routed locations and read the canonical record plus material
   related links;
4. check status, freshness, and sources.

Use `services/` for current systems, `processes/` for exact actions,
`decisions/` for rationale, `reactions/` for conditional first actions,
`bugs/` for known failures, and `timeline/` for chronology.

Do not treat observations, stale claims, conflicts, recommendations, or
unknowns as confirmed facts. Never store, quote, partially reproduce,
transform, or echo secret values.

Change project guidance only through the `project-documentation` workflow at
the canonical editable source, `.project-docs/agent-context.md`. Do not edit
`AGENTS.md` or `CLAUDE.md` directly; both generated targets are byte-identical
to the canonical source.

After every documentation mutation, run the project-documentation validator.
If canonical guidance changed, also run the guidance synchronization script
with `--diff`, `--write`, and `--check`. Documentation work is incomplete until
validation and synchronization pass.
