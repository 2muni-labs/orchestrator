"""Detached OpenPGP v4 signature checks with the standard library only (ADR 0019)."""

import base64
import hashlib
import hmac
from dataclasses import dataclass


class SignatureError(Exception):
    """The key or signature is malformed, unsupported or does not verify."""


@dataclass(frozen=True)
class PublicKey:
    fingerprint: str
    n: int
    e: int


class _Reader:
    """Read bounded fields without silently accepting truncated slices."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise SignatureError("Truncated OpenPGP field.")
        value = self.data[self.offset : end]
        self.offset = end
        return value

    def uint(self, size: int) -> int:
        return int.from_bytes(self.read(size), "big")

    def mpi(self) -> int:
        bits = self.uint(2)
        return self.uint((bits + 7) // 8)

    def at_end(self) -> bool:
        return self.offset == len(self.data)


def _crc24(data: bytes) -> bytes:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return (crc & 0xFFFFFF).to_bytes(3, "big")


def _dearmor(armored: str, label: str) -> bytes:
    lines = armored.strip().splitlines()
    if (
        len(lines) < 3
        or lines[0] != f"-----BEGIN PGP {label}-----"
        or lines[-1] != f"-----END PGP {label}-----"
    ):
        raise SignatureError("Invalid OpenPGP armor boundaries.")
    separator = 1
    while separator < len(lines) - 1 and lines[separator]:
        if ": " not in lines[separator]:
            raise SignatureError("Invalid OpenPGP armor header.")
        separator += 1
    if separator >= len(lines) - 1:
        raise SignatureError("Missing OpenPGP armor separator.")
    body = lines[separator + 1 : -1]
    checksum = None
    for index, line in enumerate(body):
        if line.startswith("="):
            if index != len(body) - 1:
                raise SignatureError("Misplaced OpenPGP armor checksum.")
            checksum = base64.b64decode(line[1:], validate=True)
    if checksum is not None:
        body = body[:-1]
    data = base64.b64decode("".join(body), validate=True)
    if not data:
        raise SignatureError("Empty OpenPGP armor body.")
    if checksum is not None and not hmac.compare_digest(checksum, _crc24(data)):
        raise SignatureError("OpenPGP armor checksum mismatch.")
    return data


def _new_length(reader: _Reader, *, subpacket: bool = False) -> int:
    first = reader.uint(1)
    if first < 192:
        return first
    if first <= (254 if subpacket else 223):
        return ((first - 192) << 8) + reader.uint(1) + 192
    if first == 255:
        return reader.uint(4)
    raise SignatureError("Partial OpenPGP packet lengths are unsupported.")


def _packet(reader: _Reader) -> tuple[int, bytes]:
    header = reader.uint(1)
    if not header & 0x80:
        raise SignatureError("Invalid OpenPGP packet header.")
    if header & 0x40:
        tag = header & 0x3F
        size = _new_length(reader)
    else:
        tag = (header >> 2) & 0x0F
        length_type = header & 0x03
        if length_type == 3:
            raise SignatureError(
                "Indeterminate OpenPGP packet lengths are unsupported."
            )
        size = reader.uint(1 << length_type)
    return tag, reader.read(size)


def _check_subpackets(data: bytes, fingerprint: bytes, *, hashed: bool) -> bool:
    reader = _Reader(data)
    created = False
    while not reader.at_end():
        size = _new_length(reader, subpacket=True)
        if size == 0:
            raise SignatureError("Empty OpenPGP signature subpacket.")
        packet = _Reader(reader.read(size))
        header = packet.uint(1)
        kind = header & 0x7F
        value = packet.read(size - 1)
        if hashed and header & 0x80 and kind not in (2, 33):
            raise SignatureError("Unsupported critical signature subpacket.")
        if kind == 2:
            created = True
        elif kind == 33:
            if not hmac.compare_digest(value, b"\x04" + fingerprint):
                raise SignatureError("Signature issuer fingerprint mismatch.")
        elif kind == 16:
            if not hmac.compare_digest(value, fingerprint[-8:]):
                raise SignatureError("Signature issuer key identifier mismatch.")
    return created


def load_public_key(armored: str) -> PublicKey:
    try:
        tag, body = _packet(_Reader(_dearmor(armored, "PUBLIC KEY BLOCK")))
        reader = _Reader(body)
        if tag != 6 or reader.uint(1) != 4:
            raise SignatureError("Expected an OpenPGP v4 public key.")
        reader.read(4)  # Creation time is included in the fingerprint.
        if reader.uint(1) != 1:
            raise SignatureError("Expected an RSA public key.")
        n = reader.mpi()
        e = reader.mpi()
        if not reader.at_end():
            raise SignatureError("Trailing public key data.")
        fingerprint = hashlib.sha1(
            b"\x99" + len(body).to_bytes(2, "big") + body
        ).hexdigest()
        return PublicKey(fingerprint.upper(), n, e)
    except SignatureError:
        raise
    except Exception:
        raise SignatureError("Invalid OpenPGP public key input.") from None


def verify_detached(document: bytes, armored_signature: str, key: PublicKey) -> None:
    try:
        packets = _Reader(_dearmor(armored_signature, "SIGNATURE"))
        tag, body = _packet(packets)
        if tag != 2 or not packets.at_end():
            raise SignatureError("Expected exactly one OpenPGP signature packet.")
        reader = _Reader(body)
        if reader.read(4) != b"\x04\x00\x01\x0a":
            raise SignatureError("Unsupported OpenPGP signature parameters.")
        hashed = reader.read(reader.uint(2))
        hashed_end = reader.offset
        unhashed = reader.read(reader.uint(2))
        left = reader.read(2)
        s = reader.mpi()
        if not reader.at_end():
            raise SignatureError("Trailing signature data.")

        fingerprint = bytes.fromhex(key.fingerprint)
        if len(key.fingerprint) != 40 or len(fingerprint) != 20:
            raise SignatureError("Invalid public key fingerprint.")
        if not _check_subpackets(hashed, fingerprint, hashed=True):
            raise SignatureError("Missing hashed signature creation time.")
        _check_subpackets(unhashed, fingerprint, hashed=False)

        digest = hashlib.sha512(document)
        digest.update(body[:hashed_end])
        digest.update(b"\x04\xff" + hashed_end.to_bytes(4, "big"))
        hashed_document = digest.digest()
        if not hmac.compare_digest(left, hashed_document[:2]):
            raise SignatureError("Signature hash prefix mismatch.")

        if key.e < 1 or not 1 <= s < key.n:
            raise SignatureError("Invalid RSA signature value or public key.")
        size = (key.n.bit_length() + 7) // 8
        info = bytes.fromhex("3051300d060960864801650304020305000440")
        padding = size - 3 - len(info) - len(hashed_document)
        if padding < 8:
            raise SignatureError("RSA signature padding is too short.")
        expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + info + hashed_document
        actual = pow(s, key.e, key.n).to_bytes(size, "big")
        if not hmac.compare_digest(actual, expected):
            raise SignatureError("RSA signature verification failed.")
    except SignatureError:
        raise
    except Exception:
        raise SignatureError("Invalid OpenPGP signature input.") from None
