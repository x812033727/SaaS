"""LINE /lang command parsing utilities."""

from __future__ import annotations

_LANG_CMD_PREFIX = "/lang "


def parse_lang_command(text: str) -> tuple[str | None, str]:
    """Parse a ``/lang <code> [message]`` command from a LINE text message.

    Returns:
        ``(lang_code, remaining_text)`` when the message is a valid ``/lang``
        command.  *lang_code* is lower-cased; *remaining_text* is the text
        after the language code (may be empty string).

        ``(None, original_text)`` when the message is **not** a ``/lang``
        command or is malformed (no code after ``/lang``).

    Examples::

        >>> parse_lang_command("/lang ja")
        ('ja', '')
        >>> parse_lang_command("/lang en hello world")
        ('en', 'hello world')
        >>> parse_lang_command("/lang zh-tw 你好")
        ('zh-tw', '你好')
        >>> parse_lang_command("hello world")
        (None, 'hello world')
        >>> parse_lang_command("/lang")
        (None, '/lang')
        >>> parse_lang_command("/lang ")
        (None, '/lang ')

    Note:
        The prefix is **case-sensitive** (``/lang``, not ``/Lang``).
        The language code is **lowercased** before being returned so callers
        can do case-insensitive comparisons.
    """
    if not text.startswith(_LANG_CMD_PREFIX):
        return None, text

    rest = text[len(_LANG_CMD_PREFIX):]
    parts = rest.split(None, 1)  # split on first whitespace run

    if not parts:
        # "/lang " followed by only whitespace — not valid
        return None, text

    lang_code = parts[0].lower()
    remaining = parts[1] if len(parts) > 1 else ""
    return lang_code, remaining
