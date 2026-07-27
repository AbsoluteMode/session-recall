# tests/test_presets.py
import pytest
from session_recall.config import resolve_embed, PRESETS


def _never_probed():
    raise AssertionError("probe must not run when the choice is already determined")


def test_named_preset_configures_a_local_openai_compatible_endpoint():
    """`SESSION_RECALL_EMBED=ollama` is the whole configuration: one variable has to
    produce endpoint, model, dimension and reranker, or 'bring your own embedder'
    stays a four-variable puzzle nobody solves."""
    s = resolve_embed({"SESSION_RECALL_EMBED": "ollama"}, probe=_never_probed)
    assert s.provider == "openai-compatible"
    assert s.base_url == "http://127.0.0.1:11434/v1"
    assert s.model == "nomic-embed-text"
    assert s.dim == 768
    assert s.rerank_provider == "none", "local presets ship no reranker"


def test_local_presets_do_not_send_the_dimensions_parameter():
    """Ollama and llama.cpp reject `dimensions`; OpenAI requires it to match the index.
    Sending it unconditionally is why a local endpoint cannot be used today."""
    assert resolve_embed({"SESSION_RECALL_EMBED": "ollama"}, probe=_never_probed).send_dimensions is False
    assert resolve_embed({"SESSION_RECALL_EMBED": "openai"}, probe=_never_probed).send_dimensions is True


def test_explicit_variables_win_over_the_preset():
    """The preset is a default, not a cage — pointing at your own endpoint or model
    must stay possible without abandoning presets entirely."""
    s = resolve_embed({
        "SESSION_RECALL_EMBED": "ollama",
        "SESSION_RECALL_EMBED_MODEL": "mxbai-embed-large",
        "SESSION_RECALL_EMBED_DIM": "1024",
    }, probe=_never_probed)
    assert s.model == "mxbai-embed-large"
    assert s.dim == 1024
    assert s.base_url == "http://127.0.0.1:11434/v1", "untouched fields keep preset values"


def test_unknown_preset_names_the_valid_ones():
    with pytest.raises(ValueError) as exc:
        resolve_embed({"SESSION_RECALL_EMBED": "gpt5"}, probe=_never_probed)
    for name in PRESETS:
        assert name in str(exc.value), "the error must list what IS available"


def test_a_running_local_server_is_used_when_there_is_no_api_key():
    """No key and no preset used to mean 'fail on every request'. If a local endpoint
    is already up, that is a better default than a guaranteed 403."""
    s = resolve_embed({}, probe=lambda: "ollama")
    assert s.provider == "openai-compatible"
    assert s.model == "nomic-embed-text"


def test_an_api_key_suppresses_the_probe_entirely():
    """Autodetect must never add latency to a configured setup — with a key present
    the probe is not merely ignored, it must not run."""
    s = resolve_embed({"VOYAGE_API_KEY": "pa-whatever"}, probe=_never_probed)
    assert s.provider == "voyage"
    assert s.rerank_provider == "voyage"


def test_falls_back_to_voyage_when_nothing_is_running():
    s = resolve_embed({}, probe=lambda: None)
    assert s.provider == "voyage"
    assert s.model == "voyage-4-large"
