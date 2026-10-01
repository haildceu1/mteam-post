import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_title_renamer.batch_metadata import (
    MAX_RESOURCE_BYTES,
    _batch_prepare_hints,
    _classify_resource,
    _douban_issue,
    _missing_fields,
    _resource_name_hints,
    scan_resources,
)
from media_title_renamer.episode_mapping import episode_number_hint, sequential_episode_map


class BatchMetadataTests(unittest.TestCase):
    def test_scans_movie_and_tv_without_following_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "TL"
            root.mkdir()
            movie = root / "A.Movie.2020.1080p.WEB-DL-GROUP"
            movie.mkdir()
            (movie / "A.Movie.2020.1080p.WEB-DL-GROUP.mkv").write_bytes(b"video")
            show = root / "A.Show.S01"
            show.mkdir()
            (show / "A.Show.S01E01.mkv").write_bytes(b"ep1")
            (show / "A.Show.S01E02.mkv").write_bytes(b"ep2")
            outside = Path(directory) / "outside.mkv"
            outside.write_bytes(b"outside")
            (movie / "outside-link.mkv").symlink_to(outside)

            resources = {item.name: item for item in scan_resources(root)}

        self.assertEqual(resources["A.Show.S01"].classification, "tv")
        self.assertEqual(resources["A.Show.S01"].input_path, str(show))
        self.assertEqual(resources["A.Movie.2020.1080p.WEB-DL-GROUP"].classification, "needs_review")
        self.assertEqual(resources["A.Movie.2020.1080p.WEB-DL-GROUP"].symlinks, ["outside-link.mkv"])
        self.assertNotIn("outside.mkv", [row["relative_path"] for row in resources["A.Movie.2020.1080p.WEB-DL-GROUP"].files])

    def test_archives_and_audio_are_not_sent_to_video_prepare(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "TL"
            root.mkdir()
            (root / "Some.Game-GROUP").mkdir()
            (root / "Some.Game-GROUP" / "data.7z").write_bytes(b"archive")
            (root / "Artist - Album").mkdir()
            (root / "Artist - Album" / "track.flac").write_bytes(b"audio")

            resources = {item.name: item for item in scan_resources(root)}

        self.assertEqual(resources["Some.Game-GROUP"].classification, "unsupported_type")
        self.assertEqual(resources["Artist - Album"].classification, "unsupported_type")
        self.assertEqual(resources["Artist - Album"].media_kind, "")

    def test_sports_broadcasts_are_not_misclassified_as_movies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "TL"
            event = root / "2026 AMA SuperMotocross Rd 2 Carson 1080p60 x264 slicknick610"
            event.mkdir(parents=True)
            (event / "event.mkv").write_bytes(b"video")

            resource = scan_resources(root)[0]

        self.assertEqual(resource.classification, "unsupported_type")
        self.assertIn("体育赛事录像", " ".join(resource.notes))

    def test_oversize_threshold_is_enforced_without_allocating_large_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resource = (root / "Big.Movie.2020.2160p.BluRay").name
            path = root / resource
            path.mkdir()
            video = path / "Big.Movie.2020.2160p.BluRay.mkv"
            video.write_bytes(b"x")
            scanned = scan_resources(root)[0]
            with patch("media_title_renamer.batch_metadata.MAX_RESOURCE_BYTES", 0):
                _classify_resource(scanned, root)

        self.assertGreater(MAX_RESOURCE_BYTES, 0)
        self.assertEqual(scanned.classification, "oversize")

    def test_nonstandard_numbered_tv_files_are_classified_as_tv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "TL"
            root.mkdir()
            show = root / "Brides.of.Christ.1991.480P.WEB.Xvid"
            show.mkdir()
            for number in range(1, 7):
                (show / f"brides{number}.avi").write_bytes(b"episode")

            resource = scan_resources(root)[0]

        self.assertEqual(resource.classification, "tv")
        self.assertEqual(resource.media_kind, "tv")
        self.assertEqual(resource.input_path, str(show))

    def test_torrent_folder_label_retains_source_and_release_group(self):
        with tempfile.TemporaryDirectory() as directory:
            name = "Death.on.the.Nile.1978.REMASTERED.1080p.BluRay.x264-GUACAMOLE"
            root = Path(directory) / name
            root.mkdir()
            resource = scan_resources(root.parent)[0]
            hints = _resource_name_hints(resource)

        self.assertEqual(hints.source, "BluRay")
        self.assertEqual(hints.group, "GUACAMOLE")
        self.assertEqual(hints.year, "1978")

    def test_resource_year_from_torrent_root_is_used_to_reject_tmdb_mismatch(self):
        package = {
            "title": "7 Days 2009 S01 480p DVD x265 DDP2.0-iVy",
            "release_name": "7 Days 2009 S01 480p DVD x265 DDP2.0-iVy",
            "subtitle": "7 Days",
            "category": "影剧/综艺/DVDiSo",
            "douban_url": "https://movie.douban.com/subject/1/",
            "group": "iVy",
            "technical_info_text": "MediaInfo",
            "tmdb": {"year": "2009"},
            "media": {
                "resolution": "480p",
                "video_codec": "x265",
                "audio_codec": "DDP",
            },
            "identification_evidence": {
                "identity": {"confidence": 0.99},
            },
            "douban_match": {"id": "1", "year": "1998", "title": "7 Days", "source": "html_search"},
        }

        missing = _missing_fields(package, expected_year="1998")

        self.assertIn("TMDB 年份冲突（资源名 1998，TMDB 2009）", missing)
        self.assertIn("发布标题年份冲突（资源名 1998，标题 2009）", missing)

    def test_torrent_folder_encode_overrides_disc_codec_and_bad_inner_edition(self):
        with tempfile.TemporaryDirectory() as directory:
            name = "Death.on.the.Nile.1978.REMASTERED.1080p.BluRay.x264-GUACAMOLE"
            root = Path(directory) / name
            root.mkdir()
            (root / "gua-deathonnile.1978.rem-1080p.mkv").write_bytes(b"video")
            resource = scan_resources(root.parent)[0]

        self.assertEqual(
            _batch_prepare_hints(resource),
            ("BluRay BDRip", "", ""),
        )

    def test_torrent_folder_remux_keeps_disc_source_and_root_edition(self):
        with tempfile.TemporaryDirectory() as directory:
            name = "Example.Movie.2020.Directors.Cut.1080p.BluRay.REMUX-GROUP"
            root = Path(directory) / name
            root.mkdir()
            (root / "feature.mkv").write_bytes(b"video")
            resource = scan_resources(root.parent)[0]

        source, edition, platform = _batch_prepare_hints(resource)
        self.assertEqual(source, "BluRay REMUX")
        self.assertEqual(edition, "")
        self.assertEqual(platform, "")

    def test_multifile_movie_is_not_called_tv_without_episode_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "TL"
            item = root / "Feature.Movie.2024.1080p.BluRay-GROUP"
            item.mkdir(parents=True)
            (item / "Feature.Movie.2024.1080p.BluRay-GROUP.mkv").write_bytes(b"feature" * 1000)
            (item / "Feature.Movie.2024.1080p.BluRay-GROUP.extra.mkv").write_bytes(b"extra" * 800)

            resource = scan_resources(root)[0]

        self.assertNotEqual(resource.classification, "tv")
        self.assertEqual(resource.classification, "needs_review")

    def test_split_archive_release_with_video_samples_is_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "TL"
            item = root / "Archive.Release.S01"
            sample = item / "Episode.01" / "Sample"
            sample.mkdir(parents=True)
            (sample / "sample.mkv").write_bytes(b"short sample")
            (item / "episode.r00").write_bytes(b"x" * (2 * 1024 * 1024))
            (item / "episode.rar").write_bytes(b"x" * (2 * 1024 * 1024))

            resource = scan_resources(root)[0]

        self.assertEqual(resource.classification, "unsupported_type")
        self.assertIn("分卷 RAR/ZIP/7z", " ".join(resource.notes))

    def test_episode_sequence_mapping_rejects_duplicates_and_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contiguous = [root / f"brides{number}.avi" for number in range(1, 7)]
            duplicate = [root / "Show - 01 [BD].mkv", root / "Show - 01 alt [BD].mkv"]
            gap = [root / "Show - 01 [BD].mkv", root / "Show - 03 [BD].mkv"]

        self.assertEqual([episode_number_hint(path) for path in contiguous], [1, 2, 3, 4, 5, 6])
        mapping = sequential_episode_map(contiguous)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping[0][contiguous[0]], "S01E01")
        self.assertIsNone(sequential_episode_map(duplicate, season=1))
        self.assertIsNone(sequential_episode_map(gap, season=1))

    def test_douban_diagnostics_keep_empty_suggest_distinct_from_zero_page_results(self):
        self.assertEqual(
            _douban_issue({"douban_diagnostics": ["HTTP 200 返回空结果（疑似豆瓣频控/风控，非 HTTP 403/429）"]}),
            "suggest_empty_uncertain",
        )
        self.assertEqual(
            _douban_issue({"douban_diagnostics": ["豆瓣网页搜索正常返回 0 个候选；这不等同于已证明豆瓣不存在该条目"]}),
            "search_completed_zero_candidates",
        )
        self.assertEqual(_douban_issue({"douban_diagnostics": ["HTML 搜索 HTTP 403"]}), "blocked_or_cookie")


if __name__ == "__main__":
    unittest.main()
