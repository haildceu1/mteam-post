import tempfile
import unittest
from pathlib import Path

from media_title_renamer.cli import (
    VIDEO_EXTENSIONS,
    _dvd_disc_label,
    _video_paths,
    build_title,
    filename_hints,
    inspect_media,
    inspect_media_from_filename,
)


def media_json(*, writing_library="", audio_format="DTS", audio_profile="MA / Core", audio_bitrate="3 833 000"):
    video = {
        "@type": "Video",
        "Format": "AVC",
        "Width": "1 920",
        "Height": "1 080",
        "FrameRate": "23.976",
        "HDR_Format": "",
    }
    if writing_library:
        video["WritingLibrary"] = writing_library
    return {
        "media": {
            "track": [
                {"@type": "General"},
                video,
                {
                    "@type": "Audio",
                    "Format": audio_format,
                    "Format_Profile": audio_profile,
                    "Channel(s)": "6",
                    "BitRate": audio_bitrate,
                },
            ]
        }
    }


class MediaTitleRenamerTests(unittest.TestCase):
    def test_directory_input_recurses_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "Season 01"
            nested.mkdir()
            episode = nested / "Show.S01E01.mkv"
            episode.write_bytes(b"")
            (nested / "notes.txt").write_text("ignore", encoding="utf-8")
            self.assertEqual(_video_paths(root, recursive=False), [episode])

    def test_remux_uses_avc_and_formats_dts_hd_ma(self):
        media = inspect_media(media_json(), source="BluRay REMUX")
        self.assertEqual(media.resolution, "1080p")
        self.assertEqual(media.video_codec, "AVC")
        self.assertEqual(media.audio_codec, "DTS-HD MA")
        self.assertEqual(media.audio_channels, "5.1")
        self.assertEqual(
            build_title(
                title="Lisa Frankenstein",
                year="2024",
                source="BluRay REMUX",
                media=media,
                group="ESiR",
            ),
            "Lisa Frankenstein 2024 BluRay REMUX 1080p AVC DTS-HD MA5.1-ESiR",
        )

    def test_encode_uses_x265_and_hdr_dovi(self):
        data = media_json(writing_library="x265 3.5", audio_format="E-AC-3", audio_profile="", audio_bitrate="768000")
        video = data["media"]["track"][1]
        video.update({"Format": "HEVC", "Width": "3 840", "Height": "2 160", "HDR_Format": "Dolby Vision, HDR10 compatible"})
        media = inspect_media(data, source="UHD BluRay BDRip")
        self.assertEqual(media.video_codec, "x265")
        self.assertEqual(media.hdr, ("HDR10", "DoVi"))
        self.assertEqual(media.audio_codec, "DDP")
        self.assertEqual(
            build_title(title="Example Film", year="2025", source="UHD BluRay BDRip", media=media, group="TEST"),
            "Example Film 2025 UHD BluRay BDRip 2160p HDR10 DoVi x265 DDP5.1-TEST",
        )

    def test_filename_hints_detects_tv_webdl_group_and_platform(self):
        media = inspect_media(media_json(writing_library="x264 core"), source="WEB-DL")
        hints = filename_hints(Path("Best.Choice.Ever.2024.S1E11-E12.1080p.NF.WEB-DL.x264.AAC-GRP.mkv"), media)
        self.assertEqual(hints.title, "Best Choice Ever")
        self.assertEqual(hints.year, "2024")
        self.assertEqual(hints.episode, "S01E11-E12")
        self.assertEqual(hints.source, "WEB-DL")
        self.assertEqual(hints.platform, "Netflix")
        self.assertEqual(hints.group, "GRP")

    def test_consecutive_multi_episode_notation_is_normalized(self):
        examples = {
            "The.Office.US.S03E12E13.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP.mkv": "S03E12-E13",
            "The.Office.US.S07E25E26.Search.Committee.EXTENDED.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP.mkv": "S07E25-E26",
        }
        for filename, expected in examples.items():
            with self.subTest(filename=filename):
                hints = filename_hints(Path(filename))
                self.assertEqual(hints.episode, expected)
                self.assertEqual(hints.group, "NOGRP")

    def test_dotted_episode_interlaced_scan_and_by_group(self):
        data = media_json(audio_format="AC-3", audio_profile="", audio_bitrate="384000")
        data["media"]["track"][1].update({"ScanType": "Interlaced", "ScanOrder": "TFF", "FrameRate": "25.000"})
        media = inspect_media(data, source="HDTV")
        hints = filename_hints(Path("20.22.s01.E01.(2024).HDTV (1080i).by.Romanok8691.ts"), media)
        self.assertEqual(media.resolution, "1080i")
        self.assertEqual(hints.title, "20 22")
        self.assertEqual(hints.episode, "S01E01")
        self.assertEqual(hints.group, "Romanok8691")
        self.assertEqual(
            build_title(
                title=hints.title,
                year=hints.year,
                episode=hints.episode,
                source=hints.source,
                media=media,
                group=hints.group,
            ),
            "20 22 2024 S01E01 1080i HDTV H.264 DD5.1-Romanok8691",
        )

    def test_highest_bitrate_audio_is_used_and_count_is_opt_in(self):
        data = media_json(audio_format="AAC", audio_profile="", audio_bitrate="192000")
        data["media"]["track"].append(
            {"@type": "Audio", "Format": "TrueHD", "Channel(s)": "8", "BitRate": "4 000 000", "Format_AdditionalFeatures": "Atmos"}
        )
        media = inspect_media(data, source="BluRay REMUX")
        self.assertEqual(media.audio_codec, "TrueHD Atmos")
        self.assertEqual(media.audio_channels, "7.1")
        self.assertEqual(media.audio_tracks, 2)
        default_title = build_title(title="Audio Test", year="2024", source="BluRay REMUX", media=media)
        self.assertNotIn("2Audio", default_title)
        self.assertIn(
            "2Audio",
            build_title(
                title="Audio Test",
                year="2024",
                source="BluRay REMUX",
                media=media,
                include_audio_count=True,
            ),
        )

    def test_movie_requires_year_but_tv_does_not(self):
        media = inspect_media(media_json(), source="WEB-DL")
        with self.assertRaisesRegex(ValueError, "年份"):
            build_title(title="No Year", year=None, source="WEB-DL", media=media)
        self.assertIn(
            "S01E01",
            build_title(title="TV Show", year=None, episode="S1E1", source="WEB-DL", media=media),
        )

    def test_dvd_iso_is_supported_and_sized_as_d9(self):
        self.assertIn(".iso", VIDEO_EXTENSIONS)
        self.assertEqual(_dvd_disc_label(4_699_000_000), "DVD5")
        self.assertEqual(_dvd_disc_label(7_352_549_376), "DVD9")
        hints = filename_hints(Path("Sandra 1965 DVDiSo 576p MPEG-2 DD.iso"))
        self.assertEqual(hints.title, "Sandra")
        self.assertEqual(hints.year, "1965")
        self.assertEqual(hints.source, "DVD")

    def test_bluray_iso_falls_back_to_filename_and_keeps_edition(self):
        path = Path("Sherlock, Jr 1924 MOC Blu-ray 1080p AVC LPCM 2.0-smwy8888.iso")
        media = inspect_media_from_filename(path, source="BluRay")
        hints = filename_hints(path, media)
        self.assertEqual(hints.title, "Sherlock, Jr")
        self.assertEqual(hints.edition, "MOC")
        self.assertEqual(hints.source, "BluRay")
        self.assertEqual(hints.group, "smwy8888")
        self.assertEqual(media.audio_codec, "LPCM")
        self.assertEqual(media.audio_channels, "2.0")
        self.assertEqual(
            build_title(
                title=hints.title,
                year=hints.year,
                edition=hints.edition,
                source=hints.source,
                media=media,
                group=hints.group,
            ),
            "Sherlock, Jr 1924 MOC BluRay 1080p AVC LPCM2.0-smwy8888",
        )

    def test_group_name_may_contain_at_sign(self):
        path = Path(
            "Everything.Everywhere.All.at.Once.2022.ITA.UHD.BluRay.2160p.HEVC.TrueHD.7.1-DiY@HDHome.iso"
        )
        media = inspect_media_from_filename(path)
        hints = filename_hints(path, media)
        self.assertEqual(hints.group, "DiY@HDHome")

    def test_4k_bluray_iso_maps_to_2160p_disc_and_uses_hevc(self):
        path = Path(
            "我要复仇 (2002) - 4K - BluRay - x265 - DTS-HD.MA.5.1 - fda80@CHDBits.iso"
        )
        media = inspect_media_from_filename(path)
        hints = filename_hints(path, media)
        self.assertEqual(media.resolution, "2160p")
        self.assertEqual(media.video_format, "HEVC")
        self.assertEqual(media.audio_codec, "DTS-HD MA")
        self.assertEqual(media.audio_channels, "5.1")
        self.assertEqual(hints.source, "UHD BluRay")
        self.assertIsNone(hints.edition)
        self.assertEqual(hints.group, "fda80@CHDBits")


if __name__ == "__main__":
    unittest.main()
