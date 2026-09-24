"""AviUtl2 本体の絵と Sashimono の絵を並べて比べる

AviUtl の効果は、項目名を実物（AviUtl2 に作らせたエイリアス）から読み取って写している
読めて描けることはテストで確かめられるが、**AviUtl と同じ絵になるか**は本体で
描いてみないと分からない 値の意味（加速の向き・角度の基準・単位）はここで初めて分かる

使い方（3 段）

1. ``build``   エイリアスを時間をずらして並べた AviUtl2 のプロジェクト（.aup2）と、
               どこに何を置いたかの一覧（manifest.json）を作る
2. AviUtl2 でそのプロジェクトを開き、同じフォルダへ ``aviutl.mp4`` として書き出す
   連番の PNG（``tools/aviutl2_export.py`` が ``aviutl`` フォルダへ書く物）でもよい
   PNG は圧縮で色が痩せないので、両方あれば PNG を使う
3. ``compare`` 書き出した動画と、同じオブジェクトを Sashimono で描いた絵を並べ、
               差の大きい順に一覧（report.html）と並べた絵（PNG）を作る

作業フォルダは既定で ``.work/aviutl-compare`` リポジトリには入れない
（配布物の絵が入るため）

合成フォント（``comfont.aux2``）を使うエイリアスは、プラグインの設定
（``compositefont\\profiles.json``）が無いと本文をそのまま返し、書体の組み替えが
起きない 比べる前に 2 つの副命令で下ごしらえする

* ``profile`` 文字種ごとに書体をはっきり変えた見本の設定を**作業フォルダへ**書き、
  AviUtl2 の置き場へ写す手順を出す 利用者の設定には書きに行かない
* ``preview`` Sashimono 側の絵だけを描く ``--app-data`` に作業フォルダの
  ``appdata`` を渡せば、利用者の設定に触れずに見本の設定で組ませられる

YMM4 側の ``tools/ymm4_compare.py`` と同じ考え方 違うのは、AviUtl の
プロジェクトが INI に似た文字の形なので、**エイリアスの本文をそのまま並べ直す**ところ
値を書き戻さないので、写し間違いが比べる側に混ざらない

差の読み方

* 0 にはならない AviUtl 側は h.264 を通っているので鮮やかな色が痩せる
  （赤 255 が 232、青 255 が 243） 画面いっぱいの原色では 5 前後が残る
* 乱数で絵が決まるもの（集中線・粒子化・ノイズ・砕け散る）は線や粒の向きが
  毎回変わるので、1 枚ずつ引き比べても縮まらない 平均から外して印を付ける
  値の意味は、占有率や明るさのような統計を測って別に確かめる
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sashimono.compat.aviutl.encoding import read_text  # noqa: E402
from sashimono.compat.aviutl.exo import ALIAS_SUFFIXES, ExoParseError, load_exo  # noqa: E402
from sashimono.compat.aviutl.mapping import map_object  # noqa: E402
from sashimono.compat.aviutl.report import CompatibilityReport, global_report  # noqa: E402
from sashimono.compat.catalog import place  # noqa: E402
from sashimono.compat.mapped import MappedObject  # noqa: E402

if TYPE_CHECKING:
    from sashimono.core.model import MediaItem, Project, ProjectSettings

WIDTH, HEIGHT, FPS = 1920, 1080, 60
#: 並べたプロジェクトの音のレート 描く側（compare）も同じ値にそろえる
AUDIO_RATE = 44100
#: 比べる絵の大きさ 書き出しは圧縮されるので、縮めてならしてから比べる
COMPARE_WIDTH, COMPARE_HEIGHT = 480, 270
#: 長さの書いていないエイリアスに当てる長さ（フレーム）
#: 書いてあるものは**そのエイリアス自身の長さ**を使う（:func:`_own_length`）
SLOT = 120

#: 並べられる長さの上限（フレーム 30 秒） これより長いものは**切り詰めずに外す**
#: 切り詰めると AviUtl 側だけが短くなり、進み具合で決まる効果を別の時点で比べてしまう
#: 壊れたファイルが何万フレームを名乗っても書き出しが終わるようにする意味もある
MAX_LENGTH = 1800
#: 枠と枠の間に空ける黒 前のエイリアスの残りが次へ混ざらないように
GAP = 12
DEFAULT_WORK = ROOT / ".work" / "aviutl-compare"

#: プロジェクトの先頭 実物の .aup2（AviUtl2 v2.1.6a の自動控え）から写した
_HEADER = """[project]
version=2010601
file={file}
display.scene=0
preview.scene=0
[scene.0]
scene=0
name=Root
video.width={width}
video.height={height}
video.rate={rate}
video.scale=1
audio.rate={audio_rate}
cursor.frame=0
cursor.layer=0
preview.frame=0
display.frame=0
display.layer=0
display.zoom=10000
display.order=0
display.camera=
"""


@dataclass
class Case:
    """比べるエイリアス 1 本"""

    name: str
    file: str
    #: 元のファイルの**丸ごとの道**（表示用の name や file とは別に持つ）
    #: 名前だけで引き当てると、別のフォルダに同じ名前のエイリアスがあったときに
    #: 片方がもう片方を上書きし、AviUtl の絵と関係の無い中身を並べてしまう
    source: str
    index: int
    start: int
    length: int
    note: str = ""
    blocks: list[list[str]] = field(default_factory=list)

    def sample_frames(self) -> list[int]:
        """比べるフレーム 入りと真ん中と終わりの手前

        登場と退場の効果は端でしか出ないので、両端を必ず見る
        """
        last = self.length - 1
        picks = {min(3, last), last // 2, max(0, last - 4)}
        return sorted(self.start + offset for offset in picks)


def _sections(text: str) -> list[list[str]]:
    """``[Object]`` ``[Object.0]`` … を節ごとの行の並びへ

    本文をそのまま持ち回る 値を読み書きすると、写し間違いが比べる側にも入る
    """
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("[") and line.endswith("]"):
            current = []
            blocks.append(current)
            continue
        if current is not None and line:
            current.append(line)
    return blocks


def _own_length(head: list[str]) -> int:
    """``[Object]`` の ``frame=始まり,終わり`` から、そのエイリアス自身の長さを読む

    **読めた長さはそのまま返す** 1 フレームでも伸ばさない 伸ばすと AviUtl 側だけが
    長い区間になり、比べるフレームがこちらの区間の外へ出て空の絵と突き合わせる

    書いていない・読めないときだけ SLOT を使う 長さが分からないので短く切れない
    """
    for line in head:
        key, _, value = line.partition("=")
        if key.strip() != "frame":
            continue
        # 中間点があると ``frame=開始,中間点…,終了`` と並ぶ 終わりは最後の値
        parts = value.split(",")
        try:
            span = int(parts[-1]) - int(parts[0]) + 1
        except ValueError:
            return SLOT
        return span if span >= 1 else SLOT
    return SLOT


def build_cases(files: list[Path]) -> tuple[list[Case], list[str]]:
    cases: list[Case] = []
    skipped: list[str] = []
    cursor = 0
    for path in files:
        try:
            document = load_exo(path)
        except (ExoParseError, OSError) as exc:
            skipped.append(f"{path.name}（{type(exc).__name__}）")
            continue
        if document.generation < 2:
            # AviUtl1 のエイリアスは AviUtl2 のプロジェクトへそのまま置けない
            skipped.append(f"{path.name}（AviUtl1 世代）")
            continue
        text, _ = read_text(path)
        blocks = _sections(text)
        if len(blocks) < 2:
            skipped.append(f"{path.name}（中身が無い）")
            continue
        # 先頭は [Object]（区間と重ね順） 残りが中身とフィルタ
        #
        # 長さはエイリアス自身が持つものに合わせる 揃えないと、長い方の端で
        # 片側だけが空になって差が跳ね上がる（登場と退場の効き方も区間の長さで決まる）
        length = _own_length(blocks[0])
        if length > MAX_LENGTH:
            # 切り詰めると AviUtl 側だけが短くなり、進み具合や終わり際で決まる
            # 効果を別の時点同士で比べることになる 比べずに外して記録に残す
            skipped.append(f"{path.name}（{length} フレーム 上限の {MAX_LENGTH} を超える）")
            continue
        cases.append(
            Case(
                name=path.stem,
                file=path.name,
                # 丸ごとの道にしてから持つ 相対のまま書くと、別の場所から
                # compare を走らせたときに元ファイルを見失う
                source=str(path.resolve()),
                index=len(cases),
                start=cursor,
                length=length,
                blocks=blocks[1:],
            )
        )
        cursor += length + GAP
    return cases, skipped


def write_project(cases: list[Case], target: Path) -> None:
    """並べたプロジェクトを書く 1 本を 1 レイヤーの 1 区間に置く"""
    out = [_HEADER.format(file=target, width=WIDTH, height=HEIGHT, rate=FPS, audio_rate=AUDIO_RATE)]
    for number, case in enumerate(cases):
        end = case.start + case.length - 1
        out.append(f"[{number}]")
        out.append("layer=0")
        out.append(f"frame={case.start},{end}")
        for index, block in enumerate(case.blocks):
            out.append(f"[{number}.{index}]")
            out.extend(block)
    target.write_text("\n".join(out) + "\n", encoding="utf-8")


def default_files() -> list[Path]:
    """比べる相手 手元のエイリアスと、落としてきた配布物"""
    roots: list[Path] = []
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        roots.append(Path(program_data) / "aviutl2" / "Alias")
    roots.append(ROOT / "tests" / "fixtures" / "aviutl")
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for suffix in ALIAS_SUFFIXES:
            found.extend(sorted(root.rglob(f"*{suffix}")))
    return sorted(set(found))


def command_build(arguments: argparse.Namespace) -> int:
    work: Path = arguments.work
    work.mkdir(parents=True, exist_ok=True)
    files = [Path(item) for item in arguments.files] or default_files()
    if not files:
        print("エイリアスが見つかりません")
        return 1

    cases, skipped = build_cases(files)
    if not cases:
        print("並べられるエイリアスがありません")
        return 1

    project = work / "compare.aup2"
    write_project(cases, project)
    (work / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": case.name,
                        "file": case.file,
                        "source": case.source,
                        "index": case.index,
                        "start": case.start,
                        "length": case.length,
                        "note": case.note,
                    }
                    for case in cases
                ],
                "skipped": skipped,
                "files": [str(path) for path in files],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    last = cases[-1].start + cases[-1].length
    print(f"{len(cases)} 本を並べた（{last} フレーム ＝ {last / FPS:.1f} 秒）")
    print(f"AviUtl2 で {project} を開き、{work / 'aviutl.mp4'} へ書き出してください")
    for line in skipped:
        print(f"  飛ばした: {line}")
    return 0


#: 合成フォントの見本で文字種ごとに当てる書体と、その書体のファイル（Windows の Fonts）
#:
#: 組み替えが起きたことが絵で一目で分かるよう、隣り合う文字種は系統から変える
#: （ひらがなは明朝、カタカナはメイリオ、漢字は MS ゴシック） Sashimono の既定の
#: 書体（Yu Gothic UI）は使わない 組み替えが起きずに既定で描かれたときと
#: 見分けが付かなくなる ファイル名はこの PC の Fonts で確かめた Windows 標準の物
#:
#: 書体名は AviUtl2 の書体の一覧に出る名前（日本語の名前を持つ書体は日本語）で書く
#: ``Yu Mincho`` ``Meiryo`` ``MS Gothic`` と英語で書いた見本では、AviUtl2 v2.1.6a が
#: どれも見つけられず、既定の Yu Gothic UI で描いた（2026-09-24 に書き出して測った #108）
COMPOSITE_FONTS: tuple[tuple[str, str, str], ...] = (
    ("western", "Times New Roman", "times.ttf"),
    ("hiragana", "游明朝", "yumin.ttf"),
    ("katakana", "メイリオ", "meiryo.ttc"),
    ("kanji", "ＭＳ ゴシック", "msgothic.ttc"),
    ("digit", "Consolas", "consola.ttf"),
    ("symbol", "Segoe UI", "segoeui.ttf"),
    ("other", "Arial", "arial.ttf"),
)
#: 見本のプロファイル名 配布エイリアスはどちらも ``profile="default"`` を引く
COMPOSITE_PROFILE = "default"
#: 利用者の AviUtl2 の置き場から見た、合成フォントの設定の道
COMPOSITE_SETTINGS = Path("compositefont") / "profiles.json"


def _adjustment(family: str) -> dict[str, object]:
    """1 つの文字種の設定 項目は実物（comfont.aux2 v0.2.0）が受け付けた形のまま

    大きさ・位置・字間は動かさない 書体と一緒に動かすと、絵の違いが書体から
    来たのか大きさから来たのかを見分けられない
    """
    return {
        "font_family": family,
        "fallback_font_families": [],
        "size_ratio": 1.0,
        "baseline_shift_em": 0.0,
        "tracking_adjust_em": 0.0,
        "metric_unit": "percent",
        "size_px": 0.0,
        "baseline_shift_px": 0.0,
        "tracking_adjust_px": 0.0,
        "vertical_scale_ratio": 1.0,
        "horizontal_scale_ratio": 1.0,
    }


def composite_profiles() -> dict[str, object]:
    """合成フォントの見本の設定（``profiles.json`` の中身）"""
    profile: dict[str, object] = {"name": COMPOSITE_PROFILE}
    for key, family, _file in COMPOSITE_FONTS:
        profile[key] = _adjustment(family)
        profile[f"{key}_fallbacks"] = []
    return {"schema_version": 1, "profiles": [profile]}


def user_composite_settings() -> Path | None:
    """利用者の AviUtl2 が読む合成フォントの設定の道 **読むだけで書かない**"""
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        return None
    return Path(program_data) / "aviutl2" / COMPOSITE_SETTINGS


def missing_fonts() -> list[str]:
    """見本に使う書体のうち、この PC の Fonts に無い物"""
    windows = os.environ.get("WINDIR") or os.environ.get("SYSTEMROOT")
    if not windows:
        return [family for _key, family, _file in COMPOSITE_FONTS]
    fonts = Path(windows) / "Fonts"
    return [family for _key, family, file in COMPOSITE_FONTS if not (fonts / file).is_file()]


#: 見本を置く前の利用者の設定の控えに付ける名前の後ろ
#:
#: 日時を入れずに 1 つに決めておき、**既にあれば取り直さない** 日時で名前を分けると
#: 上書きはしないが控えが溜まり、戻す手順を出すときにどれが見本を置く前の物かを
#: 道具が当て推量で選ぶことになる 1 つに決めて取り直さなければ、控えは常に
#: 最初に置き換える前の物になる
BACKUP_SUFFIX = ".sashimono-bak"


def backup_of(target: Path) -> Path:
    """見本を置く前の設定の控えの道"""
    return target.with_name(target.name + BACKUP_SUFFIX)


def _holds_sample(target: Path, sample: Path) -> bool:
    """置き場の設定が見本と同じ中身か（見本が置かれたままか）"""
    try:
        return target.read_bytes() == sample.read_bytes()
    except OSError:
        return False


def profile_guide(sample: Path, target: Path | None) -> list[str]:
    """見本の設定を AviUtl2 の置き場へ写す手順と、終わったあとに戻す手順 写すのは本人

    道具が自分で置きに行かないのは、利用者が合成フォントのエディターで作った
    設定を黙って上書きしうるため 控えを取るかどうかも本人が決める

    戻す手順まで出す 置く手順だけだと、比べ終わったあとも見本の組み替えが
    普段の AviUtl2（と Sashimono）に残る 手順をなぞり直しても元の設定を
    失わないよう、控えが既にあれば取り直さない（:data:`BACKUP_SUFFIX`）
    """
    lines = [f"見本の設定を書いた: {sample}"]
    if target is None:
        lines.append("PROGRAMDATA が無いので、AviUtl2 の置き場が分からない")
        return lines
    backup = backup_of(target)
    placed = target.exists() and _holds_sample(target, sample)
    lines.append("AviUtl2 に同じ組み替えをさせるには、次を本人が手で行う")
    lines.append("（AviUtl2 を開いていれば閉じてから 設定を読み直す時機は確かめていない）")
    if backup.exists():
        # 取り直すと、置いたままの見本を「元の設定」として控えに上書きしてしまう
        lines.append(f"  控え {backup} が既にあるので取り直さない")
        if target.exists() and not placed:
            lines.append(
                "  控えを取ったあとに置き場の設定を変えたなら、先に控えを別の名前へ移しておく"
            )
    elif target.exists() and not placed:
        lines.append(f"  既に {target} があるので、先に控えを取る")
        lines.append(f"  Copy-Item -LiteralPath {_ps(target)} -Destination {_ps(backup)}")
    elif not target.exists():
        lines.append(f"  {target} はまだ無い（置き場のフォルダから作る）")
        lines.append(f"  New-Item -ItemType Directory -Force -Path {_ps(target.parent)}")
    if placed:
        lines.append(f"  {target} には見本が置かれたまま（写し直さなくてよい）")
    else:
        lines.append(f"  Copy-Item -LiteralPath {_ps(sample)} -Destination {_ps(target)}")

    lines.append("比べ終わったら、AviUtl2 を閉じてから元へ戻す")
    if backup.exists() or (target.exists() and not placed):
        lines.append(f"  Move-Item -Force -LiteralPath {_ps(backup)} -Destination {_ps(target)}")
    else:
        # 見本と同じ中身を元から持っていた場合と見分けられない 控えが無いのは
        # 見本を置く前に設定が無かったときだけのはずだが、決め打ちせず本人に確かめさせる
        lines.append(
            "  （控えが無い 見本を置く前に設定が無かったなら消す 元から同じ中身を"
            "持っていたなら消さない）"
        )
        lines.append(f"  Remove-Item -LiteralPath {_ps(target)}")
    return lines


def _ps(path: Path) -> str:
    """PowerShell の命令に書くパス 単一引用符で囲み、中の ``'`` は ``''`` にする

    二重引用符だと ``$`` が変数として展開されて名前が欠ける ``-Path`` は ``[`` ``]`` を
    ワイルドカードとして読むので、``作業[2]`` のような置き場では見本も控えも見つからない
    命令の側は ``-LiteralPath`` で受ける
    """
    return "'" + str(path).replace("'", "''") + "'"


def command_profile(arguments: argparse.Namespace) -> int:
    work: Path = arguments.work
    sample = work / "appdata" / COMPOSITE_SETTINGS
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(
        json.dumps(composite_profiles(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for line in profile_guide(sample, user_composite_settings()):
        print(line)
    for family in missing_fonts():
        print(f"  注意: {family} がこの PC の Fonts に無い 組み替えても既定の書体で描かれる")
    print(f"Sashimono 側の絵は preview --app-data {sample.parent.parent} で描ける")
    return 0


#: 見本の絵の名前に使うエイリアスの名前の長さ 頭の番号と拡張子を足しても、
#: ファイル名の上限（NTFS で UTF-16 の 255 単位）に収まるようにする 番号で見分けが付くので、
#: 切っても別のエイリアスの絵と取り違えない
PREVIEW_NAME_LIMIT = 200


def preview_name(case: Case) -> str:
    """見本の絵の名前 並べた位置（``compare`` の ``images/`` と同じ）を頭に付ける

    名前だけにすると、別のフォルダにある同じ名前のエイリアスの絵が先の絵を
    上書きし、描いたはずの 1 本が黙って消える 並べた位置は 1 本ごとに違う
    """
    return f"{case.start:06d}_{_fit_utf16(case.name, PREVIEW_NAME_LIMIT)}.png"


def _fit_utf16(text: str, limit: int) -> str:
    """UTF-16 の単位で ``limit`` に収まる所まで切る

    NTFS の上限は UTF-16 の単位で数える 絵文字のような補助面の文字は 1 文字で
    2 単位なので、Python の文字数で切ると、名前だけで上限を超えて絵が作られない
    文字の途中（サロゲートの片割れ）では切らない
    """
    used = 0
    for index, character in enumerate(text):
        used += 2 if ord(character) > 0xFFFF else 1
        if used > limit:
            return text[:index]
    return text


def command_preview(arguments: argparse.Namespace) -> int:
    """AviUtl2 の書き出しを待たずに、Sashimono 側の絵だけを描く"""
    from sashimono.core.model import ProjectSettings
    from sashimono.core.timebase import FrameRate
    from sashimono.engine.render import FrameRenderer

    _use_app_data(arguments.app_data)
    work: Path = arguments.work
    files = [Path(item) for item in arguments.files] or default_files()
    cases, skipped = build_cases(files)
    if not cases:
        print("描けるエイリアスがありません")
        return 1
    settings = ProjectSettings(
        width=WIDTH, height=HEIGHT, frame_rate=FrameRate(FPS), sample_rate=AUDIO_RATE
    )
    target = work / "preview"
    target.mkdir(parents=True, exist_ok=True)
    report = CompatibilityReport()
    renderer: FrameRenderer | None = None
    failed = 0
    try:
        for case in cases:
            objects = [
                item
                for obj in load_exo(Path(case.source)).objects
                if (item := map_object(obj, settings.frame_rate, report=report)) is not None
            ]
            project, missing = placed_project(objects, settings, case.start)
            if missing:
                # 素材の無い絵を見本として残すと、描けたものと取り違える
                skipped.append(lacking_note(case.name, missing))
                continue
            if renderer is None:
                renderer = FrameRenderer(project)
            else:
                renderer.set_project(project)
            frames = case.sample_frames()
            frame = frames[len(frames) // 2]
            image = np.asarray(renderer.render(frame))[..., :3]
            # 真っ黒を「描けた」と取り違えないよう、光っている画素を数えて出す
            lit = int((image.max(axis=2) > 8).sum())
            out = target / preview_name(case)
            if not _save_png(np.ascontiguousarray(image), out):
                failed += 1
                print(
                    f"{case.name}: フレーム {frame} 光っている画素 {lit} → 保存できなかった {out}"
                )
                continue
            print(f"{case.name}: フレーム {frame} 光っている画素 {lit} → {out}")
    finally:
        if renderer is not None:
            renderer.close()
    for line in skipped:
        print(f"  飛ばした: {line}")
    for line in (*report.lines(), *global_report.lines()):
        print(f"  記録: {line}")
    # 絵が書けなかったのに 0 で終えると、描けたものとして次の手順へ進んでしまう
    return 1 if failed else 0


def _probe_or_none(path: Path) -> MediaItem | None:
    from sashimono.engine.decode import ProbeError, probe_media

    try:
        return probe_media(path)
    except ProbeError:
        return None


def placed_project(
    objects: list[MappedObject], settings: ProjectSettings, start: int
) -> tuple[Project, tuple[str, ...]]:
    """写した結果を素材の登録から置くまで済ませたプロジェクトと、見つからなかった素材の道

    素材を登録せずに置くと、画像や動画のクリップが素材を持たず何も映らない
    （画像の拡大率の見本が真っ黒のまま差だけ大きく出た #167） 見つからなかった素材が
    あれば、その見本は素材の無い絵になるので、呼ぶ側は比べた結果に入れない
    """
    from sashimono.compat.catalog import gather_media
    from sashimono.core.model import Project

    project = Project.create(settings)
    plan = gather_media(objects, project, _probe_or_none)
    for command in [*plan.commands, *place(objects, project, at_frame=start, media=plan.media)]:
        project = command.apply(project)
    return project, plan.missing


def lacking_note(name: str, missing: tuple[str, ...]) -> str:
    """素材が見つからずに比べなかった見本の知らせ"""
    return f"{name}（素材が見つからないので比べない: {', '.join(missing)}）"


def _use_app_data(path: Path | None) -> None:
    """汎用プラグインへ渡す置き場を差し替える 描き始める前に呼ぶ

    プラグインは最初に読んだときの置き場を持ち続けるので、1 枚でも描いた後では遅い
    """
    if path is None:
        return
    from sashimono.compat.aviutl import plugin

    plugin.set_app_data_path(path.resolve())
    print(f"汎用プラグインへ渡す置き場: {path.resolve()}")


def _shrink(image: np.ndarray) -> np.ndarray:
    """面積の平均で縮める 1920x1080 から 4 分の 1 ならちょうど割り切れる"""
    height, width = image.shape[:2]
    fy, fx = height // COMPARE_HEIGHT, width // COMPARE_WIDTH
    cropped = image[: COMPARE_HEIGHT * fy, : COMPARE_WIDTH * fx, :3].astype(np.float32)
    return cropped.reshape(COMPARE_HEIGHT, fy, COMPARE_WIDTH, fx, 3).mean(axis=(1, 3))


def _save_png(image: np.ndarray, target: Path) -> bool:
    """RGB の配列を PNG へ 画像のためだけに Pillow を足さず、入っている Qt で書く

    書けたかを返す Qt は失敗しても例外を投げないので、見ないと「書いた」と
    出したのに絵が無い、になる（名前が長すぎる・置き場に書けない など）
    """
    from PySide6.QtGui import QImage

    height, width = image.shape[:2]
    data = np.ascontiguousarray(image)
    image_ = QImage(data.data, width, height, width * 3, QImage.Format.Format_RGB888)
    return bool(image_.save(str(target)))


def _video_frames(video: Path, wanted: set[int]) -> dict[int, np.ndarray]:
    import av

    found: dict[int, np.ndarray] = {}
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        rate = float(stream.average_rate or FPS)
        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                continue
            index = round(float(frame.pts * frame.time_base) * rate)
            if index in wanted:
                found[index] = frame.to_ndarray(format="rgb24")
            if len(found) == len(wanted):
                break
    return found


#: 連番の PNG を置くフォルダの名前（作業フォルダの中） ``tools/aviutl2_export.py`` の出力先
PNG_FOLDER = "aviutl"
_PNG_NUMBER = re.compile(r"(\d+)\.png$", re.IGNORECASE)


def png_frames(folder: Path, wanted: set[int]) -> dict[int, np.ndarray]:
    """連番の PNG から欲しいフレームを読む 背景は黒にして返す（動画の書き出しと同じ見え方）

    AviUtl2 の連番ファイル出力は、名前の後ろにフレームの番号を付ける（``frame000.png``）
    桁の数は長さで変わるので、名前の並びではなく番号の値で引く
    透明色ありで書き出すと、何も無い所は透明になる 黒の上に重ねないと、同じ絵でも
    Sashimono 側（黒の背景）との差が出てしまう
    """
    from PySide6.QtGui import QImage

    found: dict[int, np.ndarray] = {}
    for path in folder.iterdir():
        match = _PNG_NUMBER.search(path.name)
        if match is None or int(match.group(1)) not in wanted:
            continue
        image = QImage(str(path)).convertToFormat(QImage.Format.Format_RGBA8888)
        if image.isNull():
            continue
        rgba = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4)
        alpha = rgba[..., 3:4].astype(np.float32) / 255.0
        found[int(match.group(1))] = np.rint(rgba[..., :3] * alpha).astype(np.uint8)
    return found


def reference_frames(work: Path, wanted: set[int]) -> dict[int, np.ndarray] | None:
    """AviUtl2 の絵 連番の PNG があればそれを、無ければ動画を読む どちらも無ければ ``None``"""
    folder = work / PNG_FOLDER
    if folder.is_dir() and any(folder.glob("*.png")):
        return png_frames(folder, wanted)
    video = work / "aviutl.mp4"
    if video.exists():
        return _video_frames(video, wanted)
    return None


def command_compare(arguments: argparse.Namespace) -> int:
    from sashimono.core.model import ProjectSettings
    from sashimono.core.timebase import FrameRate
    from sashimono.engine.render import FrameRenderer

    _use_app_data(arguments.app_data)
    work: Path = arguments.work
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    cases = manifest["cases"]
    if arguments.only:
        words = [word for word in arguments.only.split(",") if word]
        cases = [case for case in cases if any(word in case["name"] for word in words)]

    wanted: set[int] = set()
    for raw in cases:
        wanted.update(Case(**raw).sample_frames())
    references = reference_frames(work, wanted)
    if references is None:
        print(
            f"{work / 'aviutl.mp4'} も {work / PNG_FOLDER} の連番の PNG もありません"
            " AviUtl2 で書き出してから走らせてください"
        )
        return 1

    # 音のレートは並べたプロジェクトの見出し（audio.rate=44100）と合わせる 音声波形は
    # 1 画素 1 サンプルなので、レートが違うと同じ横幅に入る時間が変わる
    settings = ProjectSettings(
        width=WIDTH,
        height=HEIGHT,
        frame_rate=FrameRate(FPS),
        sample_rate=AUDIO_RATE,
        blending=arguments.blending,
    )
    images = work / "images"
    images.mkdir(exist_ok=True)
    rows: list[tuple[float, str, str, int, str, str]] = []
    lacking: list[str] = []
    report = CompatibilityReport()
    renderer: FrameRenderer | None = None
    try:
        for raw in cases:
            case = Case(**raw)
            # 並べたときのファイルをそのまま読む 名前で引き直すと、同じ名前の
            # エイリアスが別のフォルダにあったときに違う中身と突き合わせる
            source = Path(case.source)
            if not source.exists():
                print(f"{case.name}: {source} が見つかりません 並べ直してください")
                return 1
            objects = [
                item
                for obj in load_exo(source).objects
                if (item := map_object(obj, settings.frame_rate, report=report)) is not None
            ]
            project, missing = placed_project(objects, settings, case.start)
            if missing:
                # 素材の無い絵を比べると、差が素材の欠けを測っただけの数になり、平均を汚す
                # 配布物は作者の機械の道を持つので珍しくない 止めずに外して一覧に載せる
                lacking.append(lacking_note(case.name, missing))
                print(lacking[-1])
                continue
            random_based = _is_random(objects)
            if renderer is None:
                renderer = FrameRenderer(project)
            else:
                renderer.set_project(project)
            for frame in case.sample_frames():
                reference = references.get(frame)
                if reference is None:
                    # 黙って飛ばすと、途中までしか書き出していない動画でも
                    # 「残りは全部合っている」ように見える平均が出てしまう
                    print(
                        f"{case.name}: 書き出した動画にフレーム {frame} がありません"
                        " 並べ直したプロジェクトを書き出してください"
                    )
                    return 1
                ours = renderer.render(frame)
                a, b = _shrink(reference), _shrink(ours)
                difference = float(np.abs(a - b).mean())
                stem = f"{case.start:06d}_{frame:06d}"
                side = np.concatenate([a, b, np.abs(a - b) * 3.0], axis=1)
                note = case.note
                if not _save_png(np.clip(side, 0, 255).astype(np.uint8), images / f"{stem}.png"):
                    # 表には載るのに絵が無い、を見分けられるよう、備考に残す
                    note = (note + " " if note else "") + "並べた絵を保存できなかった"
                if random_based:
                    note = (note + " " if note else "") + RANDOM_NOTE
                rows.append((difference, case.name, case.file, frame, stem, note))
    finally:
        if renderer is not None:
            renderer.close()

    rows.sort(reverse=True)
    steady = [row for row in rows if RANDOM_NOTE not in row[5]]
    # 写すときの記録と、描くときの記録を両方載せる スクリプトが描画の途中で
    # 諦めた（``obj.getpixeldata`` など）のは後者にしか出ない
    skipped = [*manifest.get("skipped", []), *lacking]
    _write_report(work / "report.html", rows, skipped, report, global_report)
    (work / "report.json").write_text(
        json.dumps(
            [
                {"difference": d, "name": n, "file": f, "frame": fr, "image": s, "note": note}
                for d, n, f, fr, s, note in rows
            ],
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    if steady:
        average = sum(row[0] for row in steady) / len(steady)
        print(f"{len(steady)} 枚を比べた 平均の差 {average:.1f}")
    if len(rows) != len(steady):
        print(f"（{len(rows) - len(steady)} 枚は乱数で絵が決まるので平均に入れていない）")
    for difference, name, _, frame, _, _ in steady[: arguments.top]:
        print(f"{difference:6.1f}  {name}  フレーム {frame}")
    return 0


#: 乱数で絵が決まる中身と効果 線や粒の向きが毎回変わるので、
#: 1 枚ずつ引き比べても差は縮まらない 値の意味は別に測って確かめる
RANDOM_SHAPES = frozenset({"concentration", "starfield"})
RANDOM_EFFECTS = frozenset({"noise", "particles", "crash", "scatter", "inout_random_direction"})

#: 設定しだいで乱数になる効果 値を見ないと決まらない
#:
#: 一律に乱数として外すと、決まった動きをする設定（点滅の間隔を一定にする、
#: 落ちる遅れを 0 にする）まで比べられなくなる
RANDOM_WHEN: dict[str, str] = {
    # 落ちながら登場 間隔 が 0 でなければ落ち始めが乱数で遅れる
    "inout_fall": "interval",
}

#: 乱数のものに付ける印 平均から外すかどうかの判定にも使う
RANDOM_NOTE = "乱数（1 枚ずつは比べない）"


def _is_random(objects: list[MappedObject]) -> bool:
    """この見本が乱数で絵を決めるか"""
    for item in objects:
        source = item.clip.source
        if source is not None and str(source.params.get("shape", "")) in RANDOM_SHAPES:
            return True
        for effect in item.clip.effects:
            if effect.kind in RANDOM_EFFECTS:
                return True
            name = RANDOM_WHEN.get(effect.kind)
            if name is not None and _randomising(effect.params.get(name)):
                return True
    return False


def _randomising(value: object) -> bool:
    """その値が「乱数で決める」側か

    チェックは**外れているとき**が乱数（点滅の「一定にする」）
    数は 0 でなければ乱数（落ちる遅れの幅）

    動く値は**キーフレームまで見る** 先頭が 0 でも途中で 0 でなくなれば、
    そのフレームは乱数で絵が決まる static だけを見ると、動く遅れを持つ
    見本が「決まった動き」に紛れ込んで、平均に乱数の差が混ざる
    """
    if isinstance(value, bool):
        return not value
    numbers = [getattr(value, "static", value)]
    numbers.extend(keyframe.value for keyframe in getattr(value, "keyframes", ()))
    return any(isinstance(n, int | float) and float(n) != 0.0 for n in numbers)


def _write_report(
    target: Path,
    rows: list[tuple[float, str, str, int, str, str]],
    skipped: list[str],
    report: CompatibilityReport,
    while_drawing: CompatibilityReport,
) -> None:
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        "<title>AviUtl と Sashimono の比べ</title>",
        "<style>body{font-family:sans-serif;background:#111;color:#eee}",
        "img{image-rendering:pixelated;width:1440px}",
        "td{padding:4px 8px;vertical-align:top}</style>",
        "<h1>AviUtl と Sashimono の比べ</h1>",
        "<p>左が AviUtl 本体、真ん中が Sashimono、右が差（3 倍に強めて表示）</p>",
        "<p>差は 0 にはならない AviUtl 側は h.264 を通っているので、"
        "鮮やかな色が痩せる（赤 255 が 232、青 255 が 243 になる） "
        "画面いっぱいの原色では、これだけで 5 前後の差が残る</p>",
        "<table>",
    ]
    for difference, name, file, frame, stem, note in rows:
        label = html.escape(f"{name}（{file}） フレーム {frame}")
        if note:
            label += f" <small>{html.escape(note)}</small>"
        parts.append(
            f"<tr><td>{difference:6.1f}</td><td>{label}<br><img src='images/{stem}.png'></td></tr>"
        )
    parts.append("</table>")
    if skipped:
        parts.append("<h2>並べなかったもの</h2><ul>")
        parts.extend(f"<li>{html.escape(line)}</li>" for line in skipped)
        parts.append("</ul>")
    if report.lines():
        parts.append("<h2>写せていないもの</h2><ul>")
        parts.extend(f"<li>{html.escape(line)}</li>" for line in report.lines())
        parts.append("</ul>")
    if while_drawing.lines():
        parts.append("<h2>描くときに諦めたもの</h2><ul>")
        parts.extend(f"<li>{html.escape(line)}</li>" for line in while_drawing.lines())
        parts.append("</ul>")
    target.write_text("\n".join(parts), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK, help="作業フォルダ")
    subparsers = parser.add_subparsers(dest="command", required=True)

    builder = subparsers.add_parser("build", help="並べたプロジェクトを作る")
    builder.add_argument("files", nargs="*", help="エイリアス 省略すると手元のものを全部")
    builder.set_defaults(func=command_build)

    comparer = subparsers.add_parser("compare", help="書き出した動画と比べる")
    comparer.add_argument("--only", default="", help="名前にこの語を含むものだけ カンマ区切り")
    comparer.add_argument("--top", type=int, default=20, help="差の大きい順に何件出すか")
    # 既定は新しく作るプロジェクトと同じ sRGB（AviUtl2 の混ぜ方） リニアを選べば、
    # 設定ができる前に保存したプロジェクトの見え方で比べられる
    comparer.add_argument(
        "--blending", choices=("srgb", "linear"), default="srgb", help="半透明の重ね合わせ"
    )
    comparer.set_defaults(func=command_compare)

    profiler = subparsers.add_parser("profile", help="合成フォントの見本の設定を作業フォルダへ書く")
    profiler.set_defaults(func=command_profile)

    previewer = subparsers.add_parser("preview", help="Sashimono 側の絵だけを描く")
    previewer.add_argument("files", nargs="*", help="エイリアス 省略すると手元のものを全部")
    previewer.set_defaults(func=command_preview)

    # 利用者の設定に触れずに合成フォントを組ませるための口 省略すると利用者の置き場
    for sub in (comparer, previewer):
        sub.add_argument(
            "--app-data",
            type=Path,
            default=None,
            help="汎用プラグインへ渡す置き場（profile が書いた appdata）",
        )

    arguments = parser.parse_args()
    result: int = arguments.func(arguments)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
