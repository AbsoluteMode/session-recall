"""meta docs: the project's memory, written down where the project lives.

A scheduled job reads what was actually SAID in each session — user messages
and the assistant's final answers, never the tool noise between them — and
keeps four documents up to date in a git repository:

- per project: ``bugs.md`` (how a bug was recognized, diagnosed, fixed,
  and proven fixed), ``actions.md`` (how to perform the procedures the user
  asks for), ``decisions.md`` (why contested choices went the way they went);
- globally: ``USER.md`` — a map of where the user's information lives and how
  to FIND it. Retrieval instructions only, never the stored values.

The distiller is a caged text-in/text-out LLM call (same discipline as
share/compose.py), its output is scanned for secrets before a single byte
lands in the repo, and every write is a git commit — review, revert, blame
and sharing come from git instead of bespoke machinery.
"""
