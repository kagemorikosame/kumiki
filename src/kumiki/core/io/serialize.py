""".kmk プロジェクトファイルの読み書き

JSON にしているのは、外部ツールと AI エージェントから素直に扱えるようにするため
バイナリにすると、AI がプロジェクトを直接読んで状況を把握することも、ユーザーが
壊れたファイルを手で直すこともできなくなる

秒は必ず ``"1001/30000"`` のような分数文字列で書き出す 浮動小数にすると保存と
読み込みを繰り返すだけで値が動く
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

from kumiki.core.model import (
    AnimatedValue,
    AudioStreamInfo,
    Clip,
    ClipId,
    Effect,
    EffectId,
    GeneratedSource,
    GroupId,
    Interpolation,
    Keyframe,
    Marker,
    MediaId,
    MediaItem,
    ParamValue,
    Project,
    ProjectSettings,
    Scene,
    SceneId,
    SegmentId,
    Timeline,
    Track,
    TrackId,
    TrackKind,
    Transcript,
    TranscriptSegment,
    VideoStreamInfo,
    Word,
)
from kumiki.core.timebase import FrameRate

__all__ = [
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "LEGACY_SUFFIXES",
    "ProjectFileError",
    "effect_from_json",
    "effect_to_json",
    "load_project",
    "project_from_dict",
    "project_to_dict",
    "save_project",
    "source_from_json",
    "source_to_json",
]

FORMAT_NAME = "kumiki-project"
#: 2 でシーン（``scenes`` と ``Clip.scene_id``）とグループ（``Clip.group_id``）を足した
#: 1 の本体は 2 を開くと「更新してください」と言う（シーンを黙って捨てて開くと、
#: 置いたシーンが何も映らない穴になり、保存し直すとシーンごと消える）
FORMAT_VERSION = 2

#: プロジェクトファイルの拡張子
SUFFIX = ".kmk"

#: 読むときだけ受け付ける、昔の名前と拡張子
#:
#: 公開前に ``NovaEdit`` から改名した 手元に保存済みのものがあるかもしれないので、
#: **読む側だけ**受ける 書くときは常に新しい名前で書く
LEGACY_FORMAT_NAMES = ("novaedit-project",)
LEGACY_SUFFIXES = (".nvep",)


class ProjectFileError(Exception):
    """プロジェクトファイルが読めない、または想定した形をしていない"""


# --- 基本型 ---------------------------------------------------------------


def _fraction_to_json(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _fraction_from_json(value: object, field: str) -> Fraction:
    if isinstance(value, str):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise ProjectFileError(f"{field} が分数として読めない: {value!r}") from exc
    if isinstance(value, int):
        return Fraction(value)
    raise ProjectFileError(f"{field} が分数ではない: {value!r}")


def _rate_to_json(rate: FrameRate) -> str:
    return f"{rate.num}/{rate.den}"


def _rate_from_json(value: object, field: str) -> FrameRate:
    fraction = _fraction_from_json(value, field)
    return FrameRate(fraction.numerator, fraction.denominator)


def _require(data: object, field: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ProjectFileError(f"{field} がオブジェクトではない")
    return data


def _get_int(data: dict[str, Any], key: str, default: int | None = None) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProjectFileError(f"{key} が整数ではない: {value!r}")
    return value


def _get_str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ProjectFileError(f"{key} が文字列ではない: {value!r}")
    return value


def _get_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ProjectFileError(f"{key} が真偽値ではない: {value!r}")
    return value


def _get_float(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProjectFileError(f"{key} が数値ではない: {value!r}")
    return float(value)


def _get_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ProjectFileError(f"{key} が配列ではない: {value!r}")
    return value


# --- エフェクト -----------------------------------------------------------


def _keyframe_to_json(keyframe: Keyframe) -> dict[str, Any]:
    data: dict[str, Any] = {
        "frame": keyframe.frame,
        "value": keyframe.value,
        "interpolation": keyframe.interpolation.value,
    }
    if keyframe.control_points is not None:
        data["control_points"] = list(keyframe.control_points)
    return data


def _keyframe_from_json(raw: object) -> Keyframe:
    data = _require(raw, "keyframe")
    name = _get_str(data, "interpolation", Interpolation.LINEAR.value)
    try:
        interpolation = Interpolation(name)
    except ValueError as exc:
        raise ProjectFileError(f"未知の補間方法: {name!r}") from exc

    points_raw = data.get("control_points")
    control_points: tuple[float, float, float, float] | None = None
    if points_raw is not None:
        if not isinstance(points_raw, list) or len(points_raw) != 4:
            raise ProjectFileError(f"control_points は 4 要素の配列: {points_raw!r}")
        a, b, c, d = (float(v) for v in points_raw)
        control_points = (a, b, c, d)

    return Keyframe(
        frame=_get_int(data, "frame"),
        value=_get_float(data, "value", 0.0),
        interpolation=interpolation,
        control_points=control_points,
    )


def _param_to_json(value: ParamValue) -> Any:
    if isinstance(value, AnimatedValue):
        # アニメーションしていない値は素の数値で書く プロジェクトファイルの
        # 大半はこちらなので、これだけでファイルサイズがかなり変わる
        if not value.is_animated:
            return {"static": value.static}
        return {
            "static": value.static,
            "keyframes": [_keyframe_to_json(k) for k in value.keyframes],
        }
    if isinstance(value, tuple):
        return list(value)
    return value


def _param_from_json(raw: object) -> ParamValue:
    if isinstance(raw, dict):
        keyframes = tuple(_keyframe_from_json(k) for k in _get_list(raw, "keyframes"))
        return AnimatedValue(static=_get_float(raw, "static", 0.0), keyframes=keyframes)
    if isinstance(raw, list):
        return tuple(float(v) for v in raw)
    if isinstance(raw, bool | int | str):
        return raw
    raise ProjectFileError(f"パラメータとして読めない値: {raw!r}")


def _params_to_json(params: dict[str, ParamValue]) -> dict[str, Any]:
    return {name: _param_to_json(value) for name, value in params.items()}


def _params_from_json(raw: object, field: str) -> dict[str, ParamValue]:
    if not isinstance(raw, dict):
        raise ProjectFileError(f"{field} がオブジェクトではない: {raw!r}")
    return {name: _param_from_json(value) for name, value in raw.items()}


def effect_to_json(effect: Effect) -> dict[str, Any]:
    """エフェクト 1 つを辞書へ プリセットの保存でも使う"""
    return {
        "id": effect.id,
        "kind": effect.kind,
        "enabled": effect.enabled,
        "params": _params_to_json(effect.params),
    }


def effect_from_json(raw: object) -> Effect:
    """:func:`effect_to_json` の逆"""
    data = _require(raw, "effect")
    return Effect(
        kind=_get_str(data, "kind"),
        params=_params_from_json(data.get("params", {}), "params"),
        enabled=_get_bool(data, "enabled", True),
        id=EffectId(_get_str(data, "id")),
    )


def source_to_json(source: GeneratedSource) -> dict[str, Any]:
    return {"kind": source.kind, "params": _params_to_json(source.params)}


def source_from_json(raw: object) -> GeneratedSource:
    data = _require(raw, "source")
    return GeneratedSource(
        kind=_get_str(data, "kind"),
        params=_params_from_json(data.get("params", {}), "source.params"),
    )


# --- 字幕 -----------------------------------------------------------------


def _transcript_to_json(transcript: Transcript) -> dict[str, Any]:
    return {
        "language": transcript.language,
        "model": transcript.model,
        "segments": [
            {
                "id": segment.id,
                "start": _fraction_to_json(segment.start),
                "end": _fraction_to_json(segment.end),
                "text": segment.text,
                "speaker": segment.speaker,
                "edited": segment.edited,
                "words": [
                    {
                        "start": _fraction_to_json(word.start),
                        "end": _fraction_to_json(word.end),
                        "text": word.text,
                    }
                    for word in segment.words
                ],
            }
            for segment in transcript.segments
        ],
    }


def _transcript_from_json(raw: object) -> Transcript:
    data = _require(raw, "transcript")
    segments = []
    for entry in _get_list(data, "segments"):
        segment_data = _require(entry, "segment")
        speaker = segment_data.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise ProjectFileError(f"speaker が文字列ではない: {speaker!r}")
        words = tuple(
            Word(
                start=_fraction_from_json(_require(w, "word")["start"], "word.start"),
                end=_fraction_from_json(_require(w, "word")["end"], "word.end"),
                text=_get_str(_require(w, "word"), "text"),
            )
            for w in _get_list(segment_data, "words")
        )
        segments.append(
            TranscriptSegment(
                start=_fraction_from_json(segment_data.get("start"), "segment.start"),
                end=_fraction_from_json(segment_data.get("end"), "segment.end"),
                text=_get_str(segment_data, "text"),
                words=words,
                speaker=speaker,
                edited=_get_bool(segment_data, "edited", False),
                id=SegmentId(_get_str(segment_data, "id")),
            )
        )
    return Transcript(
        segments=tuple(segments),
        language=_get_str(data, "language"),
        model=_get_str(data, "model"),
    )


# --- 素材 -----------------------------------------------------------------


def _media_to_json(item: MediaItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "path": str(item.path),
        "duration": _fraction_to_json(item.duration),
        "display_name": item.display_name,
        "video_streams": [
            {
                "index": stream.index,
                "width": stream.width,
                "height": stream.height,
                "frame_rate": _rate_to_json(stream.frame_rate),
                "time_base": _fraction_to_json(stream.time_base),
                "codec": stream.codec,
                "pixel_format": stream.pixel_format,
                "rotation": stream.rotation,
            }
            for stream in item.video_streams
        ],
        "audio_streams": [
            {
                "index": stream.index,
                "sample_rate": stream.sample_rate,
                "channels": stream.channels,
                "time_base": _fraction_to_json(stream.time_base),
                "codec": stream.codec,
                "language": stream.language,
            }
            for stream in item.audio_streams
        ],
        "transcript": (
            _transcript_to_json(item.transcript) if item.transcript is not None else None
        ),
    }


def _media_from_json(raw: object) -> MediaItem:
    data = _require(raw, "media")
    video_streams = []
    for s in _get_list(data, "video_streams"):
        stream_data = _require(s, "video_stream")
        video_streams.append(
            VideoStreamInfo(
                index=_get_int(stream_data, "index"),
                width=_get_int(stream_data, "width"),
                height=_get_int(stream_data, "height"),
                frame_rate=_rate_from_json(stream_data.get("frame_rate"), "frame_rate"),
                time_base=_fraction_from_json(stream_data.get("time_base"), "time_base"),
                codec=_get_str(stream_data, "codec"),
                pixel_format=_get_str(stream_data, "pixel_format"),
                rotation=_get_int(stream_data, "rotation", 0),
            )
        )

    audio_streams = []
    for s in _get_list(data, "audio_streams"):
        stream_data = _require(s, "audio_stream")
        language = stream_data.get("language")
        if language is not None and not isinstance(language, str):
            raise ProjectFileError(f"language が文字列ではない: {language!r}")
        audio_streams.append(
            AudioStreamInfo(
                index=_get_int(stream_data, "index"),
                sample_rate=_get_int(stream_data, "sample_rate"),
                channels=_get_int(stream_data, "channels"),
                time_base=_fraction_from_json(stream_data.get("time_base"), "time_base"),
                codec=_get_str(stream_data, "codec"),
                language=language,
            )
        )

    transcript_raw = data.get("transcript")
    return MediaItem(
        path=Path(_get_str(data, "path")),
        duration=_fraction_from_json(data.get("duration", 0), "duration"),
        video_streams=tuple(video_streams),
        audio_streams=tuple(audio_streams),
        transcript=_transcript_from_json(transcript_raw) if transcript_raw is not None else None,
        display_name=_get_str(data, "display_name"),
        id=MediaId(_get_str(data, "id")),
    )


# --- タイムライン ---------------------------------------------------------


def _clip_to_json(clip: Clip) -> dict[str, Any]:
    return {
        "id": clip.id,
        "timeline_start": clip.timeline_start,
        "duration": clip.duration,
        "media_id": clip.media_id,
        "source": source_to_json(clip.source) if clip.source is not None else None,
        "source_in": _fraction_to_json(clip.source_in),
        "stream_index": clip.stream_index,
        "speed": _fraction_to_json(clip.speed),
        "opacity": _param_to_json(clip.opacity),
        "blend_mode": clip.blend_mode,
        "link_group": clip.link_group,
        "scene_id": clip.scene_id,
        "group_id": clip.group_id,
        "enabled": clip.enabled,
        "effects": [effect_to_json(e) for e in clip.effects],
    }


def _clip_from_json(raw: object) -> Clip:
    data = _require(raw, "clip")
    media_id = data.get("media_id")
    if media_id is not None and not isinstance(media_id, str):
        raise ProjectFileError(f"media_id が文字列ではない: {media_id!r}")
    link_group = data.get("link_group")
    if link_group is not None and not isinstance(link_group, str):
        raise ProjectFileError(f"link_group が文字列ではない: {link_group!r}")
    scene_id = data.get("scene_id")
    if scene_id is not None and not isinstance(scene_id, str):
        raise ProjectFileError(f"scene_id が文字列ではない: {scene_id!r}")
    group_id = data.get("group_id")
    if group_id is not None and not isinstance(group_id, str):
        raise ProjectFileError(f"group_id が文字列ではない: {group_id!r}")

    opacity = _param_from_json(data.get("opacity", {"static": 1.0}))
    if not isinstance(opacity, AnimatedValue):
        raise ProjectFileError(f"opacity がアニメーション値ではない: {opacity!r}")

    source_raw = data.get("source")
    return Clip(
        timeline_start=_get_int(data, "timeline_start"),
        duration=_get_int(data, "duration"),
        media_id=MediaId(media_id) if media_id is not None else None,
        source=source_from_json(source_raw) if source_raw is not None else None,
        source_in=_fraction_from_json(data.get("source_in", 0), "source_in"),
        stream_index=_get_int(data, "stream_index", 0),
        speed=_fraction_from_json(data.get("speed", 1), "speed"),
        effects=tuple(effect_from_json(e) for e in _get_list(data, "effects")),
        opacity=opacity,
        blend_mode=_get_str(data, "blend_mode", "normal"),
        link_group=GroupId(link_group) if link_group is not None else None,
        scene_id=SceneId(scene_id) if scene_id is not None else None,
        group_id=GroupId(group_id) if group_id is not None else None,
        enabled=_get_bool(data, "enabled", True),
        id=ClipId(_get_str(data, "id")),
    )


def _track_to_json(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "kind": track.kind.value,
        "name": track.name,
        "locked": track.locked,
        "muted": track.muted,
        "solo": track.solo,
        "height": track.height,
        "volume_db": track.volume_db,
        "pan": track.pan,
        "effects": [effect_to_json(e) for e in track.effects],
        "clips": [_clip_to_json(c) for c in track.clips],
    }


def _track_from_json(raw: object) -> Track:
    data = _require(raw, "track")
    kind_name = _get_str(data, "kind")
    try:
        kind = TrackKind(kind_name)
    except ValueError as exc:
        raise ProjectFileError(f"未知のトラック種別: {kind_name!r}") from exc

    return Track(
        kind=kind,
        name=_get_str(data, "name"),
        clips=tuple(_clip_from_json(c) for c in _get_list(data, "clips")),
        effects=tuple(effect_from_json(e) for e in _get_list(data, "effects")),
        locked=_get_bool(data, "locked", False),
        muted=_get_bool(data, "muted", False),
        solo=_get_bool(data, "solo", False),
        height=_get_int(data, "height", 60),
        volume_db=_get_float(data, "volume_db", 0.0),
        pan=_get_float(data, "pan", 0.0),
        id=TrackId(_get_str(data, "id")),
    )


def _timeline_to_json(timeline: Timeline) -> dict[str, Any]:
    return {
        "rate": _rate_to_json(timeline.rate),
        "tracks": [_track_to_json(t) for t in timeline.tracks],
        "markers": [
            {"frame": m.frame, "label": m.label, "color": m.color} for m in timeline.markers
        ],
        "work_area": list(timeline.work_area) if timeline.work_area is not None else None,
    }


def _timeline_from_json(raw: object) -> Timeline:
    data = _require(raw, "timeline")
    work_area_raw = data.get("work_area")
    work_area: tuple[int, int] | None = None
    if work_area_raw is not None:
        if not isinstance(work_area_raw, list) or len(work_area_raw) != 2:
            raise ProjectFileError(f"work_area は 2 要素の配列: {work_area_raw!r}")
        work_area = (int(work_area_raw[0]), int(work_area_raw[1]))

    markers = tuple(
        Marker(
            frame=_get_int(_require(m, "marker"), "frame"),
            label=_get_str(_require(m, "marker"), "label"),
            color=_get_str(_require(m, "marker"), "color", "#ffcc00"),
        )
        for m in _get_list(data, "markers")
    )
    return Timeline(
        rate=_rate_from_json(data.get("rate"), "rate"),
        tracks=tuple(_track_from_json(t) for t in _get_list(data, "tracks")),
        markers=markers,
        work_area=work_area,
    )


# --- プロジェクト ---------------------------------------------------------


def project_to_dict(project: Project) -> dict[str, Any]:
    """プロジェクトを JSON にできる辞書へ"""
    settings = project.settings
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "name": project.name,
        "settings": {
            "width": settings.width,
            "height": settings.height,
            "frame_rate": _rate_to_json(settings.frame_rate),
            "sample_rate": settings.sample_rate,
            "channels": settings.channels,
            "color_space": settings.color_space,
        },
        "media": [_media_to_json(m) for m in project.media],
        "timeline": _timeline_to_json(project.timeline),
        "scenes": [
            {"id": scene.id, "name": scene.name, "timeline": _timeline_to_json(scene.timeline)}
            for scene in project.scenes
        ],
    }


def project_from_dict(data: object) -> Project:
    """:func:`project_to_dict` の出力からプロジェクトを復元する"""
    root = _require(data, "プロジェクト")

    format_name = _get_str(root, "format")
    if format_name not in (FORMAT_NAME, *LEGACY_FORMAT_NAMES):
        raise ProjectFileError(f"Kumiki のプロジェクトファイルではない: format={format_name!r}")
    version = _get_int(root, "version", 0)
    if version > FORMAT_VERSION:
        # 壊れているのではなく、こちらが古い 直す手立てを言う
        # 自動更新を入れたあとは、ここが「更新してください」の入口になる
        raise ProjectFileError(
            f"新しい形式のプロジェクトファイル (version {version})"
            f"このバージョンが対応しているのは {FORMAT_VERSION} までです"
            "Kumiki を新しい版に更新してください"
        )

    settings_data = _require(root.get("settings"), "settings")
    settings = ProjectSettings(
        width=_get_int(settings_data, "width", 1920),
        height=_get_int(settings_data, "height", 1080),
        frame_rate=_rate_from_json(settings_data.get("frame_rate", "30/1"), "frame_rate"),
        sample_rate=_get_int(settings_data, "sample_rate", 48000),
        channels=_get_int(settings_data, "channels", 2),
        color_space=_get_str(settings_data, "color_space", "rec709"),
    )

    timeline = _timeline_from_json(root.get("timeline", {"rate": "30/1"}))
    if timeline.rate != settings.frame_rate:
        raise ProjectFileError(
            "タイムラインとプロジェクト設定のフレームレートが食い違っている: "
            f"{timeline.rate} と {settings.frame_rate}"
        )

    scenes = tuple(_scene_from_json(raw) for raw in _get_list(root, "scenes"))
    try:
        return Project(
            settings=settings,
            timeline=timeline,
            media=tuple(_media_from_json(m) for m in _get_list(root, "media")),
            name=_get_str(root, "name", "無題"),
            scenes=scenes,
        )
    except ValueError as exc:
        # シーンの入れ子が自分へ戻っている、など 壊れたファイルとして伝える
        raise ProjectFileError(str(exc)) from exc


def _scene_from_json(raw: object) -> Scene:
    data = _require(raw, "scene")
    return Scene(
        name=_get_str(data, "name", "シーン"),
        timeline=_timeline_from_json(data.get("timeline", {"rate": "30/1"})),
        id=SceneId(_get_str(data, "id")),
    )


def save_project(project: Project, path: Path) -> None:
    """プロジェクトをファイルへ書き出す

    一時ファイルへ書いてから差し替える 書き込み中に落ちても、既存のプロジェクト
    ファイルは無傷で残る 編集作業をまるごと失うのが一番痛い失敗なので、
    ここは常に atomic にする
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    payload = json.dumps(project_to_dict(project), ensure_ascii=False, indent=2)

    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def load_project(path: Path) -> Project:
    """プロジェクトファイルを読み込む"""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectFileError(f"プロジェクトファイルを開けない: {path}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectFileError(f"JSON として読めない: {path} ({exc})") from exc

    project = project_from_dict(data)
    # ファイル名をプロジェクト名の既定にする 名前が入っていない古いファイル対策
    if not project.name or project.name == "無題":
        project = replace(project, name=path.stem)
    return project
