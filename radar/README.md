# Gnarworks Radar

Finds Reddit threads where Waste Finder or CheckPunch would genuinely help, scores them 0-10, and drafts a reply in your voice. **It never posts.** You read the thread, edit the draft, and post it yourself.

## Setup (10 min)

1. **Python 3.11+**. No pip installs needed.
2. **Reddit app (free):** go to https://www.reddit.com/prefs/apps, click "create another app", pick **script**, name it `gnarworks-radar`, set the redirect URI to `http://localhost:8080`. The client ID is the string under the app name; the secret is labeled "secret".
3. Copy `.env.example` to `.env` and fill in the Reddit values. Add `ANTHROPIC_API_KEY` for scoring and drafts (about a cent or two per run). Telegram is optional.
4. Run it: `python radar.py`. It writes `out/digest-<time>.html`. Open that in a browser.

## Run it daily

- **Raspberry Pi (best):** `crontab -e`, then add  
  `0 8,17 * * * cd /home/pi/gnarworks/radar && /usr/bin/python3 radar.py >> radar.log 2>&1`  
  This runs at 8am and 5pm. With Telegram set up, the top hits land on your phone.
- **Windows:** Task Scheduler → Create Basic Task → Daily → Start a program: `python`, arguments `radar.py`, start in: this folder.

## Tuning

Edit `config.toml`: keywords, subreddits, `min_fit`, and your voice. Subreddits that don't exist get skipped with a warning. `python radar.py --reset` forgets which posts it has already shown you.

## Ground rules (so you don't get banned)

- Answer the question first. Only link when they're asking for tools and the sub allows it (the "link OK" badge checks the sub's rules).
- Always say "I built this" when you mention your own tool.
- Edit every draft so it sounds like you. Never paste one unread.
- Keep it to a few replies a day, and mostly don't link at all.
- It's read-only and low volume (well under Reddit's free 100 requests/min), for personal use. Selling it as a product would need Reddit's commercial approval.
