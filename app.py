"""CampusFind - Lost & Found Portal (Streamlit).

Run locally:  streamlit run app.py
Database: SQLite locally; Supabase PostgreSQL when [database] url is set in st.secrets.
"""
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import sqlite3
from html import escape as esc
from datetime import date, datetime, timedelta, timezone

import streamlit as st
from PIL import Image, UnidentifiedImageError

st.set_page_config(page_title="CampusFind", page_icon="🔎", layout="wide")

# ============================================================ settings
CATEGORIES = ["Electronics", "Documents & IDs", "Wallets & Bags", "Keys",
              "Clothing", "Books & Stationery", "Accessories", "Other"]
LOCATIONS = ["Library", "Canteen", "Classroom Block", "Laboratory", "Hostel",
             "Sports Ground", "Parking", "Auditorium", "Main Gate", "Other"]
MAX_IMAGE_BYTES = 2 * 1024 * 1024
PAGE_SIZE = 12
MAX_FAILS = 5
SQLITE_FILE = "campusfind.db"
DUMMY_HASH = "00" * 16 + "$" + "00" * 32
# Item columns without the image blob (keeps list queries light)
ITEM_COLS = ("id, user_id, kind, name, category, description, event_date, "
             "location, contact, question, answer, status, created_at")
CARD_COLS = "id, kind, name, category, description, event_date, location, status, image"
Image.MAX_IMAGE_PIXELS = 10_000_000
log = logging.getLogger("campusfind")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9 \-]{7,15}$")

P_HOME, P_BROWSE, P_LOST, P_FOUND, P_MY, P_ACCT, P_ADMIN = (
    "🏠 Home", "🔍 Browse Items", "📢 Report Lost", "✅ Report Found",
    "📁 My Reports", "👤 Account", "🛡️ Admin")
CAT_ICON = {"Electronics": "📱", "Documents & IDs": "🪪", "Wallets & Bags": "👜", "Keys": "🔑",
            "Clothing": "👕", "Books & Stationery": "📚", "Accessories": "🕶️", "Other": "📦"}

CSS = """<style>
.block-container{max-width:1180px;padding-top:1.2rem}
.stApp,[data-testid="stHeader"]{background:#fff!important}
.stApp,.stApp p,.stApp label,.stApp li,.stApp h1,.stApp h2,.stApp h3,.stApp h4,.stApp h5,
.stApp h6,.stApp [data-testid="stCaptionContainer"]{color:#0f1b3d}
.stApp input,.stApp textarea,.stApp [data-baseweb="select"] > div{background:#fff!important;color:#0f1b3d!important}
button[kind="primary"],button[kind="primaryFormSubmit"]{background:#1f6feb!important;border-color:#1f6feb!important}
button[kind="primary"] p,button[kind="primaryFormSubmit"] p{color:#fff!important}
[data-testid="stSidebar"]{background:#eef4ff;border-right:1px solid #d6e2f7}
.brand{font-size:1.35rem;font-weight:800;color:#0b3d91;margin:.2rem 0 .9rem}
.brand small{display:block;font-size:.78rem;font-weight:500;color:#52648a}
[data-testid="stSidebar"] [role="radiogroup"]{gap:.3rem}
[data-testid="stSidebar"] [role="radiogroup"] label{background:#fff;border:1px solid #d6e2f7;
border-radius:10px;padding:.5rem .8rem;width:100%;cursor:pointer}
[data-testid="stSidebar"] [role="radiogroup"] label > div:first-child{display:none}
[data-testid="stSidebar"] [role="radiogroup"] label p{color:#0f1b3d;font-weight:600}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked){background:#1f6feb;border-color:#1f6feb}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) *{color:#fff!important}
.user{display:flex;gap:.6rem;align-items:center;margin-bottom:.6rem;color:#0f1b3d}
.user .av{background:#1f6feb;color:#fff;width:2.2rem;height:2.2rem;border-radius:50%;
display:flex;align-items:center;justify-content:center;font-weight:700}
.user small{color:#52648a}
.hero{background:linear-gradient(110deg,#0b3d91,#1f6feb);padding:1.6rem 1.8rem;border-radius:16px;margin-bottom:1rem}
.hero h1{color:#fff!important;margin:0;padding:0;font-size:1.9rem;line-height:1.25}
.hero p{margin:.4rem 0 0;color:#e6efff;font-size:1.02rem}
.stat,.step{background:#fff;border:1px solid #d6e2f7;border-radius:14px;padding:1rem 1.1rem;color:#0f1b3d;height:100%}
.stat{text-align:center;color:#52648a;font-weight:600}
.stat b{display:block;font-size:2.2rem;color:#0b3d91;line-height:1.1}
.step{background:#f4f8ff}.step b{color:#0b3d91}
.ph{height:170px;display:flex;align-items:center;justify-content:center;font-size:3.2rem;
background:#eef4ff;border-radius:8px;margin-bottom:.5rem}
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stImage"] img{height:170px;
object-fit:cover;width:100%;border-radius:8px}
.empty{text-align:center;padding:2rem;border:2px dashed #c3d3ee;border-radius:14px;color:#52648a}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.72rem;font-weight:600;margin-right:6px}
.badge.lost{background:#dbeafe;color:#1e40af}
.badge.found{background:#e0f2fe;color:#075985}
.badge.resolved{background:#dcfce7;color:#166534}
.stButton>button,.stFormSubmitButton>button{border-radius:10px;font-weight:600}
</style>"""


# ============================================================ database
def get_secret(section, key):
    """Read a value from st.secrets; return None if missing."""
    try:
        return st.secrets[section][key]
    except Exception:
        return None


def connect():
    """Return (connection, is_postgres)."""
    url = get_secret("database", "url")
    if url:
        import psycopg2
        return psycopg2.connect(url, connect_timeout=10), True
    conn = sqlite3.connect(SQLITE_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn, False


def _rows(cur):
    cols = [c[0] for c in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        for k, v in d.items():
            if isinstance(v, memoryview):
                d[k] = bytes(v)
        out.append(d)
    return out


def run(sql, params=(), fetch=False, returning=False):
    """Run one parameterized statement. Use '?' placeholders for both databases."""
    conn, pg = connect()
    try:
        cur = conn.cursor()
        if pg:
            sql = sql.replace("?", "%s")
            if returning:
                sql += " RETURNING id"
        cur.execute(sql, params)
        result = None
        if fetch:
            result = _rows(cur)
        elif returning:
            result = cur.fetchone()[0] if pg else cur.lastrowid
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_tx(statements):
    """Run several (sql, params) statements in one transaction."""
    conn, pg = connect()
    try:
        cur = conn.cursor()
        for sql, params in statements:
            cur.execute(sql.replace("?", "%s") if pg else sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def one(sql, params=()):
    rows = run(sql, params, fetch=True)
    return rows[0] if rows else None


@st.cache_resource(show_spinner="Connecting to database...")
def init_db():
    """Create tables once per server process."""
    conn, pg = connect()
    try:
        pk = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        blob = "BYTEA" if pg else "BLOB"
        schema = [
            f"""CREATE TABLE IF NOT EXISTS users (
                id {pk}, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL)""",
            f"""CREATE TABLE IF NOT EXISTS items (
                id {pk}, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
                description TEXT NOT NULL, event_date TEXT NOT NULL, location TEXT NOT NULL,
                contact TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
                image {blob}, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL)""",
            f"""CREATE TABLE IF NOT EXISTS claims (
                id {pk}, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                answer TEXT NOT NULL, contact TEXT NOT NULL, message TEXT,
                status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at)",
        ]
        cur = conn.cursor()
        for s in schema:
            cur.execute(s)
        if pg:  # block Supabase's public Data API; the app connects directly
            for t in ("users", "items", "claims"):
                cur.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        conn.commit()
    finally:
        conn.close()
    return True


# ============================================================ helpers
def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def md(s):
    """Escape user text so it can't inject markdown (links, images, headings)."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~$&:])", r"\\\1", s or "").replace("\n", "  \n")


def hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    try:
        salt, h = stored.split("$")
        new = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    except ValueError:
        return False
    return hmac.compare_digest(new, h)


def valid_contact(s):
    return len(s) <= 100 and bool(EMAIL_RE.match(s) or PHONE_RE.match(s))


def process_image(file):
    """Validate an upload and return compressed JPEG bytes (or None)."""
    if file is None:
        return None
    data = file.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image must be 2 MB or smaller.")
    try:
        Image.open(io.BytesIO(data)).verify()
        img = Image.open(io.BytesIO(data))
        if img.format not in ("JPEG", "PNG"):
            raise ValueError("Only PNG and JPG images are allowed.")
        img = img.convert("RGB")
        img.thumbnail((800, 800))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80, optimize=True)
        return out.getvalue()
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("That file is not a valid image.")


def flash(msg, level="success"):
    st.session_state.setdefault("flash", []).append((level, msg))


def show_flash():
    for level, msg in st.session_state.pop("flash", []):
        getattr(st, level)(msg)


# ---- session-based auth
def me():
    return st.session_state.get("user")


def is_admin():
    u = me()
    return bool(u and u["role"] == "admin")


def is_member():
    u = me()
    return bool(u and u["role"] == "user")


def can_manage(owner_id):
    """Owner of the item or admin."""
    u = me()
    return bool(u and (u["role"] == "admin" or u["id"] == owner_id))


def locked():
    return st.session_state.get("fails", 0) >= MAX_FAILS


def add_fail():
    st.session_state["fails"] = st.session_state.get("fails", 0) + 1


def logout():
    st.session_state.pop("user", None)
    st.session_state["page"] = P_HOME


def goto(page):
    """Request a page change (applied at the start of the next run)."""
    st.session_state["_goto"] = page


def open_item(item_id):
    st.session_state["view_item"] = item_id
    st.session_state["page"] = P_BROWSE


def head(title, sub=""):
    st.markdown(f"## {title}")
    if sub:
        st.caption(sub)


def login_prompt(text, key):
    st.info(text)
    st.button("Log in / Register", on_click=goto, args=(P_ACCT,), key=key, type="primary")


def close_item():
    st.session_state.pop("view_item", None)


# ============================================================ shared UI
def badges(it):
    html = f'<span class="badge {it["kind"]}">{it["kind"].upper()}</span>'
    if it["status"] == "resolved":
        html += '<span class="badge resolved">RESOLVED</span>'
    return html


def render_cards(items, prefix):
    if not items:
        st.markdown('<div class="empty">📭 No items found. Try changing your filters.</div>',
                    unsafe_allow_html=True)
        return
    cols = st.columns(3)
    for i, it in enumerate(items):
        with cols[i % 3].container(border=True):
            if it["image"]:
                st.image(it["image"])
            else:
                st.markdown(f'<div class="ph">{CAT_ICON.get(it["category"], "📦")}</div>',
                            unsafe_allow_html=True)
            st.markdown(badges(it), unsafe_allow_html=True)
            st.markdown(f"**{md(it['name'])}**")
            desc = it["description"]
            st.markdown(md(desc[:90] + ("…" if len(desc) > 90 else "")))
            st.caption(f"{it['category']} · {it['location']} · {it['event_date']}")
            st.button("View details", key=f"{prefix}_{it['id']}",
                      on_click=open_item, args=(it["id"],))


def _idx(options, value):
    return options.index(value) if value in options else 0


def item_fields(p, d=None):
    """Shared widgets for report/edit forms. Call inside st.form. Returns cleaned values."""
    d = d or {}
    v = {}
    st.markdown("##### 1 · Item details")
    v["name"] = st.text_input("Item name *", d.get("name", ""), max_chars=80, key=p + "name",
                              placeholder="e.g. Black leather wallet").strip()
    c1, c2, c3 = st.columns(3)
    v["category"] = c1.selectbox("Category *", CATEGORIES,
                                 index=_idx(CATEGORIES, d.get("category")), key=p + "cat")
    v["location"] = c2.selectbox("Location *", LOCATIONS,
                                 index=_idx(LOCATIONS, d.get("location")), key=p + "loc")
    v["date"] = c3.date_input(
        "Date *", date.fromisoformat(d["event_date"]) if d else date.today(),
        max_value=date.today() + timedelta(days=1), key=p + "date")
    v["description"] = st.text_area("Description *", d.get("description", ""), max_chars=1000,
                                    key=p + "desc",
                                    placeholder="Colour, brand, marks, what is inside...").strip()
    st.markdown("##### 2 · Contact & verification")
    st.caption("🔒 Your contact stays hidden. Anyone claiming this item must answer your question; "
               "an admin compares it with your answer before approving.")
    v["contact"] = st.text_input("Your contact (email or phone) *", d.get("contact", ""),
                                 max_chars=100, key=p + "contact",
                                 placeholder="you@college.edu or +91 98765 43210").strip()
    v["question"] = st.text_input(
        "Verification question *", d.get("question", ""), max_chars=200, key=p + "q",
        placeholder="e.g. Which card is inside the wallet?").strip()
    v["answer"] = st.text_input("Correct answer (private) *", d.get("answer", ""),
                                max_chars=100, key=p + "a", placeholder="e.g. Library card").strip()
    st.markdown("##### 3 · Photo (optional)")
    v["image"] = st.file_uploader("PNG or JPG, max 2 MB", type=["png", "jpg", "jpeg"], key=p + "img")
    return v


def validate_item(v):
    errs = []
    if not 3 <= len(v["name"]) <= 80:
        errs.append("Item name must be 3-80 characters.")
    if not 10 <= len(v["description"]) <= 1000:
        errs.append("Description must be 10-1000 characters.")
    if not valid_contact(v["contact"]):
        errs.append("Enter a valid email address or phone number as contact.")
    if not 5 <= len(v["question"]) <= 200:
        errs.append("Verification question must be 5-200 characters.")
    if not 1 <= len(v["answer"]) <= 100:
        errs.append("Verification answer is required (max 100 characters).")
    if v["date"] > date.today() + timedelta(days=1):
        errs.append("Date cannot be in the future.")
    return errs


def manage_item(it, prefix):
    """Expander with claims, edit form, resolve and delete. Authorization enforced."""
    iid = it["id"]
    if not can_manage(it["user_id"]):
        return
    with st.expander(md(f"{it['kind'].title()} · {it['name']} · {it['status']}")):
        claims = run(
            """SELECT c.id, c.status, c.created_at, u.name AS claimant,
               CASE WHEN c.status='approved' THEN c.contact END AS contact
               FROM claims c JOIN users u ON u.id=c.user_id
               WHERE c.item_id=? ORDER BY c.id DESC""", (iid,), fetch=True)
        if claims:
            st.markdown("**Claims**")
            for c in claims:
                line = f"- {md(c['claimant'])} — {c['status']}"
                if c["contact"]:
                    line += f" — contact: {md(c['contact'])}"
                st.markdown(line)
        with st.form(f"edit_{prefix}{iid}"):
            v = item_fields(f"e{prefix}{iid}_", it)
            st.caption("Leave the photo empty to keep the current one.")
            save = st.form_submit_button("Save changes")
        if save:
            errs = validate_item(v)
            img = None
            if not errs:
                try:
                    img = process_image(v["image"])
                except ValueError as e:
                    errs.append(str(e))
            if errs:
                for e in errs:
                    st.error(e)
            else:
                sets = ("name=?, category=?, description=?, event_date=?, location=?, "
                        "contact=?, question=?, answer=?")
                params = [v["name"], v["category"], v["description"], v["date"].isoformat(),
                          v["location"], v["contact"], v["question"], v["answer"]]
                if img:
                    sets += ", image=?"
                    params.append(img)
                run(f"UPDATE items SET {sets} WHERE id=?", tuple(params + [iid]))
                flash("Report updated.")
                st.rerun()
        c1, c2 = st.columns(2)
        if it["status"] == "open" and c1.button("Mark as resolved", key=f"res_{prefix}{iid}"):
            run("UPDATE items SET status='resolved' WHERE id=?", (iid,))
            flash("Report marked as resolved.")
            st.rerun()
        confirm = c2.checkbox("Confirm delete", key=f"cd_{prefix}{iid}")
        if c2.button("Delete report", key=f"del_{prefix}{iid}", disabled=not confirm):
            run("DELETE FROM items WHERE id=?", (iid,))
            flash("Report deleted.")
            st.rerun()


# ============================================================ pages
def page_home():
    st.markdown('<div class="hero"><h1>Lost something? Found something?</h1>'
                '<p>CampusFind reunites belongings with their owners — quickly and privately.</p></div>',
                unsafe_allow_html=True)
    rows = run("SELECT kind, status, COUNT(*) AS n FROM items GROUP BY kind, status", fetch=True)
    lost = sum(r["n"] for r in rows if r["kind"] == "lost" and r["status"] == "open")
    found = sum(r["n"] for r in rows if r["kind"] == "found" and r["status"] == "open")
    resolved = sum(r["n"] for r in rows if r["status"] == "resolved")
    for col, n, label in zip(st.columns(3), (lost, found, resolved),
                             ("Lost items (open)", "Found items (open)", "Resolved")):
        col.markdown(f'<div class="stat"><b>{n}</b>{label}</div>', unsafe_allow_html=True)
    st.write("")
    a, b, _ = st.columns([1, 1, 2])
    a.button("📢 I lost something", on_click=goto, args=(P_LOST,), type="primary", key="h_lost")
    b.button("✅ I found something", on_click=goto, args=(P_FOUND,), key="h_found")
    st.markdown("### How it works")
    steps = [("1 · Report", "Post a lost or found item with place, date and an optional photo."),
             ("2 · Verify", "Claimants answer a private question only the real owner would know."),
             ("3 · Reunite", "An admin approves the claim and contact details are shared.")]
    for col, (t, d) in zip(st.columns(3), steps):
        col.markdown(f'<div class="step"><b>{t}</b><br>{d}</div>', unsafe_allow_html=True)
    st.markdown("### Recently reported")
    items = run(f"SELECT {CARD_COLS} FROM items WHERE status='open' "
                "ORDER BY created_at DESC, id DESC LIMIT 6", fetch=True)
    render_cards(items, "home")


def page_browse():
    if st.session_state.get("view_item"):
        show_details(st.session_state["view_item"])
        return
    head("🔍 Browse items", "Search and filter every lost and found report.")
    box = st.container(border=True)
    q = box.text_input("Search", placeholder="Search by name, description or location", key="b_q")
    a, b, c, d, e = box.columns(5)
    kind = a.selectbox("Type", ["All", "Lost", "Found"], key="b_kind")
    cat = b.selectbox("Category", ["All"] + CATEGORIES, key="b_cat")
    status = c.selectbox("Status", ["Open", "Resolved", "All"], key="b_status")
    loc = d.selectbox("Location", ["All"] + LOCATIONS, key="b_loc")
    newest = e.selectbox("Sort", ["Newest first", "Oldest first"], key="b_sort") == "Newest first"

    where, p = [], []
    if kind != "All":
        where.append("kind=?"); p.append(kind.lower())
    if cat != "All":
        where.append("category=?"); p.append(cat)
    if status != "All":
        where.append("status=?"); p.append(status.lower())
    if loc != "All":
        where.append("location=?"); p.append(loc)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        where.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ? OR LOWER(location) LIKE ?)")
        p += [like, like, like]
    w = (" WHERE " + " AND ".join(where)) if where else ""

    total = run("SELECT COUNT(*) AS n FROM items" + w, tuple(p), fetch=True)[0]["n"]
    pages = max(1, -(-total // PAGE_SIZE))
    page = 1
    if pages > 1:
        page = st.selectbox("Page", list(range(1, pages + 1)), key="b_page")
    order = "DESC" if newest else "ASC"
    items = run(f"SELECT {CARD_COLS} FROM items{w} ORDER BY created_at {order}, id {order} "
                "LIMIT ? OFFSET ?", tuple(p) + (PAGE_SIZE, (page - 1) * PAGE_SIZE), fetch=True)
    st.caption(f"{total} item(s) found")
    render_cards(items, "browse")


def show_details(item_id):
    st.button("← Back to results", on_click=close_item)
    it = one("SELECT * FROM items WHERE id=?", (item_id,))
    if not it:
        st.warning("This item no longer exists.")
        return
    u = me()
    my_claim = None
    if is_member():
        my_claim = one("SELECT status FROM claims WHERE item_id=? AND user_id=?", (item_id, u["id"]))
    left, right = st.columns([1, 1.5])
    with left:
        if it["image"]:
            st.image(it["image"])
        else:
            st.caption("No photo provided.")
    with right:
        st.markdown(badges(it), unsafe_allow_html=True)
        st.markdown(f"## {md(it['name'])}")
        st.markdown(f"**Category:** {md(it['category'])}  \n**Location:** {md(it['location'])}  \n"
                    f"**Date:** {it['event_date']}  \n**Reported:** {it['created_at'][:10]}")
        st.markdown(f"**Description**  \n{md(it['description'])}")
        if can_manage(it["user_id"]) or (my_claim and my_claim["status"] == "approved"):
            st.success(f"Contact: {md(it['contact'])}")
        else:
            st.caption("🔒 Contact details are private and shared only after a claim is approved.")

    st.divider()
    st.markdown("### Is this yours?" if it["kind"] == "found" else "### Did you find this?")
    if it["status"] == "resolved":
        st.info("This item has been resolved.")
    elif not is_member():
        login_prompt("Log in with a user account to submit a claim.", "lp_claim")
    elif u["id"] == it["user_id"]:
        st.caption("This is your own report. Manage it from **My Reports**.")
    elif my_claim:
        st.info(f"You already submitted a claim for this item. Status: **{my_claim['status']}**.")
    else:
        st.markdown(f"**Verification question:** {md(it['question'])}")
        with st.form(f"claim_{item_id}"):
            ans = st.text_input("Your answer *", max_chars=200).strip()
            contact = st.text_input("Your contact (email or phone) *", max_chars=100).strip()
            msg = st.text_area("Message (optional)", max_chars=300).strip()
            go = st.form_submit_button("Submit claim", type="primary")
        if go:
            errs = []
            if not ans:
                errs.append("Please answer the verification question.")
            if not valid_contact(contact):
                errs.append("Enter a valid email address or phone number.")
            if errs:
                for e in errs:
                    st.error(e)
            else:
                run("INSERT INTO claims (item_id, user_id, answer, contact, message, status, created_at) "
                    "VALUES (?,?,?,?,?,?,?)", (item_id, u["id"], ans, contact, msg, "pending", now()))
                flash("Claim submitted. An admin will review it.")
                st.rerun()


def page_report(kind):
    head(f"{'📢' if kind == 'lost' else '✅'} Report a {kind} item",
         "Fill in the details below. Fields marked * are required.")
    if not is_member():
        login_prompt("Please log in or create an account to submit a report.", "lp_report")
        return
    n = st.session_state.get("form_n", 0)
    with st.form(f"report_{kind}_{n}"):
        v = item_fields(f"{kind}{n}_")
        submit = st.form_submit_button("Submit report", type="primary")
    if not submit:
        return
    errs = validate_item(v)
    img = None
    if not errs:
        try:
            img = process_image(v["image"])
        except ValueError as e:
            errs.append(str(e))
    if errs:
        for e in errs:
            st.error(e)
        return
    run("INSERT INTO items (user_id, kind, name, category, description, event_date, location, "
        "contact, question, answer, image, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (me()["id"], kind, v["name"], v["category"], v["description"], v["date"].isoformat(),
         v["location"], v["contact"], v["question"], v["answer"], img, "open", now()))
    st.session_state["form_n"] = n + 1
    flash(f"Your {kind} item report was submitted.")
    goto(P_MY)
    st.rerun()


def page_my():
    head("📁 My reports", "Edit, resolve or delete your reports, and track your claims.")
    if not is_member():
        login_prompt("Please log in to see your reports.", "lp_my")
        return
    uid = me()["id"]
    t1, t2 = st.tabs(["Items I reported", "Claims I made"])
    with t1:
        items = run(f"SELECT {ITEM_COLS} FROM items WHERE user_id=? ORDER BY id DESC", (uid,), fetch=True)
        if not items:
            st.info("You have not reported anything yet.")
        for it in items:
            manage_item(it, "m")
    with t2:
        rows = run(
            """SELECT c.id, c.status, i.id AS item_id, i.name, i.kind,
               CASE WHEN c.status='approved' THEN i.contact END AS owner_contact
               FROM claims c JOIN items i ON i.id=c.item_id
               WHERE c.user_id=? ORDER BY c.id DESC""", (uid,), fetch=True)
        if not rows:
            st.info("You have not made any claims yet.")
        for r in rows:
            with st.container(border=True):
                st.markdown(f"**{md(r['name'])}** ({r['kind']}) — claim **{r['status']}**")
                if r["owner_contact"]:
                    st.success(f"Contact: {md(r['owner_contact'])}")
                st.button("View item", key=f"mc_{r['id']}", on_click=open_item, args=(r["item_id"],))


def page_account():
    u = me()
    if u:
        head("👤 My account")
        st.success(f"Signed in as **{md(u['name'])}** ({u['role']})")
        st.button("Log out", on_click=logout, key="acct_logout")
        return
    _, mid, _ = st.columns([1, 1.6, 1])
    with mid:
        head("Welcome to CampusFind", "Browsing is open to everyone. Log in to report items and make claims.")
        mode = st.radio("Choose", ["Log in", "Create account"], horizontal=True,
                        key="auth_mode", label_visibility="collapsed")
        if mode == "Log in":
            with st.form("login"):
                email = st.text_input("Email", placeholder="you@college.edu")
                pw = st.text_input("Password", type="password")
                ok = st.form_submit_button("Log in", type="primary")
            st.caption("New here? Choose **Create account** above.")
            if ok:
                if locked():
                    st.error("Too many failed attempts. Please try again later.")
                    return
                row = one("SELECT id, name, password_hash FROM users WHERE email=?",
                          (email.strip().lower(),))
                good = check_pw(pw, row["password_hash"] if row else DUMMY_HASH)
                if row and good:
                    st.session_state["user"] = {"id": row["id"], "name": row["name"], "role": "user"}
                    st.session_state["fails"] = 0
                    flash(f"Welcome back, {md(row['name'])}!")
                    goto(P_HOME)
                    st.rerun()
                add_fail()
                st.error("Invalid email or password.")
        else:
            with st.form("register"):
                name = st.text_input("Full name", max_chars=60, placeholder="Your name").strip()
                email = st.text_input("Email", max_chars=120, key="reg_email",
                                      placeholder="you@college.edu").strip().lower()
                pw = st.text_input("Password (min 8 characters)", type="password", key="reg_pw")
                pw2 = st.text_input("Confirm password", type="password")
                ok = st.form_submit_button("Create account", type="primary")
            if ok:
                errs = []
                if not 2 <= len(name) <= 60:
                    errs.append("Name must be 2-60 characters.")
                if not EMAIL_RE.match(email):
                    errs.append("Enter a valid email address.")
                if len(pw) < 8:
                    errs.append("Password must be at least 8 characters.")
                if pw != pw2:
                    errs.append("Passwords do not match.")
                if not errs and one("SELECT id FROM users WHERE email=?", (email,)):
                    errs.append("This email is already registered. Choose Log in instead.")
                if errs:
                    for e in errs:
                        st.error(e)
                else:
                    uid = run("INSERT INTO users (name, email, password_hash, created_at) "
                              "VALUES (?,?,?,?)", (name, email, hash_pw(pw), now()), returning=True)
                    st.session_state["user"] = {"id": uid, "name": name, "role": "user"}
                    flash(f"Welcome, {md(name)}! Your account is ready.")
                    goto(P_HOME)
                    st.rerun()


def decide_claim(claim_id, approve):
    if not is_admin():
        raise PermissionError("Admin only")
    claim = one("SELECT item_id FROM claims WHERE id=? AND status='pending'", (claim_id,))
    if not claim:
        return
    if approve:
        run_tx([
            ("UPDATE claims SET status='approved' WHERE id=?", (claim_id,)),
            ("UPDATE claims SET status='rejected' WHERE item_id=? AND id<>? AND status='pending'",
             (claim["item_id"], claim_id)),
            ("UPDATE items SET status='resolved' WHERE id=?", (claim["item_id"],)),
        ])
    else:
        run("UPDATE claims SET status='rejected' WHERE id=?", (claim_id,))


def page_admin():
    head("🛡️ Admin panel", "Manage reports and review ownership claims.")
    if not is_admin():
        admin_user = get_secret("admin", "username") or os.environ.get("ADMIN_USERNAME")
        admin_pw = get_secret("admin", "password") or os.environ.get("ADMIN_PASSWORD")
        if not admin_user or not admin_pw:
            st.warning("Admin account is not set up yet. Create the file "
                       "`.streamlit/secrets.toml` next to app.py (on Streamlit Cloud: App → "
                       "Settings → Secrets), add this, then restart the app:")
            st.code('[admin]\nusername = "admin"\npassword = "choose-a-strong-password"', language="toml")
            return
        with st.form("admin_login"):
            u = st.text_input("Admin username")
            p = st.text_input("Admin password", type="password")
            ok = st.form_submit_button("Log in as admin", type="primary")
        if ok:
            if locked():
                st.error("Too many failed attempts. Please try again later.")
                return
            good_u = hmac.compare_digest(u.encode(), str(admin_user).encode())
            good_p = hmac.compare_digest(p.encode(), str(admin_pw).encode())
            if good_u and good_p:
                st.session_state["user"] = {"id": None, "name": "Admin", "role": "admin"}
                st.session_state["fails"] = 0
                st.rerun()
            add_fail()
            st.error("Invalid admin credentials.")
        return

    st.button("Log out", on_click=logout, key="admin_logout")
    t1, t2 = st.tabs(["Claims", "Reports"])
    with t1:
        status = st.selectbox("Claim status", ["pending", "approved", "rejected"], key="adm_cstatus")
        rows = run(
            """SELECT c.id, c.answer, c.contact AS claimant_contact, c.message, c.created_at,
               u.name AS claimant, i.name AS item_name, i.kind, i.question,
               i.answer AS expected, i.contact AS owner_contact
               FROM claims c JOIN users u ON u.id=c.user_id JOIN items i ON i.id=c.item_id
               WHERE c.status=? ORDER BY c.id DESC LIMIT 50""", (status,), fetch=True)
        if not rows:
            st.info(f"No {status} claims.")
        for r in rows:
            with st.container(border=True):
                st.markdown(f"**{md(r['item_name'])}** ({r['kind']}) — claimed by {md(r['claimant'])}")
                st.markdown(f"**Question:** {md(r['question'])}  \n"
                            f"**Expected answer:** {md(r['expected'])}  \n"
                            f"**Claimant's answer:** {md(r['answer'])}")
                if r["message"]:
                    st.markdown(f"**Message:** {md(r['message'])}")
                st.caption(f"Claimant contact: {r['claimant_contact']} · "
                           f"Reporter contact: {r['owner_contact']} · {r['created_at'][:10]}")
                if status == "pending":
                    a, b = st.columns(2)
                    if a.button("Approve", key=f"ap_{r['id']}", type="primary"):
                        decide_claim(r["id"], True)
                        flash("Claim approved and item marked resolved.")
                        st.rerun()
                    if b.button("Reject", key=f"rj_{r['id']}"):
                        decide_claim(r["id"], False)
                        flash("Claim rejected.")
                        st.rerun()
    with t2:
        c1, c2 = st.columns([2, 1])
        text = c1.text_input("Search reports", key="adm_q")
        status = c2.selectbox("Status", ["All", "Open", "Resolved"], key="adm_status")
        where, p = [], []
        if status != "All":
            where.append("status=?"); p.append(status.lower())
        if text.strip():
            like = f"%{text.strip().lower()}%"
            where.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ? OR LOWER(location) LIKE ?)")
            p += [like, like, like]
        w = (" WHERE " + " AND ".join(where)) if where else ""
        items = run(f"SELECT {ITEM_COLS} FROM items{w} ORDER BY id DESC LIMIT 30", tuple(p), fetch=True)
        st.caption("Showing up to 30 reports.")
        for it in items:
            manage_item(it, "a")


# ============================================================ main
PAGES = {
    P_HOME: page_home,
    P_BROWSE: page_browse,
    P_LOST: lambda: page_report("lost"),
    P_FOUND: lambda: page_report("found"),
    P_MY: page_my,
    P_ACCT: page_account,
    P_ADMIN: page_admin,
}


def main():
    st.markdown(CSS, unsafe_allow_html=True)
    try:
        init_db()
    except Exception as e:
        log.error("Database init failed: %s", type(e).__name__)
        st.error("Cannot connect to the database. Check your secrets configuration.")
        st.stop()

    options = list(PAGES)
    if not is_member():
        options.remove(P_MY)
    if "_goto" in st.session_state:
        st.session_state["page"] = st.session_state.pop("_goto")
    if st.session_state.get("page") not in options:
        st.session_state["page"] = P_HOME
    with st.sidebar:
        st.markdown('<div class="brand">🔎 CampusFind<small>Lost &amp; Found Portal</small></div>',
                    unsafe_allow_html=True)
        page = st.radio("Menu", options, key="page", label_visibility="collapsed")
        st.divider()
        u = me()
        if u:
            role = "Administrator" if u["role"] == "admin" else "Student"
            st.markdown(f'<div class="user"><span class="av">{esc(u["name"][:1].upper())}</span>'
                        f'<div><b>{esc(u["name"])}</b><br><small>{role}</small></div></div>',
                        unsafe_allow_html=True)
            st.button("Log out", on_click=logout, key="side_logout")
        else:
            st.caption("You are browsing as a guest.")
            st.button("Log in / Register", on_click=goto, args=(P_ACCT,),
                      key="side_login", type="primary")
    if page != P_BROWSE:
        st.session_state.pop("view_item", None)
    show_flash()
    try:
        PAGES[page]()
    except Exception as e:
        log.error("Page error: %s", type(e).__name__)
        st.error("Something went wrong. Please try again.")


main()