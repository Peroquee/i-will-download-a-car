"""Per-type metadata for a job (issue #14).

What the user has to tell the app about a download so that it can be filed into the right
library folder later (#16): a movie needs title and year, a series title and one or more seasons
(and an episode, or none for a season pack), an audiobook author and title, a music release artist
and album. The result is the `metadata` JSON object stored in `jobs.metadata` (#9).

`type` is the discriminator and uses the same names as the qBittorrent categories
(movies, series, audiobooks, music; a test keeps them equal), so one value picks the
category, the library folder and the schema.

    parse_metadata({"type": "movies", "title": "Big Buck Bunny", "year": 2008})

raises pydantic's `ValidationError` with one error per bad field (`loc`, `type`, `msg`), which
FastAPI turns into a 422 response as it is. The text values will become folder names, so they
all go through `safe_path_component`, the one place that decides what is allowed in a name;
#16 uses the same function for anything else it builds a path from.

pydantic comes with FastAPI, so this adds no dependency.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)
from pydantic_core import PydanticCustomError

MAX_NAME_LENGTH = 200
MIN_YEAR = 1870  # the earliest films; anything older is a typo
MAX_NUMBER = 9999  # seasons are sometimes years ("Formula 1", season 2024)
MIN_SEASON = 0
MAX_SEASON = 999
MAX_SEASONS = 50
MAX_SEASON_SPEC_LENGTH = 1000


class SeasonSpecError(ValueError):
    """The text is not a season, a range or a list of seasons."""


def safe_path_component(value: str) -> str:
    """A user-supplied text that may become ONE folder or file name; returns it cleaned.

    Strips surrounding whitespace and normalises to Unicode NFC (an "ö" typed as o + combining
    diaeresis would otherwise be a different folder than the same word typed as one character).
    Refuses, with a machine-readable error type:

    * empty after stripping (`name_empty`) and longer than 200 characters (`name_too_long`),
    * `/` and `\\`, which would leave the folder (`name_path_separator`),
    * control, format and line-separator characters: newline, tab, NUL, bidi overrides and
      the like (`name_control_character`),
    * a name made only of dots, `.` `..` `...` (`name_dots_only`).

    Deliberately allowed: a colon, quotes, a leading dot ("...And Justice for All" is a film),
    umlauts and all other letters. Characters that are awkward on some file systems are
    #16's business when it builds the real path; nothing is built here.
    """
    if not isinstance(value, str):
        raise PydanticCustomError("name_not_text", "Input should be a text")
    name = unicodedata.normalize("NFC", value.strip())
    if not name:
        raise PydanticCustomError("name_empty", "Must not be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise PydanticCustomError(
            "name_too_long",
            "Must be at most {max} characters, got {got}",
            {"max": MAX_NAME_LENGTH, "got": len(name)},
        )
    if "/" in name or "\\" in name:
        raise PydanticCustomError("name_path_separator", "Must not contain / or \\")
    if any(
        unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in {"Zl", "Zp"}
        for ch in name
    ):
        raise PydanticCustomError(
            "name_control_character", "Must not contain control or invisible characters"
        )
    if set(name) == {"."}:
        raise PydanticCustomError("name_dots_only", "Must not consist of dots only")
    return name


def sanitize_name(text: str, fallback: str) -> str:
    """Make a name that YouTube (or any other outside source) gave us usable as ONE folder or file
    name: `/` and `\\` become `-`, control and invisible characters become spaces, runs of spaces
    collapse, the length is cut. If `safe_path_component` still refuses it (empty, only dots),
    `fallback` is returned. The user's own input is NOT passed through here: it is checked and
    refused instead, never silently changed."""
    cleaned = "".join(
        " "
        if unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in {"Zl", "Zp"}
        else ch
        for ch in str(text).replace("/", "-").replace("\\", "-")
    )
    try:
        return safe_path_component(" ".join(cleaned.split())[:MAX_NAME_LENGTH])
    except PydanticCustomError:
        return fallback


def _no_bool(value: Any) -> Any:
    """True/False would pass as 1/0 for an int field; a flag is never a year or a season."""
    if isinstance(value, bool):
        raise PydanticCustomError("int_not_bool", "Input should be a number, not true/false")
    return value


_SEASON_PART = re.compile(r"([0-9]+)\s*(?:-\s*([0-9]+))?")


def parse_seasons(text: str) -> list[int]:
    """Parse `2`, `1-3`, `1,2,5` or `1-3,5` into a sorted list of unique seasons (#52).

    Spaces around a comma or a hyphen are allowed. Every season must be 0..999 and a list may
    contain at most 50 seasons. Raises `SeasonSpecError` with a text a person can read.
    """
    if not isinstance(text, str):
        raise SeasonSpecError("a season must be text")
    spec = text.strip()
    if not spec:
        raise SeasonSpecError("enter a season like 2, a range like 1-3 or a list like 1,2,5")
    if len(spec) > MAX_SEASON_SPEC_LENGTH:
        raise SeasonSpecError("the season list is too long")
    seasons: set[int] = set()
    for part in spec.split(","):
        token = part.strip()
        if not token:
            raise SeasonSpecError("the season list has an empty entry")
        match = _SEASON_PART.fullmatch(token)
        if match is None:
            raise SeasonSpecError(f"{token!r} is not a season like 2 or a range like 1-3")
        start = int(match[1])
        end = int(match[2]) if match[2] is not None else start
        if start > end:
            raise SeasonSpecError(f"the season range {token!r} runs backwards")
        for season in range(start, end + 1):
            if season > MAX_SEASON:
                raise SeasonSpecError(f"season {season} is outside 0-{MAX_SEASON}")
            seasons.add(season)
            if len(seasons) > MAX_SEASONS:
                raise SeasonSpecError(f"at most {MAX_SEASONS} seasons")
    return sorted(seasons)


def _parse_season_field(value: Any) -> list[int]:
    """The `season` field: an old integer, the new spec text, or the stored `seasons` list."""
    if isinstance(value, bool):
        raise PydanticCustomError("int_not_bool", "Input should be a number, not true/false")
    if isinstance(value, int):
        # Old jobs and old clients send one integer; keep its old 1..MAX_NUMBER range (#52
        # changes the new text form, not the meaning of stored data).
        if value < 1:
            raise PydanticCustomError(
                "greater_than_equal", "Input should be greater than or equal to 1"
            )
        if value > MAX_NUMBER:
            raise PydanticCustomError(
                "less_than_equal",
                "Input should be less than or equal to {max}",
                {"max": MAX_NUMBER},
            )
        return [value]
    if isinstance(value, str):
        try:
            return parse_seasons(value)
        except SeasonSpecError as exc:
            raise PydanticCustomError("season_invalid", "{reason}", {"reason": str(exc)}) from exc
    if isinstance(value, (list, tuple)):
        seasons: set[int] = set()
        for entry in value:
            if isinstance(entry, bool) or not isinstance(entry, int):
                raise PydanticCustomError("season_invalid", "Every season must be a whole number")
            if not MIN_SEASON <= entry <= MAX_SEASON:
                raise PydanticCustomError(
                    "season_invalid",
                    f"Season {entry} is outside {MIN_SEASON}-{MAX_SEASON}",
                )
            seasons.add(entry)
            if len(seasons) > MAX_SEASONS:
                raise PydanticCustomError("season_invalid", f"At most {MAX_SEASONS} seasons")
        if not seasons:
            raise PydanticCustomError("season_invalid", "Enter at least one season")
        return sorted(seasons)
    raise PydanticCustomError("season_invalid", "A season must be a number, a range or a list")


def _check_year(year: int) -> int:
    latest = datetime.now(UTC).year + 1  # a film announced for next year
    if not MIN_YEAR <= year <= latest:
        raise PydanticCustomError(
            "year_out_of_range",
            "Year must be between {low} and {high}",
            {"low": MIN_YEAR, "high": latest},
        )
    return year


Name = Annotated[str, AfterValidator(safe_path_component)]
Year = Annotated[int, BeforeValidator(_no_bool), AfterValidator(_check_year)]
Number = Annotated[int, BeforeValidator(_no_bool), Field(ge=1, le=MAX_NUMBER)]


class _Metadata(BaseModel):
    # A stray field is an error, not silently dropped: the stored JSON stays exactly the schema.
    model_config = ConfigDict(extra="forbid")


class MovieMetadata(_Metadata):
    type: Literal["movies"]
    title: Name
    year: Year


class SeriesMetadata(_Metadata):
    type: Literal["series"]
    title: Name
    # One or more seasons (#52). Input may use the old `season` integer, the new spec text
    # (`1-3,5`) or the stored `seasons` list; it is a sorted list here. `season` keeps the field
    # name so old errors and clients still point at the same field.
    season: Annotated[list[int], BeforeValidator(_parse_season_field)]
    # None = a season pack (all episodes of the season in one torrent).
    episode: Number | None = None

    @model_validator(mode="before")
    @classmethod
    def _stored_seasons(cls, data: Any) -> Any:
        """Read the `seasons` list that new jobs store, under the `season` field."""
        if isinstance(data, dict) and "seasons" in data:
            data = dict(data)
            data["season"] = data.pop("seasons")
        return data

    @property
    def seasons(self) -> list[int]:
        return self.season


class AudiobookMetadata(_Metadata):
    type: Literal["audiobooks"]
    author: Name
    title: Name


class MusicMetadata(_Metadata):
    type: Literal["music"]
    artist: Name
    album: Name


Metadata = Annotated[
    MovieMetadata | SeriesMetadata | AudiobookMetadata | MusicMetadata,
    Field(discriminator="type"),
]
METADATA_TYPES = ("movies", "series", "audiobooks", "music")

_adapter: TypeAdapter[Metadata] = TypeAdapter(Metadata)


def parse_metadata(data: Any) -> MovieMetadata | SeriesMetadata | AudiobookMetadata | MusicMetadata:
    """Validate a dict (for example a request body); raises pydantic.ValidationError."""
    return _adapter.validate_python(data)


def metadata_to_json(metadata: _Metadata) -> str:
    """The JSON object text for `jobs.metadata`: no nulls (a season pack has no `episode` key).

    A series always stores `seasons` (sorted, unique) and, for exactly one season, also the old
    `season` integer, so existing readers and jobs keep working (#52).
    """
    data = metadata.model_dump(mode="json", exclude_none=True)
    if isinstance(metadata, SeriesMetadata):
        seasons = data.pop("season")
        data["seasons"] = seasons
        if len(seasons) == 1:
            data["season"] = seasons[0]
    return json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def metadata_from_json(
    text: str,
) -> MovieMetadata | SeriesMetadata | AudiobookMetadata | MusicMetadata:
    """Read back what `metadata_to_json` stored."""
    return _adapter.validate_json(text)
