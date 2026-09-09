"""OpenGL による合成とエフェクト処理。"""

from novaedit.engine.gpu.compositor import (
    BlendMode,
    Compositor,
    Placement,
    Transform,
    fit_placement,
)
from novaedit.engine.gpu.context import (
    CurrentGLContext,
    GLContextError,
    GLScope,
    OffscreenGLContext,
    ensure_qt_application,
    preferred_surface_format,
)
from novaedit.engine.gpu.effects import EffectProcessor, srgb_to_linear
from novaedit.engine.gpu.glutil import (
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
