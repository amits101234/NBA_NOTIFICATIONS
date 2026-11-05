# 🏀 NBA Highlights Watcher

Spoiler-free WhatsApp alerts when your YouTube channels post highlights of your teams' games.

**How it works:** every 5 minutes GitHub runs `watcher.py`. Most runs do nothing. Once a day it reads the NBA schedule for your teams. When one of their games is over, it checks your channels on each run. As soon as a channel posts the full-game highlights, you get one WhatsApp message with the matchup, the channel and the link. It never sends the score or the video title. When every channel has posted, or after 4 hours, it stops checking until your teams' next game.

---

## Setup (about 10 minutes)

### 1. Get your CallMeBot key
1. On your phone, open https://www.callmebot.com/blog/free-api-whatsapp-messages/ and add the bot's number to your contacts (the number is listed on that page).
2. Send it this WhatsApp message: `I allow callmebot to send me messages`
3. It replies with your **API key**. Keep it handy.

### 2. Create the GitHub repository
1. Go to https://github.com/new and name it, for example, `nba-highlights-watcher`.
2. Choose **Public**. Public repos get unlimited free GitHub Actions minutes, and a private repo would run out of free minutes at a 5-minute interval. Your phone number and key stay hidden as secrets (step 4), and the logs never show scores or titles.
3. Click **Create repository**, then **uploading an existing file**, and drag in **everything** from this folder, including the `.github` folder. On a Mac, press `Cmd + Shift + .` in Finder to see hidden folders. Then click **Commit changes**.

### 3. Fill in `config.yaml`
In the repo, open `config.yaml`, click the ✏️ pencil, and replace `TEAM_1`, `TEAM_2` and the channel placeholders with your own. Commit.

### 4. Add your two secrets
In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**:

| Name               | Value                                       |
|--------------------|---------------------------------------------|
| `CALLMEBOT_PHONE`  | your number with country code, e.g. `+972501234567` |
| `CALLMEBOT_APIKEY` | the key from step 1                         |

### 5. Turn it on and test
1. Open the **Actions** tab. If GitHub asks, click **I understand… enable them**.
2. Click **NBA highlights watcher → Run workflow**, choose **test-whatsapp**, then click **Run**. A test WhatsApp should arrive within a minute.
3. Run it again with **check**. Open the run's log to see your channels resolved and your teams' upcoming games, listed by tip-off time only.

That's it. From now on it runs by itself.

---

## Good to know
- **Link previews:** the test message includes a harmless YouTube link. If WhatsApp shows a preview with a title or thumbnail, preview text could spoil a real game, so let Claude know and the link format can be changed.
- **Timing:** GitHub runs every 5 minutes, but at busy times a run can be delayed by a few minutes.
- **Adding or changing teams:** edit `teams:` in `config.yaml` (up to 3 is ideal).
- **Wrong video matched or missed?** Adjust `title_must_contain` / `title_must_not_contain` in `config.yaml`.
- **Pause it:** go to Actions → NBA highlights watcher → ⋯ → Disable workflow.
- `state.json` is the bot's memory (which games it has already handled). The workflow updates it automatically, so don't edit it.
