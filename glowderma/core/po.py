"""A stdlib-only gettext catalog compiler: ``.po`` text → GNU ``.mo`` bytes.

The ops console ships its own Uzbek catalog (``locale/uz/LC_MESSAGES/django.po``, ``LOCALE_PATHS``) for
the English strings of Unfold and of Django's admin. GNU gettext (``msgfmt``, which Django's
``compilemessages`` needs) is in neither the dev container nor the image, so ``manage.py compile_locale``
uses this module and the compiled ``.mo`` is committed; a test fails when it no longer matches the ``.po``.

Supported: comments, the ``fuzzy`` flag (fuzzy entries are skipped, the header excepted), ``msgctxt``,
``msgid_plural`` / ``msgstr[n]``, multi-line strings and the C escapes ``\\n \\t \\" \\\\``.
"""

from __future__ import annotations

import ast
import struct
from dataclasses import dataclass, field

MAGIC = 0x950412DE


@dataclass
class Entry:
    msgid: str = ""
    msgid_plural: str | None = None
    msgctxt: str | None = None
    msgstr: dict[int, str] = field(default_factory=dict)
    fuzzy: bool = False

    def key(self) -> str:
        base = self.msgid if self.msgid_plural is None else f"{self.msgid}\0{self.msgid_plural}"
        return base if self.msgctxt is None else f"{self.msgctxt}\x04{base}"

    def value(self) -> str:
        return "\0".join(self.msgstr[index] for index in sorted(self.msgstr))


def _string(token: str, line_no: int) -> str:
    token = token.strip()
    if not (len(token) >= 2 and token[0] == token[-1] == '"'):
        raise ValueError(f"line {line_no}: expected a quoted string, got {token!r}")
    value = ast.literal_eval(token)
    if not isinstance(value, str):
        raise ValueError(f"line {line_no}: not a string")
    return value


def parse(text: str) -> list[Entry]:
    """The entries of a ``.po`` file, in file order."""
    entries: list[Entry] = []
    current = Entry()
    started = False
    target: tuple[str, int] | None = None  # the keyword a continuation line extends

    def finish() -> None:
        nonlocal current, started, target
        if started:
            entries.append(current)
        current, started, target = Entry(), False, None

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            finish()
            continue
        if line.startswith("#"):
            if started and target is not None and not line.startswith("#,"):
                finish()
            if line.startswith("#,") and "fuzzy" in line:
                current.fuzzy = True
            continue
        if line.startswith('"'):
            if target is None:
                raise ValueError(f"line {line_no}: continuation without a keyword")
            name, index = target
            piece = _string(line, line_no)
            if name == "msgid":
                current.msgid += piece
            elif name == "msgid_plural":
                current.msgid_plural = (current.msgid_plural or "") + piece
            elif name == "msgctxt":
                current.msgctxt = (current.msgctxt or "") + piece
            else:
                current.msgstr[index] = current.msgstr.get(index, "") + piece
            continue
        keyword, _, rest = line.partition(" ")
        if keyword in ("msgctxt", "msgid") and started and current.msgstr:
            finish()
        started = True
        value = _string(rest, line_no)
        if keyword == "msgctxt":
            current.msgctxt, target = value, ("msgctxt", 0)
        elif keyword == "msgid":
            current.msgid, target = value, ("msgid", 0)
        elif keyword == "msgid_plural":
            current.msgid_plural, target = value, ("msgid_plural", 0)
        elif keyword == "msgstr":
            current.msgstr[0], target = value, ("msgstr", 0)
        elif keyword.startswith("msgstr[") and keyword.endswith("]"):
            index = int(keyword[7:-1])
            current.msgstr[index], target = value, ("msgstr", index)
        else:
            raise ValueError(f"line {line_no}: unknown keyword {keyword!r}")
    finish()
    return entries


def compile_po(text: str) -> bytes:
    """The ``.mo`` bytes of ``text`` (untranslated and fuzzy entries left out, like ``msgfmt``)."""
    catalog: dict[bytes, bytes] = {}
    for entry in parse(text):
        if entry.fuzzy and entry.msgid:
            continue
        if not any(entry.msgstr.values()):
            continue
        catalog[entry.key().encode("utf-8")] = entry.value().encode("utf-8")
    keys = sorted(catalog)
    count = len(keys)
    originals_at = 7 * 4
    translations_at = originals_at + count * 8
    data_at = translations_at + count * 8
    tables: list[int] = []
    blob = b""
    for source in (keys, [catalog[key] for key in keys]):
        for item in source:
            tables.extend((len(item), data_at + len(blob)))
            blob += item + b"\0"
    header = struct.pack("<7I", MAGIC, 0, count, originals_at, translations_at, 0, data_at)
    originals = struct.pack(f"<{count * 2}I", *tables[: count * 2])
    translations = struct.pack(f"<{count * 2}I", *tables[count * 2 :])
    return header + originals + translations + blob
