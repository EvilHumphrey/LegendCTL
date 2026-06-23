"""Dear PyGui localization wrappers for static labels and text."""

from __future__ import annotations

from functools import wraps

from zd_app.i18n import translate_ui_value


_PATCHED_ATTR = "_zdul_i18n_wrapped"

_CALL_SPECS = {
    "add_text": {0, "default_value"},
    "add_button": {"label"},
    "add_checkbox": {"label"},
    "add_input_text": {"label", "hint"},
    "add_slider_int": {"label"},
    "add_slider_float": {"label"},
    "add_radio_button": {"label"},
    "add_menu_item": {"label"},
    "add_selectable": {"label"},
    "add_collapsing_header": {"label"},
    "add_tab": {"label"},
    "create_viewport": {"title"},
    "window": {"label"},
}


def _wrap(func, spec):
    @wraps(func)
    def wrapper(*args, **kwargs):
        translated_args = list(args)
        for item in spec:
            if isinstance(item, int) and item < len(translated_args):
                translated_args[item] = translate_ui_value(translated_args[item])
            elif isinstance(item, str) and item in kwargs:
                kwargs[item] = translate_ui_value(kwargs[item])
        return func(*translated_args, **kwargs)

    setattr(wrapper, _PATCHED_ATTR, True)
    return wrapper


def install_dearpygui_i18n(dpg_module) -> None:
    """Patch Dear PyGui label/text constructors once for static UI literals."""

    for name, spec in _CALL_SPECS.items():
        func = getattr(dpg_module, name, None)
        if func is None or getattr(func, _PATCHED_ATTR, False):
            continue
        setattr(dpg_module, name, _wrap(func, spec))
