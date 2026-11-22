"""Rule-by-rule signature tests with a test RSA key (m-signature review follow-up).

Each test builds a signature whose hash, left 16 bits and RSA value are all correct
except for the one rule under test, so a refusal can only come from that rule.
"""

import base64
import hashlib
import random

import pytest

from orch.signature import PublicKey, SignatureError, verify_detached

DOCUMENT = b'{"version": "9.9.9"}\n'
FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
SHA512_INFO = bytes.fromhex("3051300d060960864801650304020305000440")
SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _probable_prime(rng: random.Random, bits: int) -> int:
    while True:
        candidate = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if all(candidate % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31)) and all(
            pow(a, candidate - 1, candidate) == 1 for a in (2, 3, 5, 7, 11, 13)
        ):
            d, r = candidate - 1, 0
            while d % 2 == 0:
                d //= 2
                r += 1
            if all(_miller_rabin(candidate, a, d, r) for a in (2, 3, 5, 7, 11, 13, 17)):
                return candidate


def _miller_rabin(n: int, a: int, d: int, r: int) -> bool:
    x = pow(a, d, n)
    if x in (1, n - 1):
        return True
    for _ in range(r - 1):
        x = pow(x, 2, n)
        if x == n - 1:
            return True
    return False


def make_key(bits: int, seed: int) -> tuple[PublicKey, int]:
    rng = random.Random(seed)
    e = 65537
    while True:
        p = _probable_prime(rng, bits // 2)
        q = _probable_prime(rng, bits - bits // 2)
        n = p * q
        phi = (p - 1) * (q - 1)
        if p != q and n.bit_length() == bits and phi % e:
            return PublicKey(FINGERPRINT, n, e), pow(e, -1, phi)


KEY, PRIVATE = make_key(1024, 1)


def subpacket(
    kind: int, value: bytes, *, critical: bool = False, form: int = 1
) -> bytes:
    body = bytes([kind | (0x80 if critical else 0)]) + value
    if form == 1:
        assert len(body) < 192
        return bytes([len(body)]) + body
    if form == 2:
        length = len(body) - 192
        assert 0 <= length < 0x3F00
        return bytes([(length >> 8) + 192, length & 0xFF]) + body
    return b"\xff" + len(body).to_bytes(4, "big") + body


CREATED = subpacket(2, (1_700_000_000).to_bytes(4, "big"))
ISSUER_FPR = subpacket(33, b"\x04" + bytes.fromhex(FINGERPRINT))
ISSUER_ID = subpacket(16, bytes.fromhex(FINGERPRINT)[-8:])


def encoded_message(digest: bytes, size: int, *, info: bytes = SHA512_INFO) -> bytes:
    return (
        b"\x00\x01"
        + b"\xff" * (size - 3 - len(info) - len(digest))
        + b"\x00"
        + info
        + digest
    )


def sign(
    *,
    hashed: bytes = CREATED + ISSUER_FPR,
    unhashed: bytes = ISSUER_ID,
    document: bytes = DOCUMENT,
    key: PublicKey = KEY,
    private: int = PRIVATE,
    em: bytes | None = None,
) -> str:
    """Return an armored signature; em replaces the encoded message when given."""
    prefix = b"\x04\x00\x01\x0a" + len(hashed).to_bytes(2, "big") + hashed
    digest = hashlib.sha512(
        document + prefix + b"\x04\xff" + len(prefix).to_bytes(4, "big")
    ).digest()
    size = (key.n.bit_length() + 7) // 8
    message = encoded_message(digest, size) if em is None else em(digest, size)
    s = pow(int.from_bytes(message, "big"), private, key.n)
    mpi = s.bit_length().to_bytes(2, "big") + s.to_bytes(
        (s.bit_length() + 7) // 8, "big"
    )
    body = prefix + len(unhashed).to_bytes(2, "big") + unhashed + digest[:2] + mpi
    packet = bytes([0xC2]) + _new_length(len(body)) + body
    text = base64.b64encode(packet).decode()
    return (
        "-----BEGIN PGP SIGNATURE-----\n\n" + text + "\n-----END PGP SIGNATURE-----\n"
    )


def _new_length(length: int) -> bytes:
    if length < 192:
        return bytes([length])
    length -= 192
    return bytes([(length >> 8) + 192, length & 0xFF])


def test_the_test_signature_verifies() -> None:
    verify_detached(DOCUMENT, sign(), KEY)


def test_the_test_signature_fails_for_another_document() -> None:
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT + b" ", sign(), KEY)


# RSA PKCS#1 v1.5 encoding: every other part of the signature is correct.


def _replace(message: bytes, index: int, value: int) -> bytes:
    data = bytearray(message)
    data[index] = value
    return bytes(data)


@pytest.mark.parametrize(
    "em",
    [
        pytest.param(
            lambda d, k: _replace(encoded_message(d, k), 1, 0x02), id="block-type-2"
        ),
        pytest.param(
            lambda d, k: _replace(encoded_message(d, k), 0, 0x01), id="first-byte"
        ),
        pytest.param(
            lambda d, k: _replace(encoded_message(d, k), 5, 0xFE), id="padding-not-ff"
        ),
        pytest.param(
            lambda d, k: _replace(encoded_message(d, k), k - 64 - 19 - 1, 0x01),
            id="separator-not-zero",
        ),
        pytest.param(
            lambda d, k: encoded_message(d[:32], k, info=SHA256_INFO), id="sha256-info"
        ),
        pytest.param(
            lambda d, k: _replace(encoded_message(d, k), k - 64 - 2, 0x03),
            id="digest-info-byte",
        ),
        pytest.param(
            lambda d, k: encoded_message(d, k - 1) + b"\x00", id="trailing-byte"
        ),
        pytest.param(
            lambda d, k: (
                b"\x00\x01\xff\xff\xff\xff\xff\xff\xff\xff\x00"
                + SHA512_INFO
                + d
                + b"\x00" * (k - 11 - 19 - 64)
            ),
            id="short-padding-garbage-after",
        ),
        pytest.param(lambda d, k: encoded_message(bytes(64), k), id="other-digest"),
    ],
)
def test_a_wrong_rsa_encoding_is_refused(em: object) -> None:
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, sign(em=em), KEY)


def test_a_modulus_with_fewer_than_eight_padding_bytes_is_refused() -> None:
    # 744 bits = 93 bytes: 93 - 3 - 19 - 64 = 7 bytes of 0xFF.
    key, private = make_key(744, 2)
    text = sign(key=key, private=private)
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, text, key)


def test_a_modulus_with_exactly_eight_padding_bytes_verifies() -> None:
    key, private = make_key(752, 3)
    verify_detached(DOCUMENT, sign(key=key, private=private), key)


# Subpacket rules: the signature is re-made over the changed hashed area.


def test_a_missing_creation_time_is_refused() -> None:
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, sign(hashed=ISSUER_FPR), KEY)


def test_a_creation_time_only_in_the_unhashed_area_is_refused() -> None:
    text = sign(hashed=ISSUER_FPR, unhashed=CREATED + ISSUER_ID)
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, text, KEY)


def test_only_a_creation_time_verifies() -> None:
    verify_detached(DOCUMENT, sign(hashed=CREATED, unhashed=b""), KEY)


@pytest.mark.parametrize("kind", [3, 20, 27, 30, 100])
def test_a_critical_unknown_hashed_subpacket_is_refused(kind: int) -> None:
    extra = subpacket(kind, b"\x01", critical=True)
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, sign(hashed=CREATED + ISSUER_FPR + extra), KEY)


@pytest.mark.parametrize("kind", [3, 20, 27, 30, 100])
def test_a_non_critical_unknown_hashed_subpacket_verifies(kind: int) -> None:
    extra = subpacket(kind, b"\x01")
    verify_detached(DOCUMENT, sign(hashed=CREATED + ISSUER_FPR + extra), KEY)


def test_critical_creation_time_and_issuer_fingerprint_verify() -> None:
    hashed = subpacket(2, (1_700_000_000).to_bytes(4, "big"), critical=True)
    hashed += subpacket(33, b"\x04" + bytes.fromhex(FINGERPRINT), critical=True)
    verify_detached(DOCUMENT, sign(hashed=hashed), KEY)


@pytest.mark.parametrize(
    "value",
    [
        b"\x04" + bytes(20),
        b"\x05" + bytes.fromhex(FINGERPRINT),
        b"\x04" + bytes.fromhex(FINGERPRINT)[:19],
        b"\x04" + bytes.fromhex(FINGERPRINT) + b"\x00",
    ],
)
def test_a_wrong_hashed_issuer_fingerprint_is_refused(value: bytes) -> None:
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, sign(hashed=CREATED + subpacket(33, value)), KEY)


def test_a_wrong_unhashed_issuer_fingerprint_is_refused() -> None:
    text = sign(unhashed=subpacket(33, b"\x04" + bytes(20)))
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, text, KEY)


def test_a_wrong_hashed_issuer_key_id_is_refused() -> None:
    text = sign(hashed=CREATED + subpacket(16, bytes(8)), unhashed=b"")
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, text, KEY)


def test_the_issuer_must_match_the_given_key_fingerprint() -> None:
    other = PublicKey("F" * 40, KEY.n, KEY.e)
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, sign(), other)


def test_no_issuer_subpackets_at_all_verifies() -> None:
    verify_detached(DOCUMENT, sign(hashed=CREATED, unhashed=b""), KEY)


@pytest.mark.parametrize("form", [2, 5])
def test_longer_subpacket_length_forms_verify(form: int) -> None:
    notation = subpacket(20, bytes(300), form=form)
    verify_detached(DOCUMENT, sign(hashed=CREATED + ISSUER_FPR + notation), KEY)
    verify_detached(DOCUMENT, sign(unhashed=ISSUER_ID + notation), KEY)


def test_an_empty_subpacket_is_refused() -> None:
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, sign(hashed=CREATED + b"\x00"), KEY)


def test_a_subpacket_longer_than_its_area_is_refused() -> None:
    with pytest.raises(SignatureError):
        verify_detached(DOCUMENT, sign(hashed=CREATED + b"\x05\x14\x00"), KEY)
