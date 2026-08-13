"""Envelope encryption with deployment-held wrapping keys."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.exceptions import CryptoError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_KEY_BYTES = 32
_NONCE_BYTES = 24


class EnvelopeDecryptionError(Exception):
    """A non-specific failure to unwrap or decrypt one value."""


@dataclass(frozen=True, slots=True)
class EncryptedEnvelope:
    """Database-safe envelope fields."""

    ciphertext: bytes
    encrypted_data_key: bytes
    wrapping_key_id: str


class EnvelopeCipher:
    """Encrypt data keys and values with separate XChaCha20 contexts."""

    def __init__(
        self,
        wrapping_keys: Mapping[str, bytes],
        *,
        current_key_id: str,
        random_bytes: Callable[[int], bytes],
    ) -> None:
        """Derive fixed-size AEAD keys from deployment-held key material."""
        if current_key_id not in wrapping_keys:
            msg = "The current wrapping key is not available."
            raise ValueError(msg)
        if any(
            not key_id or len(value) < _KEY_BYTES
            for key_id, value in wrapping_keys.items()
        ):
            msg = "Each wrapping key must have an identity and at least 256 bits."
            raise ValueError(msg)
        self._keys = {
            key_id: hashlib.blake2b(
                value,
                digest_size=_KEY_BYTES,
                person=b"llmr-wrap-v1",
            ).digest()
            for key_id, value in wrapping_keys.items()
        }
        self.current_key_id = current_key_id
        self._random_bytes = random_bytes

    @property
    def available_key_ids(self) -> frozenset[str]:
        """Return safe available wrapping-key identities."""
        return frozenset(self._keys)

    def encrypt(
        self, plaintext: bytes, *, context: dict[str, str]
    ) -> EncryptedEnvelope:
        """Encrypt one value under a random data key and the current wrapping key."""
        data_key = bytearray(self._random_bytes(_KEY_BYTES))
        if len(data_key) != _KEY_BYTES:
            msg = "The random source did not return the required key length."
            raise ValueError(msg)
        try:
            ciphertext = self._seal(
                plaintext,
                key=bytes(data_key),
                aad=_aad("secret", context),
            )
            encrypted_data_key = self._seal(
                bytes(data_key),
                key=self._keys[self.current_key_id],
                aad=_aad("data-key", context, key_id=self.current_key_id),
            )
        finally:
            data_key[:] = bytes(_KEY_BYTES)
        return EncryptedEnvelope(ciphertext, encrypted_data_key, self.current_key_id)

    def decrypt(
        self,
        envelope: EncryptedEnvelope,
        *,
        context: dict[str, str],
    ) -> bytearray:
        """Decrypt one value or fail without key or ciphertext detail."""
        wrapping_key = self._keys.get(envelope.wrapping_key_id)
        if wrapping_key is None:
            raise EnvelopeDecryptionError
        data_key = bytearray()
        try:
            data_key.extend(
                self._open(
                    envelope.encrypted_data_key,
                    key=wrapping_key,
                    aad=_aad("data-key", context, key_id=envelope.wrapping_key_id),
                )
            )
            return bytearray(
                self._open(
                    envelope.ciphertext,
                    key=bytes(data_key),
                    aad=_aad("secret", context),
                )
            )
        except (CryptoError, ValueError) as error:
            raise EnvelopeDecryptionError from error
        finally:
            data_key[:] = bytes(len(data_key))

    def rewrap(
        self,
        encrypted_data_key: bytes,
        *,
        old_key_id: str,
        context: dict[str, str],
    ) -> bytes:
        """Move one data key to the current wrapping key without secret plaintext."""
        old_key = self._keys.get(old_key_id)
        if old_key is None:
            raise EnvelopeDecryptionError
        data_key = bytearray()
        try:
            data_key.extend(
                self._open(
                    encrypted_data_key,
                    key=old_key,
                    aad=_aad("data-key", context, key_id=old_key_id),
                )
            )
            return self._seal(
                bytes(data_key),
                key=self._keys[self.current_key_id],
                aad=_aad("data-key", context, key_id=self.current_key_id),
            )
        except (CryptoError, ValueError) as error:
            raise EnvelopeDecryptionError from error
        finally:
            data_key[:] = bytes(len(data_key))

    def _seal(self, value: bytes, *, key: bytes, aad: bytes) -> bytes:
        nonce = self._random_bytes(_NONCE_BYTES)
        if len(nonce) != _NONCE_BYTES:
            msg = "The random source did not return the required nonce length."
            raise ValueError(msg)
        return nonce + crypto_aead_xchacha20poly1305_ietf_encrypt(
            value, aad, nonce, key
        )

    @staticmethod
    def _open(value: bytes, *, key: bytes, aad: bytes) -> bytes:
        if len(value) <= _NONCE_BYTES:
            raise ValueError
        nonce, ciphertext = value[:_NONCE_BYTES], value[_NONCE_BYTES:]
        return crypto_aead_xchacha20poly1305_ietf_decrypt(ciphertext, aad, nonce, key)


def _aad(domain: str, context: dict[str, str], *, key_id: str | None = None) -> bytes:
    document = {"domain": domain, "version": 1, **context}
    if key_id is not None:
        document["wrapping_key_id"] = key_id
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
