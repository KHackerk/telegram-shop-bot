"""
Telegram Digital Shop Bot + Admin Web Dashboard
- Bot UI matches the reference: profile card, Shop / Orders / Wallet / Invite etc.
- Admin web at /admin to manage categories, products, stock, orders, users, top-ups.
- Runs on Render via webhook (RENDER_EXTERNAL_URL) or locally via polling.
Only sell products you are authorised to sell.
"""
import os, sqlite3, secrets, threading, time
from html import escape as hesc
from functools import wraps
from datetime import datetime
from flask import Flask, request, session, redirect, url_for, render_template, flash, abort

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "shop.db"))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "admin123"  # empty env var must not become the password; change on Render!
SHOP_NAME = os.environ.get("SHOP_NAME", "My Digital Shop")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))

# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
      balance_cents INTEGER DEFAULT 0, total_spent_cents INTEGER DEFAULT 0,
      referrer_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS categories(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, emoji TEXT DEFAULT '', sort INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER REFERENCES categories(id),
      name TEXT NOT NULL, description TEXT DEFAULT '', price_cents INTEGER NOT NULL DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS stock_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER REFERENCES products(id),
      content TEXT NOT NULL, is_sold INTEGER DEFAULT 0, order_id INTEGER);
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
      price_cents INTEGER, status TEXT DEFAULT 'pending', delivery TEXT DEFAULT '',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS topups(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount_cents INTEGER,
      method TEXT DEFAULT '', status TEXT DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, detail TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS chat_messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, direction TEXT, text TEXT,
      ts TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS saved(
      user_id INTEGER, product_id INTEGER, PRIMARY KEY(user_id, product_id));
    """)
    _cols = [r["name"] for r in c.execute("PRAGMA table_info(categories)").fetchall()]
    if "logo_id" not in _cols:
        c.execute("ALTER TABLE categories ADD COLUMN logo_id TEXT DEFAULT ''")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS payment_methods(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, details TEXT DEFAULT '',
      active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS topup_sessions(
      telegram_id INTEGER PRIMARY KEY, step TEXT DEFAULT '', amount_cents INTEGER DEFAULT 0, method_id INTEGER DEFAULT 0);
    """)
    _tcols = [r["name"] for r in c.execute("PRAGMA table_info(topups)").fetchall()]
    if "method_name" not in _tcols:
        c.execute("ALTER TABLE topups ADD COLUMN method_name TEXT DEFAULT ''")
    if "screenshot" not in _tcols:
        c.execute("ALTER TABLE topups ADD COLUMN screenshot BLOB")
    if c.execute("SELECT COUNT(*) n FROM payment_methods").fetchone()["n"] == 0:
        c.executemany("INSERT INTO payment_methods(name,details,active,sort) VALUES(?,?,1,?)", [
          ("KBZ Pay", "Phone: 09 XXX XXX XXX\nName: Your Name\n(Edit this in Admin \u2192 Bot UI \u2192 Payment Methods)", 1),
          ("Wave Money", "Phone: 09 XXX XXX XXX\nName: Your Name\n(Edit this in Admin \u2192 Bot UI \u2192 Payment Methods)", 2),
        ])
    if c.execute("SELECT COUNT(*) n FROM categories").fetchone()["n"] == 0:
        seed = [
            ("ChatGPT Plus","🌀"),("Capcut Pro","✂️"),("Mail Box","📧"),("Figma","🖌️"),
            ("Gemini","♊"),("Cursor & Grok","🖱️"),("Codex API","🔑"),("Claude","🧡"),
            ("Muse Ai","🎵"),("MS Office","🪟"),("n8n","🔗"),("Adobe","🅰️"),
            ("Duolingo","🦉"),("Streaming","🍿"),("VPN","🛡️"),
        ]
        for i,(name,emoji) in enumerate(seed):
            cur = c.execute("INSERT INTO categories(name,emoji,sort) VALUES(?,?,?)",(name,emoji,i))
            c.execute("INSERT INTO products(category_id,name,description,price_cents) VALUES(?,?,?,?)",
                      (cur.lastrowid, f"{name} — 1 Month", "Admin: edit this product, set your real price and add stock in the dashboard.", 1000))
    # Brand-matching emojis: apply to existing rows too (runs every startup, harmless to repeat)
    EMOJI_MAP = {"ChatGPT Plus":"🌀","Capcut Pro":"✂️","Mail Box":"📧","Figma":"🖌️","Gemini":"♊",
                 "Cursor & Grok":"🖱️","Codex API":"🔑","Claude":"🧡","Muse Ai":"🎵","MS Office":"🪟",
                 "n8n":"🔗","Adobe":"🅰️","Duolingo":"🦉","Streaming":"🍿","VPN":"🛡️"}
    for _name, _em in EMOJI_MAP.items():
        c.execute("UPDATE categories SET emoji=? WHERE name=?", (_em, _name))
    # Extra categories the owner sells (added if missing) — placeholder products, edit in admin
    EXTRA_CATS = [("Telegram","✈️"),("Japan TikTok Account","🎵"),("WhatsApp","💬"),("Spotify","🎧")]
    _sort = (c.execute("SELECT COALESCE(MAX(sort),0) m FROM categories").fetchone()["m"] or 0)
    for _nm, _em in EXTRA_CATS:
        if not c.execute("SELECT id FROM categories WHERE name=?", (_nm,)).fetchone():
            _sort += 1
            _cur = c.execute("INSERT INTO categories(name,emoji,sort) VALUES(?,?,?)", (_nm, _em, _sort))
            c.execute("INSERT INTO products(category_id,name,description,price_cents) VALUES(?,?,?,?)",
                      (_cur.lastrowid, f"{_nm} — 1 Month", "Admin: edit this product, set your real price and add stock in the dashboard.", 1000))
    c.commit(); c.close()

# Animated company-logo custom emojis (Telegram pack "ApplicationEmoji"), by category name.
# Used as icon_custom_emoji_id on Shop buttons (Bot API). Owner account is Premium.
LOGO_EMOJI = {"ChatGPT Plus": "5359726582447487916", "Capcut Pro": "5364339557712020484",
              "Telegram": "5330237710655306682", "Japan TikTok Account": "5327982530702359565",
              "WhatsApp": "5334998226636390258", "Spotify": "5346074681004801565"}
LOGO_OPTIONS = [("ChatGPT logo", "5359726582447487916"), ("CapCut logo", "5364339557712020484"),
                ("Telegram logo", "5330237710655306682"), ("TikTok logo", "5327982530702359565"),
                ("WhatsApp logo", "5334998226636390258"), ("Spotify logo", "5346074681004801565")]
_LOGO_THUMB_CACHE = {}

def cat_logo(cat):
    try: v = cat["logo_id"]
    except Exception: v = None
    return v or LOGO_EMOJI.get(cat["name"], "")

def logo_thumb_bytes(cid):
    if cid in _LOGO_THUMB_CACHE: return _LOGO_THUMB_CACHE[cid]
    sts = bot.get_custom_emoji_stickers([str(cid)])
    if not sts: return None
    thumb = getattr(sts[0], "thumbnail", None) or getattr(sts[0], "thumb", None)
    if thumb is None: return None
    f = bot.get_file(thumb.file_id); raw = bot.download_file(f.file_path)
    _LOGO_THUMB_CACHE[cid] = raw
    return raw

def money(cents): return f"${cents/100:.2f}"
def tier(total_spent_cents):
    if total_spent_cents >= 50000: return "Gold"
    if total_spent_cents >= 10000: return "Silver"
    return "Bronze"

init_db()

def log_event(kind, detail=""):
    try:
        c = db(); c.execute("INSERT INTO events(kind,detail) VALUES(?,?)", (kind, str(detail)[:300])); c.commit(); c.close()
    except Exception:
        pass

def get_setting(key, default=""):
    try:
        c = db(); r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone(); c.close()
        return r["value"] if r else default
    except Exception:
        return default

def set_setting(key, value):
    c = db(); c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))); c.commit(); c.close()

def tget(tid):
    c = db(); r = c.execute("SELECT * FROM topup_sessions WHERE telegram_id=?", (tid,)).fetchone(); c.close(); return r

def tset(tid, step, amount=None, method_id=None):
    cur = tget(tid)
    a = amount if amount is not None else (cur["amount_cents"] if cur else 0)
    mid = method_id if method_id is not None else (cur["method_id"] if cur else 0)
    c = db()
    c.execute("INSERT INTO topup_sessions(telegram_id,step,amount_cents,method_id) VALUES(?,?,?,?) "
              "ON CONFLICT(telegram_id) DO UPDATE SET step=excluded.step, amount_cents=excluded.amount_cents, method_id=excluded.method_id",
              (tid, step, a, mid)); c.commit(); c.close()

def tclear(tid):
    c = db(); c.execute("DELETE FROM topup_sessions WHERE telegram_id=?", (tid,)); c.commit(); c.close()

def log_chat(user_id, direction, text):
    try:
        c = db(); c.execute("INSERT INTO chat_messages(user_id,direction,text) VALUES(?,?,?)", (user_id, direction, (text or "")[:1000])); c.commit(); c.close()
    except Exception:
        pass

def plain(html_text):
    import re as _re
    return _re.sub("<[^>]+>", "", html_text or "").strip()

# ---------- Telegram Bot (optional until BOT_TOKEN is set) ----------
bot = None
if BOT_TOKEN:
    import telebot
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)

    def get_user(msg):
        c = db(); u = c.execute("SELECT * FROM users WHERE telegram_id=?", (msg.from_user.id,)).fetchone()
        if not u:
            ref = None
            parts = (msg.text or "").split()
            if len(parts) > 1 and parts[1].startswith("ref_"):
                try: ref = int(parts[1][4:])
                except: pass
            c.execute("INSERT INTO users(telegram_id,username,first_name,referrer_id) VALUES(?,?,?,?)",
                      (msg.from_user.id, msg.from_user.username, msg.from_user.first_name, ref))
            c.commit(); u = c.execute("SELECT * FROM users WHERE telegram_id=?", (msg.from_user.id,)).fetchone()
        c.close(); return u

    def main_menu(msg_or_call, edit=False):
        u = get_user(msg_or_call) if hasattr(msg_or_call, "from_user") else None
        if u is None: return
        c = db()
        counts = {s: c.execute("SELECT COUNT(*) n FROM orders WHERE user_id=? AND status=?", (u["telegram_id"], s)).fetchone()["n"]
                  for s in ("pending","delivered","confirmed")}
        c.close()
        t = tier(u["total_spent_cents"])
        to_next = max(0, 10000 - u["total_spent_cents"]) if t == "Bronze" else 0
        username = f"@{hesc(u['username'])}" if u["username"] else "-"
        try: bot_name = bot.get_me().username
        except: bot_name = "your_bot"
        text = (f"<b>{hesc(u['first_name'] or '') or username}</b>\n\n"
                f"Tier: <b>{t}</b>\nUser ID: <code>{u['telegram_id']}</code>\nUsername: {username}\n\n"
                f"Balance: <b>{money(u['balance_cents'])}</b> · Total spent: {money(u['total_spent_cents'])}\n"
                + (f"To Silver: {money(to_next)} more\n" if t=="Bronze" else "")
                + f"Orders: Pending: {counts['pending']} · Delivered: {counts['delivered']} · Confirmed: {counts['confirmed']}\n\n"
                f"Your invite link: https://t.me/{bot_name}?start=ref_{u['telegram_id']}")
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(InlineKeyboardButton("🛍️ Shop", callback_data="shop"))
        kb.add(InlineKeyboardButton("📦 My orders", callback_data="orders"),
               InlineKeyboardButton("💳 Wallet", callback_data="wallet"))
        kb.add(InlineKeyboardButton("📣 What's new", callback_data="new"),
               InlineKeyboardButton("🤝 Invite & earn", callback_data="invite"))
        kb.add(InlineKeyboardButton("❤️ Saved", callback_data="saved"),
               InlineKeyboardButton("🆘 Help", callback_data="help"))
        kb.add(InlineKeyboardButton("⚙️ Settings", callback_data="settings"))
        chat_id = msg_or_call.chat.id if hasattr(msg_or_call, "chat") else msg_or_call.message.chat.id
        log_chat(u["telegram_id"], "out", plain(text))
        if edit and hasattr(msg_or_call, "message"):
            bot.edit_message_text(text, chat_id, msg_or_call.message.message_id, reply_markup=kb)
        else:
            bot.send_message(chat_id, text, reply_markup=kb)

    @bot.message_handler(commands=["start"])
    def start(msg):
        log_event("start_received", f"user={msg.from_user.id}")
        try:
            main_menu(msg); log_event("start_reply_sent", f"user={msg.from_user.id}")
        except Exception as e:
            log_event("start_reply_failed", f"{type(e).__name__}: {e}")
            print(f"[bot] /start handler failed: {type(e).__name__}: {e}", flush=True)

    @bot.message_handler(commands=["menu"])
    def menu(msg): main_menu(msg)

    @bot.callback_query_handler(func=lambda c: True)
    def cb(call):
        c = db(); data = call.data
        log_chat(call.from_user.id, "in", f"[tap] {data}")
        def show(text, kb=None):
            log_chat(call.from_user.id, "out", plain(text))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        back = InlineKeyboardMarkup(); back.add(InlineKeyboardButton("⬅️ Back", callback_data="home"))
        if data == "home":
            c.close(); main_menu(call, edit=True); return
        if data == "shop":
            cats = c.execute("SELECT cat.*, (SELECT COUNT(*) FROM stock_items s JOIN products p ON p.id=s.product_id WHERE p.category_id=cat.id AND s.is_sold=0) stock FROM categories cat ORDER BY sort").fetchall()
            try: cat_cols = max(1, min(2, int(get_setting("cat_cols", "2"))))
            except Exception: cat_cols = 2
            raw_rows, kb = [], InlineKeyboardMarkup(row_width=cat_cols)
            for cat in cats:
                cnt = c.execute("SELECT COUNT(*) n FROM products WHERE category_id=? AND active=1", (cat["id"],)).fetchone()["n"]
                logo = cat_logo(cat)
                _lbl = f"{cat['name']} ({cnt})" if get_setting("show_counts", "1") == "1" else cat["name"]
                if logo:
                    btn = {"text": _lbl, "callback_data": f"cat_{cat['id']}", "icon_custom_emoji_id": logo}
                    fb_text = f"{cat['emoji']} {_lbl}"
                else:
                    btn = {"text": f"{cat['emoji']} {_lbl}", "callback_data": f"cat_{cat['id']}"}
                    fb_text = btn["text"]
                if not raw_rows or len(raw_rows[-1]) >= cat_cols: raw_rows.append([])
                raw_rows[-1].append(btn)
                kb.add(InlineKeyboardButton(fb_text, callback_data=f"cat_{cat['id']}"))
            raw_rows.append([{"text": "⬅️ Back", "callback_data": "home"}])
            kb.add(InlineKeyboardButton("⬅️ Back", callback_data="home"))
            import json as _json
            _title = get_setting("shop_title", "") or f"🛍️ {SHOP_NAME}"
            _sub = get_setting("shop_subtitle", "") or "Choose a category:"
            _head = f"<b>{hesc(_title)}</b>\n{hesc(_sub)}"
            try:
                show(_head, _json.dumps({"inline_keyboard": raw_rows}))
            except Exception:
                show(_head, kb)
        elif data.startswith("cat_"):
            cid = int(data[4:]); prods = c.execute("SELECT * FROM products WHERE category_id=? AND active=1", (cid,)).fetchall()
            try: prod_cols = max(1, min(2, int(get_setting("prod_cols", "1"))))
            except Exception: prod_cols = 1
            kb = InlineKeyboardMarkup(row_width=prod_cols)
            for p in prods:
                stock = c.execute("SELECT COUNT(*) n FROM stock_items WHERE product_id=? AND is_sold=0", (p["id"],)).fetchone()["n"]
                kb.add(InlineKeyboardButton(f"{p['name']} — {money(p['price_cents'])} · stock {stock}", callback_data=f"prod_{p['id']}"))
            kb.add(InlineKeyboardButton("⬅️ Back", callback_data="shop")); show("<b>Products</b>", kb)
        elif data.startswith("prod_"):
            pid = int(data[5:]); p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            stock = c.execute("SELECT COUNT(*) n FROM stock_items WHERE product_id=? AND is_sold=0", (pid,)).fetchone()["n"]
            kb = InlineKeyboardMarkup(row_width=2)
            kb.add(InlineKeyboardButton("✅ Buy now", callback_data=f"buy_{pid}"),
                   InlineKeyboardButton("❤️ Save", callback_data=f"save_{pid}"))
            kb.add(InlineKeyboardButton("⬅️ Back", callback_data="shop"))
            show(f"<b>{p['name']}</b>\n\n{p['description']}\n\nPrice: <b>{money(p['price_cents'])}</b>\nStock: {stock}", kb)
        elif data.startswith("save_"):
            pid = int(data[5:]); c.execute("INSERT OR IGNORE INTO saved VALUES(?,?)", (call.from_user.id, pid)); c.commit()
            bot.answer_callback_query(call.id, "Saved ❤️"); 
        elif data.startswith("buy_"):
            pid = int(data[4:]); p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            u = c.execute("SELECT * FROM users WHERE telegram_id=?", (call.from_user.id,)).fetchone()
            if not u: bot.answer_callback_query(call.id, "Send /start first"); c.close(); return
            if u["balance_cents"] < p["price_cents"]:
                show(f"❌ Not enough balance.\nYou have {money(u['balance_cents'])}, need {money(p['price_cents'])}.\nTop up from Wallet — admin will approve it.", back); c.close(); return
            item = c.execute("SELECT * FROM stock_items WHERE product_id=? AND is_sold=0 ORDER BY id LIMIT 1", (pid,)).fetchone()
            c.execute("UPDATE users SET balance_cents=balance_cents-?, total_spent_cents=total_spent_cents+? WHERE telegram_id=?", (p["price_cents"], p["price_cents"], call.from_user.id))
            if item:
                cur = c.execute("INSERT INTO orders(user_id,product_id,price_cents,status,delivery) VALUES(?,?,?,'delivered',?)", (call.from_user.id, pid, p["price_cents"], item["content"]))
                c.execute("UPDATE stock_items SET is_sold=1, order_id=? WHERE id=?", (cur.lastrowid, item["id"])); c.commit()
                show(f"✅ <b>Order #{cur.lastrowid} delivered</b>\n\n{item['content']}", back)
            else:
                cur = c.execute("INSERT INTO orders(user_id,product_id,price_cents,status) VALUES(?,?,?,'pending')", (call.from_user.id, pid, p["price_cents"])); c.commit()
                show(f"✅ <b>Order #{cur.lastrowid} created (Pending)</b>\nAdmin will deliver it soon. Check My orders.", back)
        elif data == "orders":
            rows = c.execute("SELECT o.*, p.name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20", (call.from_user.id,)).fetchall()
            txt = "<b>📦 My orders</b>\n\n" + ("\n".join(f"#{r['id']} · {r['name']} · {money(r['price_cents'])} · <b>{r['status']}</b>" + (f"\n{r['delivery']}" if r["delivery"] else "") for r in rows) or "No orders yet.")
            show(txt, back)
        elif data == "wallet":
            u = c.execute("SELECT * FROM users WHERE telegram_id=?", (call.from_user.id,)).fetchone()
            kb = InlineKeyboardMarkup(); kb.add(InlineKeyboardButton("➕ Deposit", callback_data="dep_start"))
            kb.add(InlineKeyboardButton("⬅️ Back", callback_data="home"))
            show(f"<b>💳 Wallet</b>\nBalance: <b>{money(u['balance_cents']) if u else '$0.00'}</b>\n\nTap ➕ Deposit, enter an amount, choose a payment method, transfer, then send your payment screenshot. The admin approves deposits in the dashboard.", kb)
        elif data == "dep_start" or data.startswith("topup_"):
            tset(call.from_user.id, "amount", amount=0, method_id=0)
            kb = InlineKeyboardMarkup(); kb.add(InlineKeyboardButton("❌ Cancel", callback_data="dep_cancel"))
            show("<b>💰 Deposit</b>\n\nType the amount you want to deposit in USD — e.g. <b>25</b> or <b>25.50</b>.", kb)
        elif data == "dep_cancel":
            tclear(call.from_user.id)
            show("❌ Deposit cancelled.", back)
        elif data == "dep_methods":
            s = tget(call.from_user.id); amt = s["amount_cents"] if s else 0
            ms = c.execute("SELECT * FROM payment_methods WHERE active=1 ORDER BY sort,id").fetchall()
            if not ms:
                show("⚠️ No payment methods are available yet. Please contact the admin.", back)
            else:
                kb = InlineKeyboardMarkup()
                for mth in ms: kb.add(InlineKeyboardButton(f"💳 {mth['name']}", callback_data=f"dep_method_{mth['id']}"))
                kb.add(InlineKeyboardButton("❌ Cancel", callback_data="dep_cancel"))
                show(f"<b>💰 Deposit {money(amt)}</b>\n\nChoose a payment method:", kb)
        elif data.startswith("dep_method_"):
            mid = int(data[11:]); m = c.execute("SELECT * FROM payment_methods WHERE id=? AND active=1", (mid,)).fetchone()
            if not m: show("⚠️ That payment method is no longer available.", back)
            else:
                tset(call.from_user.id, "confirm", method_id=mid)
                s = tget(call.from_user.id); amt = s["amount_cents"] if s else 0
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton("✅ I have transferred", callback_data="dep_done"))
                kb.add(InlineKeyboardButton("⬅️ Payment methods", callback_data="dep_methods"))
                kb.add(InlineKeyboardButton("❌ Cancel", callback_data="dep_cancel"))
                show(f"<b>💳 {hesc(m['name'])}</b>\n\n{hesc(m['details'])}\n\nAmount to transfer: <b>{money(amt)}</b>\n\nTransfer the amount, then tap ✅ I have transferred.", kb)
        elif data == "dep_done":
            s = tget(call.from_user.id)
            if s and s["step"] == "confirm":
                tset(call.from_user.id, "screenshot")
                kb = InlineKeyboardMarkup(); kb.add(InlineKeyboardButton("❌ Cancel", callback_data="dep_cancel"))
                show("📸 Now please <b>send your payment screenshot</b> as a photo in this chat.", kb)
            else:
                show("⚠️ Please start again from Wallet → Deposit.", back)
        elif data == "invite":
            try: bot_name = bot.get_me().username
            except: bot_name = "your_bot"
            n = c.execute("SELECT COUNT(*) n FROM users WHERE referrer_id=?", (call.from_user.id,)).fetchone()["n"]
            show(f"<b>🤝 Invite & earn</b>\nInvited users: {n}\n\nYour link:\nhttps://t.me/{bot_name}?start=ref_{call.from_user.id}", back)
        elif data == "saved":
            rows = c.execute("SELECT p.* FROM saved s JOIN products p ON p.id=s.product_id WHERE s.user_id=?", (call.from_user.id,)).fetchall()
            show("<b>❤️ Saved</b>\n\n" + ("\n".join(f"• {r['name']} — {money(r['price_cents'])}" for r in rows) or "Nothing saved yet."), back)
        elif data == "new":
            rows = c.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC LIMIT 10").fetchall()
            show("<b>📣 What's new</b>\n\n" + "\n".join(f"• {r['name']} — {money(r['price_cents'])}" for r in rows), back)
        elif data == "help":
            show("<b>🆘 Help</b>\n1. Add funds in Wallet\n2. Open Shop and buy\n3. Auto items deliver instantly; others are delivered by admin.\nContact the shop admin if an order is delayed.", back)
        elif data == "settings":
            show("<b>⚙️ Settings</b>\nLanguage and notifications are coming in the admin dashboard update.", back)
        c.close()

    @bot.message_handler(content_types=["photo"])
    def dep_photo(msg):
        s = tget(msg.from_user.id)
        if not s or s["step"] != "screenshot" or not s["amount_cents"]: return
        try:
            data = bot.download_file(bot.get_file(msg.photo[-1].file_id).file_path)
        except Exception:
            bot.send_message(msg.chat.id, "⚠️ Could not read that photo — please send the screenshot again.")
            return
        c = db(); m = c.execute("SELECT * FROM payment_methods WHERE id=?", (s["method_id"],)).fetchone()
        mname = m["name"] if m else ""
        c.execute("INSERT INTO topups(user_id,amount_cents,method,method_name,screenshot) VALUES(?,?,?,?,?)",
                  (msg.from_user.id, s["amount_cents"], mname, mname, data)); c.commit(); c.close()
        tclear(msg.from_user.id)
        log_chat(msg.from_user.id, "in", "📷 Payment screenshot")
        txt = ("✅ <b>Deposit request received!</b>\n\n"
               f"Amount: <b>{money(s['amount_cents'])}</b>\nMethod: <b>{hesc(mname)}</b>\n\n"
               "The admin will check your screenshot and credit your balance. You can see it in 💳 Wallet.")
        log_chat(msg.from_user.id, "out", plain(txt))
        kb = InlineKeyboardMarkup(); kb.add(InlineKeyboardButton("🏠 Menu", callback_data="home"))
        bot.send_message(msg.chat.id, txt, reply_markup=kb)

    @bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith("/"), content_types=["text"])
    def dep_text(msg):
        s = tget(msg.from_user.id)
        if not s: return
        if s["step"] == "amount":
            raw = (msg.text or "").replace("$", "").replace(",", "").strip()
            try: cents = int(round(float(raw) * 100))
            except Exception: cents = 0
            if cents <= 0:
                bot.send_message(msg.chat.id, "⚠️ Please type a valid amount, e.g. 25")
                return
            tset(msg.from_user.id, "method", amount=cents)
            c = db(); ms = c.execute("SELECT * FROM payment_methods WHERE active=1 ORDER BY sort,id").fetchall(); c.close()
            kb = InlineKeyboardMarkup()
            for mth in ms: kb.add(InlineKeyboardButton(f"💳 {mth['name']}", callback_data=f"dep_method_{mth['id']}"))
            kb.add(InlineKeyboardButton("❌ Cancel", callback_data="dep_cancel"))
            txt = f"<b>💰 Deposit {money(cents)}</b>\n\nChoose a payment method:"
            log_chat(msg.from_user.id, "out", plain(txt))
            bot.send_message(msg.chat.id, txt, reply_markup=kb)
        elif s["step"] == "screenshot":
            bot.send_message(msg.chat.id, "📸 Please send the screenshot as a <b>photo</b> (not text).")
        elif s["step"] in ("method", "confirm"):
            bot.send_message(msg.chat.id, "👆 Please tap the buttons above to continue your deposit — or open 💳 Wallet to cancel.")

    @app.route(f"/webhook", methods=["POST"])
    def webhook():
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        try:
            m0 = update.message or update.edited_message
            if m0 is not None and getattr(m0, "text", None):
                log_chat(m0.from_user.id, "in", m0.text)
                if not (update.message and (m0.text or "").startswith("/start")):
                    pass
        except Exception:
            pass
        bot.process_new_updates([update]); return "ok"

# ---------- Admin web ----------
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin"): return redirect(url_for("login"))
        return f(*a, **k)
    return w

@app.route("/")
def home(): return redirect(url_for("dashboard"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True; return redirect(url_for("dashboard"))
        flash("Wrong password")
    return render_template("login.html", shop=SHOP_NAME)

@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("login"))

@app.route("/emoji-pack")
def emoji_pack():
    # Lists a public Telegram custom-emoji pack's stickers (ids + assigned emoji chars) so
    # the shop can map brand logo emojis to categories. Returns no secrets / file paths.
    if not bot: return {"error": "bot off"}
    name = request.args.get("name", "ApplicationEmoji")
    try:
        s = bot.get_sticker_set(name)
        out = []
        for i, st in enumerate(getattr(s, "stickers", []) or []):
            out.append({"i": i, "emoji": getattr(st, "emoji", ""), "custom_emoji_id": getattr(st, "custom_emoji_id", None),
                        "animated": bool(getattr(st, "is_animated", False)), "video": bool(getattr(st, "is_video", False))})
        return {"pack": name, "title": getattr(s, "title", ""), "count": len(out), "stickers": out}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}

def _u16len(s): return len(s.encode("utf-16-le")) // 2

@app.route("/emoji-sheet")
def emoji_sheet():
    # Builds ONE contact-sheet PNG for a range of pack stickers (thumbnail images + big index
    # numbers) and sends it as a photo to the given chat, so logos can be identified visually.
    if not bot: return {"error": "bot off"}
    chat = request.args.get("chat", "")
    if not chat: return {"error": "chat param required"}
    name = request.args.get("name", "ApplicationEmoji")
    try:
        start = int(request.args.get("from", 0)); count = min(int(request.args.get("count", 30)), 50)
    except Exception:
        return {"error": "bad from/count"}
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io
        s = bot.get_sticker_set(name)
        stickers = list(getattr(s, "stickers", []) or [])[start:start + count]
        cell, cols = 200, 5
        rows = (len(stickers) + cols - 1) // cols
        img = Image.new("RGB", (cols * cell, rows * cell), (24, 26, 32))
        d = ImageDraw.Draw(img)
        try: font = ImageFont.load_default(size=44)
        except Exception: font = ImageFont.load_default()
        placed = 0
        for j, st in enumerate(stickers):
            thumb = getattr(st, "thumbnail", None) or getattr(st, "thumb", None)
            x, y = (j % cols) * cell, (j // cols) * cell
            if thumb is not None:
                try:
                    f = bot.get_file(thumb.file_id)
                    raw = bot.download_file(f.file_path)
                    tile = Image.open(_io.BytesIO(raw)).convert("RGBA")
                    tile.thumbnail((cell - 60, cell - 60))
                    img.paste(tile, (x + 30, y + 10), tile)
                    placed += 1
                except Exception:
                    pass
            d.text((x + cell // 2, y + cell - 52), str(start + j), fill=(255, 210, 60), font=font, anchor="ma")
        buf = _io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
        bot.send_photo(int(chat), buf, caption=f"{name} logos {start}..{start + len(stickers) - 1} — reply with the numbers for your apps")
        return {"ok": True, "from": start, "count": len(stickers), "placed": placed}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}

@app.route("/emoji-catalog")
def emoji_catalog():
    # Sends a numbered catalog of a custom-emoji pack INTO a Telegram chat via the bot,
    # so a human can see which index is which logo (all stickers share the same fallback char).
    if not bot: return {"error": "bot off"}
    chat = request.args.get("chat", "")
    if not chat: return {"error": "chat param required"}
    name = request.args.get("name", "ApplicationEmoji")
    try:
        s = bot.get_sticker_set(name)
        stickers = list(getattr(s, "stickers", []) or [])
        sent = 0
        for chunk_start in range(0, len(stickers), 40):
            chunk = stickers[chunk_start:chunk_start + 40]
            text, entities, off = f"Pack {name} — logos {chunk_start}..{chunk_start + len(chunk) - 1}:\n", [], 0
            off = _u16len(text)
            for j, st in enumerate(chunk):
                i = chunk_start + j
                label = f"{i}:"
                text += label; off += _u16len(label)
                cid = getattr(st, "custom_emoji_id", None)
                placeholder = getattr(st, "emoji", None) or "\U0001F4F1"
                text += placeholder + " "
                if cid:
                    entities.append(telebot.types.MessageEntity(type="custom_emoji", offset=off, length=_u16len(placeholder), custom_emoji_id=str(cid)))
                off += _u16len(placeholder) + 1
            bot.send_message(int(chat), text, entities=entities, parse_mode=None)
            sent += 1
        return {"ok": True, "messages_sent": sent, "stickers": len(stickers)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}

@app.route("/admin")
@login_required
def dashboard():
    c = db(); s = {
      "users": c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
      "orders": c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"],
      "revenue": c.execute("SELECT COALESCE(SUM(price_cents),0) n FROM orders WHERE status!='cancelled'").fetchone()["n"],
      "pending_orders": c.execute("SELECT COUNT(*) n FROM orders WHERE status='pending'").fetchone()["n"],
      "pending_topups": c.execute("SELECT COUNT(*) n FROM topups WHERE status='pending'").fetchone()["n"]}
    c.close(); return render_template("dashboard.html", s=s, money=money, shop=SHOP_NAME)

# Real company logos for the ADMIN dashboard, fetched from public logo sources
# (Simple Icons CDN + Wikimedia Commons) and cached in-process.
_SI = "https://cdn.simpleicons.org/"
_WM = "https://commons.wikimedia.org/wiki/Special:FilePath/"
BRAND_LOGOS = {
  "ChatGPT Plus": _WM + "ChatGPT_logo.svg",
  "Capcut Pro": _WM + "CapCut_logo.svg",
  "Mail Box": _SI + "gmail",
  "Figma": _SI + "figma",
  "Gemini": _SI + "googlegemini",
  "Cursor & Grok": _SI + "cursor",
  "Codex API": _WM + "OpenAI_Logo.svg",
  "Claude": _SI + "anthropic",
  "Muse Ai": _SI + "meta",
  "MS Office": _WM + "Microsoft_logo_%282012%29.svg",
  "n8n": _SI + "n8n",
  "Adobe": _WM + "Adobe_Systems_logo_and_wordmark.svg",
  "Duolingo": _SI + "duolingo",
  "Streaming": _SI + "netflix",
  "VPN": _SI + "protonvpn",
  "Telegram": _SI + "telegram",
  "Japan TikTok Account": _SI + "tiktok",
  "WhatsApp": _SI + "whatsapp",
  "Spotify": _SI + "spotify",
}
_BRAND_CACHE = {}

@app.route("/brand-logo/<int:cat_id>")
def brand_logo(cat_id):
    c = db(); r = c.execute("SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone(); c.close()
    if not r: abort(404)
    url = BRAND_LOGOS.get(r["name"])
    if not url: abort(404)
    from flask import Response
    if cat_id in _BRAND_CACHE:
        raw, mime = _BRAND_CACHE[cat_id]
    else:
        try:
            import urllib.request as _u
            req = _u.Request(url, headers={"User-Agent": "Mozilla/5.0 (shop-admin logo fetch)"})
            raw = _u.urlopen(req, timeout=12).read()
            head = raw[:200].lstrip()
            mime = "image/svg+xml" if (head.startswith(b"<svg") or head.startswith(b"<?xml")) else "image/png"
            _BRAND_CACHE[cat_id] = (raw, mime)
        except Exception:
            abort(404)
    resp = Response(raw, mimetype=mime); resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

@app.route("/logo-img/<cid>")
def logo_img(cid):
    if not bot or not str(cid).isdigit(): abort(404)
    try:
        raw = logo_thumb_bytes(cid)
        if not raw: abort(404)
        from flask import Response
        r = Response(raw, mimetype="image/webp"); r.headers["Cache-Control"] = "public, max-age=86400"
        return r
    except Exception:
        abort(404)

@app.route("/admin/categories", methods=["GET","POST"])
@login_required
def categories():
    c = db()
    if request.method == "POST":
        c.execute("INSERT INTO categories(name,emoji,logo_id) VALUES(?,?,?)", (request.form["name"], request.form.get("emoji",""), request.form.get("logo_id",""))); c.commit()
    rows = c.execute("SELECT * FROM categories ORDER BY sort,id").fetchall()
    logos = {r["id"]: cat_logo(r) for r in rows}
    brands = {r["id"]: (r["name"] in BRAND_LOGOS) for r in rows}
    c.close()
    return render_template("categories.html", rows=rows, logos=logos, brands=brands, logo_options=LOGO_OPTIONS, shop=SHOP_NAME)

@app.route("/admin/categories/<int:cid>/delete")
@login_required
def cat_delete(cid):
    c = db(); c.execute("DELETE FROM categories WHERE id=?", (cid,)); c.commit(); c.close(); return redirect(url_for("categories"))

@app.route("/admin/products", methods=["GET","POST"])
@login_required
def products():
    c = db()
    if request.method == "POST":
        c.execute("INSERT INTO products(category_id,name,description,price_cents) VALUES(?,?,?,?)",
                  (request.form["category_id"], request.form["name"], request.form.get("description",""), int(float(request.form["price"])*100))); c.commit()
    rows = c.execute("SELECT p.*, cat.name cat, (SELECT COUNT(*) FROM stock_items s WHERE s.product_id=p.id AND s.is_sold=0) stock FROM products p LEFT JOIN categories cat ON cat.id=p.category_id ORDER BY p.id DESC").fetchall()
    cats = c.execute("SELECT * FROM categories").fetchall(); c.close()
    return render_template("products.html", rows=rows, cats=cats, money=money, shop=SHOP_NAME)

@app.route("/admin/products/<int:pid>/delete")
@login_required
def prod_delete(pid):
    c = db(); c.execute("DELETE FROM products WHERE id=?", (pid,)); c.commit(); c.close(); return redirect(url_for("products"))

@app.route("/admin/stock", methods=["GET","POST"])
@login_required
def stock():
    c = db()
    if request.method == "POST":
        lines = [x.strip() for x in request.form["content"].splitlines() if x.strip()]
        for line in lines: c.execute("INSERT INTO stock_items(product_id,content) VALUES(?,?)", (request.form["product_id"], line))
        c.commit()
    prods = c.execute("SELECT * FROM products").fetchall()
    rows = c.execute("SELECT s.*, p.name FROM stock_items s JOIN products p ON p.id=s.product_id ORDER BY s.id DESC LIMIT 100").fetchall(); c.close()
    return render_template("stock.html", rows=rows, prods=prods, shop=SHOP_NAME)

@app.route("/admin/orders", methods=["GET","POST"])
@login_required
def orders():
    c = db()
    if request.method == "POST":
        oid = request.form["order_id"]
        c.execute("UPDATE orders SET status=?, delivery=? WHERE id=?", (request.form["status"], request.form.get("delivery",""), oid)); c.commit()
        # notify user if bot is live
        if bot:
            o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            try: bot.send_message(o["user_id"], f"📦 Order #{oid} update: {o['status']}\n{o['delivery']}")
            except: pass
    rows = c.execute("SELECT o.*, p.name FROM orders o LEFT JOIN products p ON p.id=o.product_id ORDER BY o.id DESC LIMIT 100").fetchall(); c.close()
    return render_template("orders.html", rows=rows, money=money, shop=SHOP_NAME)

@app.route("/admin/users")
@login_required
def users():
    c = db(); rows = c.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall(); c.close()
    return render_template("users.html", rows=rows, money=money, tier=tier, shop=SHOP_NAME)

@app.route("/admin/users/<int:tid>/balance", methods=["POST"])
@login_required
def balance(tid):
    c = db(); c.execute("UPDATE users SET balance_cents=balance_cents+? WHERE telegram_id=?", (int(float(request.form["amount"])*100), tid)); c.commit(); c.close()
    return redirect(url_for("users"))

@app.route("/admin/support/<int:tid>/reply", methods=["POST"])
@login_required
def support_reply(tid):
    text = (request.form.get("text") or "").strip()
    photo = request.files.get("attach") or request.files.get("photo")
    if not bot:
        flash("Bot is off (no BOT_TOKEN) — cannot send.")
        return redirect(url_for("support_thread", tid=tid))
    try:
        if photo and photo.filename:
            import io as _io
            cap = text if text else None
            fname = photo.filename
            is_img = (photo.mimetype or "").startswith("image/") or fname.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))
            if is_img:
                buf = _io.BytesIO(photo.read()); buf.name = fname or "photo.jpg"
                bot.send_photo(tid, buf, caption=cap, parse_mode=None)
                log_chat(tid, "out", ("📷 Photo" + (f": {text}" if text else "")))
            else:
                bot.send_document(tid, (fname, _io.BytesIO(photo.read())), caption=cap, parse_mode=None)
                log_chat(tid, "out", ("📎 File: " + fname + (f" — {text}" if text else "")))
        elif text:
            bot.send_message(tid, text, parse_mode=None)
            log_chat(tid, "out", text)
        else:
            flash("Write a message or attach a photo/file first.")
            return redirect(url_for("support_thread", tid=tid))
        flash("Sent to customer in Telegram ✓")
    except Exception as e:
        flash(f"Could not send: {type(e).__name__}: {str(e)[:150]}")
    return redirect(url_for("support_thread", tid=tid))

@app.route("/admin/topups")
@login_required
def topups():
    c = db(); rows = c.execute("SELECT * FROM topups ORDER BY id DESC").fetchall(); c.close()
    return render_template("topups.html", rows=rows, money=money, shop=SHOP_NAME)

@app.route("/admin/topups/<int:tid>/img")
@login_required
def topup_img(tid):
    c = db(); r = c.execute("SELECT screenshot FROM topups WHERE id=?", (tid,)).fetchone(); c.close()
    if not r or not r["screenshot"]: abort(404)
    from flask import Response
    return Response(r["screenshot"], mimetype="image/jpeg")

@app.route("/admin/bot-ui", methods=["GET","POST"])
@login_required
def bot_ui():
    if request.method == "POST":
        if request.form.get("section") == "design":
            for key in ("cat_cols", "prod_cols"):
                v = request.form.get(key, "")
                if v in ("1", "2"): set_setting(key, v)
            set_setting("shop_title", request.form.get("shop_title", "").strip())
            set_setting("shop_subtitle", request.form.get("shop_subtitle", "").strip())
            set_setting("show_counts", "1" if request.form.get("show_counts") else "0")
            flash("Design settings saved — the bot uses them immediately.")
        elif request.form.get("section") == "cats":
            c = db()
            for r in c.execute("SELECT id FROM categories").fetchall():
                cid = r["id"]
                nm = request.form.get(f"cat_{cid}_name", "").strip()
                em = request.form.get(f"cat_{cid}_emoji", "")
                lg = request.form.get(f"cat_{cid}_logo", "")
                if nm: c.execute("UPDATE categories SET name=?, emoji=?, logo_id=? WHERE id=?", (nm, em, lg, cid))
            c.commit(); c.close()
            flash("Category name / emoji / logo saved — live in the bot now.")
        elif request.form.get("section") == "pay_add":
            nm = request.form.get("pm_name", "").strip()
            if nm:
                c = db(); mx = c.execute("SELECT COALESCE(MAX(sort),0) m FROM payment_methods").fetchone()["m"]
                c.execute("INSERT INTO payment_methods(name,details,active,sort) VALUES(?,?,1,?)",
                          (nm, request.form.get("pm_details", ""), mx + 1)); c.commit(); c.close()
                flash("Payment method added — customers can use it in the bot now.")
            else:
                flash("Payment method name is required.")
        elif request.form.get("section") == "pay_one":
            pmid = request.form.get("pm_id", "")
            c = db()
            if request.form.get("act") == "delete":
                c.execute("DELETE FROM payment_methods WHERE id=?", (pmid,))
                flash("Payment method deleted.")
            else:
                c.execute("UPDATE payment_methods SET name=?, details=?, active=? WHERE id=?",
                          (request.form.get("pm_name", "").strip() or "Method", request.form.get("pm_details", ""),
                           1 if request.form.get("pm_active") else 0, pmid))
                flash("Payment method saved — live in the bot now.")
            c.commit(); c.close()
        return redirect(url_for("bot_ui"))
    c = db(); cats = c.execute("SELECT * FROM categories ORDER BY sort,id").fetchall()
    pmethods = c.execute("SELECT * FROM payment_methods ORDER BY sort,id").fetchall(); c.close()
    logos = {r["id"]: cat_logo(r) for r in cats}
    brands = {r["id"]: (r["name"] in BRAND_LOGOS) for r in cats}
    return render_template("bot_ui.html", cats=cats, logos=logos, brands=brands, logo_options=LOGO_OPTIONS, pmethods=pmethods,
                           cat_cols=get_setting("cat_cols", "2"), prod_cols=get_setting("prod_cols", "1"),
                           shop_title=get_setting("shop_title", ""), shop_subtitle=get_setting("shop_subtitle", ""),
                           show_counts=get_setting("show_counts", "1"), shop=SHOP_NAME)

@app.route("/admin/support")
@login_required
def support():
    c = db()
    rows = c.execute("""SELECT m.user_id, u.username, u.first_name, COUNT(*) n, MAX(m.ts) last_ts,
                        (SELECT text FROM chat_messages WHERE user_id=m.user_id ORDER BY id DESC LIMIT 1) last_text
                        FROM chat_messages m LEFT JOIN users u ON u.telegram_id=m.user_id
                        GROUP BY m.user_id ORDER BY last_ts DESC""").fetchall()
    c.close()
    return render_template("support.html", rows=rows, shop=SHOP_NAME)

@app.route("/admin/support/<int:tid>")
@login_required
def support_thread(tid):
    c = db()
    user = c.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
    msgs = c.execute("SELECT * FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 200", (tid,)).fetchall()[::-1]
    c.close()
    return render_template("support_thread.html", user=user, tid=tid, msgs=msgs, shop=SHOP_NAME)

@app.route("/admin/topups/<int:tid>/<act>")
@login_required
def topup_act(tid, act):
    c = db(); t = c.execute("SELECT * FROM topups WHERE id=?", (tid,)).fetchone()
    if t and t["status"] == "pending":
        if act == "approve":
            c.execute("UPDATE users SET balance_cents=balance_cents+? WHERE telegram_id=?", (t["amount_cents"], t["user_id"]))
            c.execute("UPDATE topups SET status='approved' WHERE id=?", (tid,))
        elif act == "reject": c.execute("UPDATE topups SET status='rejected' WHERE id=?", (tid,))
        c.commit()
    c.close(); return redirect(url_for("topups"))

@app.route("/health")
def health(): return {"ok": True, "bot": bool(BOT_TOKEN), "shop": SHOP_NAME}

@app.route("/bot-status")
def bot_status():
    # Safe diagnostics: never returns the token itself.
    if not bot: return {"bot": False}
    try:
        me = bot.get_me()
        info = bot.get_webhook_info()
        c = db(); n = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        ev = [dict(r) for r in c.execute("SELECT kind, detail, ts FROM events ORDER BY id DESC LIMIT 5").fetchall()]; c.close()
        return {"bot": True, "bot_username": me.username, "bot_id": me.id, "db_users": n, "events": ev,
                "webhook_url": info.url or "", "pending_updates": info.pending_update_count,
                "last_error": info.last_error_message or "", "max_connections": info.max_connections}
    except Exception as e:
        return {"bot": True, "webhook_error": str(e)[:200]}

def setup_webhook():
    base = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    print(f"[startup] BOT set: {bool(BOT_TOKEN)}, base URL: {base}", flush=True)
    if bot and base:
        try:
            bot.remove_webhook(); time.sleep(1)
            ok = bot.set_webhook(url=base.rstrip("/") + "/webhook")
            print(f"[startup] set_webhook -> {ok} for {base.rstrip('/')}/webhook", flush=True)
        except Exception as e:
            print(f"[startup] set_webhook FAILED: {e}", flush=True)

if __name__ == "__main__":
    if bot and not (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")):
        threading.Thread(target=bot.infinity_polling, daemon=True).start()
    else:
        setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    setup_webhook()
