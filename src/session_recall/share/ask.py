"""Composing a request worth answering.

A bare "не работает" is not a question — it hands the answering side a search
with nothing to search on and hands the owner a preview they cannot judge. The
protocol therefore carries three parts, and this module refuses to send until
all three say something:

  doing    — what the asker is building or trying to do
  problem  — what went wrong, with the symptoms they actually observed
  want     — what they want to know from the owner's history

Retrieval quality follows directly: the query is built from all three, so
symptoms and context steer it, not just the closing question.
"""

MIN_DOING = 25
MIN_PROBLEM = 25
MIN_WANT = 15

TEMPLATE = """\
say what you are doing, what broke, and what you want to know:

  session-recall share ask <peer> \\
    --doing   "поднимаю relay session-recall на своём сервере, ставлю из git" \\
    --problem "CI падает на сборе тестов: ModuleNotFoundError mcp.server.fastmcp" \\
    --want    "как вы это чинили — пин версии или миграция на новый API?"

each part carries its weight: `--doing` and `--problem` steer the search,
`--want` tells the owner what to approve. \"не работает\" is not a request."""


class AskTooThin(ValueError):
    pass


def validate(doing: str, problem: str, want: str) -> dict:
    """Returns the request body, or raises with the specific part to fix."""
    thin = []
    if len(doing.strip()) < MIN_DOING:
        thin.append(f"--doing needs at least {MIN_DOING} characters of context")
    if len(problem.strip()) < MIN_PROBLEM:
        thin.append(f"--problem needs at least {MIN_PROBLEM} characters: "
                    "what broke, and what you saw")
    if len(want.strip()) < MIN_WANT:
        thin.append(f"--want needs at least {MIN_WANT} characters: "
                    "the actual question")
    if thin:
        raise AskTooThin("\n".join(f"  - {t}" for t in thin) + "\n\n" + TEMPLATE)
    return {"task": doing.strip(), "problem": problem.strip(),
            "question": want.strip()}


def retrieval_query(body: dict) -> str:
    """All three parts steer the search — symptoms often match the transcript
    where the polished question does not."""
    return " ".join(p for p in (body.get("question", ""), body.get("problem", ""),
                                body.get("task", "")) if p).strip()
