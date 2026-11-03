# Agent Rules of Engagement (RoE)

## Always:
* Communicate in simple terms. Use short, direct sentences, consistent terms, and explicit statements of what happened and why.
* Follow [prose authoring guidelines](prose-authoring-guidelines.md) when writing documentation, issues, or pull request descriptions.
* Practice true partnership. For non-trivial decisions, identify assumptions, risks, tradeoffs, and at least one alternative. Separate facts from interpretation, then recommend or act.
* Commit and push each beneficial advancement on a working branch. Committed work can be reverted; uncommitted work is easy to lose.
* Back retrieval, chunking, embedding, and prompt changes with before/after evaluation results.
* Report validation honestly: state exactly what ran, what passed, and what did not run.
* Keep `docs/plans/` current when a milestone's status changes.
* Record every design decision in `docs/plans/` before implementing it: the options considered, their tradeoffs, the recommendation, and a status (`Open`, `Proposed`, or `Decided`). When a design discussion happens in chat, save it there in the same session. Update the status as soon as the maintainer decides.

## Never:
* Never commit secrets, `.env` files, downloaded corpus content, or database dumps.
* Never ingest a source that is not on the [data-source allowlist](../data-sources.md).
* Never claim a metric or result that was not produced by a recorded run.
* Never add a new command-line entry point without the maintainer's explicit consent; extend an existing one instead.
* Never treat an `Open` or `Proposed` design decision as `Decided`, or build on it as settled, without the maintainer's explicit confirmation.
