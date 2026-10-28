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

from . import game, models, security  # noqa: E402
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
        counts = {(4, 7): 8, (0, 7): 5, (0, 0): 3, (14, 11): 3, (14, 0): 3}
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
        r = self.submit(token, sq, canvas, sid, x=(sq["x"] + 1) % 15)
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
            ({"x": 15}, 400, "INVALID_COORDINATE"),
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
            done = set(db.query(models.SubmittedForTypeOne.x,
                                models.SubmittedForTypeOne.y).filter_by(iteration=1))
            db.query(models.SubmittedForTypeOne).filter_by(iteration=1).update(
                {"submitted_string": "3" * 1024})
            for y in range(12):
                for x in range(15):
                    if (x, y) in done:
                        continue
                    db.add(models.SubmittedForTypeOne(
                        user_id=uid, submitted_string="3" * 1024, accuracy=1.0,
                        iteration=1, x=x, y=y, submission_id=str(uuid.uuid4())))
            db.commit()

        p = self.client.post("/api/user/fetch_current_progress",
                             json={"token": user, "game_type": 1}).json()
        self.assertTrue(p["success"])
        self.assertEqual(len(p["latest_iteration"]), 180)
        self.assertEqual(set(p["latest_iteration"]), {1})
        self.assertEqual(p["open_iterations"], [])
        self.assertEqual(p["player"]["contribution_count"], 180 - len(done))
        self.assertEqual(p["grid"], {"columns": 15, "rows": 12, "tile_size": 32})

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
                         [{"iteration": 2, "filled_tiles": 1, "total_tiles": 180}])

        # leaderboard: qualifies with >= 10 contributions
        lb = self.client.post("/api/user/request_leaderboard",
                              json={"token": user, "game_type": 1}).json()
        mine = [e for e in lb["top_contributors"] if e["username"] == "Filler"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["count"], 181 - len(done))
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

    # ---- suspicious events ---------------------------------------------------

    def events(self, **params):
        r = self.client.get("/api/admin/suspicious_events", params=params, auth=ADMIN)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_suspicious_events_need_admin(self):
        self.assertEqual(self.client.get("/api/admin/suspicious_events").status_code, 401)

    def test_fast_submissions_and_auto_ban_are_logged(self):
        token = self.register("Speedy Gonzales")["token"]
        uid = next(u for u in self.admin_rows("users")
                   if u["username"] == "Speedy Gonzales")["user_id"]
        # two submissions (different game types) back to back
        for gt in (1, 2):
            self.assertTrue(self.submit(token, self.square(token, gt)).json()["success"])
        fast = self.events(event_type="FAST_SUBMISSIONS", user_id=uid)["events"]
        self.assertEqual(len(fast), 1)
        self.assertLess(fast[0]["details"]["seconds_since_previous"], 20)
        self.assertEqual(fast[0]["severity"], "warning")

        # auto ban
        with SessionLocal() as db:
            for i in range(5):
                db.add(models.TileHold(
                    game_type=2, iteration=901, x=i, y=0, user_id=uid,
                    expires_at=models.utcnow() + timedelta(seconds=30),
                    reference_iteration=0))
            db.commit()
        self.client.post("/api/user/request_square", json={"token": token, "game_type": 1})
        ban = self.events(event_type="AUTO_BAN", user_id=uid)
        self.assertEqual(ban["total"], 1)
        self.assertEqual(ban["events"][0]["severity"], "critical")
        self.assertEqual(ban["events"][0]["details"]["active_holds"], 5)
        self.assertGreaterEqual(ban["last_24h"]["AUTO_BAN"], 1)
        # later calls with the banned token are logged separately, not as another ban
        self.client.post("/api/user/request_square", json={"token": token, "game_type": 1})
        self.assertEqual(self.events(event_type="AUTO_BAN", user_id=uid)["total"], 1)
        self.assertEqual(self.events(event_type="BANNED_USER_ACCESS", user_id=uid)["total"], 1)

    def test_submit_without_hold_is_logged(self):
        token = self.register("Forger")["token"]
        uid = next(u for u in self.admin_rows("users") if u["username"] == "Forger")["user_id"]
        sq = {"x": 1, "y": 1, "game_type": 1, "iteration": 77}
        self.assertEqual(self.submit(token, sq).json()["error"], "HOLD_NOT_FOUND")
        self.assertEqual(self.events(event_type="SUBMIT_WITHOUT_HOLD", user_id=uid)["total"], 1)

    def test_traffic_monitor_rules(self):
        t = [0.0]
        m = security.Monitor(clock=lambda: t[0])
        limit, window = security.IP_RATE
        events = []
        for i in range(limit + 5):
            t[0] += window / (limit * 2)  # fast enough to exceed the rule
            events += m.check_request("1.2.3.4", "/api/user/request_square")
        self.assertEqual([e[0] for e in events], ["RATE_LIMIT_EXCEEDED"])  # cooldown dedupes
        self.assertEqual(m.check_request("5.6.7.8", "/api/user/request_square"), [])

        # slow traffic is fine
        m2 = security.Monitor(clock=lambda: t[0])
        for _ in range(limit * 2):
            t[0] += window
            self.assertEqual(m2.check_request("1.1.1.1", "/api/user/fetch_tiles"), [])

        # repeated registers from one IP, whether they succeed or not
        m3 = security.Monitor(clock=lambda: t[0])
        types = []
        for _ in range(security.REGISTER_RATE[0] + 1):
            t[0] += 1
            types += [e[0] for e in m3.check_request("9.9.9.9", security.REGISTER_PATH)]
        self.assertEqual(types, ["REGISTER_FLOOD"])
        # a different IP is unaffected
        self.assertEqual(m3.check_request("8.8.8.8", security.REGISTER_PATH), [])

        # invalid-token probing and admin brute force
        m4 = security.Monitor(clock=lambda: t[0])
        out = []
        for _ in range(security.INVALID_TOKEN_RATE[0] + 1):
            t[0] += 0.1
            out += m4.check_invalid_token("7.7.7.7")
        self.assertEqual([e[0] for e in out], ["INVALID_TOKEN_PROBING"])
        out = []
        for _ in range(security.ADMIN_LOGIN_RATE[0] + 1):
            t[0] += 0.1
            out += m4.check_admin_login_failure("7.7.7.7")
        self.assertEqual([e[0] for e in out], ["ADMIN_LOGIN_BRUTEFORCE"])

    def test_global_traffic_spike(self):
        t = [0.0]
        m = security.Monitor(clock=lambda: t[0])
        limit, window = security.GLOBAL_RATE
        types = set()
        for i in range(limit + 2):
            t[0] += window / (limit * 2)
            types |= {e[0] for e in m.check_request(f"10.0.{i // 250}.{i % 250}", "/api/user/x")}
        self.assertEqual(types, {"TRAFFIC_SPIKE"})  # no single IP is hot

    def test_failed_admin_logins_and_register_floods_are_logged(self):
        for _ in range(security.ADMIN_LOGIN_RATE[0] + 1):
            self.client.post("/api/admin/login", auth=("boss", "wrong"))
        self.assertGreaterEqual(self.events(event_type="ADMIN_LOGIN_BRUTEFORCE")["total"], 1)
        for i in range(security.REGISTER_RATE[0] + 2):
            self.client.post("/api/user/register", json={"username": f"Flood Name {i}"})
        self.assertGreaterEqual(self.events(event_type="REGISTER_FLOOD")["total"], 1)
    def test_admin_can_ban_user_and_ip(self):
        token = self.register("Bad Actor")["token"]
        uid = next(u for u in self.admin_rows("users") if u["username"] == "Bad Actor")["user_id"]
        self.square(token, 1)  # leaves a hold
        self.client.post("/api/user/submit_result", json={"token": token})  # 422, no event
        # give the user an event row so the buttons apply
        security.log_event("TEST_EVENT", "info", "testclient", uid)
        ev = self.events(event_type="TEST_EVENT", user_id=uid)["events"][0]
        self.assertEqual((ev["user_banned"], ev["ip_banned"]), (False, False))

        post = lambda path, **kw: self.client.post(f"/api/admin/{path}", auth=ADMIN, **kw)
        self.assertEqual(post(f"users/{uid}/ban").json()["banned"], True)
        self.assertEqual(post("users/999999/ban").status_code, 404)
        r = self.client.post("/api/user/request_square", json={"token": token, "game_type": 1})
        self.assertEqual((r.status_code, r.json()["error"]), (403, "USER_BANNED"))
        with SessionLocal() as db:
            self.assertEqual(db.query(models.TileHold).filter_by(user_id=uid).count(), 0)
        self.assertTrue(self.events(event_type="TEST_EVENT", user_id=uid)["events"][0]["user_banned"])
        post(f"users/{uid}/unban")
        self.assertTrue(self.square(token, 1)["success"])

        try:
            post("ban_ip", json={"ip": "testclient", "reason": "test"})
            self.assertTrue(self.events(event_type="TEST_EVENT", user_id=uid)["events"][0]["ip_banned"])
            security.ip_bans.invalidate()
            r = self.client.post("/api/user/request_square", json={"token": token, "game_type": 1})
            self.assertEqual((r.status_code, r.json()["error"]), (403, "IP_BANNED"))
            # admin endpoints are exempt, so the admin can always undo it
        finally:
            post("unban_ip", json={"ip": "testclient"})
        self.assertTrue(self.square(token, 1)["success"])

    def test_visualizer(self):
        token = self.register("Painter")["token"]
        sq = self.square(token, 2)
        self.assertTrue(self.submit(token, sq, "3" * 1024).json()["success"])
        self.assertEqual(self.client.get("/api/admin/visualizer?game_type=2").status_code, 401)
        s = self.client.get("/api/admin/visualizer?game_type=2", auth=ADMIN).json()
        self.assertEqual(s["grid"], {"columns": 15, "rows": 12, "tile_size": 32})
        self.assertEqual(len(s["palette"]), 12)
        its = {i["iteration"]: i["filled_tiles"] for i in s["iterations"]}
        self.assertEqual(its[0], 180)
        self.assertGreaterEqual(its[sq["iteration"]], 1)
        it = self.client.get(
            "/api/admin/visualizer/iteration?game_type=2&iteration=" + str(sq["iteration"]),
            auth=ADMIN).json()
        mine = [t for t in it["tiles"] if (t["x"], t["y"]) == (sq["x"], sq["y"])]
        self.assertEqual((mine[0]["pixels"], mine[0]["username"]), ("3" * 1024, "Painter"))
        orig = self.client.get(
            "/api/admin/visualizer/iteration?game_type=2&iteration=0", auth=ADMIN).json()
        self.assertEqual(len(orig["tiles"]), 180)
        self.assertEqual(self.client.get(
            "/api/admin/visualizer?game_type=3", auth=ADMIN).status_code, 400)

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

    def test_itch_preflight_is_allowed(self):
        # Godot's web export posts JSON, so the browser sends this preflight
        # from the itch.io game origin before any API call.
        r = self.client.options(
            "/api/user/register",
            headers={
                "Origin": "https://html-classic.itch.zone",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["access-control-allow-origin"], "*")
        self.assertIn("content-type", r.headers["access-control-allow-headers"].lower())
        self.assertIn("POST", r.headers["access-control-allow-methods"])

        posted = self.client.post(
            "/api/user/register",
            json={"username": "Itch Player"},
            headers={"Origin": "https://html-classic.itch.zone"},
        )
        self.assertEqual(posted.status_code, 200, posted.text)
        self.assertEqual(posted.headers["access-control-allow-origin"], "*")
        self.assertTrue(posted.json()["success"])

    # ---- admin table editor -------------------------------------------------

    def test_list_tables(self):
        tables = {t["name"]: t for t in
                  self.client.get("/api/admin/tables", auth=ADMIN).json()}
        self.assertEqual(
            set(tables),
            {"health", "users_requests", "admin_requests", "users",
             "submitted_for_type_one", "submitted_for_type_two",
             "tile_holds", "game_state", "suspicious_events", "banned_ips"},
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
