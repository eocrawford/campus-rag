"""Rewrite a conversational follow-up into a standalone search query."""

import re
from collections.abc import Sequence

from campus_rag.generate.chat import ChatClient, ChatError
from campus_rag.generate.models import ChatTurn

MAX_EARLIER_QUESTIONS = 3
MAX_QUESTION_CHARS = 400
MAX_QUERY_CHARS = 300
MIN_LENGTH_ALLOWANCE = 100  # short follow-ups such as "and for staff?" need room to expand

# Only the user's earlier questions are sent. With the assistant's answers included,
# qwen2.5:3b-instruct often returned "Does that also apply ...?" unchanged.
REWRITE_SYSTEM_PROMPT = """\
You turn a follow-up question into a standalone search question.
If the follow-up refers to the earlier questions with words like "that", "it", "this", "they", or \
"and", rewrite it so it names the topic explicitly. If the follow-up asks about a new topic, \
repeat it exactly. Output only the standalone question."""

_LABEL = re.compile(r"^(?:standalone|rewritten)?\s*(?:question|query)\s*:\s*", re.IGNORECASE)


def rewrite_query(chat: ChatClient, model: str, history: Sequence[ChatTurn], question: str) -> str:
    """Return a standalone search query for `question` given earlier `history`.

    Falls back to `question` when there are no earlier user questions, the model fails, or its
    reply is empty or overlong.
    """
    earlier = [
        turn.content.strip() for turn in history if turn.role == "user" and turn.content.strip()
    ]
    if not earlier:
        return question
    messages = [
        {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
        {"role": "user", "content": _rewrite_request(earlier, question)},
    ]
    try:
        result = chat.complete(messages, model=model, temperature=0.0, max_tokens=100)
    except ChatError:
        return question
    rewritten = _clean(result.text)
    if not rewritten or len(rewritten) > _length_limit(question):
        return question
    return rewritten


def _rewrite_request(earlier: Sequence[str], question: str) -> str:
    lines = []
    for text in earlier[-MAX_EARLIER_QUESTIONS:]:
        text = " ".join(text.split())
        if len(text) > MAX_QUESTION_CHARS:
            text = text[:MAX_QUESTION_CHARS].rstrip() + " ..."
        lines.append(f"Earlier question: {text}")
    lines.append(f"Follow-up question: {question.strip()}")
    return "\n".join(lines) + "\n\nStandalone question:"


def _clean(text: str) -> str:
    for line in text.strip().splitlines():
        line = _LABEL.sub("", line.strip()).strip().strip("\"'`*").strip()
        if line:
            return line
    return ""


def _length_limit(question: str) -> int:
    return min(MAX_QUERY_CHARS, max(3 * len(question.strip()), MIN_LENGTH_ALLOWANCE))
