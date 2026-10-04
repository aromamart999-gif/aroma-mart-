#!/usr/bin/env python3
"""AMORA MART's small, self-hosted storefront API and SQLite database."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape as xml_escape
from collections.abc import Iterator, Mapping

try:
    import turso_serverless
except ImportError:  # Local SQLite still works without the optional Vercel driver.
    turso_serverless = None


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "amora-mart.sqlite3"
UPLOAD_DIR = ROOT / "uploads"
SESSION_HOURS = 12
PASSWORD_ROUNDS = 310_000
MAX_BODY = 8 * 1024 * 1024
STATUSES = ("Order Received", "Confirmed", "Processing", "Shipped", "Out for Delivery", "Delivered", "Cancelled")
DEMO_IMAGES = {
    "home": "https://images.unsplash.com/photo-1586023492125-27b2c045efd7?auto=format&fit=crop&w=900&q=82",
    "kitchen": "https://images.unsplash.com/photo-1556911220-bff31c812dba?auto=format&fit=crop&w=900&q=82",
    "mobile": "https://images.unsplash.com/photo-1511707171634-5f897ff02aa9?auto=format&fit=crop&w=900&q=82",
    "fashion": "https://images.unsplash.com/photo-1611652022419-a9419f74343d?auto=format&fit=crop&w=900&q=82",
    "organizer": "https://images.unsplash.com/photo-1595428774223-ef52624120d2?auto=format&fit=crop&w=900&q=82",
    "lamp": "https://images.unsplash.com/photo-1507473885765-e6ed057f782c?auto=format&fit=crop&w=900&q=82",
    "bottle": "https://images.unsplash.com/photo-1602143407151-7111542de6e8?auto=format&fit=crop&w=900&q=82",
    "bag": "https://images.unsplash.com/photo-1548036328-c9fa89d128fa?auto=format&fit=crop&w=900&q=82",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DatabaseRow(Mapping):
    """SQLite-style row supporting integer and column-name access."""

    def __init__(self, cursor, row):
        self._values = tuple(row)
        self._keys = tuple(column[0] for column in (cursor.description or ()))
        self._by_name = dict(zip(self._keys, self._values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._by_name[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


def make_row(cursor, row) -> DatabaseRow:
    return DatabaseRow(cursor, row)


DB_ERRORS = (sqlite3.Error,) + ((turso_serverless.Error,) if turso_serverless else ())
DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + ((turso_serverless.IntegrityError,) if turso_serverless else ())


class ClosingConnection:
    """Preserve DB-API context behavior and close remote HTTP sessions."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self._connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self._connection.close()


def connect():
    database_url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    database_token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if database_url or database_token:
        if not (database_url and database_token):
            raise sqlite3.OperationalError("Both Turso database settings must be configured.")
        if turso_serverless is None:
            raise sqlite3.OperationalError("The remote database driver is not installed.")
        conn = turso_serverless.connect(database_url, auth_token=database_token)
        conn.row_factory = make_row
        conn.execute("PRAGMA foreign_keys=ON")
        return ClosingConnection(conn)
    if os.environ.get("VERCEL") == "1":
        raise sqlite3.OperationalError("Add TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in Vercel project settings.")
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = make_row
    conn.execute("PRAGMA foreign_keys=ON")
    return ClosingConnection(conn)


def init_db() -> None:
    remote_database = bool(os.environ.get("TURSO_DATABASE_URL") and os.environ.get("TURSO_AUTH_TOKEN"))
    if not remote_database and os.environ.get("VERCEL") != "1":
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS admins (
              username TEXT PRIMARY KEY, salt TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at REAL NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS categories (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, slug TEXT NOT NULL UNIQUE,
              description TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS products (
              id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
              category TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', mrp INTEGER NOT NULL,
              price INTEGER NOT NULL, supplier_cost INTEGER NOT NULL DEFAULT 0, images TEXT NOT NULL DEFAULT '[]',
              variants TEXT NOT NULL DEFAULT '[]', specs TEXT NOT NULL DEFAULT '[]', stock_status TEXT NOT NULL DEFAULT 'in_stock',
              cod INTEGER NOT NULL DEFAULT 1, featured INTEGER NOT NULL DEFAULT 0, bestseller INTEGER NOT NULL DEFAULT 0,
              active INTEGER NOT NULL DEFAULT 1, rating REAL NOT NULL DEFAULT 4.6, reviews INTEGER NOT NULL DEFAULT 0,
              is_demo INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL UNIQUE, customer_token_hash TEXT NOT NULL UNIQUE,
              full_name TEXT NOT NULL, phone TEXT NOT NULL, alternate_phone TEXT NOT NULL DEFAULT '',
              address TEXT NOT NULL, payment_method TEXT NOT NULL, payment_status TEXT NOT NULL,
              subtotal INTEGER NOT NULL, shipping INTEGER NOT NULL, discount INTEGER NOT NULL, total INTEGER NOT NULL,
              status TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
              product_id INTEGER REFERENCES products(id) ON DELETE SET NULL, product_name TEXT NOT NULL,
              product_slug TEXT NOT NULL, variant TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL,
              price INTEGER NOT NULL, mrp INTEGER NOT NULL, image TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
        """)
        for name, slug, description, image, order in [
            ("Home & Living", "home-living", "Thoughtful finds for a calmer home", DEMO_IMAGES["home"], 1),
            ("Kitchen", "kitchen", "Little upgrades for everyday cooking", DEMO_IMAGES["kitchen"], 2),
            ("Mobile & Tech", "mobile-tech", "Handy accessories for your everyday", DEMO_IMAGES["mobile"], 3),
            ("Fashion & Accessories", "fashion-accessories", "Finishing touches, made easy", DEMO_IMAGES["fashion"], 4),
            ("Everyday Essentials", "everyday-essentials", "Useful things for daily life", DEMO_IMAGES["organizer"], 5),
        ]:
            db.execute("INSERT OR IGNORE INTO categories(name,slug,description,image,sort_order) VALUES(?,?,?,?,?)", (name, slug, description, image, order))
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('shop_name','AMORA MART')")
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('whatsapp_phone','')")
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('support_email','')")
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('delivery_charge','49')")
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('free_delivery_threshold','999')")
        if db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
            demo_products = [
                ("Everyday Storage Basket Set", "home-living", "A versatile set of woven-look storage baskets to bring a little order to shelves, counters and cupboards.", 799, 449, 235, [DEMO_IMAGES["home"], DEMO_IMAGES["organizer"]], ["Set of 3", "Set of 5"], ["Material: PP weave", "Lightweight and easy to clean", "Colour: Natural"], 1, 1, 4.8, 124),
                ("Compact Vegetable Chopper", "kitchen", "A handy manual chopper for quick everyday prep. Compact enough to keep within easy reach.", 699, 349, 175, [DEMO_IMAGES["kitchen"]], ["500 ml", "900 ml"], ["Manual pull-cord design", "Easy-clean bowl", "Includes blade insert"], 1, 1, 4.6, 86),
                ("Foldable Desk Phone Stand", "mobile-tech", "Keep your screen at a comfortable viewing angle at your desk, bedside or kitchen counter.", 499, 249, 112, [DEMO_IMAGES["mobile"]], ["Black", "White"], ["Adjustable angle", "Fold-flat design", "For most smartphones"], 1, 0, 4.5, 63),
                ("Minimal Pendant Necklace", "fashion-accessories", "An easy-to-style everyday piece with a clean silhouette and a soft, polished finish.", 899, 399, 205, [DEMO_IMAGES["fashion"], DEMO_IMAGES["bag"]], ["Gold tone", "Silver tone"], ["Lightweight design", "Adjustable chain", "Store dry between wears"], 1, 1, 4.7, 98),
                ("Clear Drawer Organizer Set", "home-living", "Make small essentials easier to find with a modular set sized for drawers, vanities and workspaces.", 649, 329, 160, [DEMO_IMAGES["organizer"]], ["Set of 4", "Set of 8"], ["Clear finish", "Mix-and-match trays", "Wipe-clean surface"], 1, 0, 4.4, 42),
                ("Warm Glow Table Lamp", "everyday-essentials", "A compact accent lamp for a gentle evening glow on side tables, desks and reading corners.", 1299, 699, 390, [DEMO_IMAGES["lamp"]], ["Cream", "Sage"], ["Warm ambient light", "Compact tabletop size", "Bulb details on packaging"], 1, 1, 4.7, 75),
                ("Leak-Resistant Travel Bottle", "everyday-essentials", "A simple reusable bottle for your commute, errands and daily routine.", 599, 299, 145, [DEMO_IMAGES["bottle"]], ["500 ml", "750 ml"], ["Easy-carry cap", "Reusable design", "Hand wash recommended"], 1, 0, 4.3, 51),
                ("Everyday Crossbody Bag", "fashion-accessories", "A practical hands-free bag with room for the essentials and a clean, versatile shape.", 1199, 649, 340, [DEMO_IMAGES["bag"]], ["Tan", "Black"], ["Adjustable strap", "Zip closure", "Colour may vary slightly by screen"], 1, 1, 4.6, 111),
            ]
            for name, category, desc, mrp, price, cost, images, variants, specs, feat, best, rating, reviews in demo_products:
                slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                db.execute("""INSERT OR IGNORE INTO products(slug,name,category,description,mrp,price,supplier_cost,images,variants,specs,stock_status,cod,featured,bestseller,active,rating,reviews,is_demo,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,1,?)""", (slug, name, category, desc, mrp, price, cost, json.dumps(images), json.dumps(variants), json.dumps(specs), "in_stock", 1, feat, best, 0, 0, utc_now()))
        # The shop owner's first supplied item is real catalogue content, not a demo.
        rack_images = ["/uploads/shoe-rack-main.webp", "/uploads/shoe-rack-details.webp", "/uploads/shoe-rack-before-after.webp"]
        db.execute("""INSERT OR IGNORE INTO products(slug,name,category,description,mrp,price,supplier_cost,images,variants,specs,stock_status,cod,featured,bestseller,active,rating,reviews,is_demo,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            "large-capacity-roll-up-door-shoe-rack", "Large-Capacity Roll-Up Door Shoe Rack", "Home & Living",
            "A tall shoe organiser designed to keep everyday pairs together and make the most of your entryway or bedroom space. The roll-up door keeps shoes tucked away while making them easy to reach. The supplied product image states capacity for up to 24 pairs; the number that fits depends on shoe size.",
            847, 847, 0, json.dumps(rack_images), json.dumps([]),
            json.dumps(["Capacity: up to 24 pairs as shown in supplied product image", "Roll-up door for easy access", "Space-saving upright design", "Colour and finish as shown in supplied photos"]),
            "in_stock", 1, 1, 0, 1, 0, 0, 0, utc_now()))


def rowdict(row: sqlite3.Row | None) -> dict:
    return dict(row) if row else {}


def product_public(row: sqlite3.Row) -> dict:
    item = rowdict(row)
    for field in ("images", "variants", "specs"):
        item[field] = json.loads(item.get(field) or "[]")
    item.pop("supplier_cost", None)
    item.pop("active", None)
    item["discount"] = max(0, round((item["mrp"] - item["price"]) * 100 / item["mrp"])) if item["mrp"] else 0
    return item


def get_settings(db: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM settings")}


def slugify(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-") or secrets.token_hex(4)


class Handler(BaseHTTPRequestHandler):
    server_version = "AmoraStore/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        # Request targets can include the private customer order-access token.
        sys.stdout.write("%s - request handled\n" % self.address_string())

    def _headers(self, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https://images.unsplash.com https://*.public.blob.vercel-storage.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store")

    def json(self, payload, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._headers()
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def fail(self, message: str, status: int = 400) -> None:
        self.json({"error": message}, status)

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ValueError("This request is too large.")
        if length == 0:
            return {}
        try:
            obj = json.loads(self.rfile.read(length))
            if not isinstance(obj, dict):
                raise ValueError("Invalid request data.")
            return obj
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Invalid request data.")

    def session(self, require_csrf: bool = False):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get("amora_admin")
        if not morsel:
            return None
        token_hash = hashlib.sha256(morsel.value.encode()).hexdigest()
        with connect() as db:
            sess = db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (token_hash, time.time())).fetchone()
        if not sess:
            return None
        if require_csrf and not hmac.compare_digest(sess["csrf"], self.headers.get("X-CSRF-Token", "")):
            self.fail("Your admin session expired. Please sign in again.", 403)
            return None
        return sess

    def admin(self, require_csrf: bool = False):
        sess = self.session()
        if not sess:
            self.fail("Please sign in to access the admin dashboard.", 401)
            return False
        if require_csrf and not hmac.compare_digest(sess["csrf"], self.headers.get("X-CSRF-Token", "")):
            self.fail("Your admin session expired. Please sign in again.", 403)
            return False
        return True

    def _route(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        return path, query

    def do_GET(self):
        path, query = self._route()
        try:
            if path == "/api/products":
                with connect() as db:
                    rows = db.execute("SELECT * FROM products WHERE active=1 ORDER BY featured DESC,bestseller DESC,id DESC").fetchall()
                    self.json([product_public(r) for r in rows]); return
            if path == "/api/categories":
                with connect() as db:
                    rows = db.execute("SELECT id,name,slug,description,image FROM categories ORDER BY sort_order,id").fetchall()
                    self.json([rowdict(r) for r in rows]); return
            if path == "/api/settings":
                with connect() as db:
                    settings = get_settings(db)
                    self.json({"shop_name": settings.get("shop_name", "AMORA MART"), "whatsapp_phone": settings.get("whatsapp_phone", ""), "support_email": settings.get("support_email", ""), "delivery_charge": int(settings.get("delivery_charge", "49")), "free_delivery_threshold": int(settings.get("free_delivery_threshold", "999")), "online_payment_enabled": False}); return
            if path == "/api/admin/session":
                sess = self.session()
                self.json({"authenticated": bool(sess), "csrf": sess["csrf"] if sess else ""}); return
            if path == "/api/admin/setup-status":
                with connect() as db:
                    has_admin = db.execute("SELECT COUNT(*) FROM admins").fetchone()[0] > 0
                self.json({"complete": has_admin, "enabled": bool(os.environ.get("AMORA_ADMIN_SETUP_TOKEN")) and not has_admin}); return
            if path == "/api/admin/dashboard":
                if not self.admin(): return
                with connect() as db:
                    total_orders = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
                    counts = {r["status"]: r["n"] for r in db.execute("SELECT status,COUNT(*) n FROM orders GROUP BY status")}
                    sales = db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='Cancelled'").fetchone()[0]
                    recent = [self.order_record(db, r) for r in db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 6")]
                    top = [rowdict(r) for r in db.execute("SELECT product_name name,SUM(quantity) units,SUM(quantity*price) sales FROM order_items GROUP BY product_name ORDER BY units DESC LIMIT 5")]
                    self.json({"total_orders": total_orders, "new_orders": counts.get("Order Received", 0), "pending_orders": counts.get("Processing", 0) + counts.get("Confirmed", 0) + counts.get("Shipped", 0) + counts.get("Out for Delivery", 0), "confirmed_orders": counts.get("Confirmed", 0), "delivered_orders": counts.get("Delivered", 0), "cancelled_orders": counts.get("Cancelled", 0), "total_sales": sales, "recent_orders": recent, "best_sellers": top, "status_counts": counts}); return
            if path == "/api/admin/orders":
                if not self.admin(): return
                with connect() as db:
                    rows = db.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
                    self.json([self.order_record(db, r) for r in rows]); return
            if path == "/api/admin/products":
                if not self.admin(): return
                with connect() as db:
                    rows = db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
                    self.json([self.product_record(r) for r in rows]); return
            if path == "/api/admin/categories":
                if not self.admin(): return
                with connect() as db:
                    self.json([rowdict(r) for r in db.execute("SELECT * FROM categories ORDER BY sort_order,id")]); return
            if path == "/api/admin/customers":
                if not self.admin(): return
                with connect() as db:
                    rows = db.execute("SELECT full_name,phone,COUNT(*) orders,MAX(created_at) last_order, SUM(CASE WHEN status!='Cancelled' THEN total ELSE 0 END) spent FROM orders GROUP BY phone ORDER BY last_order DESC").fetchall()
                    self.json([rowdict(r) for r in rows]); return
            if path == "/api/admin/settings":
                if not self.admin(): return
                with connect() as db:
                    settings = get_settings(db)
                    self.json({"shop_name": settings.get("shop_name", "AMORA MART"), "whatsapp_phone": settings.get("whatsapp_phone", ""), "support_email": settings.get("support_email", ""), "delivery_charge": int(settings.get("delivery_charge", "49")), "free_delivery_threshold": int(settings.get("free_delivery_threshold", "999"))}); return
            if path == "/api/admin/payments":
                if not self.admin(): return
                with connect() as db:
                    self.json({"cod_orders": db.execute("SELECT COUNT(*) FROM orders WHERE payment_method='COD'").fetchone()[0], "cod_pending": db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE payment_method='COD' AND payment_status='Pending'").fetchone()[0], "online_enabled": False, "online_message": "Online payments are disabled until a real payment gateway is configured."}); return
            if path == "/api/admin/analytics":
                if not self.admin(): return
                with connect() as db:
                    by_day = [rowdict(r) for r in db.execute("SELECT substr(created_at,1,10) day,COUNT(*) orders,COALESCE(SUM(CASE WHEN status!='Cancelled' THEN total ELSE 0 END),0) sales FROM orders GROUP BY day ORDER BY day DESC LIMIT 14")]
                    self.json({"days": by_day}); return
            if path.startswith("/api/order/"):
                order_id = path.rsplit("/", 1)[-1]
                authorization = self.headers.get("Authorization", "")
                token = authorization[7:] if authorization.lower().startswith("bearer ") else query.get("token", [""])[0]
                if len(token) < 30:
                    self.fail("Order not found.", 404); return
                with connect() as db:
                    row = db.execute("SELECT * FROM orders WHERE order_id=? AND customer_token_hash=?", (order_id, hashlib.sha256(token.encode()).hexdigest())).fetchone()
                    if not row:
                        self.fail("Order not found.", 404); return
                    self.json(self.order_record(db, row, customer=True)); return
            if path == "/api/my-orders":
                try:
                    body = self.body()
                    tokens = body.get("tokens", [])
                    if not isinstance(tokens, list): raise ValueError()
                    hashes = [hashlib.sha256(t.encode()).hexdigest() for t in tokens[:30] if isinstance(t, str) and len(t) >= 30]
                except ValueError:
                    self.fail("Invalid order access information."); return
                with connect() as db:
                    records = []
                    for h in hashes:
                        row = db.execute("SELECT * FROM orders WHERE customer_token_hash=?", (h,)).fetchone()
                        if row: records.append(self.order_record(db, row, customer=True))
                    self.json(records); return
            if path.startswith("/api/product/"):
                slug = path.rsplit("/", 1)[-1]
                with connect() as db:
                    row = db.execute("SELECT * FROM products WHERE slug=? AND active=1", (slug,)).fetchone()
                    if row: self.json(product_public(row))
                    else: self.fail("Product not found.", 404)
                return
            self.serve_file(path)
        except DB_ERRORS:
            self.fail("The store database is temporarily unavailable.", 500)

    def product_record(self, row: sqlite3.Row) -> dict:
        item = rowdict(row)
        for field in ("images", "variants", "specs"):
            item[field] = json.loads(item.get(field) or "[]")
        item["discount"] = max(0, round((item["mrp"] - item["price"]) * 100 / item["mrp"])) if item["mrp"] else 0
        return item

    def order_record(self, db: sqlite3.Connection, row: sqlite3.Row, customer: bool = False) -> dict:
        order = rowdict(row)
        order["address"] = json.loads(order["address"])
        order["items"] = [rowdict(item) for item in db.execute("SELECT product_id,product_name,product_slug,variant,quantity,price,mrp,image FROM order_items WHERE order_id=?", (row["id"],))]
        if not customer:
            order.pop("customer_token_hash", None)
        else:
            order.pop("customer_token_hash", None)
            order.pop("notes", None)
        return order

    def serve_file(self, url_path: str) -> None:
        if url_path == "/sitemap.xml":
            configured = os.environ.get("AMORA_PUBLIC_URL", "").rstrip("/")
            if configured:
                parsed = urlsplit(configured)
                base = configured if parsed.scheme in ("http", "https") and parsed.netloc else ""
            else:
                host = self.headers.get("Host", "127.0.0.1:4173")
                if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
                    host = "127.0.0.1:4173"
                scheme = "https" if os.environ.get("AMORA_COOKIE_SECURE") == "1" else "http"
                base = f"{scheme}://{host}"
            with connect() as db:
                slugs = [r["slug"] for r in db.execute("SELECT slug FROM products WHERE active=1")]
            locations = ["/", "/shop", "/privacy", "/terms", "/shipping", "/returns", "/contact"] + [f"/product/{slug}" for slug in slugs]
            urls = "".join(f"<url><loc>{xml_escape(base + path)}</loc></url>" for path in locations)
            payload = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode("utf-8")
            self.send_response(200)
            self._headers("application/xml; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        allowed_root = ROOT
        if url_path == "/admin/setup":
            target = ROOT / "admin-setup.html"
        elif url_path in ("/", "/admin", "/admin/", "/shop", "/cart", "/checkout", "/orders", "/track", "/privacy", "/terms", "/shipping", "/returns", "/contact") or url_path.startswith("/product/") or url_path.startswith("/track/") or url_path.startswith("/category/"):
            target = ROOT / "public" / "index.html"
        elif url_path.startswith("/assets/"):
            allowed_root = ROOT / "assets"
            target = allowed_root / url_path.removeprefix("/assets/")
        elif url_path.startswith("/uploads/"):
            allowed_root = UPLOAD_DIR
            target = allowed_root / url_path.removeprefix("/uploads/")
        elif url_path == "/robots.txt":
            target = ROOT / "robots.txt"
        elif url_path == "/404.html":
            target = ROOT / "public" / "404.html"
        else:
            try:
                payload = (ROOT / "public" / "404.html").read_bytes()
            except (FileNotFoundError, PermissionError):
                self.fail("Page not found.", 404); return
            self.send_response(404)
            self._headers("text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        try:
            target = target.resolve(strict=True)
            if not target.is_relative_to(allowed_root) or not target.is_file():
                self.fail("Page not found.", 404); return
            payload = target.read_bytes()
        except (FileNotFoundError, PermissionError):
            self.fail("Page not found.", 404); return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self._headers(content_type + ("; charset=utf-8" if content_type.startswith(("text/", "application/javascript")) else ""))
        if target.name in ("app.js", "styles.css") or target.name == "index.html":
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers(); self.wfile.write(payload)

    def do_POST(self):
        path, _ = self._route()
        try:
            data = self.body()
            if path == "/api/admin/login":
                username = str(data.get("username", "")).strip()[:120]
                password = str(data.get("password", ""))[:200]
                address = self.client_address[0]
                fails, locked_until = LOGIN_LIMIT.get(address, (0, 0))
                if locked_until > time.time():
                    self.fail("Too many sign-in attempts. Please wait a few minutes and try again.", 429); return
                with connect() as db:
                    admin = db.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
                valid = False
                if admin:
                    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(admin["salt"]), PASSWORD_ROUNDS).hex()
                    valid = hmac.compare_digest(candidate, admin["password_hash"])
                if not valid:
                    fails += 1
                    LOGIN_LIMIT[address] = (fails, time.time() + 900 if fails >= 5 else 0)
                    self.fail("That username or password is not correct.", 401); return
                LOGIN_LIMIT.pop(address, None)
                raw = secrets.token_urlsafe(40)
                csrf = secrets.token_urlsafe(24)
                expires = time.time() + SESSION_HOURS * 3600
                with connect() as db:
                    db.execute("INSERT INTO sessions(token_hash,csrf,expires_at,created_at) VALUES(?,?,?,?)", (hashlib.sha256(raw.encode()).hexdigest(), csrf, expires, utc_now()))
                secure = "; Secure" if os.environ.get("AMORA_COOKIE_SECURE") == "1" or os.environ.get("VERCEL") == "1" else ""
                self.json({"authenticated": True, "csrf": csrf}, extra_headers={"Set-Cookie": f"amora_admin={raw}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_HOURS*3600}{secure}"}); return
            if path == "/api/admin/setup":
                setup_token = os.environ.get("AMORA_ADMIN_SETUP_TOKEN", "")
                supplied_token = str(data.get("setup_token", ""))[:300]
                username = str(data.get("username", "")).strip()[:120]
                password = str(data.get("password", ""))[:200]
                if len(setup_token) < 32:
                    self.fail("First-time admin setup is not enabled. Ask the site owner to configure it in Vercel.", 503); return
                if len(supplied_token) < 32 or not hmac.compare_digest(setup_token, supplied_token):
                    self.fail("That setup code is not correct.", 401); return
                if not username or len(password) < 12:
                    self.fail("Enter a username and a password with at least 12 characters."); return
                with connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT COUNT(*) FROM admins").fetchone()[0]:
                        self.fail("Admin setup is already complete. Sign in at /admin.", 409); return
                    salt = secrets.token_bytes(16)
                    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ROUNDS).hex()
                    db.execute("INSERT INTO admins(username,salt,password_hash,created_at) VALUES(?,?,?,?)", (username, salt.hex(), password_hash, utc_now()))
                self.json({"ok": True}); return
            if path == "/api/admin/logout":
                sess = self.session(require_csrf=True)
                if not sess: return
                cookie = SimpleCookie(); cookie.load(self.headers.get("Cookie", ""))
                raw = cookie["amora_admin"].value
                with connect() as db: db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(raw.encode()).hexdigest(),))
                self.json({"ok": True}, extra_headers={"Set-Cookie": "amora_admin=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"}); return
            if path == "/api/orders":
                self.create_order(data); return
            if path == "/api/track-order":
                self.lookup_order(data); return
            if path == "/api/admin/upload":
                if not self.admin(require_csrf=True): return
                self.upload_image(data); return
            if path == "/api/admin/products":
                if not self.admin(require_csrf=True): return
                self.save_product(data); return
            if path == "/api/admin/categories":
                if not self.admin(require_csrf=True): return
                self.save_category(data); return
            if path == "/api/admin/settings":
                if not self.admin(require_csrf=True): return
                self.save_settings(data); return
            if path == "/api/my-orders":
                tokens = data.get("tokens", [])
                if not isinstance(tokens, list): self.fail("Invalid order access information."); return
                hashes = [hashlib.sha256(t.encode()).hexdigest() for t in tokens[:30] if isinstance(t, str) and len(t) >= 30]
                with connect() as db:
                    records = []
                    for h in hashes:
                        row = db.execute("SELECT * FROM orders WHERE customer_token_hash=?", (h,)).fetchone()
                        if row: records.append(self.order_record(db, row, customer=True))
                    self.json(records); return
            self.fail("Endpoint not found.", 404)
        except ValueError as exc:
            self.fail(str(exc) or "Please check your information.")
        except DB_INTEGRITY_ERRORS:
            self.fail("We could not save that record. Please check the details and try again.")
        except DB_ERRORS:
            self.fail("The store database is temporarily unavailable.", 500)

    def do_PUT(self):
        path, _ = self._route()
        try:
            data = self.body()
            if not self.admin(require_csrf=True): return
            if path.startswith("/api/admin/orders/"):
                order_id = path.rsplit("/", 1)[-1]
                status = str(data.get("status", ""))
                notes = str(data.get("notes", ""))[:4000]
                if status not in STATUSES:
                    self.fail("Select a valid order status."); return
                with connect() as db:
                    cur = db.execute("UPDATE orders SET status=?,notes=? WHERE order_id=?", (status, notes, order_id))
                    if cur.rowcount == 0: self.fail("Order not found.", 404)
                    else: self.json({"ok": True})
                return
            if path.startswith("/api/admin/products/"):
                product_id = path.rsplit("/", 1)[-1]
                self.save_product(data, product_id); return
            if path.startswith("/api/admin/categories/"):
                category_id = path.rsplit("/", 1)[-1]
                self.save_category(data, category_id); return
            if path == "/api/admin/settings":
                self.save_settings(data); return
            self.fail("Endpoint not found.", 404)
        except ValueError as exc:
            self.fail(str(exc) or "Please check your information.")
        except DB_INTEGRITY_ERRORS:
            self.fail("That name is already in use. Please choose another.")
        except DB_ERRORS:
            self.fail("The store database is temporarily unavailable.", 500)

    def do_DELETE(self):
        path, _ = self._route()
        if not self.admin(require_csrf=True): return
        try:
            with connect() as db:
                if path.startswith("/api/admin/products/"):
                    product_id = path.rsplit("/", 1)[-1]
                    cur = db.execute("UPDATE products SET active=0 WHERE id=?", (product_id,))
                elif path.startswith("/api/admin/categories/"):
                    category_id = path.rsplit("/", 1)[-1]
                    cat = db.execute("SELECT name FROM categories WHERE id=?", (category_id,)).fetchone()
                    if cat:
                        db.execute("UPDATE products SET category='uncategorized' WHERE category=?", (cat["name"],))
                    cur = db.execute("DELETE FROM categories WHERE id=?", (category_id,))
                else:
                    self.fail("Endpoint not found.", 404); return
                if cur.rowcount == 0: self.fail("Item not found.", 404)
                else: self.json({"ok": True})
        except DB_INTEGRITY_ERRORS:
            self.fail("This item is in use and cannot be removed.")

    def lookup_order(self, data: dict) -> None:
        """Allow safe cross-device tracking with an order reference and matching phone."""
        order_id = str(data.get("order_id", "")).strip().upper()
        phone = re.sub(r"[\s()-]", "", str(data.get("phone", "")))
        if not re.fullmatch(r"ORD-\d{8}-\d{1,}", order_id) or not re.fullmatch(r"(?:\+91)?[6-9]\d{9}", phone):
            self.fail("We couldn’t match that order. Check the order ID and mobile number.", 404); return

        address = self.client_address[0]
        attempts, reset_at = TRACK_LOOKUP_LIMIT.get(address, (0, 0))
        now = time.time()
        if reset_at <= now:
            attempts, reset_at = 0, now + 900
        if attempts >= 20:
            self.fail("Too many lookup attempts. Please try again in 15 minutes.", 429); return

        with connect() as db:
            row = db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if not row or not hmac.compare_digest(row["phone"], phone):
                TRACK_LOOKUP_LIMIT[address] = (attempts + 1, reset_at)
                self.fail("We couldn’t match that order. Check the order ID and mobile number.", 404); return
            TRACK_LOOKUP_LIMIT.pop(address, None)
            items = [dict(r) for r in db.execute(
                "SELECT product_name,variant,quantity,price FROM order_items WHERE order_id=? ORDER BY id",
                (row["id"],),
            ).fetchall()]
            self.json({
                "order_id": row["order_id"], "created_at": row["created_at"],
                "status": row["status"], "payment_method": row["payment_method"],
                "payment_status": row["payment_status"], "total": row["total"],
                "items": items,
            })

    def create_order(self, data: dict) -> None:
        customer = str(data.get("full_name", "")).strip()
        phone = re.sub(r"[\s()-]", "", str(data.get("phone", "")))
        alternate = re.sub(r"[\s()-]", "", str(data.get("alternate_phone", "")))[:20]
        address = data.get("address", {})
        if not customer or len(customer) > 120 or not re.fullmatch(r"(?:\+91)?[6-9]\d{9}", phone):
            raise ValueError("Enter your full name and a valid mobile number.")
        if not isinstance(address, dict): raise ValueError("Enter your delivery address.")
        required = ("house", "street", "locality", "city", "district", "state", "pin")
        addr = {key: str(address.get(key, "")).strip()[:150] for key in required}
        addr["landmark"] = str(address.get("landmark", "")).strip()[:150]
        if any(not addr[k] for k in required): raise ValueError("Complete all required delivery address fields.")
        if not re.fullmatch(r"\d{6}", addr["pin"]): raise ValueError("Enter a valid 6-digit PIN code.")
        if not data.get("confirmed_details"): raise ValueError("Please confirm your delivery details before placing the order.")
        if data.get("payment_method") != "COD": raise ValueError("Online payment is unavailable right now. Please select Cash on Delivery.")
        raw_items = data.get("items")
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 30: raise ValueError("Your cart is empty.")
        clean = []
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            subtotal = mrp_total = 0
            for item in raw_items:
                try:
                    product_id = int(item.get("product_id")); quantity = int(item.get("quantity"))
                except (ValueError, TypeError, AttributeError):
                    raise ValueError("Please review the products in your cart.")
                if not 1 <= quantity <= 20: raise ValueError("Each product quantity must be between 1 and 20.")
                product = db.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
                if not product or product["stock_status"] != "in_stock": raise ValueError("A product in your cart is currently unavailable.")
                if not product["cod"]: raise ValueError("A product in your cart is not available for Cash on Delivery.")
                variants = json.loads(product["variants"] or "[]")
                variant = str(item.get("variant", "")).strip()[:100]
                if variants and variant not in variants: raise ValueError("Please select an available product option.")
                if not variants: variant = "Standard"
                subtotal += product["price"] * quantity
                mrp_total += product["mrp"] * quantity
                clean.append((product, variant, quantity))
            settings = get_settings(db)
            ship = 0 if subtotal >= int(settings.get("free_delivery_threshold", "999")) else int(settings.get("delivery_charge", "49"))
            discount = max(0, mrp_total - subtotal)
            total = subtotal + ship
            today = datetime.now().strftime("%Y%m%d")
            daily = db.execute("SELECT COUNT(*) FROM orders WHERE order_id LIKE ?", (f"ORD-{today}-%",)).fetchone()[0] + 1
            order_id = f"ORD-{today}-{daily:04d}"
            access_token = secrets.token_urlsafe(36)
            cursor = db.execute("""INSERT INTO orders(order_id,customer_token_hash,full_name,phone,alternate_phone,address,payment_method,payment_status,subtotal,shipping,discount,total,status,created_at)
              VALUES(?,?,?,?,?,?,'COD','Pending',?,?,?,?,?,?)""", (order_id, hashlib.sha256(access_token.encode()).hexdigest(), customer, phone, alternate, json.dumps(addr, ensure_ascii=False), subtotal, ship, discount, total, "Order Received", utc_now()))
            for product, variant, quantity in clean:
                images = json.loads(product["images"] or "[]")
                db.execute("INSERT INTO order_items(order_id,product_id,product_name,product_slug,variant,quantity,price,mrp,image) VALUES(?,?,?,?,?,?,?,?,?)", (cursor.lastrowid, product["id"], product["name"], product["slug"], variant, quantity, product["price"], product["mrp"], images[0] if images else ""))
            result = self.order_record(db, db.execute("SELECT * FROM orders WHERE id=?", (cursor.lastrowid,)).fetchone(), customer=True)
        self.json({"order": result, "access_token": access_token}, 201)

    def save_product(self, data: dict, product_id: str | None = None) -> None:
        name = str(data.get("name", "")).strip()[:180]
        if not name: raise ValueError("Enter a product name.")
        try:
            price = int(data.get("price")); mrp = int(data.get("mrp")); cost = max(0, int(data.get("supplier_cost", 0)))
        except (ValueError, TypeError): raise ValueError("Enter valid prices in rupees.")
        if price < 1 or mrp < price or mrp > 5_000_000: raise ValueError("MRP must be equal to or higher than the selling price.")
        images = data.get("images", [])
        variants = data.get("variants", [])
        specs = data.get("specs", [])
        if not isinstance(images, list) or not all(isinstance(x, str) and len(x) < 800 for x in images[:12]): raise ValueError("Add valid product images.")
        if not isinstance(variants, list) or not all(isinstance(x, str) and len(x) <= 100 for x in variants[:30]): raise ValueError("Enter valid product variants.")
        if not isinstance(specs, list) or not all(isinstance(x, str) and len(x) <= 200 for x in specs[:30]): raise ValueError("Enter valid product specifications.")
        category = str(data.get("category", "Everyday Essentials"))[:100]
        cod = 1 if data.get("cod", True) else 0
        stock = "out_of_stock" if data.get("stock_status") == "out_of_stock" else "in_stock"
        featured = int(bool(data.get("featured"))); bestseller = int(bool(data.get("bestseller")))
        active = int(bool(data.get("active", True)))
        demo = int(bool(data.get("is_demo", False)))
        slug = slugify(str(data.get("slug") or name))
        values = (slug, name, category, str(data.get("description", ""))[:6000], mrp, price, cost, json.dumps(images[:12]), json.dumps(variants[:30]), json.dumps(specs[:30]), stock, cod, featured, bestseller, active, demo)
        with connect() as db:
            if product_id:
                cur = db.execute("""UPDATE products SET slug=?,name=?,category=?,description=?,mrp=?,price=?,supplier_cost=?,images=?,variants=?,specs=?,stock_status=?,cod=?,featured=?,bestseller=?,active=?,is_demo=? WHERE id=?""", (*values, int(product_id)))
                if cur.rowcount == 0: self.fail("Product not found.", 404); return
            else:
                db.execute("""INSERT INTO products(slug,name,category,description,mrp,price,supplier_cost,images,variants,specs,stock_status,cod,featured,bestseller,active,is_demo,rating,reviews,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,4.6,0,?)""", (*values, utc_now()))
        self.json({"ok": True})

    def save_category(self, data: dict, category_id: str | None = None) -> None:
        name = str(data.get("name", "")).strip()[:100]
        if not name: raise ValueError("Enter a category name.")
        slug = slugify(str(data.get("slug") or name))
        description = str(data.get("description", ""))[:300]
        image = str(data.get("image", ""))[:800]
        sort_order = int(data.get("sort_order", 0))
        with connect() as db:
            if category_id:
                old = db.execute("SELECT name FROM categories WHERE id=?", (int(category_id),)).fetchone()
                if not old: self.fail("Category not found.", 404); return
                db.execute("UPDATE categories SET name=?,slug=?,description=?,image=?,sort_order=? WHERE id=?", (name, slug, description, image, sort_order, int(category_id)))
                if old["name"] != name: db.execute("UPDATE products SET category=? WHERE category=?", (name, old["name"]))
            else:
                db.execute("INSERT INTO categories(name,slug,description,image,sort_order) VALUES(?,?,?,?,?)", (name, slug, description, image, sort_order))
        self.json({"ok": True})

    def save_settings(self, data: dict) -> None:
        shop = str(data.get("shop_name", "AMORA MART")).strip()[:100] or "AMORA MART"
        wa = re.sub(r"[^\d+]", "", str(data.get("whatsapp_phone", "")))[:20]
        email = str(data.get("support_email", "")).strip()[:160]
        try:
            delivery = max(0, min(5000, int(data.get("delivery_charge", 49))))
            threshold = max(0, min(100000, int(data.get("free_delivery_threshold", 999))))
        except (TypeError, ValueError): raise ValueError("Enter valid delivery amounts.")
        with connect() as db:
            for key, value in (("shop_name", shop), ("whatsapp_phone", wa), ("support_email", email), ("delivery_charge", str(delivery)), ("free_delivery_threshold", str(threshold))):
                db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.json({"ok": True})

    def upload_image(self, data: dict) -> None:
        encoded = str(data.get("data_url", ""))
        match = re.fullmatch(r"data:image/(jpeg|png|webp|gif);base64,([A-Za-z0-9+/=]+)", encoded)
        if not match: raise ValueError("Choose a JPG, PNG, WebP or GIF image.")
        try: image = base64.b64decode(match.group(2), validate=True)
        except ValueError: raise ValueError("That image could not be read.")
        max_image_bytes = 3 * 1024 * 1024 if os.environ.get("VERCEL") == "1" else 5 * 1024 * 1024
        if not image or len(image) > max_image_bytes:
            raise ValueError(f"Images must be smaller than {max_image_bytes // (1024 * 1024)} MB.")
        kind = match.group(1)
        signatures = {"jpeg": image.startswith(b"\xff\xd8\xff"), "png": image.startswith(b"\x89PNG\r\n\x1a\n"), "gif": image.startswith((b"GIF87a", b"GIF89a")), "webp": image.startswith(b"RIFF") and image[8:12] == b"WEBP"}
        if not signatures[kind]: raise ValueError("That file does not look like the selected image type.")
        extension = "jpg" if kind == "jpeg" else kind
        name = f"{uuid.uuid4().hex}.{extension}"
        if os.environ.get("BLOB_READ_WRITE_TOKEN"):
            try:
                from vercel.blob import BlobClient
                blob = BlobClient().put(f"products/{name}", image, access="public", content_type=f"image/{kind}", add_random_suffix=False)
                url = blob.get("url") if isinstance(blob, dict) else getattr(blob, "url", None)
            except Exception as exc:
                sys.stderr.write(f"AMORA image upload failed ({type(exc).__name__}).\n")
                raise ValueError("The product photo could not be saved to Vercel Blob. Please try again.") from None
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError("The product photo service returned an invalid URL.")
            self.json({"url": url}, 201)
            return
        if os.environ.get("VERCEL") == "1":
            raise ValueError("Product photo storage is not configured. Add a Vercel Blob store to this project first.")
        (UPLOAD_DIR / name).write_bytes(image)
        self.json({"url": f"/uploads/{name}"}, 201)


def setup_admin() -> None:
    import getpass
    init_db()
    print("Create the private AMORA MART admin login. The password is stored as a salted hash in the configured database.")
    username = input("Admin username [admin]: ").strip() or "admin"
    if len(username) > 120: print("Username is too long."); raise SystemExit(2)
    first = getpass.getpass("Admin password (at least 12 characters): ")
    if len(first) < 12: print("Password must be at least 12 characters."); raise SystemExit(2)
    if first != getpass.getpass("Confirm password: "): print("Passwords did not match."); raise SystemExit(2)
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", first.encode(), salt, PASSWORD_ROUNDS).hex()
    with connect() as db:
        db.execute("INSERT INTO admins(username,salt,password_hash,created_at) VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET salt=excluded.salt,password_hash=excluded.password_hash", (username, salt.hex(), password_hash, utc_now()))
        db.execute("DELETE FROM sessions")
    print(f"Admin login is ready. Username: {username}. Keep your password private.")


LOGIN_LIMIT: dict[str, tuple[int, float]] = {}
TRACK_LOOKUP_LIMIT: dict[str, tuple[int, float]] = {}


def main() -> None:
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "setup-admin":
        setup_admin(); return
    with connect() as db:
        if db.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 0:
            print("Admin login is not set up yet. Run: python server.py setup-admin")
            raise SystemExit(2)
    host = os.environ.get("AMORA_HOST", "127.0.0.1")
    port = int(os.environ.get("AMORA_PORT", "4173"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"AMORA MART is ready at http://{host}:{port}")
    print("Admin: /admin   |   Stop with Ctrl+C")
    try: httpd.serve_forever()
    except KeyboardInterrupt: print("\nStore server stopped.")
    finally: httpd.server_close()


if __name__ == "__main__":
    main()
