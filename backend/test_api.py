import json
import os
import re
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

# Point the app at a throwaway DB and admin secret *before* importing it.
_TMP = tempfile.mkdtemp()
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")
os.environ["ADMIN_SECRET_FILE"] = str(Path(_TMP) / "admin.json")
Path(os.environ["ADMIN_SECRET_FILE"]).write_text(
    json.dumps({"username": "boss", "password": "s3cret"})
)

from fastapi.testclient import TestClient  # noqa: E402

from . import game, models  # noqa: E402
from .database import SessionLocal  # noqa: E402
from .main import app  # noqa: E402

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ADMIN = ("boss", "s3cret")
CANVAS = "2" * 1024


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def register(self, name):
        r = self.client.post("/api/user/register", json={"username": name})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def admin_rows(self, table, limit=50):
        r = self.client.get(f"/api/admin/tables/{table}/rows?limit={limit}", auth=ADMIN)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["rows"]

    # ---- users -------------------------------------------------------------

    def test_register(self):
        res = self.register("Alice One")
        self.assertTrue(res["success"])
        self.assertRegex(res["token"], UUID_RE)

        dup = self.register("alice one")  # case-insensitive duplicate
        self.assertFalse(dup["success"])
        self.assertIn("taken", dup["error"])

        self.assertFalse(self.register("ab")["success"])  # too short
        self.assertFalse(self.register("s a n d n i g g e r")["success"])

    def test_user_endpoints_need_valid_token(self):
        bad = "00000000-0000-0000-0000-000000000000"
        for path, body in [
            ("request_square", {"token": bad, "game_type": 1}),
            ("request_leaderboard", {"token": bad, "game_type": 2}),
            ("fetch_current_progress", {"token": bad, "game_type": 1}),
            ("fetch_tiles", {"token": bad, "game_type": 1, "tiles": []}),
            ("submit_result", {"token": bad, "x": 1, "y": 2, "canvas": CANVAS,
                               "time_taken": 1.5, "game_type": 1, "iteration": 0,
                               "submission_id": str(uuid.uuid4())}),
        ]:
            r = self.client.post(f"/api/user/{path}", json=body)
            self.assertEqual(r.status_code, 401, path)
            self.assertEqual(r.json()["error"], "INVALID_TOKEN")
            self.assertFalse(r.json()["success"], path)

    def square(self, token, gt=1):
        r = self.client.post("/api/user/request_square", json={"token": token, "game_type": gt})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def submit(self, token, sq, canvas=CANVAS, sid=None, **over):
        body = {"token": token, "x": sq["x"], "y": sq["y"], "canvas": canvas,
                "time_taken": 2.5, "game_type": sq["game_type"],
                "iteration": sq["iteration"], "submission_id": sid or str(uuid.uuid4())}
        body.update(over)
        return self.client.post("/api/user/submit_result", json=body)

    def test_request_square_levels(self):
        token = self.register("Level Check")["token"]
        sq = self.square(token, 1)
        self.assertTrue(sq["success"])
        lvl = sq["level_information"]
        self.assertEqual((lvl["kind"], lvl["width"], lvl["height"]), ("canvas", 32, 32))
        self.assertEqual(len(lvl["pixels"]), 1024)
        self.assertGreaterEqual(sq["iteration"], 1)
        self.assertLess(lvl["source_iteration"], sq["iteration"])

        sq2 = self.square(token, 2)
        lvl = sq2["level_information"]
        self.assertEqual(lvl["kind"], "context")
        self.assertEqual(sum(len(r["pixels"]) for r in lvl["regions"]), 3072)
        for r in lvl["regions"]:
            self.assertEqual(len(r["pixels"]), r["width"] * r["height"])
        self.assertEqual(
            self.client.post("/api/user/request_square",
                             json={"token": token, "game_type": 3}).json()["error"],
            "INVALID_GAME_TYPE")

    def test_context_layout(self):
        counts = {(4, 7): 8, (0, 7): 5, (0, 0): 3, (29, 23): 3, (29, 0): 3}
        for (x, y), n in counts.items():
            (wx, wy), cells = game.context_layout(x, y)
            self.assertEqual(len(cells), n, (x, y))
            self.assertEqual(sum(w * h for _, _, w, h in cells), 3072)
        self.assertEqual(game.context_layout(4, 7)[0], (112, 208))
        self.assertEqual(game.context_layout(0, 7)[0], (0, 208))

    def test_submit_flow_idempotency_and_leaderboard(self):
        token = self.register("Gamer Flow")["token"]
        sq = self.square(token, 1)
        sid = str(uuid.uuid4())

        canvas = "." * 409 + "2" * 615  # exactly the minimum coverage
        r = self.submit(token, sq, canvas, sid)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["normalized_canvas"], "1" * 409 + "2" * 615)
        ref = sq["level_information"]["pixels"]
        expected = sum(a == b for a, b in zip(body["normalized_canvas"], ref)) / 1024
        self.assertAlmostEqual(body["accuracy"], expected)

        # replay returns the same body; a different tile with the same id is rejected
        self.assertEqual(self.submit(token, sq, canvas, sid).json(), body)
        r = self.submit(token, sq, canvas, sid, x=(sq["x"] + 1) % 30)
        self.assertEqual((r.status_code, r.json()["error"]), (409, "SUBMISSION_ID_REUSED"))
        r = self.submit(token, sq, canvas)
        self.assertEqual(r.json()["error"], "TILE_ALREADY_SUBMITTED")

        user = next(u for u in self.admin_rows("users") if u["username"] == "Gamer Flow")
        subs = [s for s in self.admin_rows("submitted_for_type_one")
                if s["user_id"] == user["user_id"]]
        self.assertEqual([(s["x"], s["y"], s["iteration"]) for s in subs],
                         [(sq["x"], sq["y"], sq["iteration"])])

        # leaderboard needs 10 contributions; this user has 1
        lb = self.client.post("/api/user/request_leaderboard",
                              json={"token": token, "game_type": 1}).json()
        self.assertTrue(lb["success"])
        self.assertEqual([e for e in lb["top_contributors"] if e["is_current_user"]], [])

    def test_submit_rejections(self):
        token = self.register("Rejecter")["token"]
        sq = self.square(token, 1)
        cases = [
            ({"canvas": "x" * 1024}, 400, "CANVAS_INVALID"),
            ({"canvas": "1" * 1023}, 400, "CANVAS_INVALID"),
            ({"canvas": "." * 410 + "2" * 614}, 400, "INSUFFICIENT_COVERAGE"),
            ({"submission_id": "nope"}, 400, "INVALID_SUBMISSION_ID"),
            ({"x": 30}, 400, "INVALID_COORDINATE"),
            ({"game_type": 3}, 400, "INVALID_GAME_TYPE"),
            ({"iteration": sq["iteration"] + 5}, 404, "HOLD_NOT_FOUND"),
        ]
        for over, status, code in cases:
            r = self.submit(token, sq, **over)
            self.assertEqual((r.status_code, r.json()["error"]), (status, code), over)
        # the hold is still usable after all rejections
        self.assertTrue(self.submit(token, sq).json()["success"])
        # malformed JSON types are still 422
        self.assertEqual(self.submit(token, sq, time_taken="abc").status_code, 422)

    def test_hold_expiry(self):
        token = self.register("Slowpoke")["token"]
        sq = self.square(token, 1)
        with SessionLocal() as db:
            db.query(models.TileHold).filter_by(
                game_type=1, iteration=sq["iteration"], x=sq["x"], y=sq["y"]
            ).update({"expires_at": models.utcnow() - timedelta(seconds=1)})
            db.commit()
        self.assertEqual(self.submit(token, sq).json()["error"], "HOLD_EXPIRED")

    def test_allocation_is_unique_and_ban(self):
        tokens = [self.register(f"Allocator {i}")["token"] for i in range(3)]
        for _ in range(4):
            current = set()
            for t in tokens:
                sq = self.square(t, 2)
                key = (sq["iteration"], sq["x"], sq["y"])
                self.assertNotIn(key, current)  # concurrent holds never collide
                current.add(key)

        # A new request releases the user's previous hold of the same game type.
        token = self.register("Slot Collector")["token"]
        for _ in range(3):
            self.square(token, 1)
        with SessionLocal() as db:
            uid = db.query(models.User).filter_by(uuid=token).one().user_id
            self.assertEqual(db.query(models.TileHold).filter_by(
                user_id=uid, game_type=1).count(), 1)

        # 5 active holds (only reachable outside the API): the next request bans
        with SessionLocal() as db:
            for i in range(5):
                db.add(models.TileHold(
                    game_type=2, iteration=900, x=i, y=0, user_id=uid,
                    expires_at=models.utcnow() + timedelta(seconds=30),
                    reference_iteration=0))
            db.commit()
        r = self.client.post("/api/user/request_square", json={"token": token, "game_type": 1})
        self.assertEqual((r.status_code, r.json()["error"]), (403, "USER_BANNED"))
        r = self.client.post("/api/user/request_leaderboard",
                             json={"token": token, "game_type": 1})
        self.assertEqual((r.status_code, r.json()["error"]), (403, "USER_BANNED"))
        with SessionLocal() as db:
            user = db.query(models.User).filter_by(uuid=token).one()
            self.assertIsNotNone(user.banned_at)
            self.assertEqual(
                db.query(models.TileHold).filter_by(user_id=user.user_id).count(), 0)

    def test_iteration_reference_and_progress(self):
        """Fill iteration 1 of game type 1; iteration 2 must use it as reference."""
        user = self.register("Filler")["token"]
        with SessionLocal() as db:
            uid = db.query(models.User).filter_by(uuid=user).one().user_id
            for y in range(24):
                for x in range(30):
                    db.add(models.SubmittedForTypeOne(
                        user_id=uid, submitted_string="3" * 1024, accuracy=1.0,
                        iteration=1, x=x, y=y, submission_id=str(uuid.uuid4())))
            db.commit()

        p = self.client.post("/api/user/fetch_current_progress",
                             json={"token": user, "game_type": 1}).json()
        self.assertTrue(p["success"])
        self.assertEqual(len(p["latest_iteration"]), 720)
        self.assertEqual(set(p["latest_iteration"]), {1})
        self.assertEqual(p["open_iterations"], [])
        self.assertEqual(p["player"]["contribution_count"], 720)
        self.assertEqual(p["grid"], {"columns": 30, "rows": 24, "tile_size": 32})

        sq = self.square(user, 1)
        self.assertEqual(sq["iteration"], 2)
        self.assertEqual(sq["level_information"]["source_iteration"], 1)
        self.assertEqual(sq["level_information"]["pixels"], "3" * 1024)

        # submitting bumps state_version and shows up as open iteration 2
        before = p["state_version"]
        self.assertEqual(self.submit(user, sq, "3" * 1024).json()["accuracy"], 1.0)
        p = self.client.post("/api/user/fetch_current_progress",
                             json={"token": user, "game_type": 1}).json()
        self.assertEqual(p["state_version"], before + 1)
        self.assertEqual(p["open_iterations"],
                         [{"iteration": 2, "filled_tiles": 1, "total_tiles": 720}])

        # leaderboard: qualifies with >= 10 contributions
        lb = self.client.post("/api/user/request_leaderboard",
                              json={"token": user, "game_type": 1}).json()
        mine = [e for e in lb["top_contributors"] if e["username"] == "Filler"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["count"], 721)
        self.assertTrue(mine[0]["is_current_user"])

        # fetch_tiles
        r = self.client.post("/api/user/fetch_tiles", json={
            "token": user, "game_type": 1,
            "tiles": [{"iteration": 1, "x": 0, "y": 0}, {"iteration": 0, "x": 0, "y": 0},
                      {"iteration": 9, "x": 0, "y": 0}]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("max-age", r.headers["cache-control"])
        out = r.json()
        self.assertEqual([t["pixels"] for t in out["tiles"]], ["3" * 1024])
        self.assertEqual(len(out["missing"]), 2)
        r = self.client.post("/api/user/fetch_tiles", json={
            "token": user, "game_type": 1,
            "tiles": [{"iteration": 1, "x": 0, "y": 0}] * 101})
        self.assertEqual((r.status_code, r.json()["error"]), (400, "TOO_MANY_TILES"))

    def test_fetch_current_progress_validation(self):
        token = self.register("Blue Panda")["token"]
        url = "/api/user/fetch_current_progress"
        self.assertEqual(
            self.client.post(url, json={"token": token, "game_type": 3}).json()["error"],
            "INVALID_GAME_TYPE")
        for bad in [{"token": token}, {"game_type": 1}]:
            self.assertEqual(self.client.post(url, json=bad).status_code, 422)

    # ---- admin auth --------------------------------------------------------

    def test_admin_auth(self):
        self.assertEqual(self.client.post("/api/admin/login").status_code, 401)
        self.assertEqual(
            self.client.post("/api/admin/login", auth=("boss", "wrong")).status_code, 401
        )
        r = self.client.post("/api/admin/login", auth=ADMIN)
        self.assertEqual(r.json(), {"success": True})
        self.assertNotIn("www-authenticate", r.headers)
        for path in ["/api/admin/tables", "/api/admin/health",
                     "/api/admin/tables/users/rows"]:
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(self.client.get("/api/health").status_code, 404)

    def test_api_docs_require_admin(self):
        for path in ["/docs", "/redoc", "/openapi.json"]:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 401, path)
            # asks the browser for its native login prompt
            self.assertIn("Basic", r.headers["www-authenticate"])
            self.assertEqual(
                self.client.get(path, auth=("boss", "wrong")).status_code, 401, path)
            self.assertEqual(self.client.get(path, auth=ADMIN).status_code, 200, path)
        schema = self.client.get("/openapi.json", auth=ADMIN).json()
        self.assertIn("/api/user/register", schema["paths"])
        self.assertNotIn("/docs", schema["paths"])

    def test_frontend_is_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    # ---- admin table editor -------------------------------------------------

    def test_list_tables(self):
        tables = {t["name"]: t for t in
                  self.client.get("/api/admin/tables", auth=ADMIN).json()}
        self.assertEqual(
            set(tables),
            {"health", "users_requests", "admin_requests", "users",
             "submitted_for_type_one", "submitted_for_type_two",
             "tile_holds", "game_state"},
        )
        for t in tables.values():
            self.assertIn("created_at", [c["name"] for c in t["columns"]])
        self.assertEqual(tables["users"]["primary_key"], "user_id")

    def test_crud(self):
        c = self.client
        created = c.post("/api/admin/tables/health/rows", auth=ADMIN,
                         json={"status": "crud-test", "id": ""}).json()
        self.assertEqual(created["status"], "crud-test")
        self.assertTrue(created["created_at"])  # default applied
        pk = created["id"]

        upd = c.put(f"/api/admin/tables/health/rows/{pk}", auth=ADMIN,
                    json={"id": 9999, "status": "edited",
                          "created_at": "2020-01-02T03:04:05"}).json()
        self.assertEqual(upd["id"], pk)  # pk is not editable
        self.assertEqual(upd["status"], "edited")
        self.assertEqual(upd["created_at"], "2020-01-02T03:04:05")

        self.assertEqual(
            c.put(f"/api/admin/tables/health/rows/{pk}", auth=ADMIN,
                  json={"nope": 1}).status_code, 400)
        self.assertEqual(
            c.post("/api/admin/tables/health/rows", auth=ADMIN,
                   json={"created_at": "garbage"}).status_code, 400)
        self.assertEqual(c.get("/api/admin/tables/nope/rows", auth=ADMIN).status_code, 404)

        self.assertEqual(
            c.delete(f"/api/admin/tables/health/rows/{pk}", auth=ADMIN).json(),
            {"success": True})
        self.assertEqual(
            c.delete(f"/api/admin/tables/health/rows/{pk}", auth=ADMIN).status_code, 404)

    def test_deleting_user_cascades_to_submissions(self):
        token = self.register("Cascade Me")["token"]
        sq = self.square(token, 2)  # also leaves a hold that must cascade
        self.assertTrue(self.submit(token, sq).json()["success"])
        self.square(token, 1)
        user = next(u for u in self.admin_rows("users") if u["username"] == "Cascade Me")
        subs = [s for s in self.admin_rows("submitted_for_type_two")
                if s["user_id"] == user["user_id"]]
        self.assertEqual(len(subs), 1)

        r = self.client.delete(f"/api/admin/tables/users/rows/{user['user_id']}", auth=ADMIN)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            [s for s in self.admin_rows("submitted_for_type_two")
             if s["user_id"] == user["user_id"]], [])

    # ---- request log -------------------------------------------------------

    def test_users_requests_keep_tokens(self):
        token = self.register("Logged User")["token"]
        self.client.post("/api/user/request_square", json={"token": token, "game_type": 1})
        bogus = "11111111-2222-3333-4444-555555555555"
        self.client.post("/api/user/request_leaderboard",
                         json={"token": bogus, "game_type": 1})

        rows = self.admin_rows("users_requests", limit=200)

        reg = next(r for r in rows if r["path"] == "/api/user/register"
                   and "Logged User" in r["request_body"])
        self.assertEqual(reg["method"], "POST")
        self.assertEqual(reg["response_status"], 200)
        self.assertEqual(reg["token"], token)        # issued token is recorded
        self.assertIn(token, reg["response_body"])   # and not redacted
        self.assertTrue(reg["client_ip"])
        self.assertIsNone(reg["user_id"])

        sq = next(r for r in rows if r["path"] == "/api/user/request_square"
                  and r["token"] == token)
        self.assertIn(token, sq["request_body"])
        self.assertIsNotNone(sq["user_id"])  # resolved from the token

        # tokens that match no user are still recorded
        lb = next(r for r in rows if r["token"] == bogus)
        self.assertIsNone(lb["user_id"])

        for r in rows:  # no admin traffic or static files in this table
            self.assertTrue(r["path"].startswith("/api/"))
            self.assertFalse(r["path"].startswith("/api/admin"))
            self.assertNotIn("authorization", json.loads(r["headers"]))

    def test_admin_requests_are_redacted(self):
        token = self.register("Admin Viewed")["token"]
        self.client.get("/api/admin/tables/users/rows", auth=ADMIN)  # response has tokens
        self.client.post("/api/admin/login", auth=("boss", "wrong"))

        rows = self.admin_rows("admin_requests", limit=200)
        everything = json.dumps(rows)
        self.assertNotIn(token, everything)
        self.assertNotIn("Basic", everything)  # authorization header dropped
        self.assertNotIn("s3cret", everything)
        self.assertNotIn("token", rows[0])  # no token / user_id columns here
        self.assertNotIn("user_id", rows[0])

        listing = next(r for r in rows if r["path"] == "/api/admin/tables/users/rows")
        self.assertEqual(listing["response_status"], 200)
        self.assertIn("[uuid]", listing["response_body"])
        self.assertTrue(any(r["response_status"] == 401 for r in rows))
        for r in rows:
            self.assertTrue(r["path"].startswith("/api/admin"))


if __name__ == "__main__":
    unittest.main()
