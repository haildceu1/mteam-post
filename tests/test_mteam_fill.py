import tempfile
import unittest
from pathlib import Path

from media_title_renamer.mteam_fill import _has_mteam_auth, _wait_for_mteam_auth, load_mteam_session


class MTeamFillTests(unittest.TestCase):
    def test_login_wait_accepts_existing_local_storage_auth(self):
        class Driver:
            def execute_script(self, _script):
                return "saved-token"

        driver = Driver()
        self.assertTrue(_has_mteam_auth(driver))
        self.assertTrue(_wait_for_mteam_auth(driver, 0))

    def test_parses_devtools_request_header_dump_as_local_storage_session(self):
        text = "\n".join(
            [
                "请求网址",
                "https://api.m-team.cc/api/member/bases",
                "authorization",
                "eyJfake-token",
                "did",
                "device-id",
                "visitorid",
                "visitor-id",
                "version",
                "1.1.7",
                "webversion",
                "1170",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.txt"
            path.write_text(text, encoding="utf-8")
            session = load_mteam_session(path)
        self.assertTrue(session.is_auth_dump)
        self.assertEqual(session.authorization, "eyJfake-token")
        self.assertEqual(session.did, "device-id")
        self.assertEqual(session.visitor_id, "visitor-id")
        self.assertEqual(session.version, "1.1.7")
        self.assertEqual(session.web_version, "1170")

    def test_parses_only_mteam_netscape_cookies(self):
        text = "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".example.com\tTRUE\t/\tFALSE\t2000000000\tother\tignored",
                ".kp.m-team.cc\tTRUE\t/\tTRUE\t2000000000\tauth\tsecret-value",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            path.write_text(text, encoding="utf-8")
            session = load_mteam_session(path)
        self.assertFalse(session.is_auth_dump)
        self.assertEqual(len(session.cookies), 1)
        self.assertEqual(session.cookies[0]["domain"], ".kp.m-team.cc")
        self.assertEqual(session.cookies[0]["name"], "auth")


if __name__ == "__main__":
    unittest.main()
