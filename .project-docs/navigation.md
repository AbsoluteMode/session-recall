# Documentation navigation

## Search order

1. Classify the question using `manifest.yaml` routes.
2. Search only routed paths with task terms, identifiers, tags, and synonyms.
3. Read the best canonical match.
4. Follow only material `related` links.
5. Check status, freshness, and sources before using a claim.

The product overview is canonical in `project.md`. Detailed engineering
rationale remains in the repository's `docs/decisions/` directory and is cited
as repository evidence rather than duplicated here.

## Stopping rules

- No canonical match means unknown.
- Observations remain unconfirmed.
- Stale records require re-verification.
- Conflicts preserve every sourced version.
- Recommendations are not current behavior.
- Missing sources invalidate confirmed claims.

Record missing knowledge in `open-questions.md`; do not invent a project
default.
