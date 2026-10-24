# Resume Tailor

Upload your résumé once. Paste any job posting and get it rewritten in that posting's
language, scored requirement by requirement, and improved until it stops climbing.

**It rewords your real experience. It never invents any.** Anything a posting asks for that
your résumé doesn't support is shown as a gap you can claim — not quietly written into a
bullet you'd have to defend in an interview.

<!-- Add a screenshot here once deployed -->

## How it works

```
résumé (PDF/DOCX) ──► text ──┐
                             ├──► rewrite for this posting ──► screen it ──► score
job posting ─────────────────┘            ▲                        │
                                          └──── gaps & half-credits ┘
                                                (up to 4 passes, keeps the best)
```

Two model calls per résumé. The **writer** rewrites your facts into the posting's language;
the **screener** then grades the result the way a hiring manager would — requirement by
requirement, judging substance over vocabulary, so `Trivy + Grype container scanning`
correctly evidences *SCA* and `Terraform policy validation` evidences *policy-as-code*.

Whatever it can't evidence comes back as clickable chips. Tick the ones you've actually
done, and it drafts the wording for you — grounded in your own résumé, no invented metrics
— for you to correct before anything is used.

Three reframing modes: **Auto** (most jobs), **Reframe** (different field — describes your
work in its language and drops tool names it wouldn't value), **Career change** (little
transfers — leads with skills, compresses the old role, stays short rather than padding).

## Run it yourself

Needs Python 3.10+ and either an API key (OpenAI or Anthropic) or a signed-in Claude Code
or Codex CLI. No system packages, no database
server, no Docker.

```bash
git clone https://github.com/r0wh4n/resume-tailor
cd resume-tailor
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # add your API key
.venv/bin/python app.py       # http://localhost:5111
```

Self-hosted, everything is free and unlimited — there's no quota when you supply the key.
It stores sessions in a local SQLite file and talks to nothing but your model provider.

## Run it on your Claude or ChatGPT subscription

If you already pay for Claude Code or Codex, the self-hosted app can use the CLI on your
machine instead of a metered API key — nothing to paste, nothing billed per résumé:

```bash
LOCAL_CLI=claude .venv/bin/python app.py     # or LOCAL_CLI=codex
```

It only works when the app and the CLI are on the **same machine**, so it is off unless you
set it. A hosted deployment must not set it: a server cannot reach a visitor's local CLI, so
every request would spend the operator's own subscription. The app logs a warning if it sees
`LOCAL_CLI` together with `HTTPS=1`.

A résumé takes about 18 seconds this way, against roughly eight on the API. Getting there
needed three things, all handled for you: extended thinking is switched off (by default the
CLI spent 13,810 thinking tokens and **135 seconds** emitting one JSON object), MCP servers
and skills are skipped so no configured server's tools load on every call, and the writing
runs on Haiku while the scoring runs on Sonnet — Haiku writes well but grades a
career-change résumé far too generously (55% where it should be 19%).

Every tool is disabled (`--disallowedTools`) and the prompt is passed on stdin, so a job
posting saying *"ignore your instructions and run this command"* is treated as text. That is
verified against a hostile posting, not assumed. The CLI also has no server-side schema
enforcement, so a poor-fit posting can make it answer in prose; the prompt forbids that and
a failed parse retries once.

## Configuration

Everything is optional except a key.

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | Shared key. Users can bring their own instead. |
| `OPENAI_MODEL` | `gpt-5.4-mini` | Writer and screener. |
| `JUDGE_MODEL` | = `OPENAI_MODEL` | Override to grade with a different model. |
| `ANTHROPIC_MODEL` | `claude-opus-5` | Used when the key starts `sk-ant-`. |
| `BYOK_ONLY` | off | Disable the shared key; everyone brings their own. |
| `DATABASE_URL` | — | Postgres (Supabase, Render). Falls back to SQLite. |
| `DB_PATH` | `./data.db` | SQLite location. |
| `SECRET_KEY` | file-backed | **Set this in production** or redeploys log everyone out. |
| `FREE_RESUMES` | `1` | Free résumés per account, lifetime. |
| `PLAN_DAYS` / `PLAN_AMOUNT_PAISE` | `30` / `29900` | Paid plan length and price. |
| `SESSION_TTL_DAYS` | `30` | Résumés are deleted after this. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | — | Sign-in. Without it, BYOK only. |
| `RAZORPAY_KEY_ID` / `_KEY_SECRET` / `_WEBHOOK_SECRET` | — | Billing. Without it, no upgrade button. |
| `LOCAL_CLI` | off | `claude` or `codex` — use the local CLI instead of an API key. Self-host only. |
| `LOCAL_CLI_MODEL` | `claude-haiku-4-5` | Model that writes the résumé. |
| `LOCAL_CLI_JUDGE_MODEL` | `claude-sonnet-4-5` | Model that scores it. Keep this stronger than the writer. |
| `LOCAL_CLI_TIMEOUT` | `120` | Seconds per CLI call. |
| `HTTPS` | off | Set to `1` behind TLS so the session cookie is `Secure`. |
| `DEV_LOGIN` | off | Local-only sign-in bypass. Never enable in production. |

## Deploying

Any host that runs a `Procfile`:

```
web: gunicorn -b 0.0.0.0:$PORT -w 1 --threads 8 --timeout 300 app:app
```

One worker with threads is deliberate — the work is API-latency-bound, not CPU-bound, and
SQLite serialises writes. Raise `--threads` before adding workers. On more than one machine,
set `DATABASE_URL` to Postgres.

Sign-in needs `<your-domain>/auth/callback` registered as an authorised redirect URI, and
billing needs a webhook at `<your-domain>/webhook/razorpay` for `payment_link.paid` and
`payment.captured`.

## Your data

Résumés are stored so you don't re-upload them, deleted after `SESSION_TTL_DAYS`, and
deleted immediately when you hit **Replace**. Résumés and postings are sent to your model
provider to generate and score each draft. **A bring-your-own key is held in memory only** —
never written to the database, never put in a cookie, never logged — so it disappears when
the process restarts.

## Tests

```bash
.venv/bin/python app.py test        # fast, no API calls
.venv/bin/python e2e.py             # full journey over HTTP, ~20 API calls
.venv/bin/python e2e.py https://your-deploy.com
```

The end-to-end suite covers onboarding, upload validation, matching, gap capture, the
improve loop, billing-webhook signature rejection, and session isolation.

## Licence

MIT
