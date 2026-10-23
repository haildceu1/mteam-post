import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from media_title_renamer import prepare
from media_title_renamer.douban_cookie import load_douban_cookie_header


class DoubanCookieTests(unittest.TestCase):
    def test_xlsx_reads_only_live_douban_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Domain", "Include Subdomains", "Path", "Secure", "Expiry", "Name", "Value"])
            sheet.append([".douban.com", True, "/", True, 0, "bid", "fake-douban-value"])
            sheet.append(["accounts.google.com", True, "/", True, 0, "sid", "not-douban"])
            sheet.append([".douban.com", True, "/", True, 1, "expired", "old-value"])
            workbook.save(path)

            header = load_douban_cookie_header(path)

        self.assertEqual(header, "bid=fake-douban-value")

    def test_netscape_cookie_file_filters_domain_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".douban.com\tTRUE\t/\tTRUE\t0\tbid\tactive-value\n"
                ".example.com\tTRUE\t/\tTRUE\t0\tother\tother-value\n"
                ".douban.com\tTRUE\t/\tTRUE\t1\texpired\told-value\n",
                encoding="utf-8",
            )

            header = load_douban_cookie_header(path)

        self.assertEqual(header, "bid=active-value")

def test_prepare_uses_existing_local_default_cookie(tmp_path, monkeypatch):
    path = tmp_path / "douban.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Domain", "Include Subdomains", "Path", "Secure", "Expiry", "Name", "Value"])
    sheet.append([".douban.com", True, "/", True, 0, "bid", "private-test-cookie"])
    workbook.save(path)
    monkeypatch.delenv("DOUBAN_COOKIE_FILE", raising=False)
    monkeypatch.setattr(prepare, "DEFAULT_DOUBAN_COOKIE_FILE", path)

    headers = prepare._douban_request_headers()

    assert headers["Cookie"] == "bid=private-test-cookie"


if __name__ == "__main__":
    unittest.main()
