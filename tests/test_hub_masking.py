"""Masking known secrets by Doppler name.

Two properties matter more than any single substitution: nothing recoverable
is stored in the map, and ordinary words are never masked — a map that eats
the string "dev" would quietly mangle the whole team's history."""

import json

from session_recall.hub.masking import SecretMap, collect_from_doppler, maskable

NETCUP = "Xk39dmPQ7wLz2vRt"          # long, mixed case + digits
ANTHROPIC = "sk-ant-api03-" + "A" * 40


def build(**entries) -> SecretMap:
    return SecretMap.build(entries, salt="test-salt")


def test_masks_a_known_value_with_its_variable_name():
    smap = build(**{"servers/NETCUP_PASSWORD": NETCUP})
    masked, hits = smap.mask(f"sshpass -p {NETCUP} ssh root@host")
    assert masked == "sshpass -p ${servers/NETCUP_PASSWORD} ssh root@host"
    assert hits == 1


def test_masks_a_value_glued_to_a_flag():
    """The widest tokenisation swallows `--key=…` whole; a narrower alphabet
    is what recovers the value itself."""
    smap = build(**{"session-recall/ANTHROPIC_API_KEY": ANTHROPIC})
    masked, hits = smap.mask(f"claude --key={ANTHROPIC} -p hi")
    assert "${session-recall/ANTHROPIC_API_KEY}" in masked
    assert ANTHROPIC not in masked and hits >= 1


def test_masks_every_occurrence():
    smap = build(**{"servers/NETCUP_PASSWORD": NETCUP})
    masked, hits = smap.mask(f"{NETCUP} and again {NETCUP}")
    assert NETCUP not in masked and hits == 2


def test_masks_inside_json_which_is_how_transcripts_actually_look():
    smap = build(**{"servers/NETCUP_PASSWORD": NETCUP})
    line = json.dumps({"role": "user", "text": f"пароль {NETCUP} для ssh"})
    masked, _ = smap.mask(line)
    assert NETCUP not in masked
    assert json.loads(masked)["text"] == "пароль ${servers/NETCUP_PASSWORD} для ssh"


def test_the_map_stores_no_recoverable_secret(tmp_path):
    smap = build(**{"servers/NETCUP_PASSWORD": NETCUP})
    path = tmp_path / "secrets.json"
    smap.save(path)
    assert NETCUP not in path.read_text()
    assert SecretMap.load(path).mask(NETCUP)[1] == 1


def test_short_and_label_like_values_are_never_masked():
    assert not maskable("DOPPLER_CONFIG", "dev")
    assert not maskable("DOPPLER_PROJECT", "session-recall")
    assert not maskable("SHORT", "abc123")                  # under the length bar
    assert not maskable("PEM", "-----BEGIN KEY-----\nabc")  # whitespace: not a token


def test_a_long_lowercase_word_needs_more_than_length():
    assert not maskable("WORD", "configuration")            # 13 chars, one class
    assert maskable("WORD", "configurationmanagement")      # 23 chars, still one
    assert maskable("KEY", "configuration7")                # two classes


def test_low_entropy_values_do_not_eat_ordinary_text():
    smap = build(**{"servers/DOPPLER_CONFIG": "dev",
                    "servers/NETCUP_PASSWORD": NETCUP})
    masked, hits = smap.mask("deploying to dev with the dev config")
    assert masked == "deploying to dev with the dev config" and hits == 0


def test_an_empty_map_is_a_no_op():
    smap = SecretMap(salt="", labels={})
    assert smap.mask("anything at all") == ("anything at all", 0)


def test_salt_is_reused_so_refreshes_stay_comparable():
    first = SecretMap.build({"a/B": NETCUP}, salt="fixed")
    second = SecretMap.build({"a/B": NETCUP}, salt=first.salt)
    assert first.labels == second.labels


def test_collect_walks_projects_configs_and_secrets():
    calls = []

    def runner(args):
        calls.append(args)
        if args[1] == "projects":
            return json.dumps([{"id": "servers"}, {"id": "locked"}])
        if args[1] == "configs":
            if args[-1] == "locked":
                raise RuntimeError("403")      # a project the token cannot read
            return json.dumps([{"name": "dev"}])
        return json.dumps({"NETCUP_PASSWORD": NETCUP, "PORT": 8080})

    entries = collect_from_doppler(runner=runner)
    assert entries == {"servers/NETCUP_PASSWORD": NETCUP}   # non-string PORT dropped
    assert any("locked" in " ".join(c) for c in calls)      # tried, then skipped
