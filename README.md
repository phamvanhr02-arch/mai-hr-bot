# Mai HR Bot

Webhook server for **Mai - HR Assistant** Lark bot. Receives Lark events, queries Dify HR Agent, replies via Lark API.

## Stack
- Flask + gunicorn (Python 3.11)
- Lark Open API (event subscription, message reply)
- Dify Cloud (HR knowledge base + chat agent)

## Deploy on Render

1. Fork/push repo to GitHub
2. New Web Service on Render → connect repo
3. Render auto-detects `render.yaml`
4. Set 2 secret env vars in Render dashboard:
   - `LARK_APP_SECRET` (from Lark Developer Console → Credentials)
   - `DIFY_API_KEY` (from Dify HR Agent → API Access)
5. Deploy → get URL like `https://mai-hr-bot.onrender.com`
6. Paste `https://mai-hr-bot.onrender.com/webhook` into Lark Event Subscription Request URL

## Keep-alive (free tier sleeps after 15 min)

Free tier ping with UptimeRobot:
- Sign up at uptimerobot.com (free)
- Add HTTP monitor → `https://mai-hr-bot.onrender.com/` every 5 min
- Server stays awake during business hours.

## Local dev

```
pip install -r requirements.txt
cp .env.template .env  # fill in secrets
python webhook.py
```
