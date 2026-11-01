"""The channel RSS feed (issue #20): reading it safely and fetching it."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from ytp.feeds import MAX_FEED_BYTES, FeedClient, FeedError, parse_feed, parse_time

CID = "UCSMOQeBJ2RAnuFungnQOxLg"


def entry(video_id, title, published):
    return f"""<entry>
  <id>yt:video:{video_id}</id>
  <yt:videoId>{video_id}</yt:videoId>
  <yt:channelId>{CID}</yt:channelId>
  <title>{title}</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v={video_id}"/>
  <author><name>Blender</name></author>
  <published>{published}</published>
  <updated>{published}</updated>
</entry>"""


def feed(*entries, channel=CID, extra=""):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        'xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">\n'
        f"{extra}<yt:channelId>{channel.removeprefix('UC')}</yt:channelId><title>Blender</title>\n"
        + "\n".join(entries)
        + "\n</feed>"
    ).encode()


NEWER = entry("aaaaaaaaaaa", "Newer &amp; better", "2026-10-31T12:00:00+00:00")
OLDER = entry("bbbbbbbbbbb", "Older", "2026-10-01T08:30:00+00:00")


def test_a_feed_gives_its_videos_newest_first():
    got = parse_feed(feed(OLDER, NEWER), CID)
    assert [e.video_id for e in got] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert got[0].title == "Newer & better"
    assert got[0].published == datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def test_an_empty_feed_is_fine():
    assert parse_feed(feed(), CID) == []


def test_odd_entries_are_skipped_not_fatal():
    bad_id = entry("short", "x", "2026-10-31T12:00:00+00:00")
    bad_time = entry("ccccccccccc", "x", "yesterday")
    no_title = entry("ddddddddddd", "", "2026-10-30T12:00:00+00:00")
    got = parse_feed(feed(bad_id, bad_time, NEWER, no_title), CID)
    assert [e.video_id for e in got] == ["aaaaaaaaaaa", "ddddddddddd"]
    assert got[1].title == ""


def test_titles_have_their_whitespace_normalised():
    got = parse_feed(feed(entry("aaaaaaaaaaa", "  a \n b\t c ", "2026-10-31T12:00:00Z")), CID)
    assert got[0].title == "a b c"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-31T12:00:00+00:00", datetime(2026, 10, 31, 12, tzinfo=UTC)),
        ("2026-10-31T12:00:00Z", datetime(2026, 10, 31, 12, tzinfo=UTC)),
        ("2026-10-31T14:00:00+02:00", datetime(2026, 10, 31, 12, tzinfo=UTC)),
        ("2026-10-31T12:00:00", datetime(2026, 10, 31, 12, tzinfo=UTC)),
        ("2026-11-01T11:17:21.485Z", datetime(2026, 11, 1, 11, 17, 21, 485000, tzinfo=UTC)),
        ("nonsense", None),
        ("", None),
    ],
)
def test_times(text, expected):
    assert parse_time(text) == expected


@pytest.mark.security
def test_a_billion_laughs_feed_is_refused_before_it_is_parsed():
    laughs = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
        b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&lol3;</title></feed>'
    )
    with pytest.raises(FeedError) as caught:
        parse_feed(laughs, CID)
    assert caught.value.kind == "bad_feed"
    assert "DTD" in caught.value.message


@pytest.mark.security
@pytest.mark.parametrize(
    "payload",
    [
        b'<?xml version="1.0"?><!DOCTYPE feed SYSTEM "http://evil.example/x.dtd"><feed/>',
        b'<?xml version="1.0"?><!doctype feed><feed/>',
        b'<?xml version="1.0"?><!  DOCTYPE feed><feed/>',
        b'<?xml version="1.0"?><!ENTITY x "y"><feed/>',
        b'<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&xxe;</title></feed>',
    ],
)
def test_any_doctype_or_entity_declaration_is_refused(payload):
    with pytest.raises(FeedError) as caught:
        parse_feed(payload, CID)
    assert caught.value.kind == "bad_feed"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not xml at all",
        b"<feed xmlns='http://www.w3.org/2005/Atom'>",
        b"<html><body>hi</body></html>",
        b"<feed><entry/></feed>",  # right name, wrong namespace
        b"\x00\x01\x02",
    ],
)
def test_garbage_is_a_bad_feed(payload):
    with pytest.raises(FeedError) as caught:
        parse_feed(payload, CID)
    assert caught.value.kind == "bad_feed"


def test_the_feed_level_channel_id_may_come_with_or_without_the_uc_prefix():
    """The real feed says `<yt:channelId>SMOQ...</yt:channelId>` (no UC) at the top."""
    assert parse_feed(feed(NEWER).replace(CID[2:].encode(), CID.encode(), 1), CID)
    assert parse_feed(feed(NEWER), CID)


def test_a_feed_of_another_channel_is_refused():
    with pytest.raises(FeedError) as caught:
        parse_feed(feed(NEWER, channel="UC" + "x" * 22), CID)
    assert "different channel" in caught.value.message


def test_an_oversized_feed_is_refused():
    with pytest.raises(FeedError):
        parse_feed(b"<feed>" + b" " * MAX_FEED_BYTES + b"</feed>", CID)


def test_only_the_newest_entries_are_read():
    many = [entry(f"{i:011d}", f"t{i}", "2026-10-31T12:00:00Z") for i in range(80)]
    assert len(parse_feed(feed(*many), CID)) == 50


# --- fetching ---


class Server:
    """A local HTTP server that answers every request with what the test says."""

    def __init__(self):
        self.status = 200
        self.body = b""
        self.headers: dict[str, str] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server.requests.append((self.path, dict(self.headers)))
                self.send_response(server.status)
                for key, value in server.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(server.body)))
                self.end_headers()
                self.wfile.write(server.body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[Server]:
    s = Server()
    yield s
    s.stop()


def test_fetch_asks_for_the_feed_of_the_channel(server):
    server.body = feed(NEWER)
    data = FeedClient(base=server.url).fetch(CID)
    assert data == server.body
    path, headers = server.requests[0]
    assert path == f"/feeds/videos.xml?channel_id={CID}"
    assert headers["User-Agent"].startswith("youtube-premium/")


@pytest.mark.parametrize(
    ("status", "kind"),
    [(404, "not_found"), (429, "temporary"), (500, "temporary"), (503, "temporary"),
     (403, "bad_feed"), (302, "bad_feed"), (204, "bad_feed")],
)  # fmt: skip
def test_fetch_status_codes(server, status, kind):
    server.status = status
    server.headers = {"Location": "http://evil.example/"} if status == 302 else {}
    with pytest.raises(FeedError) as caught:
        FeedClient(base=server.url).fetch(CID)
    assert caught.value.kind == kind
    assert len(server.requests) == 1, "a redirect is not followed"


def test_a_connection_failure_is_a_network_error(server):
    url = server.url
    server.stop()
    with pytest.raises(FeedError) as caught:
        FeedClient(base=url, timeout=2).fetch(CID)
    assert caught.value.kind == "network"
    assert "VPN" in caught.value.message


def test_a_slow_server_times_out_as_a_network_error():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)  # accepts the connection but never answers
    try:
        client = FeedClient(base=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=0.3)
        with pytest.raises(FeedError) as caught:
            client.fetch(CID)
        assert caught.value.kind == "network"
    finally:
        sock.close()


def test_an_oversized_answer_is_not_read_to_the_end(server):
    server.body = b"x" * (MAX_FEED_BYTES + 5000)
    data = FeedClient(base=server.url).fetch(CID)
    assert len(data) == MAX_FEED_BYTES + 1, "read one byte past the limit, then stopped"
    with pytest.raises(FeedError):
        parse_feed(data, CID)


@pytest.mark.security
@pytest.mark.parametrize("bad", ["", "x", CID + "&evil=1", "../etc", "UC" + "a" * 21, CID + "\n"])
def test_the_channel_id_is_checked_before_it_goes_into_a_url(server, bad):
    with pytest.raises(FeedError):
        FeedClient(base=server.url).fetch(bad)
    assert server.requests == []


def test_the_client_repr_does_not_show_the_proxy():
    assert "gluetun" not in repr(FeedClient("http://gluetun:8888"))
