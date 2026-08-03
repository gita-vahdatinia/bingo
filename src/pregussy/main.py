"""FastAPI app: host an event, share a link, fill a board, watch the room light up."""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, snapshot
from .board import completed_lines, prompts_required
from .models import (
    BoardPreview,
    CreateEventRequest,
    Event,
    EventCreated,
    EventPublic,
    GuestName,
    HostDashboard,
    HostPlayerRow,
    JoinRequest,
    JoinResponse,
    Participant,
    Player,
    PlayerState,
    RenamePlayerRequest,
    SignRequest,
    UpdateEventRequest,
)
from .store import (
    DuplicateSigner,
    EventNotFound,
    EventStore,
    NotEnoughPrompts,
    PlayerNotFound,
    SelfSign,
    SquareOutOfRange,
    WrongState,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Not in Python's default table; without it browsers ignore the PWA manifest.
mimetypes.add_type("application/manifest+json", ".webmanifest")

store = EventStore(snapshot=snapshot.from_environment())
app = FastAPI(title="Pregussy Bingo", version=__version__)

CodeParam = Annotated[str, PathParam(min_length=1, max_length=12)]
PlayerToken = Annotated[str | None, Header(alias="X-Player-Token")]
HostToken = Annotated[str | None, Header(alias="X-Host-Token")]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _public(event: Event) -> EventPublic:
    return EventPublic(
        code=event.code,
        title=event.title,
        description=event.description,
        size=event.size,
        free_centre=event.free_centre,
        state=event.state,
        allow_repeat_signers=event.allow_repeat_signers,
        player_count=len(event.players),
        squares_per_board=event.size * event.size,
    )


def _player_state(event: Event, player: Player) -> PlayerState:
    filled = [square.filled for square in player.squares]
    lines = completed_lines(filled, event.size)
    return PlayerState(
        event=_public(event),
        player_id=player.id,
        name=player.name,
        avatar=player.avatar,
        squares=player.squares,
        completed_lines=lines,
        has_bingo=bool(lines),
        blackout=all(filled),
        filled_count=sum(filled),
        bingo_at=player.bingo_at,
        place=store.places(event).get(player.id),
    )


def _rows(event: Event) -> list[HostPlayerRow]:
    places = store.places(event)
    rows = []
    for player in event.players.values():
        filled = [square.filled for square in player.squares]
        lines = completed_lines(filled, event.size)
        rows.append(
            HostPlayerRow(
                id=player.id,
                name=player.name,
                avatar=player.avatar,
                filled_count=sum(filled),
                line_count=len(lines),
                has_bingo=bool(lines),
                blackout=all(filled),
                joined_at=player.joined_at,
                bingo_at=player.bingo_at,
                place=places.get(player.id),
            )
        )
    rows.sort(key=lambda r: (-r.line_count, -r.filled_count, r.joined_at))
    return rows


def load_event(code: CodeParam) -> Event:
    try:
        return store.get_event(code)
    except EventNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


EventDep = Annotated[Event, Depends(load_event)]


def load_player(event: EventDep, x_player_token: PlayerToken = None) -> Player:
    player = store.player_by_token(event.code, x_player_token)
    if player is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "We lost your board. Enter your name to pick it back up."
        )
    return player


def require_host(event: EventDep, x_host_token: HostToken = None) -> Event:
    if not store.is_host(event.code, x_host_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Host link required.")
    return event


PlayerDep = Annotated[Player, Depends(load_player)]
HostEventDep = Annotated[Event, Depends(require_host)]


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.post("/api/events", response_model=EventCreated, status_code=status.HTTP_201_CREATED)
def create_event(request: CreateEventRequest) -> EventCreated:
    event = store.create_event(request)
    return EventCreated(code=event.code, host_token=event.host_token)


@app.get("/api/events/{code}", response_model=EventPublic)
def get_event(event: EventDep) -> EventPublic:
    return _public(event)


@app.patch("/api/events/{code}", response_model=HostDashboard)
def update_event(event: HostEventDep, request: UpdateEventRequest) -> HostDashboard:
    try:
        store.update_event(event.code, request)
    except NotEnoughPrompts as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except WrongState as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return dashboard(event)


@app.delete("/api/events/{code}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(event: HostEventDep) -> None:
    store.delete_event(event.code)


@app.get("/api/events/{code}/dashboard", response_model=HostDashboard)
def dashboard(event: HostEventDep) -> HostDashboard:
    rows = _rows(event)
    winners = sorted(
        (row for row in rows if row.place is not None),
        key=lambda row: row.place or 0,
    )
    return HostDashboard(
        event=_public(event),
        prompts=event.prompts,
        frozen=event.frozen_prompts is not None,
        prompts_required=prompts_required(event.size, event.free_centre),
        players=rows,
        winners=winners,
        guest_names=[
            GuestName(name=name, count=count)
            for name, count in store.guest_names(event).most_common(30)
        ],
        join_url_path=f"/e/{event.code}",
    )


@app.get("/api/events/{code}/preview", response_model=BoardPreview)
def preview_board(event: HostEventDep) -> BoardPreview:
    try:
        squares = store.deal(event)
    except NotEnoughPrompts as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return BoardPreview(size=event.size, squares=squares)


# --------------------------------------------------------------------------- #
# Participants
# --------------------------------------------------------------------------- #


@app.get("/api/events/{code}/participants", response_model=list[Participant])
def participants(event: EventDep) -> list[Participant]:
    """Everyone who has joined. Powers name suggestions on the way in and on a square."""
    people = sorted(event.players.values(), key=lambda p: p.name.casefold())
    return [Participant(id=p.id, name=p.name, avatar=p.avatar) for p in people]


@app.post(
    "/api/events/{code}/players", response_model=JoinResponse, status_code=status.HTTP_201_CREATED
)
def join_event(event: EventDep, request: JoinRequest) -> JoinResponse:
    try:
        player, created = store.join(event.code, request.name)
    except WrongState as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except NotEnoughPrompts as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    state = _player_state(event, player)
    return JoinResponse(**state.model_dump(), player_token=player.token, created=created)


@app.patch("/api/events/{code}/players/{player_id}", response_model=HostDashboard)
def rename_player(
    event: HostEventDep, player_id: str, request: RenamePlayerRequest
) -> HostDashboard:
    try:
        store.rename_player(event.code, player_id, request.name)
    except PlayerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return dashboard(event)


@app.delete("/api/events/{code}/players/{player_id}", response_model=HostDashboard)
def remove_player(event: HostEventDep, player_id: str) -> HostDashboard:
    try:
        store.remove_player(event.code, player_id)
    except PlayerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return dashboard(event)


# --------------------------------------------------------------------------- #
# Board
# --------------------------------------------------------------------------- #


@app.get("/api/events/{code}/me", response_model=PlayerState)
def my_board(event: EventDep, player: PlayerDep) -> PlayerState:
    return _player_state(event, player)


@app.put("/api/events/{code}/me/squares/{index}", response_model=PlayerState)
def sign_square(
    event: EventDep,
    player: PlayerDep,
    index: Annotated[int, PathParam(ge=0, le=24)],
    request: SignRequest,
) -> PlayerState:
    try:
        store.sign_square(event.code, player, index, request.name, request.participant_id)
    except (DuplicateSigner, SelfSign, WrongState) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PlayerNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except SquareOutOfRange as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _player_state(event, player)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


def _page(name: str) -> FileResponse:
    return FileResponse(STATIC_DIR / name)


@app.get("/", include_in_schema=False)
def home_page() -> FileResponse:
    return _page("index.html")


@app.get("/e/{code}", include_in_schema=False)
def event_page(code: CodeParam) -> FileResponse:
    return _page("event.html")


@app.get("/h/{code}", include_in_schema=False)
def host_page(code: CodeParam) -> FileResponse:
    return _page("host.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
