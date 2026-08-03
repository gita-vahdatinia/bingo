"""In-memory event store with a JSON snapshot, so a restart doesn't kill a live party."""

from __future__ import annotations

import logging
import random
import secrets
import threading
from collections import Counter
from datetime import UTC, datetime

from .board import (
    completed_lines,
    free_index,
    largest_size_for,
    prompts_required,
    supports_free_centre,
)
from .models import (
    AVATARS,
    CreateEventRequest,
    Event,
    EventState,
    Player,
    Square,
    UpdateEventRequest,
    clean,
    name_key,
)
from .snapshot import NullSnapshot, Snapshot

logger = logging.getLogger(__name__)

# Ambiguous glyphs (I/O/0/1) removed so a code read off a phone screen is unambiguous.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 5

#: Which state you may move to from where. Ending is always allowed; nothing follows it.
TRANSITIONS: dict[EventState, set[EventState]] = {
    EventState.draft: {EventState.live, EventState.ended},
    EventState.live: {EventState.paused, EventState.ended},
    EventState.paused: {EventState.live, EventState.ended},
    EventState.ended: set(),
}


class StoreError(Exception):
    """Base class for expected, user-facing store failures."""


class EventNotFound(StoreError):
    pass


class NotEnoughPrompts(StoreError):
    pass


class DuplicateSigner(StoreError):
    pass


class SelfSign(StoreError):
    pass


class SquareOutOfRange(StoreError):
    pass


class WrongState(StoreError):
    pass


class PlayerNotFound(StoreError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


class EventStore:
    """Thread-safe store. One process, one lock — plenty for a room full of phones."""

    def __init__(self, snapshot: Snapshot | None = None) -> None:
        self._events: dict[str, Event] = {}
        self._lock = threading.RLock()
        self._rng = random.SystemRandom()
        self._snapshot: Snapshot = snapshot or NullSnapshot()
        self._load()

    # --------------------------------------------------------------- events --

    def create_event(self, request: CreateEventRequest) -> Event:
        free_centre = request.free_centre
        size = request.size or largest_size_for(len(request.prompts), free_centre) or 5
        with self._lock:
            code = self._new_code()
            event = Event(
                code=code,
                title=request.title,
                description=request.description,
                prompts=request.prompts,
                size=size,
                free_centre=free_centre,
                allow_repeat_signers=request.allow_repeat_signers,
                state=EventState.draft,
                host_token=secrets.token_urlsafe(24),
                created_at=_now(),
            )
            self._events[code] = event
            self._save()
        logger.info("event_created", extra={"code": code, "size": size})
        return event

    def get_event(self, code: str) -> Event:
        with self._lock:
            event = self._events.get(_normalise_code(code))
        if event is None:
            raise EventNotFound(f"No event with code {code!r}. Check the link and try again.")
        return event

    def is_host(self, code: str, host_token: str | None) -> bool:
        if not host_token:
            return False
        return secrets.compare_digest(self.get_event(code).host_token, host_token)

    def delete_event(self, code: str) -> None:
        with self._lock:
            self._events.pop(_normalise_code(code), None)
            self._save()

    def update_event(self, code: str, request: UpdateEventRequest) -> Event:
        with self._lock:
            event = self.get_event(code)
            data = request.model_dump(exclude_none=True)
            target = data.pop("state", None)

            # Boards are dealt from the pool the moment an event goes live, so the
            # shape of the game is frozen from then on.
            locked = {"prompts", "size", "free_centre"}
            if event.state is not EventState.draft and locked & data.keys():
                raise WrongState(
                    "The board is locked once the game starts — prompts and size can only "
                    "change while the event is a draft."
                )

            for field, value in data.items():
                setattr(event, field, value)

            if target is not None and target is not event.state:
                self._transition(event, EventState(target))

            self._validate_shape(event)
            self._save()
        return event

    def _transition(self, event: Event, target: EventState) -> None:
        if target not in TRANSITIONS[event.state]:
            raise WrongState(f"Can't go from {event.state.value} to {target.value}.")
        if target is EventState.live and event.state is EventState.draft:
            self._require_prompts(event)
            # Freeze the pool so late arrivals play the same game as everyone else.
            event.frozen_prompts = list(event.prompts)
            event.started_at = _now()
        if target is EventState.ended:
            event.ended_at = _now()
        event.state = target
        logger.info("event_state", extra={"code": event.code, "state": target.value})

    def _validate_shape(self, event: Event) -> None:
        if event.state is EventState.draft and event.prompts:
            self._require_prompts(event)

    def _require_prompts(self, event: Event) -> None:
        needed = prompts_required(event.size, event.free_centre)
        if len(event.prompts) < needed:
            raise NotEnoughPrompts(
                f"A {event.size}×{event.size} board needs {needed} prompts; "
                f"you have {len(event.prompts)}."
            )

    # -------------------------------------------------------------- players --

    def join(self, code: str, name: str) -> tuple[Player, bool]:
        """Join, or reclaim the board already sitting under this name.

        Names are the identity here: tapping your own name on a new phone hands you
        back your board. That is deliberate for a party on the honour system, and it
        is why we never put anything private on a board.
        """
        with self._lock:
            event = self.get_event(code)
            if not event.accepts_joins:
                raise WrongState(
                    "This event hasn't opened yet." if event.state is EventState.draft
                    else "This event has ended."
                )
            key = name_key(name)
            existing = next((p for p in event.players.values() if name_key(p.name) == key), None)
            if existing is not None:
                return existing, False

            player = Player(
                id=secrets.token_hex(8),
                name=name,
                avatar=self._rng.choice(AVATARS),
                token=secrets.token_urlsafe(24),
                squares=self.deal(event),
                joined_at=_now(),
            )
            event.players[player.id] = player
            self._save()
        logger.info("player_joined", extra={"code": event.code, "player": player.id})
        return player, True

    def player_by_token(self, code: str, token: str | None) -> Player | None:
        if not token:
            return None
        for player in self.get_event(code).players.values():
            if secrets.compare_digest(player.token, token):
                return player
        return None

    def rename_player(self, code: str, player_id: str, name: str) -> Player:
        with self._lock:
            event = self.get_event(code)
            player = event.players.get(player_id)
            if player is None:
                raise PlayerNotFound("That player is not in this event.")
            player.name = name
            self._save()
        return player

    def remove_player(self, code: str, player_id: str) -> None:
        with self._lock:
            event = self.get_event(code)
            if event.players.pop(player_id, None) is None:
                raise PlayerNotFound("That player is not in this event.")
            self._save()

    # ------------------------------------------------------------- squares --

    def sign_square(
        self,
        code: str,
        player: Player,
        index: int,
        name: str | None,
        participant_id: str | None,
    ) -> Player:
        with self._lock:
            event = self.get_event(code)
            if not event.accepts_edits:
                raise WrongState(
                    "The game is paused — hang tight." if event.state is EventState.paused
                    else "This event has ended, so boards are final."
                )
            if not 0 <= index < len(player.squares):
                raise SquareOutOfRange(f"Square {index} is not on this board.")
            square = player.squares[index]
            if square.free:
                raise SquareOutOfRange("The free square is already yours.")

            if name is None:
                square.signed_name = None
                square.signed_participant_id = None
                square.signed_at = None
            else:
                # A tapped participant is authoritative: use their canonical name.
                if participant_id is not None:
                    if participant_id == player.id:
                        raise SelfSign("That's you! Go find someone else.")
                    match = event.players.get(participant_id)
                    if match is None:
                        raise PlayerNotFound("That player has left the event.")
                    name = match.name
                self._check_unique(event, player, index, name)
                square.signed_name = name
                square.signed_participant_id = participant_id
                square.signed_at = _now()

            self._restamp(event, player)
            self._save()
        return player

    def _check_unique(self, event: Event, player: Player, index: int, name: str) -> None:
        if event.allow_repeat_signers:
            return
        key = name_key(name)
        clash = next(
            (
                other
                for i, other in enumerate(player.squares)
                if i != index and other.signed_name and name_key(other.signed_name) == key
            ),
            None,
        )
        if clash is not None:
            raise DuplicateSigner(
                f"{name} is already on “{clash.prompt}”. One square per person — "
                "go talk to someone new."
            )

    def _restamp(self, event: Event, player: Player) -> None:
        """Server owns bingo timing; undoing a square gives the place back up."""
        filled = [square.filled for square in player.squares]
        has_bingo = bool(completed_lines(filled, event.size))
        blackout = all(filled)
        if has_bingo and player.bingo_at is None:
            player.bingo_at = _now()
            logger.info("bingo", extra={"code": event.code, "player": player.id})
        elif not has_bingo:
            player.bingo_at = None
        if blackout and player.blackout_at is None:
            player.blackout_at = _now()
        elif not blackout:
            player.blackout_at = None

    # -------------------------------------------------------------- boards --

    def deal(self, event: Event) -> list[Square]:
        """A fresh random board, drawn from the event's (possibly frozen) pool."""
        pool = event.pool
        needed = prompts_required(event.size, event.free_centre)
        if len(pool) < needed:
            raise NotEnoughPrompts(
                f"A {event.size}×{event.size} board needs {needed} prompts; the pool has "
                f"{len(pool)}."
            )
        squares = [Square(prompt=prompt) for prompt in self._rng.sample(pool, needed)]
        slot = free_index(event.size, event.free_centre)
        if slot is not None:
            squares.insert(slot, Square(prompt="Free space", free=True))
        return squares

    def places(self, event: Event) -> dict[str, int]:
        """Player id → finishing position, by the server's bingo timestamps."""
        winners = sorted(
            (p for p in event.players.values() if p.bingo_at is not None),
            key=lambda p: (p.bingo_at, p.joined_at),  # type: ignore[arg-type,return-value]
        )
        return {player.id: rank for rank, player in enumerate(winners, start=1)}

    def guest_names(self, event: Event) -> Counter[str]:
        """Write-in names that never joined, so the host can see who's in the room."""
        joined = {name_key(p.name) for p in event.players.values()}
        tally: Counter[str] = Counter()
        for player in event.players.values():
            for square in player.squares:
                if (
                    square.signed_name
                    and square.signed_participant_id is None
                    and name_key(square.signed_name) not in joined
                ):
                    tally[clean(square.signed_name)] += 1
        return tally

    # -------------------------------------------------------------- helpers --

    def _new_code(self) -> str:
        for _ in range(1000):
            code = "".join(self._rng.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self._events:
                return code
        raise StoreError("Could not allocate an event code; too many active events.")

    # ---------------------------------------------------------- persistence --

    def _save(self) -> None:
        # Tokens are excluded from serialisation (they never go over the wire in a
        # response), so they are re-attached explicitly for the snapshot.
        payload = {
            code: event.model_dump(mode="json")
            | {
                "host_token": event.host_token,
                "players": {
                    pid: player.model_dump(mode="json") | {"token": player.token}
                    for pid, player in event.players.items()
                },
            }
            for code, event in self._events.items()
        }
        self._snapshot.save(payload)

    def _load(self) -> None:
        raw = self._snapshot.load()
        if not raw:
            return
        try:
            self._events = {code: Event.model_validate(data) for code, data in raw.items()}
            logger.info("snapshot_loaded", extra={"events": len(self._events)})
        except ValueError as exc:
            logger.warning("snapshot_invalid, starting empty: %s", exc)
            self._events = {}


def _normalise_code(code: str) -> str:
    return code.strip().upper().replace(" ", "")


__all__ = [
    "DuplicateSigner",
    "EventNotFound",
    "EventStore",
    "NotEnoughPrompts",
    "PlayerNotFound",
    "SelfSign",
    "SquareOutOfRange",
    "StoreError",
    "WrongState",
    "supports_free_centre",
]
