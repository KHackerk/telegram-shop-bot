# Telegram Digital Shop Bot + Admin Website

A Telegram shop like the reference screenshots, with a web admin dashboard to control it from behind.

## Bot features (what buyers see)
- Profile card: Tier (Bronze/Silver/Gold), User ID, Username, Balance, Total spent, Order counts, personal invite link
- Main menu: 🛍️ Shop · 📦 My orders · 💳 Wallet · 📣 What's new · 🤝 Invite & earn · ❤️ Saved · 🆘 Help · ⚙️ Settings
- Shop by category (ChatGPT Plus, Capcut Pro, Mail Box, Figma, Gemini, Cursor & Grok, Codex API, Claude, Muse Ai, MS Office, n8n, Adobe, Duolingo, Streaming, VPN — all editable in admin)
- Buy with wallet balance: stock items deliver instantly; out-of-stock items create a Pending order for manual delivery
- Wallet top-up requests (admin approves), referral invite links, saved items

## Admin website features (what you control)
- Dashboard stats, Categories, Products & prices, Stock (one auto-delivery item per line)
- Orders: mark delivered/confirmed/cancelled + delivery text — the buyer gets a Telegram notification
- Users: view tier/balance/spent, adjust balance
- Top-ups: approve / reject (approving credits the user's wallet)

> Only list products you are authorised to sell. The seeded products are placeholders — edit names, prices and stock before selling.

## 1) Create the Telegram bot
1. In Telegram open **@BotFather** → `/newbot` → choose a name and username (ends in `bot`)
2. BotFather gives you a **BOT_TOKEN**. Keep it secret — you'll paste it into Render, not into GitHub.

## 2) Put this on GitHub
```bash
git init && git add . && git commit -m "Telegram shop bot"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```
(Create the empty repo first at github.com/new — or tell Muse your GitHub repo and it can push for you once you're signed in.)

## 3) Deploy on Render
1. render.com → **New + → Blueprint** → connect this GitHub repo (Render reads `render.yaml`), or **New Web Service** with:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
2. Set environment variables:
   - `BOT_TOKEN` = your BotFather token
   - `ADMIN_PASSWORD` = a strong password for /admin
   - `SHOP_NAME` = your shop name
3. Deploy. Open `https://<your-app>.onrender.com/health` → should show `{"ok": true, "bot": true}`
4. Admin dashboard: `https://<your-app>.onrender.com/admin`
5. In Telegram, send `/start` to your bot.

Note: Render's free disk is temporary, so SQLite data resets on redeploy. For a real shop, switch `DATABASE_PATH`/add a Render Postgres DB before taking real money.

## Run locally
```bash
pip install -r requirements.txt
BOT_TOKEN=xxx ADMIN_PASSWORD=admin123 python app.py
# Admin: http://localhost:5000/admin  (default local password admin123 if ADMIN_PASSWORD not set)
```
