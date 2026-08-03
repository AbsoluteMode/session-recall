"""End-to-end ceremony through the real CLI with a FileTransport: two homes,
one shared folder, both sides finish with each other in their trust stores."""

import pytest

from session_recall import cli, config


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_RECALL_SHARE_TRANSPORT_DIR", str(tmp_path / "relay"))
    monkeypatch.delenv("SESSION_RECALL_RELAY_URL", raising=False)

    def as_user(home):
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / home)
    return as_user


def _run(argv):
    return cli.main(argv) or 0


def test_full_ceremony(homes, capsys):
    homes("maxim")
    assert _run(["share", "init", "maxim"]) == 0
    assert _run(["share", "invite"]) == 0
    out = capsys.readouterr().out
    code = next(line.strip() for line in out.splitlines()
                if line.strip() and "-" in line and " " not in line.strip())

    homes("egor")
    assert _run(["share", "init", "egor"]) == 0
    assert _run(["share", "join", code]) == 0
    sas_egor = capsys.readouterr().out
    # enrolling without choosing a name must not work — the bundle's name is
    # the peer's own claim
    assert _run(["share", "trust"]) == 1
    assert "name you choose" in capsys.readouterr().out
    assert _run(["share", "trust", "maxim-lead"]) == 0

    homes("maxim")
    assert _run(["share", "complete"]) == 0
    sas_maxim = capsys.readouterr().out
    assert _run(["share", "trust", "egor-work"]) == 0

    sas = [l for l in sas_egor.splitlines() if "SAS code:" in l]
    assert sas and sas == [l for l in sas_maxim.splitlines() if "SAS code:" in l]

    assert _run(["share", "devices"]) == 0
    out = capsys.readouterr().out
    assert "egor-work" in out and '"egor"' in out   # petname + their claim

    homes("egor")
    assert _run(["share", "devices"]) == 0
    assert "maxim-lead" in capsys.readouterr().out


def test_join_with_bad_code_fails_cleanly(homes, capsys):
    homes("egor")
    _run(["share", "init", "egor"])
    assert _run(["share", "join", "zzzz-zzzz"]) == 1
    assert "pairing failed" in capsys.readouterr().out


def test_trust_requires_pairing(homes, capsys):
    homes("maxim")
    _run(["share", "init", "maxim"])
    assert _run(["share", "trust", "egor"]) == 1
    assert "nothing to trust" in capsys.readouterr().out


def test_revoke_and_allow(homes, capsys):
    homes("maxim")
    _run(["share", "init", "maxim"])

    assert _run(["share", "allow"]) == 0
    assert "default deny" in capsys.readouterr().out
    # granting without saying who must not work — opening a project to every
    # contact has to be typed out, never the accident of a forgotten flag
    assert _run(["share", "allow", "session-recall"]) == 1
    assert "say who" in capsys.readouterr().out
    assert _run(["share", "allow", "session-recall", "--to", "all"]) == 0
    assert _run(["share", "allow"]) == 0
    assert "session-recall" in capsys.readouterr().out
    assert _run(["share", "allow", "session-recall", "--to", "all", "--remove"]) == 0
    assert _run(["share", "allow", "x", "--to", "ghost"]) == 1  # unknown contact

    assert _run(["share", "revoke", "nobody"]) == 1


def test_pause_and_resume(homes, capsys):
    homes("maxim")
    _run(["share", "init", "maxim"])
    assert _run(["share", "pause"]) == 0
    assert "paused" in capsys.readouterr().out
    assert _run(["share", "resume"]) == 0
    assert "resumed" in capsys.readouterr().out


def test_commands_without_identity_point_to_init(homes, capsys):
    homes("maxim")
    assert _run(["share", "devices"]) == 1
    assert "share init" in capsys.readouterr().out


def test_no_transport_hint_says_what_to_set(homes, capsys, monkeypatch):
    """No transport is the fresh-install default now — the failure must teach
    the fix (which env var, where the recipes live), not just refuse."""
    monkeypatch.delenv("SESSION_RECALL_SHARE_TRANSPORT_DIR", raising=False)
    monkeypatch.delenv("SESSION_RECALL_RELAY_URL", raising=False)
    homes("maxim")
    _run(["share", "init", "maxim"])
    assert _run(["share", "invite"]) == 1
    out = capsys.readouterr().out
    assert "no transport configured" in out
    assert "SESSION_RECALL_RELAY_URL" in out
