"""Parser for Telegram Desktop HTML chat exports.

Telegram Desktop writes one or more ``ChatExport_*.html`` files containing a
small, slightly inconsistent HTML document.  This module deliberately uses
only the standard library so it can also be used by import scripts before the
rest of the application is configured.
"""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence


_MESSAGE_ID_RE = re.compile(r"(?:message[-_])?(\d+)$", re.IGNORECASE)
_ISO_DATE_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)"
)
_TELEGRAM_DATE_RE = re.compile(
    r"(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})\s+"
    r"(?P<time>\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)"
    r"(?:\s+UTC(?P<offset>[+-]\d{2}:\d{2}))?"
)
_MEDIA_LABELS = {
    "audio",
    "аудио",
    "document",
    "file",
    "файл",
    "photo",
    "фото",
    "poll",
    "опрос",
    "sticker",
    "стикер",
    "video",
    "видео",
    "voice message",
    "voice message audio",
    "голосовое сообщение",
    "геопозиция",
    "контакт",
    "анимация",
}
_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "dd",
    "div",
    "dl",
    "dt",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}


@dataclass(frozen=True)
class TelegramMessage:
    """A dated, text-bearing message from an exported Telegram channel."""

    message_id: int | None
    published_at: datetime
    sender: str
    text: str
    source_file: str = ""

    @property
    def date(self) -> datetime:
        """Compatibility alias for callers that use ``date`` for Telegram messages."""

        return self.published_at

    def as_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "published_at": self.published_at.isoformat(),
            "sender": self.sender,
            "text": self.text,
            "source_file": self.source_file,
        }


@dataclass(frozen=True)
class TelegramExport:
    title: str
    messages: tuple[TelegramMessage, ...]
    files: tuple[Path, ...]

    @property
    def message_count(self) -> int:
        return len(self.messages)


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(
        self,
        tag: str,
        attrs: Mapping[str, str | None] | None = None,
        parent: _Node | None = None,
    ) -> None:
        self.tag = tag.lower()
        self.attrs = dict(attrs or {})
        self.children: list[_Node | str] = []
        self.parent = parent


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(node)
        if tag.lower() not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self._stack[-1].tag == tag.lower():
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


def _walk(node: _Node) -> Iterator[_Node]:
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _classes(node: _Node) -> set[str]:
    return set((node.attrs.get("class") or "").split())


def _has_class(node: _Node, name: str) -> bool:
    return name in _classes(node)


def _find_all(node: _Node, predicate) -> list[_Node]:
    return [candidate for candidate in _walk(node) if predicate(candidate)]


def _find_first(node: _Node, predicate) -> _Node | None:
    return next((candidate for candidate in _walk(node) if predicate(candidate)), None)


def _raw_text(node: _Node | str) -> str:
    if isinstance(node, str):
        return node
    return "".join(_raw_text(child) for child in node.children)


def _normalise_text(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in value.split("\n")]
    value = "\n".join(lines)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _render_text(node: _Node | str) -> str:
    if isinstance(node, str):
        return node

    if node.tag == "br":
        return "\n"
    if node.tag in {"script", "style", "noscript"}:
        return ""

    rendered = "".join(_render_text(child) for child in node.children)
    if node.tag == "a":
        href = (node.attrs.get("href") or "").strip()
        label = _normalise_text(rendered)
        if href and label and href.casefold() != label.casefold():
            rendered = f"{label} ({href})"
        elif label:
            rendered = label
        elif href:
            rendered = href
    if node.tag in _BLOCK_TAGS and rendered.strip():
        # Paragraphs are the meaningful visual separators in Telegram text.
        # Parent containers must not collapse their trailing blank line.
        if node.tag == "p":
            rendered = "\n\n" + rendered.strip("\n") + "\n\n"
        elif not rendered.endswith("\n"):
            rendered += "\n"
    return rendered


def html_to_plain_text(node_or_html: _Node | str) -> str:
    """Convert Telegram message HTML to readable plain text.

    Paragraphs and ``<br>`` become line breaks.  Links retain their visible
    label and URL, e.g. ``Read more (https://example.com)``.
    """

    if isinstance(node_or_html, str):
        parser = _DocumentParser()
        parser.feed(node_or_html)
        parser.close()
        return _normalise_text(_render_text(parser.root))
    return _normalise_text(_render_text(node_or_html))


def _outside_message(node: _Node) -> bool:
    parent = node.parent
    while parent is not None:
        if _has_class(parent, "message"):
            return False
        parent = parent.parent
    return True


def _message_nodes(root: _Node) -> list[_Node]:
    return [
        node
        for node in _find_all(root, lambda item: _has_class(item, "message"))
        if _outside_message(node)
    ]


def _first_text(node: _Node, class_name: str) -> str:
    candidate = _find_first(node, lambda item: _has_class(item, class_name))
    return _normalise_text(_raw_text(candidate)) if candidate else ""


def _message_text(node: _Node) -> str:
    text_nodes = _find_all(node, lambda item: _has_class(item, "text"))
    rendered = "\n".join(_render_text(item) for item in text_nodes)
    return _normalise_text(rendered)


def _is_media_only(node: _Node, text: str) -> bool:
    if text.casefold() in _MEDIA_LABELS:
        return True
    has_media = bool(
        _find_first(
            node,
            lambda item: bool(_classes(item) & {"media", "media_wrap", "photo", "video", "document"}),
        )
    )
    return has_media and not text


def _parse_message_id(node: _Node) -> int | None:
    raw_id = (node.attrs.get("id") or "").strip()
    match = _MESSAGE_ID_RE.search(raw_id)
    return int(match.group(1)) if match else None


def _parse_datetime(raw: str, previous: datetime | None) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return previous

    match = _ISO_DATE_RE.search(raw)
    if match:
        date_text = match.group("date")
        time_text = match.group("time")
        suffix = raw[match.end("time") :].strip()
        if suffix.upper().startswith("UTC"):
            suffix = suffix[3:]
        try:
            return datetime.fromisoformat(f"{date_text}T{time_text}{suffix}")
        except ValueError:
            pass

    telegram_match = _TELEGRAM_DATE_RE.search(raw)
    if telegram_match:
        offset = telegram_match.group("offset") or ""
        value = (
            f"{telegram_match.group('year')}-{telegram_match.group('month')}-"
            f"{telegram_match.group('day')}T{telegram_match.group('time')}{offset}"
        )
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass

    if previous is not None:
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                parsed_time = datetime.strptime(raw, fmt).time()
                return datetime.combine(previous.date(), parsed_time, previous.tzinfo)
            except ValueError:
                continue
    return previous


def _message_datetime(node: _Node, previous: datetime | None) -> datetime | None:
    date_node = _find_first(node, lambda item: _has_class(item, "date"))
    if date_node is None:
        return previous
    title = (date_node.attrs.get("title") or "").strip()
    return _parse_datetime(title or _raw_text(date_node), previous)


def _extract_title(root: _Node) -> str:
    page_header = _find_first(root, lambda item: _has_class(item, "page_header"))
    if page_header:
        candidates = _find_all(page_header, lambda item: _has_class(item, "text"))
        if candidates:
            title = _normalise_text(_raw_text(candidates[0]))
            if title:
                return title

    for class_name in ("person_name", "chat_title", "channel_title"):
        candidate = _find_first(root, lambda item: _has_class(item, class_name))
        if candidate:
            title = _normalise_text(_raw_text(candidate))
            if title:
                return title

    # Older exports put the channel name in the initial service message.
    for node in _message_nodes(root):
        if _has_class(node, "service"):
            candidate = _find_first(node, lambda item: _has_class(item, "title"))
            if candidate:
                title = _normalise_text(_raw_text(candidate))
                if title and title.casefold() not in {"channel created", "chat created"}:
                    return title
    return ""


def _parse_document(
    path: Path,
    previous_sender: str = "",
    previous_date: datetime | None = None,
) -> tuple[str, list[TelegramMessage], str, datetime | None]:
    parser = _DocumentParser()
    parser.feed(path.read_text(encoding="utf-8-sig", errors="replace"))
    parser.close()
    root = parser.root
    title = _extract_title(root)
    messages: list[TelegramMessage] = []
    for node in _message_nodes(root):
        published_at = _message_datetime(node, previous_date)
        sender = _first_text(node, "from_name") or previous_sender
        text = _message_text(node)
        if not text or _has_class(node, "service") or _is_media_only(node, text):
            continue
        if published_at is not None:
            previous_date = published_at
        if sender:
            previous_sender = sender
        if not published_at:
            continue
        messages.append(
            TelegramMessage(
                message_id=_parse_message_id(node),
                published_at=published_at,
                sender=sender,
                text=text,
                source_file=str(path),
            )
        )
    return title, messages, previous_sender, previous_date


def _natural_name_key(name: str) -> tuple[object, ...]:
    stem = Path(name).stem
    part_match = re.search(r"\s*(?:\(\s*part\s*(\d+)\s*\)|messages(?:\s*(\d+))?)$", stem, re.IGNORECASE)
    if part_match:
        part_number = int(part_match.group(1) or part_match.group(2) or 1)
        base = stem[: part_match.start()].rstrip()
        return (base.casefold(), 1, part_number, stem.casefold())
    return (stem.casefold(), 0, 0, stem.casefold())


def _natural_path_key(path: Path) -> tuple[object, ...]:
    return (_natural_name_key(path.parent.name), _natural_name_key(path.name))


def _export_directory_files(directory: Path) -> list[Path]:
    """Return Telegram Desktop's files inside one ``ChatExport_*`` directory."""

    return list(directory.glob("messages*.html"))


def discover_export_files(path_or_pattern: str | Path) -> list[Path]:
    """Resolve a file, glob, or export directory into naturally sorted HTML files."""

    raw = str(path_or_pattern)
    if any(char in raw for char in "*?[]"):
        paths = []
        for item in glob.glob(raw):
            path = Path(item)
            paths.extend(_export_directory_files(path) if path.is_dir() else [path])
    else:
        path = Path(raw)
        if path.is_file():
            paths = [path]
        elif path.is_dir():
            paths = _export_directory_files(path)
            if not paths:
                export_directories = sorted(
                    (
                        child
                        for child in path.glob("ChatExport_*")
                        if child.is_dir()
                    ),
                    key=_natural_path_key,
                )
                for export_directory in export_directories:
                    paths.extend(_export_directory_files(export_directory))
            if not paths:
                paths = list(path.glob("ChatExport_*.html"))
        else:
            paths = []
    paths = sorted({path.resolve() for path in paths if path.suffix.casefold() == ".html"}, key=_natural_path_key)
    if not paths:
        raise FileNotFoundError(f"Telegram HTML export not found: {path_or_pattern}")
    return paths


def parse_telegram_exports(paths: Iterable[str | Path] | str | Path) -> TelegramExport:
    """Parse one export or all matching split export files and merge messages."""

    if isinstance(paths, (str, Path)):
        raw_paths: Sequence[str | Path] = [paths]
    else:
        raw_paths = list(paths)
    files: list[Path] = []
    for item in raw_paths:
        files.extend(discover_export_files(item))
    files = sorted(set(files), key=_natural_path_key)

    title = ""
    messages: list[TelegramMessage] = []
    seen_ids: set[int] = set()
    previous_sender = ""
    previous_date: datetime | None = None
    for path in files:
        file_title, file_messages, previous_sender, previous_date = _parse_document(
            path, previous_sender, previous_date
        )
        title = title or file_title
        for message in file_messages:
            if message.message_id is not None and message.message_id in seen_ids:
                continue
            if message.message_id is not None:
                seen_ids.add(message.message_id)
            messages.append(message)

    if not files:
        raise FileNotFoundError("No Telegram HTML export files were supplied")
    messages.sort(key=lambda item: (item.published_at.isoformat(), item.message_id if item.message_id is not None else -1))
    return TelegramExport(title=title, messages=tuple(messages), files=tuple(files))


def parse_export(path: str | Path) -> TelegramExport:
    """Short alias retained for simple CLI and integration callers."""

    return parse_telegram_exports(path)


__all__ = [
    "TelegramExport",
    "TelegramMessage",
    "discover_export_files",
    "html_to_plain_text",
    "parse_export",
    "parse_telegram_exports",
]
