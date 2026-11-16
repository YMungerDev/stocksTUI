import copy
import json
import logging
from typing import Any

from textual.theme import Theme

logger = logging.getLogger(__name__)

# A base template for all themes. It defines the required keys and uses
# placeholder variables (e.g., '$blue') that will be substituted with
# concrete colors from a specific theme's palette.
BASE_THEME_STRUCTURE: dict[str, Any] = {
    "dark": False,
    "primary": "$blue",
    "secondary": "$cyan",
    "accent": "$orange",
    "success": "$green",
    "warning": "$yellow",
    "error": "$red",
    "background": "$bg3",
    "surface": "$bg2",
    "panel": "$bg1",
    "foreground": "$fg0",
    "variables": {
        "price": "$cyan",
        "latency-high": "$red",
        "latency-medium": "$yellow",
        "latency-low": "$blue",
        "text-muted": "$fg1",
        "status-open": "$green",
        "status-pre": "$yellow",
        "status-post": "$yellow",
        "status-closed": "$red",
        "button-foreground": "$fg3",
        "scrollbar": "$bg0",
        "scrollbar-hover": "$fg2",
    },
}


def substitute_colors(template: dict[str, Any], palette: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively substitutes color variables (e.g., '$blue') in a theme
    structure with concrete color values from a palette.
    """
    resolved: dict[str, Any] = {}
    for key, value in template.items():
        if isinstance(value, dict):
            resolved[key] = substitute_colors(value, palette)
        elif isinstance(value, str) and value.startswith("$"):
            color_name = value[1:]
            resolved[key] = palette.get(color_name, f"UNDEFINED_{color_name.upper()}")
        else:
            resolved[key] = value
    return resolved


def resolve_themes(
    themes_config: dict[str, dict[str, Any]],
) -> tuple[dict[str, Theme], dict[str, dict[str, Any]]]:
    """
    Resolves user/default theme palette definitions against BASE_THEME_STRUCTURE.

    Returns:
        A tuple of (textual_themes_by_name, resolved_theme_dicts_by_name).
    """
    textual_themes: dict[str, Theme] = {}
    valid_themes: dict[str, dict[str, Any]] = {}

    for name, theme_data in themes_config.items():
        palette = theme_data.get("palette")
        if not palette:
            logger.warning(f"Theme '{name}' has no 'palette' defined. Skipping.")
            continue

        try:
            resolved_theme_dict = substitute_colors(
                copy.deepcopy(BASE_THEME_STRUCTURE), palette
            )
            resolved_theme_dict["dark"] = theme_data.get("dark", False)

            resolved_json = json.dumps(resolved_theme_dict)
            if "UNDEFINED_" in resolved_json:
                raise ValueError(
                    f"Theme '{name}' is missing one or more required color definitions in its palette."
                )

            textual_themes[name] = Theme(name=name, **resolved_theme_dict)
            valid_themes[name] = resolved_theme_dict
        except Exception as e:  # noqa: BLE001
            logger.error(f"Theme '{name}' failed to load: {e}")

    return textual_themes, valid_themes


def extract_theme_variables(theme_dict: dict[str, Any]) -> dict[str, Any]:
    """Builds the flat theme variable map used for programmatic Rich styling."""
    return {
        "primary": theme_dict.get("primary"),
        "secondary": theme_dict.get("secondary"),
        "accent": theme_dict.get("accent"),
        "success": theme_dict.get("success"),
        "warning": theme_dict.get("warning"),
        "error": theme_dict.get("error"),
        "foreground": theme_dict.get("foreground"),
        "background": theme_dict.get("background"),
        "surface": theme_dict.get("surface"),
        **theme_dict.get("variables", {}),
    }
