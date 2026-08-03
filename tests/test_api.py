"""End-to-end API tests: event lifecycle, joining, signing rules, host controls."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lineup import main
from lineup.board import completed_lines
from lineup.store import EventStore

PROMPTS = [f"Has done thing number {i}" for i in range(30)]


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    """Every test gets an empty, on-disk-isolated store."""
    monkeypatch.setattr(main, "store", EventStore(snapshot_path=tmp_path / "events.json"))


@pytest.fixture
def client():
    return TestClient(main.app)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_event(client, *, prompts=None, size=5, start=True, **settings):
    created = client.post(
        "/api/events",
        json={"title": "Pregame", "description": "Rooftop", "prompts": prompts or PROMPTS},
    )
    assert created.status_code == 201, created.text
    code = created.json()["code"]
    host = {"X-Host-Token": created.json()["host_token"]}

    response = client.patch(f"/api/events/{code}", json={"size": size, **settings}, headers=host)
    assert response.status_code == 200, response.text
    if start:
        response = client.patch(f"/api/events/{code}", json={"state": "live"}, headers=host)
        assert response.status_code == 200, response.text
    return code, host


def join(client, code, name):
    response = client.post(f"/api/events/{code}/players", json={"name": name})
    assert response.status_code == 201, response.text
    body = response.json()
    return body, {"X-Player-Token": body["player_token"]}


def sign(client, code, auth, index, name, participant_id=None):
    return client.put(
        f"/api/events/{code}/me/squares/{index}",
        json={"name": name, "participant_id": participant_id},
        headers=auth,
    )


def open_indexes(state):
    return [i for i, square in enumerate(state["squares"]) if not square["free"]]


# --------------------------------------------------------------------------- #
# Event lifecycle
# --------------------------------------------------------------------------- #


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_new_event_starts_as_draft_and_refuses_joins(client):
    code, _host = make_event(client, start=False)
    assert client.get(f"/api/events/{code}").json()["state"] == "draft"
    response = client.post(f"/api/events/{code}/players", json={"name": "Ada"})
    assert response.status_code == 409
    assert "hasn't opened" in response.json()["detail"]


def test_state_machine_allows_pause_resume_end(client):
    code, host = make_event(client)
    for target in ("paused", "live", "ended"):
        response = client.patch(f"/api/events/{code}", json={"state": target}, headers=host)
        assert response.status_code == 200
        assert response.json()["event"]["state"] == target
    # Nothing follows "ended".
    reopen = client.patch(f"/api/events/{code}", json={"state": "live"}, headers=host)
    assert reopen.status_code == 409


def test_cannot_start_without_enough_prompts(client):
    created = client.post("/api/events", json={"title": "Thin", "prompts": PROMPTS[:10]})
    code = created.json()["code"]
    host = {"X-Host-Token": created.json()["host_token"]}
    client.patch(f"/api/events/{code}", json={"size": 5}, headers=host)
    response = client.patch(f"/api/events/{code}", json={"state": "live"}, headers=host)
    assert response.status_code == 422
    assert "needs 24 prompts" in response.json()["detail"]


def test_starting_freezes_the_prompt_pool(client):
    code, host = make_event(client)
    assert client.get(f"/api/events/{code}/dashboard", headers=host).json()["frozen"] is True
    response = client.patch(f"/api/events/{code}", json={"prompts": PROMPTS[:24]}, headers=host)
    assert response.status_code == 409
    assert "locked" in response.json()["detail"]


def test_host_endpoints_require_the_host_token(client):
    code, _host = make_event(client)
    assert client.get(f"/api/events/{code}/dashboard").status_code == 403
    assert client.patch(f"/api/events/{code}", json={"state": "ended"}).status_code == 403
    assert client.delete(f"/api/events/{code}").status_code == 403


def test_unknown_event_is_404(client):
    assert client.get("/api/events/ZZZZZ").status_code == 404


# --------------------------------------------------------------------------- #
# Boards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("size", "free_centre", "expected_free"),
    [(3, True, 1), (4, True, 0), (5, True, 1), (5, False, 0), (3, False, 0)],
)
def test_board_shape(client, size, free_centre, expected_free):
    code, _host = make_event(client, size=size, free_centre=free_centre)
    state, _auth = join(client, code, "Ada")
    assert len(state["squares"]) == size * size
    assert sum(square["free"] for square in state["squares"]) == expected_free
    prompts = [s["prompt"] for s in state["squares"] if not s["free"]]
    assert len(set(prompts)) == len(prompts)  # no repeated prompt on one board


def test_each_player_gets_a_different_arrangement(client):
    code, _host = make_event(client)
    boards = set()
    for i in range(6):
        state, _auth = join(client, code, f"Player {i}")
        boards.add(tuple(square["prompt"] for square in state["squares"]))
    assert len(boards) > 1


def test_free_centre_is_pre_filled_and_not_signable(client):
    code, _host = make_event(client, size=5)
    state, auth = join(client, code, "Ada")
    assert state["squares"][12]["free"] is True
    assert state["filled_count"] == 1
    assert sign(client, code, auth, 12, "Someone").status_code == 400


# --------------------------------------------------------------------------- #
# Joining
# --------------------------------------------------------------------------- #


def test_join_returns_a_board_and_a_token(client):
    code, _host = make_event(client)
    state, _auth = join(client, code, "  Ada   Lovelace ")
    assert state["name"] == "Ada Lovelace"  # whitespace collapsed
    assert state["created"] is True
    assert state["player_token"]


def test_rejoining_by_name_reopens_the_same_board(client):
    code, _host = make_event(client)
    first, _auth = join(client, code, "Ada")
    again, _auth2 = join(client, code, "  ADA  ")
    assert again["created"] is False
    assert again["player_id"] == first["player_id"]
    assert [s["prompt"] for s in again["squares"]] == [s["prompt"] for s in first["squares"]]


def test_late_joining_works_while_paused(client):
    code, host = make_event(client)
    join(client, code, "Early")
    client.patch(f"/api/events/{code}", json={"state": "paused"}, headers=host)
    state, _auth = join(client, code, "Late")
    assert len(state["squares"]) == 25
    assert state["event"]["state"] == "paused"
    participants = client.get(f"/api/events/{code}/participants").json()
    assert {p["name"] for p in participants} == {"Early", "Late"}


def test_joining_is_closed_once_the_event_ends(client):
    code, host = make_event(client)
    client.patch(f"/api/events/{code}", json={"state": "ended"}, headers=host)
    assert client.post(f"/api/events/{code}/players", json={"name": "Ada"}).status_code == 409


def test_blank_name_is_rejected(client):
    code, _host = make_event(client)
    assert client.post(f"/api/events/{code}/players", json={"name": "   "}).status_code == 422


def test_unknown_player_token_is_401(client):
    code, _host = make_event(client)
    response = client.get(f"/api/events/{code}/me", headers={"X-Player-Token": "nope"})
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Signing squares
# --------------------------------------------------------------------------- #


def test_sign_and_clear_a_square(client):
    code, _host = make_event(client)
    state, auth = join(client, code, "Ada")
    index = open_indexes(state)[0]

    signed = sign(client, code, auth, index, "Grace").json()
    assert signed["squares"][index]["signed_name"] == "Grace"
    assert signed["squares"][index]["signed_at"] is not None
    assert signed["filled_count"] == 2  # the free centre plus this one

    cleared = sign(client, code, auth, index, None).json()
    assert cleared["squares"][index]["signed_name"] is None
    assert cleared["filled_count"] == 1


def test_the_same_person_cannot_take_two_squares(client):
    code, _host = make_event(client)
    state, auth = join(client, code, "Ada")
    first, second = open_indexes(state)[:2]
    assert sign(client, code, auth, first, "Grace").status_code == 200
    response = sign(client, code, auth, second, "  grace  ")  # case and spacing ignored
    assert response.status_code == 409
    assert "One square per person" in response.json()["detail"]


def test_host_can_allow_repeat_signers(client):
    code, _host = make_event(client, allow_repeat_signers=True)
    state, auth = join(client, code, "Ada")
    first, second = open_indexes(state)[:2]
    assert sign(client, code, auth, first, "Grace").status_code == 200
    assert sign(client, code, auth, second, "Grace").status_code == 200


def test_cannot_sign_a_square_with_your_own_participant_id(client):
    code, _host = make_event(client)
    state, auth = join(client, code, "Ada")
    response = sign(client, code, auth, open_indexes(state)[0], "Ada", state["player_id"])
    assert response.status_code == 409
    assert "That's you" in response.json()["detail"]


def test_your_own_name_typed_by_hand_is_allowed(client):
    """The UI warns; the server permits it, because names aren't identities here."""
    code, _host = make_event(client)
    state, auth = join(client, code, "Ada")
    index = open_indexes(state)[0]
    response = sign(client, code, auth, index, "Ada")
    assert response.status_code == 200
    assert response.json()["squares"][index]["signed_participant_id"] is None


def test_signing_a_participant_uses_their_canonical_name(client):
    code, _host = make_event(client)
    ada, auth = join(client, code, "Ada")
    grace, _auth = join(client, code, "Grace Hopper")
    index = open_indexes(ada)[0]
    response = sign(client, code, auth, index, "typo", grace["player_id"])
    assert response.status_code == 200
    square = response.json()["squares"][index]
    assert square["signed_name"] == "Grace Hopper"
    assert square["signed_participant_id"] == grace["player_id"]


def test_signing_an_unknown_participant_id_is_404(client):
    code, _host = make_event(client)
    state, auth = join(client, code, "Ada")
    assert sign(client, code, auth, open_indexes(state)[0], "X", "deadbeef").status_code == 404


def test_squares_are_frozen_while_paused_and_after_the_end(client):
    code, host = make_event(client)
    state, auth = join(client, code, "Ada")
    index = open_indexes(state)[0]

    client.patch(f"/api/events/{code}", json={"state": "paused"}, headers=host)
    response = sign(client, code, auth, index, "Grace")
    assert response.status_code == 409
    assert "paused" in response.json()["detail"]

    client.patch(f"/api/events/{code}", json={"state": "ended"}, headers=host)
    response = sign(client, code, auth, index, "Grace")
    assert response.status_code == 409
    assert "ended" in response.json()["detail"]
    # Reading the final board still works.
    assert client.get(f"/api/events/{code}/me", headers=auth).status_code == 200


def test_square_index_must_be_on_the_board(client):
    code, _host = make_event(client, size=3)
    _state, auth = join(client, code, "Ada")
    assert sign(client, code, auth, 20, "Grace").status_code == 400


# --------------------------------------------------------------------------- #
# Bingo
# --------------------------------------------------------------------------- #


def test_bingo_is_detected_and_stamped_by_the_server(client):
    code, _host = make_event(client, size=3)
    _state, auth = join(client, code, "Ada")
    # 3x3 top row is 0, 1, 2 — the free square is the centre, index 4.
    for i, index in enumerate([0, 1, 2]):
        state = sign(client, code, auth, index, f"Guest {i}").json()

    assert state["has_bingo"] is True
    assert [0, 1, 2] in state["completed_lines"]
    assert state["bingo_at"] is not None
    assert state["place"] == 1


def test_undoing_a_square_gives_the_bingo_back(client):
    code, _host = make_event(client, size=3)
    _state, auth = join(client, code, "Ada")
    for i, index in enumerate([0, 1, 2]):
        state = sign(client, code, auth, index, f"Guest {i}").json()
    assert state["has_bingo"] is True

    state = sign(client, code, auth, 1, None).json()
    assert state["has_bingo"] is False
    assert state["bingo_at"] is None
    assert state["place"] is None


def test_places_follow_server_completion_order(client):
    code, _host = make_event(client, size=3)
    _first_state, first = join(client, code, "First")
    _second_state, second = join(client, code, "Second")

    for i, index in enumerate([0, 1, 2]):
        first_state = sign(client, code, first, index, f"A{i}").json()
    for i, index in enumerate([0, 1, 2]):
        second_state = sign(client, code, second, index, f"B{i}").json()

    assert first_state["place"] == 1
    assert second_state["place"] == 2
    # The game keeps going after the first bingo.
    assert client.get(f"/api/events/{code}").json()["state"] == "live"


def test_blackout_fills_every_square(client):
    code, _host = make_event(client, size=3, free_centre=False, prompts=PROMPTS[:9])
    _state, auth = join(client, code, "Ada")
    for index in range(9):
        state = sign(client, code, auth, index, f"Guest {index}").json()
    assert state["blackout"] is True
    assert state["filled_count"] == 9
    assert len(state["completed_lines"]) == len(completed_lines([True] * 9, 3))


# --------------------------------------------------------------------------- #
# Host dashboard
# --------------------------------------------------------------------------- #


def test_dashboard_lists_players_progress_and_winners(client):
    code, host = make_event(client, size=3)
    _state, auth = join(client, code, "Ada")
    join(client, code, "Idle")
    for i, index in enumerate([0, 1, 2]):
        sign(client, code, auth, index, f"Guest {i}")

    dashboard = client.get(f"/api/events/{code}/dashboard", headers=host).json()
    assert dashboard["event"]["player_count"] == 2
    assert len(dashboard["players"]) == 2
    assert dashboard["players"][0]["name"] == "Ada"  # sorted by progress
    assert dashboard["players"][0]["line_count"] >= 1
    assert [w["name"] for w in dashboard["winners"]] == ["Ada"]
    assert dashboard["winners"][0]["place"] == 1
    assert dashboard["join_url_path"] == f"/e/{code}"


def test_write_in_names_are_listed_separately_from_participants(client):
    code, host = make_event(client, size=3)
    ada_state, ada = join(client, code, "Ada")
    grace, _auth = join(client, code, "Grace")

    indexes = open_indexes(ada_state)
    sign(client, code, ada, indexes[0], "Grace", grace["player_id"])
    sign(client, code, ada, indexes[1], "Sam From The Bar")

    dashboard = client.get(f"/api/events/{code}/dashboard", headers=host).json()
    guests = {g["name"]: g["count"] for g in dashboard["guest_names"]}
    assert guests == {"Sam From The Bar": 1}  # Grace joined, so she isn't a write-in
    assert {p["name"] for p in dashboard["players"]} == {"Ada", "Grace"}


def test_host_can_rename_and_remove_a_player(client):
    code, host = make_event(client)
    state, auth = join(client, code, "Rude Name")

    renamed = client.patch(
        f"/api/events/{code}/players/{state['player_id']}",
        json={"name": "Polite Name"},
        headers=host,
    )
    assert renamed.status_code == 200
    assert renamed.json()["players"][0]["name"] == "Polite Name"

    removed = client.delete(f"/api/events/{code}/players/{state['player_id']}", headers=host)
    assert removed.status_code == 200
    assert removed.json()["players"] == []
    # Their token stops working once they're gone.
    assert client.get(f"/api/events/{code}/me", headers=auth).status_code == 401


def test_removing_an_unknown_player_is_404(client):
    code, host = make_event(client)
    assert client.delete(f"/api/events/{code}/players/nope", headers=host).status_code == 404


def test_preview_deals_a_sample_board(client):
    code, host = make_event(client, size=4, start=False)
    preview = client.get(f"/api/events/{code}/preview", headers=host).json()
    assert preview["size"] == 4
    assert len(preview["squares"]) == 16
    assert all(square["signed_name"] is None for square in preview["squares"])


def test_host_can_edit_prompts_and_shape_while_drafting(client):
    code, host = make_event(client, start=False)
    response = client.patch(
        f"/api/events/{code}",
        json={"size": 3, "prompts": ["a", "b", "c", "d", "e", "f", "g", "h"]},
        headers=host,
    )
    assert response.status_code == 200
    assert response.json()["prompts_required"] == 8
    assert response.json()["prompts"] == ["a", "b", "c", "d", "e", "f", "g", "h"]


def test_prompts_are_deduped_and_trimmed(client):
    created = client.post(
        "/api/events",
        json={"title": "T", "prompts": ["  Same  thing ", "SAME THING", "other", "  "]},
    )
    code = created.json()["code"]
    host = {"X-Host-Token": created.json()["host_token"]}
    dashboard = client.get(f"/api/events/{code}/dashboard", headers=host).json()
    assert dashboard["prompts"] == ["Same thing", "other"]


def test_delete_event(client):
    code, host = make_event(client)
    assert client.delete(f"/api/events/{code}", headers=host).status_code == 204
    assert client.get(f"/api/events/{code}").status_code == 404


# --------------------------------------------------------------------------- #
# Persistence and pages
# --------------------------------------------------------------------------- #


def test_state_survives_a_restart(client, monkeypatch):
    code, _host = make_event(client)
    state, auth = join(client, code, "Ada")
    index = open_indexes(state)[0]
    sign(client, code, auth, index, "Grace")

    snapshot = main.store._snapshot_path
    monkeypatch.setattr(main, "store", EventStore(snapshot_path=snapshot))
    reloaded = TestClient(main.app)

    restored = reloaded.get(f"/api/events/{code}/me", headers=auth).json()
    assert restored["squares"][index]["signed_name"] == "Grace"
    assert restored["name"] == "Ada"


@pytest.mark.parametrize("path", ["/", "/e/ABCDE", "/h/ABCDE"])
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_static_assets_are_served(client):
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/manifest.webmanifest").status_code == 200
