"""Pydantic contracts for events, participants, boards, and the HTTP API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from .board import SUPPORTED_SIZES

MAX_PROMPTS = 200
MAX_PROMPT_LEN = 240
MAX_NAME_LEN = 40
MAX_TITLE_LEN = 80
MAX_DESCRIPTION_LEN = 280

# Playful marks so a name in a suggestion list is recognisable at a glance in a
# crowded room. Assigned at join time and kept for the life of the event.
AVATARS = "🐝🌈🎸🌻🍄🦋🎡🍑🐙🌮🪩🦩🌵🎠🍋🐳🎈🌊🦜🍒🎺🌙🐬🥁🌺🦚🍍🎷🐝🛼"


def clean(value: str) -> str:
    """Collapse whitespace — the difference between 'Sam  R' and 'Sam R' is noise."""
    return " ".join(value.split())


def name_key(value: str) -> str:
    """Identity key for a person's name: whitespace- and case-insensitive."""
    return clean(value).casefold()


# --------------------------------------------------------------------------- #
# Domain
# --------------------------------------------------------------------------- #


class EventState(StrEnum):
    draft = "draft"
    live = "live"
    paused = "paused"
    ended = "ended"


class Square(BaseModel):
    prompt: str
    free: bool = False
    signed_name: str | None = None
    #: Set when the match was picked from the joined-participant list, so the host
    #: dashboard can tell registered players from write-in guests.
    signed_participant_id: str | None = None
    signed_at: datetime | None = None

    @property
    def filled(self) -> bool:
        return self.free or bool(self.signed_name)


class Player(BaseModel):
    id: str
    name: str
    avatar: str = "🎈"
    token: str = Field(exclude=True)
    squares: list[Square]
    joined_at: datetime
    #: Server-stamped. Cleared if a player undoes their way out of a bingo.
    bingo_at: datetime | None = None
    blackout_at: datetime | None = None


class Event(BaseModel):
    code: str
    title: str
    description: str = ""
    prompts: list[str]
    size: int
    free_centre: bool = True
    allow_repeat_signers: bool = False
    state: EventState = EventState.draft
    host_token: str = Field(exclude=True)
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    #: Snapshot taken when the event goes live. Late joiners deal from this, so every
    #: board in the room is drawn from the same pool.
    frozen_prompts: list[str] | None = None
    players: dict[str, Player] = Field(default_factory=dict)

    @property
    def pool(self) -> list[str]:
        return self.frozen_prompts if self.frozen_prompts is not None else self.prompts

    @property
    def accepts_joins(self) -> bool:
        return self.state in (EventState.live, EventState.paused)

    @property
    def accepts_edits(self) -> bool:
        return self.state is EventState.live


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


def _prompt_list(value: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in value:
        prompt = clean(raw)[:MAX_PROMPT_LEN]
        key = prompt.casefold()
        if prompt and key not in seen:
            seen.add(key)
            cleaned.append(prompt)
    if len(cleaned) > MAX_PROMPTS:
        raise ValueError(f"at most {MAX_PROMPTS} prompts")
    return cleaned


class CreateEventRequest(BaseModel):
    title: str = "Pregame Bingo"
    description: str = ""
    prompts: list[str] = Field(default_factory=list)
    size: int | None = Field(
        default=None,
        description=f"Board width, one of {SUPPORTED_SIZES}. Omit for 'largest that fits'.",
    )
    free_centre: bool = True
    allow_repeat_signers: bool = False

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return clean(value)[:MAX_TITLE_LEN] or "Pregame Bingo"

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return clean(value)[:MAX_DESCRIPTION_LEN]

    @field_validator("prompts")
    @classmethod
    def _prompts(cls, value: list[str]) -> list[str]:
        return _prompt_list(value)

    @field_validator("size")
    @classmethod
    def _size(cls, value: int | None) -> int | None:
        if value is not None and value not in SUPPORTED_SIZES:
            raise ValueError(f"size must be one of {SUPPORTED_SIZES}")
        return value


class UpdateEventRequest(BaseModel):
    """Host edits. Every field is optional; only what's sent is applied."""

    title: str | None = None
    description: str | None = None
    prompts: list[str] | None = None
    size: int | None = None
    free_centre: bool | None = None
    allow_repeat_signers: bool | None = None
    state: EventState | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return clean(value)[:MAX_TITLE_LEN] or "Pregame Bingo"

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return None if value is None else clean(value)[:MAX_DESCRIPTION_LEN]

    @field_validator("prompts")
    @classmethod
    def _prompts(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _prompt_list(value)

    @field_validator("size")
    @classmethod
    def _size(cls, value: int | None) -> int | None:
        if value is not None and value not in SUPPORTED_SIZES:
            raise ValueError(f"size must be one of {SUPPORTED_SIZES}")
        return value


class JoinRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = clean(value)[:MAX_NAME_LEN]
        if not name:
            raise ValueError("name is required")
        return name


class SignRequest(BaseModel):
    """`name=None` (or blank) clears the square."""

    name: str | None = None
    #: Present when the player tapped a joined participant instead of typing a name.
    participant_id: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return clean(value)[:MAX_NAME_LEN] or None


class RenamePlayerRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = clean(value)[:MAX_NAME_LEN]
        if not name:
            raise ValueError("name is required")
        return name


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class EventCreated(BaseModel):
    code: str
    host_token: str


class EventPublic(BaseModel):
    """What a guest is allowed to know before they join."""

    code: str
    title: str
    description: str
    size: int
    free_centre: bool
    state: EventState
    allow_repeat_signers: bool
    player_count: int
    squares_per_board: int


class Participant(BaseModel):
    id: str
    name: str
    avatar: str


class PlayerState(BaseModel):
    event: EventPublic
    player_id: str
    name: str
    avatar: str
    squares: list[Square]
    completed_lines: list[list[int]]
    has_bingo: bool
    blackout: bool
    filled_count: int
    bingo_at: datetime | None
    #: 1-based finishing position among players who have a bingo, by server time.
    place: int | None


class JoinResponse(PlayerState):
    player_token: str
    #: False when the name matched someone already in the room and we handed back
    #: their existing board rather than dealing a new one.
    created: bool


class HostPlayerRow(BaseModel):
    id: str
    name: str
    avatar: str
    filled_count: int
    line_count: int
    has_bingo: bool
    blackout: bool
    joined_at: datetime
    bingo_at: datetime | None
    place: int | None


class GuestName(BaseModel):
    """A write-in name that isn't a joined participant."""

    name: str
    count: int


class HostDashboard(BaseModel):
    event: EventPublic
    prompts: list[str]
    frozen: bool
    prompts_required: int
    players: list[HostPlayerRow]
    winners: list[HostPlayerRow]
    guest_names: list[GuestName]
    join_url_path: str


class BoardPreview(BaseModel):
    size: int
    squares: list[Square]
