import json
from pathlib import Path

from media_title_renamer.douban_backfill import (
    BackfillResult,
    WorkIdentity,
    _canonical_subject_url,
    _choose_candidate,
    _consensus_result,
    _discover_paths,
    _existing_link_consensus,
    _identity,
    _load_identity_overrides,
    _merge_identity_override,
    _query_plan,
    _run_one,
    _search_package,
    _seed_rate_state,
    _web_search_candidates,
    _web_page_candidate,
    _write_package,
    main,
)
from media_title_renamer.prepare import DoubanMatch


def _wheel_identity():
    return WorkIdentity(
        titles=("Wheel of Fortune", "幸运轮盘"),
        year="1983",
        media_type="tv",
        tmdb_id="2778",
        imdb_id="tt0072584",
        season_number=29,
    )


def _wheel_candidate(*, season=29, year="2010", item_id="1234567"):
    return DoubanMatch(
        id=item_id,
        url=f"https://movie.douban.com/subject/{item_id}/",
        title="幸运轮盘 第29季",
        original_title="Wheel of Fortune Season 29",
        year=year,
        score=94.0,
        season_number=season,
        source="html_search",
    )


def test_query_plan_includes_exact_imdb_query_within_bound():
    plan = _query_plan(_wheel_identity(), 3)
    assert len(plan) == 3
    assert plan[-1] == ("tt0072584", None)
    assert "Season 29" in plan[0][0]
    assert plan[0][1] is None  # Season air year must not use the series year filter.


def test_single_query_budget_prefers_title_over_imdb_number():
    assert _query_plan(_wheel_identity(), 1) == [("Wheel of Fortune Season 29", None)]


def test_exact_imdb_match_can_confirm_different_season_air_year():
    candidate = _wheel_candidate(year="2010")
    chosen, confidence, matched_by, refusal = _choose_candidate(
        [candidate],
        {candidate.id: {"imdb_id": "tt0072584", "media_type": "tv"}},
        _wheel_identity(),
    )
    assert chosen == candidate
    assert confidence == 1.0
    assert matched_by == "exact_imdb_id"
    assert not refusal


def test_exact_imdb_does_not_override_wrong_season_or_media_type():
    wrong_season = _wheel_candidate(season=28)
    assert _choose_candidate(
        [wrong_season],
        {wrong_season.id: {"imdb_id": "tt0072584", "media_type": "tv"}},
        _wheel_identity(),
    )[0] is None


def test_exact_series_imdb_allows_first_season_page_for_later_season():
    identity = WorkIdentity(
        ("The Mega-Brands That Built America",), "2023", "tv", "231217", "tt28224056", 4
    )
    candidate = DoubanMatch(
        "36488669", "https://movie.douban.com/subject/36488669/", "造就美国的超级品牌 第一季",
        "The Mega-Brands That Built America Season 1", "2023", 90, 1, "html_search",
    )
    chosen, confidence, matched_by, refusal = _choose_candidate(
        [candidate],
        {candidate.id: {"imdb_id": "tt28224056", "media_type": "tv"}},
        identity,
    )

    assert chosen == candidate
    assert confidence == 0.98
    assert matched_by == "exact_imdb_id_series_first_season"
    assert not refusal


def test_first_season_fallback_still_requires_matching_series_type_year_and_imdb():
    identity = WorkIdentity(
        ("The Mega-Brands That Built America",), "2023", "tv", "231217", "tt28224056", 4
    )
    wrong_year = DoubanMatch(
        "36488669", "https://movie.douban.com/subject/36488669/", "造就美国的超级品牌 第一季",
        "The Mega-Brands That Built America Season 1", "2025", 90, 1, "html_search",
    )
    evidence = {wrong_year.id: {"imdb_id": "tt28224056", "media_type": "tv"}}
    assert _choose_candidate([wrong_year], evidence, identity)[0] is None

    right_year_wrong_type = DoubanMatch(
        "36488670", "https://movie.douban.com/subject/36488670/", "造就美国的超级品牌 第一季",
        "The Mega-Brands That Built America Season 1", "2023", 90, 1, "html_search",
    )
    assert _choose_candidate(
        [right_year_wrong_type],
        {right_year_wrong_type.id: {"imdb_id": "tt28224056", "media_type": "movie"}},
        identity,
    )[0] is None


def test_first_season_page_can_match_exact_series_title_and_premiere_year_without_imdb():
    identity = WorkIdentity(("Monsters",), "1988", "tv", "6323", "tt0094512", 3)
    candidate = DoubanMatch(
        "3929933", "https://movie.douban.com/subject/3929933/", "怪物 第一季",
        "Monsters Season 1", "1988", 11.67, 1, "html_search",
    )

    chosen, confidence, matched_by, refusal = _choose_candidate(
        [candidate], {candidate.id: {"imdb_id": "", "media_type": "tv"}}, identity
    )

    assert chosen == candidate
    assert confidence == 0.94
    assert matched_by == "exact_title_year_series_first_season"
    assert not refusal


def test_first_season_title_year_fallback_rejects_conflicts_and_unverified_pages():
    identity = WorkIdentity(("Monsters",), "1988", "tv", "6323", "tt0094512", 3)
    base = DoubanMatch(
        "3929933", "https://movie.douban.com/subject/3929933/", "怪物 第一季",
        "Monsters Season 1", "1988", 95, 1, "html_search",
    )

    wrong_year = DoubanMatch(**{**base.__dict__, "year": "1989"})
    assert _choose_candidate([wrong_year], {base.id: {"media_type": "tv"}}, identity)[0] is None

    wrong_title = DoubanMatch(**{**base.__dict__, "original_title": "Monster Season 1"})
    assert _choose_candidate([wrong_title], {base.id: {"media_type": "tv"}}, identity)[0] is None

    evidence = {base.id: {"media_type": "tv", "imdb_id": "tt0000001"}}
    assert _choose_candidate([base], evidence, identity)[0] is None

    wrong_type = {base.id: {"media_type": "movie", "imdb_id": ""}}
    assert _choose_candidate([base], wrong_type, identity)[0] is None

    unverified = {base.id: {"media_type": "tv", "imdb_id": "", "error": "subject_http_403"}}
    assert _choose_candidate([base], unverified, identity)[0] is None


def test_first_season_title_year_fallback_rejects_close_ambiguity():
    identity = WorkIdentity(("Monsters",), "1988", "tv", "6323", "tt0094512", 3)
    first = DoubanMatch(
        "3929933", "https://movie.douban.com/subject/3929933/", "怪物 第一季",
        "Monsters Season 1", "1988", 91, 1, "html_search",
    )
    second = DoubanMatch(
        "3929934", "https://movie.douban.com/subject/3929934/", "怪物 第一季",
        "Monsters Season 1", "1988", 87, 1, "html_search",
    )

    chosen, confidence, matched_by, refusal = _choose_candidate(
        [first, second],
        {first.id: {"media_type": "tv"}, second.id: {"media_type": "tv"}},
        identity,
    )

    assert chosen is None
    assert confidence is None
    assert not matched_by
    assert "多个第一季豆瓣候选" in refusal


def test_exact_imdb_and_exact_title_allow_mislabelled_series_level_douban_page():
    identity = WorkIdentity(
        ("The Kids of Degrassi Street",), "1979", "tv", "995", "tt0083435", 1
    )
    candidate = DoubanMatch(
        "2150460", "https://movie.douban.com/subject/2150460/",
        "无德格拉西街的孩子们", "The Kids of Degrassi Street", "1984", 98, None, "html_search",
    )
    chosen, confidence, matched_by, refusal = _choose_candidate(
        [candidate],
        {candidate.id: {"imdb_id": "tt0083435", "media_type": "movie"}},
        identity,
    )

    assert chosen == candidate
    assert confidence == 0.99
    assert matched_by == "exact_imdb_id_type_override"
    assert not refusal


def test_exact_imdb_type_override_rejects_non_exact_title_or_season_page():
    identity = WorkIdentity(
        ("The Kids of Degrassi Street",), "1979", "tv", "995", "tt0083435", 1
    )
    wrong_title = DoubanMatch(
        "2150461", "https://movie.douban.com/subject/2150461/", "Degrassi Junior High",
        "Degrassi Junior High", "1987", 97, None, "html_search",
    )
    evidence = {wrong_title.id: {"imdb_id": "tt0083435", "media_type": "movie"}}
    assert _choose_candidate([wrong_title], evidence, identity)[0] is None

    numbered_page = DoubanMatch(
        "2150462", "https://movie.douban.com/subject/2150462/", "The Kids of Degrassi Street Season 1",
        "The Kids of Degrassi Street", "1984", 97, 1, "html_search",
    )
    evidence = {numbered_page.id: {"imdb_id": "tt0083435", "media_type": "movie"}}
    assert _choose_candidate([numbered_page], evidence, identity)[0] is None


def test_exact_imdb_series_page_is_not_discarded_by_weak_season_candidate():
    identity = WorkIdentity(
        ("The Kids of Degrassi Street",), "1979", "tv", "995", "tt0083435", 1
    )
    exact_series = DoubanMatch(
        "2150460", "https://movie.douban.com/subject/2150460/",
        "无德格拉西街的孩子们 The Kids of Degrassi Street", "The Kids of Degrassi Street",
        "1984", 42.5, None, "verified_subject",
    )
    weak_season = DoubanMatch(
        "2150461", "https://movie.douban.com/subject/2150461/",
        "The Kids of Degrassi Street Season 1", "The Kids of Degrassi Street", "1979", 90, 1, "html_search",
    )
    evidence = {
        exact_series.id: {"imdb_id": "tt0083435", "media_type": "movie"},
        weak_season.id: {"imdb_id": "tt0099999", "media_type": "tv"},
    }

    chosen, confidence, matched_by, refusal = _choose_candidate(
        [weak_season, exact_series], evidence, identity
    )

    assert chosen == exact_series
    assert confidence == 0.99
    assert matched_by == "exact_imdb_id_type_override"
    assert not refusal


def test_combined_douban_heading_retains_exact_original_title_for_type_override(monkeypatch):
    from media_title_renamer import web_candidates

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://movie.douban.com/subject/2150460/"

        def read(self, _size=-1):
            return (
                '<script type="application/ld+json">{"@type":"Movie"}</script>'
                '<span property="v:itemreviewed">无德格拉西街的孩子们 The Kids of Degrassi Street</span>'
                '<span class="year">(1984)</span><div id="info">IMDb: tt0083435</div>'
            ).encode()

    monkeypatch.setattr(web_candidates, "open_search", lambda *_args: Response())
    monkeypatch.setattr("media_title_renamer.douban_backfill._douban_request_headers", lambda _diag: {})
    identity = WorkIdentity(("The Kids of Degrassi Street",), "1979", "tv", "995", "tt0083435", 1)
    diagnostics = []

    candidate, evidence = _web_page_candidate(
        "https://movie.douban.com/subject/2150460/", identity, diagnostics
    )
    chosen, confidence, matched_by, refusal = _choose_candidate([candidate], {candidate.id: evidence}, identity)

    assert candidate.original_title == "The Kids of Degrassi Street"
    assert evidence == {"imdb_id": "tt0083435", "media_type": "movie"}
    assert chosen == candidate
    assert confidence == 0.99
    assert matched_by == "exact_imdb_id_type_override"
    assert not refusal


def test_search_package_rechecks_explicit_candidate_urls(monkeypatch):
    import media_title_renamer.douban_backfill as backfill

    package = {
        "title": "The Kids of Degrassi Street 1979 S01",
        "kind": "tv",
        "episode": "S01",
        "tmdb": {"id": 995, "name": "The Kids of Degrassi Street", "year": "1979", "media_type": "tv", "imdb_id": "tt0083435"},
        "imdb_url": "https://www.imdb.com/title/tt0083435/",
    }
    identity = _identity(package)
    candidate = DoubanMatch(
        "2150460", "https://movie.douban.com/subject/2150460/",
        "无德格拉西街的孩子们 The Kids of Degrassi Street", "The Kids of Degrassi Street",
        "1984", 100, None, "cliproxy_web_search_verified_subject",
    )
    evidence = {"imdb_id": "tt0083435", "media_type": "movie"}
    monkeypatch.setattr(backfill, "_douban_search_page_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backfill, "_suggest_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backfill, "_web_page_candidate", lambda *_args, **_kwargs: (candidate, evidence))

    class TmdbUnavailable:
        available = False

    result_identity, candidates, page_evidence, _queries, diagnostics = _search_package(
        package,
        max_queries=1,
        max_details=0,
        tmdb_client=TmdbUnavailable(),
        identity_override={"candidate_urls": [candidate.url]},
    )

    assert result_identity == identity
    assert candidates == [candidate]
    assert page_evidence[candidate.id] == evidence
    assert any("重新核验" in item for item in diagnostics)


def test_identity_override_adds_alias_and_missing_imdb_without_replacing_identity():
    package = {
        "title": "Fai caan che 1984 WEB-DL 1080p",
        "year": "1984",
        "kind": "movie",
        "subtitle": "Fai caan che [汉语]",
        "douban_url": "",
    }
    identity, fields = _merge_identity_override(
        _identity(package),
        {
            "titles": ["Wheels on Meals", "快餐车"],
            "year": "1984",
            "media_type": "movie",
            "imdb_id": "tt0087578",
        },
    )

    assert identity.year == "1984"
    assert identity.media_type == "movie"
    assert identity.imdb_id == "tt0087578"
    assert "Fai caan che" in identity.titles
    assert "Wheels on Meals" in identity.titles
    assert "快餐车" in identity.titles
    assert fields == ["imdb_id", "media_type", "titles", "year"]


def test_run_one_searches_with_identity_override_and_records_its_fields(monkeypatch, tmp_path):
    import media_title_renamer.douban_backfill as backfill

    package = {
        "title": "Fai caan che 1984 WEB-DL 1080p",
        "year": "1984",
        "kind": "movie",
        "subtitle": "Fai caan che [汉语]",
        "douban_url": "",
    }
    override = {
        "titles": ["Wheels on Meals", "快餐车"],
        "media_type": "movie",
        "imdb_id": "tt0087578",
    }
    candidate = DoubanMatch(
        "1292867", "https://movie.douban.com/subject/1292867/",
        "快餐车", "Wheels on Meals", "1984", 100, None, "html_search",
    )
    seen = {}

    def fake_search(_package, *, identity_override, **_kwargs):
        seen["override"] = identity_override
        identity, _fields = _merge_identity_override(_identity(package), identity_override)
        return identity, [candidate], {candidate.id: {"imdb_id": "tt0087578", "media_type": "movie"}}, ["Wheels on Meals"], []

    monkeypatch.setattr(backfill, "_search_package", fake_search)
    result = _run_one(
        package,
        [tmp_path / "mteam-prepare.json"],
        max_queries=2,
        max_details=2,
        tmdb_client=object(),
        identity_override=override,
    )

    assert seen["override"] == override
    assert result.status == "mapped"
    assert result.douban_url == candidate.url
    assert result.imdb_id == "tt0087578"
    assert result.identity_override_fields == ["imdb_id", "media_type", "titles"]


def test_identity_override_rejects_conflicts_and_invalid_identifiers():
    identity = WorkIdentity(("Example",), "1984", "movie", "123", "tt0000001", None)
    try:
        _merge_identity_override(identity, {"imdb_id": "tt0000002"})
    except ValueError as exc:
        assert "conflicts with existing imdb_id" in str(exc)
    else:
        raise AssertionError("conflicting IMDb override was accepted")
    try:
        _merge_identity_override(identity, {"imdb_id": "not-an-imdb-id"})
    except ValueError as exc:
        assert "valid tt identifier" in str(exc)
    else:
        raise AssertionError("invalid IMDb override was accepted")


def test_identity_override_file_uses_exact_resolved_package_paths(tmp_path):
    package = tmp_path / "mteam-prepare.json"
    override_file = tmp_path / "identity-overrides.json"
    override_file.write_text(json.dumps({
        str(package): {"titles": ["Wheels on Meals"], "year": "1984", "media_type": "movie", "imdb_id": "tt0087578"}
    }), encoding="utf-8")

    overrides = _load_identity_overrides(override_file)

    assert overrides[str(package.resolve())]["imdb_id"] == "tt0087578"


def test_exact_imdb_can_resolve_a_localized_title_not_in_tmdb_names():
    candidate = _wheel_candidate()
    chosen, confidence, matched_by, refusal = _choose_candidate(
        [candidate],
        {candidate.id: {"imdb_id": "tt0072584", "media_type": "tv"}},
        WorkIdentity(("Wheel of Fortune",), "1983", "tv", "2778", "tt0072584", 29),
    )
    assert chosen == candidate
    assert confidence == 1.0
    assert matched_by == "exact_imdb_id"
    assert not refusal

    right_season = _wheel_candidate()
    assert _choose_candidate(
        [right_season],
        {right_season.id: {"imdb_id": "tt0072584", "media_type": "movie"}},
        _wheel_identity(),
    )[0] is None


def test_title_year_selection_requires_clear_winner():
    identity = WorkIdentity(("Dr Lisa to the Rescue",), "2015", "tv", "104535", "", 1)
    one = DoubanMatch("1111111", "https://movie.douban.com/subject/1111111/",
                      "Dr Lisa to the Rescue", "Dr Lisa to the Rescue", "2015", 100, 1, "html_search")
    chosen, confidence, matched_by, refusal = _choose_candidate([one], {}, identity)
    assert chosen == one and confidence == 0.95 and matched_by == "title_year_season" and not refusal

    two = DoubanMatch("2222222", "https://movie.douban.com/subject/2222222/",
                      "Dr Lisa to the Rescue", "Dr Lisa to the Rescue", "2015", 99, 1, "html_search")
    chosen, confidence, matched_by, refusal = _choose_candidate([one, two], {}, identity)
    assert chosen is None and confidence is None and not matched_by
    assert "多个豆瓣候选" in refusal


def test_candidate_url_is_rebuilt_from_id_and_rejects_external_hosts():
    candidate = _wheel_candidate()
    candidate = DoubanMatch(candidate.id, "https://evil.example/subject/1234567/",
                             candidate.title, candidate.original_title, candidate.year,
                             candidate.score, candidate.season_number, candidate.source)
    assert _canonical_subject_url(candidate) == ""


def test_clipproxy_web_search_proposal_is_page_verified_before_selection(monkeypatch):
    from media_title_renamer import web_candidates

    class Assistant:
        available = True
        last_web_search_used = True
        last_web_search_queries = ['"Wheel of Fortune" 1983 豆瓣']
        seen_queries = []

        def __init__(self, **_kwargs):
            pass

        def _complete_responses(self, system, user):
            assert "web_search" in system
            request = json.loads(user)
            assert "tt0072584" in user
            self.seen_queries.append(request["search_query"])
            return {"urls": [
                "https://movie.douban.com/subject/1234567/?from=search",
                "https://not-douban.example/subject/8888888/",
            ]}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://movie.douban.com/subject/1234567/"

        def read(self, _size=-1):
            return (
                '<script type="application/ld+json">{"@type":"TVSeries"}</script>'
                '<span property="v:itemreviewed">Wheel of Fortune - 第29季</span>'
                '<span class="year">(2010)</span>'
                '<div id="info">又名: Wheel of Fortune Season 29<br>'
                'IMDb: tt0072584</div>'
            ).encode("utf-8")

    monkeypatch.setattr("media_title_renamer.ai.CliproxyAssistant", Assistant)
    monkeypatch.setattr(web_candidates, "open_search", lambda *_args: Response())
    monkeypatch.setattr("media_title_renamer.douban_backfill._douban_request_headers", lambda _diag: {})
    diagnostics = []
    identity = WorkIdentity(("Wheel of Fortune",), "1983", "tv", "2778", "tt0072584", 29)

    candidates, evidence, used = _web_search_candidates(identity, diagnostics)

    assert used
    assert [candidate.id for candidate in candidates] == ["1234567"]
    assert candidates[0].original_title == "Wheel of Fortune Season 29"
    assert evidence["1234567"] == {"imdb_id": "tt0072584", "media_type": "tv"}
    chosen, confidence, method, refusal = _choose_candidate(candidates, evidence, identity)
    assert chosen == candidates[0]
    assert confidence == 1.0 and method == "exact_imdb_id" and not refusal
    assert any("实际调用" in item for item in diagnostics)
    assert any('"Wheel of Fortune" 1983 豆瓣' in item for item in diagnostics)
    assert Assistant.seen_queries == ["Wheel of Fortune 1983 Season 29 豆瓣", "tt0072584 豆瓣"]


def test_clipproxy_answer_without_actual_web_search_is_rejected(monkeypatch):
    class Assistant:
        available = True
        last_web_search_used = False

        def __init__(self, **_kwargs):
            pass

        def _complete_responses(self, *_args):
            return {"urls": ["https://movie.douban.com/subject/1234567/"]}

    monkeypatch.setattr("media_title_renamer.ai.CliproxyAssistant", Assistant)
    diagnostics = []
    identity = WorkIdentity(("Wheel of Fortune",), "1983", "tv", "2778", "", 29)

    candidates, evidence, used = _web_search_candidates(identity, diagnostics)

    assert not used
    assert not candidates and not evidence
    assert any("拒绝使用模型答案" in item for item in diagnostics)


def test_run_one_uses_verified_web_candidate_only_after_regular_search_fails(monkeypatch, tmp_path):
    import media_title_renamer.douban_backfill as backfill

    package = {
        "title": "Wheel of Fortune 1983 S29 1080p WEB-DL",
        "douban_url": "",
        "kind": "tv",
        "episode": "S29",
        "tmdb": {"id": 2778, "name": "Wheel of Fortune", "year": "1983", "media_type": "tv"},
        "imdb_url": "https://www.imdb.com/title/tt0072584/",
    }
    candidate = _wheel_candidate(item_id="1234567")
    monkeypatch.setattr(
        backfill,
        "_search_package",
        lambda *_args, **_kwargs: (
            _wheel_identity(), [], {}, ["Wheel of Fortune Season 29"], ["normal search empty"]
        ),
    )
    monkeypatch.setattr(
        backfill,
        "_web_search_candidates",
        lambda *_args, **_kwargs: ([candidate], {candidate.id: {"imdb_id": "tt0072584", "media_type": "tv"}}, True),
    )

    result = _run_one(
        package, [tmp_path / "mteam-prepare.json"], max_queries=3, max_details=3,
        tmdb_client=object(), use_web_search=True,
    )

    assert result.status == "mapped"
    assert result.douban_url == candidate.url
    assert result.matched_by == "exact_imdb_id"
    assert result.web_search_used


def test_web_search_only_skips_native_douban_search(monkeypatch, tmp_path):
    import media_title_renamer.douban_backfill as backfill

    class TmdbUnavailable:
        available = False

    package = {
        "title": "Wheel of Fortune 1983 S29 1080p WEB-DL",
        "douban_url": "",
        "kind": "tv",
        "episode": "S29",
        "tmdb": {"id": 2778, "name": "Wheel of Fortune", "year": "1983", "media_type": "tv"},
        "imdb_url": "https://www.imdb.com/title/tt0072584/",
    }
    candidate = _wheel_candidate(item_id="7654321")
    monkeypatch.setattr(backfill, "_search_package", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("native Douban search ran")))
    monkeypatch.setattr(
        backfill,
        "_web_search_candidates",
        lambda *_args, **_kwargs: ([candidate], {candidate.id: {"imdb_id": "tt0072584", "media_type": "tv"}}, True),
    )

    result = _run_one(
        package, [tmp_path / "mteam-prepare.json"], max_queries=3, max_details=3,
        tmdb_client=TmdbUnavailable(), use_web_search=True, web_search_only=True,
    )

    assert result.status == "mapped"
    assert result.web_search_used


def test_discover_paths_returns_canonical_packages_in_stable_path_order(tmp_path):
    first = tmp_path / "batch" / "tasks" / "b" / "mteam-prepare.json"
    second = tmp_path / "batch" / "tasks" / "A" / "mteam-prepare.json"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")

    discovered = _discover_paths([tmp_path / "batch"])

    assert discovered == sorted([first.resolve(), second.resolve()], key=lambda path: str(path).casefold())


def test_apply_writes_only_canonical_url_and_keeps_recoverable_backup(tmp_path):
    package_path = tmp_path / "mteam-prepare.json"
    original = {"title": "Wheel of Fortune 1983 S29", "douban_url": ""}
    package_path.write_text(json.dumps(original), encoding="utf-8")
    output = tmp_path / "report"
    chosen = {
        "id": "1234567",
        "url": "https://movie.douban.com/subject/1234567/",
        "title": "幸运轮盘 第29季",
        "original_title": "Wheel of Fortune Season 29",
        "year": "2010",
        "score": 94,
        "source": "html_search",
    }
    result = BackfillResult(
        package_paths=[str(package_path)], title=original["title"], tmdb_id="2778",
        imdb_id="tt0072584", status="mapped", douban_url=chosen["url"],
        confidence=1.0, matched_by="exact_imdb_id", query="tt0072584",
        chosen=chosen, candidates=[chosen], diagnostics=[],
    )

    assert _write_package(package_path, result, output_dir=output) == "updated"
    updated = json.loads(package_path.read_text(encoding="utf-8"))
    assert updated["douban_url"] == chosen["url"]
    assert updated["title"] == original["title"]
    assert updated["douban_selection"]["matched_by"] == "exact_imdb_id"
    backups = list((output / "backups").glob("*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == original


def test_apply_never_overwrites_an_existing_link(tmp_path):
    package_path = tmp_path / "mteam-prepare.json"
    package_path.write_text(json.dumps({"douban_url": "https://movie.douban.com/subject/7654321/"}),
                            encoding="utf-8")
    result = BackfillResult(
        package_paths=[str(package_path)], title="x", tmdb_id="", imdb_id="",
        status="mapped", chosen={"id": "1234567", "url": "https://movie.douban.com/subject/1234567/"},
    )
    assert _write_package(package_path, result, output_dir=tmp_path / "report").startswith("not overwritten")
    assert json.loads(package_path.read_text(encoding="utf-8"))["douban_url"].endswith("7654321/")


def _consensus_package(url=""):
    return {
        "title": "Stranger Things Tales from '85 2026 S02 WEB-DL",
        "kind": "tv",
        "episode": "S02",
        "douban_url": url,
        "tmdb": {"id": 224263, "media_type": "tv", "name": "Stranger Things: Tales from '85"},
        "imdb_url": "https://www.imdb.com/title/tt27486290/",
    }


def test_link_consensus_requires_two_independent_packages_with_exact_identity(tmp_path):
    url = "https://movie.douban.com/subject/38444244/"
    groups = []
    for name in ("1080p", "720p"):
        path = tmp_path / name / "mteam-prepare.json"
        path.parent.mkdir()
        path.write_text(json.dumps(_consensus_package(url)), encoding="utf-8")
        groups.append((_consensus_package(url), [path]))
    target_path = tmp_path / "target" / "mteam-prepare.json"
    target_path.parent.mkdir()
    target = _consensus_package("")

    result = _consensus_result(target, [target_path], _existing_link_consensus(groups))

    assert result is not None
    assert result.status == "mapped"
    assert result.douban_url == url
    assert result.matched_by == "same_tmdb_imdb_season_consensus"
    assert len(result.chosen["corroborating_package_paths"]) == 2


def test_link_consensus_refuses_single_or_conflicting_urls(tmp_path):
    url_a = "https://movie.douban.com/subject/38444244/"
    url_b = "https://movie.douban.com/subject/11111111/"
    first = tmp_path / "first" / "mteam-prepare.json"
    second = tmp_path / "second" / "mteam-prepare.json"
    first.parent.mkdir()
    second.parent.mkdir()
    target_path = tmp_path / "target" / "mteam-prepare.json"
    target_path.parent.mkdir()
    target = _consensus_package("")

    single = [(_consensus_package(url_a), [first])]
    assert _consensus_result(target, [target_path], _existing_link_consensus(single)) is None

    conflict = [(_consensus_package(url_a), [first]), (_consensus_package(url_b), [second])]
    assert _consensus_result(target, [target_path], _existing_link_consensus(conflict)) is None


def test_link_consensus_does_not_cross_seasons_or_imdb_ids(tmp_path):
    first = tmp_path / "first" / "mteam-prepare.json"
    second = tmp_path / "second" / "mteam-prepare.json"
    first.parent.mkdir()
    second.parent.mkdir()
    target = _consensus_package("")
    wrong_season = _consensus_package("https://movie.douban.com/subject/38444244/")
    wrong_season["episode"] = "S01"
    wrong_imdb = _consensus_package("https://movie.douban.com/subject/38444244/")
    wrong_imdb["imdb_url"] = "https://www.imdb.com/title/tt00000000/"

    index = _existing_link_consensus([(wrong_season, [first]), (wrong_imdb, [second])])

    assert _consensus_result(target, [tmp_path / "target.json"], index) is None


def test_consensus_only_cli_applies_link_without_cookie_or_network(monkeypatch, tmp_path):
    import media_title_renamer.douban_backfill as backfill

    source = tmp_path / "packages"
    url = "https://movie.douban.com/subject/38444244/"
    for name in ("one", "two"):
        path = source / name / "mteam-prepare.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(_consensus_package(url)), encoding="utf-8")
    target = source / "target" / "mteam-prepare.json"
    target.parent.mkdir()
    target.write_text(json.dumps(_consensus_package("")), encoding="utf-8")
    monkeypatch.setattr(backfill, "load_douban_cookie_header", lambda: (_ for _ in ()).throw(AssertionError("cookie should not load")))
    monkeypatch.setattr(backfill, "_run_one", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network search should not run")))
    report = tmp_path / "consensus-report"

    assert main([str(source), "--consensus-only", "--apply", "--output", str(report)]) == 0
    updated = json.loads(target.read_text(encoding="utf-8"))
    assert updated["douban_url"] == url
    records = [json.loads(line) for line in (report / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(item["status"] == "mapped" for item in records) == 1
    assert sum(item["status"] == "consensus_unavailable" for item in records) == 0


def test_rate_state_upgrade_inherits_latest_previous_batch_timestamp(monkeypatch, tmp_path):
    import media_title_renamer.douban_backfill as backfill

    root = tmp_path / "rate-root"
    old_state = root / "old-batch" / "cache" / "douban-request-rate.txt"
    old_state.parent.mkdir(parents=True)
    old_state.write_text("12345.5", encoding="ascii")
    monkeypatch.setattr(backfill, "DEFAULT_OUTPUT_ROOT", root)
    shared_state = root / "cache" / "douban-request-rate.txt"

    _seed_rate_state(shared_state)

    assert shared_state.read_text(encoding="ascii") == "12345.5"
