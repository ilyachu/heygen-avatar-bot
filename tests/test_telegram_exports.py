import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from domain.telegram_exports import (
    html_to_plain_text,
    parse_telegram_exports,
)


PART_ONE = """<!doctype html>
<html><body>
  <div class="page_header"><div class="content"><div class="text bold">Канал «Тест»</div></div></div>
  <div class="history">
    <div class="message service clearfix" id="message-1">
      <div class="body details">Channel created</div>
    </div>
    <div class="message default clearfix" id="message-2">
      <div class="pull_right date details" title="13.09.2026 09:05:00 UTC+03:00">09:05</div>
      <div class="from_name">Алина</div>
      <div class="text">Первый абзац.<br>Вторая строка.<p>Новый абзац и <a href="https://example.com">ссылка</a>.</p></div>
    </div>
    <div class="message default clearfix joined" id="message-3">
      <div class="body">
        <div class="text">Продолжение от того же автора.</div>
      </div>
    </div>
    <div class="message default clearfix" id="message-4">
      <div class="pull_right date details" title="2026-09-13 09:07:00 UTC+03:00">09:07</div>
      <div class="media_wrap clearfix"><div class="media"></div></div>
      <div class="text">Photo</div>
    </div>
  </div>
</body></html>"""

PART_TWO = """<html><body>
  <div class="page_header"><div class="content"><div class="text bold">Канал «Тест»</div></div></div>
  <div class="history">
    <div class="message default clearfix joined" id="message-5">
      <div class="text"><b>Новый день</b> и <a href="https://example.org">https://example.org</a></div>
    </div>
    <div class="message default clearfix" id="message-6">
      <div class="pull_right date details" title="2026-09-13 09:09:00 UTC+03:00">09:09</div>
      <div class="media_wrap clearfix"><div class="media"></div></div>
    </div>
  </div>
</body></html>"""

PART_THREE = """<html><body>
  <div class="history">
    <div class="message default clearfix" id="message-7">
      <div class="pull_right date details" title="2026-09-13 09:10:00 UTC+03:00">09:10</div>
      <div class="from_name">Борис</div>
      <div class="text">Сообщение из messages2.html.</div>
    </div>
  </div>
</body></html>"""


class TelegramExportParserTests(unittest.TestCase):
    def test_html_to_plain_text_keeps_breaks_and_links(self):
        text = html_to_plain_text(
            '<p>Один&nbsp;два</p><p><a href="https://example.test">Читать</a></p>'
        )
        self.assertEqual("Один два\n\nЧитать (https://example.test)", text)

    def test_directory_parser_merges_split_parts_and_inherits_joined_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "ChatExport_2026-09-13 messages2.html").write_text(
                PART_TWO, encoding="utf-8"
            )
            (directory / "ChatExport_2026-09-13.html").write_text(
                PART_ONE, encoding="utf-8"
            )

            export = parse_telegram_exports(directory)

        self.assertEqual("Канал «Тест»", export.title)
        self.assertEqual(3, export.message_count)
        self.assertEqual([2, 3, 5], [message.message_id for message in export.messages])
        self.assertEqual("Алина", export.messages[1].sender)
        self.assertEqual(export.messages[0].published_at, export.messages[1].published_at)
        self.assertEqual("Алина", export.messages[2].sender)
        self.assertEqual(export.messages[0].published_at, export.messages[2].published_at)
        self.assertEqual(
            "Первый абзац.\nВторая строка.\n\nНовый абзац и ссылка (https://example.com).",
            export.messages[0].text,
        )

    def test_duplicate_ids_are_not_imported_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            content = PART_ONE.replace("message-3", "message-2").replace(
                "Продолжение от того же автора.", "Дубликат"
            )
            (directory / "ChatExport_2026-09-13-a.html").write_text(content, encoding="utf-8")
            (directory / "ChatExport_2026-09-13-b.html").write_text(content, encoding="utf-8")
            export = parse_telegram_exports(directory)

        self.assertEqual(1, sum(message.message_id == 2 for message in export.messages))

    def test_real_export_directories_discover_messages_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            first = parent / "ChatExport_2026-09-13 (1)"
            second = parent / "ChatExport_2026-09-13 (2)"
            historical = parent / "ChatExport_2025-01-02"
            first.mkdir()
            second.mkdir()
            historical.mkdir()
            (first / "messages.html").write_text(PART_ONE, encoding="utf-8")
            (second / "messages.html").write_text(PART_TWO, encoding="utf-8")
            (second / "messages2.html").write_text(PART_THREE, encoding="utf-8")
            (historical / "messages.html").write_text(
                PART_THREE.replace("message-7", "message-8"), encoding="utf-8"
            )

            export = parse_telegram_exports(parent)
            glob_export = parse_telegram_exports(str(parent / "ChatExport_2026-09-13*"))
            single_export = parse_telegram_exports(first)

        self.assertEqual(["messages.html", "messages.html", "messages.html", "messages2.html", "messages.html"], [
            Path(path).name for path in (message.source_file for message in export.messages)
        ])
        self.assertEqual([2, 3, 5, 7, 8], [message.message_id for message in export.messages])
        self.assertEqual([2, 3, 5, 7], [message.message_id for message in glob_export.messages])
        self.assertEqual([2, 3], [message.message_id for message in single_export.messages])


if __name__ == "__main__":
    unittest.main()
