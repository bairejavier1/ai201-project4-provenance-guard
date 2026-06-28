# Provenance Guard — planning.md

> This document was written before implementation and updated where the implementation
> diverged from the original plan. Updates are marked with **[updated]**.

---

## Architecture

### System narrative

A piece of text enters via `POST /submit` carrying a `text` payload and a `creator_id`. The system validates the input first — rejecting anything too short, too long, or malformed — then routes the text through two independent detection signals.

Signal 1 (LLM via Groq) reads the text semantically, asking a large language model to assess whether the content reads as AI-generated based on phrasing patterns, structural clichés, and stylistic coherence. Signal 2 (stylometric heuristics) ignores meaning entirely and looks at statistical structure: sentence-length variance, vocabulary diversity, punctuation density, and frequency of formulaic transition phrases.

The two signal scores pass to a confidence scoring function that weights and combines them, applies a disagreement penalty when signals diverge, and maps the result to one of three attribution buckets. That bucket determines which transparency label variant is returned. Every step is written to a structured audit log. On startup, the log is read back to rebuild the in-memory content store so appeals work correctly across restarts.

An appeal enters via `POST /appeal` with a `content_id` and `creator_reasoning`. The system updates the record status to `under_review`, logs the appeal alongside the original classification, and returns a confirmation. No automated re-classification occurs.

### Architecture diagram

```
POST /submit
    │
    ▼
┌─────────────────────────────────────┐
│  Rate limiter (10/min · 100/day)    │──── 429 ──▶ Error response
└───────────────┬─────────────────────┘
                │
    ┌───────────▼─────────────────────┐
    │  Input validation               │──── 400 ──▶ Error response
    │  length · type · sanitise       │
    └───────────┬─────────────────────┘
                │ clean text
        ┌───────┴────────┐
        ▼                ▼
┌──────────────┐  ┌─────────────────────┐
│  SIGNAL 1    │  │  SIGNAL 2            │
│  LLM via     │  │  Stylometric         │
│  Groq        │  │  heuristics          │
│              │  │                      │
│  Semantic    │  │  - Sentence variance │
│  holistic    │  │  - Type-token ratio  │
│  assessment  │  │  - Punctuation density│
│              │  │  - Formulaic phrases │
│  → float     │  │  → float [0.0–1.0]  │
│  [0.0–1.0]   │  │  (pure Python)       │
└──────┬───────┘  └──────────┬───────────┘
       └──────────┬───────────┘
                  ▼
    ┌─────────────────────────────┐
    │  compute_confidence()       │
    │  0.60×LLM + 0.40×stylo      │
    │  if |diff| > 0.3 → cap 0.70 │
    │                             │
    │  ≥ 0.65 → likely_ai         │
    │  ≤ 0.35 → likely_human      │
    │  else   → uncertain         │
    └──────────────┬──────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
┌──────────────┐    ┌──────────────────┐
│ generate_    │    │  audit_log.json  │
│ label()      │    │  (persisted)     │
│              │    │                  │
│ 3 variants   │    │  rebuilt into    │
│ + warnings   │    │  content_store   │
└──────┬───────┘    │  on startup      │
       │            └──────────────────┘
       ▼
  JSON response


POST /appeal
    │
    ▼
┌────────────────────────────────────┐
│  Look up content_id in store       │
│  Validate not already under review │
│  Update status → "under_review"    │
│  Append appeal_filed to audit log  │
│  Return confirmation               │
└────────────────────────────────────┘
```

---

## Detection Signals

### Signal 1 — LLM semantic analysis (Groq)

**What it measures:** Semantic and stylistic coherence as a whole. The LLM is prompted to identify AI generation markers: formulaic transitions, hedged vagueness, structural predictability, absence of personal texture, and overly balanced argumentation.

**Output format:** Float [0.0, 1.0] where 1.0 = model is confident the text is AI-generated. Also returns a one-sentence `reasoning` string. **[updated: reasoning field added during implementation]**

**Why it captures what it does:** LLMs recognise LLM output particularly well because they share the same training distributions. Captures holistic semantic quality no rule-based system can replicate.

**Blind spot:** Formal human writing (academic papers, legal prose) shares surface properties with AI output. Cannot distinguish a human who writes like an AI from an AI itself.

**Resilience:** Regex fallback for malformed JSON responses. Returns neutral 0.5 on API failure. **[updated: added during implementation]**

---

### Signal 2 — Stylometric heuristics (pure Python)

**What it measures:** Four independent structural statistics computed from the raw text:

1. Sentence-length variance — AI text has suspiciously uniform sentence lengths
2. Type-token ratio (TTR) — AI reuses common vocabulary more than humans
3. Punctuation density — human writing uses more varied punctuation
4. Formulaic transition phrase frequency — explicit pattern matching **[updated: added as a 4th metric during testing; original spec planned 2–3 metrics]**

**Output format:** Weighted float [0.0, 1.0]. Automatically returns neutral 0.5 when text is too short, non-Latin, or looks like source code. **[updated: non-Latin and code detection added during implementation]**

**Why it captures what it does:** Statistical distributions of AI text differ measurably from human writing due to fluency optimization in training. Structurally independent from Signal 1.

**Blind spot:** Short texts (under 50 words) produce unreliable variance. Academic human writing is the most significant false-positive class.

---

## Uncertainty Representation

### Score thresholds

| Combined score | Attribution | Label variant |
|---|---|---|
| 0.00–0.35 | `likely_human` | High-confidence human |
| 0.36–0.64 | `uncertain` | Uncertain |
| 0.65–1.00 | `likely_ai` | High-confidence AI |

### Scoring formula

```
combined = (0.60 × llm_score) + (0.40 × stylo_score)
```

LLM gets 60% weight because semantic judgment outperforms structural statistics on short or stylistically varied text.

### Signal disagreement penalty

If `|llm_score − stylo_score| > 0.30`, the combined score is capped at 0.70. This forces the system into "uncertain" territory when signals conflict — disagreement between independent methods is itself evidence of uncertainty.

### What a score of 0.60 means

A score of 0.60 sits just below the `likely_ai` threshold and produces an "uncertain" label. The system is not confident enough at 0.60 to accuse a creator of using AI. A score of 0.66 — after passing the disagreement check — would flip to "Likely AI-Generated."

### False positive asymmetry

The `likely_ai` threshold is set at 0.65, not 0.5, because wrongly labeling a human creator's work as AI-generated is more harmful than missing an AI submission.

### Input length limits **[updated: added during implementation]**

| Limit | Value | Reason |
|---|---|---|
| Minimum | 50 characters | Below this the stylometric signal is unreliable |
| LLM analysis window | 4,000 characters | Raised from original 2,000 for better long-text coverage |
| Maximum | 10,000 characters | Beyond this only a small fraction would be analyzed by LLM |

---

## Transparency Label Variants

Three variants, written out exactly as displayed to users:

### Variant 1 — High-confidence AI (`combined ≥ 0.65`)

```
Headline: ⚠️ Likely AI-Generated
Body:     Our system is {pct}% confident this content was generated by an AI
          writing tool, not written by a person. The author can submit an
          appeal if this is incorrect.{warning_note}
CTA:      Dispute this label →
```

### Variant 2 — High-confidence human (`combined ≤ 0.35`)

```
Headline: ✅ Likely Human-Written
Body:     Our system is {100-pct}% confident this content was written by a
          person. We found no strong signs of AI generation.{warning_note}
CTA:      (none)
```

### Variant 3 — Uncertain (`0.35 < combined < 0.65`)

```
Headline: 🔍 Origin Unclear
Body:     We couldn't determine with confidence whether this content was
          written by a person or generated by AI. The author can add context
          or submit an appeal to clarify.{warning_note}
CTA:      Add context →
```

`{warning_note}` is appended when the system detected short text, non-Latin script, source code, or LLM truncation. **[updated: warning system added during implementation]**

---

## Appeals Workflow

**Who can submit:** Any creator who knows the `content_id` returned at submission time.

**What they provide:**
- `content_id` — the ID from the original submission
- `creator_reasoning` — free-text explanation (max 2,000 characters)

**What the system does:**
1. Validates the `content_id` exists and is not already under review
2. Updates status from `"classified"` to `"under_review"`
3. Writes an `appeal_filed` event to the audit log including the original scores and the creator's reasoning
4. Returns a confirmation with a `filed_at` timestamp

**What a human reviewer sees:**
- Audit log entries filtered by `event: "appeal_filed"`
- Original attribution, original confidence, and creator's reasoning side by side
- Full content_id to look up the original text and both signal scores

**What the system does NOT do:** Automated re-classification. A human makes the final call.

**UI behaviour:** **[updated: added during implementation]** The appeal section only appears when the verdict is `likely_ai` or `uncertain`. It is hidden entirely when the verdict is `likely_human` because there is no accusation to dispute.

---

## Anticipated Edge Cases

1. **Short lyric poetry with simple vocabulary:** A haiku or 4-line poem with simple, repetitive words will score high on AI-likeness from the stylometric signal (low TTR, low sentence variance) even if a human wrote it. Resolution: the system detects texts under 50 words, returns a neutral stylometric score, and shows a reliability warning.

2. **Non-native English speaker writing formally:** A creator who learned English formally may write in complete sentences, avoid contractions, and use transition words like "Furthermore" — all AI markers. Both signals may flag this as AI-generated. This is the most ethically serious false-positive case. The appeals workflow is the primary mitigation, which is why it is prominently surfaced.

3. **Lightly edited AI output:** A human who takes AI-generated text and makes small edits may produce a score in the mid-range. The disagreement penalty further softens the result. No clean solution exists for this case.

4. **Source code submitted as text:** Braces, indentation, and semicolons make the stylometric signal meaningless. **[updated: system now detects this and returns neutral 0.5 with a warning]**

5. **Very long content (over 4,000 characters):** **[updated: added during implementation]** The LLM only sees the first 4,000 characters. The system detects this and shows a warning indicating what percentage was analyzed.

---

## AI Tool Plan

### M3 — Submission endpoint and first signal

**Spec sections provided:** Detection signals (Signal 1 only), architecture diagram

**What I asked for:** Flask app skeleton with `POST /submit` route stub, `llm_signal()` function

**Verification:** Called `llm_signal()` directly on 3 test inputs, confirmed float return between 0 and 1, confirmed Flask route returned JSON with required fields

**What I revised:** Rewrote the Groq prompt to enforce JSON-only output, added regex fallback for malformed responses, added score clamping

### M4 — Second signal and confidence scoring

**Spec sections provided:** Detection signals (Signal 2), uncertainty representation section, architecture diagram

**What I asked for:** `stylometric_signal()` function, `compute_confidence()` combining function

**Verification:** Tested clearly AI and clearly human text through both signals separately, confirmed combined scores produce different attributions, confirmed disagreement penalty fires correctly

**What I revised:** Changed disagreement penalty from fixed subtraction to `min(combined, 0.70)` cap, added `signal_disagreement` field to return dict and audit log

### M5 — Production layer

**Spec sections provided:** Transparency label variants, appeals workflow, architecture diagram

**What I asked for:** `generate_label()` function, `POST /appeal` endpoint

**Verification:** Submitted texts targeting all three score ranges, confirmed all three label variants are reachable, submitted appeal and confirmed `GET /log` shows `under_review` status

**Additional work beyond the plan:** **[updated]**
- Added comprehensive input validation with specific error messages for each failure mode
- Added non-Latin script detection and source code detection to stylometric signal
- Added reliability warning system surfaced in both the label and the UI
- Added `build_content_store()` for persistence across restarts
- Added live countdown timer in UI for rate limit feedback
- Added full web UI (templates/index.html + static/app.js) with animated pipeline steps, signal score bars, and appeal form
- Added GET `/` route serving the UI, GET `/api` info endpoint, and GET `/status/<content_id>` endpoint
- Added Score guide tab to the UI explaining score ranges, both signals, the disagreement penalty, reliability warnings, and the appeals process in plain language