"""
Telegram Digital Shop Bot + Admin Web Dashboard
- Bot UI matches the reference: profile card, Shop / Orders / Wallet / Invite etc.
- Admin web at /admin to manage categories, products, stock, orders, users, top-ups.
- Runs on Render via webhook (RENDER_EXTERNAL_URL) or locally via polling.
Only sell products you are authorised to sell.
"""
import os, sqlite3, secrets, threading, time
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
    CREATE TABLE IF NOT EXISTS saved(
      user_id INTEGER, product_id INTEGER, PRIMARY KEY(user_id, product_id));
    """)
    if c.execute("SELECT COUNT(*) n FROM categories").fetchone()["n"] == 0:
        seed = [
            ("ChatGPT Plus","🤖"),("Capcut Pro","🎬"),("Mail Box","📧"),("Figma","🎨"),
            ("Gemini","✨"),("Cursor & Grok","💻"),("Codex API","🧩"),("Claude","🧡"),
            ("Muse Ai","〰️"),("MS Office","🪟"),("n8n","🛍️"),("Adobe","🅰️"),
            ("Duolingo","🦉"),("Streaming","📺"),("VPN","🌀"),
        ]
        for i,(name,emoji) in enumerate(seed):
            cur = c.execute("INSERT INTO categories(name,emoji,sort) VALUES(?,?,?)",(name,emoji,i))
            c.execute("INSERT INTO products(category_id,name,description,price_cents) VALUES(?,?,?,?)",
                      (cur.lastrowid, f"{name} — 1 Month", "Admin: edit this product, set your real price and add stock in the dashboard.", 1000))
    c.commit(); c.close()

def money(cents): return f"${cents/100:.2f}"
def tier(total_spent_cents):
    if total_spent_cents >= 50000: return "Gold"
    if total_spent_cents >= 10000: return "Silver"
    return "Bronze"

init_db()

# ---------- Telegram Bot (optional until BOT_TOKEN is set) ----------
bot = None
if BOT_TOKEN:
    import telebot
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

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
        username = f"@{u['username']}" if u["username"] else "-"
        try: bot_name = bot.get_me().username
        except: bot_name = "your_bot"
        text = (f"<b>{u['first_name'] or username}</b>\n\n"
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
        if edit and hasattr(msg_or_call, "message"):
            bot.edit_message_text(text, chat_id, msg_or_call.message.message_id, reply_markup=kb)
        else:
            bot.send_message(chat_id, text, reply_markup=kb)

    @bot.message_handler(commands=["start"])
    def start(msg): main_menu(msg)

    @bot.message_handler(commands=["menu"])
    def menu(msg): main_menu(msg)

    @bot.callback_query_handler(func=lambda c: True)
    def cb(call):
        c = db(); data = call.data
        def show(text, kb=None):
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
        back = InlineKeyboardMarkup(); back.add(InlineKeyboardButton("⬅️ Back", callback_data="home"))
        if data == "home":
            c.close(); main_menu(call, edit=True); return
        if data == "shop":
            cats = c.execute("SELECT cat.*, (SELECT COUNT(*) FROM stock_items s JOIN products p ON p.id=s.product_id WHERE p.category_id=cat.id AND s.is_sold=0) stock FROM categories cat ORDER BY sort").fetchall()
            kb = InlineKeyboardMarkup(row_width=2)
            for cat in cats:
                cnt = c.execute("SELECT COUNT(*) n FROM products WHERE category_id=? AND active=1", (cat["id"],)).fetchone()["n"]
                kb.add(InlineKeyboardButton(f"{cat['emoji']} {cat['name']} ({cnt})", callback_data=f"cat_{cat['id']}"))
            kb.add(InlineKeyboardButton("⬅️ Back", callback_data="home")); show(f"<b>🛍️ {SHOP_NAME}</b>\nChoose a category:", kb)
        elif data.startswith("cat_"):
            cid = int(data[4:]); prods = c.execute("SELECT * FROM products WHERE category_id=? AND active=1", (cid,)).fetchall()
            kb = InlineKeyboardMarkup(row_width=1)
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
            kb = InlineKeyboardMarkup(); kb.add(InlineKeyboardButton("➕ Add $10 (request top-up)", callback_data="topup_1000"))
            kb.add(InlineKeyboardButton("⬅️ Back", callback_data="home"))
            show(f"<b>💳 Wallet</b>\nBalance: <b>{money(u['balance_cents']) if u else '$0.00'}</b>\n\nTop-ups are approved by admin in the web dashboard.", kb)
        elif data.startswith("topup_"):
            amt = int(data[6:]); c.execute("INSERT INTO topups(user_id,amount_cents,method) VALUES(?,?,'manual')", (call.from_user.id, amt)); c.commit()
            show("✅ Top-up request sent. Admin will approve after you pay them.", back)
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

    @app.route(f"/webhook", methods=["POST"])
    def webhook():
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
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

@app.route("/admin/categories", methods=["GET","POST"])
@login_required
def categories():
    c = db()
    if request.method == "POST":
        c.execute("INSERT INTO categories(name,emoji) VALUES(?,?)", (request.form["name"], request.form.get("emoji",""))); c.commit()
    rows = c.execute("SELECT * FROM categories ORDER BY sort,id").fetchall(); c.close()
    return render_template("categories.html", rows=rows, shop=SHOP_NAME)

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

@app.route("/admin/topups")
@login_required
def topups():
    c = db(); rows = c.execute("SELECT * FROM topups ORDER BY id DESC").fetchall(); c.close()
    return render_template("topups.html", rows=rows, money=money, shop=SHOP_NAME)

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
        c = db(); n = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]; c.close()
        return {"bot": True, "bot_username": me.username, "bot_id": me.id, "db_users": n,
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
