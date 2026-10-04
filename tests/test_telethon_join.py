import unittest

from scripts.telethon_join import _url_kind


class UrlKindTests(unittest.TestCase):
    def test_private_invite(self):
        self.assertEqual(_url_kind("https://t.me/+hash"), ("invite", "hash"))

    def test_public_t_me(self):
        self.assertEqual(_url_kind("https://t.me/name"), ("public", "name"))

    def test_public_telegram_me(self):
        self.assertEqual(_url_kind("https://telegram.me/name"), ("public", "name"))

    def test_invalid_host_and_path(self):
        self.assertIsNone(_url_kind("https://example.com/name"))
        self.assertIsNone(_url_kind("https://t.me/joinchat/hash"))


if __name__ == "__main__":
    unittest.main()
