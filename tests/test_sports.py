import pytest

from media_title_renamer.autonomous import deterministic_category
from media_title_renamer.sports import is_sports_resource


@pytest.mark.parametrize("title", [
    "CCTV-5+ 2026 Asian Games-Beach Volleyball Women's Semifinals HDTV",
    "2026 Asian Games Tennis Mens Final Wong vs Wu CHI",
    "UEFA Nations League 2026-27 Match Day 3 Highlights",
    "2026 Asian Games Tennis Womens Doubles Final Zhang Wu vs Xie Liang",
    "CCTV-5 2026 Asian Games-Compound Bow Women's Individual Bronze Medal Match",
    "China Open Women's Singles 20261003 Gao Xinyu vs Swiatek",
    "EPL 2023 Bournemouth vs Chelsea",
    "China Open Men's Singles 20261003 De Minaur vs Alize",
])
def test_mteam_sports_event_titles_from_reference_screenshot(title):
    package = {"title": title, "kind": "unknown", "category": "电影/HD"}

    assert is_sports_resource(package)


def test_sports_category_is_explicit_even_without_filename_cues():
    assert is_sports_resource({"title": "Live Event", "kind": "movie", "category": "運動"})


def test_obvious_single_match_is_sports_even_when_model_says_movie():
    package = {
        "title": "2026 Asian Games Tennis Men's Final Wong vs Wu CHI 1080p",
        "kind": "movie",
        "category": "电影/HD",
    }
    rows = [{"name": package["title"] + ".mkv", "bytes": 10}]

    assert is_sports_resource(package, rows)
    assert deterministic_category(package, rows) == "運動"


def test_sports_theme_film_does_not_become_sports_event():
    package = {
        "title": "The Final Match 2024",
        "kind": "movie",
        "category": "电影/HD",
    }

    assert not is_sports_resource(package)


def test_sports_word_in_episode_title_does_not_reclassify_tv_series():
    package = {
        "title": "Friends 1994 S01-S10 1080p WEB-DL H.265",
        "kind": "tv",
        "category": "影剧/综艺/HD",
    }
    rows = [{
        "name": "S03/Friends.S03E09.The.One.with.the.Football.1080p.x265.mkv",
        "bytes": 10,
    }]

    assert not is_sports_resource(package, rows)
    assert deterministic_category(package, rows) == "影剧/综艺/HD"
