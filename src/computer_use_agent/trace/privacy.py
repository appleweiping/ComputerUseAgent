"""Privacy boundary shared by the ScienceBoard X11 recorder and tests."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

# Field names are matched case-insensitively at every nesting depth. The
# recorder may inspect a keysym or window property in memory to classify an
# event, but none of these content-bearing fields may cross this boundary.
FORBIDDEN_EVENT_FIELDS = frozenset(
    {
        "clipboard",
        "clipboard_contents",
        "command",
        "file_path",
        "filename",
        "key",
        "keycode",
        "keysym",
        "process_command",
        "text",
        "title",
        "value",
        "window_title",
    }
)

ALLOWED_EVENT_TYPES = frozenset(
    {
        "focusin",
        "focusout",
        "keydown",
        "keyup",
        "pointerdown",
        "pointermove",
        "pointerup",
        "wheel",
    }
)

ALLOWED_KEY_CLASSES = frozenset(
    {
        "correction",
        "editing_command",
        "function",
        "modifier",
        "navigation",
        "printable",
        "system",
        "unknown",
    }
)


def sanitize_application_id(value: object) -> str:
    """Return a canonical/hashed WM_CLASS identifier, never its raw value."""

    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "unknown"))
    normalized = normalized.strip("_").lower()
    canonical = {
        "kalgebra": "KAlgebra",
        "celestia": "Celestia",
        "grass": "GrassGIS",
        "wxgui": "GrassGIS",
        "gnome-terminal": "GnomeTerminal",
        "gnome_terminal": "GnomeTerminal",
    }
    for marker, application_id in canonical.items():
        if marker in normalized:
            return application_id
    # A stable digest preserves within-session focus identity without exposing
    # an unexpected WM_CLASS value. The recorder never queries a window title.
    return "other_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def classify_keysym(name: str | None) -> str:
    """Reduce an X11 keysym name to a non-content-bearing class."""

    if not name:
        return "unknown"
    if name.startswith("F") and name[1:].isdigit():
        return "function"
    if name in {
        "Alt_L",
        "Alt_R",
        "Control_L",
        "Control_R",
        "Hyper_L",
        "Hyper_R",
        "Meta_L",
        "Meta_R",
        "Shift_L",
        "Shift_R",
        "Super_L",
        "Super_R",
    }:
        return "modifier"
    # Only destructive edit keys are corrections. Return/Tab/Insert/Clear are
    # commands, not evidence that the user corrected a prior input.
    if name in {"BackSpace", "Delete"}:
        return "correction"
    if name in {"Clear", "Insert", "Return", "Tab"}:
        return "editing_command"
    if name in {
        "Begin",
        "Down",
        "End",
        "Home",
        "Left",
        "Next",
        "Page_Down",
        "Page_Up",
        "Prior",
        "Right",
        "Up",
    }:
        return "navigation"
    if len(name) == 1 or name.startswith(("KP_", "dead_")):
        return "printable"
    if name in {"Escape", "Menu", "Pause", "Print", "Scroll_Lock", "Sys_Req"}:
        return "system"
    # Xlib returns names such as space, comma, semicolon, and quotedbl for
    # printable keys. Treat unrecognized alphabetic names as printable only
    # when they are common X11 character names; unknown is the safe fallback.
    if name in {
        "ampersand",
        "apostrophe",
        "asciicircum",
        "asciitilde",
        "asterisk",
        "at",
        "backslash",
        "bar",
        "braceleft",
        "braceright",
        "bracketleft",
        "bracketright",
        "colon",
        "comma",
        "dollar",
        "equal",
        "exclam",
        "grave",
        "greater",
        "less",
        "minus",
        "numbersign",
        "parenleft",
        "parenright",
        "percent",
        "period",
        "plus",
        "question",
        "quotedbl",
        "semicolon",
        "slash",
        "space",
        "underscore",
    }:
        return "printable"
    return "unknown"


def _walk_keys(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).lower()
            if key in FORBIDDEN_EVENT_FIELDS:
                raise ValueError(f"Privacy-forbidden event field: {'.'.join((*path, key))}")
            _walk_keys(nested, (*path, key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            _walk_keys(nested, (*path, str(index)))


def assert_privacy_safe_event(event: Mapping[str, object]) -> None:
    """Reject content-bearing or out-of-schema desktop event records."""

    if event.get("type") not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"Unsupported desktop event type: {event.get('type')!r}")
    key_class = event.get("key_class")
    if key_class is not None and key_class not in ALLOWED_KEY_CLASSES:
        raise ValueError(f"Unsupported key class: {key_class!r}")
    _walk_keys(event)
