import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from media_title_renamer.cli import (
    VIDEO_EXTENSIONS,
    _dvd_disc_label,
    _infer_source,
    _parenthesized_release_group,
    _resolve_fields,
    _autonomous_source,
    _resolution,
    _strip_release_prefix,
    _strip_group,
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
    def test_auto_publish_source_policy_prefers_remux_or_real_disc(self):
        cases = (
            ('Movie.2024.BluRay.x265.mkv', '', 'WEB-DL'),
            ('Movie.2024.HDTV.mkv', '', 'WEB-DL'),
            ('Movie.2024.WEBRip.mkv', '', 'WEB-DL'),
            ('Movie.2024.mkv', 'Show.S01.WEBRip', 'WEB-DL'),
            ('Movie.2024.mkv', 'Movie.2024.BluRay.REMUX', 'BluRay REMUX'),
            ('Movie.2024.mkv', 'Movie.2024.UHD.BluRay.REMUX', 'UHD BluRay REMUX'),
        )
        for name, context, expected in cases:
            with self.subTest(name=name, context=context):
                self.assertEqual(_autonomous_source(Path('/media') / name, context), expected)
        self.assertEqual(_autonomous_source(Path('/media/Movie.2024.BluRay.iso'), ''), 'BluRay')
        with tempfile.TemporaryDirectory() as directory:
            bdmv = Path(directory) / 'Movie.UHD.BluRay' / 'BDMV'
            stream = bdmv / 'STREAM'
            stream.mkdir(parents=True)
            (bdmv / 'index.bdmv').write_bytes(b'')
            self.assertEqual(_autonomous_source(stream / '00001.m2ts', 'Movie.UHD.BluRay'), 'UHD BluRay')

    def test_abbreviated_movie_filename_uses_release_folder_title_year_and_source(self):
        args = Namespace(
            title=None, year=None, source="auto", group=None, edition=None,
            episode=None, platform=None, kind="movie", gpt=False,
            default_group_nogrp=True,
            _source_context="Universal.Soldier.II.Brothers.In.Arms.1998.1080P.BLURAY.H264-UNDERTAKERS",
        )
        path = Path("/downloads/TL/Universal.Soldier.II.Brothers.In.Arms.1998.1080P.BLURAY.H264-UNDERTAKERS/undertakers-universalsoldieriibia1998-1080.mkv")
        media = inspect_media(media_json(writing_library="x264"), source="BluRay BDRip")
        title, year, source, group, *_ = _resolve_fields(args, path, media)
        self.assertEqual(title, "Universal Soldier II Brothers In Arms")
        self.assertEqual(year, "1998")
        self.assertEqual(source, "BluRay BDRip")
        self.assertEqual(group, "UNDERTAKERS")

    def test_source_can_be_read_from_release_root_or_webrip_marker(self):
        self.assertEqual(_infer_source('American Dad S22 DSNP Webrip x265', '.mkv'), 'WEBRip')
        self.assertIsNone(_infer_source('Bookish S01 HEVC x265', '.mkv'))
        self.assertEqual(_parenthesized_release_group('American Dad (1080p WEBRip x265 - Goki)[TAoE]'), 'Goki')
    def test_repack_is_valid_for_encoded_bluray_but_regional_cut_is_not(self):
        media=inspect_media(media_json(writing_library="x265"),source="BluRay BDRip")
        title=build_title(title="Apocalypto",year="2006",source="BluRay BDRip",media=media,edition="REPACK")
        self.assertIn("REPACK",title)
        with self.assertRaises(ValueError):
            build_title(title="Apocalypto",year="2006",source="BluRay BDRip",media=media,edition="US Cut")

    def test_resolution_is_normalized_to_supported_mteam_buckets(self):
        self.assertEqual(_resolution(640, 464), "480p")
        self.assertEqual(_resolution(720, 576), "540p")
        self.assertEqual(_resolution(1920, 1080, "Interlaced", "TFF"), "1080p")
        self.assertEqual(_resolution(3840, 2160), "2160p")

    def test_mp3_title_separates_codec_and_channel_layout(self):
        data = media_json(audio_format="MPEG Audio", audio_profile="Layer 3")
        data["media"]["track"][2]["Channel(s)"] = "2"
        media = inspect_media(data, source="WEB-DL")
        title = build_title(
            title="Brides of Christ",
            year="1991",
            source="WEB-DL",
            media=media,
            group="NOGRP",
        )
        self.assertEqual(media.audio_codec, "MP3")
        self.assertIn("MP3 2.0", title)

    def test_disc_release_group_with_escaped_at_sign_is_kept_consistently(self):
        stem, group = _strip_group(
            "JoJo.Season2.Disc1.2014.JPN.1080p.Blu-ray.AVC.DTS-HD.MA.2.1-blucook#300\\@CHDBits"
        )
        self.assertTrue(stem.endswith("DTS-HD.MA.2.1"))
        self.assertEqual(group, "blucook#300@CHDBits")


    def test_large_untagged_iso_is_inferred_as_bluray(self):
        self.assertEqual(
            _infer_source("第1碟", ".iso", file_size=42_610_000_000),
            "BluRay",
        )

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
            "Example Film 2025 UHD BluRay 2160p HDR10 DoVi x265 DDP5.1-TEST",
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

    def test_release_site_prefix_is_removed_without_touching_title(self):
        path = Path(
            "[BDshare.org].Super.Inframan.1975.USA.BluRay.1080p.AVC.LPCM.1.0-FFansDIY@至尊宝.iso"
        )
        hints = filename_hints(path)
        self.assertEqual(_strip_release_prefix(path.stem), "Super.Inframan.1975.USA.BluRay.1080p.AVC.LPCM.1.0-FFansDIY@至尊宝")
        self.assertEqual(hints.title, "Super Inframan")
        self.assertEqual(hints.year, "1975")
        self.assertEqual(hints.source, "BluRay")
        self.assertIsNone(hints.group)

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

    def test_generic_release_version_is_not_treated_as_bluray_edition(self):
        hints = filename_hints(
            Path("超时空辉夜姬！ (2026).V1.1080p.BluRay.Remux.AVC.TrueHD.5.1.2Audio-AnimeF.mkv")
        )
        self.assertIsNone(hints.edition)
        self.assertEqual(hints.source, "BluRay REMUX")

    def test_dotted_episode_interlaced_scan_and_by_group(self):
        data = media_json(audio_format="AC-3", audio_profile="", audio_bitrate="384000")
        data["media"]["track"][1].update({"ScanType": "Interlaced", "ScanOrder": "TFF", "FrameRate": "25.000"})
        media = inspect_media(data, source="HDTV")
        hints = filename_hints(Path("20.22.s01.E01.(2024).HDTV (1080i).by.Romanok8691.ts"), media)
        self.assertEqual(media.resolution, "1080p")
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
            "20 22 2024 S01E01 1080p HDTV H.264 DD5.1-Romanok8691",
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

    def test_country_region_tag_is_kept_in_filename_and_title(self):
        path = Path("High School Girl's Diary 1981 BluRay 1080p JPN AVC TrueHD 2.0-DIY@BC.iso")
        media = inspect_media_from_filename(path, source="BluRay")
        hints = filename_hints(path, media)
        self.assertEqual(hints.country, "JPN")
        self.assertEqual(
            build_title(
                title=hints.title,
                year=hints.year,
                source=hints.source,
                media=media,
                group=hints.group,
                country=hints.country,
            ),
            "High School Girl's Diary 1981 BluRay 1080p JPN AVC TrueHD2.0-DIY@BC",
        )
        self.assertIsNone(filename_hints(Path("Us.2019.1080p.WEB-DL.AVC.AAC-GRP.mkv")).country)

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
