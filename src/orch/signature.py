"""Detached OpenPGP v4 signature checks with the standard library only (ADR 0019)."""

from dataclasses import dataclass


class SignatureError(Exception):
    """The key or signature is malformed, unsupported or does not verify."""


@dataclass(frozen=True)
class PublicKey:
    fingerprint: str
    n: int
    e: int


def load_public_key(armored: str) -> PublicKey:
    raise NotImplementedError


def verify_detached(document: bytes, armored_signature: str, key: PublicKey) -> None:
    raise NotImplementedError
