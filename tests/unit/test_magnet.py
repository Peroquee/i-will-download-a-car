"""app/ytp/magnet.py: only a single, clean magnet link with one v1 infohash gets through."""

from __future__ import annotations

import base64

import pytest

from ytp.magnet import MAX_LENGTH, InvalidMagnet, infohash_from_magnet

HEX = "0123456789abcdef0123456789abcdef01234567"
BASE32 = base64.b32encode(bytes.fromhex(HEX)).decode()


def test_hex_infohash_is_returned_lower_case():
    assert infohash_from_magnet(f"magnet:?xt=urn:btih:{HEX.upper()}") == HEX


def test_base32_infohash_is_converted_to_hex():
    assert len(BASE32) == 32
    assert infohash_from_magnet(f"magnet:?xt=urn:btih:{BASE32}") == HEX
    assert infohash_from_magnet(f"magnet:?xt=urn:btih:{BASE32.lower()}") == HEX


def test_name_and_trackers_are_fine():
    magnet = (
        f"magnet:?xt=urn:btih:{HEX}&dn=Gr%C3%B6%C3%9Fte%20H%C3%B6rb%C3%BCcher"
        "&tr=udp%3A%2F%2Ftracker.example%3A1337%2Fannounce&tr=http%3A%2F%2Fa.example%2Fannounce"
    )
    assert infohash_from_magnet(magnet) == HEX


def test_hybrid_link_with_a_v2_hash_too_is_accepted():
    magnet = f"magnet:?xt=urn:btih:{HEX}&xt=urn:btmh:1220{'ab' * 32}"
    assert infohash_from_magnet(magnet) == HEX


def test_the_same_hash_twice_is_one_torrent():
    assert infohash_from_magnet(f"magnet:?xt=urn:btih:{HEX}&xt=urn:btih:{HEX.upper()}") == HEX


def test_scheme_and_parameter_names_are_case_insensitive():
    assert infohash_from_magnet(f"MAGNET:?XT=URN:BTIH:{HEX}") == HEX


@pytest.mark.parametrize(
    ("magnet", "message"),
    [
        ("", "empty"),
        (None, "empty"),
        (123, "empty"),
        ("http://example.com/a.torrent", "start with magnet"),
        ("magnet:", "start with magnet"),
        ("magnet:?", "start with magnet"),
        (f"xmagnet:?xt=urn:btih:{HEX}", "start with magnet"),
        ("magnet:?dn=only-a-name", "no BitTorrent infohash"),
        (f"magnet:?xt=urn:btmh:1220{'ab' * 32}", "no BitTorrent infohash"),
        (f"magnet:?xt=urn:sha1:{HEX}", "no BitTorrent infohash"),
        (f"magnet:?xt=urn:btih:{HEX}&xt=urn:btih:{'f' * 40}", "more than one"),
        ("magnet:?xt=urn:btih:", "40 hex or 32 base32"),
        (f"magnet:?xt=urn:btih:{HEX[:-1]}", "40 hex or 32 base32"),
        (f"magnet:?xt=urn:btih:{HEX}0", "40 hex or 32 base32"),
        (f"magnet:?xt=urn:btih:{'g' * 40}", "40 hex or 32 base32"),
        (f"magnet:?xt=urn:btih:{'1' * 32}", "40 hex or 32 base32"),  # 1 is not base32
    ],
)
def test_things_that_are_not_acceptable_magnets(magnet, message):
    with pytest.raises(InvalidMagnet, match=message):
        infohash_from_magnet(magnet)


@pytest.mark.security
@pytest.mark.parametrize(
    "suffix",
    [
        "\nhttp://169.254.169.254/latest/meta-data",  # a second URL qBittorrent would fetch
        "\r\nhttp://evil.example/x.torrent",
        "\n",
        " ",
        "\t",
        "\x00",
        "\x85",  # NEXT LINE
        "\u2028",
        "\u00a0",
        "\x7f",
    ],
)
def test_anything_that_could_smuggle_a_second_line_is_refused(suffix):
    with pytest.raises(InvalidMagnet, match="single line"):
        infohash_from_magnet(f"magnet:?xt=urn:btih:{HEX}{suffix}")


def test_leading_whitespace_is_refused_callers_strip_first():
    with pytest.raises(InvalidMagnet):
        infohash_from_magnet(f" magnet:?xt=urn:btih:{HEX}")


def test_length_limit():
    ok = f"magnet:?xt=urn:btih:{HEX}&dn=" + "a" * (
        MAX_LENGTH - len(f"magnet:?xt=urn:btih:{HEX}&dn=")
    )
    assert len(ok) == MAX_LENGTH
    assert infohash_from_magnet(ok) == HEX
    with pytest.raises(InvalidMagnet, match="longer than"):
        infohash_from_magnet(ok + "a")


def test_invalid_magnet_is_a_value_error():
    assert issubclass(InvalidMagnet, ValueError)
