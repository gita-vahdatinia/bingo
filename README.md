# Pregussy Bingo

Social bingo for a pregame. You write the prompts, share one link, and everyone standing around
with a drink gets their own randomly dealt board. No accounts, no app, no QR codes.

Built phone-first: the whole game happens one-handed on a 390px screen in a crowded room.

## Quick start

```bash
uv sync
uv run uvicorn pregussy.main:app --host 0.0.0.0 --port 8000 --reload
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
uv run pytest --cov=pregussy --cov-report=term-missing
uv run ruff check .
```

## Deploying

Push to GitHub, then in Render: **New → Blueprint**, pick the repo. `render.yaml` provisions a
free web service plus a free Postgres and wires them together. Every push to `main` redeploys.

Both the service and the database are on Render's free plans, which come with two catches worth
knowing before a party:

- **The service sleeps after ~15 minutes of no traffic**, and a cold start takes roughly a minute.
  Any board that's open on screen pings `/api/health` once a minute to hold it awake, so a live
  game keeps itself up — but the *first* guest of the night may wait through a cold start. Open
  the event link yourself a minute before you send it round.
- **A free Postgres is deleted 30 days after it's created** (plus a 14-day grace period). No game
  lasts that long, so the data doesn't matter — but the app will stop persisting once the database
  is gone. It degrades rather than failing: it keeps serving and logs `running in memory only`.
  Create a new free database and update `DATABASE_URL` to restore durability.

`plan: starter` on the service removes the sleep; a paid database removes the expiry.

### Configuration

| Variable       | Default             | Purpose                                              |
| -------------- | ------------------- | ---------------------------------------------------- |
| `DATABASE_URL` | unset               | Postgres to snapshot into. Unset falls back to a file. |
| `PREGUSSY_DATA`  | `data/events.json`  | Snapshot file path, used only when there's no `DATABASE_URL`. |
| `PORT`         | `8000`              | Read by the container's start command.                |
| `LOG_LEVEL`    | `INFO`              | Log verbosity.                                        |

Run it as a container anywhere:

```bash
docker build -t pregussy-bingo .
docker run -p 8000:8000 -e DATABASE_URL=postgres://... pregussy-bingo
```

## Ops notes

- State lives in memory and the whole store is snapshotted after every mutation — to Postgres when
  `DATABASE_URL` is set, otherwise to `data/events.json`. Boards therefore survive a restart, a
  redeploy, or a free-tier instance waking back up.
- The snapshot is one JSONB row, not a relational schema. The app already holds every event in
  memory behind a single lock, so a normalised schema would buy nothing; what's needed is a durable
  place to put the same blob the file backend writes. Swap `PostgresSnapshot` for something else in
  `snapshot.py` if that stops being true.
- Snapshot failures are logged and swallowed. A database outage must never break the tap that
  triggered the write — the next mutation writes the entire state again.
- Single-process only — the store uses a thread lock, not a shared backend. Run one uvicorn
  worker. If you need more, swap `EventStore` for Redis; the interface is small on purpose.
- Events are never garbage-collected. For a long-lived deployment, add a sweep for events older
  than a day.
- The host dashboard polls every 5s while an event is live. Guests re-sync when their tab becomes
  visible rather than polling, to be kind to phone batteries.
- `LOG_LEVEL` controls verbosity. Names typed onto squares are user content and land in the
  snapshot file — delete it after an event if that matters to you.

## Rollback

Locally: stop the server and `rm -rf data/`. On Render: `DELETE FROM pregussy_snapshot;` against the
database, or just delete and recreate it — everything the app persists lives in that one table.

To roll back a deploy, revert the commit and push; Render redeploys from `main`. Boards survive the
restart because they're in Postgres, not in the container.
