"""Tests for Pantrypls.

Run from the project folder:
    pip install -r requirements.txt
    python -m unittest discover -s tests -v

Every test gets its own empty database in a temporary folder.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="pantrypls-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["APP_TZ"] = "UTC"
import app as A  # noqa: E402  (must come after the environment is set)


def places(state):
    return {p["key"]: p for p in state["places"]}


def items(state, key=None):
    return {i["name"]: i for p in state["places"] if key in (None, p["key"]) for i in p["items"]}


class Base(unittest.TestCase):
    def fresh_db(self, seed=None):
        d = tempfile.mkdtemp(dir=_TMP)
        A.DATA_DIR, A.DB_PATH = d, os.path.join(d, "pantry.db")
        if seed:
            seed(A.DB_PATH)
        A.init_db()
        return A.app.test_client()

    def setUp(self):
        self.c = self.fresh_db()

    def state(self):
        return self.c.get("/api/state").get_json()

    def quick(self, text, **ctx):
        r = self.c.post("/api/quick", json={"text": text, **ctx})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class Shorthand(Base):
    def test_adds_item_and_bare_name_keeps_location(self):
        self.quick("milk +2 loc:fridge")
        s = self.quick("milk +2")
        self.assertEqual(items(s)["milk"]["quantity"], 4)
        self.assertEqual(items(s)["milk"]["location"], "fridge")

    def test_trailing_loc_line_applies_to_whole_batch(self):
        s = self.quick("eggs +12\nbutter +1\nbogus line\nloc:freezer")
        self.assertEqual(items(s)["eggs"]["location"], "freezer")
        self.assertEqual(items(s)["butter"]["location"], "freezer")
        self.assertEqual(s["applied"], 2)
        self.assertEqual(s["skipped"], ["bogus line"])

    def test_subtract_never_goes_below_zero_and_equals_sets(self):
        self.quick("milk +2")
        self.assertEqual(items(self.quick("milk -9"))["milk"]["quantity"], 0)
        self.assertEqual(items(self.quick("milk =6"))["milk"]["quantity"], 6)

    def test_names_match_case_insensitively(self):
        self.quick("milk +1")
        s = self.quick("MILK +1")
        self.assertEqual(items(s)["milk"]["quantity"], 2)
        self.assertNotIn("MILK", items(s))

    def test_inside_a_bin(self):
        self.c.post("/api/bins", json={"name": "Toolbox"})
        bid = next(p["id"] for p in self.state()["places"] if p["kind"] == "bin")
        s = self.quick("tape +4", bin_id=bid)
        self.assertEqual(items(s, f"bin:{bid}")["tape"]["quantity"], 4)

    def test_viewed_place_only_decides_where_NEW_items_go(self):
        # Regression: typing "milk +1" while looking at Pantry used to move fridge milk.
        self.quick("milk +1 loc:fridge")
        s = self.quick("milk +1", location="pantry")
        self.assertEqual(items(s)["milk"]["location"], "fridge")
        s = self.quick("naan +1", location="pantry")
        self.assertEqual(items(s)["naan"]["location"], "pantry")

    def test_explicit_location_still_moves_an_existing_item(self):
        self.quick("milk +1 loc:fridge")
        self.assertEqual(items(self.quick("milk +1 loc:freezer", location="pantry"))["milk"]["location"], "freezer")
        self.assertEqual(items(self.quick("milk +1\nloc:pantry", location="fridge"))["milk"]["location"], "pantry")


class TwoPeople(Base):
    def test_thirty_simultaneous_taps_all_count(self):
        iid = items(self.quick("milk +0"))["milk"]["id"]

        def tap():
            A.app.test_client().patch(f"/api/items/pantry/{iid}", json={"delta": 1})

        threads = [threading.Thread(target=tap) for _ in range(30)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(items(self.state())["milk"]["quantity"], 30)

    def test_saving_a_new_name_keeps_the_other_persons_taps(self):
        # Regression: the edit box used to resend the old quantity and erase a partner's tap.
        iid = items(self.quick("milk +3"))["milk"]["id"]
        self.c.patch(f"/api/items/pantry/{iid}", json={"delta": 5})
        s = self.c.patch(f"/api/items/pantry/{iid}", json={"name": "Milk 2%"}).get_json()
        self.assertEqual(items(s)["Milk 2%"]["quantity"], 8)

    def test_tap_on_an_item_the_other_person_deleted(self):
        r = self.c.patch("/api/items/pantry/99999", json={"delta": 1})
        self.assertEqual(r.status_code, 404)
        self.assertIn("state", r.get_json())


class Validation(Base):
    def test_rejects_bad_input(self):
        bad = [
            {"kind": "pantry", "name": "x", "quantity": -1},
            {"kind": "pantry", "name": "x", "quantity": 99999},
            {"kind": "pantry", "name": "x", "expires_on": "tomorrow"},
            {"kind": "pantry", "name": "x", "location": "a<b>"},
            {"kind": "pantry", "name": "   "},
            {"kind": "bin", "name": "x", "bin_id": 999},
            {"kind": "nonsense", "name": "x"},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.c.post("/api/items", json=body).status_code, 400)

    def test_duplicate_names_are_refused(self):
        self.c.post("/api/items", json={"kind": "pantry", "name": "eggs"})
        self.assertEqual(self.c.post("/api/items", json={"kind": "pantry", "name": "Eggs"}).status_code, 409)
        self.c.post("/api/items", json={"kind": "pantry", "name": "milk"})
        iid = items(self.state())["milk"]["id"]
        self.assertEqual(self.c.patch(f"/api/items/pantry/{iid}", json={"name": "eggs"}).status_code, 409)

    def test_only_real_json_is_accepted(self):
        r = self.c.post("/api/items", data="name=x", content_type="application/x-www-form-urlencoded")
        self.assertEqual(r.status_code, 415)

    def test_cross_site_writes_are_refused(self):
        body = {"kind": "pantry", "name": "x"}
        evil = self.c.post("/api/items", json=body, headers={"Origin": "http://evil.example"})
        self.assertEqual(evil.status_code, 403)
        null = self.c.post("/api/items", json=body, headers={"Origin": "null"})
        self.assertEqual(null.status_code, 403)
        same = self.c.post("/api/items", json=body, headers={"Origin": "http://localhost"})
        self.assertEqual(same.status_code, 201)


class Expiry(Base):
    def test_status_uses_the_configured_local_date(self):
        today = datetime.now(A.TZ).date()
        for name, delta in (("old", -1), ("tomorrow", 1), ("later", 10)):
            self.c.post("/api/items", json={"kind": "pantry", "name": name, "expires_on": (today + timedelta(days=delta)).isoformat()})
        it = items(self.state())
        self.assertEqual((it["old"]["exp_status"], it["old"]["days_left"]), ("expired", -1))
        self.assertEqual((it["tomorrow"]["exp_status"], it["tomorrow"]["days_left"]), ("soon", 1))
        self.assertEqual(it["later"]["exp_status"], "ok")

    def test_low_and_summary_counts(self):
        self.c.post("/api/items", json={"kind": "pantry", "name": "rice", "quantity": 3, "threshold": 1})
        self.quick("pasta +0")
        s = self.state()
        self.assertFalse(items(s)["rice"]["low"])
        self.assertTrue(items(s)["pasta"]["out"])
        self.assertEqual(s["counts"]["low"], 1)


class Places(Base):
    def test_new_location_gets_its_own_place_and_unsorted_maps_to_empty(self):
        s = self.c.post("/api/items", json={"kind": "pantry", "name": "rice", "location": "garage"}).get_json()
        self.assertIn("loc:garage", places(s))
        self.assertEqual(A.clean_loc("Unsorted"), "")

    def test_bin_lifecycle_and_stable_nfc_paths(self):
        r = self.c.post("/api/bins", json={"name": "Garage Bin", "location": "shelf"})
        self.assertEqual(r.status_code, 201)
        b = next(p for p in r.get_json()["places"] if p["title"] == "Garage Bin")
        self.assertEqual(b["path"], f"/bin/{b['id']}")
        self.assertEqual(self.c.post("/api/bins", json={"name": "garage bin"}).status_code, 409)
        self.c.post("/api/quick", json={"text": "hammer +1", "bin_id": b["id"]})
        self.c.patch(f"/api/bins/{b['id']}", json={"name": "Renamed"})
        s = self.state()
        self.assertEqual(places(s)[f"bin:{b['id']}"]["path"], f"/bin/{b['id']}")  # rename keeps the tag working
        s = self.c.delete(f"/api/bins/{b['id']}").get_json()
        self.assertNotIn(f"bin:{b['id']}", places(s))
        self.assertNotIn("hammer", items(s))  # its items went with it

    def test_default_places_always_exist(self):
        keys = [p["key"] for p in self.state()["places"]][:3]
        self.assertEqual(keys, ["loc:fridge", "loc:pantry", "loc:freezer"])


class Pages(Base):
    def test_pages_and_static_files(self):
        for path, kind in [("/", "text/html"), ("/loc/fridge", "text/html"), ("/loc/unsorted", "text/html"),
                           ("/bin/1", "text/html"), ("/static/manifest.json", "application/json"),
                           ("/static/icon-180.png", "image/png")]:
            with self.subTest(path=path):
                r = self.c.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.content_type.startswith(kind), r.content_type)
                r.close()
        self.assertEqual(self.c.get("/healthz").data, b"ok")


class Migration(Base):
    def test_a_database_from_the_earlier_discord_version_opens_as_is(self):
        def seed(path):
            con = sqlite3.connect(path)
            con.executescript(A.SCHEMA)
            con.execute("INSERT INTO bins (name, location, discord_channel_id) VALUES ('Toolbox', 'Garage', '123')")
            con.execute("INSERT INTO items (bin_id, name, quantity, updated_at) VALUES (1, 'paintbrush', 2, '2026-01-01')")
            con.execute("INSERT INTO pantry_items (name, quantity, location, low_stock_threshold, updated_at) VALUES ('milk', 1, 'fridge', 1, '2026-01-01')")
            con.commit()
            con.close()

        c = self.fresh_db(seed)
        s = c.get("/api/state").get_json()
        self.assertEqual(items(s, "loc:fridge")["milk"]["quantity"], 1)
        self.assertTrue(items(s, "loc:fridge")["milk"]["low"])
        self.assertEqual(items(s, "bin:1")["paintbrush"]["quantity"], 2)


if __name__ == "__main__":
    unittest.main()
