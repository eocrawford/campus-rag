"""Rewrite a conversational follow-up into a standalone search query."""

import re
from collections.abc import Sequence

from campus_rag.generate.chat import ChatClient, ChatError
from campus_rag.generate.models import ChatTurn

MAX_EARLIER_QUESTIONS = 3
MAX_QUESTION_CHARS = 400
MAX_QUERY_CHARS = 300
MIN_LENGTH_ALLOWANCE = 200  # short follow-ups such as "What about X?" expand to a full question

# Only the user's earlier questions are sent. With the assistant's answers included,
# qwen2.5:3b-instruct often returned "Does that also apply ...?" unchanged. Without examples it
# also treated elliptical follow-ups ("What about graduate students?") as new topics, so the
# prompt includes a few worked examples. The examples are invented and deliberately unrelated
# to the golden set.
REWRITE_SYSTEM_PROMPT = """\
You rewrite the user's latest question so it can be searched on its own.
A follow-up is often short and leans on the earlier questions: it may use words like "that", \
"it", "this", or "they", or be an ellipsis such as "What about X?", "And for Y?", or "What if Z?".
Rewrite a follow-up into one complete question that names the earlier topic and keeps every \
detail the user just added. If the latest question already makes sense on its own, or starts a \
new topic, repeat it exactly. Output only the question."""

REWRITE_EXAMPLES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("How do I renew a library book online?",),
        "What about overdue fines?",
        "What are the fines for overdue library books?",
    ),
    (
        ("Can visitors park in campus lots overnight?",),
        "And for motorcycles?",
        "Can visitors park motorcycles in campus lots overnight?",
    ),
    (
        ("Can I get a refund on my parking permit if I stop using it?",),
        "What if I'm stopping because I'm moving out of state?",
        "Can I get a refund on my parking permit if I stop using it because I'm moving out of "
        "state?",
    ),
    (
        ("Who can approve a purchase over $10,000?",),
        "How do I reserve a study room in the library?",
        "How do I reserve a study room in the library?",
    ),
)

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
    messages = [{"role": "system", "content": REWRITE_SYSTEM_PROMPT}]
    for example_earlier, follow_up, standalone in REWRITE_EXAMPLES:
        messages.append({"role": "user", "content": _rewrite_request(example_earlier, follow_up)})
        messages.append({"role": "assistant", "content": standalone})
    messages.append({"role": "user", "content": _rewrite_request(earlier, question)})
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
    return "\n".join(lines)


def _clean(text: str) -> str:
    for line in text.strip().splitlines():
        line = _LABEL.sub("", line.strip()).strip().strip("\"'`*").strip()
        if line:
            return line
    return ""


def _length_limit(question: str) -> int:
    return min(MAX_QUERY_CHARS, max(3 * len(question.strip()), MIN_LENGTH_ALLOWANCE))
