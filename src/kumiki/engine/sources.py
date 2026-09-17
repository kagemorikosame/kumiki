"""テキストと図形を絵にする

Qt の描画系（``QPainter``）を使う 日本語の禁則処理やフォントの字形選択、
縁取りの輪郭生成を自前で書くのは現実的ではなく、Qt はそれをすべて持っている

戻り値は常に sRGB・ストレートアルファの ``(高さ, 幅, 4)`` uint8 素材から
デコードした絵とまったく同じ形なので、この先の合成は区別せずに済む
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QTransform,
)

from kumiki.core.model import AnimatedValue, GeneratedSource, ParamValue
from kumiki.effects.sources import SourceDefinition, source_registry

__all__ = ["render_source"]

#: 縦の基準ごとに、指定した位置より上へ出す割合 ``下`` なら全部が上に出る
_VERTICAL_SHARE = {"top": 0.0, "middle": 0.5, "bottom": 1.0}


def render_source(
    source: GeneratedSource, width: int, height: int, *, frame: int = 0
) -> np.ndarray | None:
    """生成オブジェクトを描いて配列で返す 未知の種類なら ``None``"""
    definition = source_registry.get(source.kind)
    if definition is None:
        return None

    values = _resolve(definition, source.params, frame)
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    try:
        if source.kind == "text":
            _draw_text(painter, values, width, height)
        elif source.kind == "shape":
            _draw_shape(painter, values, width, height)
    finally:
        painter.end()

    return _to_array(image)


#: 画面より大きい絵を作るときの一辺の上限（画素） GPU のテクスチャの上限より十分小さく
MAX_CANVAS = 8192


def source_canvas(
    source: GeneratedSource, width: int, height: int, *, frame: int = 0
) -> tuple[int, int]:
    """生成オブジェクトを描く絵の大きさ 画面より大きい図形なら、はみ出す分まで広げる

    中心は画面の中心のまま広げる（描く位置の計算は変えない） 画面の大きさで
    切ってしまうと、画面より大きい図形を回したり動かしたりしたときに、切れた端が
    見えてしまう（YMM4 の斜めの帯のトランジションは高さ 2160 の図形を 45 度回す）
    """
    if source.kind != "shape":
        return width, height
    definition = source_registry.get(source.kind)
    if definition is None:
        return width, height
    values = _resolve(definition, source.params, frame)
    shape_width = max(1.0, float(values.get("width", 400)))  # type: ignore[arg-type]
    shape_height = max(1.0, float(values.get("height", 400)))  # type: ignore[arg-type]
    line = float(values.get("line_width", 0.0))  # type: ignore[arg-type]
    # 回しても収まるよう、対角線の長さで見積もる
    reach = (shape_width**2 + shape_height**2) ** 0.5 / 2.0 + line
    needed_width = 2.0 * (abs(float(values.get("pos_x", 0.0))) + reach)  # type: ignore[arg-type]
    needed_height = 2.0 * (abs(float(values.get("pos_y", 0.0))) + reach)  # type: ignore[arg-type]
    grown_width = min(MAX_CANVAS, max(width, int(np.ceil(needed_width))))
    grown_height = min(MAX_CANVAS, max(height, int(np.ceil(needed_height))))
    # 画面と偶奇をそろえる 差が奇数だと、中心が半画素ずれて輪郭がにじむ
    grown_width += (grown_width - width) % 2
    grown_height += (grown_height - height) % 2
    return grown_width, grown_height


def _resolve(
    definition: SourceDefinition, params: dict[str, ParamValue], frame: int
) -> dict[str, object]:
    """パラメータを、その時刻での素の値へ

    数値は :class:`~kumiki.core.model.AnimatedValue` なので、フレームを与えて
    評価する ここを飛ばすとキーフレームが効かない
    """
    resolved: dict[str, object] = {}
    for spec in definition.parameters:
        value = spec.coerce(params.get(spec.name))
        resolved[spec.name] = value.at(frame) if isinstance(value, AnimatedValue) else value
    return resolved


def _draw_text(painter: QPainter, values: dict[str, object], width: int, height: int) -> None:
    text = _revealed(str(values.get("text", "")), values)
    if not text:
        return

    font = QFont(str(values.get("font", "Yu Gothic UI")))
    font.setPixelSize(max(1, int(float(values.get("size", 64)))))  # type: ignore[arg-type]
    font.setBold(bool(values.get("bold", False)))
    font.setItalic(bool(values.get("italic", False)))
    letter_spacing = float(values.get("letter_spacing", 0.0))  # type: ignore[arg-type]
    if letter_spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)

    metrics = QFontMetricsF(font)
    if bool(values.get("vertical", False)):
        _draw_vertical_text(painter, text, font, metrics, values, width, height)
        return

    lines = text.split(chr(10))
    line_height = metrics.height() + float(values.get("line_spacing", 0.0))  # type: ignore[arg-type]
    block_height = line_height * len(lines)

    align = str(values.get("align", "center"))
    centre_x = width / 2.0 + float(values.get("pos_x", 0.0))  # type: ignore[arg-type]
    centre_y = height / 2.0 - float(values.get("pos_y", 0.0))  # type: ignore[arg-type]
    top = centre_y - block_height * _VERTICAL_SHARE.get(str(values.get("valign", "middle")), 0.5)

    # 文字を輪郭（パス）として組み立てる 縁取りを外側だけに出すには、
    # 塗りとは別に輪郭を太らせる必要があり、それはパスでしかできない
    path = QPainterPath()
    for index, line in enumerate(lines):
        line_width = metrics.horizontalAdvance(line)
        if align == "left":
            x = centre_x - _widest(metrics, lines) / 2.0
        elif align == "right":
            x = centre_x + _widest(metrics, lines) / 2.0 - line_width
        else:
            x = centre_x - line_width / 2.0
        baseline = top + line_height * index + metrics.ascent()
        path.addText(QPointF(x, baseline), font, line)

    _paint_glyphs(painter, path, values, width, height)


def _revealed(text: str, values: dict[str, object]) -> str:
    """文字送り 先頭から指定の割合だけを出す

    テロップを 1 文字ずつ出す表現は AviUtl でも定番で、こちらでもキーフレームを
    打てば同じことができる 改行は文字数に数えない 数えると、行が変わる瞬間に
    見た目の速度が変わる
    """
    ratio = float(values.get("reveal", 100.0)) / 100.0  # type: ignore[arg-type]
    if ratio >= 1.0:
        return text
    if ratio <= 0.0:
        return ""

    visible = round(len([c for c in text if c != chr(10)]) * ratio)
    shown: list[str] = []
    for character in text:
        if character == chr(10):
            shown.append(character)
            continue
        if visible <= 0:
            break
        shown.append(character)
        visible -= 1
    return "".join(shown)


def _draw_vertical_text(
    painter: QPainter,
    text: str,
    font: QFont,
    metrics: QFontMetricsF,
    values: dict[str, object],
    width: int,
    height: int,
) -> None:
    """縦書き 行は右から左へ並べる

    Qt に縦書きの組版は無いので、1 文字ずつ縦に置く 日本語のテロップでは
    使う場面がはっきりあるので、簡素でも入れておく
    """
    columns = text.split(chr(10))
    advance = metrics.height() + float(values.get("letter_spacing", 0.0))  # type: ignore[arg-type]
    column_width = metrics.height() + float(values.get("line_spacing", 0.0))  # type: ignore[arg-type]

    centre_x = width / 2.0 + float(values.get("pos_x", 0.0))  # type: ignore[arg-type]
    centre_y = height / 2.0 - float(values.get("pos_y", 0.0))  # type: ignore[arg-type]
    tallest = max((len(column) for column in columns), default=0)
    left = centre_x + column_width * (len(columns) - 1) / 2.0
    top = centre_y - advance * tallest / 2.0

    path = QPainterPath()
    for column_index, column in enumerate(columns):
        x = left - column_width * column_index
        for row_index, character in enumerate(column):
            baseline = top + advance * row_index + metrics.ascent()
            offset = metrics.horizontalAdvance(character) / 2.0
            path.addText(QPointF(x - offset, baseline), font, character)

    _paint_glyphs(painter, path, values, width, height)


def _paint_glyphs(
    painter: QPainter,
    path: QPainterPath,
    values: dict[str, object],
    width: int,
    height: int,
) -> None:
    """組み上がった文字の輪郭を、影・縁取り・塗りの順に描く

    縦書きでも横書きでも飾りの付け方は同じなので、ここに 1 つだけ置く
    順番は下から影・縁・塗り 入れ替えると縁が影を隠す
    """
    shadow = _shadow_layer(path, values, width, height)
    if shadow is not None:
        painter.drawImage(0, 0, shadow)

    border_width = float(values.get("border_width", 0.0))  # type: ignore[arg-type]
    if border_width > 0:
        painter.fillPath(_stroke(path, border_width), _color(values.get("border_color")))
    painter.fillPath(path, _color(values.get("color")))


def _stroke(path: QPainterPath, width: float) -> QPainterPath:
    """輪郭を太らせたパス

    太さは輪郭の中心から両側へ広がるので、指定の 2 倍にして外側に指定幅を出す
    """
    stroker = QPainterPathStroker()
    stroker.setWidth(width * 2.0)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return stroker.createStroke(path)


def _shadow_layer(
    path: QPainterPath, values: dict[str, object], width: int, height: int
) -> QImage | None:
    """文字の影を別の面に描いて返す 影が無ければ ``None``

    ぼかしのために 1 枚離す 影は単色なので、ぼかすのは不透明度だけでよく、
    色の 3 成分はそのままにできる RGB ごとぼかすと、縁で色がにじむ
    """
    offset_x = float(values.get("shadow_x", 0.0))  # type: ignore[arg-type]
    offset_y = float(values.get("shadow_y", 0.0))  # type: ignore[arg-type]
    blur = float(values.get("shadow_blur", 0.0))  # type: ignore[arg-type]
    colour = _color(values.get("shadow_color"))
    if (offset_x, offset_y, blur) == (0.0, 0.0, 0.0) or colour.alpha() == 0:
        return None

    layer = QImage(width, height, QImage.Format.Format_RGBA8888)
    layer.fill(Qt.GlobalColor.transparent)
    shifted = QPainterPath(path)
    # 画面の Y は下向き 設定の Y は上向きなので符号を反転する
    shifted.translate(offset_x, -offset_y)

    shadow_painter = QPainter(layer)
    shadow_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    border_width = float(values.get("border_width", 0.0))  # type: ignore[arg-type]
    if border_width > 0:
        # 縁取りがあるときは、その外形の影が落ちる 塗りだけの影にすると
        # 縁の分だけ影が細く見える
        shadow_painter.fillPath(_stroke(shifted, border_width), colour)
    shadow_painter.fillPath(shifted, colour)
    shadow_painter.end()

    if blur <= 0.0:
        return layer
    return _blur_alpha(layer, blur)


def _blur_alpha(image: QImage, radius: float) -> QImage:
    """不透明度だけを平均化する

    箱ぼかしを縦横 2 回ずつ掛ける 厳密なガウスではないが、影の輪郭を
    やわらげる用途では見分けが付かず、こちらは掛け算が要らない
    """
    span = max(1, round(radius))
    # ``_to_array`` は元のバッファをそのまま見ていることがあり、書き込めない
    array = _to_array(image).copy()
    alpha = array[:, :, 3].astype(np.float32)
    for _ in range(2):
        alpha = _box_blur(alpha, span)
    array[:, :, 3] = np.clip(alpha, 0.0, 255.0).astype(np.uint8)
    return QImage(
        array.tobytes(), image.width(), image.height(), QImage.Format.Format_RGBA8888
    ).copy()


def _box_blur(values: np.ndarray, span: int) -> np.ndarray:
    """縦横に窓幅 ``2*span+1`` の移動平均を掛ける

    累積和で求めるので、窓の幅を広げても速さは変わらない
    """
    result = values
    for axis in (0, 1):
        padded = np.pad(result, [(span, span) if a == axis else (0, 0) for a in (0, 1)], "edge")
        cumulative = np.cumsum(padded, axis=axis)
        zero = np.zeros_like(np.take(cumulative, [0], axis=axis))
        cumulative = np.concatenate([zero, cumulative], axis=axis)
        length = result.shape[axis]
        upper = np.take(cumulative, range(2 * span + 1, 2 * span + 1 + length), axis=axis)
        lower = np.take(cumulative, range(0, length), axis=axis)
        result = (upper - lower) / (2 * span + 1)
    return result


def _widest(metrics: QFontMetricsF, lines: list[str]) -> float:
    return max((metrics.horizontalAdvance(line) for line in lines), default=0.0)


def _draw_shape(painter: QPainter, values: dict[str, object], width: int, height: int) -> None:
    shape_width = max(1.0, float(values.get("width", 400)))  # type: ignore[arg-type]
    shape_height = max(1.0, float(values.get("height", 400)))  # type: ignore[arg-type]
    centre_x = width / 2.0 + float(values.get("pos_x", 0.0))  # type: ignore[arg-type]
    centre_y = height / 2.0 - float(values.get("pos_y", 0.0))  # type: ignore[arg-type]

    rect = QRectF(-shape_width / 2.0, -shape_height / 2.0, shape_width, shape_height)
    path = _shape_path(str(values.get("shape", "rect")), rect, values)

    transform = QTransform()
    transform.translate(centre_x, centre_y)
    transform.rotate(float(values.get("rotation", 0.0)))  # type: ignore[arg-type]
    path = transform.map(path)

    color = _color(values.get("color"))
    line_width = float(values.get("line_width", 0.0))  # type: ignore[arg-type]

    if bool(values.get("outline_only", False)):
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, max(line_width, 1.0)))
        painter.drawPath(path)
        return

    painter.fillPath(path, color)
    if line_width > 0:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, line_width))
        painter.drawPath(path)


def _shape_path(kind: str, rect: QRectF, values: dict[str, object]) -> QPainterPath:
    path = QPainterPath()
    if kind == "ellipse":
        path.addEllipse(rect)
    elif kind == "rounded":
        radius = float(values.get("corner_radius", 24))  # type: ignore[arg-type]
        path.addRoundedRect(rect, radius, radius)
    elif kind == "triangle":
        path.moveTo(rect.center().x(), rect.top())
        path.lineTo(rect.right(), rect.bottom())
        path.lineTo(rect.left(), rect.bottom())
        path.closeSubpath()
    elif kind == "pentagon":
        path = _polygon_path(rect, 5)
    elif kind == "hexagon":
        path = _polygon_path(rect, 6)
    elif kind == "star":
        path = _star_path(rect)
    else:
        # ``background`` もここ 大きさは呼び出し側が画面いっぱいに指定する
        path.addRect(rect)
    return path


def _polygon_path(rect: QRectF, sides: int) -> QPainterPath:
    """正多角形 頂点を上に向けて置く

    AviUtl の五角形・六角形に対応する 頂点の向きを合わせておかないと、
    移植した資産で図形だけ傾いて見える
    """
    path = QPainterPath()
    radius_x, radius_y = rect.width() / 2.0, rect.height() / 2.0
    centre = rect.center()
    for index in range(sides):
        angle = 2.0 * np.pi * index / sides - np.pi / 2.0
        x = centre.x() + np.cos(angle) * radius_x
        y = centre.y() + np.sin(angle) * radius_y
        if index == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


def _star_path(rect: QRectF, points: int = 5) -> QPainterPath:
    """5 芒星 外周と内周の頂点を交互に結ぶ"""
    path = QPainterPath()
    outer_x, outer_y = rect.width() / 2.0, rect.height() / 2.0
    inner_ratio = 0.382  # 正五芒星の内接比
    centre = rect.center()

    for index in range(points * 2):
        angle = np.pi * index / points - np.pi / 2.0
        ratio = 1.0 if index % 2 == 0 else inner_ratio
        x = centre.x() + np.cos(angle) * outer_x * ratio
        y = centre.y() + np.sin(angle) * outer_y * ratio
        if index == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


def _color(value: object) -> QColor:
    """``(R, G, B, A)`` の 0..1（sRGB）を :class:`QColor` へ"""
    if not isinstance(value, tuple) or len(value) < 3:
        return QColor(255, 255, 255, 255)
    channels = [round(min(max(float(v), 0.0), 1.0) * 255) for v in value[:4]]
    while len(channels) < 4:
        channels.append(255)
    return QColor(*channels)


def _to_array(image: QImage) -> np.ndarray:
    """``QImage`` を ``(高さ, 幅, 4)`` の配列へ

    ``QImage`` は行ごとに詰め物を入れることがあるので、``bytesPerLine`` を見て
    余りを落とす 幅だけで整形すると絵が斜めにずれる
    """
    width, height = image.width(), image.height()
    buffer = bytes(image.constBits())
    stride = image.bytesPerLine()
    rows = np.frombuffer(buffer, dtype=np.uint8).reshape(height, stride)
    return np.ascontiguousarray(rows[:, : width * 4].reshape(height, width, 4))
