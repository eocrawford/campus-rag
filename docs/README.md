# campus-rag documentation

## Start here

- [Walkthrough](walkthrough.md) - run the whole system, then read the code in the order a question flows through it.
- [Case study](case-study.md) - the problem, key decisions with their measured results, limits, and next steps.
- [Architecture](architecture.md) - components, diagrams, request flow, and design tradeoffs.
- [Evaluation](evaluation.md) - golden question set, retrieval and answer metrics, and how to compare changes.
- [Data sources and usage policy](data-sources.md) - which sources may be ingested and how.
- [Glossary](glossary.md) - each term's problem, solution, and where it appears in campus-rag.

## Results

- [Retrieval](results/retrieval.md) - recall@k, MRR, latency, and HNSW recall for keyword, vector, and hybrid search.
- [Answers](results/answers.md) - refusals, citations, faithfulness, relevance, follow-up rewriting, and failures.
- [Embedding strategies](results/embedding-strategies.md) - heading, task prefixes, a second model, and reduced dimensions.

## Diagrams

Each diagram's Mermaid source is in [`images/`](images/), next to its rendered PNG and SVG, and is
embedded in [architecture](architecture.md#components).

- System overview: [PNG](images/system-overview.png), [SVG](images/system-overview.svg), [source](images/system-overview.mmd).
- Answering a question: [PNG](images/answering-a-question.png), [SVG](images/answering-a-question.svg), [source](images/answering-a-question.mmd).

## Plans

- [MVP plan](plans/mvp.md) - milestones and their status.
- [MVP design decisions](plans/mvp-design.md) - options, tradeoffs, and the decision for each choice.

## Contributing

- [Contributor and agent guide](../AGENTS.md)
- [Agent guidance](agent-guidance/) - rules of engagement, foundations, and writing guidelines.
- [Operational runbooks](../runbooks/AGENTS.md)
