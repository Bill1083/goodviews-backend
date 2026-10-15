"""Review moderation: the detector, the strike ladder (warning -> 7-day
suspension -> block + deletion request), the admin actions, and the auth
gates that enforce them."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import create_app
from app.controllers import moderation as moderation_controller
from app.services import moderation, profanity
from app.utils import auth as auth_utils
from tests.fakes import FakeSupabase

USER = "user-1"
ADMIN = "admin-1"


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def make_db(**user_overrides) -> FakeSupabase:
    user = {"id": USER, "username": "bob", "warning_count": 0, "moderation_status": "active", "suspended_until": None, "is_admin": False}
    user.update(user_overrides)
    admin = {"id": ADMIN, "username": "boss", "warning_count": 0, "moderation_status": "active", "suspended_until": None, "is_admin": True}
    return FakeSupabase({"profiles": [user, admin]})


def profile(db: FakeSupabase, user_id: str = USER) -> dict:
    return next(r for r in db.tables["profiles"] if r["id"] == user_id)


# ─── Detector ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "this is fucking awful", "What a sh1t film", "f*ck this", "f**king terrible", "a$$hole director",
    "f.u.c.k", "f u c k", "fuuuuck", "FUCK", "b*tch", "c*nt", "fuk this", "bullshit plot",
    "you retard", "stupid faggot", "n1gger", "go back to your country", "kill all the jews",
])
def test_flags_hate_and_swearing(text):
    assert profanity.check(text) is not None


@pytest.mark.parametrize("text", [
    "Classic! 10/10", "spicy and spiced, spices galore", "I love Moby Dick and Dick Tracy",
    "Pussy Galore is a great Bond girl", "*****", "Pakistan scenes", "Dick Van Dyke is great",
    "raccoon", "fire retardant", "Scunthorpe class assassin bass", "the bit chilly part",
    "It was damn good, hell of a ride, crap ending", "I'd kill for that cinematography",
    "Kill Bill is great", "cockpit scenes", "A B C D E and I a m", "100% $20 budget @home 5/5",
    "", None,
])
def test_leaves_normal_reviews_alone(text):
    assert profanity.check(text) is None


def test_reports_category():
    assert profanity.check("what a bitch")[0] == "profanity"
    assert profanity.check("stupid faggot")[0] == "hate"


# ─── Strike ladder ──────────────────────────────────────────────────────────
def test_first_strike_is_a_warning():
    db = make_db()
    outcome = moderation.record_violation(db, USER, category="profanity", text="bad words")
    assert outcome["action"] == "warning" and outcome["warning_count"] == 1
    assert profile(db)["warning_count"] == 1
    assert profile(db)["moderation_status"] == "active"
    assert db.tables["moderation_events"][0]["flagged_text"] == "bad words"


def test_second_strike_suspends_for_seven_days():
    db = make_db(warning_count=1)
    outcome = moderation.record_violation(db, USER, category="hate", text="x")
    assert outcome["action"] == "suspension" and outcome["warning_count"] == 2
    row = profile(db)
    assert row["moderation_status"] == "suspended"
    until = datetime.fromisoformat(row["suspended_until"])
    assert timedelta(days=6, hours=23) < until - datetime.now(timezone.utc) <= timedelta(days=7)


def test_third_strike_blocks_and_queues_a_deletion_request():
    db = make_db(warning_count=2)
    outcome = moderation.record_violation(db, USER, category="hate", text="x")
    assert outcome["action"] == "block"
    assert profile(db)["moderation_status"] == "blocked"
    (req,) = db.tables["deletion_requests"]
    assert req["user_id"] == USER and req["status"] == "pending"


def test_only_one_pending_request_per_account():
    db = make_db(warning_count=2)
    moderation.record_violation(db, USER, category="hate", text="x")
    moderation.record_violation(db, USER, category="hate", text="y")
    assert len(db.tables["deletion_requests"]) == 1


def test_long_flagged_text_is_truncated_in_the_audit_log():
    db = make_db()
    moderation.record_violation(db, USER, category="profanity", text="a" * 5000)
    assert len(db.tables["moderation_events"][0]["flagged_text"]) == moderation.FLAGGED_TEXT_MAX_LEN


# ─── State ──────────────────────────────────────────────────────────────────
def test_expired_suspension_is_lifted_lazily_but_keeps_its_warnings():
    db = make_db(warning_count=2, moderation_status="suspended", suspended_until=iso(timedelta(hours=-1)))
    state = moderation.get_state(db, USER)
    assert state["moderation_status"] == "active"
    assert profile(db)["moderation_status"] == "active" and profile(db)["warning_count"] == 2


def test_active_suspension_blocks_posting():
    db = make_db(warning_count=2, moderation_status="suspended", suspended_until=iso(timedelta(days=3)))
    body, status = moderation.posting_block(moderation.get_state(db, USER))
    assert status == 403 and body["code"] == "account_suspended"


def test_state_fails_open_when_columns_missing():
    db = make_db()
    db.missing_columns = {"profiles": {"moderation_status"}}
    assert moderation.get_state(db, USER) is None
    assert moderation.posting_block(None) is None


# ─── Admin actions ──────────────────────────────────────────────────────────
def test_admin_can_lift_a_suspension_early():
    db = make_db(warning_count=2, moderation_status="suspended", suspended_until=iso(timedelta(days=3)))
    moderation.lift_suspension(db, ADMIN, USER)
    assert profile(db)["moderation_status"] == "active" and profile(db)["warning_count"] == 2
    assert db.tables["moderation_events"][0]["action"] == "suspension_lifted"


def test_lifting_can_also_reset_warnings():
    db = make_db(warning_count=2, moderation_status="suspended", suspended_until=iso(timedelta(days=3)))
    moderation.lift_suspension(db, ADMIN, USER, warnings=0)
    assert profile(db)["warning_count"] == 0


def test_cannot_lift_a_suspension_that_isnt_there():
    with pytest.raises(moderation.ModerationError) as err:
        moderation.lift_suspension(make_db(), ADMIN, USER)
    assert err.value.status == 409


@pytest.mark.parametrize("warnings", [0, 1, 2])
def test_rejecting_a_deletion_restores_with_the_chosen_warnings(warnings):
    db = make_db(warning_count=3, moderation_status="blocked")
    db.tables["deletion_requests"] = [{"id": "req-1", "user_id": USER, "username": "bob", "status": "pending"}]
    moderation.reject_deletion(db, ADMIN, "req-1", warnings)
    row = profile(db)
    assert row["moderation_status"] == "active" and row["warning_count"] == warnings and row["suspended_until"] is None
    req = db.tables["deletion_requests"][0]
    assert req["status"] == "rejected" and req["restored_warnings"] == warnings


@pytest.mark.parametrize("bad", [3, -1, "1", None, True, 1.5])
def test_restore_warnings_must_be_0_to_2(bad):
    db = make_db(warning_count=3, moderation_status="blocked")
    db.tables["deletion_requests"] = [{"id": "req-1", "user_id": USER, "status": "pending"}]
    with pytest.raises(moderation.ModerationError):
        moderation.reject_deletion(db, ADMIN, "req-1", bad)
    assert db.tables["deletion_requests"][0]["status"] == "pending"


def test_approving_a_deletion_deletes_the_account(monkeypatch):
    db = make_db(warning_count=3, moderation_status="blocked")
    db.tables["deletion_requests"] = [{"id": "req-1", "user_id": USER, "username": "bob", "status": "pending"}]
    deleted = []
    monkeypatch.setattr(moderation, "_delete_auth_user", deleted.append)
    moderation.approve_deletion(db, ADMIN, "req-1")
    assert deleted == [USER]
    assert db.tables["deletion_requests"][0]["status"] == "approved"
    assert db.tables["moderation_events"][-1]["action"] == "deleted"


def test_a_resolved_request_cannot_be_resolved_again(monkeypatch):
    db = make_db()
    db.tables["deletion_requests"] = [{"id": "req-1", "user_id": USER, "status": "approved"}]
    monkeypatch.setattr(moderation, "_delete_auth_user", lambda _uid: pytest.fail("must not delete twice"))
    with pytest.raises(moderation.ModerationError) as err:
        moderation.approve_deletion(db, ADMIN, "req-1")
    assert err.value.status == 409


def test_set_warnings_refuses_blocked_accounts():
    db = make_db(warning_count=3, moderation_status="blocked")
    with pytest.raises(moderation.ModerationError):
        moderation.set_warnings(db, ADMIN, USER, 1)


# ─── HTTP gates ─────────────────────────────────────────────────────────────
@pytest.fixture
def client_for(monkeypatch):
    def _make(db: FakeSupabase, acting_as: str):
        app = create_app()
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        fake_user = SimpleNamespace(id=acting_as, factors=[], created_at=None)
        monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)
        monkeypatch.setattr(auth_utils, "get_supabase", lambda: db)
        monkeypatch.setattr(moderation_controller, "get_supabase", lambda: db)
        return app.test_client()

    return _make


AUTH = {"Authorization": "Bearer good"}


def test_blocked_account_can_only_read_its_own_status(client_for):
    client = client_for(make_db(warning_count=3, moderation_status="blocked"), USER)
    me = client.get("/api/moderation/me", headers=AUTH)
    assert me.status_code == 200 and me.get_json()["status"] == "blocked"
    other = client.get("/api/profile/", headers=AUTH)
    assert other.status_code == 403 and other.get_json()["code"] == "account_blocked"


def test_non_admin_gets_404_from_admin_endpoints(client_for):
    client = client_for(make_db(), USER)
    assert client.get("/api/admin/overview", headers=AUTH).status_code == 404
    assert client.post("/api/admin/deletion-requests/x/approve", headers=AUTH).status_code == 404


def test_admin_overview_lists_pending_requests_with_context(client_for):
    db = make_db(warning_count=3, moderation_status="blocked")
    db.tables["deletion_requests"] = [{"id": "req-1", "user_id": USER, "username": "bob", "status": "pending"}]
    db.tables["moderation_events"] = [{"id": "e1", "user_id": USER, "action": "block", "flagged_text": "bad"}]
    data = client_for(db, ADMIN).get("/api/admin/overview", headers=AUTH).get_json()
    (req,) = data["pending_deletions"]
    assert req["profile"]["warning_count"] == 3 and req["events"][0]["flagged_text"] == "bad"


def test_admin_reject_endpoint_validates_warnings(client_for):
    db = make_db(warning_count=3, moderation_status="blocked")
    db.tables["deletion_requests"] = [{"id": "req-1", "user_id": USER, "status": "pending"}]
    client = client_for(db, ADMIN)
    assert client.post("/api/admin/deletion-requests/req-1/reject", headers=AUTH, json={"warnings": 7}).status_code == 400
    ok = client.post("/api/admin/deletion-requests/req-1/reject", headers=AUTH, json={"warnings": 1})
    assert ok.status_code == 200 and profile(db)["warning_count"] == 1


def test_moderation_status_unreadable_means_not_locked_out(client_for):
    db = make_db()
    db.missing_columns = {"profiles": {"moderation_status"}}
    me = client_for(db, USER).get("/api/moderation/me", headers=AUTH)
    assert me.status_code == 200 and me.get_json()["status"] == "active"


# ─── Review endpoints ───────────────────────────────────────────────────────
@pytest.fixture
def review_client(monkeypatch, client_for):
    from app.controllers import reviews as reviews_controller

    def _make(db: FakeSupabase):
        client = client_for(db, USER)
        monkeypatch.setattr(reviews_controller, "get_supabase", lambda: db)
        monkeypatch.setattr(reviews_controller.movie_cache, "get_movie", lambda *a, **k: {})
        monkeypatch.setattr(reviews_controller.cache, "invalidate_user_stats", lambda _uid: None)
        return client

    return _make


def test_posting_a_hateful_review_saves_the_rating_without_the_comment(review_client):
    db = make_db()
    resp = review_client(db).post("/api/reviews/", headers=AUTH, json={"movie_id": 1, "rating": 4, "review_text": "what a fucking film"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["review_text"] == "" and body["rating"] == 4
    assert body["moderation"]["action"] == "warning"
    assert profile(db)["warning_count"] == 1
    assert db.tables["reviews"][0]["review_text"] == ""
    assert "fucking" in db.tables["moderation_events"][0]["flagged_text"]


def test_clean_review_is_untouched(review_client):
    db = make_db()
    resp = review_client(db).post("/api/reviews/", headers=AUTH, json={"movie_id": 1, "rating": 5, "review_text": "Great, 10/10!"})
    assert resp.status_code == 201
    assert "moderation" not in resp.get_json() and resp.get_json()["review_text"] == "Great, 10/10!"
    assert profile(db)["warning_count"] == 0


def test_suspended_user_cannot_post_or_edit(review_client):
    db = make_db(warning_count=2, moderation_status="suspended", suspended_until=iso(timedelta(days=2)))
    db.tables["reviews"] = [{"id": "r1", "user_id": USER, "movie_id": 1, "review_text": "ok"}]
    client = review_client(db)
    assert client.post("/api/reviews/", headers=AUTH, json={"movie_id": 1, "rating": 3}).status_code == 403
    assert client.put("/api/reviews/r1", headers=AUTH, json={"rating": 3}).status_code == 403


def test_editing_in_a_swear_keeps_the_old_comment_and_strikes(review_client):
    db = make_db()
    db.tables["reviews"] = [{"id": "r1", "user_id": USER, "movie_id": 1, "review_text": "Lovely film"}]
    resp = review_client(db).put("/api/reviews/r1", headers=AUTH, json={"review_text": "total bullshit"})
    assert resp.status_code == 200
    assert resp.get_json()["moderation"]["action"] == "warning"
    assert db.tables["reviews"][0]["review_text"] == "Lovely film"


def test_third_offence_through_the_api_blocks_the_account(review_client):
    db = make_db(warning_count=2)
    resp = review_client(db).post("/api/reviews/", headers=AUTH, json={"movie_id": 1, "rating": 1, "review_text": "kill all the jews"})
    assert resp.get_json()["moderation"]["action"] == "block"
    assert profile(db)["moderation_status"] == "blocked"
    assert db.tables["deletion_requests"][0]["status"] == "pending"
