"""合成モードの式 塗りのエフェクトとトラックの合成で同じ式を使う

どちらも YMM4（Direct2D）に合わせて sRGB のまま混ぜる 片方だけ直すと、
同じ名前の合成が塗りとアイテムで違う色になる
"""

from __future__ import annotations

__all__ = ["BLEND_FUNCTIONS", "BLEND_MODES", "blend_index"]

#: 合成モード シェーダの番号と同じ並び 名前は YMM4 の表記に寄せた
BLEND_MODES = (
    ("normal", "通常"),
    ("add", "加算"),
    ("subtract", "減算"),
    ("multiply", "乗算"),
    ("screen", "スクリーン"),
    ("overlay", "オーバーレイ"),
    ("soft_light", "ソフトライト"),
    ("hard_light", "ハードライト"),
    ("color_dodge", "覆い焼きカラー"),
    ("color_burn", "焼き込みカラー"),
    ("lighten", "比較(明)"),
    ("darken", "比較(暗)"),
    ("lighter_color", "カラー比較(明)"),
    ("darker_color", "カラー比較(暗)"),
    ("difference", "差の絶対値"),
    ("exclusion", "除外"),
    ("linear_burn", "焼き込みリニア"),
    ("linear_light", "リニアライト"),
    ("vivid_light", "ビビッドライト"),
    ("pin_light", "ピンライト"),
    ("hard_mix", "ハードミックス"),
    ("division", "除算"),
    ("hue", "色相"),
    ("saturation", "彩度"),
    ("color", "カラー"),
    ("luminosity", "輝度"),
)


def blend_index(name: str) -> int:
    """シェーダへ渡す番号 知らない名前は通常（0）"""
    for index, (key, _) in enumerate(BLEND_MODES):
        if key == name:
            return index
    return 0


#: ``blend_colors(mode, below, above)`` と、その下請けの関数
BLEND_FUNCTIONS = """
float luminance(vec3 c) { return dot(c, vec3(0.3, 0.59, 0.11)); }
vec3 with_luminance(vec3 c, float l) {
    c += l - luminance(c);
    float n = min(min(c.r, c.g), c.b);
    float x = max(max(c.r, c.g), c.b);
    float lum = luminance(c);
    if (n < 0.0) c = lum + (c - lum) * lum / max(lum - n, 1e-5);
    if (x > 1.0) c = lum + (c - lum) * (1.0 - lum) / max(x - lum, 1e-5);
    return c;
}
float saturation_of(vec3 c) { return max(max(c.r, c.g), c.b) - min(min(c.r, c.g), c.b); }
vec3 with_saturation(vec3 c, float s) {
    float n = min(min(c.r, c.g), c.b);
    float x = max(max(c.r, c.g), c.b);
    return x > n ? (c - n) * s / (x - n) : vec3(0.0);
}

float soft_light(float b, float s) {
    if (s <= 0.5) return b - (1.0 - 2.0 * s) * b * (1.0 - b);
    float d = b <= 0.25 ? ((16.0 * b - 12.0) * b + 4.0) * b : sqrt(b);
    return b + (2.0 * s - 1.0) * (d - b);
}
float color_dodge(float b, float s) {
    return b <= 0.0 ? 0.0 : (s >= 1.0 ? 1.0 : min(1.0, b / (1.0 - s)));
}
float color_burn(float b, float s) {
    return b >= 1.0 ? 1.0 : (s <= 0.0 ? 0.0 : 1.0 - min(1.0, (1.0 - b) / s));
}
vec3 dodge3(vec3 b, vec3 s) {
    return vec3(color_dodge(b.r, s.r), color_dodge(b.g, s.g), color_dodge(b.b, s.b));
}
vec3 burn3(vec3 b, vec3 s) {
    return vec3(color_burn(b.r, s.r), color_burn(b.g, s.g), color_burn(b.b, s.b));
}

// b は下、s は上 どちらも sRGB mode は BLEND_MODES の並びの番号
vec3 blend_colors(int mode, vec3 b, vec3 s) {
    if (mode == 1) return min(b + s, 1.0);
    if (mode == 2) return max(b - s, 0.0);
    if (mode == 3) return b * s;
    if (mode == 4) return b + s - b * s;
    if (mode == 5) return mix(2.0 * b * s, 1.0 - 2.0 * (1.0 - b) * (1.0 - s), step(0.5, b));
    if (mode == 6) return vec3(soft_light(b.r, s.r), soft_light(b.g, s.g), soft_light(b.b, s.b));
    if (mode == 7) return mix(2.0 * b * s, 1.0 - 2.0 * (1.0 - b) * (1.0 - s), step(0.5, s));
    if (mode == 8) return dodge3(b, s);
    if (mode == 9) return burn3(b, s);
    if (mode == 10) return max(b, s);
    if (mode == 11) return min(b, s);
    if (mode == 12) return luminance(s) > luminance(b) ? s : b;
    if (mode == 13) return luminance(s) < luminance(b) ? s : b;
    if (mode == 14) return abs(b - s);
    if (mode == 15) return b + s - 2.0 * b * s;
    if (mode == 16) return max(b + s - 1.0, 0.0);
    if (mode == 17) return clamp(b + 2.0 * s - 1.0, 0.0, 1.0);
    if (mode == 18) {
        return mix(burn3(b, 2.0 * s), dodge3(b, 2.0 * s - 1.0), step(0.5, s));
    }
    if (mode == 19) return mix(min(b, 2.0 * s), max(b, 2.0 * s - 1.0), step(0.5, s));
    if (mode == 20) return step(1.0, b + s);
    if (mode == 21) return clamp(b / max(s, 1e-5), 0.0, 1.0);
    if (mode == 22) return with_luminance(with_saturation(s, saturation_of(b)), luminance(b));
    if (mode == 23) return with_luminance(with_saturation(b, saturation_of(s)), luminance(b));
    if (mode == 24) return with_luminance(s, luminance(b));
    if (mode == 25) return with_luminance(b, luminance(s));
    return s;
}
"""
