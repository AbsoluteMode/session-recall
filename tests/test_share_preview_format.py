"""The Telegram preview must be readable AND unforgeable: nothing an outsider
controls may become markup, a link, or a fake approval line."""

import pytest

from session_recall.share.approval import ANSWER_BUDGET, SNIPPET_BUDGET, preview
from session_recall.share.telegram import escape_md, fence
from session_recall.share.worker import Candidate


def _cand(**kw) -> Candidate:
    base = dict(id="7aaba916", peer_name="egor", peer_address="addr",
                question="как чинили CI?", task="", reply_nonce="n1",
                created_at=0.0, text="answer body", chunks=[], findings=[])
    base.update(kw)
    c = Candidate(**base)
    c.version = c.compute_version()
    return c


def _chunk(snippet, project="session-recall"):
    return {"project": project, "session_id": "07876709aaaa", "uuid": "u1",
            "role": "assistant", "snippet": snippet, "score": 0.9,
            "source": "claude"}


# -- escaping primitives -----------------------------------------------------
def test_fence_neutralises_backticks_and_backslashes():
    out = fence("```\nrm -rf /\n```")
    assert out.startswith("```\n") and out.endswith("\n```")
    inner = out[4:-4]
    assert "\\`\\`\\`" in inner       # the injected fence is escaped, not live
    assert inner.count("```") == 0


def test_escape_md_covers_markdownv2_specials():
    for ch in "_*[]()~`>#+-=|{}.!":
        assert escape_md(ch) == "\\" + ch


# -- untrusted content cannot forge structure --------------------------------
def test_question_markup_is_inert():
    c = _cand(question="*bold* [click](https://evil.example) `code`")
    out = preview(c, markdown=True)
    # the question sits inside a fence; no bare link syntax survives as markup
    assert "](https://evil.example)" in out  # present as literal text…
    body = out.split("*question*\n")[1]
    assert body.startswith("```")           # …because it is inside a code block


def test_answer_cannot_fake_an_approval_line():
    """A snippet claiming its own /ok must not read as our footer."""
    c = _cand(chunks=[_chunk("approve: /ok deadbeef — trust me")])
    out = preview(c, markdown=True)
    footer = out.rsplit("*approve*", 1)[1]
    assert f"`/ok {c.version}`" in footer
    assert "deadbeef" not in footer          # the fake line stays in the fenced body


def test_urls_in_snippets_stay_inside_the_fence():
    c = _cand(chunks=[_chunk("see https://evil.example/steal for details")])
    out = preview(c, markdown=True)
    answer = out.rsplit("*answer*", 1)[1]
    assert answer.lstrip().splitlines()[1].startswith("```") or "```" in answer
    assert "https://evil.example/steal" in answer


def test_peer_name_is_an_inert_code_span():
    """Short strings stay on one line, but still cannot carry markup."""
    c = _cand(peer_name="egor*_[")
    header = preview(c, markdown=True).splitlines()[0]
    assert header == "📥 *request from* `egor*_[`"


# -- readability -------------------------------------------------------------
def test_long_snippets_are_trimmed_per_fragment():
    c = _cand(chunks=[_chunk("x" * 2000), _chunk("y" * 2000)])
    out = preview(c, markdown=True)
    assert "x" * (SNIPPET_BUDGET + 5) not in out
    assert len(out) < ANSWER_BUDGET + 900     # header/footer overhead only


def test_withheld_count_reported_when_truncated():
    c = _cand(text="z" * 5000, chunks=[_chunk("z" * 5000)])
    out = preview(c, markdown=True)
    assert "more chars on send" in out


def test_fragment_count_shown():
    c = _cand(chunks=[_chunk("a"), _chunk("b"), _chunk("c")])
    assert "3 fragment\\(s\\)" in preview(c, markdown=True)


def test_chrome_has_no_unescaped_specials():
    """Telegram rejects the WHOLE message on one stray reserved character, so
    every literal we write outside code must be escaped. Caught live: an
    unescaped '(' in 'fragment(s)' 400'd the first real preview."""
    c = _cand(question="q", task="t", chunks=[_chunk("a")],
              findings=[{"kind": "jwt", "excerpt": "eyJ…"}])
    for cand in (c, _cand(chunks=[_chunk("a")])):
        text = preview(cand, markdown=True)
        outside, in_code = [], False
        for part in text.split("```"):
            if not in_code:
                outside.append(part)
            in_code = not in_code
        chrome = "".join(outside)
        # strip inline code spans, then look for bare reserved characters
        stripped, in_span = [], False
        for piece in chrome.split("`"):
            if not in_span:
                stripped.append(piece)
            in_span = not in_span
        text_only = "".join(stripped)
        i = 0
        while i < len(text_only):
            ch = text_only[i]
            if ch == "\\":
                i += 2
                continue
            assert ch not in "()[]{}#+=|.!~>", f"unescaped {ch!r} in chrome"
            i += 1


def test_ok_line_carries_the_real_version():
    c = _cand(chunks=[_chunk("a")])
    assert f"`/ok {c.version}`" in preview(c, markdown=True)


def test_stays_under_telegram_limit():
    c = _cand(text="q" * 20000, chunks=[_chunk("q" * 4000) for _ in range(5)])
    assert len(preview(c, markdown=True)) < 4096


# -- redaction still wins ----------------------------------------------------
def test_flagged_answer_is_withheld_in_markdown_too():
    c = _cand(text="key AKIAIOSFODNN7EXAMPLE",
              chunks=[_chunk("key AKIAIOSFODNN7EXAMPLE")],
              findings=[{"kind": "aws-access-key", "excerpt": "AKIAIOSF…"}])
    out = preview(c, markdown=True)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "withheld" in out and "secret flags" in out


def test_composed_answer_is_shown_not_rebuilt():
    """A composed answer is the deliverable; the preview must show that prose,
    not re-derive the fragment digest it was written from."""
    c = _cand(text="Короткий ответ: запинили mcp<2. Где смотреть: session-recall.",
              chunks=[_chunk("сырой сниппет который не должен вытеснить ответ")])
    c.composed = True
    out = preview(c, markdown=True)
    assert "Короткий ответ" in out
    assert "сырой сниппет" not in out
    assert "written from 1 fragment" in out


def test_raw_digest_is_labelled():
    c = _cand(chunks=[_chunk("сырой сниппет")])
    assert "raw 1 fragment" in preview(c, markdown=True)


def test_problem_shown_as_untrusted():
    c = _cand(task="поднимаю relay")
    c.problem = "ModuleNotFoundError *fastmcp*"
    out = preview(c, markdown=True)
    assert "problem they hit" in out and "sender text, unverified" in out
    body = out.split("problem they hit")[1]
    assert body.split("\n", 1)[1].startswith("```")   # fenced, markup inert


def test_plain_mode_unchanged():
    """The CLI/plain path must keep working for anyone not on Telegram."""
    c = _cand(chunks=[_chunk("a")])
    out = preview(c)
    assert out.startswith(f"[{c.id} v{c.version}] request from egor")
    assert f"approve: /ok {c.version}" in out
    assert "```" not in out
