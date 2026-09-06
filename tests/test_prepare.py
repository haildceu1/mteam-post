import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from media_title_renamer.cli import MediaInfo

from media_title_renamer.prepare import (
    _bdinfo_list_command,
    _bdinfo_scan_command,
    DoubanMatch,
    TmdbMatch,
    _extract_screenshots,
    automatic_piece_length,
    build_subtitle,
    create_private_v1_folder_torrent,
    create_private_v1_torrent,
    infer_mteam_category,
    main as prepare_main,
    prepare_technical_info,
    read_bdinfo_report,
    select_longest_bdinfo_playlist,
)


class PrepareTests(unittest.TestCase):
    @patch("random_video_screenshots.cli.extract_screenshots")
    def test_screenshot_result_excludes_stale_files(self, extract_screenshots):
        def create_new_file(_video, output, *, count):
            self.assertEqual(count, 1)
            output.mkdir(parents=True, exist_ok=True)
            (output / "new.jpg").write_bytes(b"new")

        extract_screenshots.side_effect = create_new_file
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "screenshots"
            output.mkdir()
            (output / "old.jpg").write_bytes(b"old")
            generated = _extract_screenshots(root / "video.mkv", output, 1)

        self.assertEqual([item.name for item in generated], ["new.jpg"])

    @patch("media_title_renamer.prepare.read_mediainfo_text", return_value="General\nComplete name : E01.mkv\n")
    @patch("media_title_renamer.prepare.read_mediainfo")
    def test_tv_folder_probes_only_the_first_episode(self, read_mediainfo, read_mediainfo_text):
        read_mediainfo.return_value = MediaInfo(
            width=1920,
            height=1080,
            resolution="1080p",
            video_format="AVC",
            writing_library="",
            video_codec="AVC",
            hdr=(),
            hfr=None,
            audio_codec="DD",
            audio_channels="5.1",
            audio_tracks=1,
            audio_bitrate=640000,
            audio_language="en",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Example Show"
            root.mkdir()
            second = root / "Example.Show.S01E02.2024.WEB-DL.1080p.AVC.DD5.1-GRP.mkv"
            first = root / "Example.Show.S01E01.2024.WEB-DL.1080p.AVC.DD5.1-GRP.mkv"
            second.write_bytes(b"episode two")
            first.write_bytes(b"episode one")
            with redirect_stdout(io.StringIO()):
                package_path = prepare_main(
                    [
                        str(root),
                        "--title",
                        "Example Show",
                        "--year",
                        "2024",
                        "--source",
                        "WEB-DL",
                        "--offline",
                        "--douban-url",
                        "https://movie.douban.com/subject/1/",
                        "--skip-screenshots",
                        "--skip-torrent",
                        "--apply",
                    ]
                )
            package = json.loads(package_path.read_text(encoding="utf-8"))
            prepared_files = sorted(
                str(path.relative_to(root))
                for path in root.rglob("*.mkv")
            )

        read_mediainfo.assert_called_once()
        self.assertEqual(read_mediainfo.call_args.args[0].name, first.name)
        self.assertEqual(read_mediainfo_text.call_count, 1)
        self.assertTrue(package["media_probe_path"].endswith("S01E01.2024.WEB-DL.1080p.AVC.DD5.1-GRP.mkv"))
        self.assertEqual(len(package["files"]), 2)
        self.assertEqual(
            prepared_files,
            [
                str(Path("Season 01") / "Example Show 2024 S01E01 1080p WEB-DL H.264 DD5.1-GRP.mkv"),
                str(Path("Season 01") / "Example Show 2024 S01E02 1080p WEB-DL H.264 DD5.1-GRP.mkv"),
            ],
        )
        self.assertTrue(all(record["relative_path"].startswith("Season 01") for record in package["files"]))
        self.assertTrue(package["files"][0]["mediainfo_text"])
        self.assertFalse(package["files"][1]["mediainfo_text"])

    def test_v1_torrent_is_private_and_has_no_tracker_or_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original.ts"
            source.write_bytes(b"torrent test payload")
            output = root / "prepared.torrent"
            piece_length = create_private_v1_torrent(source, output, "Renamed.ts")
            payload = output.read_bytes()
            self.assertIn(b"4:infod", payload)
            self.assertIn(b"7:privatei1e", payload)
            self.assertIn(b"4:name10:Renamed.ts", payload)
            self.assertIn(hashlib.sha1(source.read_bytes()).digest(), payload)
            self.assertNotIn(b"announce", payload)
            self.assertNotIn(b"6:source", payload)
            self.assertEqual(piece_length, 64 * 1024)

    def test_automatic_piece_size_targets_at_most_2000_pieces(self):
        size = 7 * 1024**3
        piece_length = automatic_piece_length(size)
        self.assertLessEqual((size + piece_length - 1) // piece_length, 2000)

    def test_v1_folder_torrent_is_private_and_hashes_files_contiguously(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Show"
            season = root / "Season 01"
            season.mkdir(parents=True)
            first = season / "old-e01.ts"
            second = season / "old-e02.ts"
            first.write_bytes(b"first episode")
            second.write_bytes(b"second episode")
            output = Path(directory) / "prepared.torrent"
            piece_length = create_private_v1_folder_torrent(
                root,
                [
                    (first, Path("Season 01") / "Show S01E01.ts"),
                    (second, Path("Season 01") / "Show S01E02.ts"),
                ],
                output,
                "Show",
            )
            payload = output.read_bytes()
            self.assertIn(b"5:filesl", payload)
            self.assertIn(b"7:privatei1e", payload)
            self.assertIn(b"4:name4:Show", payload)
            self.assertIn(b"Show S01E01.ts", payload)
            self.assertIn(b"Show S01E02.ts", payload)
            self.assertIn(hashlib.sha1(first.read_bytes() + second.read_bytes()).digest(), payload)
            self.assertNotIn(b"announce", payload)
            self.assertNotIn(b"6:source", payload)
            self.assertEqual(piece_length, 64 * 1024)

    def test_mteam_category_mapping(self):
        self.assertEqual(
            infer_mteam_category(kind="movie", source="DVD9", resolution="576i", animation=False),
            "电影/DVDiSo",
        )
        self.assertEqual(
            infer_mteam_category(kind="movie", source="BluRay REMUX", resolution="1080p", animation=False),
            "电影/Remux",
        )
        self.assertEqual(
            infer_mteam_category(kind="tv", source="BluRay", resolution="1080p", animation=False),
            "影剧/综艺/BluRay",
        )
        self.assertEqual(
            infer_mteam_category(kind="tv", source="WEB-DL", resolution="1080p", animation=True),
            "动画",
        )

    def test_subtitle_contains_chinese_original_name_and_source_language(self):
        douban = DoubanMatch(
            id="1",
            url="https://movie.douban.com/subject/1/",
            title="示例剧",
            original_title="Example Show",
            year="2024",
            score=100,
        )
        tmdb = TmdbMatch(
            id=1,
            media_type="tv",
            name="Example Show",
            chinese_name="示例剧",
            original_name="Оригинал",
            original_language="ru",
            year="2024",
            imdb_id="tt1",
            genre_ids=(),
            score=100,
        )
        self.assertEqual(
            build_subtitle(douban=douban, tmdb=tmdb, fallback_title="Example Show", language_code="ru"),
            "示例剧 / Оригинал [俄语]",
        )

    def test_longest_bdinfo_playlist_is_selected(self):
        listing = """
#   Group  Playlist File  Length    Estimated Bytes Measured Bytes
2   1      00001.MPLS     00:03:20
1   1      00005.MPLS     02:20:07
3   2      00009.MPLS     00:01:00
"""
        self.assertEqual(select_longest_bdinfo_playlist(listing), "00005")

    def test_bdinfo_rs_iso_commands_include_report_destination(self):
        disc = Path(r"D:\Movie\Disc.iso")
        output = Path(r"D:\Movie\Disc.prepare")
        self.assertEqual(
            _bdinfo_list_command("bdinfo-rs", "rs", disc, output),
            ["bdinfo-rs", "--list", str(disc), str(output)],
        )
        self.assertEqual(
            _bdinfo_scan_command("bdinfo-rs", "rs", disc, output, "00005"),
            ["bdinfo-rs", "--mpls", "00005", str(disc), str(output)],
        )

    def test_bluray_iso_uses_existing_bdinfo_report(self):
        report = """DISC INFO:
Disc Title: Example

PLAYLIST REPORT:
Name: 00005.MPLS

VIDEO:
MPEG-H HEVC Video

AUDIO:
Dolby TrueHD/Atmos Audio
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "source.txt"
            report_path.write_text(report, encoding="utf-8")
            kind, text, saved_path, playlist = prepare_technical_info(
                root / "Example.iso",
                "Example.iso",
                "UHD BluRay",
                root / "prepare",
                bdinfo_report=report_path,
            )
            self.assertEqual(kind, "BDInfo")
            self.assertEqual(playlist, "00005")
            self.assertEqual(read_bdinfo_report(saved_path), text)
            self.assertEqual(saved_path.name, "bdinfo.txt")


if __name__ == "__main__":
    unittest.main()
