"""AviUtl スクリプトの制御文字を読む

スクリプトの先頭に並ぶ ``--track0:`` のような行が、そのスクリプトの設定欄を
決めている ここでそれを :mod:`sashimono.effects.spec` のパラメータ仕様へ写す

**この対応付けが P2 の設計の答え合わせになっている** 自前のエフェクトも
AviUtl のスクリプトも同じ ``ParameterSpec`` に載るので、設定 UI もプリセットも
キーフレームも、ここから先は 1 つの実装で足りる

対応する制御文字（AviUtl1 世代）:

===================== ==========================================================
``--track0..3``       数値スライダー ``名前,最小,最大,初期[,刻み]``
``--check0``          チェックボックス ``名前,初期(0/1)``
``--color``           色 ``--color:名前`` で ``color`` 変数に入る
``--file``            ファイル ``file`` 変数に入る
``--dialog``          まとめて指定 ``ラベル,変数=初期値;…``
                      ラベル末尾の ``/chk`` ``/col`` ``/fig`` ``/file`` ``/font``
                      で種類が変わる
``--param``           初期化コード ``a=1;b=2;``
``--label``           見出し
===================== ==========================================================

AviUtl2 世代で増えた ``--track@名前:`` ``--check@名前:`` ``--select@`` ``--value@``
も同じ入口で受ける ``@`` の有無で変数名の決まり方だけが変わる
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from sashimono.effects.spec import (
    CheckSpec,
    ColorSpec,
    FileSpec,
    FontSpec,
    ParameterSpec,
    SelectSpec,
    TextSpec,
    TrackSpec,
    ValueSpec,
)

__all__ = [
    "ScriptHeader",
    "ScriptSection",
    "lua_string",
    "lua_value",
    "parse_control",
    "split_dialog",
    "split_scripts",
]

#: 制御行 ``--track0:...`` の形
_CONTROL = re.compile(
    r"^\s*--(?P<kind>[a-zA-Z]+)(?P<slot>\d+)?(?:@(?P<name>[^\s:]*))?:?(?P<body>.*)$"
)

#: スクリプトの区切り ``@名前`` だけの行
_SECTION = re.compile(r"^\s*@(?P<name>.*?)\s*$")

#: 色を 16 進で書いた値 ``0xffffff``
_HEX_COLOR = re.compile(r"^0[xX][0-9a-fA-F]{1,8}$")


@dataclass(frozen=True, slots=True)
class ScriptHeader:
    """1 つのスクリプトの設定欄"""

    #: 画面に出す名前 ``@`` の行があればその名前
    name: str = ""
    parameters: tuple[ParameterSpec, ...] = ()
    #: ``--param`` に書かれた初期化コード 実行前に流し込む
    setup: str = ""
    #: 見出し（``--label``） パラメータの並びの説明としてそのまま出す
    labels: tuple[str, ...] = ()
    #: 解釈できなかった制御行 互換性の穴として記録に残す
    unknown: tuple[str, ...] = field(default_factory=tuple)

    def spec(self, name: str) -> ParameterSpec | None:
        return next((p for p in self.parameters if p.name == name), None)


@dataclass(frozen=True, slots=True)
class ScriptSection:
    """1 ファイルの中の 1 スクリプト

    AviUtl は ``@名前`` で 1 ファイルに複数のスクリプトを入れられる
    """

    header: ScriptHeader
    #: そのスクリプトの本体（Lua）
    source: str


def split_scripts(
    text: str, *, on_error: Callable[[str, ValueError], None] | None = None
) -> tuple[ScriptSection, ...]:
    """``@名前`` でスクリプトを分ける

    区切りが無ければファイル全体で 1 つ その場合の名前は空にしておき、
    呼び出し側がファイル名を使う

    ``on_error`` を渡すと、設定欄を作れない節（範囲の崩れた値など）はそこへ知らせて
    飛ばし、ほかの節は返す 渡さなければ例外のまま 1 本のファイルに十数本を入れる
    配布物（``@効果集σ.anm`` は 13 本）で、1 節の崩れのために残りまで失うのを避ける
    """
    sections: list[tuple[str, list[str]]] = []
    current: list[str] = []
    name = ""

    for line in text.splitlines():
        matched = _SECTION.match(line)
        # ``@`` で始まっていても、Lua のコードとして意味のある行ではないこと
        if matched is not None and not line.strip().startswith("@@"):
            if current or sections:
                sections.append((name, current))
            name = matched.group("name")
            current = []
            continue
        current.append(line)

    sections.append((name, current))
    built: list[ScriptSection] = []
    for title, body in sections:
        if not (title or "".join(body).strip() or len(sections) == 1):
            continue
        source = "\n".join(body)
        try:
            header = parse_control(source, name=title)
        except ValueError as exc:
            if on_error is None:
                raise
            on_error(title, exc)
            continue
        built.append(ScriptSection(header=header, source=source))
    return tuple(built)


def parse_control(text: str, *, name: str = "") -> ScriptHeader:
    """制御行を読んで設定欄を組み立てる"""
    parameters: list[ParameterSpec] = []
    labels: list[str] = []
    unknown: list[str] = []
    setup: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--"):
            continue
        matched = _CONTROL.match(stripped)
        if matched is None:
            continue

        kind = matched.group("kind").lower()
        slot = matched.group("slot")
        named = matched.group("name")
        body = matched.group("body").strip()

        if kind == "track":
            spec = _track(body, slot, named)
        elif kind == "check":
            spec = _check(body, slot, named)
        elif kind == "color":
            spec = _color(body, named)
        elif kind == "file":
            spec = FileSpec(named or "file", _first(body) or "ファイル")
        elif kind == "font":
            spec = _font(body, named)
        elif kind == "text":
            spec = _text(body, named)
        elif kind in ("figure", "fig"):
            spec = _figure(body, named)
        elif kind in ("select", "list"):
            spec = _select(body, named)
        elif kind == "value":
            spec = _value(body, named)
        elif kind == "dialog":
            parameters.extend(_dialog(body, unknown))
            continue
        elif kind == "param":
            setup.append(body)
            continue
        elif kind in ("label", "information"):
            labels.append(body)
            continue
        else:
            # コメントとの区別が付かないものは記録しない 制御文字として
            # 意味を持つ綴りだけを未対応として残す
            if kind in _KNOWN_UNSUPPORTED:
                unknown.append(stripped)
            continue

        if spec is not None:
            parameters.append(spec)

    return ScriptHeader(
        name=name,
        parameters=tuple(parameters),
        setup="\n".join(setup),
        labels=tuple(labels),
        unknown=tuple(unknown),
    )


#: 意味は分かるが、まだ効かせていない制御文字
#: 記録して一覧に出すためだけに持っている
_KNOWN_UNSUPPORTED = frozenset(
    {
        "trackgroup",
        "twopoint",
        "speed",
        "timecontrol",
        "folder",
        "figurefile",
        "dialogex",
    }
)


def _track(body: str, slot: str | None, named: str | None) -> ParameterSpec | None:
    """``名前,最小,最大,初期[,刻み]``"""
    parts = _split(body)
    if not parts:
        return None
    label = parts[0] or (named or f"track{slot or 0}")
    minimum = _number(parts, 1, 0.0)
    maximum = _number(parts, 2, 100.0)
    default = _number(parts, 3, minimum)
    step = _number(parts, 4, 0.1)
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    default = min(max(default, minimum), maximum)
    name = named or f"track{slot if slot is not None else 0}"
    return TrackSpec(name, label, minimum, maximum, default, step=step or 0.1)


def _check(body: str, slot: str | None, named: str | None) -> ParameterSpec:
    """``名前,初期値`` 初期値は ``0`` / ``1`` のほか ``true`` / ``false`` も来る

    AviUtl2 世代の配布スクリプトは ``--check@bold:太字,true`` と書く 数値だけを
    見ていると、既定で入っているはずのチェックが外れた状態で読み込まれる
    """
    parts = _split(body)
    label = (parts[0] if parts else "") or (named or "チェック")
    raw = parts[1].strip().lower() if len(parts) > 1 else ""
    default = raw in ("1", "true", "on", "yes") or _number(parts, 1, 0.0) != 0
    return CheckSpec(named or f"check{slot if slot is not None else 0}", label, default)


def _font(body: str, named: str | None) -> ParameterSpec:
    parts = _split(body)
    label = parts[0] if parts else "フォント"
    default = lua_string(parts[1]) if len(parts) > 1 and parts[1] else "Yu Gothic UI"
    return FontSpec(named or "font", label or "フォント", default)


def _text(body: str, named: str | None) -> ParameterSpec:
    """``--text@名前:ラベル,既定値`` AviUtl2 世代の文字入力欄"""
    parts = _split(body)
    label = parts[0] if parts else "文字"
    default = lua_string(",".join(parts[1:])) if len(parts) > 1 else ""
    return TextSpec(named or "text", label or "文字", default)


def _color(body: str, named: str | None) -> ParameterSpec:
    parts = _split(body)
    label = parts[0] if parts else "色"
    value = parts[1] if len(parts) > 1 else ""
    return ColorSpec(named or "color", label or "色", _color_value(value, (1.0, 1.0, 1.0, 1.0)))


def _figure(body: str, named: str | None) -> ParameterSpec:
    """図形の種類 AviUtl の図形名をそのまま選択肢にする"""
    parts = _split(body)
    label = parts[0] if parts else "図形"
    return SelectSpec(named or "figure", label or "図形", FIGURE_CHOICES, FIGURE_CHOICES[0][0])


def _select(body: str, named: str | None) -> ParameterSpec | None:
    """選択肢 実際の配布スクリプトには 2 通りの書き方がある

    ``ラベル=既定値,表示名=値,表示名=値``
        AviUtl2 の配布スクリプトで実際に使われている形

    ``ラベル,表示名=値/表示名=値,既定値``
        資料に載っている形

    どちらも受ける 片方だけに対応すると、動かないスクリプトの理由が
    「書き方が違う」になってしまい、原因が分かりにくい
    """
    parts = _split(body)
    if not parts:
        return None

    label, separator, head_default = parts[0].partition("=")
    choices: list[tuple[str, str]] = []
    default = head_default.strip() if separator else ""

    if separator:
        # ラベルに既定値が付いている形 以降がすべて選択肢
        for chunk in parts[1:]:
            _add_choice(choices, chunk)
    else:
        for chunk in parts[1].split("/") if len(parts) > 1 else []:
            _add_choice(choices, chunk)
        default = parts[2].strip() if len(parts) > 2 else ""

    if not choices:
        return None
    if all(default != value for value, _ in choices):
        default = choices[0][0]
    return SelectSpec(named or "select", label.strip() or "選択", tuple(choices), default)


def _add_choice(choices: list[tuple[str, str]], chunk: str) -> None:
    title, separator, value = chunk.strip().partition("=")
    if separator:
        choices.append((value.strip(), title.strip()))
    elif title.strip():
        choices.append((title.strip(), title.strip()))


def _value(body: str, named: str | None) -> ParameterSpec:
    """``--value@`` はスライダーを持たない数値 整数として扱う"""
    parts = _split(body)
    label = parts[0] if parts else "値"
    number = _number(parts, 1, 0.0)
    # nan と inf は整数にできず、int() が例外を投げてスクリプト一覧の走査を止める
    # （inf の OverflowError は節ごとの受け止めもすり抜ける） 0 から始める
    return ValueSpec(named or "value", label or "値", int(number) if math.isfinite(number) else 0)


def _dialog(body: str, unknown: list[str]) -> list[ParameterSpec]:
    """``ラベル,変数=初期値;ラベル/col,色=0xffffff;…``

    ラベルの末尾に付く ``/chk`` ``/col`` などが種類を決める AviUtl の
    仕様がここだけ独特なので、素直に書き下す
    """
    specs: list[ParameterSpec] = []
    for chunk in split_dialog(body):
        item = chunk.strip()
        if not item:
            continue
        label_part, separator, assignment = item.partition(",")
        if not separator:
            continue
        name, has_default, raw_default = assignment.partition("=")
        name = name.strip()
        if not name:
            continue

        label, _, suffix = label_part.strip().partition("/")
        label = label.strip() or name
        suffix = suffix.strip().lower()
        default = raw_default.strip() if has_default else ""
        if default == "nil":
            # 初期値が nil の欄は、Lua の表などを直接書き込むための欄
            # （sigma のスクリプトの ``TRACK,_0=nil`` は、トラックバーの値を表で
            # 差し替える入口） 数のスライダーにすると 0 が入り、``if _0 then _0[1]``
            # が数を表として引いて、sigma の効果が 1 本残らず 1 行目で落ちた
            # こちらの設定欄では表を書けないので、欄を作らず nil のまま渡す
            continue

        specs.append(_dialog_item(name, label, suffix, default, unknown))
    return specs


#: ``--dialog`` の 1 項目 引用符と ``[[ ]]`` の中の ``;`` では切らない
#: 引用符の中では ``\`` と次の 1 文字をまとめて読む（Lua の書き方） そうしないと
#: ``"a\";b"`` の ``\"`` を閉じ引用符と取り違え、中の ``;`` で切ってしまう
_DIALOG_ITEM = re.compile(
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[\[.*?\]\]|[^;])+""",
    re.DOTALL,
)

#: 1 文字で決まる逃がし書き LuaJIT の字句解析（lj_lex.c）と同じ並び
_LUA_SIMPLE_ESCAPES = {
    "a": 7,
    "b": 8,
    "f": 12,
    "n": 10,
    "r": 13,
    "t": 9,
    "v": 11,
    "\\": 92,
    '"': 34,
    "'": 39,
    "\n": 10,
    "\r": 10,
}

#: ``\z`` が読み飛ばす空白 Lua の ``isspace`` と同じ 6 文字
_LUA_SPACES = " \t\n\r\v\f"


class _BadEscapeError(ValueError):
    """Lua なら読み込みで止まる逃がし書き"""


def split_dialog(body: str) -> list[str]:
    """``--dialog`` の本文や ``.exa`` の ``param=`` を項目ごとに切る

    どちらも ``;`` 区切りだが、Lua の文字（``"…"`` ``[[…]]``）の中にも ``;`` は書ける
    素直に ``split(";")`` すると ``[[a;b]]`` が ``[[a`` と ``b]]`` に割れ、初期値が
    壊れたうえ後ろの半分が別の項目として読まれる 制御文字とエイリアスの値で
    切り方がずれないよう、どちらもここを通す
    """
    return [chunk for chunk in _DIALOG_ITEM.findall(body) if chunk.strip()]


def _dialog_item(
    name: str, label: str, suffix: str, default: str, unknown: list[str]
) -> ParameterSpec:
    if suffix == "chk":
        return CheckSpec(name, label, _number([default], 0, 0.0) != 0, as_number=True)
    if suffix == "col":
        return ColorSpec(name, label, _color_value(default, (1.0, 1.0, 1.0, 1.0)))
    if suffix == "fig":
        # AviUtl1 の図形は figure フォルダの画像の名前も取れるので、こちらの一覧に無い
        # 名前も書かれうる そのまま選択肢の既定値にすると SelectSpec が例外を投げ、
        # スクリプト一覧の走査がその 1 本で止まる 記録に残して既定の図形へ寄せる
        figure = lua_string(default)
        if figure and figure not in dict(FIGURE_CHOICES):
            unknown.append(f"--dialog の図形: {figure}")
            figure = ""
        return SelectSpec(name, label, FIGURE_CHOICES, figure or FIGURE_CHOICES[0][0])
    if suffix == "file":
        return FileSpec(name, label)
    if suffix == "font":
        return FontSpec(name, label, lua_string(default) or "Yu Gothic UI")

    # ``[[…]]`` も Lua の文字 sigma の効果集は画像のパスの欄を ``パターン画像,_2=[[]]`` と
    # 書く 数として読むと、パスの欄がスライダーになり、中身の入ったエイリアスを
    # 読むたびに「数として読めない値」と記録されていた
    if default.startswith(('"', "'", "[[")):
        return TextSpec(name, label, lua_string(default))

    # 数値はスライダーにする AviUtl のダイアログは入力欄で、範囲も無いが、
    # こちらではキーフレームを打てる方が使い出がある 範囲は初期値から広めに取る
    value = _number([default], 0, 0.0)
    if not math.isfinite(value):
        # nan を既定値にするとスライダーの範囲の検査で例外になり、inf では範囲が
        # 無限に広がる どちらも数の欄としては持てないので 0 から始める
        unknown.append(f"--dialog の数として持てない初期値: {name}={default}")
        value = 0.0
    span = max(100.0, abs(value) * 10.0)
    step = 0.01 if value != int(value) else 1.0
    return TrackSpec(name, label, -span, span, value, step=step)


#: AviUtl の図形の種類 ``obj.load("figure", ...)`` でも同じ名前を使う
FIGURE_CHOICES: tuple[tuple[str, str], ...] = (
    ("円", "円"),
    ("四角形", "四角形"),
    ("三角形", "三角形"),
    ("五角形", "五角形"),
    ("六角形", "六角形"),
    ("星形", "星形"),
    ("背景", "背景"),
)


def lua_value(spec: ParameterSpec, value: object) -> object:
    """パラメータの値を、AviUtl のスクリプトが期待する型へ

    ここを合わせないと、色が Python のタプルのまま Lua へ渡り、
    ``color / 65536`` のような計算でいきなり落ちる AviUtl では色は
    ``0xRRGGBB`` の数値、選択肢は番号、チェックは真偽値
    """
    if isinstance(spec, ColorSpec) and isinstance(value, tuple):
        red, green, blue = (int(min(max(float(part), 0.0), 1.0) * 255) for part in value[:3])
        return (red << 16) | (green << 8) | blue
    if isinstance(spec, CheckSpec):
        return int(bool(value)) if spec.as_number else bool(value)
    if isinstance(spec, SelectSpec) and isinstance(value, str):
        # 選択肢の識別子が数字なら数値で渡す 配布スクリプトは
        # ``if style == 1 then`` のように数値で比べる
        try:
            return int(value)
        except ValueError:
            return value
    if isinstance(spec, TrackSpec | ValueSpec) and isinstance(value, int | float):
        return float(value)
    return value


def _split(body: str) -> list[str]:
    return [part.strip() for part in body.split(",")]


def _first(body: str) -> str:
    return _split(body)[0] if body else ""


def _number(parts: list[str], index: int, default: float) -> float:
    if index >= len(parts):
        return default
    raw = parts[index].strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def lua_string(value: str) -> str:
    r"""Lua の文字の書き方（``"…"`` ``'…'`` ``[[…]]``）を外す それ以外はそのまま

    引用符の中の逃がし書きは、AviUtl1 が使う LuaJIT の字句解析と同じ規則で戻す
    （``\ddd`` ``\xXX`` ``\z`` ``\u{XXXX}`` と 1 文字のもの） 戻し方を半端にすると
    ``\r`` が ``r`` に、``\065`` が ``065`` になり、スクリプトが受け取る文字が変わる

    **Lua が読み込みで止める書き方（``\q`` や ``\256``）は、引用符ごと書かれたまま返す**
    AviUtl1 ではそのダイアログ自体が読めずに失敗する物で、こちらで推し量って直すと、
    本体では動かない値が動く値として紛れる 書かれたままなら、設定欄を開いた人に見える
    """
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        # ``[[…]]`` は Lua でも逃がし書きを持たないので、こちらだけ戻す
        try:
            return _unescape_lua(text[1:-1])
        except _BadEscapeError:
            return text
    if len(text) >= 4 and text.startswith("[[") and text.endswith("]]"):
        return text[2:-2]
    return text


def _unescape_lua(body: str) -> str:
    r"""引用符の中身の逃がし書きを戻す 読めない書き方なら :class:`_BadEscapeError`

    Lua の文字はバイト列なので、``\ddd`` と ``\xXX`` はバイトを 1 つ作る いったん
    UTF-8 のバイト列に組み、読めないバイトは ``surrogateescape`` で落とさずに持つ
    （``\255`` 1 つでも、1 バイトとして Lua へ返せば元と同じになる）
    """
    out = bytearray()
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char != "\\":
            out += char.encode("utf-8", "surrogateescape")
            index += 1
            continue
        if index + 1 >= length:
            raise _BadEscapeError(body)
        code = body[index + 1]
        index += 2
        if code in _LUA_SIMPLE_ESCAPES:
            out.append(_LUA_SIMPLE_ESCAPES[code])
            # 逃がした改行が ``\r\n`` か ``\n\r`` なら、2 文字で 1 つの改行
            if code in "\n\r" and index < length and body[index] in "\n\r" and body[index] != code:
                index += 1
        elif code.isascii() and code.isdigit():
            end = index
            while end < length and end - index < 2 and body[end].isascii() and body[end].isdigit():
                end += 1
            number = int(code + body[index:end])
            if number > 255:
                raise _BadEscapeError(body)
            out.append(number)
            index = end
        elif code == "x":
            digits = body[index : index + 2]
            if len(digits) != 2 or not all(c in "0123456789abcdefABCDEF" for c in digits):
                raise _BadEscapeError(body)
            out.append(int(digits, 16))
            index += 2
        elif code == "z":
            while index < length and body[index] in _LUA_SPACES:
                index += 1
        elif code == "u":
            index = _unicode_escape(body, index, out)
        else:
            raise _BadEscapeError(body)
    return out.decode("utf-8", "surrogateescape")


def _unicode_escape(body: str, index: int, out: bytearray) -> int:
    r"""LuaJIT 2.1 の ``\u{XXXX}`` 返り値は読み終えた位置"""
    close = body.find("}", index)
    digits = body[index + 1 : close] if body[index : index + 1] == "{" and close > 0 else ""
    if not digits or not all(c in "0123456789abcdefABCDEF" for c in digits):
        raise _BadEscapeError(body)
    number = int(digits, 16)
    # 代用符号（サロゲート U+D800..U+DFFF）と U+10FFFF より先は、LuaJIT 2.1 が
    # invalid escape sequence で読み込みを止める（lupa の LuaJIT で確かめた）
    # UTF-8 の形に作って通すと、AviUtl1 では読めない値がこちらでは読めてしまう
    if number >= 0x110000 or 0xD800 <= number <= 0xDFFF:
        raise _BadEscapeError(body)
    out += chr(number).encode("utf-8")
    return close + 1


def _color_value(
    raw: str, fallback: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """``0xrrggbb`` を 0..1 の組へ 読めなければ ``fallback``"""
    text = raw.strip()
    if not _HEX_COLOR.match(text):
        return fallback
    value = int(text, 16)
    red = (value >> 16) & 0xFF
    green = (value >> 8) & 0xFF
    blue = value & 0xFF
    return (red / 255.0, green / 255.0, blue / 255.0, 1.0)
