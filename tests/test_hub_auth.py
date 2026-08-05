"""Member keys: what they prove, what a leaked hub database does not give away."""

import json

import pytest

from session_recall.hub.auth import KeyStore, bearer, parse_owner


@pytest.fixture
def keys(tmp_path):
    return KeyStore(tmp_path / "keys.json")


def test_issued_key_verifies_to_its_owner(keys):
    key = keys.issue("egor", note="macbook")
    assert keys.verify(f"Bearer {key}") == "egor"


def test_key_carries_the_owner_in_the_clear(keys):
    assert parse_owner(keys.issue("egor")) == "egor"


@pytest.mark.parametrize("header", [
    None, "", "Bearer", "Bearer   ", "Basic sr_egor_" + "0" * 32,
    "Bearer sr_egor_nothex", "Bearer sr_egor_0000", "Bearer not-a-key",
    "Bearer sr_EGOR_" + "0" * 32,            # owner names are lowercase
])
def test_malformed_credentials_are_refused(keys, header):
    keys.issue("egor")
    assert keys.verify(header) is None


def test_an_unissued_but_well_formed_key_is_refused(keys):
    keys.issue("egor")
    assert keys.verify("Bearer sr_egor_" + "a" * 32) is None


def test_revoking_by_owner_kills_every_device_at_once(keys):
    laptop, desktop = keys.issue("egor", "laptop"), keys.issue("egor", "desktop")
    other = keys.issue("maxim")
    assert keys.revoke("egor") == 2
    assert keys.verify(f"Bearer {laptop}") is None
    assert keys.verify(f"Bearer {desktop}") is None
    assert keys.verify(f"Bearer {other}") == "maxim"


def test_revoking_by_key_id_kills_one_device(keys):
    laptop, desktop = keys.issue("egor", "laptop"), keys.issue("egor", "desktop")
    target = next(r["id"] for r in keys.listing() if r["note"] == "laptop")
    assert keys.revoke(target) == 1
    assert keys.verify(f"Bearer {laptop}") is None
    assert keys.verify(f"Bearer {desktop}") == "egor"


def test_revoking_twice_is_not_counted_again(keys):
    keys.issue("egor")
    assert keys.revoke("egor") == 1
    assert keys.revoke("egor") == 0


def test_the_store_never_holds_a_usable_key(keys, tmp_path):
    key = keys.issue("egor")
    on_disk = (tmp_path / "keys.json").read_text()
    assert key not in on_disk
    assert key.split("_")[-1] not in on_disk


def test_listing_shows_members_without_leaking_credentials(keys):
    key = keys.issue("egor", "macbook")
    row, = keys.listing()
    assert (row["owner"], row["note"]) == ("egor", "macbook")
    assert key not in json.dumps(row)


def test_bad_owner_names_are_refused_at_issue(keys):
    for bad in ("", "Egor", "e" * 33, "../maxim", "egor egor"):
        with pytest.raises(ValueError):
            keys.issue(bad)


def test_bearer_parsing_is_case_insensitive_on_the_scheme():
    assert bearer("bearer abc") == "abc"
    assert bearer("BEARER abc") == "abc"
    assert bearer("Bearerabc") is None
