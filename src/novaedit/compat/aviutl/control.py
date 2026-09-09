"""AviUtl スクリプトの制御文字を読む。

スクリプトの先頭に並ぶ ``--track0:`` のような行が、そのスクリプトの設定欄を
決めている。ここでそれを :mod:`novaedit.effects.spec` のパラメータ仕様へ写す。

**この対応付けが P2 の設計の答え合わせになっている。** 自前のエフェクトも
AviUtl のスクリプトも同じ ``ParameterSpec`` に載るので、設定 UI もプリセットも
キーフレームも、ここから先は 1 つの実装で足りる。

対応する制御文字（AviUtl1 世代）:

===================== ==========================================================
``--track0..3``       数値スライダー。``名前,最小,最大,初期[,刻み]``
``--check0``          チェックボックス。``名前,初期(0/1)``
``--color``           色。``--color:名前`` で ``color`` 変数に入る
``--file``            ファイル。``file`` 変数に入る
``--dialog``          まとめて指定。``ラベル,変数=初期値;…``。
                      ラベル末尾の ``/chk`` ``/col`` ``/fig`` ``/file`` ``/font``
                      で種類が変わる
``--param``           初期化コード。``a=1;b=2;``
``--label``           見出し
===================== ==========================================================

AviUtl2 世代で増えた ``--track@名前:`` ``--check@名前:`` ``--select@`` ``--value@``
も同じ入口で受ける。``@`` の有無で変数名の決まり方だけが変わる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from novaedit.effects.spec import (
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
    "lua_value",
    "parse_control",
    "split_scripts",
]

#: 制御行。``--track0:...`` の形。
_CONTROL = re.compile(
    r"^\s*--(?P<kind>[a-zA-Z]+)(?P<slot>\d+)?(?:@(?P<name>[^\s:]*))?:?(?P<body>.*)$"
)

#: スクリプトの区切り。``@名前`` だけの行。
_SECTION = re.compile(r"^\s*@(?P<name>.*?)\s*$")

#: 色を 16 進で書いた値。``0xffffff``。
_HEX_COLOR = re.compile(r"^0[xX][0-9a-fA-F]{1,8}$")


@dataclass(frozen=True, slots=True)
class ScriptHeader:
    """1 つのスクリプトの設定欄。"""

    #: 画面に出す名前。``@`` の行があればその名前。
    name: str = ""
    parameters: tuple[ParameterSpec, ...] = ()
    #: ``--param`` に書かれた初期化コード。実行前に流し込む。
    setup: str = ""
    #: 見出し（``--label``）。パラメータの並びの説明としてそのまま出す。
    labels: tuple[str, ...] = ()
    #: 解釈できなかった制御行。互換性の穴として記録に残す。
    unknown: tuple[str, ...] = field(default_factory=tuple)

    def spec(self, name: str) -> ParameterSpec | None:
        return next((p for p in self.parameters if p.name == name), None)


@dataclass(frozen=True, slots=True)
class ScriptSection:
    """1 ファイルの中の 1 スクリプト。

    AviUtl は ``@名前`` で 1 ファイルに複数のスクリプトを入れられる。
    """

    header: ScriptHeader
    #: そのスクリプトの本体（Lua）。
    source: str


def split_scripts(text: str) -> tuple[ScriptSection, ...]:
    """``@名前`` でスクリプトを分ける。

    区切りが無ければファイル全体で 1 つ。その場合の名前は空にしておき、
    呼び出し側がファイル名を使う。
    """
    sections: list[tuple[str, list[str]]] = []
    current: list[str] = []
    name = ""

    for line in text.splitlines():
        matched = _SECTION.match(line)
        # ``@`` で始まっていても、Lua のコードとして意味のある行ではないこと。
        if matched is not None and not line.strip().startswith("@@"):
            if current or sections:
                sections.append((name, current))
            name = matched.group("name")
            current = []
            continue
        current.append(line)

    sections.append((name, current))
    return tuple(
        ScriptSection(header=parse_control("\n".join(body), name=title), source="\n".join(body))
        for title, body in sections
        if title or "".join(body).strip() or len(sections) == 1
    )


def parse_control(text: str, *, name: str = "") -> ScriptHeader:
    """制御行を読んで設定欄を組み立てる。"""
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
            parameters.extend(_dialog(body))
            continue
        elif kind == "param":
            setup.append(body)
            continue
        elif kind in ("label", "information"):
            labels.append(body)
            continue
        else:
            # コメントとの区別が付かないものは記録しない。制御文字として
            # 意味を持つ綴りだけを未対応として残す。
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


#: 意味は分かるが、まだ効かせていない制御文字。
#: 記録して一覧に出すためだけに持っている。
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
    """``名前,最小,最大,初期[,刻み]``。"""
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
    """``名前,初期値``。初期値は ``0`` / ``1`` のほか ``true`` / ``false`` も来る。

    AviUtl2 世代の配布スクリプトは ``--check@bold:太字,true`` と書く。数値だけを
    見ていると、既定で入っているはずのチェックが外れた状態で読み込まれる。
    """
    parts = _split(body)
    label = (parts[0] if parts else "") or (named or "チェック")
    raw = parts[1].strip().lower() if len(parts) > 1 else ""
    default = raw in ("1", "true", "on", "yes") or _number(parts, 1, 0.0) != 0
    return CheckSpec(named or f"check{slot if slot is not None else 0}", label, default)


def _font(body: str, named: str | None) -> ParameterSpec:
    parts = _split(body)
    label = parts[0] if parts else "フォント"
    default = _unquote(parts[1]) if len(parts) > 1 and parts[1] else "Yu Gothic UI"
    return FontSpec(named or "font", label or "フォント", default)


def _text(body: str, named: str | None) -> ParameterSpec:
    """``--text@名前:ラベル,既定値``。AviUtl2 世代の文字入力欄。"""
    parts = _split(body)
    label = parts[0] if parts else "文字"
    default = _unquote(",".join(parts[1:])) if len(parts) > 1 else ""
    return TextSpec(named or "text", label or "文字", default)


def _color(body: str, named: str | None) -> ParameterSpec:
    parts = _split(body)
    label = parts[0] if parts else "色"
    value = parts[1] if len(parts) > 1 else ""
    return ColorSpec(named or "color", label or "色", _color_value(value, (1.0, 1.0, 1.0, 1.0)))


def _figure(body: str, named: str | None) -> ParameterSpec:
    """図形の種類。AviUtl の図形名をそのまま選択肢にする。"""
    parts = _split(body)
    label = parts[0] if parts else "図形"
    return SelectSpec(named or "figure", label or "図形", FIGURE_CHOICES, FIGURE_CHOICES[0][0])


def _select(body: str, named: str | None) -> ParameterSpec | None:
    """選択肢。実際の配布スクリプトには 2 通りの書き方がある。

    ``ラベル=既定値,表示名=値,表示名=値``
        AviUtl2 の配布スクリプトで実際に使われている形。

    ``ラベル,表示名=値/表示名=値,既定値``
        資料に載っている形。

    どちらも受ける。片方だけに対応すると、動かないスクリプトの理由が
    「書き方が違う」になってしまい、原因が分かりにくい。
    """
    parts = _split(body)
    if not parts:
        return None

    label, separator, head_default = parts[0].partition("=")
    choices: list[tuple[str, str]] = []
    default = head_default.strip() if separator else ""

    if separator:
        # ラベルに既定値が付いている形。以降がすべて選択肢。
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
    """``--value@`` はスライダーを持たない数値。整数として扱う。"""
    parts = _split(body)
    label = parts[0] if parts else "値"
    return ValueSpec(named or "value", label or "値", int(_number(parts, 1, 0.0)))


def _dialog(body: str) -> list[ParameterSpec]:
    """``ラベル,変数=初期値;ラベル/col,色=0xffffff;…``。

    ラベルの末尾に付く ``/chk`` ``/col`` などが種類を決める。AviUtl の
    仕様がここだけ独特なので、素直に書き下す。
    """
    specs: list[ParameterSpec] = []
    for chunk in body.split(";"):
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

        specs.append(_dialog_item(name, label, suffix, default))
    return specs


def _dialog_item(name: str, label: str, suffix: str, default: str) -> ParameterSpec:
    if suffix == "chk":
        return CheckSpec(name, label, _number([default], 0, 0.0) != 0)
    if suffix == "col":
        return ColorSpec(name, label, _color_value(default, (1.0, 1.0, 1.0, 1.0)))
    if suffix == "fig":
        return SelectSpec(name, label, FIGURE_CHOICES, _unquote(default) or FIGURE_CHOICES[0][0])
    if suffix == "file":
        return FileSpec(name, label)
    if suffix == "font":
        return FontSpec(name, label, _unquote(default) or "Yu Gothic UI")

    if default.startswith(('"', "'")):
        return TextSpec(name, label, _unquote(default))

    # 数値はスライダーにする。AviUtl のダイアログは入力欄で、範囲も無いが、
    # こちらではキーフレームを打てる方が使い出がある。範囲は初期値から広めに取る。
    value = _number([default], 0, 0.0)
    span = max(100.0, abs(value) * 10.0)
    step = 0.01 if value != int(value) else 1.0
    return TrackSpec(name, label, -span, span, value, step=step)


#: AviUtl の図形の種類。``obj.load("figure", ...)`` でも同じ名前を使う。
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
    """パラメータの値を、AviUtl のスクリプトが期待する型へ。

    ここを合わせないと、色が Python のタプルのまま Lua へ渡り、
    ``color / 65536`` のような計算でいきなり落ちる。AviUtl では色は
    ``0xRRGGBB`` の数値、選択肢は番号、チェックは真偽値。
    """
    if isinstance(spec, ColorSpec) and isinstance(value, tuple):
        red, green, blue = (int(min(max(float(part), 0.0), 1.0) * 255) for part in value[:3])
        return (red << 16) | (green << 8) | blue
    if isinstance(spec, CheckSpec):
        return bool(value)
    if isinstance(spec, SelectSpec) and isinstance(value, str):
        # 選択肢の識別子が数字なら数値で渡す。配布スクリプトは
        # ``if style == 1 then`` のように数値で比べる。
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


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _color_value(
    raw: str, fallback: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """``0xrrggbb`` を 0..1 の組へ。読めなければ ``fallback``。"""
    text = raw.strip()
    if not _HEX_COLOR.match(text):
        return fallback
    value = int(text, 16)
    red = (value >> 16) & 0xFF
    green = (value >> 8) & 0xFF
    blue = value & 0xFF
    return (red / 255.0, green / 255.0, blue / 255.0, 1.0)
