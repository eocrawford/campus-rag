# Data sources and usage policy

campus-rag ingests only public documents that are intended for general readers.

## Rules

- Ingest only sources listed in the allowlist below.
- Honor each site's `robots.txt` and terms of use, identify the crawler with a descriptive
  user agent, and rate-limit requests.
- Never ingest pages that require sign-in, or any student, patient, or personnel records.
  The project must not process data covered by FERPA or HIPAA.
- Keep the source URL and retrieval date with every chunk so answers can be verified.
- Do not commit downloaded content. It lives under the ignored `data/` directory and can be
  rebuilt from the allowlist.

## Allowlist

| Source | Scope | `robots.txt` (checked 2026-11-03) | Status |
|--------|-------|-----------------------------------|--------|
| [UW Policy Directory](https://policy.uw.edu) | Public policy pages listed in `https://policy.uw.edu/wp-sitemap.xml` | Allows all content; disallows `/wp-admin/` | Approved |
| [UW-IT Connect](https://it.uw.edu) | Public help articles listed in `https://it.uw.edu/sitemap.xml` | Allows content; disallows `/wp-admin/`, `/wp-includes/`, `/tag/*`, `/*?s=*` | Approved; deferred because pages render in the browser (see [decision 9](plans/mvp-design.md#9-m3-corpus-pipeline)) |

Add a source only after confirming its `robots.txt` and terms of use permit automated retrieval.

## UW terms of use constraints

The [UW website terms](https://www.washington.edu/online/terms/) (checked 2026-11-03) prohibit
reproducing, distributing, or displaying site content without permission, except as allowed by law
such as fair use, and prohibit placing an unreasonable load on UW infrastructure. Therefore:

- **Crawl politely.** Fetch from sitemaps, at most one request per second, with a descriptive user
  agent that links to this repository. Re-fetch only changed pages.
- **Keep content local.** Downloaded pages, chunks, and embeddings stay in the ignored `data/`
  directory and the local database. Never commit or publish them.
- **Write the golden set in original words.** Store each question, its source URL and section, and a
  reference answer written in our own words. Do not copy passages into the repository.
- **Keep answers short and attributed.** Answers summarize and link to the source page rather than
  reproducing long passages.
- **Do not host a public service over UW content** without UW's permission. Demos run locally or use
  recorded examples.

This is a good-faith reading of the terms, not legal advice.
