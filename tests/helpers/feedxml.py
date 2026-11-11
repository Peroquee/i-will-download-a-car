"""YouTube RSS feed XML for the tests (issue #77): the channel id, one entry and a whole feed."""

from __future__ import annotations

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
