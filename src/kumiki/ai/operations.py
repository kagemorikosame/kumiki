"""AI に見せる編集操作。

読み取りと変更をはっきり分けてある（:attr:`Operation.writes`）。変更系だけに確認を
挟めるようにするためで、この区別が無いと「全部確認する」か「何も確認しない」かの
どちらかになる。

ここは Qt も MCP も知らない。素の関数として書いてあるので、テストではホストを
偽物に差し替えるだけで全部のツールを試せる。MCP のツールに変換するのは
:mod:`kumiki.ai.server` の仕事。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from kumiki.ai.host import EditorHost, ToolError
from kumiki.core.commands import (
    AddEffect,
    AddTrack,
    Command,
    MoveClip,
    ParamPath,
    ParamTarget,
    RemoveClip,
    RippleCut,
    SetClipProperty,
    SetKeyframe,
    SetParam,
    SetSegmentText,
    SetTranscript,
    SplitClip,
    TrimClip,
    insert_generated,
    insert_media,
)
from kumiki.core.jetcut import plan_cuts
from kumiki.core.model import (
    AnimatedValue,
    Clip,
    ClipId,
    EffectId,
    Interpolation,
    MediaId,
    MediaItem,
    ParamValue,
    Project,
    SegmentId,
    Track,
    TrackId,
    TrackKind,
)
from kumiki.core.projection import project_timeline
from kumiki.core.timebase import format_timecode
from kumiki.effects import registry
from kumiki.effects.sources import SHAPE, TEXT, source_registry
from kumiki.effects.spec import (
    CheckSpec,
    ColorSpec,
    ParameterSpec,
    ParamInput,
    SelectSpec,
    TrackSpec,
)

__all__ = ["OPERATIONS", "ImageResult", "Operation", "find_operation"]

#: プレビュー画像の既定の横幅。小さめにしてあるのは、AI が見るのは
#: 「意図した絵になっているか」であって、画素を数えるわけではないため。
DEFAULT_PREVIEW_WIDTH = 640
MAX_PREVIEW_WIDTH = 1280


@dataclass(frozen=True, slots=True)
class ImageResult:
    """画像を返すツールの戻り値。"""

    png: bytes
    caption: str = ""


@dataclass(frozen=True, slots=True)
class Operation:
    """AI に見せるツール 1 つ。"""

    name: str
    description: str
    #: MCP へ渡す JSON Schema。省略可能な引数を表せるよう、素の辞書で持つ。
    schema: dict[str, Any]
    handler: Callable[[EditorHost, dict[str, Any]], object]
    #: プロジェクトを変えるか。確認ダイアログの要否がこれで決まる。
    writes: bool = False

    def __call__(self, host: EditorHost, arguments: dict[str, Any]) -> object:
        return self.handler(host, arguments)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _integer(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def _number(description: str) -> dict[str, Any]:
    return {"type": "number", "description": description}


def _boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


# --- 取り出しの補助 ---


def _project(host: EditorHost) -> Project:
    return host.document.project


def _require_clip(project: Project, clip_id: str) -> tuple[Track, Clip]:
    located = project.timeline.locate_clip(ClipId(clip_id))
    if located is None:
        raise ToolError(f"クリップが見つかりません: {clip_id}（list_clips で一覧を取れます）")
    return located


def _require_media(project: Project, media_id: str) -> MediaItem:
    item = project.find_media(MediaId(media_id))
    if item is None:
        raise ToolError(f"素材が見つかりません: {media_id}（list_media で一覧を取れます）")
    return item


def _target_clip(host: EditorHost, arguments: dict[str, Any]) -> tuple[Track, Clip]:
    """引数のクリップ、無ければ選択中のクリップ。

    「選んでいるやつに掛けて」という指示が通るようにする。毎回 ID を聞き返すのは
    会話として重い。
    """
    clip_id = str(arguments.get("clip_id") or "")
    if not clip_id:
        selected = host.selected_clip
        if selected is None:
            raise ToolError("clip_id を指定してください（選択中のクリップもありません）")
        clip_id = str(selected)
    return _require_clip(_project(host), clip_id)


# --- 読み取り ---


def _get_project(host: EditorHost, arguments: dict[str, Any]) -> object:
    del arguments
    project = _project(host)
    settings = project.settings
    return {
        "name": project.name,
        "resolution": f"{settings.width}x{settings.height}",
        "frame_rate": f"{settings.frame_rate.num}/{settings.frame_rate.den}",
        "fps": round(float(settings.frame_rate.fps), 3),
        "sample_rate": settings.sample_rate,
        "duration_frames": project.duration,
        "duration_timecode": format_timecode(project.duration, project.rate),
        "media_count": len(project.media),
        "track_count": len(project.timeline.tracks),
        "playhead": host.playhead,
        "can_undo": host.document.can_undo,
    }


def _list_media(host: EditorHost, arguments: dict[str, Any]) -> object:
    del arguments
    project = _project(host)
    return [
        {
            "media_id": str(item.id),
            "name": item.name,
            "path": str(item.path),
            "duration_seconds": round(float(item.duration), 3),
            "has_video": item.has_video,
            "has_audio": item.has_audio,
            "subtitle_count": len(item.transcript) if item.transcript is not None else 0,
        }
        for item in project.media
    ]


def _list_tracks(host: EditorHost, arguments: dict[str, Any]) -> object:
    del arguments
    return [
        {
            "track_id": str(track.id),
            "kind": track.kind.value,
            "name": track.name,
            "clip_count": len(track.clips),
            "locked": track.locked,
            "muted": track.muted,
        }
        for track in _project(host).timeline.tracks
    ]


def _list_clips(host: EditorHost, arguments: dict[str, Any]) -> object:
    project = _project(host)
    wanted = str(arguments.get("track_id") or "")
    clips = []
    for track in project.timeline.tracks:
        if wanted and str(track.id) != wanted:
            continue
        for clip in track.clips:
            media = project.find_media(clip.media_id) if clip.media_id is not None else None
            clips.append(
                {
                    "clip_id": str(clip.id),
                    "track_id": str(track.id),
                    "track_kind": track.kind.value,
                    "start": clip.timeline_start,
                    "duration": clip.duration,
                    "start_timecode": format_timecode(clip.timeline_start, project.rate),
                    "source_in_seconds": round(float(clip.source_in), 3),
                    "speed": round(float(clip.speed), 4),
                    "media": media.name if media is not None else None,
                    "source": clip.source.kind if clip.source is not None else None,
                    "effects": [
                        {"effect_id": str(e.id), "kind": e.kind, "enabled": e.enabled}
                        for e in clip.effects
                    ],
                }
            )
    return clips


def _get_selection(host: EditorHost, arguments: dict[str, Any]) -> object:
    del arguments
    project = _project(host)
    selected = host.selected_clip
    return {
        "clip_id": str(selected) if selected is not None else None,
        "playhead": host.playhead,
        "playhead_timecode": format_timecode(host.playhead, project.rate),
    }


def _list_effects(host: EditorHost, arguments: dict[str, Any]) -> object:
    del host, arguments
    definitions = []
    for definition in registry.all():
        definitions.append(
            {
                "kind": definition.kind,
                "label": definition.label,
                "category": definition.category,
                "parameters": [_describe_spec(spec) for spec in definition.parameters],
            }
        )
    definitions.extend(
        {
            "kind": source.kind,
            "label": source.label,
            "category": "オブジェクト",
            "parameters": [_describe_spec(spec) for spec in source.parameters],
        }
        for source in (TEXT, SHAPE)
    )
    return definitions


def _describe_spec(spec: object) -> dict[str, Any]:
    """パラメータ 1 つの説明。AI が値の範囲を外さないよう、上下限まで見せる。"""
    described: dict[str, Any] = {
        "name": getattr(spec, "name", ""),
        "label": getattr(spec, "label", ""),
    }
    if isinstance(spec, TrackSpec):
        described.update(
            {"type": "number", "min": spec.minimum, "max": spec.maximum, "unit": spec.unit}
        )
    elif isinstance(spec, SelectSpec):
        described.update({"type": "select", "choices": [value for value, _ in spec.choices]})
    elif isinstance(spec, ColorSpec):
        described["type"] = "color"
        described["format"] = "#RRGGBB か #RRGGBBAA、または [R, G, B, A]（0..1）"
    else:
        described["type"] = type(spec).__name__.replace("Spec", "").lower()
    return described


def _get_subtitles(host: EditorHost, arguments: dict[str, Any]) -> object:
    project = _project(host)
    wanted = str(arguments.get("media_id") or "")
    rows = []
    for subtitle in project_timeline(project):
        located = project.timeline.locate_clip(subtitle.clip_id)
        media_id = located[1].media_id if located is not None else None
        if wanted and str(media_id) != wanted:
            continue
        rows.append(
            {
                "segment_id": str(subtitle.segment.id),
                "media_id": str(media_id) if media_id is not None else None,
                "text": subtitle.segment.text,
                "start": subtitle.start_frame,
                "end": subtitle.end_frame,
                "start_timecode": format_timecode(subtitle.start_frame, project.rate),
                "source_start_seconds": round(float(subtitle.segment.start), 3),
            }
        )
    return rows


def _preview_frame(host: EditorHost, arguments: dict[str, Any]) -> object:
    """指定フレームを合成して画像で返す。

    これが無いと、AI は自分の編集結果を確かめる手段が無く、当てずっぽうになる。
    描く前に再生を止める。再生しながら別のフレームを描くと GL の資源を取り合う。
    """
    project = _project(host)
    frame = int(arguments.get("frame", host.playhead))
    frame = max(0, min(frame, max(project.duration - 1, 0)))
    width = int(arguments.get("width", DEFAULT_PREVIEW_WIDTH))
    width = max(160, min(width, MAX_PREVIEW_WIDTH))

    host.stop_playback()
    png = host.render_png(frame, width=width)
    when = format_timecode(frame, project.rate)
    return ImageResult(png=png, caption=f"{when}（{frame} フレーム）")


def _get_history(host: EditorHost, arguments: dict[str, Any]) -> object:
    del arguments
    document = host.document
    return {
        "can_undo": document.can_undo,
        "undo_label": document.undo_label,
        "recent": list(document.history_labels[-10:]),
    }


# --- 変更 ---


def _import_media(host: EditorHost, arguments: dict[str, Any]) -> object:
    raw = arguments.get("paths") or []
    if isinstance(raw, str):
        raw = [raw]
    paths = [Path(str(entry)) for entry in raw]
    if not paths:
        raise ToolError("paths が空です")

    project = _project(host)
    commands: list[Command] = []
    added: list[str] = []
    for path in paths:
        if not path.exists():
            raise ToolError(f"ファイルがありません: {path}")
        media = host.probe(path)
        batch = insert_media(project, media, at_frame=None)
        for command in batch:
            project = command.apply(project)
        commands.extend(batch)
        host.analyze(media)
        added.append(f"{media.name} ({media.id})")

    host.apply_commands(commands, f"素材を読み込み: {len(paths)} 件")
    return {"imported": added}


def _add_track(host: EditorHost, arguments: dict[str, Any]) -> object:
    kind = str(arguments.get("kind", "video")).lower()
    if kind not in ("video", "audio"):
        raise ToolError("kind は video か audio です")
    track_kind = TrackKind.VIDEO if kind == "video" else TrackKind.AUDIO
    prefix = "V" if track_kind is TrackKind.VIDEO else "A"
    index = sum(1 for t in _project(host).timeline.tracks if t.kind is track_kind) + 1
    track = Track(kind=track_kind, name=str(arguments.get("name") or f"{prefix}{index}"))
    host.apply_commands([AddTrack(track)], f"トラックを追加: {track.name}")
    return {"track_id": str(track.id), "name": track.name}


def _place_media(host: EditorHost, arguments: dict[str, Any]) -> object:
    project = _project(host)
    media = _require_media(project, str(arguments.get("media_id", "")))
    at_frame = arguments.get("at_frame")
    commands = insert_media(
        project, media, at_frame=int(at_frame) if at_frame is not None else None
    )
    if not commands:
        raise ToolError(f"{media.name} は長さが無いので置けません")
    host.apply_commands(commands, f"配置: {media.name}")
    return {"placed": media.name}


def _add_text(host: EditorHost, arguments: dict[str, Any]) -> object:
    text = str(arguments.get("text", "")).strip()
    if not text:
        raise ToolError("text が空です")
    overrides: dict[str, float | str] = {"text": text}
    for name in ("size", "pos_x", "pos_y", "border_width"):
        if name in arguments:
            overrides[name] = float(arguments[name])

    project = _project(host)
    at_frame = arguments.get("at_frame")
    duration = int(arguments.get("duration", 150))
    if duration < 1:
        raise ToolError("duration は 1 フレーム以上です")
    commands = insert_generated(
        project,
        TEXT.create(**overrides),
        at_frame=int(at_frame) if at_frame is not None else host.playhead,
        duration=duration,
    )
    host.apply_commands(commands, f"テキストを追加: {text[:12]}")
    return {"added": text}


def _split_clip(host: EditorHost, arguments: dict[str, Any]) -> object:
    _, clip = _target_clip(host, arguments)
    frame = int(arguments.get("frame", host.playhead))
    host.apply_commands([SplitClip(clip.id, frame)], "クリップを分割")
    return {"split_at": frame}


def _trim_clip(host: EditorHost, arguments: dict[str, Any]) -> object:
    _, clip = _target_clip(host, arguments)
    head = int(arguments.get("head_delta", 0))
    tail = int(arguments.get("tail_delta", 0))
    if head == 0 and tail == 0:
        raise ToolError("head_delta か tail_delta のどちらかを指定してください")
    host.apply_commands([TrimClip(clip.id, head_delta=head, tail_delta=tail)], "クリップをトリム")
    return {"head_delta": head, "tail_delta": tail}


def _move_clip(host: EditorHost, arguments: dict[str, Any]) -> object:
    _, clip = _target_clip(host, arguments)
    start = int(arguments.get("timeline_start", clip.timeline_start))
    track_id = str(arguments.get("track_id") or "")
    host.apply_commands(
        [MoveClip(clip.id, start, TrackId(track_id) if track_id else None)], "クリップを移動"
    )
    return {"timeline_start": start}


def _delete_clip(host: EditorHost, arguments: dict[str, Any]) -> object:
    _, clip = _target_clip(host, arguments)
    ripple = bool(arguments.get("ripple", False))
    host.apply_commands([RemoveClip(clip.id, ripple=ripple)], "クリップを削除")
    return {"deleted": str(clip.id), "ripple": ripple}


def _set_clip_property(host: EditorHost, arguments: dict[str, Any]) -> object:
    _, clip = _target_clip(host, arguments)
    name = str(arguments.get("name", ""))
    if name not in SetClipProperty.ALLOWED:
        raise ToolError(f"変えられるのは {'、'.join(SetClipProperty.ALLOWED)} です")
    value: object = arguments.get("value")
    if name == "speed":
        value = Fraction(str(value)).limit_denominator(1000)
    elif name == "enabled":
        value = bool(value)
    elif name == "stream_index":
        value = int(str(value))
    host.apply_commands([SetClipProperty(clip.id, name, value)], f"クリップの{name}を変更")
    return {"name": name, "value": str(value)}


def _add_effect(host: EditorHost, arguments: dict[str, Any]) -> object:
    _, clip = _target_clip(host, arguments)
    kind = str(arguments.get("kind", ""))
    definition = registry.get(kind)
    if definition is None:
        available = "、".join(d.kind for d in registry.all())
        raise ToolError(f"そのエフェクトはありません: {kind}（使えるのは {available}）")

    raw = arguments.get("params") or {}
    if not isinstance(raw, dict):
        raise ToolError("params はオブジェクトで渡してください")
    effect = definition.create(**{str(k): v for k, v in raw.items()})
    host.apply_commands([AddEffect(clip.id, effect)], f"エフェクトを追加: {definition.label}")
    return {"effect_id": str(effect.id), "kind": kind}


def _param_path(clip_id: ClipId, arguments: dict[str, Any]) -> ParamPath:
    name = str(arguments.get("name", ""))
    if not name:
        raise ToolError("name が空です")
    effect_id = str(arguments.get("effect_id") or "")
    if effect_id:
        return ParamPath.of_effect(clip_id, EffectId(effect_id), name)
    target = str(arguments.get("target", "source")).lower()
    if target == "clip":
        return ParamPath.of_clip(clip_id, name)
    return ParamPath.of_source(clip_id, name)


def _set_param(host: EditorHost, arguments: dict[str, Any]) -> object:
    _, clip = _target_clip(host, arguments)
    path = _param_path(clip.id, arguments)
    value = arguments.get("value")
    resolved = _coerce_param(_project(host), path, value)
    host.apply_commands([SetParam(path, resolved)], f"{path.name} を変更")
    return {"name": path.name, "value": str(value)}


def _spec_for(project: Project, path: ParamPath) -> ParameterSpec | None:
    """そのパラメータの定義を引く。

    UI と同じ定義を通して値を寄せるためにある。ここを通さないと、色に
    ``"#FFFFFF"`` という文字列がそのまま入るような食い違いが起きる。
    """
    located = project.timeline.locate_clip(path.clip_id)
    if located is None:
        return None
    _, clip = located

    if path.target is ParamTarget.EFFECT and path.effect_id is not None:
        effect = next((e for e in clip.effects if e.id == path.effect_id), None)
        definition = registry.get(effect.kind) if effect is not None else None
        return definition.spec(path.name) if definition is not None else None

    if path.target is ParamTarget.SOURCE and clip.source is not None:
        source = source_registry.get(clip.source.kind)
        return source.spec(path.name) if source is not None else None
    return None


def _coerce_param(project: Project, path: ParamPath, value: object) -> ParamValue:
    """AI が渡した値を、パラメータの型へ寄せる。"""
    prepared: ParamInput
    if isinstance(value, str):
        parsed = _parse_color(value)
        prepared = parsed if parsed is not None else value
    elif isinstance(value, list):
        prepared = tuple(float(entry) for entry in value)
    elif isinstance(value, bool):
        prepared = 1.0 if value else 0.0
    elif isinstance(value, int | float):
        prepared = float(value)
    else:
        prepared = str(value)

    spec = _spec_for(project, path)
    if spec is not None:
        if isinstance(spec, CheckSpec):
            return int(spec.coerce(_as_check(value, prepared)))
        return spec.coerce(prepared)

    # 定義が引けないもの（クリップ自身の不透明度など）は数値として扱う。
    if isinstance(prepared, float):
        return AnimatedValue(prepared)
    if isinstance(prepared, tuple):
        return prepared
    return str(prepared)


def _as_check(original: object, prepared: ParamInput) -> ParamInput:
    """チェック項目は、真偽値をそのまま渡した方が素直に決まる。"""
    return original if isinstance(original, bool | str) else prepared


def _parse_color(text: str) -> tuple[float, ...] | None:
    """``#RRGGBB`` / ``#RRGGBBAA`` を 0..1 の組へ。色でなければ ``None``。

    AI は色を 16 進で書いてくる。ここで受けないと、色のパラメータに文字列が
    入って描画側で無視される（しかも見た目が変わらないので気付きにくい）。
    """
    value = text.strip()
    if not value.startswith("#"):
        return None
    digits = value[1:]
    if len(digits) not in (6, 8) or any(c not in "0123456789abcdefABCDEF" for c in digits):
        return None
    channels = [int(digits[i : i + 2], 16) / 255.0 for i in range(0, len(digits), 2)]
    while len(channels) < 4:
        channels.append(1.0)
    return tuple(channels)


def _add_keyframe(host: EditorHost, arguments: dict[str, Any]) -> object:
    _, clip = _target_clip(host, arguments)
    path = _param_path(clip.id, arguments)
    frame = int(arguments.get("frame", host.playhead))
    try:
        value = float(arguments["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolError("value に数値が要ります") from exc

    name = str(arguments.get("interpolation", "linear")).lower()
    try:
        interpolation = Interpolation(name)
    except ValueError as exc:
        choices = "、".join(i.value for i in Interpolation)
        raise ToolError(f"補間は {choices} のどれかです") from exc

    host.apply_commands(
        [SetKeyframe(path, frame, value, interpolation)], f"{path.name} にキーフレーム"
    )
    return {"name": path.name, "frame": frame, "value": value}


def _set_subtitle_text(host: EditorHost, arguments: dict[str, Any]) -> object:
    project = _project(host)
    media = _require_media(project, str(arguments.get("media_id", "")))
    segment_id = str(arguments.get("segment_id", ""))
    text = str(arguments.get("text", ""))
    host.apply_commands([SetSegmentText(media.id, SegmentId(segment_id), text)], "字幕を編集")
    return {"segment_id": segment_id, "text": text}


def _clean_subtitles(host: EditorHost, arguments: dict[str, Any]) -> object:
    from kumiki.asr.cleanup import CleanupOptions, clean_transcript

    project = _project(host)
    media = _require_media(project, str(arguments.get("media_id", "")))
    if media.transcript is None:
        raise ToolError(f"{media.name} にはまだ字幕がありません")

    options = CleanupOptions(
        max_line_chars=int(arguments.get("max_line_chars", 20)),
        max_lines=int(arguments.get("max_lines", 2)),
        punctuation=str(arguments.get("punctuation", "keep")),
    )
    cleaned = clean_transcript(media.transcript, options)
    changed = sum(
        1
        for before, after in zip(media.transcript.segments, cleaned.segments, strict=False)
        if before.text != after.text
    )
    host.apply_commands([SetTranscript(media.id, cleaned)], "字幕を整形")
    return {"changed": changed, "remaining": len(cleaned)}


def _jet_cut(host: EditorHost, arguments: dict[str, Any]) -> object:
    from kumiki.engine.audio.silence import SilenceOptions, detect_silence, keep_speech

    project = _project(host)
    media = _require_media(project, str(arguments.get("media_id", "")))
    waveform = host.waveform(media)
    if waveform is None:
        raise ToolError(f"{media.name} の波形解析がまだ終わっていません。少し待ってください")

    options = SilenceOptions(
        threshold_db=float(arguments.get("threshold_db", -40.0)),
        min_silence=_seconds(arguments.get("min_silence", 0.5)),
        padding=_seconds(arguments.get("padding", 0.1)),
    )
    silences = detect_silence(waveform, options)
    if bool(arguments.get("keep_speech", True)) and media.transcript is not None:
        silences = keep_speech(silences, media.transcript)

    ranges = plan_cuts(project, media.id, silences)
    if not ranges:
        raise ToolError("切れる無音が見つかりません。threshold_db を上げてみてください")

    removed = sum(end - start for start, end in ranges)
    host.apply_commands([RippleCut(ranges)], f"無音カット: {len(ranges)} か所")
    return {
        "cuts": len(ranges),
        "removed_frames": removed,
        "removed_seconds": round(float(removed * project.rate.frame_duration), 2),
    }


def _seconds(value: object) -> Fraction:
    return Fraction(round(float(str(value)) * 100), 100)


def _transcribe(host: EditorHost, arguments: dict[str, Any]) -> object:
    project = _project(host)
    media = _require_media(project, str(arguments.get("media_id", "")))
    if not media.has_audio:
        raise ToolError(f"{media.name} に音声がありません")
    message = host.start_transcription(media.id, str(arguments.get("model", "large-v3")))
    return {
        "started": message,
        "next": "しばらく待ってから transcription_status を見てください。"
        "終わったら get_subtitles で結果を取れます。",
    }


def _transcription_status(host: EditorHost, arguments: dict[str, Any]) -> object:
    del arguments
    return {"status": host.transcription_status()}


def _undo(host: EditorHost, arguments: dict[str, Any]) -> object:
    steps = max(1, int(arguments.get("steps", 1)))
    document = host.document
    undone: list[str] = []
    for _ in range(steps):
        label = document.undo_label
        if label is None:
            break
        document.undo()
        undone.append(label)
    if not undone:
        raise ToolError("戻せる操作がありません")
    return {"undone": undone}


def _seek(host: EditorHost, arguments: dict[str, Any]) -> object:
    frame = max(0, int(arguments.get("frame", 0)))
    host.seek(frame)
    return {"playhead": frame}


def _select(host: EditorHost, arguments: dict[str, Any]) -> object:
    clip_id = str(arguments.get("clip_id") or "")
    if clip_id:
        _require_clip(_project(host), clip_id)
        host.select_clip(ClipId(clip_id))
    else:
        host.select_clip(None)
    return {"selected": clip_id or None}


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        name="get_project",
        description="プロジェクトの設定（解像度・fps・長さ）と再生ヘッドの位置を返す。",
        schema=_schema({}),
        handler=_get_project,
    ),
    Operation(
        name="list_media",
        description="メディアプールの素材を一覧する。media_id はここで得る。",
        schema=_schema({}),
        handler=_list_media,
    ),
    Operation(
        name="list_tracks",
        description="タイムラインのトラックを一覧する。",
        schema=_schema({}),
        handler=_list_tracks,
    ),
    Operation(
        name="list_clips",
        description="クリップを一覧する。track_id を省くと全トラックが対象。",
        schema=_schema({"track_id": _string("絞り込むトラック")}),
        handler=_list_clips,
    ),
    Operation(
        name="get_selection",
        description="選択中のクリップと再生ヘッドの位置。",
        schema=_schema({}),
        handler=_get_selection,
    ),
    Operation(
        name="list_effects",
        description="使えるエフェクトと生成オブジェクト、そのパラメータ名と範囲。",
        schema=_schema({}),
        handler=_list_effects,
    ),
    Operation(
        name="get_subtitles",
        description="タイムラインに出る字幕を、表示位置つきで一覧する。",
        schema=_schema({"media_id": _string("絞り込む素材")}),
        handler=_get_subtitles,
    ),
    Operation(
        name="get_history",
        description="直近の操作履歴と、取り消せるかどうか。",
        schema=_schema({}),
        handler=_get_history,
    ),
    Operation(
        name="preview_frame",
        description=(
            "そのフレームを合成して画像で返す。編集した結果を自分の目で確かめるために使う。"
            "再生中なら止めてから描く。"
        ),
        schema=_schema(
            {
                "frame": _integer("見たいフレーム。省略すると再生ヘッド"),
                "width": _integer("画像の横幅（160〜1280、既定 640）"),
            }
        ),
        handler=_preview_frame,
    ),
    Operation(
        name="seek",
        description="再生ヘッドを動かす。",
        schema=_schema({"frame": _integer("移動先のフレーム")}, ["frame"]),
        handler=_seek,
    ),
    Operation(
        name="select_clip",
        description="クリップを選択する。clip_id を空にすると選択を解く。",
        schema=_schema({"clip_id": _string("選ぶクリップ")}),
        handler=_select,
    ),
    Operation(
        name="import_media",
        description="ファイルを読み込んでタイムラインの末尾へ置く。",
        schema=_schema(
            {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ファイルパス",
                }
            },
            ["paths"],
        ),
        handler=_import_media,
        writes=True,
    ),
    Operation(
        name="place_media",
        description="読み込み済みの素材をタイムラインへ置く。",
        schema=_schema(
            {
                "media_id": _string("置く素材"),
                "at_frame": _integer("置く位置。省略すると末尾"),
            },
            ["media_id"],
        ),
        handler=_place_media,
        writes=True,
    ),
    Operation(
        name="add_track",
        description="トラックを足す。",
        schema=_schema({"kind": _string("video か audio"), "name": _string("表示名")}),
        handler=_add_track,
        writes=True,
    ),
    Operation(
        name="add_text",
        description="テキストオブジェクトを置く。テロップや字幕の焼き込みに使う。",
        schema=_schema(
            {
                "text": _string("本文。改行を含めてよい"),
                "at_frame": _integer("置く位置。省略すると再生ヘッド"),
                "duration": _integer("長さ（フレーム、既定 150）"),
                "size": _number("文字サイズ"),
                "pos_x": _number("中央からの横位置"),
                "pos_y": _number("中央からの縦位置。正が上"),
                "border_width": _number("縁取りの太さ"),
            },
            ["text"],
        ),
        handler=_add_text,
        writes=True,
    ),
    Operation(
        name="split_clip",
        description="クリップを分割する。clip_id を省くと選択中のクリップ。",
        schema=_schema(
            {"clip_id": _string("対象"), "frame": _integer("分割位置。省略すると再生ヘッド")}
        ),
        handler=_split_clip,
        writes=True,
    ),
    Operation(
        name="trim_clip",
        description="クリップの端を動かす。head_delta は正で短く、tail_delta は正で長くなる。",
        schema=_schema(
            {
                "clip_id": _string("対象"),
                "head_delta": _integer("先頭を動かす量"),
                "tail_delta": _integer("末尾を動かす量"),
            }
        ),
        handler=_trim_clip,
        writes=True,
    ),
    Operation(
        name="move_clip",
        description="クリップを別の位置・別のトラックへ動かす。",
        schema=_schema(
            {
                "clip_id": _string("対象"),
                "timeline_start": _integer("移動先"),
                "track_id": _string("移動先トラック"),
            },
            ["timeline_start"],
        ),
        handler=_move_clip,
        writes=True,
    ),
    Operation(
        name="delete_clip",
        description="クリップを消す。ripple を真にすると後ろを詰める。",
        schema=_schema({"clip_id": _string("対象"), "ripple": _boolean("詰めるか")}),
        handler=_delete_clip,
        writes=True,
    ),
    Operation(
        name="set_clip_property",
        description="クリップの blend_mode / speed / enabled / stream_index を変える。",
        schema=_schema(
            {
                "clip_id": _string("対象"),
                "name": _string("項目名"),
                "value": {"description": "新しい値"},
            },
            ["name", "value"],
        ),
        handler=_set_clip_property,
        writes=True,
    ),
    Operation(
        name="add_effect",
        description="クリップにエフェクトを積む。使える kind は list_effects で分かる。",
        schema=_schema(
            {
                "clip_id": _string("対象"),
                "kind": _string("エフェクトの種類"),
                "params": {"type": "object", "description": "初期値"},
            },
            ["kind"],
        ),
        handler=_add_effect,
        writes=True,
    ),
    Operation(
        name="set_param",
        description=(
            "パラメータを変える。effect_id を渡せばそのエフェクト、"
            "省略すればテキストや図形の中身（target=clip でクリップ自身）。"
        ),
        schema=_schema(
            {
                "clip_id": _string("対象"),
                "effect_id": _string("エフェクト"),
                "target": _string("source か clip"),
                "name": _string("パラメータ名"),
                "value": {"description": "新しい値"},
            },
            ["name", "value"],
        ),
        handler=_set_param,
        writes=True,
    ),
    Operation(
        name="add_keyframe",
        description="パラメータにキーフレームを打つ。値は数値のみ。",
        schema=_schema(
            {
                "clip_id": _string("対象"),
                "effect_id": _string("エフェクト"),
                "target": _string("source か clip"),
                "name": _string("パラメータ名"),
                "frame": _integer("位置。省略すると再生ヘッド"),
                "value": _number("その位置での値"),
                "interpolation": _string("linear / ease / hold / bezier"),
            },
            ["name", "value"],
        ),
        handler=_add_keyframe,
        writes=True,
    ),
    Operation(
        name="set_subtitle_text",
        description="字幕 1 枚の本文を書き換える。素材に紐付くので全出現箇所に反映される。",
        schema=_schema(
            {
                "media_id": _string("素材"),
                "segment_id": _string("字幕"),
                "text": _string("新しい本文"),
            },
            ["media_id", "segment_id", "text"],
        ),
        handler=_set_subtitle_text,
        writes=True,
    ),
    Operation(
        name="clean_subtitles",
        description="フィラー語を落とし、改行位置を整える。",
        schema=_schema(
            {
                "media_id": _string("素材"),
                "max_line_chars": _integer("1 行の文字数（0 で折り返さない）"),
                "max_lines": _integer("行数の上限"),
                "punctuation": _string("keep / space / strip"),
            },
            ["media_id"],
        ),
        handler=_clean_subtitles,
        writes=True,
    ),
    Operation(
        name="jet_cut",
        description="無音区間をタイムラインからまとめて削って詰める。",
        schema=_schema(
            {
                "media_id": _string("素材"),
                "threshold_db": _number("無音とみなす音量（既定 -40）"),
                "min_silence": _number("最短の無音（秒、既定 0.5）"),
                "padding": _number("前後に残す余白（秒、既定 0.1）"),
                "keep_speech": _boolean("字幕のある区間は切らない（既定 true）"),
            },
            ["media_id"],
        ),
        handler=_jet_cut,
        writes=True,
    ),
    Operation(
        name="transcribe",
        description="素材の字幕起こしを始める。終わるまで数分かかる。",
        schema=_schema({"media_id": _string("素材"), "model": _string("モデル名")}, ["media_id"]),
        handler=_transcribe,
        writes=True,
    ),
    Operation(
        name="transcription_status",
        description="走っている字幕起こしの様子を見る。",
        schema=_schema({}),
        handler=_transcription_status,
    ),
    Operation(
        name="undo",
        description="直前の操作を取り消す。",
        schema=_schema({"steps": _integer("戻す段数（既定 1）")}),
        handler=_undo,
        writes=True,
    ),
)


def find_operation(name: str) -> Operation | None:
    for operation in OPERATIONS:
        if operation.name == name:
            return operation
    return None
