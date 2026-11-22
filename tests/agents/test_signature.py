import ast
import base64
from pathlib import Path

import pytest

from orch.signature import PublicKey, SignatureError, load_public_key, verify_detached

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "tests" / "data" / "claude-manifests"
KEY_TEXT = (ROOT / "config" / "keys" / "claude-code.asc").read_text()
FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
VERSIONS = ["2.1.300", "2.1.319"]


def manifest(version: str) -> bytes:
    return (DATA / f"manifest-{version}.json").read_bytes()


def signature(version: str) -> str:
    return (DATA / f"manifest-{version}.json.sig").read_text()


# armor helpers written independently of the module under test


def crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def dearmor(text: str) -> bytes:
    lines = text.strip().splitlines()
    blank = lines.index("")
    body = [line for line in lines[blank + 1 : -1] if not line.startswith("=")]
    return base64.b64decode("".join(body))


def armor(
    data: bytes,
    label: str = "SIGNATURE",
    *,
    headers: tuple[str, ...] = (),
    crc: bool = True,
    crc_value: int | None = None,
) -> str:
    encoded = base64.b64encode(data).decode()
    lines = [f"-----BEGIN PGP {label}-----", *headers, ""]
    lines += [encoded[i : i + 64] for i in range(0, len(encoded), 64)]
    if crc:
        value = crc24(data) if crc_value is None else crc_value
        lines.append("=" + base64.b64encode(value.to_bytes(3, "big")).decode())
    lines.append(f"-----END PGP {label}-----")
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def key() -> PublicKey:
    return load_public_key(KEY_TEXT)


# old format packet: one tag byte, then a two byte length
SIG_HEADER = 3


def raw_signature(version: str = "2.1.319") -> bytes:
    raw = dearmor(signature(version))
    assert raw[0] == 0x89 and int.from_bytes(raw[1:3], "big") == len(raw) - 3
    return raw


def mutate(raw: bytes, offset: int, value: int) -> bytes:
    data = bytearray(raw)
    data[SIG_HEADER + offset] = value
    return bytes(data)


# load_public_key


def test_the_committed_key_loads_with_the_pinned_fingerprint(key: PublicKey) -> None:
    assert key.fingerprint == FINGERPRINT
    assert key.e == 65537
    assert key.n.bit_length() == 4096


def test_a_key_loads_with_armor_headers_and_without_a_crc() -> None:
    raw = dearmor(KEY_TEXT)
    for text in [
        armor(raw, "PUBLIC KEY BLOCK", headers=("Comment: x", "Version: y")),
        armor(raw, "PUBLIC KEY BLOCK", crc=False),
    ]:
        assert load_public_key(text).fingerprint == FINGERPRINT


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not armor",
        signature("2.1.319"),  # a signature, not a key
        KEY_TEXT.replace("PUBLIC KEY BLOCK-----\n", "PUBLIC KEY BLOCK-----\nx\n", 1),
    ],
)
def test_a_bad_key_armor_is_refused(text: str) -> None:
    with pytest.raises(SignatureError):
        load_public_key(text)


def test_a_key_with_a_wrong_crc_is_refused() -> None:
    raw = dearmor(KEY_TEXT)
    with pytest.raises(SignatureError):
        load_public_key(armor(raw, "PUBLIC KEY BLOCK", crc_value=crc24(raw) ^ 1))


def test_a_key_without_the_blank_line_is_refused() -> None:
    lines = KEY_TEXT.strip().splitlines()
    lines.remove("")
    with pytest.raises(SignatureError):
        load_public_key("\n".join(lines))


@pytest.mark.parametrize(("offset", "value"), [(0, 3), (5, 17), (5, 22)])
def test_a_key_other_than_v4_rsa_is_refused(offset: int, value: int) -> None:
    raw = bytearray(dearmor(KEY_TEXT))
    raw[3 + offset] = value
    with pytest.raises(SignatureError):
        load_public_key(armor(bytes(raw), "PUBLIC KEY BLOCK"))


def test_a_first_packet_that_is_not_a_public_key_is_refused() -> None:
    raw = bytearray(dearmor(KEY_TEXT))
    raw[0] = 0x80 | (14 << 2) | 1  # public subkey tag, same length form
    with pytest.raises(SignatureError):
        load_public_key(armor(bytes(raw), "PUBLIC KEY BLOCK"))


def test_every_truncated_key_raises_only_signature_error() -> None:
    raw = dearmor(KEY_TEXT)
    key_packet_end = 3 + int.from_bytes(raw[1:3], "big")
    for length in range(key_packet_end):
        with pytest.raises(SignatureError):
            load_public_key(armor(raw[:length], "PUBLIC KEY BLOCK"))


# verify_detached


@pytest.mark.parametrize("version", VERSIONS)
def test_a_real_manifest_signature_verifies(key: PublicKey, version: str) -> None:
    assert verify_detached(manifest(version), signature(version), key) is None


def test_a_signature_for_another_manifest_fails(key: PublicKey) -> None:
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.300"), signature("2.1.319"), key)


@pytest.mark.parametrize("position", [0, 1, 100, -2, -1])
def test_a_one_byte_change_in_the_manifest_fails(key: PublicKey, position: int) -> None:
    data = bytearray(manifest("2.1.319"))
    data[position] ^= 0x01
    with pytest.raises(SignatureError):
        verify_detached(bytes(data), signature("2.1.319"), key)


def test_an_extra_byte_after_the_manifest_fails(key: PublicKey) -> None:
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319") + b"\n", signature("2.1.319"), key)


def test_another_rsa_key_fails(key: PublicKey) -> None:
    other = PublicKey(key.fingerprint, key.n + 2, key.e)
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), signature("2.1.319"), other)


def test_the_same_numbers_under_another_fingerprint_fail(key: PublicKey) -> None:
    other = PublicKey("0" * 40, key.n, key.e)
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), signature("2.1.319"), other)


def test_a_signature_with_armor_headers_and_without_a_crc_verifies(
    key: PublicKey,
) -> None:
    raw = raw_signature()
    for text in [armor(raw, headers=("Comment: x",)), armor(raw, crc=False)]:
        verify_detached(manifest("2.1.319"), text, key)


def test_a_new_format_packet_header_verifies(key: PublicKey) -> None:
    raw = raw_signature()
    body = raw[SIG_HEADER:]
    length = len(body) - 192
    header = bytes([0xC0 | 2, (length >> 8) + 192, length & 0xFF])
    verify_detached(manifest("2.1.319"), armor(header + body), key)


def test_a_five_byte_new_format_length_verifies(key: PublicKey) -> None:
    raw = raw_signature()
    body = raw[SIG_HEADER:]
    header = bytes([0xC0 | 2, 0xFF]) + len(body).to_bytes(4, "big")
    verify_detached(manifest("2.1.319"), armor(header + body), key)


def test_a_partial_body_length_is_refused(key: PublicKey) -> None:
    raw = raw_signature()
    body = raw[SIG_HEADER:]
    header = bytes([0xC0 | 2, 0xE9])  # partial length 512
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), armor(header + body), key)


def test_an_indeterminate_old_format_length_is_refused(key: PublicKey) -> None:
    raw = raw_signature()
    with pytest.raises(SignatureError):
        verify_detached(
            manifest("2.1.319"), armor(bytes([0x8B]) + raw[SIG_HEADER:]), key
        )


def test_a_wrong_crc_is_refused(key: PublicKey) -> None:
    raw = raw_signature()
    with pytest.raises(SignatureError):
        verify_detached(
            manifest("2.1.319"), armor(raw, crc_value=crc24(raw) ^ 0x10), key
        )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "-----BEGIN PGP SIGNATURE-----\n\n@@@@\n-----END PGP SIGNATURE-----\n",
        KEY_TEXT,
        signature("2.1.319").replace("SIGNATURE-----\n", "MESSAGE-----\n", 1),
    ],
)
def test_bad_signature_armor_is_refused(key: PublicKey, text: str) -> None:
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), text, key)


@pytest.mark.parametrize(
    ("offset", "value"),
    [
        (0, 3),  # version
        (0, 5),
        (1, 0x01),  # canonical text document
        (2, 17),  # DSA
        (3, 8),  # SHA-256
        (3, 2),  # SHA-1
    ],
)
def test_unsupported_signature_fields_are_refused(
    key: PublicKey, offset: int, value: int
) -> None:
    with pytest.raises(SignatureError):
        verify_detached(
            manifest("2.1.319"), armor(mutate(raw_signature(), offset, value)), key
        )


def hashed_end(raw: bytes) -> int:
    body = raw[SIG_HEADER:]
    return 6 + int.from_bytes(body[4:6], "big")


def test_wrong_left_sixteen_bits_are_refused(key: PublicKey) -> None:
    raw = raw_signature()
    body = raw[SIG_HEADER:]
    end = hashed_end(raw)
    unhashed = int.from_bytes(body[end : end + 2], "big")
    left = end + 2 + unhashed
    data = mutate(raw, left, body[left] ^ 0xFF)
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), armor(data), key)


def test_a_wrong_issuer_key_id_in_the_unhashed_area_is_refused(key: PublicKey) -> None:
    raw = raw_signature()
    body = raw[SIG_HEADER:]
    end = hashed_end(raw)
    # the unhashed area holds exactly one issuer key id subpacket: 09 10 <8 bytes>
    assert body[end : end + 4] == bytes([0x00, 0x0A, 0x09, 0x10])
    data = mutate(raw, end + 4, body[end + 4] ^ 0x01)
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), armor(data), key)


def test_a_critical_flag_in_the_unhashed_area_is_not_refused(key: PublicKey) -> None:
    # Only the hashed area is checked for critical types; an unhashed issuer key id
    # marked critical still carries the right value.
    raw = raw_signature()
    end = hashed_end(raw)
    data = mutate(raw, end + 3, 0x80 | 0x10)
    verify_detached(manifest("2.1.319"), armor(data), key)


def test_a_signature_with_s_not_below_n_is_refused(key: PublicKey) -> None:
    raw = raw_signature()
    body = raw[SIG_HEADER:]
    end = hashed_end(raw)
    unhashed = int.from_bytes(body[end : end + 2], "big")
    mpi = end + 2 + unhashed + 2
    s = key.n + int.from_bytes(body[mpi + 2 :], "big")
    new_mpi = s.bit_length().to_bytes(2, "big") + s.to_bytes(
        (s.bit_length() + 7) // 8, "big"
    )
    new_body = body[:mpi] + new_mpi
    header = bytes([0x89]) + len(new_body).to_bytes(2, "big")
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), armor(header + new_body), key)


def test_a_trailing_byte_inside_the_packet_is_refused(key: PublicKey) -> None:
    body = raw_signature()[SIG_HEADER:] + b"\x00"
    header = bytes([0x89]) + len(body).to_bytes(2, "big")
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), armor(header + body), key)


def test_two_signature_packets_are_refused(key: PublicKey) -> None:
    raw = raw_signature()
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), armor(raw + raw), key)


def test_a_trailing_byte_after_the_packet_is_refused(key: PublicKey) -> None:
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), armor(raw_signature() + b"\x00"), key)


def test_a_packet_that_is_not_a_signature_is_refused(key: PublicKey) -> None:
    raw = bytearray(raw_signature())
    raw[0] = 0x80 | (6 << 2) | 1
    with pytest.raises(SignatureError):
        verify_detached(manifest("2.1.319"), armor(bytes(raw)), key)


def test_every_truncated_signature_raises_only_signature_error(key: PublicKey) -> None:
    raw = raw_signature()
    for length in range(len(raw)):
        with pytest.raises(SignatureError):
            verify_detached(manifest("2.1.319"), armor(raw[:length]), key)


def test_every_truncated_body_with_a_fixed_header_raises_only_signature_error(
    key: PublicKey,
) -> None:
    body = raw_signature()[SIG_HEADER:]
    for length in range(len(body)):
        header = bytes([0x89]) + length.to_bytes(2, "big")
        with pytest.raises(SignatureError):
            verify_detached(manifest("2.1.319"), armor(header + body[:length]), key)


def test_error_messages_do_not_repeat_the_input(key: PublicKey) -> None:
    marker = "orch-marker-7f3a"
    text = f"-----BEGIN PGP SIGNATURE-----\n{marker}\n\n{marker}\n"
    with pytest.raises(SignatureError) as caught:
        verify_detached(manifest("2.1.319"), text, key)
    assert marker not in str(caught.value)
    assert "\n" not in str(caught.value)


def test_the_module_uses_no_process_or_network_modules() -> None:
    tree = ast.parse((ROOT / "src" / "orch" / "signature.py").read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert not names & {"subprocess", "socket", "urllib", "http", "os", "orch"}
