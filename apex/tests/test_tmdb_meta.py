"""Unit tests for apex_stream.tmdb_meta — parsing, matching & NFO builders."""

import pytest

from apex_stream.tmdb_meta import (
    best_tmdb_match,
    build_episode_nfo,
    build_movie_nfo,
    build_tvshow_nfo,
    extract_imdb_id,
    nfo_needs_repair,
    parse_media_meta,
    tmdb_image_url,
)


class TestParseMediaMeta:
    def test_movie_basic(self):
        meta = parse_media_meta("SpiderMan Homecoming 2017 720p BluRay x264")
        assert meta["is_tv"] is False
        assert meta["year"] == 2017
        assert "2017" not in meta["clean_title"]
        assert meta["search_title"] == "spiderman homecoming"

    def test_movie_group_tag_stripped(self):
        meta = parse_media_meta("[WMR] Manjummel Boys 2023 Malayalam 1080p WEB-DL")
        assert meta["is_tv"] is False
        assert meta["year"] == 2023
        assert "malayalam" not in meta["search_title"]
        assert "wmr" not in meta["search_title"]

    def test_leading_release_group_handle_is_removed_before_identity_matching(self):
        meta = parse_media_meta("@WMR_SpiderMan.Homecoming.2017.720p.BluRay.x264.mp4")
        assert meta["year"] == 2017
        assert meta["search_title"] == "spiderman homecoming"
        assert "wmr" not in meta["search_title"]

    def test_tv_sxe(self):
        meta = parse_media_meta("Breaking Bad S01E01 720p x264")
        assert meta["is_tv"] is True
        assert meta["show_name"] == "Breaking Bad"
        assert meta["season"] == 1
        assert meta["episode"] == 1
        assert meta["title"] == "Breaking Bad - S01E01"

    def test_tv_season_episode_words(self):
        meta = parse_media_meta("Game of Thrones Season 1 Episode 5")
        assert meta["is_tv"] is True
        assert meta["show_name"] == "Game Of Thrones"
        assert meta["season"] == 1
        assert meta["episode"] == 5

    def test_tv_1x02(self):
        meta = parse_media_meta("Some Show 3x09 PROPER")
        assert meta["is_tv"] is True
        assert meta["season"] == 3
        assert meta["episode"] == 9

    def test_tv_year_kept_for_query(self):
        meta = parse_media_meta("The Last of Us 2023 S01E02")
        assert meta["is_tv"] is True
        assert meta["year"] == 2023

    def test_empty_fallback(self):
        meta = parse_media_meta(None)
        assert meta["title"] == "Unknown_Media"
        assert meta["is_tv"] is False

    def test_handle_stripped(self):
        meta = parse_media_meta("@ApexStore Movie Title 2021")
        assert "apexstore" not in meta["search_title"]
        assert meta["year"] == 2021

    def test_dots_become_spaces(self):
        meta = parse_media_meta("Spider-Man.Far.From.Home.2021.1080p.WEB-DL")
        assert meta["is_tv"] is False
        assert meta["year"] == 2021
        assert meta["clean_title"] == "Spider-Man Far From Home"
        assert meta["search_title"] == "spider-man far from home"

    def test_dot_run_and_release_group(self):
        meta = parse_media_meta("Spider-Man.No.Way.Home. . . . -Tinymkv.Xyz (2021).mkv")
        assert meta["is_tv"] is False
        assert meta["year"] == 2021
        assert meta["clean_title"] == "Spider-Man No Way Home"
        assert meta["search_title"] == "spider-man no way home"

    def test_trailing_dash_group_stripped(self):
        meta = parse_media_meta("Spider-Man Far From Home -Pah")
        assert meta["clean_title"] == "Spider-Man Far From Home"
        assert meta["search_title"] == "spider-man far from home"

    def test_hyphenated_series_part_kept(self):
        meta = parse_media_meta("Mission Impossible - Dead Reckoning Part 1 2023")
        assert meta["year"] == 2023
        assert "dead reckoning" in meta["search_title"]

    def test_dotted_release_group_stripped(self):
        meta = parse_media_meta("Premam.2015.Malayalam.1080p.x264.Pah")
        assert meta["is_tv"] is False
        assert meta["year"] == 2015
        assert meta["search_title"] == "premam"
        assert meta["clean_title"] == "Premam"

    def test_dotted_group_after_bluray_anchor(self):
        meta = parse_media_meta("Inception.2010.1080p.BluRay.x264.DTS-WiKi")
        assert meta["search_title"] == "inception"
        assert "wiki" not in meta["search_title"]

    def test_short_stop_tokens_removed(self):
        meta = parse_media_meta("Njan.Gandharvan.2023.1080p.Malayalam.HQ")
        assert meta["search_title"] == "njan gandharvan"
        assert "hq" not in meta["search_title"]

    def test_numeric_only_title_recovered(self):
        meta = parse_media_meta("2018.2018.Malayalam.1080p")
        assert meta["is_tv"] is False
        assert meta["title"] == "2018"
        assert meta["search_title"] == "2018"
        assert meta["year"] == 2018

    def test_numeric_title_with_release_year(self):
        meta = parse_media_meta("1917.2019.1080p.x264")
        assert meta["is_tv"] is False
        assert meta["title"] == "1917"
        assert meta["search_title"] == "1917"

    def test_ott_brand_audio_tail_stripped(self):
        meta = parse_media_meta("I Nobody Jiohotstar Dd+5.1 H (2026).mkv")
        assert meta["is_tv"] is False
        assert meta["year"] == 2026
        assert meta["search_title"] == "i nobody"

    def test_ott_brand_with_glued_channel_count(self):
        meta = parse_media_meta("I Nobody Jiohotstar Dd5.1 H (2026).mkv")
        assert meta["search_title"] == "i nobody"
        assert "jiohotstar" not in meta["search_title"]
        assert "dd5" not in meta["search_title"]
        assert "h" not in meta["search_title"]

    def test_series_part_number_kept(self):
        meta = parse_media_meta("K.G.F.Chapter.2.2022.1080p.mkv")
        assert meta["year"] == 2022
        assert meta["search_title"] == "k g f chapter 2"

    def test_channel_marker_stripped(self):
        meta = parse_media_meta(
            "Obsession.2025.1080p.10bit.WEBRip.6CH.x265.HEVC-PSA.mkv"
        )
        assert meta["is_tv"] is False
        assert meta["year"] == 2025
        assert meta["search_title"] == "obsession"
        assert "6ch" not in meta["search_title"]

    def test_channel_marker_lower_and_glued(self):
        meta = parse_media_meta("Thudakkam.2026.720p.WEB-DL.DD+5.1.5.1ch.mkv")
        assert meta["search_title"] == "thudakkam"
        assert "ch" not in meta["search_title"].split()

    def test_channel_marker_mid_run(self):
        meta = parse_media_meta("Movie.2024.1080p.6CH.HEVC.mkv")
        assert meta["search_title"] == "movie"

    def test_codec_ma_tail_stripped(self):
        meta = parse_media_meta("Interstellar (2014) 1080p DTS-MA Hindi.mkv")
        assert meta["year"] == 2014
        assert meta["search_title"] == "interstellar"

    def test_audio_channel_count_lang_stripped(self):
        meta = parse_media_meta("Vikram.2022.1080p.DDP5.1.Tamil.mkv")
        assert meta["year"] == 2022
        assert meta["search_title"] == "vikram"

    def test_ott_brand_malayalam_tail_stripped(self):
        meta = parse_media_meta("Bharath.Circus.2023.1080p.JioHotstar.DD+5.1.Malayalam.mkv")
        assert meta["year"] == 2023
        assert meta["search_title"] == "bharath circus"

    def test_ott_brand_short_isocode_tail_stripped(self):
        meta = parse_media_meta("Kaathal.The.Core.2023.1080p.Hotstar.Mal.mkv")
        assert meta["year"] == 2023
        assert meta["search_title"] == "kaathal the core"

    def test_webdl_audio_tail_stripped(self):
        meta = parse_media_meta("Manjummel.Boys.2024.720p.Hotstar.WEB-DL.x265.DDP5.1.Hindi.mkv")
        assert meta["year"] == 2024
        assert meta["search_title"] == "manjummel boys"

    def test_hdr_bit_audio_tail_stripped(self):
        meta = parse_media_meta("Deadpool.and.Wolverine.2024.1080p.JioHotstar.HDR.10bit.DD+.Mal.mkv")
        assert meta["year"] == 2024
        assert meta["search_title"] == "deadpool and wolverine"

    def test_search_candidates_present_and_ordered(self):
        meta = parse_media_meta("Bharath.Circus.2023.1080p.JioHotstar.DD+5.1.Malayalam.mkv")
        cands = meta["search_candidates"]
        assert cands
        assert cands[0] == "bharath circus"
        assert "bharath" in cands

    def test_kattalan_mmax_parsed_cleanly(self):
        raw = "[MS] Kattalan (2026) Malayalam 720p MMAX WEBDL AVC A.mkv"
        meta = parse_media_meta(raw)
        assert meta["title"] == "Kattalan"
        assert meta["clean_title"] == "Kattalan"
        assert meta["search_title"] == "kattalan"
        assert meta["year"] == 2026
        assert meta["language"] == "Malayalam"
        assert "kattalan" in meta["search_candidates"]
        assert "ms" not in meta["search_candidates"]
        assert "mmax" not in meta["search_candidates"]

    def test_iron_man_3_size_tag_stripped_cleanly(self):
        raw = "Iron Man 3 (2013) English BluRay 720p x264 780MB.mkv"
        meta = parse_media_meta(raw)
        assert meta["title"] == "Iron Man 3"
        assert meta["clean_title"] == "Iron Man 3"
        assert meta["search_title"] == "iron man 3"
        assert meta["year"] == 2013
        assert meta["language"] == "English"
        assert "iron man 3" in meta["search_candidates"]
        assert "780mb" not in meta["search_candidates"]
        assert "780" not in meta["search_candidates"]


class TestBestTmdbMatch:
    def test_exact_film(self):
        results = [
            {"id": 1, "title": "Spider-Man: Homecoming", "release_date": "2017-07-07"},
            {"id": 2, "title": "Spider-Man", "release_date": "2002-05-03"},
            {"id": 3, "title": "The Amazing Spider-Man", "release_date": "2012-06-23"},
        ]
        hit = best_tmdb_match(results, "spiderman homecoming", "movie", 2017)
        assert hit is not None
        assert hit["id"] == 1

    def test_iron_man_3_rejects_generation_iron(self):
        results = [
            {"id": 210879, "title": "Generation Iron", "release_date": "2013-09-20"},
            {"id": 68721, "title": "Iron Man 3", "release_date": "2013-04-18"},
        ]
        hit = best_tmdb_match(results, "iron man 3", "movie", 2013)
        assert hit is not None
        assert hit["id"] == 68721

        # Even if Generation Iron is the only hit returned, it must not match
        lone_gen = [{"id": 210879, "title": "Generation Iron", "release_date": "2013-09-20"}]
        assert best_tmdb_match(lone_gen, "iron man 3", "movie", 2013) is None

    def test_type_filter_excludes_wrong_media(self):
        results = [
            {"id": 1, "title": "Breaking Bad", "media_type": "tv", "first_air_date": "2008-01-20"},
        ]
        assert best_tmdb_match(results, "breaking bad", "movie") is None

    def test_no_match_returns_none(self):
        assert best_tmdb_match([{"id": 9, "title": "Avatar"}], "Interstellar", "movie") is None

    def test_year_boost(self):
        results = [
            {"id": 1, "title": "Dune Part Two", "release_date": "2024-02-27"},
            {"id": 2, "title": "Dune", "release_date": "2021-10-22"},
        ]
        hit = best_tmdb_match(results, "dune part two", "movie", 2024)
        assert hit["id"] == 1

    def test_empty_results(self):
        assert best_tmdb_match([], "Whatever", "movie") is None

    def test_numeric_title_matches(self):
        results = [
            {"id": 451048, "title": "2018", "release_date": "2023-05-05", "original_language": "ml"},
            {"id": 999, "title": "Player", "release_date": "2018-06-14"},
        ]
        hit = best_tmdb_match(results, "2018", "movie", 2018)
        assert hit is not None
        assert hit["id"] == 451048

    def test_expected_none_no_match(self):
        # A lone 4-digit token that matches nothing still returns None.
        assert best_tmdb_match(
            [{"id": 7, "title": "Dune", "release_date": "2021-10-22"}],
            "2099", "movie",
        ) is None

    def test_roman_numeral_suffix_matches_arabic(self):
        results = [
            {"id": 1, "title": "Rocky", "release_date": "1976-11-21"},
            {"id": 2, "title": "Rocky 3", "release_date": "1982-05-28"},
            {"id": 3, "title": "Rocky IV", "release_date": "1985-11-27"},
        ]
        # Filename used "Rocky III"; TMDB stores "Rocky 3".
        hit = best_tmdb_match(results, "rocky iii", "movie")
        assert hit["id"] == 2

    def test_roman_numeral_picks_arabic_with_exact_year(self):
        results = [
            {"id": 1, "title": "Rocky", "release_date": "1976-11-21"},
            {"id": 2, "title": "Rocky 3", "release_date": "1982-05-28"},
            {"id": 3, "title": "Rocky IV", "release_date": "1985-11-27"},
        ]
        hit = best_tmdb_match(results, "rocky iii", "movie", 1982)
        assert hit["id"] == 2

    def test_original_title_merge_rescues_non_latin_hit(self):
        # Primary title is non-Latin script (no latin tokens at all); only the
        # merged original_title can bridge the transliterated query.
        results = [
            {
                "id": 1,
                "title": "ആട്",
                "original_title": "Aadu",
                "release_date": "2015-01-01",
                "original_language": "ml",
            },
        ]
        hit = best_tmdb_match(results, "aadu", "movie")
        assert hit is not None and hit["id"] == 1

    def test_original_title_merge_for_transliterated_query(self):
        results = [
            {
                "id": 1,
                "title": "Aadu 3 Part 1",
                "original_title": "ആട് 3 പാർട്ട് 1",
                "release_date": "2025-01-23",
                "original_language": "ml",
            },
        ]
        hit = best_tmdb_match(results, "aadu 3 part 1", "movie", 2025)
        assert hit["id"] == 1

    def test_aadu_3_rejects_unnumbered_aadu_1(self):
        # When searching for Aadu 3, candidate Aadu (2015, no digit) MUST be rejected
        results = [
            {
                "id": 324976,
                "title": "Aadu",
                "release_date": "2015-02-06",
                "original_language": "ml",
            },
        ]
        assert best_tmdb_match(results, "aadu 3", "movie", language="ml") is None
        assert best_tmdb_match(results, "aadu 3 part 1", "movie", language="ml") is None

    def test_aadu_3_matches_aadu_3_part_1(self):
        results = [
            {
                "id": 324976,
                "title": "Aadu",
                "release_date": "2015-02-06",
                "original_language": "ml",
            },
            {
                "id": 489709,
                "title": "Aadu 2",
                "release_date": "2017-12-22",
                "original_language": "ml",
            },
            {
                "id": 999999,
                "title": "Aadu 3 Part 1",
                "release_date": "2026-03-19",
                "original_language": "ml",
            },
        ]
        hit = best_tmdb_match(results, "aadu 3", "movie", language="ml")
        assert hit is not None and hit["id"] == 999999
        hit_p1 = best_tmdb_match(results, "aadu 3 part 1", "movie", language="ml")
        assert hit_p1 is not None and hit_p1["id"] == 999999

    def test_year_proximity_beats_exact_mismatch(self):
        # Both films share title tokens; the ±1 year film should win over a
        # far-year film when the query year is close to one of them.
        results = [
            {"id": 1, "title": "Rocky", "release_date": "2001-03-15"},
            {"id": 2, "title": "Rocky", "release_date": "1985-11-27"},
        ]
        hit = best_tmdb_match(results, "rocky", "movie", 1984)
        assert hit["id"] == 2

    def test_language_affinity_bonus(self):
        results = [
            {"id": 1, "title": "Premam", "original_title": "പ്രേമം", "release_date": "2015-05-29", "original_language": "ml"},
            {"id": 2, "title": "Premam", "release_date": "2015-05-29", "original_language": "en"},
        ]
        hit = best_tmdb_match(results, "premam", "movie", 2015, language="ml")
        assert hit["id"] == 1

    def test_reject_false_positive_foreign_movie_with_partial_overlap(self):
        # A 2026 Tamil query for "Dhurandhar The Revenge" must NOT match an unrelated
        # Portuguese 1980 film "A Vingança de Dhurandhar"
        results = [
            {
                "id": 999,
                "title": "A Vingança de Dhurandhar",
                "original_title": "A Vingança de Dhurandhar",
                "release_date": "1980-05-12",
                "original_language": "pt",
            }
        ]
        hit = best_tmdb_match(results, "dhurandhar the revenge", "movie", year=2026, language="ta")
        assert hit is None

    def test_reject_large_year_gap(self):
        results = [
            {
                "id": 100,
                "title": "The Beast",
                "release_date": "1975-04-10",
                "original_language": "en",
            }
        ]
        hit = best_tmdb_match(results, "the beast", "movie", year=2024, language="en")
        assert hit is None

    def test_ambiguous_tied_candidates_rejected(self):
        # Two distinct movies with identical title tokens, language, and same release year
        # Must be rejected as ambiguous rather than picking one arbitrarily.
        results = [
            {"id": 101, "title": "Obsession", "release_date": "2024-05-01", "original_language": "en"},
            {"id": 102, "title": "Obsession", "release_date": "2024-05-01", "original_language": "en"},
        ]
        hit = best_tmdb_match(results, "obsession", "movie", year=2024, language="en")
        assert hit is None

    def test_ambiguous_candidates_resolved_by_exact_year(self):
        # Two movies with same title, but candidate 1 has exact year 2024 while candidate 2 is 2023
        results = [
            {"id": 101, "title": "Obsession", "release_date": "2024-05-01", "original_language": "en"},
            {"id": 102, "title": "Obsession", "release_date": "2023-05-01", "original_language": "en"},
        ]
        hit = best_tmdb_match(results, "obsession", "movie", year=2024, language="en")
        assert hit is not None
        assert hit["id"] == 101

    def test_duplicate_same_id_candidates_not_ambiguous(self):
        # Multiple hits with identical TMDB ID (e.g. from multi-page search or aliases)
        results = [
            {"id": 101, "title": "Obsession", "release_date": "2024-05-01", "original_language": "en"},
            {"id": 101, "title": "Obsession", "release_date": "2024-05-01", "original_language": "en"},
        ]
        hit = best_tmdb_match(results, "obsession", "movie", year=2024, language="en")
        assert hit is not None
        assert hit["id"] == 101


class TestExtractImdbId:
    def test_radarr_brace_imdb(self):
        assert extract_imdb_id("Movie.2024.720p.WEB-DL.{imdb-tt12345678}") == "tt12345678"

    def test_bare_tt(self):
        assert extract_imdb_id("Some Title (2023) [tt0987654]") == "tt0987654"

    def test_dotted_imdb_prefix(self):
        assert extract_imdb_id("Show.S01E01.1080p.imdb-tt1234567.x265") == "tt1234567"

    def test_none_and_empty(self):
        assert extract_imdb_id(None) is None
        assert extract_imdb_id("") is None
        assert extract_imdb_id("A completely normal title 2024.mkv") is None

    def test_case_insensitive(self):
        assert extract_imdb_id("Movie.Web-DL.TT1234567.mkv") == "tt1234567"

    def test_roman_normalized_in_search_candidates(self):
        meta = parse_media_meta("Mission Impossible III 2006 720p.mkv")
        assert "3" in " ".join(meta["search_candidates"])


class TestNfoBuilders:
    def test_movie_nfo(self):
        data = {
            "id": 315635,
            "title": "Spider-Man: Homecoming",
            "original_title": "Spider-Man: Homecoming",
            "release_date": "2017-07-07",
            "vote_average": 7.3,
            "overview": "A young Peter Parker fights crime.",
            "genres": [{"name": "Action"}, {"name": "Adventure"}],
            "production_countries": [{"iso_3166_1": "US"}],
        }
        nfo = build_movie_nfo(data)
        assert "<movie>" in nfo
        assert "<lockdata>true</lockdata>" in nfo
        assert "Spider-Man: Homecoming" in nfo
        assert "<uniqueid type=\"tmdb\">315635</uniqueid>" in nfo
        assert "<genre>Action</genre>" in nfo
        assert "<country>US</country>" in nfo
        assert "poster.jpg" in nfo

    def test_tvshow_nfo(self):
        data = {
            "id": 1396,
            "name": "Breaking Bad",
            "first_air_date": "2008-01-20",
            "vote_average": 8.9,
            "overview": "A high school chemistry teacher.",
            "genres": [{"name": "Drama"}, {"name": "Crime"}],
        }
        nfo = build_tvshow_nfo(data)
        assert "<tvshow>" in nfo
        assert "<lockdata>true</lockdata>" in nfo
        assert "<uniqueid type=\"tmdb\">1396</uniqueid>" in nfo
        assert "<genre>Drama</genre>" in nfo

    def test_episode_nfo(self):
        data = {
            "id": 621961,
            "name": "Pilot",
            "overview": "Walt turns fifty.",
            "season_number": 1,
            "episode_number": 1,
            "air_date": "2008-01-20",
        }
        nfo = build_episode_nfo(data)
        assert "<episodedetails>" in nfo
        assert "<lockdata>true</lockdata>" in nfo
        assert "<season>1</season>" in nfo
        assert "<episode>1</episode>" in nfo
        assert "<uniqueid type=\"tmdb\">621961</uniqueid>" in nfo

    def test_image_url(self):
        assert tmdb_image_url("/abc123.jpg") == "https://image.tmdb.org/t/p/w500/abc123.jpg"
        assert tmdb_image_url(None) is None


class TestNfoRepair:
    def test_healthy_nfo_is_not_flagged(self, tmp_path):
        nfo = tmp_path / "movie.nfo"
        nfo.write_text(
            '<movie><title>Mollywood Times</title><originaltitle>മോളിവുഡ് ടൈംസ്</originaltitle></movie>',
            encoding="utf-8",
        )
        assert nfo_needs_repair(str(nfo)) is False

    def test_malformed_originaltitle_is_flagged(self, tmp_path):
        nfo = tmp_path / "movie.nfo"
        nfo.write_text(
            '<movie><title>Mollywood Times</title><originaltitle>?????</originaltitle></movie>',
            encoding="utf-8",
        )
        assert nfo_needs_repair(str(nfo)) is True

    def test_malformed_tvshow_original_name_is_flagged(self, tmp_path):
        nfo = tmp_path / "tvshow.nfo"
        nfo.write_text(
            '<tvshow><name>Test</name><original_name>??????</original_name></tvshow>',
            encoding="utf-8",
        )
        assert nfo_needs_repair(str(nfo)) is True

    def test_foreign_portuguese_nfo_is_flagged(self, tmp_path):
        nfo = tmp_path / "movie.nfo"
        nfo.write_text(
            '<movie><title>A Vingança de Dhurandhar</title><originaltitle>A Vingança</originaltitle></movie>',
            encoding="utf-8",
        )
        assert nfo_needs_repair(str(nfo)) is True

    def test_dhurandhar_candidates_strip_tagline(self):
        meta = parse_media_meta("Dhurandhar The Revenge 2026 Tamil HQ HDRip - 1080p - x264 -.mkv")
        cands = meta["search_candidates"]
        assert "dhurandhar" in cands
        assert "dhurandhar the revenge" in cands

    def test_vishwanath_candidates_include_amp(self):
        meta = parse_media_meta("Vishwanath and Sons 2026 Tamil HQ HDRip.mkv")
        cands = meta["search_candidates"]
        assert "vishwanath & sons" in cands
        assert "vishwanath" in cands

    def test_patriot_movie_2026_malayalam_parsed_cleanly(self):
        meta = parse_media_meta("Patriot Movie 2026 malayalam.mkv")
        assert meta["clean_title"] == "Patriot"
        assert meta["search_title"] == "patriot"
        assert meta["year"] == 2026
        assert meta["language"] == "Malayalam"

    def test_patriot_2026_rejects_mel_gibson_2000(self):
        results = [
            {
                "id": 2108,
                "title": "The Patriot",
                "original_title": "The Patriot",
                "release_date": "2000-06-28",
                "original_language": "en",
            }
        ]
        # Must strictly reject 26-year gap and English movie for Malayalam release
        hit = best_tmdb_match(results, "patriot", "movie", 2026, language="ml")
        assert hit is None

    def test_patriot_no_year_rejects_generic_english_for_malayalam(self):
        results = [
            {
                "id": 2108,
                "title": "The Patriot",
                "original_title": "The Patriot",
                "release_date": "2000-06-28",
                "original_language": "en",
            }
        ]
        # Even without year, generic single-word English movie cannot match Malayalam
        hit = best_tmdb_match(results, "patriot", "movie", None, language="ml")
        assert hit is None


    def test_missing_file_not_flagged(self, tmp_path):
        assert nfo_needs_repair(str(tmp_path / "nope.nfo")) is False

    def test_generate_fallback_backdrop_hd_dimensions(self, tmp_path):
        import sys, types
        from unittest.mock import MagicMock
        pg = sys.modules.setdefault("pyrogram", types.ModuleType("pyrogram"))
        pg.__path__ = []
        pg.Client = MagicMock()
        pg.filters = MagicMock()
        for sub in ("raw", "raw.functions", "raw.types", "enums", "types"):
            mod = types.ModuleType(f"pyrogram.{sub}")
            mod.ChatType = MagicMock()
            mod.Message = MagicMock()
            sys.modules[f"pyrogram.{sub}"] = mod
        err_mod = types.ModuleType("pyrogram.errors")
        err_mod.FloodWait = type("FloodWait", (Exception,), {})
        err_mod.RPCError = type("RPCError", (Exception,), {})
        err_mod.AuthKeyDuplicated = type("AuthKeyDuplicated", (Exception,), {})
        sys.modules["pyrogram.errors"] = err_mod

        from tg_streamer import _generate_fallback_backdrop
        from PIL import Image

        poster = tmp_path / "poster.jpg"
        backdrop = tmp_path / "backdrop.jpg"

        # Create a mock poster
        img = Image.new("RGB", (600, 900), color=(180, 40, 40))
        img.save(str(poster), "JPEG")

        assert _generate_fallback_backdrop(str(poster), str(backdrop)) is True
        assert backdrop.exists()

        with Image.open(str(backdrop)) as out_img:
            assert out_img.size == (1920, 1080)

    def test_ms_rose_mr_cane_blacklisted(self):
        from tg_streamer import is_blacklisted
        assert is_blacklisted("Ms.Rose & Mr.Cane") is True
        assert is_blacklisted("Ms Rose & Mr Cane 2026") is True
        assert is_blacklisted("Ms. Rose & Mr. Cane (2026).strm") is True
        assert is_blacklisted("Mot test servicing") is True
        assert is_blacklisted("Obsession (2025).strm") is False

    def test_popular_movie_preferred_within_one_year_gap(self):
        candidates = [
            {
                "id": 999999,
                "title": "Obsession",
                "release_date": "2025-01-01",
                "popularity": 0.5,
                "vote_count": 0,
                "poster_path": None,
                "backdrop_path": None,
            },
            {
                "id": 1339713,
                "title": "Obsession",
                "release_date": "2026-05-15",
                "popularity": 65.0,
                "vote_count": 450,
                "poster_path": "/obsession_poster.jpg",
                "backdrop_path": "/obsession_backdrop.jpg",
                "overview": "A man uses a mystical charm to make his crush fall in love.",
            }
        ]
        match = best_tmdb_match(candidates, "obsession", "movie", year=2025)
        assert match is not None
        assert match["id"] == 1339713

    @pytest.mark.asyncio
    async def test_ai_disambiguate_candidates_no_credentials(self, monkeypatch):
        import sys, types
        from unittest.mock import MagicMock
        pg = sys.modules.setdefault("pyrogram", types.ModuleType("pyrogram"))
        pg.__path__ = []
        pg.Client = MagicMock()
        pg.filters = MagicMock()
        for sub in ("raw", "raw.functions", "raw.types", "enums", "types"):
            mod = types.ModuleType(f"pyrogram.{sub}")
            mod.ChatType = MagicMock()
            mod.Message = MagicMock()
            sys.modules[f"pyrogram.{sub}"] = mod
        err_mod = types.ModuleType("pyrogram.errors")
        err_mod.FloodWait = type("FloodWait", (Exception,), {})
        err_mod.RPCError = type("RPCError", (Exception,), {})
        err_mod.AuthKeyDuplicated = type("AuthKeyDuplicated", (Exception,), {})
        sys.modules["pyrogram.errors"] = err_mod

        import tg_streamer
        monkeypatch.setattr(tg_streamer, "OMNIROUTE_URL", "")
        monkeypatch.setattr(tg_streamer, "OMNIROUTE_KEY", "")

        candidates = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]
        res = await tg_streamer._ai_disambiguate_candidates("Some Movie", candidates)
        assert res is None

    @pytest.mark.asyncio
    async def test_ai_disambiguate_candidates_success_and_fallback(self, monkeypatch):
        import sys, types
        from unittest.mock import MagicMock, AsyncMock
        pg = sys.modules.setdefault("pyrogram", types.ModuleType("pyrogram"))
        pg.__path__ = []
        pg.Client = MagicMock()
        pg.filters = MagicMock()
        for sub in ("raw", "raw.functions", "raw.types", "enums", "types"):
            mod = types.ModuleType(f"pyrogram.{sub}")
            mod.ChatType = MagicMock()
            mod.Message = MagicMock()
            sys.modules[f"pyrogram.{sub}"] = mod
        err_mod = types.ModuleType("pyrogram.errors")
        err_mod.FloodWait = type("FloodWait", (Exception,), {})
        err_mod.RPCError = type("RPCError", (Exception,), {})
        err_mod.AuthKeyDuplicated = type("AuthKeyDuplicated", (Exception,), {})
        sys.modules["pyrogram.errors"] = err_mod

        import tg_streamer
        monkeypatch.setattr(tg_streamer, "OMNIROUTE_URL", "https://api.omniroute.ai/v1")
        monkeypatch.setattr(tg_streamer, "OMNIROUTE_KEY", "test_key")
        tg_streamer._AI_DISAMBIGUATE_CACHE.clear()

        candidates = [
            {"id": 100, "title": "Patriot", "release_date": "2000-06-28", "original_language": "en"},
            {"id": 200, "title": "Patriot", "release_date": "2026-01-15", "original_language": "ml"},
        ]

        class FakeResponse:
            def __init__(self, status, json_data):
                self.status = status
                self._json_data = json_data

            async def json(self):
                return self._json_data

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

            def post(self, url, **kwargs):
                return FakeResponse(200, {
                    "choices": [{
                        "message": {
                            "content": '```json\n{"tmdb_id": 200, "reason": "2026 Malayalam release"}\n```'
                        }
                    }]
                })

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        monkeypatch.setattr("aiohttp.ClientSession", FakeClientSession)

        # 1. Success case: returns matched candidate
        res = await tg_streamer._ai_disambiguate_candidates("Patriot 2026 Malayalam", candidates)
        assert res is not None
        assert res["id"] == 200

        # 2. Cached case
        cached_res = await tg_streamer._ai_disambiguate_candidates("Patriot 2026 Malayalam", candidates)
        assert cached_res is not None
        assert cached_res["id"] == 200

        # 3. Error case: 500 status code fails safe to None
        class FailingClientSession(FakeClientSession):
            def post(self, url, **kwargs):
                return FakeResponse(500, {})

        monkeypatch.setattr("aiohttp.ClientSession", FailingClientSession)
        fail_res = await tg_streamer._ai_disambiguate_candidates("Other Movie 2026", candidates)
        assert fail_res is None
