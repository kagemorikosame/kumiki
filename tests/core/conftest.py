"""core のテストで共有する題材"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest

from sashimono.core.commands import AddClip, AddMedia, AddTrack, SetTranscript
from sashimono.core.model import (
    AnimatedValue,
    Effect,
    Interpolation,
    Keyframe,
    Marker,
    MediaItem,
    Project,
    Track,
    TrackKind,
    Transcript,
    Word,
)
from tests.conftest import make_clip


@pytest.fixture
def rich_project(
    project: Project, video_media: MediaItem, audio_media: MediaItem, transcript: Transcript
) -> Project:
    """ひととおりの要素が入ったプロジェクト 往復テストの対象"""
    result = AddMedia(audio_media).apply(project)
    result = SetTranscript(
        video_media.id,
        replace(
            transcript,
            segments=(
                replace(
                    transcript.segments[0],
                    words=(
                        Word(start=Fraction(1), end=Fraction(2), text="今日"),
                        Word(start=Fraction(2), end=Fraction(3), text="は"),
                    ),
                    speaker="話者A",
                ),
                *transcript.segments[1:],
            ),
        ),
    ).apply(result)

    audio_track = Track(kind=TrackKind.AUDIO, name="A1", volume_db=-3.5, pan=0.25)
    result = AddTrack(audio_track).apply(result)

    video_clip = replace(
        make_clip(0, 300, video_media),
        speed=Fraction(3, 2),
        effects=(
            Effect(
                kind="blur",
                params={
                    "radius": AnimatedValue(
                        keyframes=(
                            Keyframe(frame=0, value=0.0, interpolation=Interpolation.EASE_IN),
                            Keyframe(
                                frame=60,
                                value=24.0,
                                interpolation=Interpolation.BEZIER,
                                control_points=(0.1, 0.2, 0.3, 0.4),
                            ),
                            Keyframe(frame=120, value=4.0, interpolation=Interpolation.HOLD),
                        )
                    ),
                    "color": (1.0, 0.5, 0.25, 1.0),
                    "invert": True,
                    "quality": 3,
                    "label": "ぼかし",
                },
            ),
        ),
        opacity=AnimatedValue(
            keyframes=(Keyframe(frame=0, value=0.0), Keyframe(frame=30, value=1.0))
        ),
    )
    result = AddClip(result.timeline.tracks[0].id, video_clip).apply(result)
    result = AddClip(audio_track.id, make_clip(0, 900, audio_media)).apply(result)

    timeline = replace(
        result.timeline,
        markers=(Marker(frame=120, label="ここから本編", color="#ff0000"),),
        work_area=(30, 600),
    )
    return result.with_timeline(timeline).renamed("配信回_07")
