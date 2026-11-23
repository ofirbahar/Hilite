# Hilite

Brand Visibility Monitor.

Checks how a company shows up online, finds what needs fixing, and tells a person what to do next.

This is the diagnosis stage: the company's own website, and how AI assistants answer buyers.

## What works today

| Part | Kind | What it does |
| --- | --- | --- |
| `site-crawler` | collector | Visits the client's site, obeys robots.txt, brings back facts per page |
| `ai-visibility` | collector | Asks AI assistants the buyer questions. Records mentions, competitors, cited sites |
| `technical-seo` | analyzer | Turns crawl facts into ranked findings (impact and effort) |
| `ai-gap` | analyzer | Share of AI answers, questions where competitors win, sites the AI trusts |
| notebook | storage | SQLite file. Every fact and finding is saved with date and source |
| report | action | One HTML page in Hebrew: one score, the top three things to fix, then the rest |
| website | app | Landing page, check form, waiting screen, report (one score and three tasks), past checks |

If one part fails, the others carry on and the failure appears in the report.

## Run it

```bash
pip install -r requirements.txt
python -m bvm web                 # then open http://127.0.0.1:8000
```

Or from the command line, with a settings file:

```bash
cp clients/example.yaml clients/acme.yaml   # edit name, slug, site
python -m bvm run clients/acme.yaml
```

The notebook is `bvm.sqlite3`. Command-line reports go to `reports/`.

## Switching on the AI check

The AI check runs only when buyer questions are entered and at least one assistant
has a key. Each question costs money, so runs are capped at 25 questions.

```bash
export ANTHROPIC_API_KEY=...                                  # Claude
export OPENAI_API_KEY=...      BVM_OPENAI_MODEL=<model name>   # ChatGPT
export PERPLEXITY_API_KEY=...  BVM_PERPLEXITY_MODEL=<model name>
```

Model names change often, so OpenAI and Perplexity need one set explicitly. Check each
provider's current model list. Perplexity moved its chat API to a newer "Agent API" in
2026, so its address (`BVM_PERPLEXITY_URL`) and request format may need updating.

Known limit: a plain API answer is not identical to what a person sees in the
assistant's own app, which may search the web. Only assistants whose API returns
sources (or writes links in the answer) contribute to the "sites the AI trusts" finding.

## Before putting the web interface online

It has no accounts or login, so run it on your own machine only. It refuses addresses
inside the server's network and checks every redirect the same way. One gap remains: a
host name could change where it points between the check and the request, so put it
behind a firewall that blocks outgoing requests to private addresses before going online.

Checks started from the web form ask the AI assistants as a buyer in Israel. A check
that was cut off by a restart is marked as failed about ten minutes after it started.

## A clickable demo without a server

```bash
python -m tests.demo demo-out      # writes demo-out/site-demo.html with sample data
```

## Test it

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests start a small fake website on this machine with known problems planted in it.

## Layout

```
bvm/
  config.py                  one settings file per client
  store.py                   the shared notebook (SQLite)
  collectors/                site_crawler.py, ai_visibility.py
  analyzers/                 technical_seo.py, ai_gap.py
  ai_providers.py            the AI assistants and their keys
  pipeline.py                prepare, collect, analyze, rank
  report.py + templates/     the report for a human
  web.py                     the web interface
  cli.py
clients/                     settings files
tests/
```

## Not built yet

Search Console and Bing, directories, Reddit, news, scanning competitors' own sites,
the other analyzers, alerts, drafts, scheduling, accounts and payments.
