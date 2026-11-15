from __future__ import annotations

from dataclasses import dataclass, field
from json import dumps, loads
from pathlib import Path
from typing import Any, Literal, cast

from octo_coding.paths import OctoPaths
from octo_coding.tui.themes import (
    BUILTIN_TUI_THEME_NAMES,
    HIGH_CONTRAST_THEME,
    OCTO_DARK_THEME,
    OCTO_LIGHT_THEME,
    TuiRoleStyle,
    TuiTheme,
    TuiThemeName,
    get_tui_theme,
)

type TurnNotificationMode = Literal["off", "bell", "desktop"]


__all__ = [
    "BUILTIN_TUI_THEME_NAMES",
    "HIGH_CONTRAST_THEME",
    "OCTO_DARK_THEME",
    "OCTO_LIGHT_THEME",
    "TuiConfigError",
    "TuiKeybindings",
    "TuiRoleStyle",
    "TuiSettings",
    "TuiTheme",
    "TuiThemeName",
    "TurnNotificationMode",
    "get_tui_theme",
    "load_tui_settings",
    "save_tui_settings",
    "tui_settings_from_json",
    "tui_settings_path",
]


class TuiConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TuiKeybindings:


    cancel: str = "escape"
    command_palette: str = "ctrl+k"
    session_picker: str = "ctrl+r"
    queue_follow_up: str = "alt+enter"
    insert_newline: str = "shift+enter"
    accept_completion: str = "tab"
    completion_next: str = "down"
    completion_previous: str = "up"
    thinking_cycle: str = "shift+tab"
    model_cycle: str = "ctrl+p"
    model_cycle_reverse: str = "ctrl+shift+p"
    toggle_thinking: str = "ctrl+t"
    toggle_tool_results: str = "ctrl+o"
    copy_message: str = "ctrl+c"
    quit: str = "ctrl+d"

    def to_json(self) -> dict[str, str]:

        return {
            "cancel": self.cancel,
            "command_palette": self.command_palette,
            "session_picker": self.session_picker,
            "queue_follow_up": self.queue_follow_up,
            "insert_newline": self.insert_newline,
            "accept_completion": self.accept_completion,
            "completion_next": self.completion_next,
            "completion_previous": self.completion_previous,
            "thinking_cycle": self.thinking_cycle,
            "model_cycle": self.model_cycle,
            "model_cycle_reverse": self.model_cycle_reverse,
            "toggle_thinking": self.toggle_thinking,
            "toggle_tool_results": self.toggle_tool_results,
            "copy_message": self.copy_message,
            "quit": self.quit,
        }


@dataclass(frozen=True, slots=True)
class TuiSettings:


    keybindings: TuiKeybindings = field(default_factory=TuiKeybindings)
    theme: TuiThemeName = "octo-dark"
    auto_copy_selection: bool = False
    sidebar_position: Literal["left", "right", "off"] = "right"
    turn_notification: TurnNotificationMode = "desktop"

    def to_json(self) -> dict[str, Any]:

        return {
            "auto_copy_selection": self.auto_copy_selection,
            "keybindings": self.keybindings.to_json(),
            "sidebar_position": self.sidebar_position,
            "theme": self.theme,
            "turn_notification": self.turn_notification,
        }

    @property
    def resolved_theme(self) -> TuiTheme:

        try:
            return get_tui_theme(self.theme)
        except KeyError:
            return OCTO_DARK_THEME


def tui_settings_path(paths: OctoPaths | None = None) -> Path:

    return (paths or OctoPaths()).home / "tui.json"


def load_tui_settings(paths: OctoPaths | None = None) -> TuiSettings:

    path = tui_settings_path(paths)
    if not path.exists():
        return TuiSettings()
    raw = loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TuiConfigError("TUI settings must be a JSON object")
    return tui_settings_from_json(raw)


def save_tui_settings(settings: TuiSettings, paths: OctoPaths | None = None) -> Path:

    path = tui_settings_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(settings.to_json(), indent=2) + "\n", encoding="utf-8")
    return path


def tui_settings_from_json(data: dict[str, Any]) -> TuiSettings:


    keybindings_data = data.get("keybindings", {})
    if not isinstance(keybindings_data, dict):
        raise TuiConfigError("TUI keybindings must be a JSON object")
    raw_sidebar = data.get("sidebar_position", "right")
    if not isinstance(raw_sidebar, str) or raw_sidebar not in {"left", "right", "off"}:
        raise TuiConfigError("sidebar_position must be 'left', 'right', or 'off'")
    raw_notification = data.get("turn_notification", "desktop")
    if not isinstance(raw_notification, str) or raw_notification not in {
        "off",
        "bell",
        "desktop",
    }:
        raise TuiConfigError("turn_notification must be 'off', 'bell', or 'desktop'")
    return TuiSettings(
        keybindings=_keybindings_from_json(keybindings_data),
        theme=_theme_name(data.get("theme", "octo-dark")),
        auto_copy_selection=_bool_setting(
            data.get("auto_copy_selection", False),
            "auto_copy_selection",
        ),
        sidebar_position=cast(Literal["left", "right", "off"], raw_sidebar),
        turn_notification=cast(TurnNotificationMode, raw_notification),
    )


def _bool_setting(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise TuiConfigError(f"TUI setting must be a boolean: {field_name}")


def _keybindings_from_json(data: dict[str, Any]) -> TuiKeybindings:
    defaults = TuiKeybindings()


    values = {
        field_name: _key_string(data.get(field_name, default_value), field_name)
        for field_name, default_value in defaults.to_json().items()
    }
    _reject_duplicate_keys(values)
    return TuiKeybindings(**values)


def _key_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TuiConfigError(f"TUI keybinding must be a non-empty string: {field_name}")
    return value.strip()


def _theme_name(value: object) -> TuiThemeName:
    if not isinstance(value, str) or not value.strip():
        raise TuiConfigError("TUI theme must be a non-empty string")
    return value.strip()


def _reject_duplicate_keys(values: dict[str, str]) -> None:
    key_to_action: dict[str, str] = {}
    for action, key in values.items():
        previous_action = key_to_action.get(key)
        if previous_action is not None:
            raise TuiConfigError(
                f"TUI keybinding {key!r} is assigned to both {previous_action!r} and {action!r}"
            )
        key_to_action[key] = action
