"""``.exo`` / ``.exa`` の読み込み。

文字コードの判別を厚めに見る。間違えると読めないのではなく、**文字化けしたまま
読めてしまう**ので、あとから原因を追いにくい。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novaedit.compat.aviutl.encoding import (
    decode_bytes,
    decode_utf16_hex,
    encode_utf16_hex,
)
from novaedit.compat.aviutl.exo import ExoParseError, load_exo, parse_exo

SAMPLE = """[exedit]
width=1920
height=1080
rate=30
scale=1
length=300
audio_rate=44100
audio_ch=2
[0]
start=1
end=60
layer=1
group=1
overlay=1
camera=0
[0.0]
_name=テキスト
サイズ=48
表示速度=0.0
color=ffffff
color2=000000
font=Yu Gothic UI
text=53004b0055005400
[0.1]
_name=標準描画
X=100.0
Y=-50.0
Z=0.0
拡大率=200.00
透明度=25.0
回転=45.00
blend=0
"""


class TestParsing:
    def test_settings_are_read(self) -> None:
        exo = parse_exo(SAMPLE)
        assert (exo.width, exo.height) == (1920, 1080)
        assert exo.frame_rate == (30, 1)

    def test_object_bounds(self) -> None:
        obj = parse_exo(SAMPLE).objects[0]
        # ファイルには 1 始まりで書かれている（start=1 end=60）。読んだ側では
        # 0 始まりに揃えるので 0..59。終端を含むので長さは 60。
        assert (obj.start, obj.end, obj.duration) == (0, 59, 60)
        assert obj.layer == 1

    def test_entries_keep_their_order(self) -> None:
        obj = parse_exo(SAMPLE).objects[0]
        assert [entry.name for entry in obj.entries] == ["テキスト", "標準描画"]
        assert obj.content is not None
        assert obj.content.name == "テキスト"
        assert [entry.name for entry in obj.filters()] == ["標準描画"]

    def test_numbers_are_read_as_numbers(self) -> None:
        draw = parse_exo(SAMPLE).objects[0].find("標準描画")
        assert draw is not None
        assert draw.number("X") == 100.0
        assert draw.number("拡大率") == 200.0
        assert draw.integer("blend") == 0

    def test_a_missing_value_falls_back(self) -> None:
        draw = parse_exo(SAMPLE).objects[0].find("標準描画")
        assert draw is not None
        assert draw.number("そんな項目は無い", 7.5) == 7.5

    def test_the_text_field_is_utf16_hex(self) -> None:
        content = parse_exo(SAMPLE).objects[0].content
        assert content is not None
        assert content.text() == "SKUT"

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        exo = parse_exo(";コメント\n\n[exedit]\nwidth=640\n")
        assert exo.width == 640

    def test_an_empty_file_is_refused(self) -> None:
        with pytest.raises(ExoParseError):
            parse_exo("")


class TestAlias:
    def test_an_alias_has_one_object_and_no_screen_size(self) -> None:
        alias = parse_exo("[0]\nstart=1\nend=30\nlayer=1\n[0.0]\n_name=図形\nサイズ=100\n")
        assert alias.is_alias is True
        assert len(alias.objects) == 1
        assert alias.objects[0].duration == 30

    def test_a_full_file_is_not_an_alias(self) -> None:
        assert parse_exo(SAMPLE).is_alias is False


class TestEncoding:
    def test_utf8_is_preferred(self) -> None:
        text, encoding = decode_bytes("テキスト".encode())
        assert (text, encoding) == ("テキスト", "utf-8")

    def test_a_bom_is_stripped(self) -> None:
        text, encoding = decode_bytes(b"\xef\xbb\xbf" + "あ".encode())
        assert text == "あ"
        assert encoding == "utf-8-sig"

    def test_shift_jis_falls_back(self) -> None:
        # AviUtl1 世代のファイル。UTF-8 としては読めないバイト列。
        text, encoding = decode_bytes("テキスト".encode("cp932"))
        assert (text, encoding) == ("テキスト", "cp932")

    def test_broken_bytes_still_return_something(self) -> None:
        text, _ = decode_bytes(b"\x81\xff\xfe")
        assert isinstance(text, str)

    def test_the_text_field_round_trips(self) -> None:
        encoded = encode_utf16_hex("こんにちは", length=16)
        assert decode_utf16_hex(encoded) == "こんにちは"

    def test_padding_after_the_terminator_is_dropped(self) -> None:
        assert decode_utf16_hex("42003100" + "0000" * 10) == "B1"

    def test_a_broken_hex_string_gives_an_empty_text(self) -> None:
        assert decode_utf16_hex("zzz") == ""


class TestLoading:
    def test_a_shift_jis_file_is_read(self, tmp_path: Path) -> None:
        target = tmp_path / "旧世代.exo"
        target.write_bytes(SAMPLE.encode("cp932"))
        exo = load_exo(target)
        assert exo.encoding == "cp932"
        content = exo.objects[0].content
        assert content is not None
        assert content.name == "テキスト"

    def test_a_utf8_file_is_read(self, tmp_path: Path) -> None:
        target = tmp_path / "新世代.exo2"
        target.write_text(SAMPLE, encoding="utf-8")
        assert load_exo(target).encoding == "utf-8"

    def test_a_missing_file_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ExoParseError, match="開けない"):
            load_exo(tmp_path / "無い.exo")
