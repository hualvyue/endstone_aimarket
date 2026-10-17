"""玩家自选显示：注册表、偏好、渲染与 GUI。"""
from .forms import UIForms
from .prefs import (
    CARRIER_ACTIONBAR,
    CARRIER_NONE,
    CARRIER_SIDEBAR,
    DEFAULT_REFRESH_TICKS,
    PrefsRepository,
    UiPrefs,
)
from .providers import register_builtin
from .registry import DisplayItem, DisplayRegistry
from .renderer import DisplayContext, DisplayRenderer
from .service import UIService

__all__ = [
    "UIService",
    "UIForms",
    "DisplayRegistry",
    "DisplayItem",
    "DisplayContext",
    "DisplayRenderer",
    "PrefsRepository",
    "UiPrefs",
    "register_builtin",
    "CARRIER_SIDEBAR",
    "CARRIER_ACTIONBAR",
    "CARRIER_NONE",
    "DEFAULT_REFRESH_TICKS",
]
