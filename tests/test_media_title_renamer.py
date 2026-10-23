from argparse import Namespace
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import media_title_renamer.cli as cli
from media_title_renamer.cli import (
    VIDEO_EXTENSIONS,
    _dvd_disc_label,
    _canonicalize_hfr_title,
    _has_bluray_disc_structure,
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


def media_json(*, video_format="AVC", writing_library="", audio_format="DTS", audio_profile="MA / Core", audio_bitrate="3 833 000"):
    video = {
        "@type": "Video",
        "Format": video_format,
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
    def test_douban_backfill_is_available_as_media_title_rename_subcommand(self):
        with patch("sys.argv", ["media-title-rename", "douban-backfill", "--preview"]), patch(
            "media_title_renamer.douban_backfill.main"
        ) as backfill_main:
            cli.main()
        backfill_main.assert_called_once_with(["--preview"])

    def test_auto_publish_source_policy_distinguishes_disc_remux_and_bdrip(self):
        cases = (
            ('Movie.2024.BluRay.x265.mkv', '', 'BluRay BDRip'),
            ('Movie.2024.UHD.BluRay.2160p.x265.mkv', '', 'UHD BluRay BDRip'),
            ('Movie.2024.HDTV.mkv', '', 'WEB-DL'),
            ('Movie.2024.WEBRip.mkv', '', 'WEB-DL'),
            ('Movie.2024.mkv', 'Show.S01.WEBRip', 'WEB-DL'),
            ('Movie.2024.mkv', 'Movie.2024.BluRay.REMUX', 'BluRay REMUX'),
            ('Movie.2024.mkv', 'Movie.2024.UHD.BluRay.REMUX', 'UHD BluRay REMUX'),
        )
        for name, context, expected in cases:
            with self.subTest(name=name, context=context):
                self.assertEqual(_autonomous_source(Path('/media') / name, context), expected)
        self.assertEqual(
            _infer_source('Movie.2024.UHD.BluRay.2160p.HEVC', '.mkv'),
            'UHD BluRay BDRip',
        )
        self.assertEqual(_autonomous_source(Path('/media/Movie.2024.BluRay.iso'), ''), 'BluRay')
        with tempfile.TemporaryDirectory() as directory:
            bdmv = Path(directory) / 'Movie.UHD.BluRay' / 'BDMV'
            stream = bdmv / 'STREAM'
            stream.mkdir(parents=True)
            (bdmv / 'index.bdmv').write_bytes(b'')
            self.assertEqual(_autonomous_source(stream / '00001.m2ts', 'Movie.UHD.BluRay'), 'UHD BluRay')
            self.assertTrue(_has_bluray_disc_structure(stream / '00001.m2ts'))
            self.assertEqual(
                _infer_source('Movie.UHD.BluRay.2160p', '.m2ts', disc_structure=True),
                'UHD BluRay',
            )

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

    def test_torrent_root_mixed_group_overrides_representative_episode_group(self):
        args = Namespace(
            title=None, year=None, source="auto", group=None, edition=None,
            episode=None, platform=None, kind="tv", gpt=False,
            default_group_nogrp=False,
            _source_context="Chicago.Fire.S14.1080p.AMZN.WEB-DL.DDP5.1.H.264-MIXED",
        )
        path = Path("/downloads/TL/Chicago.Fire.S14.MIXED/Chicago.Fire.S14E01.1080p.WEB-DL.H.264-Kitsune.mkv")
        media = inspect_media(media_json(writing_library="x264"), source="WEB-DL")

        _, _, _, group, *_ = _resolve_fields(args, path, media)

        self.assertEqual(group, "MIXED")

    def test_source_can_be_read_from_release_root_or_webrip_marker(self):
        self.assertEqual(_infer_source('American Dad S22 DSNP Webrip x265', '.mkv'), 'WEBRip')
        self.assertIsNone(_infer_source('Bookish S01 HEVC x265', '.mkv'))
        self.assertEqual(_parenthesized_release_group('American Dad (1080p WEBRip x265 - Goki)[TAoE]'), 'Goki')
        self.assertEqual(_parenthesized_release_group('Robin Hood S01 (1080p Bluray x265 HEVC 10bit AAC 5.1 Tigole)'), 'Tigole')
        self.assertIsNone(_parenthesized_release_group('American Dad (1080p WEBRip x265 AAC 2.0)'))
    def test_repack_is_valid_for_encoded_bluray_but_regional_cut_is_not(self):
        media=inspect_media(media_json(writing_library="x265"),source="BluRay BDRip")
        title=build_title(title="Apocalypto",year="2006",source="BluRay BDRip",media=media,edition="REPACK")
        self.assertIn("REPACK",title)
        with self.assertRaises(ValueError):
            build_title(title="Apocalypto",year="2006",source="BluRay BDRip",media=media,edition="US Cut")

    def test_bluray_bdrip_title_keeps_bluray_and_uses_encode_codec(self):
        media = inspect_media(media_json(video_format="HEVC", writing_library="x265"), source="BluRay BDRip")
        self.assertEqual(media.video_codec, "x265")
        title = build_title(title="Example Movie", year="2020", source="BluRay BDRip",
                            media=media, group="GROUP")
        self.assertIn("2020 BluRay 1080p x265", title)

    def test_uhd_bluray_bdrip_title_keeps_uhd_bluray_and_encode_codec(self):
        raw = media_json(video_format="HEVC", writing_library="x265")
        raw['media']['track'][1]['Width'] = '3 840'
        raw['media']['track'][1]['Height'] = '2 160'
        media = inspect_media(raw, source="UHD BluRay BDRip")
        title = build_title(title="Example Movie", year="2020", source="UHD BluRay BDRip",
                            media=media, group="GROUP")
        self.assertIn("2020 UHD BluRay 2160p x265", title)

    def test_uhd_bluray_bdrip_downscaled_to_1080p_omits_uhd_from_title(self):
        media = inspect_media(
            media_json(video_format="HEVC", writing_library="x265"),
            source="UHD BluRay BDRip",
        )
        title = build_title(
            title="Carnal Knowledge", year="1971", source="UHD BluRay BDRip",
            media=media, group="SM737",
        )
        self.assertEqual(
            title,
            "Carnal Knowledge 1971 BluRay 1080p x265 DTS-HD MA5.1-SM737",
        )

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

    def test_duplicate_trailing_tmdb_year_is_emitted_once(self):
        media = inspect_media(media_json(), source="WEB-DL")
        title = build_title(
            title="The UnBelievable with Dan Aykroyd 2023",
            year="2023", episode="S02", source="WEB-DL", media=media,
            group="iVy",
        )
        self.assertTrue(title.startswith("The UnBelievable with Dan Aykroyd 2023 S02 "))
        self.assertEqual(title.count("2023"), 1)

    def test_numeric_work_title_is_not_removed_when_it_equals_year(self):
        media = inspect_media(media_json(), source="WEB-DL")
        title = build_title(title="1917", year="1917", source="WEB-DL", media=media)
        self.assertTrue(title.startswith("1917 1917 WEB-DL "))

    def test_disc_release_group_with_escaped_at_sign_is_kept_consistently(self):
        stem, group = _strip_group(
            "JoJo.Season2.Disc1.2014.JPN.1080p.Blu-ray.AVC.DTS-HD.MA.2.1-blucook#300\\@CHDBits"
        )
        self.assertTrue(stem.endswith("DTS-HD.MA.2.1"))
        self.assertEqual(group, "blucook#300@CHDBits")

    def test_dts_hd_ma_hyphen_does_not_get_misread_as_release_group(self):
        stem, group = _strip_group(
            "The Walk 2015 2160p UHD BluRay DV REMUX HDR10 HEVC DTS-HD-MA 5 1-UnKn0wn"
        )
        self.assertTrue(stem.endswith("DTS-HD-MA 5 1"))
        self.assertEqual(group, "UnKn0wn")


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

    def test_primary_audio_uses_maximum_bitrate_for_truehd_atmos_core(self):
        data = media_json(audio_format="PCM", audio_profile="", audio_bitrate="2304000")
        video = data["media"]["track"][1]
        video.update({
            "Format": "HEVC", "Width": "3 840", "Height": "2 160",
            "HDR_Format": "SMPTE ST 2086", "HDR_Format_Compatibility": "HDR10",
            "transfer_characteristics": "PQ",
        })
        data["media"]["track"].append({
            "@type": "Audio",
            "Format": "MLP FBA",
            "Format_Commercial_IfAny": "Dolby TrueHD with Dolby Atmos",
            "Format_AdditionalFeatures": "AC-3 16-ch",
            "BitRate": "384000",
            "BitRate_Maximum": "6726000",
            "Channels": "8",
        })

        media = inspect_media(data, source="UHD BluRay")

        self.assertEqual(media.audio_codec, "TrueHD Atmos")
        self.assertEqual(media.audio_channels, "7.1")
        self.assertEqual(media.hdr, ("HDR10",))

    def test_high_frame_rate_uses_mteam_hfr_spelling(self):
        data = media_json()
        data["media"]["track"][1]["FrameRate"] = "50.000"
        media = inspect_media(data, source="WEB-DL")
        title = build_title(
            title="Blue Lights",
            year="2023",
            episode="S04",
            source="WEB-DL",
            media=media,
            group="NoGrp",
        )
        self.assertEqual(media.hfr, "HFR")
        self.assertEqual(title, "Blue Lights 2023 S04 1080p WEB-DL HFR H.264 DTS-HD MA5.1-NoGrp")
        self.assertNotRegex(title, r"\b\d+\s*FPS?\b")
        legacy_media = replace(media, hfr="50Fps")
        legacy_title = build_title(
            title="Blue Lights", year="2023", episode="S04", source="WEB-DL",
            media=legacy_media, group="NoGrp",
        )
        self.assertIn(" HFR ", legacy_title)
        self.assertNotIn("50Fps", legacy_title)

    def test_cached_numeric_fps_title_is_canonicalized_for_mteam(self):
        self.assertEqual(
            _canonicalize_hfr_title("Blue Lights 2023 S04 1080p WEB-DL 50Fps H.264 AAC 2.0-NoGrp"),
            "Blue Lights 2023 S04 1080p WEB-DL HFR H.264 AAC 2.0-NoGrp",
        )
        self.assertEqual(
            _canonicalize_hfr_title("Show S01 1080p WEB-DL 50 fps HFR H.264"),
            "Show S01 1080p WEB-DL HFR H.264",
        )
        self.assertNotIn("24FPS", _canonicalize_hfr_title("Film 2024 1080p WEB-DL 24FPS x264 AAC2.0"))

    def test_filename_high_frame_rate_uses_hfr_for_compact_and_spaced_fps(self):
        for name in (
            "Example.Movie.2024.1080p.WEB-DL.x264.50FPS.iso",
            "Example.Movie.2024.1080p.WEB-DL.x264.60 fps.iso",
        ):
            with self.subTest(name=name):
                media = inspect_media_from_filename(Path(name), source="WEB-DL")
                self.assertEqual(media.hfr, "HFR")
                title = build_title(
                    title="Example Movie",
                    year="2024",
                    source="WEB-DL",
                    media=media,
                    group="NOGRP",
                )
                self.assertIn(" HFR ", title)
                self.assertNotRegex(title, r"\b\d+\s*FPS?\b")

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
