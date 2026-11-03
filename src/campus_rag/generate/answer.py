"""Answer a question from retrieved passages, with validated citations and enforced refusals."""

import time
from collections.abc import Callable, Sequence

from campus_rag.config import ChatSettings
from campus_rag.generate.chat import ChatClient
from campus_rag.generate.citations import check_answer
from campus_rag.generate.models import REFUSAL_TEXT, Answer, ChatTurn
from campus_rag.generate.prompt import MAX_PASSAGES, PASSAGE_TOKEN_BUDGET, build_prompt
from campus_rag.generate.rewrite import rewrite_query
from campus_rag.retrieve.models import Searcher, SearchFilters, SearchMode

ANSWER_MAX_TOKENS = 400  # about 300 words; the prompt asks for under 150


class AnswerService:
    """Rewrite (follow-ups only), retrieve, prompt, generate, and check one answer."""

    def __init__(
        self,
        searcher: Searcher,
        chat: ChatClient,
        settings: ChatSettings,
        *,
        k: int = 8,
        max_passages: int = MAX_PASSAGES,
        passage_token_budget: int = PASSAGE_TOKEN_BUDGET,
        search_mode: SearchMode = "hybrid",
        filters: SearchFilters | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if k < 1:
            raise ValueError("k must be >= 1")
        self._searcher = searcher
        self._chat = chat
        self._settings = settings
        self._k = k
        self._max_passages = max_passages
        self._passage_token_budget = passage_token_budget
        self._search_mode: SearchMode = search_mode
        self._filters = filters
        self._clock = clock

    def answer(self, question: str, history: Sequence[ChatTurn] = ()) -> Answer:
        """Answer `question`; earlier `history` turns make it a follow-up that gets rewritten.

        Raises `ChatError`, `EmbeddingError`, or a database error when a service fails.
        """
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")
        timings: dict[str, float] = {}

        search_query = question
        if history:
            started = self._clock()
            search_query = rewrite_query(
                self._chat, self._settings.rewrite_model, history, question
            )
            timings["rewrite"] = self._elapsed_ms(started)

        started = self._clock()
        chunks = self._searcher.search(
            search_query, mode=self._search_mode, k=self._k, filters=self._filters
        )
        timings["retrieve"] = self._elapsed_ms(started)

        model = self._settings.answer_model
        if not chunks:
            timings["generate"] = 0.0
            return Answer(
                question=question,
                search_query=search_query,
                text=REFUSAL_TEXT,
                refused=True,
                citations=(),
                passages=(),
                model=model,
                timings_ms=timings,
            )

        # The answer model sees no history, so it gets the standalone query.
        prompt = build_prompt(
            search_query,
            chunks,
            max_passages=self._max_passages,
            token_budget=self._passage_token_budget,
        )
        started = self._clock()
        result = self._chat.complete(
            prompt.messages, model=model, temperature=0.0, max_tokens=ANSWER_MAX_TOKENS
        )
        timings["generate"] = self._elapsed_ms(started)

        checked = check_answer(result.text, prompt.passages)
        return Answer(
            question=question,
            search_query=search_query,
            text=checked.text,
            refused=checked.refused,
            citations=checked.citations,
            passages=prompt.passages,
            model=result.model or model,
            invalid_citations=checked.invalid_citations,
            timings_ms=timings,
        )

    def _elapsed_ms(self, started: float) -> float:
        return round((self._clock() - started) * 1000, 1)
