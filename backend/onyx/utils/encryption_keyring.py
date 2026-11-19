"""Authenticated encryption of secrets at rest.

A sealed value is:

    MAGIC | key id length (1 byte) | key id (ASCII) | nonce (12 bytes) | AES-256-GCM ciphertext + tag

The header (MAGIC, length and key id) is the GCM associated data, so a value
cannot be moved to another key id without failing authentication. A value
without MAGIC is plain text written while encryption was off.
"""

import base64
import binascii
import json
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field

# A leading NUL byte never starts UTF-8 text that Onyx stores (JSON or a
# credential string), so plain text cannot be mistaken for a sealed value.
MAGIC = b"\x00OXF1"
LEGACY_KEY_ID = "legacy"
KEY_BYTES = 32
NONCE_BYTES = 12
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_LEGACY_KDF_INFO = b"onyx-fork secret encryption v1 legacy key"


class EncryptionConfigError(ValueError):
    """The encryption settings are unusable. Processes refuse to start."""


class DecryptionError(ValueError):
    """A stored value cannot be decrypted with the configured keys."""


class Keyring(BaseModel):
    model_config = ConfigDict(frozen=True)

    keys: dict[str, bytes] = Field(default_factory=dict)
    # None means new values are written as plain text.
    write_key_id: str | None = None

    @property
    def encrypts(self) -> bool:
        return self.write_key_id is not None


def _derive_legacy_key(secret: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=None,
        info=_LEGACY_KDF_INFO,
    ).derive(secret.encode())


def _reject_duplicate_ids(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EncryptionConfigError(f"ENCRYPTION_KEYS: key id {key!r} repeats.")
        result[key] = value
    return result


def _parse_keys_json(keys_json: str) -> dict[str, bytes]:
    try:
        parsed = json.loads(keys_json, object_pairs_hook=_reject_duplicate_ids)
    except json.JSONDecodeError as e:
        raise EncryptionConfigError(f"ENCRYPTION_KEYS is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise EncryptionConfigError("ENCRYPTION_KEYS must be a JSON object.")

    keys: dict[str, bytes] = {}
    for key_id, encoded in parsed.items():
        if not _KEY_ID_RE.match(key_id):
            raise EncryptionConfigError(
                f"ENCRYPTION_KEYS: key id {key_id!r} must have 1 to 32 letters, "
                "digits, '.', '_' or '-'."
            )
        if key_id == LEGACY_KEY_ID:
            raise EncryptionConfigError(
                f"ENCRYPTION_KEYS: the id {LEGACY_KEY_ID!r} is reserved for "
                "ENCRYPTION_KEY_SECRET."
            )
        if not isinstance(encoded, str):
            raise EncryptionConfigError(
                f"ENCRYPTION_KEYS: the key for {key_id!r} must be a base64 string."
            )
        try:
            raw = base64.b64decode(encoded, validate=True)
        except binascii.Error as e:
            raise EncryptionConfigError(
                f"ENCRYPTION_KEYS: the key for {key_id!r} is not valid base64."
            ) from e
        if len(raw) != KEY_BYTES:
            raise EncryptionConfigError(
                f"ENCRYPTION_KEYS: the key for {key_id!r} must decode to "
                f"{KEY_BYTES} bytes, not {len(raw)}."
            )
        keys[key_id] = raw
    return keys


def build_keyring(key_secret: str, keys_json: str, write_key_id: str) -> Keyring:
    """Builds the keyring from the three settings. Raises EncryptionConfigError
    when they are unusable."""
    keys = _parse_keys_json(keys_json) if keys_json else {}
    if key_secret:
        keys[LEGACY_KEY_ID] = _derive_legacy_key(key_secret)

    if write_key_id:
        if write_key_id not in keys:
            raise EncryptionConfigError(
                f"ENCRYPTION_WRITE_KEY_ID is {write_key_id!r}, but no key with "
                "that id is configured."
            )
        return Keyring(keys=keys, write_key_id=write_key_id)

    # Without a write key id, writes follow ENCRYPTION_KEY_SECRET.
    return Keyring(keys=keys, write_key_id=LEGACY_KEY_ID if key_secret else None)


def is_sealed(data: bytes) -> bool:
    return data.startswith(MAGIC)


def sealed_key_id(data: bytes) -> str | None:
    """The key id of a sealed value, or None for plain text."""
    if not is_sealed(data):
        return None
    return _split(data)[0]


def _header(key_id: str) -> bytes:
    encoded_id = key_id.encode("ascii")
    return MAGIC + bytes([len(encoded_id)]) + encoded_id


def _split(data: bytes) -> tuple[str, bytes, bytes, bytes]:
    """(key id, header, nonce, ciphertext) of a sealed value."""
    offset = len(MAGIC)
    if len(data) < offset + 1:
        raise DecryptionError("The sealed value is truncated.")
    id_length = data[offset]
    id_end = offset + 1 + id_length
    nonce_end = id_end + NONCE_BYTES
    if len(data) < nonce_end:
        raise DecryptionError("The sealed value is truncated.")
    try:
        key_id = data[offset + 1 : id_end].decode("ascii")
    except UnicodeDecodeError as e:
        raise DecryptionError("The sealed value has a malformed key id.") from e
    return key_id, data[:id_end], data[id_end:nonce_end], data[nonce_end:]


def seal(keyring: Keyring, plaintext: str) -> bytes:
    """Encrypts under the write key, or returns UTF-8 plain text when the
    keyring has no write key."""
    if keyring.write_key_id is None:
        return plaintext.encode()
    header = _header(keyring.write_key_id)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(keyring.keys[keyring.write_key_id]).encrypt(
        nonce, plaintext.encode(), header
    )
    return header + nonce + ciphertext


def unseal(keyring: Keyring, data: bytes) -> str:
    """Decrypts a sealed value, or decodes plain text."""
    if not is_sealed(data):
        try:
            return data.decode()
        except UnicodeDecodeError as e:
            raise DecryptionError(
                "The stored value is neither plain text nor in this build's "
                "encrypted format. It may be ciphertext from another Onyx edition."
            ) from e

    key_id, header, nonce, ciphertext = _split(data)
    key = keyring.keys.get(key_id)
    if key is None:
        raise DecryptionError(
            f"The value was encrypted with key id {key_id!r}, which is not "
            "configured. Add that key to ENCRYPTION_KEYS (or set "
            "ENCRYPTION_KEY_SECRET for 'legacy')."
        )
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, header).decode()
    except InvalidTag as e:
        raise DecryptionError(
            f"The value does not authenticate under key id {key_id!r}: the key "
            "is wrong or the value was changed."
        ) from e
