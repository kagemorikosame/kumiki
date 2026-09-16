"""OpenGL による合成とエフェクト処理"""

from kumiki.engine.gpu.compositor import (
    BlendMode,
    Compositor,
    Corners,
    Placement,
    Transform,
    fit_placement,
)
from kumiki.engine.gpu.context import (
    CurrentGLContext,
    GLContextError,
    GLScope,
    OffscreenGLContext,
    ensure_qt_application,
    preferred_surface_format,
)
from kumiki.engine.gpu.effects import EffectProcessor, srgb_to_linear
from kumiki.engine.gpu.glutil import (
    FULL_RECT,
    IDENTITY,
    VERTEX_SHADER,
    Framebuffer,
    Program,
    ScreenQuad,
    ShaderError,
    Texture,
)

__all__ = [
    "FULL_RECT",
    "IDENTITY",
    "VERTEX_SHADER",
    "BlendMode",
    "Compositor",
    "Corners",
    "CurrentGLContext",
    "EffectProcessor",
    "Framebuffer",
    "GLContextError",
    "GLScope",
    "OffscreenGLContext",
    "Placement",
    "Program",
    "ScreenQuad",
    "ShaderError",
    "Texture",
    "Transform",
    "ensure_qt_application",
    "fit_placement",
    "preferred_surface_format",
    "srgb_to_linear",
]
