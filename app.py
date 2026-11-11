"""Pantrypls: a self-hosted pantry / fridge / bin tracker for two people, with no login and no cloud.

Keeps the schema of an earlier Discord-bot version, so a database from it opens as is.
Two people edit at once, so every quantity change is a single atomic SQL statement.
"""
import mimetypes
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "pantry.db")
TZ = ZoneInfo(os.environ.get("APP_TZ") or os.environ.get("TZ") or "UTC")
WARN_DAYS = int(os.environ.get("EXPIRY_WARN_DAYS", "3"))
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5056").rstrip("/")
DEFAULT_LOCS = ["fridge", "pantry", "freezer"]
MAX_QTY = 9999

# Kept identical to the earlier Discord-bot version (including the discord_* columns and pantry_channel
# table, which are unused here) so an old database file opens without changes.
SCHEMA = """
CREATE TABLE IF NOT EXISTS bins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    location TEXT DEFAULT '',
    discord_channel_id TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bin_id INTEGER NOT NULL REFERENCES bins(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    unit TEXT DEFAULT '',
    category TEXT DEFAULT '',
    low_stock_threshold INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pantry_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    unit TEXT DEFAULT '',
    location TEXT DEFAULT '',
    low_stock_threshold INTEGER NOT NULL DEFAULT 0,
    expires_on TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pantry_channel (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    discord_channel_id TEXT NOT NULL
);
"""

# "milk +2 loc:fridge exp:2026-12-01"  (same shorthand the Discord bot used)
TEXT_PATTERN = re.compile(
    r"^(?P<name>.+?)\s+(?P<op>[+\-=])\s*(?P<qty>\d+)"
    r"(?:\s+loc:(?P<loc>\w+))?"
    r"(?:\s+exp:(?P<exp>\d{4}-\d{2}-\d{2}))?$",
    re.IGNORECASE,
)
DEFAULT_LOC_PATTERN = re.compile(r"^loc:(?P<loc>\w+)$", re.IGNORECASE)

# Don't trust the host's file-type table (a Windows registry entry once made .png serve as "silenteye/png").
for _ext, _type in ((".png", "image/png"), (".json", "application/json"), (".html", "text/html")):
    mimetypes.add_type(_type, _ext)

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


class Bad(Exception):
    """A request the user can fix; becomes a 400 with the message."""


# ---- database ------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)  # autocommit; we BEGIN by hand
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def write_tx():
    """One immediate write transaction: two phones tapping at once queue up instead of clobbering."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_db()
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(bins)")}
    if "discord_channel_id" not in cols:
        conn.execute("ALTER TABLE bins ADD COLUMN discord_channel_id TEXT")
    # The shorthand matches items by name, so duplicate names would make "milk +2" ambiguous.
    for sql in (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_pantry_name ON pantry_items(lower(name))",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_bin_item_name ON items(bin_id, lower(name))",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_bin_name ON bins(lower(name))",
    ):
        try:
            conn.execute(sql)
        except sqlite3.IntegrityError:
            print("WARNING: duplicate names exist, could not add:", sql)
    conn.close()


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


# ---- validation ----------------------------------------------------------------

def clean_name(v, what="Name"):
    v = " ".join(str(v or "").split())
    if not v or len(v) > 100:
        raise Bad(f"{what} must be 1 to 100 characters.")
    return v


def clean_unit(v):
    v = " ".join(str(v or "").split())
    if len(v) > 20:
        raise Bad("Unit must be 20 characters or fewer.")
    return v


def clean_loc(v):
    v = " ".join(str(v or "").split()).lower()
    if v == "unsorted":
        return ""
    if len(v) > 30 or not re.fullmatch(r"[a-z0-9 _-]*", v):
        raise Bad("Location can use letters, numbers, spaces, - and _ (30 max).")
    return v


def clean_int(v, field, lo=0, hi=MAX_QTY):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Bad(f"{field} must be a whole number.")
    if not lo <= n <= hi:
        raise Bad(f"{field} must be between {lo} and {hi}.")
    return n


def clean_date(v):
    if v in (None, ""):
        return None
    try:
        datetime.strptime(str(v), "%Y-%m-%d")
    except ValueError:
        raise Bad("Expiry date must look like 2026-12-01.")
    return str(v)


def json_body():
    body = request.get_json()  # 415 unless Content-Type is application/json (blocks cross-site form posts)
    if not isinstance(body, dict):
        raise Bad("Expected a JSON object.")
    return body


# ---- state ---------------------------------------------------------------------

def item_out(kind, r, today, place_title):
    exp, days, status = None, None, "ok"
    if kind == "pantry" and r["expires_on"]:
        try:
            days = (date.fromisoformat(r["expires_on"]) - today).days
            exp = r["expires_on"]
            status = "expired" if days < 0 else "soon" if days <= WARN_DAYS else "ok"
        except ValueError:
            pass
    return {
        "kind": kind, "id": r["id"], "name": r["name"], "quantity": r["quantity"],
        "unit": r["unit"] or "", "threshold": r["low_stock_threshold"],
        "low": r["quantity"] <= r["low_stock_threshold"], "out": r["quantity"] == 0,
        "expires_on": exp, "days_left": days, "exp_status": status,
        "location": (r["location"] or "") if kind == "pantry" else None,
        "bin_id": r["bin_id"] if kind == "bin" else None, "place": place_title,
    }


def build_state(conn):
    today = datetime.now(TZ).date()
    by_loc = {}
    for r in conn.execute("SELECT * FROM pantry_items ORDER BY lower(name)"):
        by_loc.setdefault(r["location"] or "", []).append(r)
    for d in DEFAULT_LOCS:
        by_loc.setdefault(d, [])
    order = DEFAULT_LOCS + sorted(k for k in by_loc if k and k not in DEFAULT_LOCS) + ([""] if "" in by_loc else [])

    places = []
    for loc in order:
        title = loc.capitalize() if loc else "Unsorted"
        places.append({
            "key": "loc:" + loc, "kind": "loc", "name": loc, "title": title,
            "path": "/loc/" + quote(loc or "unsorted", safe=""),
            "items": [item_out("pantry", r, today, title) for r in by_loc.get(loc, [])],
        })
    for b in conn.execute("SELECT * FROM bins ORDER BY lower(name)").fetchall():
        rows = conn.execute("SELECT * FROM items WHERE bin_id = ? ORDER BY lower(name)", (b["id"],)).fetchall()
        places.append({
            "key": f"bin:{b['id']}", "kind": "bin", "id": b["id"], "title": b["name"],
            "subtitle": b["location"] or "", "path": f"/bin/{b['id']}",
            "items": [item_out("bin", r, today, b["name"]) for r in rows],
        })

    every = [i for p in places for i in p["items"]]
    return {
        "places": places, "base_url": BASE_URL, "today": today.isoformat(), "warn_days": WARN_DAYS,
        "counts": {
            "low": sum(1 for i in every if i["low"]),
            "expired": sum(1 for i in every if i["exp_status"] == "expired"),
            "soon": sum(1 for i in every if i["exp_status"] == "soon"),
        },
    }


# ---- routes: pages -------------------------------------------------------------

def page():
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    return page()


@app.get("/loc/<path:name>")
def page_loc(name):  # stable NFC tag URL for a location
    return page()


@app.get("/bin/<int:bin_id>")
def page_bin(bin_id):  # stable NFC tag URL for a bin (id-based, so renaming never breaks a tag)
    return page()


@app.get("/healthz")
def healthz():
    return "ok"


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.before_request
def same_origin_only():
    """Refuse cross-site writes, so a web page open in your browser cannot edit your pantry."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        return jsonify(error="Cross-site request refused."), 403
    return None


@app.errorhandler(Bad)
def on_bad(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(sqlite3.IntegrityError)
def on_integrity(e):
    return jsonify(error="That name already exists there."), 409


@app.errorhandler(HTTPException)
def on_http(e):
    return jsonify(error=e.description), e.code


# ---- routes: items ---------------------------------------------------------------

@app.get("/api/state")
def api_state():
    conn = get_db()
    try:
        return jsonify(build_state(conn))
    finally:
        conn.close()


@app.post("/api/items")
def api_add_item():
    b = json_body()
    kind = b.get("kind")
    name = clean_name(b.get("name"))
    qty = clean_int(b.get("quantity", 0), "Quantity")
    unit = clean_unit(b.get("unit"))
    thr = clean_int(b.get("threshold", 0), "Low-stock level")
    with write_tx() as conn:
        if kind == "bin":
            bid = clean_int(b.get("bin_id"), "Bin", 1, 10**9)
            if not conn.execute("SELECT 1 FROM bins WHERE id = ?", (bid,)).fetchone():
                raise Bad("That bin no longer exists.")
            conn.execute(
                "INSERT INTO items (bin_id, name, quantity, unit, category, low_stock_threshold, updated_at)"
                " VALUES (?, ?, ?, ?, '', ?, ?)", (bid, name, qty, unit, thr, now_iso()))
        elif kind == "pantry":
            conn.execute(
                "INSERT INTO pantry_items (name, quantity, unit, location, low_stock_threshold, expires_on, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, qty, unit, clean_loc(b.get("location")), thr, clean_date(b.get("expires_on")), now_iso()))
        else:
            raise Bad("Unknown item type.")
        return jsonify(build_state(conn)), 201


@app.patch("/api/items/<kind>/<int:item_id>")
def api_patch_item(kind, item_id):
    if kind not in ("pantry", "bin"):
        raise Bad("Unknown item type.")
    table = "pantry_items" if kind == "pantry" else "items"
    b = json_body()
    sets, args = [], []
    if "delta" in b:  # atomic: two phones tapping + at once both count
        sets.append(f"quantity = MAX(0, MIN({MAX_QTY}, quantity + ?))")
        args.append(clean_int(b["delta"], "Change", -MAX_QTY, MAX_QTY))
    elif "quantity" in b:
        sets.append("quantity = ?")
        args.append(clean_int(b["quantity"], "Quantity"))
    if "name" in b:
        sets.append("name = ?")
        args.append(clean_name(b["name"]))
    if "unit" in b:
        sets.append("unit = ?")
        args.append(clean_unit(b["unit"]))
    if "threshold" in b:
        sets.append("low_stock_threshold = ?")
        args.append(clean_int(b["threshold"], "Low-stock level"))
    if kind == "pantry":
        if "location" in b:
            sets.append("location = ?")
            args.append(clean_loc(b["location"]))
        if "expires_on" in b:
            sets.append("expires_on = ?")
            args.append(clean_date(b["expires_on"]))
    if not sets:
        raise Bad("Nothing to change.")
    sets.append("updated_at = ?")
    args.append(now_iso())
    with write_tx() as conn:
        cur = conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", (*args, item_id))
        if cur.rowcount == 0:
            return jsonify(error="That item was already removed.", state=build_state(conn)), 404
        return jsonify(build_state(conn))


@app.delete("/api/items/<kind>/<int:item_id>")
def api_delete_item(kind, item_id):
    if kind not in ("pantry", "bin"):
        raise Bad("Unknown item type.")
    table = "pantry_items" if kind == "pantry" else "items"
    with write_tx() as conn:
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        return jsonify(build_state(conn))


def apply_line(conn, m, bin_id, default_loc, view_loc=None):
    """One shorthand line. Creates the item if new; a bare 'milk +2' never moves an existing item.

    default_loc is a standalone 'loc:x' line in the message and can move existing items, like the old bot.
    view_loc is just the place being looked at on screen: it only decides where a NEW item goes.
    """
    name = clean_name(m["name"])
    op, qty = m["op"], min(int(m["qty"]), MAX_QTY)
    if op == "=":
        sql_qty, arg = "?", qty
    else:
        sql_qty, arg = f"MAX(0, MIN({MAX_QTY}, quantity + ?))", (qty if op == "+" else -qty)
    if bin_id:
        row = conn.execute("SELECT id FROM items WHERE bin_id = ? AND lower(name) = lower(?)", (bin_id, name)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO items (bin_id, name, quantity, unit, category, low_stock_threshold, updated_at)"
                " VALUES (?, ?, ?, '', '', 0, ?)", (bin_id, name, qty if op in "+=" else 0, now_iso()))
        else:
            conn.execute(f"UPDATE items SET quantity = {sql_qty}, updated_at = ? WHERE id = ?", (arg, now_iso(), row["id"]))
        return
    explicit = m["loc"] or default_loc
    loc = clean_loc(explicit) if explicit else None          # moves an existing item
    exp = clean_date(m["exp"])
    row = conn.execute("SELECT id FROM pantry_items WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row is None:
        new_loc = loc if loc is not None else (clean_loc(view_loc) if view_loc else "")
        conn.execute(
            "INSERT INTO pantry_items (name, quantity, unit, location, low_stock_threshold, expires_on, updated_at)"
            " VALUES (?, ?, '', ?, 0, ?, ?)", (name, qty if op in "+=" else 0, new_loc, exp, now_iso()))
    else:
        sets, args = [f"quantity = {sql_qty}", "updated_at = ?"], [arg, now_iso()]
        if exp is not None:
            sets.append("expires_on = ?")
            args.append(exp)
        if loc is not None:
            sets.append("location = ?")
            args.append(loc)
        conn.execute(f"UPDATE pantry_items SET {', '.join(sets)} WHERE id = ?", (*args, row["id"]))


@app.post("/api/quick")
def api_quick():
    b = json_body()
    text = str(b.get("text") or "")
    if len(text) > 4000:
        raise Bad("That is too much text at once.")
    bin_id = clean_int(b["bin_id"], "Bin", 1, 10**9) if b.get("bin_id") else None
    default_loc = None
    todo, skipped = [], []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        lo = DEFAULT_LOC_PATTERN.match(ln)
        if lo:
            default_loc = lo["loc"]
            continue
        m = TEXT_PATTERN.match(ln)
        todo.append(m) if m else skipped.append(ln)
    view_loc = str(b["location"]) if b.get("location") else None  # the place on screen: only for new items
    applied = 0
    with write_tx() as conn:
        if bin_id and not conn.execute("SELECT 1 FROM bins WHERE id = ?", (bin_id,)).fetchone():
            raise Bad("That bin no longer exists.")
        for m in todo:
            try:
                conn.execute("SAVEPOINT line")
                apply_line(conn, m, bin_id, default_loc, view_loc)
                conn.execute("RELEASE line")
                applied += 1
            except Bad as e:
                conn.execute("ROLLBACK TO line")
                conn.execute("RELEASE line")
                skipped.append(f"{m.group(0)} ({e})")
        out = build_state(conn)
    out.update(applied=applied, skipped=skipped)
    return jsonify(out)


# ---- routes: bins ----------------------------------------------------------------

@app.post("/api/bins")
def api_add_bin():
    b = json_body()
    name = clean_name(b.get("name"), "Bin name")
    with write_tx() as conn:
        conn.execute("INSERT INTO bins (name, location) VALUES (?, ?)", (name, clean_name(b["location"], "Where") if b.get("location") else ""))
        return jsonify(build_state(conn)), 201


@app.patch("/api/bins/<int:bin_id>")
def api_patch_bin(bin_id):
    b = json_body()
    sets, args = [], []
    if "name" in b:
        sets.append("name = ?")
        args.append(clean_name(b["name"], "Bin name"))
    if "location" in b:
        sets.append("location = ?")
        args.append(clean_name(b["location"], "Where") if b["location"] else "")
    if not sets:
        raise Bad("Nothing to change.")
    with write_tx() as conn:
        conn.execute(f"UPDATE bins SET {', '.join(sets)} WHERE id = ?", (*args, bin_id))
        return jsonify(build_state(conn))


@app.delete("/api/bins/<int:bin_id>")
def api_delete_bin(bin_id):
    with write_tx() as conn:
        conn.execute("DELETE FROM bins WHERE id = ?", (bin_id,))  # items cascade
        return jsonify(build_state(conn))


init_db()
