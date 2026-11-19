import base64
import json
import os

import pytest

from onyx.utils.encryption_keyring import (
    LEGACY_KEY_ID,
    MAGIC,
    DecryptionError,
    EncryptionConfigError,
    Keyring,
    build_keyring,
    seal,
    sealed_key_id,
    unseal,
)


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _keys_json(**keys: str) -> str:
    return json.dumps(keys)


def test_no_settings_writes_plain_text() -> None:
    keyring = build_keyring("", "", "")
    assert not keyring.encrypts
    assert seal(keyring, "secret") == b"secret"
    assert unseal(keyring, b"secret") == "secret"


def test_key_secret_alone_writes_under_legacy_key() -> None:
    keyring = build_keyring("my-secret", "", "")
    sealed = seal(keyring, "secret")
    assert sealed.startswith(MAGIC)
    assert b"secret" not in sealed
    assert sealed_key_id(sealed) == LEGACY_KEY_ID
    assert unseal(keyring, sealed) == "secret"


def test_write_key_id_selects_the_key() -> None:
    keyring = build_keyring("", _keys_json(**{"2026-11": _key()}), "2026-11")
    sealed = seal(keyring, '{"token": "x"}')
    assert sealed_key_id(sealed) == "2026-11"
    assert unseal(keyring, sealed) == '{"token": "x"}'


def test_same_plaintext_seals_differently() -> None:
    keyring = build_keyring("my-secret", "", "")
    assert seal(keyring, "secret") != seal(keyring, "secret")


def test_plain_text_stays_readable_after_encryption_is_on() -> None:
    keyring = build_keyring("my-secret", "", "")
    assert unseal(keyring, "written before".encode()) == "written before"


def test_rotation_reads_old_and_new_keys() -> None:
    old_key, new_key = _key(), _key()
    before = build_keyring("", _keys_json(old=old_key), "old")
    old_value = seal(before, "v1")

    after = build_keyring("", _keys_json(old=old_key, new=new_key), "new")
    assert unseal(after, old_value) == "v1"
    assert sealed_key_id(seal(after, "v2")) == "new"


def test_legacy_key_survives_a_switch_to_keys() -> None:
    before = build_keyring("my-secret", "", "")
    old_value = seal(before, "v1")
    after = build_keyring("my-secret", _keys_json(new=_key()), "new")
    assert unseal(after, old_value) == "v1"


def test_missing_key_id_fails_with_its_name() -> None:
    writer = build_keyring("", _keys_json(gone=_key()), "gone")
    reader = build_keyring("", _keys_json(other=_key()), "other")
    with pytest.raises(DecryptionError, match="'gone'"):
        unseal(reader, seal(writer, "v"))


def test_wrong_key_with_the_same_id_fails() -> None:
    writer = build_keyring("", _keys_json(k=_key()), "k")
    reader = build_keyring("", _keys_json(k=_key()), "k")
    with pytest.raises(DecryptionError, match="does not authenticate"):
        unseal(reader, seal(writer, "v"))


def test_tampered_value_fails() -> None:
    keyring = build_keyring("my-secret", "", "")
    sealed = bytearray(seal(keyring, "secret"))
    sealed[-1] ^= 0x01
    with pytest.raises(DecryptionError):
        unseal(keyring, bytes(sealed))


def test_moving_a_value_to_another_key_id_fails() -> None:
    shared = _key()
    keyring = build_keyring("", _keys_json(a=shared, b=shared), "a")
    sealed = seal(keyring, "secret")
    # Rewrite the header to the other id; the header is authenticated.
    relabelled = sealed.replace(MAGIC + b"\x01a", MAGIC + b"\x01b", 1)
    with pytest.raises(DecryptionError):
        unseal(keyring, relabelled)


def test_truncated_value_fails() -> None:
    with pytest.raises(DecryptionError, match="truncated"):
        unseal(Keyring(), MAGIC + b"\x05ab")


def test_foreign_binary_fails_clearly() -> None:
    with pytest.raises(DecryptionError, match="another Onyx edition"):
        unseal(Keyring(), b"\xff\xfe\x80binary")


@pytest.mark.parametrize(
    ("keys_json", "write_key_id", "message"),
    [
        ("not json", "", "not valid JSON"),
        ('["a"]', "", "JSON object"),
        ('{"a": "x", "a": "y"}', "", "repeats"),
        (json.dumps({"bad id!": base64.b64encode(b"0" * 32).decode()}), "", "1 to 32"),
        (
            json.dumps({LEGACY_KEY_ID: base64.b64encode(b"0" * 32).decode()}),
            "",
            "reserved",
        ),
        (json.dumps({"a": 5}), "", "base64 string"),
        (json.dumps({"a": "%%%"}), "", "not valid base64"),
        (json.dumps({"a": base64.b64encode(b"short").decode()}), "", "32 bytes"),
        ("", "missing", "no key with that id"),
    ],
)
def test_unusable_settings_are_rejected(
    keys_json: str, write_key_id: str, message: str
) -> None:
    with pytest.raises(EncryptionConfigError, match=message):
        build_keyring("", keys_json, write_key_id)


def test_legacy_write_key_id_needs_the_secret() -> None:
    with pytest.raises(EncryptionConfigError, match="no key with that id"):
        build_keyring("", "", LEGACY_KEY_ID)
    assert build_keyring("s", "", LEGACY_KEY_ID).write_key_id == LEGACY_KEY_ID


def test_stored_secrets_go_through_the_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onyx.utils import encryption

    monkeypatch.setattr(encryption, "_KEYRING", build_keyring("my-secret", "", ""))
    sealed = encryption.encrypt_string_to_bytes("api-key-123")
    assert sealed.startswith(MAGIC)
    assert b"api-key-123" not in sealed
    assert encryption.decrypt_bytes_to_string(sealed) == "api-key-123"


def test_unusable_settings_stop_startup_and_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onyx.utils import encryption

    monkeypatch.setattr(encryption, "_KEYRING", None)
    monkeypatch.setattr(encryption, "_KEYRING_ERROR", EncryptionConfigError("bad key"))
    with pytest.raises(EncryptionConfigError, match="bad key"):
        encryption.validate_encryption_configuration()
    with pytest.raises(EncryptionConfigError):
        encryption.encrypt_string_to_bytes("v")
