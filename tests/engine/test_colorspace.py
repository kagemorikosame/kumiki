"""YUV と RGB の間の色の行列と色のタグ（#61）

色の範囲は SDR の sRGB / Rec.709 書き出しは BT.709 / limited で変換してタグを付け、
読み込みはタグに従い、タグが無ければ大きさから当てる ここが崩れると、書き出した
動画の赤が明るく緑が暗くなったり、タグの無い HD の素材がくすんで見えたりする
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import av
import pytest
from av.video.reformatter import ColorPrimaries, ColorRange, Colorspace, ColorTrc

from kumiki.core.model import MediaItem
from kumiki.engine.cache.proxy import ProxyStore, create_proxy, proxy_codecs
from kumiki.engine.cache.store import CacheStore, media_key
from kumiki.engine.colorspace import source_matrix, tag_bt709, to_bt709
from kumiki.engine.decode import VideoDecoder
from tests.color_bars import (
    AVCOL_SPC_BT470BG,
    AVCOL_SPC_BT709,
    AVCOL_SPC_UNSPECIFIED,
    BT709_YUV,
    COLORS,
    assert_close,
    bars,
    rgb_at_bars,
    write_bars,
    yuv_at_bars,
)


class TestWriting:
    def test_rgb_becomes_bt709_limited(self) -> None:
        # 直す前は swscale の既定の BT.601 で、赤の Y が 63 ではなく 81 になっていた
        rgb = av.VideoFrame.from_ndarray(bars(320, 240), format="rgb24")
        converted = to_bt709(rgb, "yuv420p")
        assert_close(yuv_at_bars(converted), BT709_YUV, tolerance=1)

    def test_the_frame_carries_the_tags(self) -> None:
        rgb = av.VideoFrame.from_ndarray(bars(320, 240), format="rgb24")
        converted = to_bt709(rgb, "yuv420p")
        assert converted.colorspace == AVCOL_SPC_BT709
        assert converted.color_range == ColorRange.MPEG
        assert converted.color_primaries == ColorPrimaries.BT709
        assert converted.color_trc == ColorTrc.BT709

    def test_an_rgb_target_is_left_alone(self) -> None:
        # 行列の無い書式へ行列を持ち込むと、値が YUV の式で曲げられる
        rgb = av.VideoFrame.from_ndarray(bars(320, 240), format="rgb24")
        converted = to_bt709(rgb, "bgr24")
        assert rgb_at_bars(converted.to_ndarray(format="rgb24")) == list(COLORS)

    def test_a_yuv_stream_is_tagged(self, tmp_path: Path) -> None:
        with av.open(str(tmp_path / "out.mp4"), mode="w") as container:
            stream = container.add_stream("libx264", rate=30)
            stream.pix_fmt = "yuv420p"
            tag_bt709(stream)
            context = stream.codec_context
            assert context.color_primaries == ColorPrimaries.BT709
            assert context.color_trc == ColorTrc.BT709
            assert context.colorspace == AVCOL_SPC_BT709
            assert context.color_range == ColorRange.MPEG

    def test_an_rgb_stream_is_not_tagged(self, tmp_path: Path) -> None:
        # 行列の無い絵に行列を書くと、読む側が YUV として変換し直して色が崩れる
        with av.open(str(tmp_path / "out.mov"), mode="w") as container:
            stream = container.add_stream("png", rate=30)
            stream.pix_fmt = "rgb24"
            tag_bt709(stream)
            assert stream.codec_context.colorspace == AVCOL_SPC_UNSPECIFIED


class TestGuessingTheMatrix:
    @pytest.mark.parametrize(
        ("width", "height", "expected"),
        [
            (1920, 1080, Colorspace.ITU709),
            (1280, 720, Colorspace.ITU709),
            # 縦に撮ったスマホの映像 幅は狭いが HD
            (720, 1280, Colorspace.ITU709),
            # PAL の SD 高さ 576 までは SD とみなす
            (720, 576, Colorspace.ITU601),
            (640, 480, Colorspace.ITU601),
        ],
    )
    def test_an_untagged_frame_is_judged_by_size(
        self, width: int, height: int, expected: Colorspace
    ) -> None:
        frame = av.VideoFrame(width, height, "yuv420p")
        assert source_matrix(frame) == expected

    def test_a_tag_wins_over_the_size(self) -> None:
        frame = av.VideoFrame(1920, 1080, "yuv420p")
        frame.colorspace = AVCOL_SPC_BT470BG
        assert source_matrix(frame) is None

    def test_rgb_has_no_matrix(self) -> None:
        assert source_matrix(av.VideoFrame(1920, 1080, "rgb24")) is None


class TestReading:
    @pytest.mark.parametrize(
        ("name", "width", "height", "matrix", "tag"),
        [
            # 直す前はこれが BT.601 で読まれ、赤が (232, 0, 1)・緑が (19, 255, 8) になっていた
            ("hd_untagged_709.mkv", 1280, 720, Colorspace.ITU709, None),
            ("sd_untagged_601.mkv", 640, 480, Colorspace.ITU601, None),
            ("sd_tagged_709.mkv", 640, 480, Colorspace.ITU709, AVCOL_SPC_BT709),
            ("hd_tagged_601.mkv", 1280, 720, Colorspace.ITU601, AVCOL_SPC_BT470BG),
        ],
    )
    def test_colors_come_back(
        self,
        tmp_path: Path,
        name: str,
        width: int,
        height: int,
        matrix: Colorspace,
        tag: int | None,
    ) -> None:
        path = write_bars(tmp_path / name, width, height, matrix=matrix, tag=tag)
        with VideoDecoder(path) as decoder:
            image = decoder.frame_at(Fraction(0))
        assert image is not None
        assert_close(rgb_at_bars(image), COLORS, tolerance=3)


@pytest.mark.skipif(not proxy_codecs(), reason="控えを作れるコーデックが無い")
class TestProxy:
    def test_a_proxy_of_untagged_hd_keeps_its_colors(self, tmp_path: Path) -> None:
        # 控えは SD の大きさなので、タグが無いと BT.601 で読まれる 元の BT.709 のまま
        # 縮めただけだと、控えで見たときだけ色がずれる
        source = write_bars(tmp_path / "hd.mkv", 1280, 720, matrix=Colorspace.ITU709, tag=None)
        target = tmp_path / "proxy.mp4"
        assert create_proxy(source, target, height=240) is not None

        with av.open(str(target)) as container:
            context = container.streams.video[0].codec_context
            assert context.colorspace == AVCOL_SPC_BT709
            assert context.color_range == ColorRange.MPEG
        with VideoDecoder(target) as decoder:
            image = decoder.frame_at(Fraction(0))
        assert image is not None
        # 控えは小ささを取った非可逆なので幅を持たせる 行列を取り違えると 20 以上ずれる
        assert_close(rgb_at_bars(image), COLORS, tolerance=6)

    def test_old_proxies_are_not_reused(self, tmp_path: Path) -> None:
        # 版 1 の控えはタグが無く、今の読み方では色がずれる 鍵が同じだと掴んでしまう
        path = tmp_path / "a.mp4"
        path.write_bytes(b"x")
        media = MediaItem(path=path, duration=Fraction(2))
        store = ProxyStore(CacheStore(tmp_path / "cache"), height=540)
        assert store.key_for(media) != media_key(path, extra="proxy540")
