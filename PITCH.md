# CivicSync — Pitch Deck (Text Form)

*A judge-facing summary of what CivicSync is, why it matters, and what's actually built. Every claim below is backed by real, working code in this repository — nothing here is aspirational.*

---

## 1. The Problem

Civic complaint reporting in most municipalities is fragmented: phone calls, paper forms, WhatsApp messages, or scattered counter visits. This creates three compounding failures:

- **For citizens:** no way to know what happened to their report after they filed it. Did anyone see it? Is it being worked on? Was it ever actually fixed?
- **For authorities:** no unified operational view. Reports arrive in different formats, in different languages, with no consistent way to triage severity or spot patterns — three "small" potholes reported separately look nothing like one dangerous stretch of road, even when they're two blocks apart.
- **For accountability:** resolutions are rarely documented, evidence rarely captured, and there's no structured way for a citizen to contest a resolution they believe was inadequate.

Language is a fourth, often-overlooked barrier: civic tech tools built English-only quietly exclude a large share of the population they're meant to serve.

## 2. The Solution

CivicSync turns an unstructured citizen complaint — typed in **English, Hindi, or Bengali**, spelling mistakes and all — into a structured civic record: classified, routed, tracked, and resolved through a governed lifecycle, with a clear, enforced line between what an AI *suggests* and what an authority *decides*.

It is a working, tested prototype: **781 passing tests**, a real FastAPI backend, a real SQLite database with 13 linear Alembic migrations, and a functioning citizen + authority experience you can click through end to end today.

## 3. Why AI, Specifically

AI is used for exactly one structural job in this system: **turning messy human language into a structured record a government workflow can act on.** That's it. It is never allowed to make an operational decision.

This is a deliberate design choice, not a limitation we ran out of time to fix:

- Gemini extracts category, severity, location description, duration, affected population, and a confidence score from raw complaint text — including text with spelling errors, transliteration, and colloquial phrasing across three languages.
- Gemini can also explain an existing classification on demand, generate an operational briefing, and suggest a priority ordering over *already-computed, grounded* insights — always advisory, always re-generated fresh, **never persisted as fact**.
- Every operationally consequential action — resolving an issue, assigning a department, approving a reopen request — goes through the exact same code-level transition validation regardless of who or what suggested it. AI has no code path that can perform any of these actions.
- Geographic hotspot detection (clustering nearby complaints) is **not** done by AI at all — it's plain haversine-distance geometry, specifically so "which complaints are related" is a fact, not a guess.

We treat "the AI said so" as never being a sufficient justification for any official action in this system. That boundary is enforced in code, not just in policy.

## 4. Architecture at a Glance

```
Citizen (EN / HI / BN)
        │
        ▼
Gemini structured extraction   ← the ONLY AI-touched step in the write path
        │
        ▼
Jurisdiction resolution → Issue created (SUBMITTED)
        │
        ▼
Authority: classify → route → acknowledge → work → resolve (+ evidence)
        │
        ▼
Citizen tracks status/resolution/evidence  →  may request reopening
        │
        ▼
Authority approves (REOPENED) or rejects — same governed transition system
```

**Stack:** FastAPI + SQLAlchemy + SQLite/Alembic on the backend, vanilla HTML/CSS/JS on the frontend (deliberately no framework, no build step — anyone can read the source directly), Google Gemini for the AI layer, PBKDF2 + signed session cookies for authority auth.

## 5. What's Actually Built (Not Roadmap)

- **Full lifecycle state machine** — 9 statuses, every transition validated, every change recorded in an append-only history.
- **Real jurisdiction hierarchy** — a 4-level tree (country → state → district → local body); every issue is scoped to a real jurisdiction from creation, independent of department assignment (a real bug from an earlier build — issues silently disappearing from jurisdiction-scoped views — was found and fixed by making jurisdiction a direct, required field rather than a derived one).
- **Resolution evidence** — authorities attach photo evidence; validated by real image-format inspection (not just a file extension), never just a client-declared content type.
- **Citizen reopening** — a full request/review workflow, never a shortcut around the lifecycle rules.
- **13 civic categories** — Street Lighting, Roads and Potholes, Water Supply, Sewage and Drainage, Sanitation and Waste, Electricity, Public Safety, Public Transport, Parks and Public Spaces, Noise Pollution, Illegal Construction, Stray Animals, Other.
- **Deterministic operational intelligence** — resolution rate, reopen rate, evidence coverage, aging, department workload, and priority advisories, all computed from real data with an explicit "nothing here is fabricated" guarantee shown to the authority.
- **Deterministic geospatial hotspot detection** — geometry-based clustering of nearby, recent complaints.
- **Multilingual citizen experience** — English/Hindi/Bengali across the report form and tracking page, with a lightweight, dependency-free i18n layer.
- **Consent-based geolocation** — browser Geolocation API only, never inferred, never required.
- **A real AI evaluation harness** — not a demo script, an actual measurement tool (see below).
- **36 test files, 781 tests** covering the backend, the evaluation harness, and the demo-data tooling.

## 6. The Evaluation Harness — Our Differentiator

Most hackathon AI projects show you a demo and ask you to trust it. We built a standalone tool that **measures** the AI pipeline instead.

`evaluation/` runs a 52-case, hand-curated dataset — English, Hindi, and Bengali; normal complaints; spelling errors; transliteration; incomplete words; colloquial phrasing; mixed-language input; and deliberately ambiguous or insufficient-information complaints — through the real, unmodified extraction pipeline, and scores the results with **deterministic, non-LLM rules** (no AI judging AI).

The single most important number it measures: **unsupported facts invented by the AI.** Every case has a `must_not_invent` list of specific facts that must never appear in the model's structured output. A complaint like *"something's wrong near the school"* must produce `null` fields and a lower confidence score — never an invented pothole, a fabricated street name, or a guessed cause. That's the actual safety property this whole system depends on, and it's the one thing we chose to make measurable rather than just claim.

```bash
python3 evaluation/run_evaluation.py          # runs the real pipeline against all 52 cases
python3 evaluation/generate_report.py          # produces a readable evaluation/report.md
```

## 7. Live Demo Script (≈4 minutes)

1. **Submit a complaint in Hindi** ("सड़क पर बहुत बड़ा गड्ढा है, बहुत खतरनाक है।") on the citizen report page — show the AI classification appear (category, severity, confidence) and the public tracking ID.
2. **Switch the page to Bengali**, then back to English — same page, same session, no reload of underlying data — to show the multilingual layer is real, not a screenshot.
3. **Track that report** on the public tracking page — show the lifecycle timeline, and note that the original citizen text is preserved exactly as typed, never machine-translated.
4. **Log into the authority dashboard** — show the jurisdiction-scoped Command Center: KPI strip, resolution & reopening metrics, civic hotspots, operational breakdowns.
5. **Open the issue, resolve it with a photo of evidence** — point out the visual separation between the "AI-DERIVED — NOT OFFICIAL" panel and the "✓ OFFICIAL" record.
6. **Back on the citizen tracking page**, show the resolution and evidence now visible, and submit a reopen request.
7. **Back in the authority dashboard**, approve the reopen request — show the issue move to REOPENED through the same governed transition system, not a special case.
8. *(If time allows)* Run `python3 evaluation/run_evaluation.py` live, or show a prior `evaluation/report.md`, to make the "we measure hallucination, we don't just claim we've solved it" point concrete.

## 8. Honest Limitations (Said Out Loud, Not Buried)

- This is a hackathon-stage prototype. There is no production deployment, no real users, no uptime guarantee.
- Evidence storage uses the local filesystem by default and is **not durable** across redeploys on a platform with an ephemeral filesystem unless a persistent volume is explicitly attached — we documented this rather than silently assuming it would just work.
- The 52-case evaluation dataset is a small, hand-curated diagnostic set, not a statistically representative benchmark of real-world complaint volume.
- The authority side is a single shared demo account — there is no multi-user or role-based access control yet.
- Geolocation is optional and citizen-consented only; we do not infer or estimate location when it isn't shared.
- No claims are made anywhere in this project about user counts, production traffic, or deployments beyond this prototype.

We'd rather a judge find these limitations here, stated plainly, than discover them by clicking around and wondering if we'd noticed.

## 9. What We'd Build Next

- Multi-authority accounts with role-based permissions (currently a single shared demo login).
- Persistent, production-grade evidence storage (a Railway Volume, or object storage, behind the same storage abstraction already in place — swapping the backend requires touching exactly one file, by design).
- Citizen-facing geocoded location capture UI improvements (currently functional but intentionally minimal, with no map dependency added yet).
- Expanding the evaluation dataset beyond 52 cases and running it at meaningfully larger scale.
- A public, read-only "Impact Map" (already reserved as a disabled nav item in the UI) built on the existing hotspot-detection data.

## 10. Why This Should Win

- **It's real.** Every screen, every workflow, every number in this document is backed by code you can run and tests you can execute — not a slide deck describing a plan.
- **It takes AI safety seriously in a way you can verify, not just trust.** The unsupported-fact evaluation harness is not a demo prop; it's a genuine measurement tool with an honestly-stated methodology and honestly-stated limitations.
- **It's built for the people it's meant to serve.** Multilingual from the ground up, not bolted on — because a civic tool that only works in English isn't actually civic infrastructure for everyone.
- **It respects the line between AI and authority.** Every consequential decision is a human, auditable, reversible action — AI informs, it never decides.
