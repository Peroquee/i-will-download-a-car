"""app/ytp/metadata.py: required fields per type, field-level errors, safe names."""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError
from starlette.testclient import TestClient

from ytp.clients.qbittorrent import CATEGORIES
from ytp.db import Database
from ytp.metadata import (
    MAX_NAME_LENGTH,
    METADATA_TYPES,
    MIN_YEAR,
    AudiobookMetadata,
    Metadata,
    MovieMetadata,
    MusicMetadata,
    SeasonSpecError,
    SeriesMetadata,
    metadata_from_json,
    metadata_to_json,
    parse_metadata,
    parse_seasons,
    safe_path_component,
)

THIS_YEAR = datetime.now(UTC).year

MOVIE = {"type": "movies", "title": "Big Buck Bunny", "year": 2008}
SERIES = {"type": "series", "title": "Tatort", "season": 3, "episode": 7}
AUDIOBOOK = {"type": "audiobooks", "author": "Michael Ende", "title": "Momo"}
MUSIC = {"type": "music", "artist": "Kraftwerk", "album": "Autobahn"}


def errors(data) -> list[tuple[str, str]]:
    """[(field path, error type)] of a failed validation."""
    with pytest.raises(ValidationError) as caught:
        parse_metadata(data)
    return [(".".join(str(p) for p in e["loc"]), e["type"]) for e in caught.value.errors()]


# --- the four types ----------------------


def test_a_movie():
    movie = parse_metadata(MOVIE)
    assert isinstance(movie, MovieMetadata)
    assert (movie.title, movie.year) == ("Big Buck Bunny", 2008)


def test_an_episode_and_a_season_pack():
    episode = parse_metadata(SERIES)
    assert isinstance(episode, SeriesMetadata)
    assert (episode.seasons, episode.episode) == ([3], 7)
    pack = parse_metadata({k: v for k, v in SERIES.items() if k != "episode"})
    assert pack.episode is None
    assert parse_metadata({**SERIES, "episode": None}).episode is None


def test_an_audiobook():
    book = parse_metadata(AUDIOBOOK)
    assert isinstance(book, AudiobookMetadata)
    assert (book.author, book.title) == ("Michael Ende", "Momo")


def test_a_music_release():
    music = parse_metadata(MUSIC)
    assert isinstance(music, MusicMetadata)
    assert (music.artist, music.album) == ("Kraftwerk", "Autobahn")


def test_the_type_names_are_the_qbittorrent_categories():
    assert set(METADATA_TYPES) == set(CATEGORIES)
    for model, name in [
        (MovieMetadata, "movies"),
        (SeriesMetadata, "series"),
        (AudiobookMetadata, "audiobooks"),
        (MusicMetadata, "music"),
    ]:
        assert model.model_fields["type"].annotation.__args__ == (name,)


# --- required fields: every missing one is its own error ----------------------


@pytest.mark.parametrize(
    ("data", "missing"),
    [
        (MOVIE, ["title", "year"]),
        (SERIES, ["title", "season"]),
        (AUDIOBOOK, ["author", "title"]),
        (MUSIC, ["artist", "album"]),
    ],
)
def test_each_missing_required_field_is_reported_by_name(data, missing):
    broken = {"type": data["type"]}
    found = errors(broken)
    assert sorted(found) == sorted((f"{data['type']}.{name}", "missing") for name in missing)


@pytest.mark.parametrize("data", [MOVIE, SERIES, AUDIOBOOK, MUSIC])
def test_dropping_one_field_names_exactly_that_field(data):
    required = [k for k in data if k not in {"type", "episode"}]
    for name in required:
        broken = {k: v for k, v in data.items() if k != name}
        assert errors(broken) == [(f"{data['type']}.{name}", "missing")]


def test_several_problems_are_all_reported_at_once():
    found = errors({"type": "movies", "title": "  ", "year": 1066})
    assert sorted(found) == [("movies.title", "name_empty"), ("movies.year", "year_out_of_range")]


# --- the discriminator ----------------------


def test_a_missing_type_is_an_error():
    assert errors({"title": "x", "year": 2000}) == [("", "union_tag_not_found")]


@pytest.mark.parametrize("value", ["movie", "Movies", "youtube", "", None, 7])
def test_an_unknown_type_is_an_error_naming_the_choices(value):
    with pytest.raises(ValidationError) as caught:
        parse_metadata({"type": value, "title": "x", "year": 2000})
    (error,) = caught.value.errors()
    assert error["type"] == "union_tag_invalid"
    for name in METADATA_TYPES:
        assert f"'{name}'" in error["msg"]


@pytest.mark.parametrize("data", [None, [], "movies", 5])
def test_something_that_is_not_an_object_is_an_error(data):
    with pytest.raises(ValidationError):
        parse_metadata(data)


# --- stray fields ----------------------


def test_a_field_from_another_type_is_refused_not_dropped():
    assert errors({**MOVIE, "season": 1}) == [("movies.season", "extra_forbidden")]
    assert errors({**AUDIOBOOK, "year": 1973}) == [("audiobooks.year", "extra_forbidden")]


# --- year ----------------------


@pytest.mark.parametrize("year", [MIN_YEAR, 1999, THIS_YEAR, THIS_YEAR + 1])
def test_years_in_range(year):
    assert parse_metadata({**MOVIE, "year": year}).year == year


@pytest.mark.parametrize("year", [MIN_YEAR - 1, 0, -2000, THIS_YEAR + 2, 20080])
def test_years_out_of_range(year):
    assert errors({**MOVIE, "year": year}) == [("movies.year", "year_out_of_range")]


@pytest.mark.parametrize(
    ("year", "kind"),
    [("abc", "int_parsing"), (None, "int_type"), (2008.5, "int_from_float"), ([], "int_type")],
)
def test_year_that_is_not_a_whole_number(year, kind):
    assert errors({**MOVIE, "year": year}) == [("movies.year", kind)]


def test_a_year_typed_into_a_form_field_is_accepted_as_text():
    assert parse_metadata({**MOVIE, "year": "2008"}).year == 2008
    assert parse_metadata({**MOVIE, "year": " 2008 "}).year == 2008


def test_true_is_not_a_year_or_a_season():
    assert errors({**MOVIE, "year": True}) == [("movies.year", "int_not_bool")]
    assert errors({**SERIES, "season": True}) == [("series.season", "int_not_bool")]
    assert errors({**SERIES, "episode": False}) == [("series.episode", "int_not_bool")]


# --- season and episode ----------------------


@pytest.mark.parametrize("value", [1, 2, 99, 2024, 9999])
def test_season_and_episode_in_range(value):
    parsed = parse_metadata({**SERIES, "season": value, "episode": value})
    assert (parsed.seasons, parsed.episode) == ([value], value)


@pytest.mark.parametrize("value", [0, -1, 10000])
def test_season_and_episode_out_of_range(value):
    assert errors({**SERIES, "season": value}) == [("series.season", _range(value))]
    assert errors({**SERIES, "episode": value}) == [("series.episode", _range(value))]


def _range(value: int) -> str:
    return "greater_than_equal" if value < 1 else "less_than_equal"


def test_episode_zero_is_not_a_season_pack():
    """A season pack has no episode (None); 0 is a mistake, not 'all of them'."""
    assert errors({**SERIES, "episode": 0}) == [("series.episode", "greater_than_equal")]


# --- several seasons in one job (#52) ----------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2", [2]),
        (" 2 ", [2]),
        ("1-3", [1, 2, 3]),
        ("1,2,5", [1, 2, 5]),
        ("1-3,5", [1, 2, 3, 5]),
        (" 1 - 3 , 5 ", [1, 2, 3, 5]),
        ("3,1,3", [1, 3]),
        ("0", [0]),
        ("999", [999]),
        ("1000", [1000]),
        ("2024", [2024]),
        ("2020-2024", [2020, 2021, 2022, 2023, 2024]),
        ("1-50", list(range(1, 51))),
    ],
)
def test_a_season_spec_parses_to_a_sorted_unique_list(text, expected):
    assert parse_seasons(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "x",
        "1-",
        "-2",
        "3-1",
        "1..3",
        "1,",
        ",2",
        "1 2",
        "10000",
        "1-9999",
        "1-51",
        "0-50",
        "-1",
        "1.5",
        "1;2",
        "1" + chr(0x2013) + "3",  # an en dash is not the ASCII hyphen the spec uses
        None,
        5,
    ],
)
def test_a_bad_season_spec_is_refused(text):
    with pytest.raises(SeasonSpecError):
        parse_seasons(text)


def test_the_season_field_accepts_the_new_text_and_lists():
    # No episode: several seasons plus one episode is refused (see the test below).
    assert parse_metadata({"type": "series", "title": "T", "season": "1-3,5"}).seasons == [
        1,
        2,
        3,
        5,
    ]
    assert parse_metadata({"type": "series", "title": "T", "season": [5, 1, 3]}).seasons == [
        1,
        3,
        5,
    ]
    assert parse_metadata({"type": "series", "title": "T", "seasons": [2, 1]}).seasons == [
        1,
        2,
    ]


@pytest.mark.parametrize(
    "value", ["", "x", "10000", "1-9999", "0-50", [], ["1"], [10000], [1, True]]
)
def test_a_bad_season_field_is_a_season_invalid(value):
    assert errors({**SERIES, "season": value}) == [("series.season", "season_invalid")]


def test_an_episode_with_several_seasons_is_refused():
    for season in ("1-3", "1,2,5", [1, 2]):
        assert errors({"type": "series", "title": "T", "season": season, "episode": 5}) == [
            ("series.episode", "episode_multi_season")
        ]


def test_an_episode_with_one_season_still_works():
    for season in ("3", 3, [3]):
        parsed = parse_metadata({"type": "series", "title": "T", "season": season, "episode": 7})
        assert (parsed.seasons, parsed.episode) == ([3], 7)


def test_a_multi_season_job_stores_the_list_and_no_single_season():
    text = metadata_to_json(parse_metadata({"type": "series", "title": "T", "season": "1-3"}))
    assert json.loads(text)["seasons"] == [1, 2, 3]
    assert "season" not in json.loads(text)


def test_old_and_new_stored_season_forms_read_the_same():
    old = metadata_from_json('{"type":"series","title":"T","season":3}')
    new = metadata_from_json('{"type":"series","title":"T","season":3,"seasons":[3]}')
    multi = metadata_from_json('{"type":"series","title":"T","seasons":[1,2]}')
    assert old.seasons == new.seasons == [3]
    assert multi.seasons == [1, 2]
    assert "season" not in json.loads(
        metadata_to_json(parse_metadata({"type": "series", "title": "T", "season": "1-2"}))
    )


# --- names: one function decides ----------------------


def test_names_are_stripped():
    assert parse_metadata({**MUSIC, "artist": "  Kraftwerk \n"}).artist == "Kraftwerk"


def test_umlauts_and_other_scripts_pass_and_are_normalised_to_nfc():
    decomposed = unicodedata.normalize("NFD", "Größe Hörbücher")
    assert decomposed != "Größe Hörbücher"
    assert safe_path_component(decomposed) == "Größe Hörbücher"
    assert safe_path_component("Ünïcödé 東京 Привет") == "Ünïcödé 東京 Привет"


@pytest.mark.parametrize(
    "name",
    [
        "Star Wars: Episode IV",
        "...And Justice for All",
        "Wait... What?",
        'He said "no"',
        "AC/DC".replace("/", "-"),
        "a.b",
        ".hidden",
        "-dash",
    ],
)
def test_ordinary_titles_are_not_refused(name):
    assert safe_path_component(name) == name


@pytest.mark.security
@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("", "name_empty"),
        ("   ", "name_empty"),
        ("\t\n", "name_empty"),
        ("a" * (MAX_NAME_LENGTH + 1), "name_too_long"),
        ("../etc/passwd", "name_path_separator"),
        ("a/b", "name_path_separator"),
        ("a\\b", "name_path_separator"),
        ("/absolute", "name_path_separator"),
        ("C:\\Windows", "name_path_separator"),
        ("..", "name_dots_only"),
        (".", "name_dots_only"),
        ("....", "name_dots_only"),
        ("two\nlines", "name_control_character"),
        ("tab\there", "name_control_character"),
        ("nul\x00byte", "name_control_character"),
        ("bell\x07", "name_control_character"),
        ("del\x7f", "name_control_character"),
        ("next\x85line", "name_control_character"),
        ("sep\u2028arator", "name_control_character"),
        ("para\u2029graph", "name_control_character"),
        ("bidi\u202eexe.gpj", "name_control_character"),  # right-to-left override
        ("zero\u200bwidth", "name_control_character"),
    ],
)
def test_names_that_must_never_become_a_folder(name, kind):
    with pytest.raises(ValidationError) as caught:
        parse_metadata({**MOVIE, "title": name})
    assert [(e["loc"][-1], e["type"]) for e in caught.value.errors()] == [("title", kind)]


def test_the_length_limit_counts_characters_after_stripping():
    assert safe_path_component("  " + "a" * MAX_NAME_LENGTH + "  ") == "a" * MAX_NAME_LENGTH


def test_every_text_field_of_every_type_uses_the_same_rules():
    for data, fields in [
        (MOVIE, ["title"]),
        (SERIES, ["title"]),
        (AUDIOBOOK, ["author", "title"]),
        (MUSIC, ["artist", "album"]),
    ]:
        for field in fields:
            assert errors({**data, field: "a/b"}) == [
                (f"{data['type']}.{field}", "name_path_separator")
            ]
            assert errors({**data, field: 5}) == [(f"{data['type']}.{field}", "string_type")]


def test_error_messages_are_readable():
    with pytest.raises(ValidationError) as caught:
        parse_metadata({**MOVIE, "title": "a" * 300, "year": 1500})
    messages = {e["loc"][-1]: e["msg"] for e in caught.value.errors()}
    assert messages["title"] == "Must be at most 200 characters, got 300"
    assert messages["year"] == f"Year must be between {MIN_YEAR} and {THIS_YEAR + 1}"


# --- JSON for jobs.metadata ----------------------


@pytest.mark.parametrize("data", [MOVIE, SERIES, AUDIOBOOK, MUSIC])
def test_json_round_trip(data):
    text = metadata_to_json(parse_metadata(data))
    assert isinstance(json.loads(text), dict)
    assert metadata_from_json(text) == parse_metadata(data)


def test_a_season_pack_has_no_episode_key_and_umlauts_stay_readable():
    pack = parse_metadata({"type": "series", "title": "Über uns", "season": 2})
    text = metadata_to_json(pack)
    assert text == '{"season":2,"seasons":[2],"title":"Über uns","type":"series"}'
    assert metadata_from_json(text).episode is None


def test_the_json_fits_the_jobs_table(tmp_path):
    """jobs.metadata must be a JSON object (CHECK json_valid + json_type = 'object')."""
    database = Database.open(tmp_path / "app.db")
    try:
        for index, data in enumerate([MOVIE, SERIES, AUDIOBOOK, MUSIC]):
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO jobs (type, external_id, metadata) VALUES ('magnet', ?, ?)",
                    (f"id{index}", metadata_to_json(parse_metadata(data))),
                )
        stored = [r["metadata"] for r in database.query("SELECT metadata FROM jobs ORDER BY id")]
        assert [metadata_from_json(s).type for s in stored] == list(METADATA_TYPES_IN_ORDER)
    finally:
        database.close()


METADATA_TYPES_IN_ORDER = ("movies", "series", "audiobooks", "music")


# --- FastAPI's 422 ----------------------


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.post("/echo")
    async def echo(body: Metadata) -> dict:
        return body.model_dump(mode="json")

    return TestClient(app)


def test_a_valid_body_is_accepted_through_fastapi(client):
    response = client.post("/echo", json=MOVIE)
    assert response.status_code == 200
    assert response.json() == MOVIE


def test_a_bad_body_gets_field_level_422_errors(client):
    response = client.post("/echo", json={"type": "movies", "title": "", "year": 1066})
    assert response.status_code == 422
    found = {(tuple(e["loc"]), e["type"]) for e in response.json()["detail"]}
    assert found == {
        (("body", "movies", "title"), "name_empty"),
        (("body", "movies", "year"), "year_out_of_range"),
    }
    assert all(e["msg"] for e in response.json()["detail"])


def test_a_missing_season_is_a_422_naming_the_field(client):
    response = client.post("/echo", json={"type": "series", "title": "Tatort"})
    assert response.status_code == 422
    assert [e["loc"] for e in response.json()["detail"]] == [["body", "series", "season"]]


def test_an_unknown_type_is_a_422(client):
    response = client.post("/echo", json={"type": "podcast", "title": "x"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "union_tag_invalid"


def test_the_adapter_is_the_documented_union():
    assert TypeAdapter(Metadata).validate_python(MUSIC) == parse_metadata(MUSIC)
