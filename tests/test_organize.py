"""Unit tests for apex_stream.organize — language detection & Jellyfin layout."""

import os

import pytest

from apex_stream.organize import (
    DEFAULT_LANGUAGE,
    detect_language,
    language_code,
    match_subtitle_media,
    movie_target_dir,
    safe_folder,
    subtitle_is_document,
    subtitle_lang_hint,
    tv_target_dir,
)


class TestDetectLanguage:
    def test_english_default_no_tag(self):
        assert detect_language("SpiderMan Homecoming 2017 720p BluRay x264") == "English"

    def test_explicit_english(self):
        assert detect_language("Dune Part Two English 4K HDR") == "English"

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("@WMR Manjummel Boys 2023 Malayalam 1080p WEB-DL", "Malayalam"),
            ("Spadikam 1995 Malayalam 720p x264", "Malayalam"),
            ("Pathaan 2023 Hindi 1080p BluRay x265", "Hindi"),
            ("Vikram 2022 Tamil 1080p", "Tamil"),
            ("Pushpa 2021 Telugu 720p", "Telugu"),
            ("K.G.F. Chapter 2 Kannada 1080p", "Kannada"),
            ("Squid Game Korean 1080p Netflix", "Korean"),
            ("Contratiempo Spanish 1080p", "Spanish"),
            ("Amelie French 2001 720p", "French"),
        ],
    )
    def test_regional_languages(self, filename, expected):
        assert detect_language(filename) == expected

    def test_short_code_within_word_is_ignored(self):
        # "mal" inside "normal" must not match; "tamil" in "Tamilnadu" must not
        # false-positive via the short "tam" token either.
        assert detect_language("normal release 2020") == "English"
        assert detect_language("Malcolm X 1992 BluRay") == "English"

    def test_none_and_empty(self):
        assert detect_language(None) == DEFAULT_LANGUAGE
        assert detect_language("") == DEFAULT_LANGUAGE


class TestLanguageCode:
    def test_known(self):
        assert language_code("English") == "eng"
        assert language_code("Malayalam") == "mal"
        assert language_code("Hindi") == "hin"

    def test_unknown(self):
        assert language_code("Klingon") == ""


class TestFolders:
    def test_movie_target_dir(self):
        assert movie_target_dir("/m/Movies", "English") == os.path.join("/m/Movies", "English")
        assert movie_target_dir("/m/Movies", "Malayalam") == os.path.join("/m/Movies", "Malayalam")

    def test_tv_target_dir(self):
        assert tv_target_dir("/m/TV", "Hindi", "Pathaan", 1) == os.path.join("/m/TV", "Hindi", "Pathaan", "Season 01")
        assert tv_target_dir("/m/TV", "English", "Breaking Bad", 2) == os.path.join("/m/TV", "English", "Breaking Bad", "Season 02")

    def test_tv_no_season_defaults_one(self):
        assert tv_target_dir("/m/TV", "Malayalam", "Manjummel Boys", None).endswith("Season 01")

    def test_safe_folder(self):
        assert safe_folder("Spider-Man: No Way Home (2021)") == "Spider-Man No Way Home (2021)"
        assert safe_folder(None) == "Unknown"
        assert safe_folder("") == "Unknown"


class TestSubtitles:
    def test_is_document(self):
        assert subtitle_is_document("Movie.mal.srt")
        assert subtitle_is_document("Show - S01E01.ass")
        assert subtitle_is_document("Movie.vtt")
        assert not subtitle_is_document("Movie.mp4")
        assert not subtitle_is_document(None)

    def test_lang_hint(self):
        assert subtitle_lang_hint("SpiderMan.Homecoming.eng") == "English"
        assert subtitle_lang_hint("Manjummel.Boys.mal") == "Malayalam"
        assert subtitle_lang_hint("Movie.hin") == "Hindi"
        assert subtitle_lang_hint("NoCode.xyz") == DEFAULT_LANGUAGE

    def test_match_subtitle_media(self):
        entries = {
            "1": {"title": "SpiderMan Homecoming 2017 720p BluRay x264"},
            "2": {"title": "Dune Part Two English 4K"},
        }
        hit = match_subtitle_media("SpiderMan.Homecoming.eng", entries)
        assert hit is not None
        msg_id, entry, score = hit
        assert msg_id == "1"
        assert entry["title"].startswith("SpiderMan")
        assert score > 0.5

    def test_match_none(self):
        entries = {"1": {"title": "Dune Part Two 4K"}}
        assert match_subtitle_media("Avatar 2009", entries) is None

    def test_match_ignores_release_tags_and_years(self):
        entries = {"7": {"title": "Spadikam 1995 Malayalam 720p x264"}}
        hit = match_subtitle_media("Spadikam.1995.mal", entries)
        assert hit and hit[0] == "7"