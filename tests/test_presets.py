# tests/test_presets.py
import pytest
from session_recall.config import builtin_preset_for, resolve_embed, PRESETS


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


def test_nothing_at_all_falls_back_to_the_bundled_model():
    """No key, no preset, no local server used to mean 'fail on every request'.
    Out of the box must WORK: the bundled ONNX model runs on CPU with no
    account anywhere — and with no language chosen, multilingual is the one
    answer that is never wrong."""
    s = resolve_embed({}, probe=lambda: None)
    assert s.provider == "builtin"
    assert s.model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert s.rerank_provider == "none"


def test_the_interaction_language_picks_the_builtin_flavor():
    """A 70MB English specialist beats a 220MB generalist on English text —
    but only the user knows their language, so onboarding asks once and the
    answer steers the default."""
    en = resolve_embed({"SESSION_RECALL_LANG": "en"}, probe=lambda: None)
    assert en.model == "BAAI/bge-small-en-v1.5" and en.dim == 384
    zh = resolve_embed({"SESSION_RECALL_LANG": "zh"}, probe=lambda: None)
    assert zh.model == "BAAI/bge-small-zh-v1.5" and zh.dim == 512
    ru = resolve_embed({"SESSION_RECALL_LANG": "ru"}, probe=lambda: None)
    assert "multilingual" in ru.model


def test_builtin_alias_resolves_by_language():
    """`SESSION_RECALL_EMBED=builtin` must be a valid answer even though the
    real preset name depends on the language — the user says WHAT, config
    figures out WHICH."""
    s = resolve_embed({"SESSION_RECALL_EMBED": "builtin",
                       "SESSION_RECALL_LANG": "en"}, probe=_never_probed)
    assert s.model == "BAAI/bge-small-en-v1.5"


def test_an_explicit_env_dict_never_reads_the_settings_file(tmp_path, monkeypatch):
    """A passed-in env is the caller's whole world — the live settings file
    must not leak into it, or tests and scripted calls stop being hermetic."""
    from session_recall import config
    settings = tmp_path / "settings.json"
    settings.write_text('{"lang": "zh"}')
    monkeypatch.setattr(config, "SETTINGS_PATH", settings)
    s = resolve_embed({}, probe=lambda: None)
    assert s.model == PRESETS[builtin_preset_for(None)].model  # multi, not zh
    assert config.user_lang() == "zh", "the live path DOES read the file"


def test_a_key_still_beats_the_bundled_fallback():
    s = resolve_embed({"VOYAGE_API_KEY": "pa-x"}, probe=_never_probed)
    assert s.provider == "voyage"
