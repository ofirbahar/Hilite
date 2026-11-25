# Hilite


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

## Sites that are built in the browser

Some sites send an empty page and build it with JavaScript. To read those, install the
optional browser once:

```bash
pip install -r requirements-render.txt
python -m playwright install chromium --only-shell
```

The scan then opens such pages in a headless browser (up to 12 per scan) and checks the
real content. It still reports that crawlers see an empty page, because that stays true
for search and AI crawlers. Without the browser the scan runs and reports only that.

## A business with no site of its own

The address can be a page on Facebook, Instagram, TikTok, Linktree, Wolt, 10bis, easy,
rest, Tripadvisor or Google Maps. Only that one page is read, never the rest of the
platform, and the business still gets a full report and a score from what can be measured:

- whether the platform keeps the page closed to AI engines (from its published rules)
- what the page says: enough text, opening hours, a phone number
- that the business has no site of its own
- its Google profile, when `GOOGLE_PLACES_API_KEY` is set (Places API (New), Text Search):
  found or not, opening hours, phone, photos, number of reviews, rating. The lookup uses
  the business name and the optional city, and costs money on that Google account.

A task whose key starts with `check-` is advice that could not be verified. It is shown
but never costs points.

## How the score is reached

The report shows this under "איך חושב הציון", so the number can always be checked.

- The site part starts at 100. Each measured problem costs points by its severity
  (at most 15, 9, 5, 3 or 1), once per kind of problem.
- A problem found on some of the pages costs less than the same problem everywhere: 40% of
  the points when it is on a single page, rising to all of them when half the pages have
  it. Problems about the whole site (no sitemap, AI crawlers blocked) always cost it all.
  A problem on the home page also costs it all. The report shows "3 מתוך 30 דפים" next
  to each discounted line.
- A task whose key starts with `check-` costs nothing. These are things that are usually
  deliberate (inner pages hidden from Google, pages closed to crawlers) or that could not
  be verified.
- A home page Google cannot show (hidden, or closed to Googlebot) caps the site part at 49.
- A site that could not be read, or a scan that was cut off, gets no score at all.
- With an AI check, the final score is the average of the site part and the share of
  answers that mention the business.

## What is checked carefully so the numbers are about the right business

- **Names in AI answers.** The business and its competitors are looked for together and
  the longest name wins, so a competitor called "Roma" is not counted in an answer that
  only names "Pizza Roma" (and the other way round).
- **The Google profile.** Google is asked for five candidates, not one. A profile that
  links to the address that was checked is taken as certain; otherwise the name must
  match and the city decides between namesakes. When nothing clearly matches, the report
  shows what Google found under "לוודא שזה העסק שלך בגוגל" and scores none of it.
  A name that only contains the business's name ("Roma" in "Roma Hair Salon") is accepted
  only in the city the owner gave. On a platform the whole address must match, including
  what follows the question mark.

## Nothing is called missing unless it is

Every claim was compared with what a real browser shows on twenty live sites, and each
kind of false claim found is now a test in `tests/test_truth.py`.

- **Checked in a browser first.** Before a page is said to lack a title, a description, a
  main heading or business details, it is opened in a real browser, because many sites add
  these with scripts and Google runs scripts. When the browser is turned away (an "access
  denied" screen) or its time for the scan is used up, the page is left out, not guessed at.
- **Pictures.** Only a picture a visitor sees counts: not a tracking dot, a `<noscript>`
  fallback, a hidden or decorative image, an interface icon, a loading ring, or a template
  picture whose description a script fills in.
- **Business details** are recognised as JSON-LD, microdata and RDFa.
- **Headings.** Only a page with no main heading is reported. Several are fine.
- **One page under two addresses** is counted once.
- **A slow page** is one that was slow twice.
- **"Hidden from Google" on the home page** is said only when every answer carries the mark.
- **Not found is not missing.** A list of pages can be given to Google directly, and a
  platform page shows visitors details it does not hand to a reader like ours. These are
  listed as things to check (`check-...`) and cost no points. Nothing at all is said about
  the content of a Google Maps page.

## One field

The form asks for one thing: the address of the site or of the business's page. The
business's name is read from the page: the name the site gives itself (`og:site_name`),
then the business details it publishes for search engines (JSON-LD), then the part of the
page title that is a name, and last the address. A page on a platform is named by its own
title without the platform's name, or by its address when it cannot be read. The report
says when the name was found this way. Name, city, competitors and questions for AI are
optional and folded under "עוד אפשרויות".

A site whose secure address (https) is broken but that opens without the lock is still
checked, at the plain address, and the report's first tasks include fixing the certificate.

## Checking again, deleting, and the contact address

- **Check again.** Every report has a "בדוק שוב" button that scans the same business with
  what was entered the first time. The new report says how the score moved since the last
  check and lists what was fixed and what is new. The score is saved with each check;
  two checks are compared only when the same version of the formula scored both and both
  measured the same things (site, Google profile, AI answers). Otherwise the score would
  move for a reason that is not the owner's doing.
- **Who can open a report.** A report opens only with one of its two secret links: the
  personal link the check ends on (`/runs/12?key=...`), and a read-only link the report
  shows for passing on (`/runs/12?v=...`). The bare number (`/runs/12`) opens nothing, and
  nobody's checks are listed. The form shows the checks made from this browser, from a list
  the browser keeps on the device. `--allow-private` is development mode: reports open by
  number and all checks are listed.
- **Delete.** Only the personal link shows "מחיקת הבדיקה", which removes the report and
  everything collected for it. There are no accounts and no
  cookies; the key is the whole permission, and it is never listed or sent to other sites.
- **Limits and protection.** One visitor may start 10 checks in 10 minutes, three checks
  run at once, a second press on a button does nothing, pages cannot be framed and load
  nothing from other sites, reports are marked noindex and no-store, and the secrets in
  links are kept out of the server log. Only the site's own scripts run: each answer
  carries a one-time mark and a script without it is refused by the browser, so text from
  a scanned site or an AI answer can never run. A form larger than 64 KB is refused. A
  page that arrives drop by drop is cut off after 30 seconds, and a site's robots.txt is
  matched in a way that cannot be made to run for hours. Behind a proxy, pass the visitor's real address
  to the app (ProxyFix), or the limit counts everyone as one visitor.
- **Deleting for someone who lost their link:** `python -m bvm delete 12`.
- **Checks made before personal links existed:** `python -m bvm link 12` prints a personal
  link for check 12, creating one if it has none.
- **After an upgrade that changed the formula:** `python -m bvm rescore` scores the older
  checks again from the pages they already read (nothing is fetched), so new checks can
  be compared with them.
- **Contact address.** Set `HILITE_CONTACT` (or `python -m bvm web --contact you@example.com`)
  and the privacy page shows it for questions and deletion requests. With nothing set, the
  page shows no address.

- **The site's own address.** Set `HILITE_URL` (for example `https://hilite.example`) and
  the home page's canonical link, `/sitemap.xml`, `/robots.txt` and `/llms.txt` use it.
  With nothing set they use the address the visitor asked for.

## The site passes its own check

The home page carries a description, a canonical link, one main heading, and its details
for search engines (JSON-LD: the site, the app, and the questions and answers shown on the
page). `/sitemap.xml` lists the two public pages, `/robots.txt` names it, and `/llms.txt`
says in plain text what the site is. A test reads the home page with Hilite's own reader
and fails if it would report a gap.

## AI crawlers: two kinds

A site can close itself to the crawlers that fetch pages for an AI answer (OAI-SearchBot,
ChatGPT-User, PerplexityBot, Perplexity-User, Claude-SearchBot, Claude-User) or to the ones
that collect text for training (GPTBot, ClaudeBot, Google-Extended). Only the first kind
keeps a business out of the answers, so only it costs points ("להסיר חסימת AI"). A site
closed to training alone gets "לבדוק את חסימת אימון ה-AI", which costs nothing. This
changed the formula (version 4): run `python -m bvm rescore` once so older checks can be
compared with new ones.

robots.txt is read the way search engines read it (the most specific rule wins), not in
file order, so "what Google may read" matches what Google does.

## Sites the scan cannot read

A site that refuses automated visitors (an error page or an "are you a robot" check) is
not scanned. The scanner always identifies itself by name and does not disguise itself
as a person's browser. The report says the site blocked the check and that the site's
builder can allow the scanner. The AI check does not need the site and still runs.

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
