"""Per-type metadata for a job (issue #14).

What the user has to tell the app about a download so that it can be filed into the right
library folder later (#16): a movie needs title and year, a series title and season (and an
episode, or none for a season pack), an audiobook author and title, a music release artist and
album. The result is the `metadata` JSON object stored in `jobs.metadata` (#9).

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
)
from pydantic_core import PydanticCustomError

MAX_NAME_LENGTH = 200
MIN_YEAR = 1870  # the earliest films; anything older is a typo
MAX_NUMBER = 9999  # seasons are sometimes years ("Formula 1", season 2024)


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


def _no_bool(value: Any) -> Any:
    """True/False would pass as 1/0 for an int field; a flag is never a year or a season."""
    if isinstance(value, bool):
        raise PydanticCustomError("int_not_bool", "Input should be a number, not true/false")
    return value


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
    season: Number
    # None = a season pack (all episodes of the season in one torrent).
    episode: Number | None = None


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
    """The JSON object text for `jobs.metadata`: no nulls (a season pack has no `episode` key)."""
    return json.dumps(
        metadata.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def metadata_from_json(
    text: str,
) -> MovieMetadata | SeriesMetadata | AudiobookMetadata | MusicMetadata:
    """Read back what `metadata_to_json` stored."""
    return _adapter.validate_json(text)
