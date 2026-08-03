# Lineup Bingo

Social bingo for a pregame. You write the prompts, share one link, and everyone standing around
with a drink gets their own randomly dealt board. No accounts, no app, no QR codes.

Built phone-first: the whole game happens one-handed on a 390px screen in a crowded room.

## Quick start

```bash
uv sync
uv run uvicorn lineup.main:app --host 0.0.0.0 --port 8000 --reload
```

Open <http://localhost:8000>, write some prompts, and you land on a host dashboard at `/h/{code}`.
Hit **Start the game** and share the `/e/{code}` link.

For a real event, bind to your LAN IP and hand out `http://<your-ip>:8000/e/{code}`, or put it
behind any HTTPS reverse proxy.

## How it plays

**Host** creates an event (draft), edits the prompt pool, picks a board size, previews a sample
board, then starts the game. From then on they watch a live dashboard: who joined, how far along
everyone is, who got bingo and in what order. They can pause, resume, end, rename someone, or
remove someone.

**Guests** open the link, type a name, and get a board. Tap a square, pick the person who matches
from the list of people already playing — or write in anyone who hasn't joined — and it's signed.
A full row, column, or diagonal is a bingo, with confetti and a finishing place. The game keeps
going afterwards; a full card is a blackout.

### Event states

| State    | Guests can join | Guests can edit squares | Host can edit the board |
| -------- | --------------- | ----------------------- | ----------------------- |
| `draft`  | no              | no                      | yes                     |
| `live`   | yes             | yes                     | no                      |
| `paused` | yes             | no                      | no                      |
| `ended`  | no              | no (board stays visible) | no                     |

Starting the game **freezes the prompt pool**. Existing boards are never reshuffled, and everyone
who joins late is dealt from that same frozen pool, so the whole room is playing one game.

### Board sizes

| Board | Prompts needed (free centre) | Prompts needed (no free centre) |
| ----- | ---------------------------- | ------------------------------- |
| 3 × 3 | 8                            | 9                               |
| 4 × 4 | 16                           | 16 (no middle square exists)    |
| 5 × 5 | 24                           | 25                              |

Supplying more prompts than the minimum is the point: with 40 prompts on a 5×5, boards overlap
far less and people have to talk to more of the room. Aim for statements true of roughly 10–30%
of the room.

### The rules that make people mingle

- **One square per person** by default. The server rejects a repeat with a nudge to go find
  someone new. Hosts can turn this off for small rooms.
- **You can't use yourself.** Tapping your own name in the list is blocked. Typing your own name
  by hand is allowed but warns you first — names aren't identities here, and there might genuinely
  be two Sams.
- Name comparison ignores case and extra spaces, so `grace`, `Grace`, and `  Grace ` are one
  person.
- Bingo times are stamped **by the server**, and finishing places come from those stamps. Undo
  your way out of a bingo and you give the place back up.

## Identity, honestly

There are no accounts. A player token lands in `localStorage` at join time and reopens the same
board on the same phone. If that's gone, typing a name that already exists **hands you that
board** — which is also how "tap your name to get back in" works.

That means anyone at the party can claim anyone's board. That is a deliberate trade for a game
with no sign-up, and it is why nothing private should ever go on a board. If you need real
identity, this is the wrong tool.

## API

Player actions use `X-Player-Token`; host actions use `X-Host-Token`.

| Method   | Path                                  | Auth   | Purpose                             |
| -------- | ------------------------------------- | ------ | ----------------------------------- |
| `POST`   | `/api/events`                         | —      | Create a draft event                |
| `GET`    | `/api/events/{code}`                  | —      | Public event info                   |
| `PATCH`  | `/api/events/{code}`                  | host   | Settings, prompts, state transitions |
| `DELETE` | `/api/events/{code}`                  | host   | Delete the event                    |
| `GET`    | `/api/events/{code}/dashboard`        | host   | Players, winners, write-in names    |
| `GET`    | `/api/events/{code}/preview`          | host   | Deal a sample board                 |
| `GET`    | `/api/events/{code}/participants`     | —      | Everyone who joined (name suggestions) |
| `POST`   | `/api/events/{code}/players`          | —      | Join, or reclaim a board by name    |
| `PATCH`  | `/api/events/{code}/players/{id}`     | host   | Rename a participant                |
| `DELETE` | `/api/events/{code}/players/{id}`     | host   | Remove a participant                |
| `GET`    | `/api/events/{code}/me`               | player | Current board + bingo state         |
| `PUT`    | `/api/events/{code}/me/squares/{i}`   | player | Sign or clear a square              |

Interactive docs at `/docs`.

## Mobile notes

The phone is the product surface, so these are load-bearing rather than nice-to-have:

- Every control is ≥44px tall and every input is 16px, so iOS Safari never zooms on focus.
- The sticky board header is **height-clamped** — the title and player name ellipsize to one line
  each. Without this an 80-character event name ate a third of an iPhone SE screen for the whole
  game.
- On short viewports (landscape phones) the header compacts and the board is sized off viewport
  height, with a floor that keeps squares tappable.
- Boards fit without horizontal scrolling from 320px up; squares stay ≥56px at 320px.
- The match sheet lifts above the on-screen keyboard via `visualViewport`.
- Confetti and sheet animations are skipped under `prefers-reduced-motion`.
- Light and dark palettes both clear WCAG AA on every text pair.

Verified against 320/360/375/390/430px portrait and 740×360 landscape.

## Tests

```bash
uv run pytest --cov=lineup --cov-report=term-missing
uv run ruff check .
```

## Ops notes

- State lives in memory, snapshotted to `data/events.json` (override with `LINEUP_DATA`) after
  every mutation, so a restart mid-event doesn't wipe boards.
- Single-process only — the store uses a thread lock, not a shared backend. Run one uvicorn
  worker. If you need more, swap `EventStore` for Redis; the interface is small on purpose.
- Events are never garbage-collected. For a long-lived deployment, add a sweep for events older
  than a day.
- The host dashboard polls every 5s while an event is live. Guests re-sync when their tab becomes
  visible rather than polling, to be kind to phone batteries.
- `LOG_LEVEL` controls verbosity. Names typed onto squares are user content and land in the
  snapshot file — delete it after an event if that matters to you.

## Rollback

Nothing persistent beyond `data/events.json`. To reset: stop the server, `rm -rf data/`, restart.
