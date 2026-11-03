# Answer results

Over 47 questions: refusal accuracy 0.98, faithfulness 0.94, relevance 4.4 of 5, citation validity 1.00.

Generated on 2026-11-03 at commit `5a5b401`. The corpus has 247 documents and 1,978 chunks, embedded with `nomic-embed-text`. The golden set has 47 questions and this run answered every question. `qwen2.5:7b-instruct` writes each answer from hybrid search (top 8 chunks, up to six passages in the prompt), `qwen2.5:3b-instruct` rewrites follow-up questions, and `llama3.1:8b` judges faithfulness and relevance. See [evaluation](../evaluation.md#answer-metrics) for the metric definitions and the [answer evaluation runbook](../../runbooks/evaluate-answers.md) to repeat the run.

## Summary

| Metric | Value | Over |
|---|---|---|
| Refusal accuracy | 0.98 (46/47) | All questions |
| False-refusal rate | 0.03 (1/40) | Answerable, refused |
| False-answer rate | 0.00 (0/7) | Unanswerable, answered |
| Citation validity | 1.00 (61/61) | Cited numbers supplied |
| Sentence citation coverage | 0.61 (48/79) | Answer sentences with a citation |
| Fully cited answers | 0.49 (19/39) | Answers citing every sentence |
| Retrieval hit | 0.97 (39/40) | Answerable, target among the passages |
| Faithfulness (mean) | 0.94 | 38 judged answers |
| Fully supported answers | 0.87 (33/38) | Every claim supported |
| Answer relevance (mean of 1-5) | 4.44 | 39 judged |
| Relevance 4 or 5 | 0.85 (33/39) | Judged answers |
| Supported claims naming no shown passage | 0.01 (1/112) | Claims judged; check these verdicts by hand |
| Judge errors | 1 | Questions with an unusable judge reply |

## By question type

Paraphrase, exact-term, and unanswerable rows hold standalone questions; follow-ups are grouped on their own. Refusal accuracy counts an answerable question as correct when it is answered and an unanswerable one when it is refused. Faithfulness and relevance are judged only for answers, not refusals.

| Group | Questions | Refusal accuracy | Citation validity | Sentence coverage | Retrieval hit | Faithfulness | Relevance |
|---|---|---|---|---|---|---|---|
| all | 47 | 0.98 (46/47) | 1.00 (61/61) | 0.61 (48/79) | 0.97 (39/40) | 0.94 | 4.44 |
| paraphrase | 19 | 0.95 (18/19) | 1.00 (29/29) | 0.64 (21/33) | 1.00 (19/19) | 0.93 | 4.44 |
| exact_term | 17 | 1.00 (17/17) | 1.00 (26/26) | 0.67 (22/33) | 1.00 (17/17) | 1.00 | 4.65 |
| unanswerable | 7 | 1.00 (7/7) | n/a | n/a | n/a | n/a | n/a |
| follow-up | 4 | 1.00 (4/4) | 1.00 (6/6) | 0.38 (5/13) | 0.75 (3/4) | 0.79 | 3.50 |

## Follow-up rewriting

Each follow-up is asked with the earlier question and this run's answer to it as history, so the rewrite model turns it into a standalone query. The last two columns show whether a target passage reached the answer model when searching the follow-up as asked and when searching the rewritten query.

| Question | Follows | Asked | Rewritten query | As asked | Rewritten |
|---|---|---|---|---|---|
| q002 | q001 | What if the reason I'm leaving is that I've been called up for active military duty? | What if the reason I'm leaving is that I've been called up for active military duty, will the classes I was taking show up on my transcript? | yes | yes |
| q005 | q004 | What about graduate students? | What about graduate students if I got sick near the end of the quarter and can't finish my final project, can my professor give me more time to complete the class? | no | no |
| q011 | q010 | Are there situations where they can make the person take the animal out of the building? | Are there situations where staff can make someone with a guide dog take their animal out of the building? | yes | yes |
| q037 | q036 | Would taking that leave push back my tenure review? | Would taking paid time off for having or adopting a baby push back my tenure review? | yes | yes |

The rewritten query put a target among the model's passages for 3 of 4 follow-ups, against 3 of 4 searching the follow-up as asked: a gain of +0.

## Failure analysis

Each question that failed a check, with its categories: retrieval miss (no target among the passages given to the model), false refusal, false answer (an unanswerable question answered), invalid citation, unsupported claim (faithfulness below 1), low relevance (a score of 2 or less), and judge error. Quotes of the model's answers are shortened.

| Question | Type | Category | Detail |
|---|---|---|---|
| q005 | paraphrase | retrieval miss, low relevance | Asked: "What about graduate students?" Relevance 2. |
| q011 | paraphrase | unsupported claim | Asked: "Are there situations where they can make the person take the animal out of the building?" 1 of 3 claims unsupported, for example: "Staff cannot make someone with a guide dog take their animal out of the building." |
| q012 | paraphrase | unsupported claim | Asked: "Is it okay to text my kids or book a dentist appointment from my work computer?" 4 of 6 claims unsupported, for example: "The use must be occasional." |
| q015 | paraphrase | false refusal | Asked: "A coworker is going through cancer treatment and has used up her sick time. Can I give her some of my vacation hours?" Refused; a target was passage 2. |
| q021 | paraphrase | unsupported claim | Asked: "I volunteer at a UW summer camp and a child said something that makes me think they're being abused at home. Who do I have to tell, and how quickly?" 1 of 3 claims unsupported, for example: "You must report your concerns to the police or the Department of Social and Health Services (DSHS) immediately." |
| q034 | paraphrase | unsupported claim | Asked: "Our student group wants to put a sculpture on the lawn outside our building for a few months. Who has to approve it?" 1 of 3 claims unsupported, for example: "Therefore, the approval process for your sculpture will involve the appropriate University representatives" |
| q037 | paraphrase | unsupported claim | Asked: "Would taking that leave push back my tenure review?" 1 of 2 claims unsupported, for example: "Taking paid parental leave for having or adopting a baby would not push back your tenure review." |
| q038 | exact_term | judge error | Asked: "Which faculty appointment types do not lead to tenure under the Faculty Code?" faithfulness: invalid judge reply after a retry: the reply is not valid JSON (Unterminated string starting at) |

## Latency

Wall time per stage on the local machine (16 CPU cores, no GPU). The first question includes loading each model, and the judge and answer models share the CPU, so treat these as rough.

| Stage | Questions | Median s | p95 s |
|---|---|---|---|
| rewrite (follow-ups) | 4 | 16.8 | 34.3 |
| retrieve | 47 | 0.2 | 0.3 |
| generate | 47 | 58.9 | 113.9 |
| judge (answered questions) | 39 | 64.4 | 162.7 |
| total per question | 47 | 120.7 | 242.8 |

## Method and limitations

One judge model, `llama3.1:8b`, grades every answer at temperature 0 with a JSON schema. It comes from a different family than the answer model, so it does not grade its own family's work, but a single 8B judge still misreads some claims; spot-check its verdicts in the raw results before acting on a small difference. The judge sometimes labels a supported claim with a passage number it was not shown; those verdicts stand and the summary counts them. Faithfulness gives the judge only the passages an answer cites, so a true claim with a missing or wrong citation counts as unsupported. Relevance gives the judge the golden reference answer as a hint of what a good answer covers. The golden set is small (47 questions), so one question moves a rate by several points. Timings come from one CPU-only run and vary with other load on the machine.
