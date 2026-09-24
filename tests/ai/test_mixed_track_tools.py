"""AI から混合トラック（YMM4 型のレイヤー）を足して見る

UI にだけ入口を付けると「画面ではできるのに AI に頼むとできない」になる
"""

from __future__ import annotations

import pytest

from sashimono.ai.host import ToolError
from sashimono.core.commands import AddClip, RemoveTrack
from sashimono.core.model import Clip, TrackKind
from tests.ai.conftest import FakeHost
from tests.ai.test_operations import run


def test_a_layer_is_added_with_a_ymm4_name(host: FakeHost) -> None:
    # video か audio しか受けないと、AI は混合の作品にレイヤーを足せない
    result = run(host, "add_track", kind="mixed")
    track = host.document.project.timeline.tracks[-1]
    assert track.kind is TrackKind.MIXED
    assert result["name"] == track.name == "レイヤー 1"


def test_a_removed_layer_does_not_cause_a_twin(host: FakeHost) -> None:
    # 本数だけで数えると、消した後にもう 1 本の「レイヤー 2」ができる
    run(host, "add_track", kind="mixed")
    run(host, "add_track", kind="mixed")
    first = next(t for t in host.document.project.timeline.tracks if t.name == "レイヤー 1")
    host.apply_commands([RemoveTrack(first.id)], "消す")
    run(host, "add_track", kind="mixed")
    names = [t.name for t in host.document.project.timeline.tracks if t.kind is TrackKind.MIXED]
    assert sorted(names) == ["レイヤー 2", "レイヤー 3"]


def test_the_list_says_mixed(host: FakeHost) -> None:
    run(host, "add_track", kind="mixed")
    kinds = [entry["kind"] for entry in run(host, "list_tracks")]
    assert kinds[-1] == "mixed"


def test_an_unknown_kind_names_the_choices(host: FakeHost) -> None:
    with pytest.raises(ToolError, match="mixed"):
        run(host, "add_track", kind="effect")


def test_layer_clips_show_what_they_play(host: FakeHost) -> None:
    # 絵と音のどちらを出すかが見えないと、AI は音の消えた動画を直せない
    run(host, "add_track", kind="mixed")
    project = host.document.project
    layer = project.timeline.tracks[-1]
    media = project.media[0]
    clip = Clip(0, 30, media_id=media.id, audio_stream=1, show_picture=False)
    host.apply_commands([AddClip(layer.id, clip)], "置く")
    listed = next(c for c in run(host, "list_clips") if c["track_kind"] == "mixed")
    assert listed["audio_stream"] == 1
    assert listed["show_picture"] is False
    others = [c for c in run(host, "list_clips") if c["track_kind"] != "mixed"]
    assert all("audio_stream" not in c for c in others)
