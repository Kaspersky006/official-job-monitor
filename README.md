# Official Careers Job Monitor

A real hourly monitor for Working Student and Internship roles. It checks only company-owned careers pages and official ATS pages linked from them, remembers previously reported roles, and sends an actual Gmail message after every run.

## What it does

- Monitors the 40 companies in `companies.yml`.
- Runs hourly through GitHub Actions.
- The Python script checks Berlin local time and only scans from 11:00 through 23:59.
- Matches Working Student, Werkstudent, Internship, Praktikum and thesis postings.
- Prioritizes DevOps, CI/CD, Python, embedded software, ML, autonomous systems, robotics, testing, IT, electrical/control engineering, Linux, Docker, MATLAB/Simulink and ROS.
- First run reports all currently matching openings.
- Later runs report only newly discovered openings.
- Sends a heartbeat email even when no new role is found.
- Stores hashes in `data/seen_jobs.json` so jobs are not repeatedly reported.

## Setup — about 10 minutes

### 1. Create a private GitHub repository

Create an empty private repository, for example `official-job-monitor`.

### 2. Upload this project

From the extracted project folder:

```bash
git init
git add .
git commit -m "Initial official job monitor"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/official-job-monitor.git
git push -u origin main
```

### 3. Create a Gmail app password

Your Gmail account must have 2-Step Verification enabled.

In Google Account → Security → App passwords, create an app password named `GitHub Job Monitor`. Copy the generated 16-character password.

### 4. Add GitHub repository secrets

Repository → Settings → Secrets and variables → Actions → New repository secret:

- `SMTP_USERNAME`: the Gmail address that sends the emails, normally `akashckb29@gmail.com`
- `SMTP_APP_PASSWORD`: the 16-character Gmail app password (spaces are optional)

Never put the app password in a source-code file.

### 5. Test immediately

Go to Actions → Official Job Monitor → Run workflow.

The first run ignores the normal time window only if you temporarily add this environment variable to the workflow:

```yaml
FORCE_RUN: "true"
```

Alternatively, run locally:

```bash
pip install -r requirements.txt
export SMTP_USERNAME="akashckb29@gmail.com"
export SMTP_APP_PASSWORD="your-app-password"
export ALERT_EMAIL="akashckb29@gmail.com"
export FORCE_RUN="true"
python src/monitor.py
```

After testing, remove `FORCE_RUN` so scheduled runs remain restricted to 11:00–23:59 Berlin time.

## Important GitHub setting

Repository → Settings → Actions → General → Workflow permissions:

Select **Read and write permissions**. This allows the workflow to save `seen_jobs.json` after each run.

## Troubleshooting

Every email includes a per-company run report. A blocked or unavailable site appears under `WARNINGS`, while the remaining companies are still checked. GitHub Actions also preserves the complete run log.

Some companies change their career-page URLs or ATS provider. Update only the affected `careers_url` in `companies.yml`; the scanning and deduplication logic stays unchanged.
