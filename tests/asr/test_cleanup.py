"""起こし結果の整形

外部依存が無いので、起こしの実行環境を入れていなくても動く
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from kumiki.asr.cleanup import CleanupOptions, clean_text, clean_transcript, wrap_text
from kumiki.core.model import Transcript, TranscriptSegment


class TestFillerRemoval:
    def test_japanese_fillers_are_dropped(self) -> None:
        assert clean_text("えーと、今日は編集します") == "今日は編集します"

    def test_longer_fillers_win(self) -> None:
        # 「えー」を先に消すと「えーと」が「と」になって残る 長い方から当てる
        assert clean_text("えーと編集します") == "編集します"

    def test_english_fillers_need_word_boundaries(self) -> None:
        # "um" が "column" の中に当たってはいけない
        assert clean_text("um, the column is here") == "the column is here"

    def test_words_kept_by_default_can_be_added(self) -> None:
        # 「なんか」は意味を持つことがあるので既定では残す
        assert "なんか" in clean_text("なんか変です")
        options = CleanupOptions(fillers=("なんか",))
        assert clean_text("なんか変です", options) == "変です"

    def test_removal_can_be_switched_off(self) -> None:
        options = CleanupOptions(remove_fillers=False, max_line_chars=0)
        assert clean_text("えーと編集します", options) == "えーと編集します"


class TestRepeats:
    def test_restated_words_collapse(self) -> None:
        assert clean_text("the the file") == "the file"

    def test_reduplicated_japanese_words_survive(self) -> None:
        # 「いろいろ」を「いろ」にしてはいけない 2 文字の畳語は対象外
        assert clean_text("いろいろ試します") == "いろいろ試します"

    def test_immediate_restatement_collapses(self) -> None:
        assert clean_text("編集します編集します") == "編集します"


class TestPunctuation:
    def test_keep_is_the_default(self) -> None:
        assert clean_text("今日は、編集します。") == "今日は、編集します。"

    def test_strip_removes_them(self) -> None:
        options = CleanupOptions(punctuation="strip", max_line_chars=0)
        assert clean_text("今日は、編集します。", options) == "今日は編集します"

    def test_space_turns_them_into_gaps(self) -> None:
        options = CleanupOptions(punctuation="space", max_line_chars=0)
        assert clean_text("今日は、編集します。", options) == "今日は 編集します"

    def test_leading_punctuation_is_dropped(self) -> None:
        # フィラーを消した跡に読点だけが残る
        assert clean_text("えーと、そうですね") == "そうですね"

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="句読点の扱いが不正"):
            CleanupOptions(punctuation="いいかんじに")


class TestWrapping:
    def test_short_text_is_left_alone(self) -> None:
        assert wrap_text("短い", 20) == "短い"

    def test_breaking_prefers_punctuation(self) -> None:
        assert wrap_text("今日は編集を、します", 8) == "今日は編集を、\nします"

    def test_falls_back_to_character_count(self) -> None:
        wrapped = wrap_text("あいうえおかきくけこさしすせそ", 5, max_lines=3)
        assert wrapped == "あいうえお\nかきくけこ\nさしすせそ"

    def test_forbidden_characters_do_not_start_a_line(self) -> None:
        # 「ー」が行頭に来ないよう、1 文字手前で切る
        wrapped = wrap_text("あいうえおーかきくけこ", 5, max_lines=3)
        assert wrapped == "あいうえ\nおーかき\nくけこ"

    def test_the_last_two_lines_are_balanced(self) -> None:
        # 上限いっぱいで切ると 1 文字だけの行ができる 字幕としては見苦しいので、
        # 残りが 2 行に収まるときは長さを揃える
        assert wrap_text("あいうえおかきくけこさ", 10) == "あいうえおか\nきくけこさ"

    def test_lines_beyond_the_limit_are_merged_into_the_last(self) -> None:
        wrapped = wrap_text("あいうえおかきくけこさしすせそ", 5, max_lines=2)
        assert wrapped == "あいうえお\nかきくけこさしすせそ"

    def test_english_breaks_at_spaces(self) -> None:
        assert wrap_text("hello there world", 12) == "hello there\nworld"


class TestCleanTranscript:
    def _transcript(self, *texts: str) -> Transcript:
        return Transcript(
            tuple(
                TranscriptSegment(start=Fraction(i), end=Fraction(i + 1), text=text)
                for i, text in enumerate(texts)
            )
        )

    def test_segments_that_become_empty_are_dropped(self) -> None:
        cleaned = clean_transcript(self._transcript("えーと", "本題です"))
        assert [s.text for s in cleaned.segments] == ["本題です"]

    def test_unchanged_segments_are_not_marked_as_edited(self) -> None:
        # 印を付けると、起こし直したときに手で直したものと区別が付かなくなる
        cleaned = clean_transcript(self._transcript("本題です"))
        assert cleaned.segments[0].edited is False

    def test_changed_segments_are_marked(self) -> None:
        cleaned = clean_transcript(self._transcript("えーと本題です"))
        assert cleaned.segments[0].edited is True

    def test_hand_edited_segments_are_left_alone(self) -> None:
        manual = TranscriptSegment(
            start=Fraction(0), end=Fraction(1), text="えーとこのままで", edited=True
        )
        cleaned = clean_transcript(Transcript((manual,)))
        assert cleaned.segments[0].text == "えーとこのままで"

    def test_language_and_model_survive(self) -> None:
        source = Transcript(
            (TranscriptSegment(Fraction(0), Fraction(1), "本題です"),),
            language="ja",
            model="large-v3",
        )
        cleaned = clean_transcript(source)
        assert (cleaned.language, cleaned.model) == ("ja", "large-v3")

    def test_short_segments_can_be_dropped(self) -> None:
        source = Transcript(
            (
                TranscriptSegment(Fraction(0), Fraction(1, 10), "はい"),
                TranscriptSegment(Fraction(1), Fraction(3), "本題です"),
            )
        )
        cleaned = clean_transcript(source, CleanupOptions(min_duration=Fraction(1, 2)))
        assert [s.text for s in cleaned.segments] == ["本題です"]
