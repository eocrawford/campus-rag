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

| Source | Scope | Status |
|--------|-------|--------|
| _To be selected_ | Public university policy and IT help documentation | Pending review of `robots.txt` and terms of use |

Add a source only after confirming its `robots.txt` and terms of use permit automated retrieval.
