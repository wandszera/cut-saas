from copy import deepcopy
from typing import Any


DEFAULT_PRESET = "clean"


RENDER_PRESETS: dict[str, dict[str, Any]] = {
    "clean": {
        "label": "Clean",
        "description": "Visual limpo e versatil para testes gerais.",
        "subtitles": {
            "short": {
                "fontname": "Arial",
                "fontsize": 58,
                "primary_colour": "&H00FFFFFF",
                "secondary_colour": "&H0078D7FF",
                "outline_colour": "&H00000000",
                "back_colour": "&H64000000",
                "bold": -1,
                "italic": 0,
                "outline": 5,
                "shadow": 2,
                "alignment": 2,
                "margin_l": 52,
                "margin_r": 52,
                "margin_v": 210,
                "max_words_per_line": 3,
                "max_chars_per_line": 18,
                "max_lines": 2,
                "karaoke_enabled": True,
            },
            "long": {
                "fontname": "Arial",
                "fontsize": 38,
                "primary_colour": "&H00FFFFFF",
                "secondary_colour": "&H00FFFFFF",
                "outline_colour": "&H00000000",
                "back_colour": "&H50000000",
                "bold": -1,
                "italic": 0,
                "outline": 4,
                "shadow": 2,
                "alignment": 2,
                "margin_l": 70,
                "margin_r": 70,
                "margin_v": 56,
                "max_words_per_line": 6,
                "max_chars_per_line": 28,
                "max_lines": 2,
                "karaoke_enabled": False,
            },
        },
        "video": {
            "short": {"blur_strength": "20:2"},
            "long": {},
        },
    },
    "impact": {
        "label": "Impact",
        "description": "Legenda forte e mais contraste para cortes agressivos.",
        "subtitles": {
            "short": {
                "fontname": "Arial",
                "fontsize": 60,
                "primary_colour": "&H0000F7FF",
                "secondary_colour": "&H0000A5FF",
                "outline_colour": "&H00000000",
                "back_colour": "&H50000000",
                "bold": -1,
                "italic": 0,
                "outline": 6,
                "shadow": 2,
                "alignment": 2,
                "margin_l": 40,
                "margin_r": 40,
                "margin_v": 220,
                "max_words_per_line": 3,
                "max_chars_per_line": 16,
                "max_lines": 2,
                "karaoke_enabled": True,
            },
            "long": {
                "fontname": "Arial",
                "fontsize": 38,
                "primary_colour": "&H0000F7FF",
                "secondary_colour": "&H00FFFFFF",
                "outline_colour": "&H00000000",
                "back_colour": "&H32000000",
                "bold": -1,
                "italic": 0,
                "outline": 4,
                "shadow": 2,
                "alignment": 2,
                "margin_l": 50,
                "margin_r": 50,
                "margin_v": 50,
                "max_words_per_line": 5,
                "max_chars_per_line": 24,
                "max_lines": 2,
                "karaoke_enabled": False,
            },
        },
        "video": {
            "short": {"blur_strength": "26:3"},
            "long": {},
        },
    },
    "viral": {
        "label": "Viral",
        "description": "Visual de alta retencao para Shorts e Reels, com blocos curtos e destaque agressivo.",
        "subtitles": {
            "short": {
                "fontname": "Arial",
                "fontsize": 64,
                "primary_colour": "&H00FFFFFF",
                "secondary_colour": "&H0038F3FF",
                "outline_colour": "&H00000000",
                "back_colour": "&H6E080808",
                "bold": -1,
                "italic": 0,
                "outline": 7,
                "shadow": 2,
                "alignment": 2,
                "margin_l": 34,
                "margin_r": 34,
                "margin_v": 250,
                "max_words_per_line": 2,
                "max_chars_per_line": 14,
                "max_lines": 2,
                "karaoke_enabled": True,
            },
            "long": {
                "fontname": "Arial",
                "fontsize": 40,
                "primary_colour": "&H00FFFFFF",
                "secondary_colour": "&H0038F3FF",
                "outline_colour": "&H00000000",
                "back_colour": "&H50080808",
                "bold": -1,
                "italic": 0,
                "outline": 4,
                "shadow": 2,
                "alignment": 2,
                "margin_l": 54,
                "margin_r": 54,
                "margin_v": 58,
                "max_words_per_line": 4,
                "max_chars_per_line": 22,
                "max_lines": 2,
                "karaoke_enabled": False,
            },
        },
        "video": {
            "short": {"blur_strength": "30:4"},
            "long": {},
        },
    },
    "podcast": {
        "label": "Podcast",
        "description": "Mais respirado para conversa e conteudo falado.",
        "subtitles": {
            "short": {
                "fontname": "Arial",
                "fontsize": 54,
                "primary_colour": "&H00FFFFFF",
                "secondary_colour": "&H00B7FFB7",
                "outline_colour": "&H00000000",
                "back_colour": "&H5A101010",
                "bold": 0,
                "italic": 0,
                "outline": 4,
                "shadow": 2,
                "alignment": 2,
                "margin_l": 60,
                "margin_r": 60,
                "margin_v": 190,
                "max_words_per_line": 4,
                "max_chars_per_line": 20,
                "max_lines": 2,
                "karaoke_enabled": True,
            },
            "long": {
                "fontname": "Arial",
                "fontsize": 34,
                "primary_colour": "&H00FFFFFF",
                "secondary_colour": "&H00B7FFB7",
                "outline_colour": "&H00000000",
                "back_colour": "&H5A101010",
                "bold": 0,
                "italic": 0,
                "outline": 3,
                "shadow": 1,
                "alignment": 2,
                "margin_l": 60,
                "margin_r": 60,
                "margin_v": 38,
                "max_words_per_line": 7,
                "max_chars_per_line": 30,
                "max_lines": 2,
                "karaoke_enabled": False,
            },
        },
        "video": {
            "short": {"blur_strength": "18:2"},
            "long": {},
        },
    },
}


def list_render_presets() -> list[dict[str, str]]:
    return [
        {
            "key": key,
            "label": preset["label"],
            "description": preset["description"],
        }
        for key, preset in RENDER_PRESETS.items()
    ]


def resolve_render_preset(preset_name: str | None = None) -> tuple[str, dict[str, Any]]:
    normalized = (preset_name or DEFAULT_PRESET).strip().lower()
    selected = RENDER_PRESETS.get(normalized)
    if not selected:
        normalized = DEFAULT_PRESET
        selected = RENDER_PRESETS[normalized]
    return normalized, deepcopy(selected)
