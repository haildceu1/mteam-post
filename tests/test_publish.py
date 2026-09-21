from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_title_renamer import publish
from media_title_renamer.prepare import _bencode


class PublishTests(unittest.TestCase):
    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_existing_json_skips_prepare(self, prepare_main, mteam_fill_main) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "mteam-prepare.json"
            package.write_text("{}", encoding="utf-8")
            publish.main([str(package), "--profile-dir", r"C:\Profiles\mteam"])

        prepare_main.assert_not_called()
        self.assertEqual(mteam_fill_main.call_args.args[0][0], str(package.resolve()))

    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_prepare_directory_skips_prepare(self, prepare_main, mteam_fill_main) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepare_dir = Path(directory) / "Example.prepare"
            prepare_dir.mkdir()
            package = prepare_dir / "mteam-prepare.json"
            package.write_text("{}", encoding="utf-8")
            publish.main([str(prepare_dir), "--cookie-file", r"C:\Secrets\mteam.txt"])

        prepare_main.assert_not_called()
        self.assertEqual(mteam_fill_main.call_args.args[0][0], str(package.resolve()))

    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_publish_automatically_reuses_package_by_original_input(self, prepare_main, mteam_fill_main) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "The Office"
            root.mkdir()
            prepare_dir = Path(directory) / "The Office S01.prepare"
            prepare_dir.mkdir()
            package = prepare_dir / "mteam-prepare.json"
            package.write_text(
                json.dumps({"input_path": str(root), "created_at": 1}),
                encoding="utf-8",
            )
            publish.main([str(root), "--profile-dir", r"C:\Profiles\mteam"])

        prepare_main.assert_not_called()
        self.assertEqual(mteam_fill_main.call_args.args[0][0], str(package.resolve()))

    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_reused_single_file_package_with_apply_renames_to_release_filename(
        self, prepare_main, mteam_fill_main
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mkv"
            source.write_bytes(b"video")
            prepare_dir = Path(directory) / "Movie 2024.prepare"
            prepare_dir.mkdir()
            package = prepare_dir / "mteam-prepare.json"
            target_name = "Movie 2024 BluRay 1080p AVC DD5.1-GRP.mkv"
            package.write_text(
                json.dumps(
                    {
                        "input_path": str(source),
                        "prepared_path": str(source),
                        "filename": target_name,
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            publish.main(
                [
                    str(source),
                    "--apply",
                    "--yes",
                    "--no-upload",
                    "--profile-dir",
                    r"C:\Profiles\mteam",
                ]
            )

            target = source.with_name(target_name)
            self.assertFalse(source.exists())
            self.assertTrue(target.exists())
            saved = json.loads(package.read_text(encoding="utf-8"))
            self.assertEqual(saved["prepared_path"], str(target))
            backup = prepare_dir / "rename-backup.txt"
            self.assertTrue(backup.is_file())
            self.assertIn("input.mkv", backup.read_text(encoding="utf-8"))

        prepare_main.assert_not_called()
        self.assertEqual(mteam_fill_main.call_args.args[0][0], str(package.resolve()))

    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_reused_folder_package_with_apply_uses_cached_rename_plan(
        self, prepare_main, mteam_fill_main
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Example Show"
            root.mkdir()
            prepare_dir = Path(directory) / "Example Show 2024 S01.prepare"
            prepare_dir.mkdir()
            package = prepare_dir / "mteam-prepare.json"
            package.write_text(
                json.dumps(
                    {
                        "input_path": str(root),
                        "prepared_path": str(root),
                        "target_filename": "Example Show-2024-S01",
                        "kind": "tv",
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )
            prepare_main.return_value = package

            publish.main([str(root), "--apply", "--yes", "--no-upload"])

        prepare_main.assert_called_once_with([str(root), "--apply", "--yes"])
        self.assertEqual(mteam_fill_main.call_args.args[0][0], str(package.resolve()))

    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_refresh_prepare_ignores_existing_package(self, prepare_main, mteam_fill_main) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Movie.mkv"
            root.write_bytes(b"video")
            prepare_dir = Path(directory) / "Movie.prepare"
            prepare_dir.mkdir()
            (prepare_dir / "mteam-prepare.json").write_text(
                json.dumps({"input_path": str(root), "created_at": 1}),
                encoding="utf-8",
            )
            generated = Path(directory) / "Fresh.prepare" / "mteam-prepare.json"
            prepare_main.return_value = generated
            publish.main(
                [
                    str(root),
                    "--refresh-prepare",
                    "--apply",
                    "--profile-dir",
                    r"C:\Profiles\mteam",
                ]
            )

        prepare_main.assert_called_once_with([str(root), "--apply"])
        self.assertEqual(mteam_fill_main.call_args.args[0][0], str(generated))

    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_refresh_prepare_can_reuse_existing_single_file_torrent(
        self, prepare_main, mteam_fill_main
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mkv"
            source.write_bytes(b"video")
            prepare_dir = Path(directory) / "Movie 2024.prepare"
            prepare_dir.mkdir()
            package = prepare_dir / "mteam-prepare.json"
            torrent = prepare_dir / "Movie 2024.torrent"
            torrent.write_bytes(b"existing torrent")
            target_name = "Movie 2024 BluRay 1080p AVC DD5.1-GRP.mkv"
            old_torrent = {"path": str(torrent), "format": "v1", "private": True}
            package.write_text(
                json.dumps(
                    {
                        "input_path": str(source),
                        "prepared_path": str(source),
                        "filename": target_name,
                        "kind": "movie",
                        "torrent": old_torrent,
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            def fake_prepare(argv):
                self.assertIn("--skip-torrent", argv)
                self.assertIn("--output", argv)
                refreshed = {
                    "input_path": str(source),
                    "prepared_path": str(source),
                    "filename": target_name,
                    "kind": "movie",
                    "title": "refreshed metadata",
                    "torrent": {"path": "", "format": "v1", "private": True},
                }
                package.write_text(json.dumps(refreshed), encoding="utf-8")
                return package

            prepare_main.side_effect = fake_prepare
            publish.main(
                [
                    str(source),
                    "--refresh-prepare",
                    "--reuse-torrent",
                    "--apply",
                    "--yes",
                    "--no-upload",
                    "--profile-dir",
                    r"C:\Profiles\mteam",
                ]
            )

            target = source.with_name(target_name)
            self.assertFalse(source.exists())
            self.assertTrue(target.exists())
            saved = json.loads(package.read_text(encoding="utf-8"))
            self.assertEqual(saved["torrent"], old_torrent)
            self.assertEqual(saved["prepared_path"], str(target))

        self.assertEqual(mteam_fill_main.call_args.args[0][0], str(package.resolve()))

    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_refresh_prepare_recovers_missing_json_from_matching_folder_torrent(
        self, prepare_main, mteam_fill_main
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Justified-2010-S01-S06-[tmdb=1436]"
            root.mkdir()
            first = root / "Season 01" / "Justified 2010 S01E01.mkv"
            second = root / "Season 02" / "Justified 2010 S02E01.mkv"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            prepare_dir = Path(directory) / "Justified 2010 S01-S06.prepare"
            prepare_dir.mkdir()
            torrent = prepare_dir / f"{root.name}.torrent"
            torrent.write_bytes(
                _bencode(
                    {
                        b"info": {
                            b"name": root.name.encode(),
                            b"piece length": 16384,
                            b"private": 1,
                            b"files": [
                                {b"length": 5, b"path": [b"Season 01", b"Justified 2010 S01E01.mkv"]},
                                {b"length": 6, b"path": [b"Season 02", b"Justified 2010 S02E01.mkv"]},
                            ],
                            b"pieces": b"x" * 40,
                        }
                    }
                )
            )

            def fake_prepare(argv):
                self.assertIn("--skip-torrent", argv)
                package = prepare_dir / "mteam-prepare.json"
                package.write_text(
                    json.dumps(
                        {
                            "input_path": str(root),
                            "prepared_path": str(root),
                            "filename": root.name,
                            "kind": "tv",
                            "files": [
                                {"relative_path": "Season 01/Justified 2010 S01E01.mkv"},
                                {"relative_path": "Season 02/Justified 2010 S02E01.mkv"},
                            ],
                            "torrent": {"path": "", "format": "v1", "private": True},
                        }
                    ),
                    encoding="utf-8",
                )
                return package

            prepare_main.side_effect = fake_prepare
            publish.main(
                [
                    str(root),
                    "--refresh-prepare",
                    "--reuse-torrent",
                    "--apply",
                    "--yes",
                    "--no-upload",
                    "--profile-dir",
                    r"C:\Profiles\mteam",
                ]
            )

            recovered = json.loads((prepare_dir / "mteam-prepare.json").read_text(encoding="utf-8"))
            self.assertEqual(recovered["torrent"]["path"], str(torrent.resolve()))
            self.assertTrue(recovered["recovered_from_torrent"])

        self.assertEqual(mteam_fill_main.call_args.args[0][0], str((prepare_dir / "mteam-prepare.json").resolve()))

    def test_profile_directory_can_be_configured_by_environment(self) -> None:
        with patch.dict(os.environ, {"MTEAM_PROFILE_DIR": r"E:\Profiles\mteam"}):
            self.assertEqual(
                publish._parser().parse_args([r"F:\Movie\Example.mkv"]).profile_dir,
                Path(r"E:\Profiles\mteam"),
            )

    def test_linux_profile_directory_uses_xdg_config_home(self) -> None:
        with (
            patch("media_title_renamer.publish.sys.platform", "linux"),
            patch.dict(
                os.environ,
                {"MTEAM_PROFILE_DIR": "", "XDG_CONFIG_HOME": "/tmp/xdg-config"},
            ),
        ):
            self.assertEqual(
                publish._default_profile_dir(),
                Path("/tmp/xdg-config/mteam-post/chrome-profile"),
            )

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
                "--login-timeout",
                "600",
            ]
        )

    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_hardlink_is_forwarded_to_fresh_prepare(self, prepare_main, mteam_fill_main) -> None:
        package = Path(r"F:\TV\Example.prepare\mteam-prepare.json")
        prepare_main.return_value = package

        publish.main(
            [
                r"F:\TV\Example",
                "--hardlink",
                "--apply",
                "--no-upload",
                "--profile-dir",
                r"C:\Profiles\mteam-chrome-profile",
            ]
        )

        prepare_main.assert_called_once_with([r"F:\TV\Example", "--apply", "--hardlink"])
        self.assertEqual(mteam_fill_main.call_args.args[0][0], str(package))

    @patch("media_title_renamer.publish.mteam_fill_main")
    @patch("media_title_renamer.publish.prepare_main")
    def test_no_upload_is_forwarded(self, prepare_main, mteam_fill_main) -> None:
        package = Path(r"F:\Movie\Example.prepare\mteam-prepare.json")
        prepare_main.return_value = package

        publish.main([r"F:\Movie\Example.mkv", "--apply", "--no-upload", "--cookie-file", r"C:\Secrets\mteam.txt"])

        self.assertNotIn("--upload", mteam_fill_main.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
