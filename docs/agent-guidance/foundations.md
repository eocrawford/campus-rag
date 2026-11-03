# Foundations

## Deterministic code versus LLM judgment

Deterministic code owns facts and control. The LLM owns language.

| Deterministic code | LLM |
|--------------------|-----|
| Fetching, extraction, chunking, hashing | Rewriting a conversational question into a standalone query |
| Embedding calls, index writes, search, fusion, filtering | Composing an answer from supplied passages |
| Prompt assembly and context budgeting | Judging faithfulness and relevance during evaluation |
| Citation validation and refusal enforcement | |

An LLM output never becomes a fact on its own. Code validates it before it reaches a user,
for example by rejecting citations to passages that were not supplied.

## Design

* Prefer the simplest complete design. Each module has one clear responsibility.
* Keep provider-specific code (embedding API, chat API, database) behind small interfaces so
  providers can be swapped through configuration and faked in tests.
* Make ingestion idempotent: re-running it on unchanged sources changes nothing.

## Python conventions

* Target Python 3.12 with type hints on public functions.
* Lint with `ruff`; test with `pytest`.
* Read configuration and secrets from environment variables, never from source files.
* Unit tests use fakes for external services and never call paid APIs. Evaluation runs are separate
  from unit tests and are invoked explicitly.
