"""faster-whisper（CTranslate2）による起こし

このモジュールは**読み込まれただけでは何も import しない** faster-whisper の
import は数秒かかり、CUDA の DLL 探索まで走る 字幕を使わない起動でその代償を
払わせないため、実際に起こすときまで遅らせている

導入されていない環境でも :meth:`FasterWhisperBackend.is_available` は落ちずに
偽を返す これが「未導入の状態で起動し、必要になったらソフト内から入れる」という
配布方針の前提になる（:mod:`sashimono.asr.environment` を参照）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from sashimono.asr.backend import (
    AsrError,
    Progress,
    ShouldCancel,
    TranscribeOptions,
    to_source_time,
)
from sashimono.asr.environment import runtime_status
from sashimono.core.model import Transcript, TranscriptSegment, Word
from sashimono.engine.decode import AudioDecoder, ProbeError, probe_media

#: faster-whisper が受け取る音の形（16kHz・モノラル・float32）
WHISPER_SAMPLE_RATE = 16000
#: 音を読むときの 1 回の長さ（秒） 長い素材を 1 回で読むと、途中で止める機会が無い
_READ_CHUNK_SECONDS = 60

__all__ = ["FasterWhisperBackend"]


class FasterWhisperBackend:
    """faster-whisper を呼ぶバックエンド

    読み込んだモデルは保持する 1 本目と 2 本目で同じモデルなら、2 回目は
    数秒の読み込みを省ける
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._loaded_with: tuple[str, str, str] | None = None

    @property
    def name(self) -> str:
        return "faster-whisper"

    def is_available(self) -> bool:
        return runtime_status().ready

    def unload(self) -> None:
        """モデルを解放する GPU のメモリを書き出しへ譲りたいときに呼ぶ"""
        self._model = None
        self._loaded_with = None

    def transcribe(
        self,
        path: Path,
        options: TranscribeOptions,
        *,
        progress: Progress | None = None,
        should_cancel: ShouldCancel | None = None,
    ) -> Transcript | None:
        source = Path(path)
        if not source.exists():
            raise AsrError(f"素材が見つからない: {source}")

        if progress is not None:
            progress(0.0, "モデルを読み込んでいる")
        model = self._ensure_model(options)
        if should_cancel is not None and should_cancel():
            return None

        audio = _media_audio(source, should_cancel)
        if audio is None:
            return None

        if progress is not None:
            progress(0.02, "音声を解析している")
        try:
            segments, info = model.transcribe(
                audio,
                language=options.language,
                beam_size=options.beam_size,
                vad_filter=options.vad_filter,
                word_timestamps=options.word_timestamps,
                initial_prompt=options.initial_prompt or None,
            )
        except Exception as exc:  # faster-whisper は独自の例外型を公開していない
            raise AsrError(f"起こしを開始できない: {exc}") from exc

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        language = str(getattr(info, "language", "") or options.language or "")

        collected: list[TranscriptSegment] = []
        try:
            # segments は生成器で、回した分だけ認識が進む ここで中断を見るので、
            # 「止めたのに GPU が回り続ける」状態にならない
            for raw in segments:
                if should_cancel is not None and should_cancel():
                    return None
                converted = _to_segment(raw)
                if converted is not None:
                    collected.append(converted)
                if progress is not None and duration > 0:
                    ratio = min(1.0, float(getattr(raw, "end", 0.0)) / duration)
                    progress(max(0.02, ratio), f"{len(collected)} 文を起こした")
        except Exception as exc:
            raise AsrError(f"起こしに失敗した: {exc}") from exc

        if progress is not None:
            progress(1.0, f"{len(collected)} 文")
        return Transcript(
            segments=tuple(_ordered(collected)),
            language=language,
            model=options.model,
        )

    def _ensure_model(self, options: TranscribeOptions) -> Any:
        key = (options.model, options.device, options.compute_type)
        if self._model is not None and self._loaded_with == key:
            return self._model

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AsrError(
                "起こしの実行環境が入っていません 字幕パネルの「環境を導入」から用意してください"
            ) from exc

        try:
            self._model = WhisperModel(
                options.model,
                device=options.device,
                compute_type=options.compute_type,
            )
        except Exception as exc:
            raise AsrError(f"モデルを読み込めない ({options.model}): {exc}") from exc
        self._loaded_with = key
        return self._model


def _media_audio(source: Path, should_cancel: ShouldCancel | None) -> np.ndarray | None:
    """素材の音を faster-whisper の形で読む 止められたら ``None``

    素材の時刻の原点（:func:`~sashimono.engine.decode.probe.media_origin`）から数えて読む
    パスを渡して faster-whisper に読ませると、音の最初のサンプルを 0 秒として数えるので、
    音の頭が原点と違う素材（AAC の前置き・音が映像より早く始まる物）では、起こした字幕が
    その差の分だけずれる（Issue #125） 原点より前の音（前置きなど）は置いたクリップでも
    鳴らない区間なので、起こさなくてよい
    """
    try:
        item = probe_media(source)
        chunk = _READ_CHUNK_SECONDS * WHISPER_SAMPLE_RATE
        total = int(item.duration * WHISPER_SAMPLE_RATE)
        # 配列を作る前に断る 音の無い長い動画で先に全長の配列を作ると、音が無いと
        # 分かる前に大きな確保が走る
        if not item.audio_streams or total <= 0:
            raise AsrError(f"音声が無い: {source}")
        # 全長の配列を先に 1 つだけ作って書き込む 読んだ分を貯めてから最後につなぐと、
        # つなぐ瞬間に同じ長さの配列が 2 つ並ぶ（1 時間で 230MB が 460MB になる）
        try:
            audio = np.empty(total, dtype=np.float32)
        except MemoryError as exc:
            # 起こしの失敗として出す 素のまま投げると起こしの枠の外（想定外の失敗）になる
            raise AsrError(f"音声を読むメモリが足りない（{total * 4 // 2**20} MB）") from exc
        with AudioDecoder(source, sample_rate=WHISPER_SAMPLE_RATE, channels=1) as decoder:
            for start in range(0, total, chunk):
                if should_cancel is not None and should_cancel():
                    return None
                count = min(chunk, total - start)
                audio[start : start + count] = decoder.read(start, count)[:, 0]
    except ProbeError as exc:
        raise AsrError(f"音声を読めない: {exc}") from exc
    # 読み終わりにも見る 見ないと、1 回で読み切る短い素材や最後の読み込みの間に止めても、
    # そのままモデルへ渡して起こしが始まる
    if should_cancel is not None and should_cancel():
        return None
    return audio


def _to_segment(raw: Any) -> TranscriptSegment | None:
    """faster-whisper のセグメントをモデルの形へ"""
    text = str(getattr(raw, "text", "")).strip()
    if not text:
        return None
    start = to_source_time(float(getattr(raw, "start", 0.0)))
    end = to_source_time(float(getattr(raw, "end", 0.0)))
    if end < start:
        end = start

    words: list[Word] = []
    for entry in getattr(raw, "words", None) or ():
        word_text = str(getattr(entry, "word", "")).strip()
        if not word_text:
            continue
        word_start = to_source_time(float(getattr(entry, "start", 0.0)))
        word_end = to_source_time(float(getattr(entry, "end", 0.0)))
        words.append(Word(start=word_start, end=max(word_start, word_end), text=word_text))

    return TranscriptSegment(start=start, end=end, text=text, words=tuple(words))


def _ordered(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """開始時刻の昇順に整える

    :class:`~sashimono.core.model.Transcript` は順序を不変条件にしている 認識器が
    まれに前後した時刻を返すので、モデルへ渡す前にここで揃える 例外にして
    起こし全体を捨てるのは割に合わない
    """
    return sorted(segments, key=lambda s: (s.start, s.end))
