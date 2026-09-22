"""起こし結果の整形 フィラー語の除去、句読点の調整、改行位置の決定

音声認識の生の出力は字幕として使えない 「えーと」「あのー」が残り、句読点の位置が
不安定で、1 行が画面幅を超える ここはその後始末をする

外部依存を持たない純粋な文字列処理にしてある 起こしの実行環境（faster-whisper と
CUDA）を導入していなくても整形だけは動くようにするため 既存の字幕を読み込んで
整えるだけ、という使い方が普通にある
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

from sashimono.core.model import Transcript, TranscriptSegment

__all__ = [
    "DEFAULT_FILLERS",
    "EXTRA_FILLERS",
    "CleanupOptions",
    "clean_text",
    "clean_transcript",
    "wrap_text",
]

#: 既定で落とすフィラー語
#:
#: 「まあ」「なんか」のように、文脈によっては意味を持つ語はここに入れていない
#: （:data:`EXTRA_FILLERS` にある） 既定で意味のある語まで消えると、消えたことに
#: 気付かないまま書き出してしまう
DEFAULT_FILLERS: tuple[str, ...] = (
    "えーと",
    "えっと",
    "ええと",
    "えーっと",
    "あのー",
    "あの一",
    "そのー",
    "えー",
    "えっ",
    "あー",
    "うー",
    "うーん",
    "んー",
    "uh",
    "um",
    "erm",
)

#: 選べば落とすが、既定では残す語 意味を持つ場合がある
EXTRA_FILLERS: tuple[str, ...] = ("まあ", "なんか", "ちょっと", "やっぱり", "like", "you know")

#: 行頭に来ても意味の無い記号 整形の最後に落とす
_LEADING_PUNCTUATION = "、。，．,.・…　 "

#: 改行を入れてよい位置（この文字の直後で切る）
_BREAK_AFTER = "、。！？…，．!?,."

#: 行頭に置いてはいけない文字（禁則）
_FORBIDDEN_AT_LINE_START = "、。！？，．)）」』】〕》’”ー"

_WHITESPACE = re.compile(r"[ \t\u3000]+")


@dataclass(frozen=True, slots=True)
class CleanupOptions:
    """整形の条件"""

    remove_fillers: bool = True
    #: 落とす語 空なら :data:`DEFAULT_FILLERS` を使う
    fillers: tuple[str, ...] = field(default_factory=tuple)
    #: 直後に同じ語が繰り返された場合に 1 つへまとめる（言い直し）
    collapse_repeats: bool = True
    #: ``"keep"`` そのまま / ``"strip"`` 落とす / ``"space"`` 空白へ
    #: 字幕では読点を空白にする流儀が多い
    punctuation: str = "keep"
    #: 1 行あたりの文字数上限 0 なら折り返さない
    max_line_chars: int = 20
    #: 行数の上限 超える分は最後の行へ押し込む 分割は :class:`SplitSegment` で行う
    max_lines: int = 2
    #: これより短くなったセグメントは捨てる（相槌だけが残った場合）
    min_duration: Fraction = field(default_factory=lambda: Fraction(0))
    #: 手で直した字幕には触らない 整形を掛け直したときに編集が消えると困る
    skip_edited: bool = True

    def __post_init__(self) -> None:
        if self.punctuation not in ("keep", "strip", "space"):
            raise ValueError(f"句読点の扱いが不正: {self.punctuation}")
        if self.max_line_chars < 0 or self.max_lines < 1:
            raise ValueError("行の上限が不正")

    def filler_words(self) -> tuple[str, ...]:
        return self.fillers or DEFAULT_FILLERS


def clean_text(text: str, options: CleanupOptions | None = None) -> str:
    """1 枚分の字幕を整える"""
    resolved = options if options is not None else CleanupOptions()
    result = text.strip()

    if resolved.remove_fillers:
        result = _remove_fillers(result, resolved.filler_words())
    if resolved.collapse_repeats:
        result = _collapse_repeats(result)
    result = _apply_punctuation(result, resolved.punctuation)

    result = _WHITESPACE.sub(" ", result).strip()
    result = result.lstrip(_LEADING_PUNCTUATION).strip()

    if resolved.max_line_chars > 0:
        result = wrap_text(result, resolved.max_line_chars, resolved.max_lines)
    return result


def clean_transcript(transcript: Transcript, options: CleanupOptions | None = None) -> Transcript:
    """起こし全体を整える 空になったセグメントは落とす"""
    resolved = options if options is not None else CleanupOptions()
    kept: list[TranscriptSegment] = []

    for segment in transcript.segments:
        if resolved.skip_edited and segment.edited:
            kept.append(segment)
            continue
        text = clean_text(segment.text, resolved)
        if not text:
            continue
        if resolved.min_duration > 0 and segment.duration < resolved.min_duration:
            continue
        # 本文が変わらなかったものに編集済みの印を付けない 付けてしまうと、
        # 起こし直したときに「手で直した」と誤認して残ってしまう
        kept.append(segment if text == segment.text else segment.with_text(text))

    return Transcript(tuple(kept), language=transcript.language, model=transcript.model)


def wrap_text(text: str, max_chars: int, max_lines: int = 2) -> str:
    """字幕として読める位置で折り返す

    日本語は語の間に空白が無いので、単純な単語折り返しが使えない 句読点の直後を
    優先し、無ければ文字数で切る 行頭に来てはいけない文字（閉じ括弧・句読点・
    長音符）は前の行へ送る
    """
    if max_chars <= 0:
        return text

    lines: list[str] = []
    for paragraph in text.split("\n"):
        remaining = paragraph.strip()
        while remaining:
            if len(remaining) <= max_chars:
                lines.append(remaining)
                break
            cut = _break_position(remaining, max_chars)
            lines.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()

    if len(lines) > max_lines:
        # 上限を超えた分は最後の行へまとめる ここで捨てると本文が消える
        lines = [*lines[: max_lines - 1], "".join(lines[max_lines - 1 :])]
    return "\n".join(line for line in lines if line)


def _break_position(text: str, max_chars: int) -> int:
    """``text`` を切ってよい位置（この位置の手前までが 1 行）"""
    window = text[: max_chars + 1]
    # 早すぎる位置では切らない 句読点が行頭近くにあるからといってそこで切ると、
    # 数文字だけの行が並んで読みにくくなる
    earliest = max(1, max_chars // 2)

    # 句読点の直後が最優先 読点で切れれば意味の切れ目と一致する
    for index in range(len(window) - 1, earliest - 1, -1):
        if window[index - 1] in _BREAK_AFTER:
            return index

    # 次に空白 英語混じりの文で語の途中を割らないため
    space = window.rfind(" ")
    if space >= earliest:
        return space

    # 残り 2 行に収まるなら、長さを揃えて切る 上限いっぱいで切ると
    # 「……ます／す」のように 1 文字だけの行ができ、字幕として見苦しい
    cut = (len(text) + 1) // 2 if len(text) <= max_chars * 2 else max_chars

    # 禁則 行頭に置けない文字が来るなら 1 文字ずつ手前へ寄せる
    while cut > 1 and cut < len(text) and text[cut] in _FORBIDDEN_AT_LINE_START:
        cut -= 1
    return cut


def _remove_fillers(text: str, fillers: tuple[str, ...]) -> str:
    """フィラー語を落とす

    長いものから順に当てる 「えー」を先に消すと「えーと」が「と」になって残る
    英字のフィラーだけは前後を語境界に限る 日本語に語境界は無いので、そのまま
    当てると "um" が "column" の中に当たる、といった事故だけを防ぐ
    """
    if not fillers:
        return text
    result = text
    for filler in sorted(fillers, key=len, reverse=True):
        if filler.isascii():
            pattern = re.compile(rf"\b{re.escape(filler)}\b", re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(filler))
        result = pattern.sub("", result)
    return result


def _collapse_repeats(text: str) -> str:
    """直後の繰り返しを 1 つへまとめる

    言い直し（「これは これは」「the the」）を潰す 空白で区切られていない
    繰り返しは 3 文字以上の単位だけを対象にする 2 文字を対象にすると
    「いろいろ」「そろそろ」のような畳語まで削ってしまう
    """
    collapsed = re.sub(r"(\b\w{2,}\b)(\s+\1\b)+", r"\1", text, flags=re.IGNORECASE)
    return re.sub(r"([^\W\d_]{3,}?)\1+", _keep_one, collapsed)


def _keep_one(match: re.Match[str]) -> str:
    return match.group(1)


def _apply_punctuation(text: str, mode: str) -> str:
    if mode == "strip":
        return re.sub(r"[、。，．]", "", text)
    if mode == "space":
        return re.sub(r"[、，]", " ", re.sub(r"[。．]", " ", text))
    return text
