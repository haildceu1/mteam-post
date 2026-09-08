import tempfile
import unittest
import zipfile
from pathlib import Path

from media_title_renamer.subtitle import (
    _subtitle_parser,
    ensure_nonempty_for_submission,
    generate_empty_series_subtitles,
    prepare_upload_files,
    subtitle_filename,
)


class SubtitleTests(unittest.TestCase):
    def test_survivor_season_seven_names_use_chs_suffix(self):
        self.assertEqual(subtitle_filename("Survivor", 7, 1), "Survivor.S07E01.chs.srt")
        self.assertEqual(subtitle_filename("Survivor", 7, 15), "Survivor.S07E15.chs.srt")

    def test_generate_fifteen_empty_srt_files_and_series_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, archive = generate_empty_series_subtitles(
                Path(directory),
                series="Survivor",
                season=7,
                episode_count=15,
            )
            self.assertEqual(len(paths), 15)
            self.assertEqual(paths[0].name, "Survivor.S07E01.chs.srt")
            self.assertEqual(paths[-1].name, "Survivor.S07E15.chs.srt")
            self.assertTrue(all(path.stat().st_size == 0 for path in paths))
            self.assertEqual(archive.name, "Survivor.S07.chs.zip")
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(bundle.namelist(), [path.name for path in paths])

    def test_directory_batch_resolves_to_one_deduplicated_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _paths, generated_archive = generate_empty_series_subtitles(
                root,
                series="Survivor",
                season=7,
                episode_count=15,
            )
            uploads = prepare_upload_files([root])
            self.assertEqual(uploads, [generated_archive])

    def test_submit_refuses_generated_empty_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            _paths, archive = generate_empty_series_subtitles(
                Path(directory),
                series="Survivor",
                season=7,
                episode_count=15,
            )
            with self.assertRaisesRegex(ValueError, "空字幕"):
                ensure_nonempty_for_submission([archive])

    def test_auto_pack_does_not_overwrite_an_existing_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "Show.S01E01.chs.srt"
            second = root / "Show.S01E02.chs.srt"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            existing = root / "Show.S01.chs.zip"
            existing.write_bytes(b"keep me")

            uploads = prepare_upload_files([first, second])

            self.assertEqual(existing.read_bytes(), b"keep me")
            self.assertEqual(uploads[0].name, "Show.S01.mteam-upload.chs.zip")

    def test_wrong_simplified_chinese_suffix_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Survivor.S07E01.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"\.chs"):
                prepare_upload_files([path])

    def test_upload_parser_defaults_to_subtitle_page_and_requires_torrent_id(self):
        args = _subtitle_parser().parse_args(
            ["--torrent-id", "123456", "Survivor.S07.chs.zip"]
        )
        self.assertEqual(args.torrent_id, 123456)
        self.assertEqual(args.url, "https://kp.m-team.cc/subtitle")
        self.assertFalse(args.submit)


if __name__ == "__main__":
    unittest.main()
