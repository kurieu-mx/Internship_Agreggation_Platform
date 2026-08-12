# Internship Digest

**A scheduled agent that finds the internships posted in the last 24 hours, ranks them against your actual experience, and writes a tailored resume and cover letter for each one — as PDFs that keep your formatting exactly.**

[![tests](https://github.com/kurieu-mx/Internship_Agreggation_Platform/actions/workflows/tests.yml/badge.svg)](https://github.com/kurieu-mx/Internship_Agreggation_Platform/actions/workflows/tests.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Applying to internships is a search problem wearing a writing problem as a hat. The postings worth applying to are scattered across eight places, most of them are stale by the time you see them, and the ones that matter reward applying within a day. Then each one needs a resume that emphasises the right half of your experience and a cover letter that proves you read the posting.

This does all of it at 3pm, every day, for about **70 cents**.

```console
$ make digest
INFO collected 585 postings (greenhouse=25, lever=2, ashby=2, simplify=324, vansh=180, websearch=49, linkedin=3)
INFO dropped 6 posting(s) that are not internships
INFO eligibility: kept 427, dropped 40 as closed to sponsorship
INFO 26/427 postings inside the 24h window
INFO prefilter: 24 postings in target categories, taking top 15
INFO rerank: scored 15 postings, top score 86
INFO shortlist: 10 postings across 10 companies (cap 1/company)
INFO research: 4 verified fact(s) about Quantbot Technologies
INFO rendered Resume_Quantbot_Technologies_Machine_Learning_Research_Engineer.pdf (1 page)
INFO sent 10 posting(s) with 20 attachments
INFO this run cost $0.75
```

---

## The three problems worth solving

### 1. A generated resume must not invent experience

This is the one that matters. A model asked to "tailor a resume" will happily add a technology you have never used, and you will not notice until an interviewer asks about it.

So the model never writes a resume. It **selects from a pool**. `profile.yml` holds every bullet you have ever written, each with an id; tailoring returns ids plus lightly reworded text; and four checks run before any PDF is accepted:

| Guardrail | Catches |
|---|---|
| **Provenance** | A bullet whose id isn't in the pool |
| **Text fidelity** | A bullet citing a *real* id while describing unrelated work |
| **Immutable facts** | Name, contact, employers, dates, school altered or dropped |
| **Page count** | A tailored resume that spills to page two |

Any failure rejects the PDF and falls back to your untailored master. The email says so, so you never discover it later.

The second check exists because the first is not enough: provenance alone is satisfied by citing `merlin-collision` and then writing "negotiated multi-million dollar vendor contracts". Text fidelity compares the rendered wording to its source and rejects substitution while still allowing the rewording that is the entire point.

**Keyword optimisation runs inside those guardrails, not around them.** Most applications are read by software before a human sees them, and a resume that describes the same work in different words scores as though it described different work. So the tailoring pass is told which of the posting's terms to use — but the list it is given is *derived from the profile*, so a term can only be suggested if some bullet, tag, or skill entry already evidences it:

```console
$ make digest
INFO keywords for Jump Trading: 10 matched (Deep Learning, quantitative research,
     distributed, machine learning, transformers, PyTorch), 4 asked for but unsupported
INFO Jump Trading: resume covers 80% of the posting's keywords (absent: statistics)
```

Terms the posting wants and the profile cannot back — Rust, Kubernetes, JAX — are extracted too, and travel in a separate list the model is told never to claim. They are the honest gaps. Measured against a live posting, turning the brief on moved coverage from 70% to 80%, by changing *which* bullets were selected rather than by padding the wording of the ones already there.

Frequency counting was tried first and is useless: on a real IMC posting the top terms by count were "markets", "environment", "may", "base", "salary". Frequency finds the boilerplate, because boilerplate is what gets repeated.

### 2. Format preservation means regenerating, not editing

The pipeline never edits a PDF. It regenerates one from structured content through the same renderer every time, which is what makes every variant come out with identical layout. Editing PDF text in place cannot do this — a replacement string of a different width reflows the line, and from there the page.

If only a PDF of your resume exists, the layout is rebuilt in HTML/CSS from measurements taken off the original with PyMuPDF — margins, leading, the exact faux-small-caps trick, the metric-compatible font. `make verify-render` then renders the master's own content and diffs it against the original:

```console
$ make verify-render
  master:  1 page(s), 501 words
  rebuilt: 1 page(s), 503 words
  text similarity: 99.4%

  The rebuild reproduces the master.
```

That runs on every template change, so drift is caught when it appears rather than when an application goes out.

### 3. A cover letter is only worth sending if it could not have been sent to anyone else

Which means naming something specific and true about the employer — exactly what a model will invent when it has nothing to go on.

So it never asks a model what a company does. It fetches the company's own pages, extracts claims **with quoted evidence**, and validates every quote back against the source text. Claims that cannot be traced are dropped, not softened. If nothing survives, the letter is written about the role instead of with a fabricated hook.

The letterhead uses the company's real logo and a colour extracted from its own pixels — the most *saturated* colour, not the most common, because logos are mostly background.

**And it has to not read as machine-written.** The five tells enforced here were counted across 31 letters this pipeline actually produced, not taken from a list of things people say about AI prose — which matters, because the famous ones were already absent. Across 14,384 words there were zero instances of "excited", "thrilled", "passionate", "align", "resonate", "leverage", "robust", "seamless", "delve", and zero of "I am writing to express my interest". The prompt had banned those from the start. What was left was subtler:

| Tell | Measured | Why it reads as machine |
|---|---|---|
| em dash | 151 (4.9/letter) | punctuation as drama |
| colon-led reveal | 63 (2.0/letter) | the same move, different mark |
| rule-of-three list | 60 (1.9/letter) | a rhythm nobody speaks in |
| sentences over ~34 words | median 33 | no short sentences anywhere |
| "That is the same X as Y" | 13 of 31 letters | one formula, every letter |

The through-line is uniformity: a person writing five cover letters produces five rhythms, and this produced one rhythm thirty-one times. Banning the em dash alone would only push the same appositive habit onto colons, which is why both are counted.

`tailor/voice.py` states the rules and measures whether they were followed, so the prompt and the check cannot drift apart. A draft that breaks them is handed back with the specific violations named, and a revision is kept **only if it scores better** — removing the dashes while doubling the sentence length is not an improvement, and editing prose against a rule sometimes produces exactly that. The loop stops as soon as a round fails to improve, so a clean draft costs nothing and a stubborn one costs at most two extra calls:

```console
INFO IBM: revising letter voice - 0 dashes, 7 triples, 1 colon-reveals, 24w mean sentence
INFO IBM: voice now 0 dashes, 1 triples, 0 colon-reveals, 16w mean sentence
```

Reported, never enforced. A letter with two three-item lists is worth sending; a letter that was never written because it could not be made perfect is not.

---

## How it works

```mermaid
flowchart LR
    A[8 sources] --> B[de-duplicate<br/>by authority]
    B --> C[internship gate]
    C --> D[work authorisation]
    D --> E[24h window]
    E --> F[prefilter<br/>deterministic]
    F --> G[rerank<br/>one model call]
    G --> H[≤1 per company]
    H --> I[tailor + research]
    I --> J[render + validate]
    J --> K[Gmail]
```

| Module | Responsibility |
|---|---|
| `sources/` | Eight adapters behind one protocol — Greenhouse, Lever, Ashby, **Workday**, two community feeds, web search, LinkedIn |
| `eligibility.py` | Internship gate, and work-authorisation filtering |
| `freshness.py` | The 24-hour window, and what to do about sources that publish no date |
| `store.py` | SQLite: seen postings, sent digests, per-run cost |
| `tailor/score.py` | Deterministic prefilter, then one model call to rank |
| `tailor/keywords.py` | Posting vocabulary, intersected with what the profile can evidence |
| `tailor/render.py` | The renderer and its four guardrails |
| `tailor/cover.py` | Grounded research and the sectioned letter |
| `tailor/voice.py` | The five machine-writing tells, measured and revised out |
| `tailor/branding.py` | Logo and brand-colour extraction |
| `budget.py` | Hard daily spend ceiling |

---

## Design decisions

**Every source fails independently.** A source that times out, changes schema, or loses its credentials costs you that source's postings for one run — not the run. The community feeds are public and always work; the search and logged-in legs are neither, and they *will* break. Isolating them keeps the reliable legs running when the unreliable ones fall over.

**The ATS you cover decides which companies you can see.** Greenhouse, Lever and Ashby are what startups, quant firms and mid-size tech use. Large enterprises use Workday, and FAANG-scale companies run bespoke portals. Covering only the first three made the digest structurally blind to most of the Fortune 500 — not as a ranking artefact but as a collection one, which is worse, because a posting that was never collected cannot be re-ranked. An IBM Summer 2027 req went out unseen before the Workday adapter existed.

Workday's `/wday/cxs` endpoint is public JSON, and two of its quirks shape the adapter. Passing `searchText` switches the API from date order to relevance order, so it issues an *empty* search and walks strictly newest-first, stopping as soon as postings age past the window — one or two requests for a company with 2,000 open roles. And the list endpoint has no real dates, only rendered strings like "Posted 5 Days Ago", so every surviving posting gets one detail fetch for a date the freshness gate can trust.

**Big employers get a bounded lift, not a veto.** A recognised name adds points in the deterministic prefilter, enough to carry it into the rerank pool rather than lose to a keyword count. The model still ranks on fit. The reason is not prestige: an applicant who needs visa sponsorship is materially better served by employers who file H-1B petitions as routine, and a famous logo on a retail-management internship is still a retail-management internship.

**ATS boards outrank aggregators.** A posting reaches Greenhouse the moment a recruiter publishes it, and reaches an aggregator when a contributor opens a PR. For a digest whose premise is "posted in the last 24 hours", that gap is the product. When the same posting arrives from both, the board's copy wins the merge and the aggregator backfills any field it left empty.

**Freshness is asked differently of different sources.** Dated sources get an exact comparison. Undated ones (search results) qualify only on first sighting — otherwise a dateless source re-qualifies its entire catalogue every day and the digest cries wolf until it gets ignored.

**Nothing is recorded as sent until it is sent.** A crash, an expired token, or a failed render means tomorrow's run picks the same postings up again. A failed send writes a Gmail draft and still reports failure, so the work survives without the postings being marked delivered.

**One application per company per day.** A second application to the same employer adds little; a first application to a different one adds a lot. Without the cap, a company that posts five roles takes half the slots — observed live, where one company took four of eight. When too few companies post, the digest is **short rather than padded**.

**Silence is not a rejection.** No source reliably publishes a sponsorship field — measured, 100% report `Unknown` — so work-authorisation status is derived from posting text instead, and only some sources publish any. Postings that stay unknown are *kept*; treating silence as a no would discard most of the corpus. The log reports the size of that blind spot on every run.

**Spend is capped in the database, not in memory.** A normal run is ~$0.72. The ceiling exists for the abnormal ones — a retry loop, a config typo, a workflow firing repeatedly. Once reached, model calls are refused and the pipeline degrades exactly as it does when the model is unreachable, so the digest still goes out. A cap held in memory does not cap a crash-loop.

**Measured, not estimated.** `make costs` counts real tokens against your real profile via `count_tokens`, which is free. `make verify-render` diffs the rebuilt resume against the original. `make boards` checks every configured job board still resolves. Guessing at any of these was wrong by 20–100% when checked.

---

## Quickstart

```bash
git clone https://github.com/kurieu-mx/Internship_Agreggation_Platform.git
cd Internship_scrapper
make install
make preview          # today's postings — no credentials needed
```

Then, for the full digest:

```bash
cp .env.example .env  # add an Anthropic key and a Composio key
cp ~/your-resume.pdf profile/master.pdf
make doctor           # reports what is still missing, and the fix for each
make digest-dry       # builds everything, sends nothing
```

`make doctor` is the load-bearing one. Setup touches four systems that each fail quietly and identically — an unset key looks exactly like a key with no Gmail connected — so it reports the actual state of the machine and the exact command that fixes each gap.

```console
$ make doctor
  [ ok ] Anthropic API key  set (sk-ant-api0...), SDK installed
  [ ok ] master resume      master.pdf (WeasyPrint, rebuilt from measurements)
  [ ok ] render toolchain   weasyprint + Nimbus Roman (metric-identical to Times)
  [ ok ] render fidelity    rebuild matches the master at 1 page(s)
  [ ok ] spend cap          $0.00 of $2.00 used today
  [ ok ] Handshake          off by choice (not in SOURCES)

  Everything is configured.
```

### Commands

| | |
|---|---|
| `make preview` | Today's postings, no credentials required |
| `make doctor` | What is configured, what is missing, how to fix it |
| `make costs` | Measure a day's API cost against your real profile |
| `make verify-render` | Prove the renderer still reproduces your master resume |
| `make boards` | Check every configured job board still resolves |
| `make digest-dry` | Build the full digest, send nothing |
| `make digest` | Build and send |
| `python main.py --apply-url <url>` | Tailor and email one posting by link |
| `make test` | 615 tests |

`python main.py --cover-preview <company>` iterates on one cover letter without a full run; add `--no-research` to make it free.

---

## Running it on a schedule

`.github/workflows/daily-digest.yml` fires at 3pm local, every day.

GitHub's scheduler is UTC-only, so hitting 3pm year-round takes two cron entries and a guard — one for daylight time, one for standard, with a step that exits the run that is an hour off. A single UTC cron drifts by an hour twice a year.

Requires six repository secrets: `ANTHROPIC_API_KEY`, `COMPOSIO_API_KEY`, `COMPOSIO_USER_ID`, `DIGEST_TO`, and `PROFILE_YML` / `MASTER_PDF_B64` — your resume is git-ignored, so it reaches CI as a secret rather than living in a public repo.

---

## Cost

Measured with `count_tokens` against a real profile and a real posting, at ten companies a day:

| Setup | Per day | Per month |
|---|---|---|
| **Opus for writing, Haiku for research** | **$0.72** | **~$22** |
| All Opus | $0.92 | ~$28 |
| All Sonnet | $0.55 | ~$17 |

Split by what each call does rather than to shave the bill: scoring decides which companies you apply to, and the resume and letter are documents a human judges you on — neither is the place to save two cents. Company research is extraction whose output is validated against its source, so a cheaper model cannot smuggle anything through.

Prompt caching carries about three-quarters of input volume at a tenth of the price; without it the same run is roughly $1.10.

Quiet days cost nothing — no fresh postings means no calls at all.

---

## Limitations

- **Work-authorisation filtering sees only part of the corpus.** Roughly a tenth of postings carry text to inspect. It catches postings that *say* they are closed to you, not all of the ones that are.
- **Web search and LinkedIn are the weakest legs.** Results are undated, unstructured, and rank below every other source. LinkedIn is reached through public web search — the site is never scraped — so coverage is whatever they choose to make publicly indexable.
- **Handshake needs a browser cookie that expires.** Off by default. It is the only source that sees school-restricted postings, and the least durable thing here.
- **One posting per company per day** is a deliberate trade, not a limitation, but it does mean a company posting several genuinely different roles is under-represented. The overflow appears in the digest as links.
- **Company research depends on a fetchable marketing site.** Small firms with thin web presence yield nothing, and their letters are written about the role instead.
- **Six large employers are unreachable by any source.** IBM, Amazon, Google, Apple, Meta and Microsoft each run their own careers portal instead of an ATS with a public API, and some are bot-protected — a plain request to IBM returns HTTP 202 with an empty body. Six bespoke adapters was judged not worth the maintenance, so those arrive by hand:

```console
$ python main.py --apply-url "https://careers.ibm.com/en_US/careers/JobDetail?jobId=128497"
INFO direct fetch returned nothing usable - trying the rendered fetcher
INFO keywords for IBM: 15 matched, 12 asked for but unsupported
INFO IBM: resume covers 100% of the posting's keywords
INFO research: 4 verified fact(s) about IBM

  IBM — Software Developer Intern 2027
  Lowell, MA, Durham, NC, Bellevue, WA, San Jose, CA, Austin, TX · Summer 2027
  posted 2026-08-11
  cost   : $0.17
```

  Same code path as the digest — same tailoring, same four render guardrails, same grounded letter, same delivery — so a hand-added posting is not a lesser application than a collected one. It runs the same gates too, reporting a graduate requirement or a sponsorship bar rather than quietly tailoring for a role you cannot take.

## License

MIT — see [LICENSE](LICENSE).

Built by [Eugenio Kuri Muzquiz](https://github.com/kurieu-mx), studying Data Science at the University of Michigan.
