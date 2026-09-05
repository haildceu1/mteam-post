from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from media_title_renamer import publish


class PublishTests(unittest.TestCase):
    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_combines_prepare_and_form_fill(self, prepare_main, mteam_fill_main) -> None:
        package = Path(r"F:\TV\Example.prepare\mteam-prepare.json")
        prepare_main.return_value = package

        publish.main(
            [
                r"F:\TV\Example",
                "--apply",
                "--category",
                "影剧/综艺/HD",
                "--profile-dir",
                r"C:\Profiles\mteam-chrome-profile",
                "--keep-open",
            ]
        )

        prepare_main.assert_called_once_with(
            [r"F:\TV\Example", "--apply", "--category", "影剧/综艺/HD"]
        )
        mteam_fill_main.assert_called_once_with(
            [
                str(package),
                "--profile-dir",
                r"C:\Profiles\mteam-chrome-profile",
                "--url",
                "https://kp.m-team.cc/upload",
                "--upload",
                "--keep-open",
            ]
        )

    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_no_upload_is_forwarded(self, prepare_main, mteam_fill_main) -> None:
        package = Path(r"F:\Movie\Example.prepare\mteam-prepare.json")
        prepare_main.return_value = package

        publish.main([r"F:\Movie\Example.mkv", "--apply", "--no-upload", "--cookie-file", r"C:\Secrets\mteam.txt"])

        self.assertNotIn("--upload", mteam_fill_main.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
