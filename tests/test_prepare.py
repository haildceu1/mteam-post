import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from media_title_renamer.cli import MediaInfo

from media_title_renamer.prepare import (
    _bdinfo_list_command,
    _bdinfo_scan_command,
    _bracket_release_group,
    _disc_episode,
    _choose_douban,
    _douban_search_page_candidates,
    _douban_for_release,
    _embedded_tmdb_id,
    _media_from_bdinfo,
    _mount_iso,
    _resume_paths,
    _resume_tmdb_id_for_folder,
    _series_folder_name,
    _torrent_file_specs,
    _torrent_resume_identity,
    _unmount_iso,
    DoubanMatch,
    IsoMount,
    TorrentProgress,
    TorrentHashCheckpoint,
    TorrentPieceHasher,
    TmdbMatch,
    TmdbClient,
    _season_number_from_episode,
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
    def test_resume_tmdb_id_matches_the_same_folder_file_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Show"
            root.mkdir()
            first = root / "Show.S01E01.mkv"
            second = root / "Show.S01E02.mkv"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            prepare_dir = root.parent / "Show 2024.prepare"
            prepare_dir.mkdir()
            checkpoint = prepare_dir / "Show.torrent.resume.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "logical_root_name": "Show-2024-[tmdb=1234]",
                        "files": [
                            {"source_path": str(first)},
                            {"source_path": str(second)},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(_resume_tmdb_id_for_folder(root, [first, second]), 1234)
            self.assertIsNone(_resume_tmdb_id_for_folder(root, [first]))

    def test_tmdb_season_returns_localized_and_original_names(self):
        client = TmdbClient(read_token="test-token")
        responses = [
            {"id": 1456, "season_number": 7, "name": "第 7 季", "air_date": "2003-09-18"},
            {"id": 1456, "season_number": 7, "name": "Pearl Islands", "air_date": "2003-09-18"},
        ]
        with patch.object(client, "_get", side_effect=responses) as get:
            season = client.season(14658, 7)
        self.assertEqual(season.name, "Pearl Islands")
        self.assertEqual(season.chinese_name, "第 7 季")
        self.assertEqual(season.year, "2003")
        self.assertEqual(get.call_count, 2)

    def test_douban_html_search_parses_tv_season(self):
        html = (
            '<script>window.__DATA__ = '
            + json.dumps(
                {
                    "items": [
                        {
                            "id": 3271362,
                            "title": "幸存者：珍珠岛 第七季 Survivor: Pearl Islands Season 7 (2003)",
                            "abstract": "美国 / 真人秀 / 幸存者 第七季 / 45分钟",
                            "url": "https://movie.douban.com/subject/3271362/",
                        }
                    ]
                },
                ensure_ascii=False,
            )
            + '; window.__USER__ = {};</script>'
        )
        with patch("media_title_renamer.prepare._get_text", return_value=html):
            candidates = _douban_search_page_candidates("Survivor Pearl Islands Season 7", "2003")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].id, "3271362")
        self.assertEqual(candidates[0].title, "幸存者：珍珠岛 第七季")
        self.assertEqual(candidates[0].original_title, "Survivor: Pearl Islands Season 7")
        self.assertEqual(candidates[0].year, "2003")
        self.assertEqual(candidates[0].season_number, 7)

    def test_douban_selection_rejects_a_different_season(self):
        wrong = DoubanMatch("50", "https://movie.douban.com/subject/50/", "幸存者 第五十季", "Survivor Season 50", "2025", 100, 50)
        right = DoubanMatch("3271362", "https://movie.douban.com/subject/3271362/", "幸存者：珍珠岛 第七季", "Survivor: Pearl Islands Season 7", "2003", 100, 7)
        self.assertIs(_choose_douban([wrong, right], expected_season=7), right)
        self.assertIsNone(_choose_douban([wrong], expected_season=7))
        self.assertEqual(_season_number_from_episode("S07E01-E02"), 7)
        self.assertEqual(_season_number_from_episode("S07D01"), 7)

    def test_douban_release_search_uses_tmdb_season_name_and_year(self):
        args = type("Args", (), {"douban_url": None, "offline": False})()
        tmdb = TmdbMatch(
            id=14658,
            media_type="tv",
            name="Survivor",
            chinese_name="幸存者 真人秀",
            original_name="Survivor",
            original_language="en",
            year="2000",
            imdb_id="tt0239195",
            genre_ids=(),
            score=100,
        )
        season = type("Season", (), {"name": "Pearl Islands", "year": "2003"})()
        candidate = DoubanMatch(
            "3271362",
            "https://movie.douban.com/subject/3271362/",
            "幸存者：珍珠岛 第七季",
            "Survivor: Pearl Islands Season 7",
            "2003",
            100,
            7,
        )
        with (
            patch("media_title_renamer.prepare._tmdb_season_for_release", return_value=season),
            patch("media_title_renamer.prepare._douban_candidates", return_value=[candidate]) as search,
        ):
            result = _douban_for_release(
                args,
                tmdb=tmdb,
                title="Survivor",
                base_title="Survivor",
                year="2000",
                season_number=7,
            )
        self.assertIs(result, candidate)
        names, search_year = search.call_args.args
        self.assertEqual(search_year, "2003")
        self.assertEqual(search.call_args.kwargs["expected_season"], 7)
        self.assertIn("Survivor Pearl Islands", names)

    def test_series_folder_name_follows_mteam_template(self):
        self.assertEqual(
            _series_folder_name("Survivor", "2000", 14658),
            "Survivor-2000-[tmdb=14658]",
        )
        self.assertEqual(_series_folder_name("幸存者：真人秀", "2000", None), "幸存者：真人秀-2000")

    def test_linux_iso_mount_prefers_udisks_for_udf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disc = root / "Disc.iso"
            disc.write_bytes(b"iso")
            mount_root = root / "mounted-disc"
            mount_root.mkdir()

            def find_tool(name: str):
                return {
                    "udisksctl": "/usr/bin/udisksctl",
                    "findmnt": "/usr/bin/findmnt",
                }.get(name)

            results = [
                subprocess.CompletedProcess([], 0, "Mapped file Disc.iso as /dev/loop7.\n", ""),
                subprocess.CompletedProcess([], 0, "Mounted /dev/loop7 at /media/test/DISC.\n", ""),
                subprocess.CompletedProcess([], 0, str(mount_root) + "\n", ""),
                subprocess.CompletedProcess([], 0, "Unmounted /dev/loop7.\n", ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            with (
                patch("media_title_renamer.prepare.sys.platform", "linux"),
                patch("media_title_renamer.prepare.shutil.which", side_effect=find_tool),
                patch("media_title_renamer.prepare.subprocess.run", side_effect=results) as run,
            ):
                mount = _mount_iso(disc)
                self.assertEqual(mount, IsoMount(mount_root, "udisks", "/dev/loop7"))
                _unmount_iso(mount)

            self.assertEqual(
                run.call_args_list[0].args[0],
                [
                    "/usr/bin/udisksctl",
                    "loop-setup",
                    "--read-only",
                    "--no-user-interaction",
                    "--file",
                    str(disc),
                ],
            )
            self.assertEqual(
                run.call_args_list[-1].args[0],
                [
                    "/usr/bin/udisksctl",
                    "loop-delete",
                    "--no-user-interaction",
                    "--block-device",
                    "/dev/loop7",
                ],
            )

    def test_linux_iso_mount_uses_fuseiso_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disc = root / "Disc.iso"
            disc.write_bytes(b"iso")
            mount_root = root / "mount"
            mount_root.mkdir()

            def find_tool(name: str):
                return {
                    "fuseiso": "/usr/bin/fuseiso",
                    "fusermount3": "/usr/bin/fusermount3",
                }.get(name)

            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                patch("media_title_renamer.prepare.sys.platform", "linux"),
                patch("media_title_renamer.prepare.shutil.which", side_effect=find_tool),
                patch("media_title_renamer.prepare.tempfile.mkdtemp", return_value=str(mount_root)),
                patch("media_title_renamer.prepare.subprocess.run", return_value=completed) as run,
            ):
                mount = _mount_iso(disc)
                self.assertEqual(mount, IsoMount(mount_root, "fuseiso", mount_root))
                _unmount_iso(mount)

            self.assertEqual(
                run.call_args_list[0].args[0],
                ["/usr/bin/fuseiso", str(disc), str(mount_root)],
            )
            self.assertEqual(
                run.call_args_list[1].args[0],
                ["/usr/bin/fusermount3", "-u", str(mount_root)],
            )
            self.assertFalse(mount_root.exists())

    def test_linux_iso_mount_uses_passwordless_sudo_in_ssh_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disc = root / "Disc.iso"
            disc.write_bytes(b"iso")
            mount_root = root / "mount"
            mount_root.mkdir()

            def find_tool(name: str):
                return {
                    "sudo": "/usr/bin/sudo",
                    "mount": "/usr/bin/mount",
                    "umount": "/usr/bin/umount",
                }.get(name)

            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                patch("media_title_renamer.prepare.sys.platform", "linux"),
                patch("media_title_renamer.prepare.shutil.which", side_effect=find_tool),
                patch("media_title_renamer.prepare.tempfile.mkdtemp", return_value=str(mount_root)),
                patch("media_title_renamer.prepare.subprocess.run", return_value=completed) as run,
            ):
                mount = _mount_iso(disc)
                self.assertEqual(mount, IsoMount(mount_root, "sudo", mount_root))
                _unmount_iso(mount)

            self.assertEqual(
                run.call_args_list[0].args[0],
                [
                    "/usr/bin/sudo",
                    "-n",
                    "/usr/bin/mount",
                    "-o",
                    "loop,ro,nosuid,nodev,noexec",
                    str(disc),
                    str(mount_root),
                ],
            )
            self.assertEqual(
                run.call_args_list[1].args[0],
                ["/usr/bin/sudo", "-n", "/usr/bin/umount", str(mount_root)],
            )
            self.assertFalse(mount_root.exists())

    def test_tv_disc_iso_hints_support_chinese_season_and_disc(self):
        root = Path(r"D:\永不者-The.Nevers-{tmdb=80828}")
        first = root / "[永不者第一季.The.Nevers.2021][第1碟.DIY官译简繁中字][TTG][42.61GB].iso"
        second = root / "[永不者第一季.The.Nevers.2021][第2碟.DIY官译简繁中字][TTG][42.61GB].ISO"

        self.assertEqual(_disc_episode(first, root), "S01D01")
        self.assertEqual(_disc_episode(second, root), "S01D02")
        self.assertEqual(_disc_episode(root / "The.Nevers.S01D02.iso", root), "S01D02")
        self.assertEqual(_bracket_release_group(first), "TTG")
        self.assertEqual(_embedded_tmdb_id(root), 80828)

    def test_bdinfo_report_can_supply_disc_set_title_media(self):
        report = """
VIDEO:
MPEG-H HEVC Video / 62000 kbps / 2160p / 23.976 fps / 16:9 / HDR10 / Dolby Vision
AUDIO:
Dolby TrueHD/Atmos Audio English / 4608 kbps / 7.1 / 48 kHz
Dolby Digital Audio Chinese / 640 kbps / 5.1 / 48 kHz
SUBTITLES:
English
"""
        media = _media_from_bdinfo(report, "UHD BluRay")

        self.assertEqual(media.resolution, "2160p")
        self.assertEqual(media.video_codec, "HEVC")
        self.assertEqual(media.hdr, ("HDR10", "DoVi"))
        self.assertEqual(media.audio_codec, "TrueHD Atmos")
        self.assertEqual(media.audio_channels, "7.1")
        self.assertEqual(media.audio_tracks, 2)

    @patch("media_title_renamer.prepare.prepare_technical_info")
    @patch("media_title_renamer.prepare.read_mediainfo")
    def test_tv_bluray_disc_folder_uses_one_bdinfo_and_renames_by_disc(
        self,
        read_mediainfo,
        prepare_technical_info_mock,
    ):
        report = """
PLAYLIST REPORT:
Name: 00001.MPLS
VIDEO:
MPEG-4 AVC Video / 30000 kbps / 1080p / 23.976 fps / 16:9
AUDIO:
Dolby TrueHD Audio English / 3000 kbps / 5.1 / 48 kHz
SUBTITLES:
English
"""
        prepare_technical_info_mock.return_value = (
            "BDInfo",
            report,
            Path("temporary-bdinfo.txt"),
            "00001",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "永不者-The.Nevers-{tmdb=80828}"
            root.mkdir()
            first = root / "[永不者第一季.The.Nevers.2021][第1碟.DIY官译简繁中字][TTG][42.61GB].iso"
            second = root / "[永不者第一季.The.Nevers.2021][第2碟.DIY官译简繁中字][TTG][42.61GB].ISO"
            first.write_bytes(b"disc one")
            second.write_bytes(b"disc two")

            with redirect_stdout(io.StringIO()):
                package_path = prepare_main(
                    [
                        str(root),
                        "--title",
                        "The Nevers",
                        "--year",
                        "2021",
                        "--source",
                        "BluRay",
                        "--offline",
                        "--douban-url",
                        "https://movie.douban.com/subject/1/",
                        "--skip-screenshots",
                        "--skip-torrent",
                        "--apply",
                    ]
                )
            package = json.loads(package_path.read_text(encoding="utf-8"))
            renamed_root = root.parent / "The Nevers-2021-[tmdb=80828]"
            prepared_files = sorted(str(path.relative_to(renamed_root)) for path in renamed_root.rglob("*.iso"))

        read_mediainfo.assert_not_called()
        prepare_technical_info_mock.assert_called_once()
        self.assertEqual(package["episode"], "S01")
        self.assertEqual(package["category"], "影剧/综艺/BluRay")
        self.assertEqual(package["technical_info_type"], "BDInfo")
        self.assertEqual(package["bdinfo_playlist"], "00001")
        self.assertEqual(package["group"], "TTG")
        self.assertEqual(package["filename"], "The Nevers-2021-[tmdb=80828]")
        self.assertEqual(package["prepared_path"], str(renamed_root))
        self.assertEqual(
            prepared_files,
            [
                str(Path("Season 01") / "The Nevers 2021 S01D01 1080p BluRay AVC TrueHD5.1-TTG.iso"),
                str(Path("Season 01") / "The Nevers 2021 S01D02 1080p BluRay AVC TrueHD5.1-TTG.iso"),
            ],
        )

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
            # Scene folders sometimes omit the year on the first episode while
            # later files still carry it; folder preparation should recover it
            # from the complete episode set.
            first = root / "Example.Show.S01E01.WEB-DL.1080p.AVC.DD5.1-GRP.mkv"
            second.write_bytes(b"episode two")
            first.write_bytes(b"episode one")
            with redirect_stdout(io.StringIO()):
                package_path = prepare_main(
                    [
                        str(root),
                        "--title",
                        "Example Show",
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
            renamed_root = root.parent / "Example Show-2024"
            prepared_files = sorted(
                str(path.relative_to(renamed_root))
                for path in renamed_root.rglob("*.mkv")
            )

        read_mediainfo.assert_called_once()
        self.assertEqual(read_mediainfo.call_args.args[0].name, first.name)
        self.assertEqual(read_mediainfo_text.call_count, 1)
        self.assertTrue(package["media_probe_path"].endswith("Example Show 2024 S01E01 1080p WEB-DL H.264 DD5.1-GRP.mkv"))
        self.assertEqual(len(package["files"]), 2)
        self.assertEqual(package["filename"], "Example Show-2024")
        self.assertEqual(package["prepared_path"], str(renamed_root))
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

    def test_torrent_progress_reports_file_percentage_size_and_eta(self):
        output = io.StringIO()
        with redirect_stdout(output):
            progress = TorrentProgress(2 * 1024 * 1024, 2)
            progress.start_file(1, Path("first-episode.mkv"))
            progress.advance(1024 * 1024)
            progress.start_file(2, Path("second-episode.mkv"))
            progress.advance(1024 * 1024)
            progress.finish()
        text = output.getvalue()
        self.assertIn("制种进度", text)
        self.assertIn("100.0%", text)
        self.assertIn("2.0 MiB/2.0 MiB", text)
        self.assertIn("文件 2/2", text)
        self.assertIn("制种完成。", text)

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

    def test_v1_single_torrent_resumes_from_saved_piece_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.mkv"
            content = b"a" * (64 * 1024) + b"b" * 17
            source.write_bytes(content)
            output = root / "episode.torrent"
            file_specs = _torrent_file_specs([(source, Path("Episode.mkv"))])
            identity = _torrent_resume_identity(
                kind="single",
                total_size=len(content),
                piece_length=64 * 1024,
                logical_root_name="Episode.mkv",
                files=file_specs,
            )
            checkpoint = TorrentHashCheckpoint.open_or_create(output, identity)
            checkpoint.append_hash(content[: 64 * 1024])
            checkpoint.close()
            metadata_path, pieces_path = _resume_paths(output)
            self.assertTrue(metadata_path.is_file())
            self.assertTrue(pieces_path.is_file())

            create_private_v1_torrent(source, output, "Episode.mkv")

            payload = output.read_bytes()
            self.assertIn(hashlib.sha1(content[: 64 * 1024]).digest(), payload)
            self.assertIn(hashlib.sha1(content[64 * 1024 :]).digest(), payload)
            self.assertFalse(metadata_path.exists())
            self.assertFalse(pieces_path.exists())

    def test_v1_folder_torrent_resumes_across_file_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Show"
            root.mkdir()
            first = root / "one.mkv"
            second = root / "two.mkv"
            first_content = b"a" * (32 * 1024)
            second_content = b"b" * (80 * 1024)
            first.write_bytes(first_content)
            second.write_bytes(second_content)
            output = Path(directory) / "Show.torrent"
            files = [(first, Path("Season 01") / "Show S01E01.mkv"), (second, Path("Season 01") / "Show S01E02.mkv")]
            file_specs = _torrent_file_specs(files)
            identity = _torrent_resume_identity(
                kind="folder",
                total_size=len(first_content) + len(second_content),
                piece_length=64 * 1024,
                logical_root_name="Show",
                files=file_specs,
            )
            checkpoint = TorrentHashCheckpoint.open_or_create(output, identity)
            combined = first_content + second_content
            checkpoint.append_hash(combined[: 64 * 1024])
            checkpoint.close()

            create_private_v1_folder_torrent(root, files, output, "Show")

            payload = output.read_bytes()
            self.assertIn(hashlib.sha1(combined[: 64 * 1024]).digest(), payload)
            self.assertIn(hashlib.sha1(combined[64 * 1024 :]).digest(), payload)
            metadata_path, pieces_path = _resume_paths(output)
            self.assertFalse(metadata_path.exists())
            self.assertFalse(pieces_path.exists())

    def test_piece_hasher_streams_across_chunks_without_pending_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "stream.torrent"
            identity = _torrent_resume_identity(
                kind="single",
                total_size=11,
                piece_length=8,
                logical_root_name="stream.mkv",
                files=[],
            )
            checkpoint = TorrentHashCheckpoint.open_or_create(output, identity)
            hasher = TorrentPieceHasher(checkpoint, 8)
            hasher.update(b"abc")
            hasher.update(b"defghijk")
            hasher.finish()
            hashes = checkpoint.read_hashes()
            checkpoint.cleanup()

        self.assertEqual(
            hashes,
            hashlib.sha1(b"abcdefgh").digest() + hashlib.sha1(b"ijk").digest(),
        )

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
            infer_mteam_category(kind="tv", source="WEB-DL", resolution="544p", animation=False),
            "影剧/综艺/SD",
        )
        self.assertEqual(
            infer_mteam_category(kind="tv", source="WEB-DL", resolution="720p", animation=False),
            "影剧/综艺/HD",
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
