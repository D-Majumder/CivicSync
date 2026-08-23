# CivicSync AI Evaluation Harness (Milestone 24)

A small, reproducible evaluation of CivicSync's existing Gemini
complaint-understanding pipeline (`ai.client.analyze_complaint`) across
English, Hindi, and Bengali, and across a range of realistic input
quality issues (spelling errors, incomplete words, transliteration,
colloquial phrasing, ambiguous or insufficient-information complaints).

This is **not** a new AI system. `run_evaluation.py` imports and calls
the exact same function the production citizen submission flow uses --
no prompt, schema, or model call is duplicated or modified anywhere in
this directory.

## The single most important metric

**Unsupported facts invented by the AI should be 0.** Every other metric
here is secondary to that one. A complaint like "something's wrong near
the school" must never turn into "a large pothole near Krishnanagar High
School" -- CivicSync's safety design depends on the model saying "I
don't know" (via `null` fields and a lower confidence score) rather than
inventing specifics the citizen never provided.

## Files

- `dataset.json` -- 52 hand-written, realistic civic complaint cases.
  Each case records `supported_facts` (informational: what's genuinely
  in the text) and `must_not_invent` (a list of specific concrete facts
  that must NOT appear in the AI's structured output). `expected_category`
  /`expected_severity` are only set when the input is unambiguous enough
  to defend a specific expectation -- both are intentionally `null` for
  ambiguous/insufficient-information cases, since forcing an expectation
  there would itself be a form of fabrication.
- `run_evaluation.py` -- runs every case through the real
  `analyze_complaint()`, applies deterministic (non-LLM) evaluation
  rules, and writes `results.json`.
- `results.json` -- generated output (not committed with fabricated
  data; only produced by actually running the script against a real
  Gemini API key).

## Running it

```bash
# from the repository root, with a real GEMINI_API_KEY in your .env
python3 evaluation/run_evaluation.py
```

If `GEMINI_API_KEY` isn't configured, the script refuses to run and
explains why, rather than producing fabricated or silently-skipped
results.

## How "unsupported fact" is detected

Deterministically, with no second AI model involved at any point (per
the milestone's explicit requirement -- an LLM is never used to judge
another LLM's output here). For each case, the AI's own structured
output fields (`problem`, `location`, `duration`, `affected_population`,
`suggested_department`) are concatenated and checked, case-insensitively,
for the literal presence of any string listed in that case's
`must_not_invent`.

**This is a conservative, defensible check, not an exhaustive semantic
hallucination detector.** A genuinely different paraphrase of a
forbidden fact (e.g. the model writes "the walkway" when the forbidden
term is "footpath") could be missed by a plain substring match. This is
disclosed as a real limitation, not hidden -- see the generated report.
The direction of this limitation is the safer one for this milestone's
purpose: it can under-report hallucinations, never manufacture false
positives against a model that behaved correctly.

## What is and isn't measured

- **Category/severity accuracy** -- only computed over cases with an
  explicit `expected_category`/`expected_severity`, never averaged
  against the full 52-case dataset (about half the dataset intentionally
  has neither, by design).
- **Multilingual successful-analysis rate** -- did the Hindi/Bengali
  cases complete without an error? This measures pipeline robustness,
  not extraction quality.
- **Imperfect-input successful-analysis rate** -- same idea, over the
  spelling-error/incomplete-word/transliteration/colloquial/mixed-language
  cases.
- **Ambiguous-input safety rate** -- of the ambiguous/insufficient-info
  cases that completed successfully, what fraction introduced zero
  unsupported facts.
- **Unsupported-fact rate** -- across every successfully-analyzed case,
  what fraction had at least one detected unsupported fact.

## Limitations (read before citing any number from this harness)

- **52 cases is a small, hand-curated prototype dataset**, not a
  statistically representative sample of real citizen complaints. Do not
  treat any percentage here as a production-grade accuracy claim.
- Gemini's outputs are not perfectly deterministic between runs (some
  sampling variance is possible even at low temperature); results.json
  reflects one run at one point in time, not a guaranteed reproduction
  every time the script is executed.
- The unsupported-fact check is a substring/keyword match, as described
  above -- it is a floor on measured hallucination, not a ceiling.
- This harness evaluates `analyze_complaint()` in isolation. It does not
  evaluate the full citizen journey, jurisdiction resolution, or any
  other part of CivicSync.
- Category/severity "correctness" reflects one reasonable human judgment
  of what a case's expected label should be, encoded at dataset-authoring
  time -- it is not the only defensible interpretation for every case.
