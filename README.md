# Job monitor

Checks company career pages every hour and sends a Telegram message when a new
working-student or internship posting appears. Runs free on GitHub Actions.

## Setup (about 10 minutes)

1. **Telegram bot.** In Telegram, open @BotFather, send `/newbot`, follow the steps, and copy the bot token.
2. **Your chat ID.** Send any message to your new bot. Open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number
   after `"chat":{"id":`.
3. **GitHub repo.** Create a new **public** repository (public keeps Actions minutes free)
   and upload every file in this folder, including the hidden `.github` folder.
4. **Secrets.** Repo, Settings, Secrets and variables, Actions, New repository secret:
   - `TELEGRAM_BOT_TOKEN` = the bot token
   - `TELEGRAM_CHAT_ID` = your chat ID
5. **Allow saving state.** Settings, Actions, General, Workflow permissions:
   choose "Read and write permissions", Save.
6. **First run.** Actions tab, job-monitor, Run workflow. You get one Telegram message
   listing which sources work and which do not. After that, it runs every hour.

## How it behaves

- The first successful check of a company only records the jobs already on the page (no alert spam).
- Later runs alert only for links it has not seen before.
- A page with fewer than 5 links, or an error, counts as broken: it is reported in the
  health message and never causes false alerts.
- A health summary arrives once a day at about 08:00 German time.

## Fixing a source

Edit `companies.yml`: change the `url`, or the `type` (`html`, `playwright`, `recruitee`).
Run locally with `python monitor.py --dry-run --only "Audi"` to see what it finds.

## Notes

- Hourly runs can be a few minutes late. GitHub may pause schedules after 60 days with no repo activity.
- Matching words and excluded fields are in `companies.yml` (`include` and `exclude`).
