# Glossary

Terms used in AI agent and retrieval-augmented generation (RAG) engineering. Each entry states
the problem the term solves, how it solves it, and where it appears in campus-rag.

**Status values**

- **Planned (M*n*)**: designed; delivered by milestone *n* of the [MVP plan](plans/mvp.md).
- **Implemented**: built; the link points to code and, where relevant, measured results.
- **Background**: defined for context; not part of this project.

When a change implements a term, update its row: link to the code or results and set the status
to **Implemented**.

## Retrieval and grounding

| Term | Problem it solves | How it solves it | In campus-rag | Status |
|------|-------------------|------------------|---------------|--------|
| **RAG** (retrieval-augmented generation) | An LLM knows only its training data, so it cannot answer about private, recent, or niche documents, and it guesses when it doesn't know. | Retrieves relevant passages at question time and places them in the prompt, so the model answers from current evidence without retraining. | The whole service; see [architecture](architecture.md). | Planned (M6-M7) |
| **Grounding** (contextual grounding) | Readers cannot tell whether a model's answer is true or invented. | Instructs the model to answer only from supplied passages and to cite them, so every claim can be checked against a source. | Cited answers; unsupported questions are declined. [Generation](architecture.md#generation) | Planned (M7) |
| **Research corpus grounding** | General-purpose answers ignore the specific, authoritative documents an organization relies on. | Restricts retrieval to a curated collection, so answers reflect that collection rather than the open web. | Answers come only from the allowlisted UW corpus. [Data sources](data-sources.md) | Planned (M3, M7) |
| **Corpus** | A system needs a defined body of knowledge to search. | Collects the documents in scope into one indexed collection. | 247 UW Policy Directory documents selected by [`config/sources.toml`](../config/sources.toml) and loaded by [corpus ingestion](../runbooks/ingest-corpus.md). [Data sources](data-sources.md) | Implemented |
| **Chunking** | Whole documents are too long to fit many in a prompt, and one long vector blurs many topics together. | Splits documents into focused passages, often with overlap so ideas spanning a boundary survive, and retrieves passages instead of documents. | Heading-aware chunks with a heading-path prefix, overlap on split sections, and short-section merging: [`chunk.py`](../src/campus_rag/ingest/chunk.py). [Ingestion](architecture.md#ingestion) | Implemented |
| **Embedding** | Computers cannot compare meaning directly, and keyword matching misses paraphrases ("forgot my login" versus "reset password"). | A model converts text into a vector of numbers where similar meanings land close together, so meaning becomes measurable distance. | Every chunk and question is embedded. [Index](architecture.md#index) | Planned (M4) |
| **Embedding strategy** | A poor model choice or poorly framed text produces vectors that retrieve the wrong passages. | Deliberately chooses the model, the exact text embedded (for example, chunk plus its heading), and dimensions, then validates the choice with retrieval metrics. | Configurable model; chunks embedded with title and section; models compared on recall@5. | Planned (M4, M6) |
| **Vector search** (semantic search) | Finding passages that mean the same thing as a question even when they share no words. | Embeds the question and returns the stored vectors closest to it. | pgvector cosine-distance search. [Retrieval](architecture.md#retrieval) | Planned (M6) |
| **Cosine similarity** | Vector length varies with text length, which distorts raw distance comparisons. | Compares only the angle between vectors, so direction (meaning) matters and length does not. | Distance metric for vector search. | Planned (M6) |
| **ANN** (approximate nearest neighbor) | Comparing a query against every stored vector is too slow at scale. | Builds an index that finds near-best matches by checking a small fraction of vectors, trading a little accuracy for large speed gains. | HNSW index on the embedding column. | Planned (M4) |
| **HNSW** (Hierarchical Navigable Small World) | Needs an ANN index that is both fast and accurate. | Builds layered graphs of neighboring vectors; search starts in a sparse top layer and descends to dense layers, homing in on the closest matches. | pgvector HNSW index. [Index](architecture.md#index) | Planned (M4) |
| **Vector search optimization** | Default settings can be slow or miss relevant results. | Tunes index parameters, candidate counts, and filters while measuring recall and latency. | HNSW parameters and candidate counts tuned against recall@5 and latency. | Planned (M6) |
| **Vector database** | Applications need to store vectors with their text and metadata and query them quickly. | Combines vector storage, ANN indexes, and metadata filtering behind one query interface. | PostgreSQL with pgvector (running since M2; chunk tables and indexes arrive in M4). [Technology choices](architecture.md#technology-choices) | Planned (M4) |
| **pgvector** | Running a separate vector database adds another system to operate and keep in sync. | Adds vector columns and ANN indexes to PostgreSQL, so vectors, text, metadata, and SQL filters live in one database. | The project's vector store: [`compose.yaml`](../compose.yaml) runs PostgreSQL with pgvector; [`001_enable_pgvector.sql`](../src/campus_rag/index/migrations/001_enable_pgvector.sql) enables it. | Implemented |
| **Pinecone** | Operating vector infrastructure at large scale takes effort. | Provides a fully managed, cloud-hosted vector database. | Alternative to pgvector. | Background |
| **Weaviate** | Teams want vector and keyword search in one self-hostable system. | Provides an open-source vector database with built-in hybrid search and modules. | Alternative to pgvector. | Background |
| **ChromaDB** | Prototypes need vector search without standing up a server. | Provides a lightweight vector database that can run inside a Python process. | Alternative to pgvector. | Background |
| **Keyword search** (full-text search, FTS) | Semantic search can miss exact identifiers such as policy numbers, system names, and acronyms. | Indexes the words in each document and matches query terms exactly, after normalizing word forms. | PostgreSQL full-text search with a GIN index. [Index](architecture.md#index) | Planned (M4, M6) |
| **TF-IDF** (term frequency-inverse document frequency) | Plain keyword matching overweights common words like "the" or "policy". | Scores a word higher when it is frequent in a document but rare across the corpus. | Conceptual basis for keyword ranking. | Background |
| **BM25** (Best Matching 25) | TF-IDF overrewards repeated words and long documents. | Caps the benefit of repeated terms and normalizes for document length. | PostgreSQL's built-in ranking is used instead. | Background |
| **Hybrid retrieval** | Keyword search misses paraphrases; vector search misses exact terms. | Runs both searches and merges the results, so each covers the other's blind spots. | Keyword + vector search fused with RRF. [Retrieval](architecture.md#retrieval) | Planned (M6) |
| **RRF** (reciprocal rank fusion) | Keyword and vector scores use different scales, so they cannot be added directly. | Ignores raw scores and sums 1 / (k + rank) across result lists, rewarding passages ranked well by either method. | Fusion step in hybrid retrieval. | Planned (M6) |
| **Reranking** (cross-encoder) | Fast first-stage retrieval ranks candidates imprecisely. | Rescores a short candidate list with a slower model that reads the question and passage together. | Optional stage, kept only if evaluation shows a gain. | Planned (M6) |
| **Metadata filtering** | Relevant-sounding passages may come from the wrong source, section, or date. | Restricts search by structured attributes before or during ranking. | Filters by source and section. | Planned (M6) |
| **Query rewriting** | Follow-up questions such as "what about for grad students?" lack the context needed for search. | Uses the LLM to rewrite the question into a standalone query using the conversation history. | First retrieval step for follow-up questions. | Planned (M7) |
| **Token** | Models and costs are limited by text length, but characters and words are inconsistent units. | Splits text into the subword units a model actually processes, giving a consistent measure. | Chunk sizes are measured in `cl100k_base` tokens ([`chunk.py`](../src/campus_rag/ingest/chunk.py)); context budgets follow in M7. | Implemented (chunking); Planned (M7) |
| **Context window** | A model can consider only a limited amount of text per request. | Defines that limit in tokens, which every prompt must fit within. | Prompt assembly fits passages within a token budget. | Planned (M7) |
| **Context window management** | Overfilled prompts cost more, run slower, and bury relevant facts where models attend to them poorly. | Selects, orders, and compresses content so the most relevant evidence fits and stands out. | Top passages only, ordered by relevance, within a budget. [Generation](architecture.md#generation) | Planned (M7) |
| **Long-context reasoning** | Some tasks need a model to consider very large inputs at once. | Uses models with very large context windows to reason over entire documents. | Selective retrieval is used instead. | Background |
| **Structured data augmentation** | Plain text passages lose attributes such as titles, sections, and URLs that answers and citations need. | Adds structured fields to the prompt alongside the text. | Passages carry title, section, and URL fields used for citations. | Planned (M7) |

## LLMs and agents

| Term | Problem it solves | How it solves it | In campus-rag | Status |
|------|-------------------|------------------|---------------|--------|
| **LLM** (large language model) | Software traditionally cannot understand or write flexible natural language. | Learns language patterns from large text corpora and generates text that follows instructions. | Writes grounded answers and rewrites queries. | Planned (M7) |
| **LLM API** | Hosting large models yourself requires specialized hardware. | Exposes hosted models through an HTTP interface. | OpenAI-compatible API; GitHub Models first. [Design](plans/mvp-design.md#1-model-provider) | Planned (M4, M7) |
| **Prompt engineering** | Model behavior varies with wording, so vague prompts give inconsistent results. | Designs instructions, structure, examples, and context deliberately, then measures their effect. | Grounded-answer prompt with citation and refusal rules, measured by evaluation. | Planned (M7) |
| **Hallucination** | Models state unsupported claims confidently, which misleads readers. | Is reduced by grounding, citations, refusal rules, and evaluation that measures unsupported claims. | Measured by faithfulness; reduced by citation checks. | Planned (M7-M8) |
| **Citation** | Readers cannot verify an answer without knowing its source. | Links each claim to the passage that supports it. | Every claim cites a passage ID; invalid citations are rejected. | Planned (M7) |
| **Chatbot** (conversational AI) | Users want to ask natural questions and follow-ups instead of searching. | Maintains conversation history and responds turn by turn. | Follow-up questions supported through query rewriting. | Planned (M7) |
| **AI agent** | Fixed pipelines cannot adapt when a task needs different steps or tools. | Lets an LLM choose actions and tools in a loop until it reaches a goal. | Agents call campus-rag search as a tool. | Planned (M9) |
| **Tool use** (function calling) | Models cannot act on systems or fetch live data by generating text alone. | The model emits a structured request; code executes it and returns the result. | The MCP `search` tool. | Planned (M9) |
| **Agentic RAG** | Fixed retrieve-then-answer pipelines retrieve even when unnecessary and cannot search again when the first results are weak. | Gives retrieval to an agent as a tool, so it decides when to search, what to search for, and whether to search again. | MCP `search` tool. [Interfaces](architecture.md#interfaces) | Planned (M9) |
| **MCP** (Model Context Protocol) | Every AI application needs custom integration code for every tool or data source. | Defines a standard protocol, so a tool built once works with any MCP-compatible AI application. | MCP server exposing `search`. | Planned (M9) |
| **Agent orchestration** | Complex tasks need multiple steps, tools, and agents coordinated reliably. | Controls sequencing, state, retries, and handoffs between steps and agents. | Single-agent tool use only. | Background |
| **Multi-agent system** | One general agent handles specialized subtasks poorly. | Splits work among specialized agents that collaborate. | Not used. | Background |
| **A2A** (Agent2Agent protocol) | Agents built on different platforms cannot discover or work with each other. | Defines an open protocol for agents to describe capabilities, delegate tasks, and exchange results. | Not used. | Background |
| **CrewAI** | Building multi-agent coordination from scratch is laborious. | Provides a Python framework for defining role-based agents and the tasks they collaborate on. | Not used. | Background |
| **nebulaONE** | Institutions want governed, secure access to generative AI without building a platform. | Provides a commercial enterprise generative-AI platform adopted by some universities. | Not used. | Background |
| **Fine-tuning** | A general model may not follow a specialized format, style, or domain reliably. | Further trains the model on task-specific examples. | Retrieval is used instead. | Background |
| **Alignment** | Models can pursue instructions in ways that conflict with intended goals or values. | Trains and constrains models to behave helpfully, honestly, and safely. | Not used. | Background |
| **Intelligent automation** (AI-powered workflow automation) | Staff spend time on repetitive process steps that require reading and judgment. | Combines AI with workflow tools so routine steps run automatically, with people handling exceptions. | Unanswerable questions become drafted help-desk tickets through the mock ticketing tool. | Planned (M12, stretch) |

## Evaluation, safety, and operations

| Term | Problem it solves | How it solves it | In campus-rag | Status |
|------|-------------------|------------------|---------------|--------|
| **Evaluation framework** | Without measurement, changes to prompts or retrieval can silently make results worse. | Runs a fixed test set and metrics before and after each change. | [Evaluation](evaluation.md) | Planned (M5-M8) |
| **Golden set** | Quality cannot be measured without known correct answers. | Curates questions with their correct sources and reference answers, including unanswerable questions. | 40 questions. [Golden set](evaluation.md#golden-set) | Planned (M5) |
| **recall@k** | Answer quality depends on whether retrieval found the right evidence. | Measures the share of questions whose correct passage appears in the top *k* results. | Primary retrieval metric. [Retrieval metrics](evaluation.md#retrieval-metrics) | Planned (M6) |
| **MRR** (mean reciprocal rank) | recall@k ignores whether the right passage ranked first or last. | Averages 1 / rank of the first correct result, rewarding higher placement. | Secondary retrieval metric. | Planned (M6) |
| **Faithfulness** | An answer can sound relevant while including unsupported claims. | Checks that every claim is supported by the cited passages. | Answer metric. [Answer metrics](evaluation.md#answer-metrics) | Planned (M8) |
| **Answer relevance** | An answer can be accurate but not address the question. | Checks that the answer responds to what was asked. | Answer metric. | Planned (M8) |
| **LLM-as-judge** | Human grading of free-form answers is slow and expensive. | Uses a separate model to grade outputs against a rubric; a different model avoids grading its own work. | Grades faithfulness and relevance. | Planned (M8) |
| **Ragas** | Teams need standard RAG metrics without writing their own. | Provides an open-source library of RAG evaluation metrics. | Metrics are implemented directly. | Background |
| **Guardrails** | Model outputs can be wrong, off-topic, or unsafe. | Places deterministic checks and rules around the model that block or correct bad outputs. | Citation validation, refusal on missing evidence, source allowlist. | Planned (M7) |
| **Safety mechanisms** | Harmful or incorrect outputs can reach users. | Applies controls before output is returned, such as validation, refusal, and filtering. | Deterministic checks run before an answer is returned. | Planned (M7) |
| **Responsible AI** | AI systems can mislead, expose private data, or treat people unfairly. | Applies principles such as transparency, accountability, privacy, and fairness throughout design and operation. | Cited answers, honest refusals, polite crawling, no protected data. [Data sources](data-sources.md) | Planned (M3, M7) |
| **Usage analytics** | Teams cannot improve a deployed system without knowing how it is used and where it fails. | Records volume, latency, and outcomes for each request. | Request logging and a usage report. [Decision 7](plans/mvp-design.md#7-capability-coverage-additions) | Planned (M11) |
| **User feedback loop** | Metrics alone miss what users find unhelpful. | Collects user ratings and comments and feeds them into evaluation and fixes. | Thumbs-up/down ratings on answers, summarized in the usage report. [Decision 7](plans/mvp-design.md#7-capability-coverage-additions) | Planned (M11) |

## Platform and integration

| Term | Problem it solves | How it solves it | In campus-rag | Status |
|------|-------------------|------------------|---------------|--------|
| **REST API** (Representational State Transfer) | Other programs need a standard way to use a service. | Exposes resources at URLs and uses standard HTTP methods and status codes. | `search` and `answer` endpoints. | Planned (M9) |
| **FastAPI** | Building typed, documented Python APIs takes boilerplate. | Generates validation and OpenAPI documentation from Python type hints. | The service's API layer. | Planned (M9) |
| **Docker** (containerization) | Software behaves differently across machines because of differing dependencies. | Packages software with its dependencies into containers that run the same everywhere. | PostgreSQL with pgvector runs in Docker: [`compose.yaml`](../compose.yaml), started by [setup](../runbooks/local-environment-setup.md). | Implemented |
| **Cloud platforms** (AWS, Azure, GCP) | Owning and running servers is costly and slow to scale. | Rents compute, storage, and managed services on demand (Amazon Web Services, Microsoft Azure, Google Cloud Platform). | Runs locally; see [UW terms constraints](data-sources.md#uw-terms-of-use-constraints). | Background |
| **Connector** | Each external system has its own API and data format. | Encapsulates one system's integration behind a consistent interface. | The polite sitemap and page fetcher ([`fetch.py`](../src/campus_rag/ingest/fetch.py)); the mock ticketing API client in M12. | Implemented (fetcher); Planned (M12) |
| **SIS** (student information system) | Universities need one authoritative record of students, enrollment, and grades. | Centralizes student academic records and related processes. | Not used. | Background |
| **LMS** (learning management system) | Courses need a shared place for content, assignments, and grades. | Hosts course materials and coursework, as in Canvas. | Not used. | Background |
| **HRIS** (human resources information system) | Employee data and HR processes are scattered and manual. | Centralizes employee records and HR workflows. | Not used. | Background |
| **ERP** (enterprise resource planning) | Finance, procurement, and operations data live in disconnected systems. | Integrates core business processes in one system. | Not used. | Background |
| **Ticketing system** (ITSM, IT service management) | Support requests get lost without tracking. | Records, routes, and tracks requests through resolution, as in ServiceNow. | A local mock ticketing API that an MCP tool files drafted tickets into. [Decision 7](plans/mvp-design.md#7-capability-coverage-additions) | Planned (M12, stretch) |
| **No-code / low-code** | Non-programmers depend on developers for simple tools. | Lets people build applications through visual configuration instead of code. | Not used. | Background |
| **Open source** | Closed code cannot be inspected, reused, or improved by others. | Publishes source under a license that permits use and modification. | This repository, under the MIT license. | Implemented |

## Privacy and compliance

| Term | Problem it solves | How it solves it | In campus-rag | Status |
|------|-------------------|------------------|---------------|--------|
| **PII** (personally identifiable information) | Data that identifies people can cause harm when exposed. | Classifies such data so it receives stronger protection. | Never ingested. [Data sources](data-sources.md#rules) | Implemented (policy) |
| **FERPA** (Family Educational Rights and Privacy Act) | Student education records could be disclosed without consent. | U.S. law limits disclosure of education records and gives students rights over them. | Student records are never ingested. [Data sources](data-sources.md#rules) | Implemented (policy) |
| **HIPAA** (Health Insurance Portability and Accountability Act) | Health information could be disclosed without consent. | U.S. law sets privacy and security requirements for protected health information. | Patient records are never ingested. [Data sources](data-sources.md#rules) | Implemented (policy) |
