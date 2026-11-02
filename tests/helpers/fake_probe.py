"""A stand-in for `YoutubeProbe.traits_blocking`: answers from a table, no network."""

from __future__ import annotations

from ytp.video_filter import VideoTraits
from ytp.youtube import ProbeError, VideoRef

ORDINARY = VideoTraits(live_status="not_live", duration=600, width=1920, height=1080)


class FakeTraitsProbe:
    def __init__(self, answers: dict[str, VideoTraits | ProbeError] | None = None):
        self.answers = answers or {}
        self.default: VideoTraits | ProbeError = ORDINARY
        self.asked: list[str] = []

    def traits_blocking(self, ref: VideoRef) -> VideoTraits:
        self.asked.append(ref.video_id)
        answer = self.answers.get(ref.video_id, self.default)
        if isinstance(answer, ProbeError):
            raise answer
        return answer
