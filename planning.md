# Provenance Guard — planning.md

## Architecture

### System Narrative

A piece of text enters via `POST /submit`. It carries a `text` payload and a `creator_id`. The system routes that text through two independent detection signals running in sequence. Signal 1 (LLM via Groq) reads the text semantically — it asks a large language model to assess whether the content reads as AI-generated based on phrasing patterns, structural clichés, and stylistic coherence. Signal 2 (stylometric heuristics) ignores meaning entirely and looks at statistical structure: sentence-length variance, vocabulary diversity (type-token ratio), punctuation density, and frequency of formulaic transition phrases. The two signal scores are passed to a confidence scoring function that weights and combines them, applies a disagreement penalty when the signals diverge, and maps the result to one of three attribution buckets. That bucket determines which transparency label variant is returned. Every step — scores, attribution, label — is written to a structured audit log. The response returns the `content_id`, both signal scores, the combined confidence, the attribution, and the label text.

An appeal enters via `POST /appeal` with a `content_id` and `creator_reasoning`. The system looks up the original record, updates its status to `under_review`, logs the appeal alongside the original classification, and returns a confirmation. No automated re-classification occurs — a human reviewer would process the queue.

### Architecture Diagram

```
POST /submit
    │
    ▼
┌─────────────────────────────────────┐
│         Input Validation            │
│  (text present, min length check)   │
└───────────────┬─────────────────────┘
                │
        ┌───────┴────────┐
        ▼                ▼
┌──────────────┐  ┌─────────────────────┐
│  SIGNAL 1    │  │     SIGNAL 2         │
│  LLM via     │  │  Stylometric         │
│  Groq        │  │  Heuristics          │
│              │  │  - Sentence variance │
│  Semantic    │  │  - Type-token ratio  │
│  holistic    │  │  - Punctuation density│
│  assessment  │  │  - Formulaic phrases │
│              │  │                      │
│  → float     │  │  → float [0.0–1.0]  │
│  [0.0–1.0]   │  │                      │
└──────┬───────┘  └──────────┬───────────┘
       │   llm_score          │  stylo_score
       └──────────┬───────────┘
                  ▼
    ┌─────────────────────────────┐
    │     Confidence Scoring      │
    │  combined = 0.6*llm         │
    │            + 0.4*stylo      │
    │  if |diff| > 0.3 → cap 0.70│
    │                             │
    │  >= 0.65  → likely_ai       │
    │  <= 0.35  → likely_human    │
    │  else     → uncertain       │
    └──────────────┬──────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
┌──────────────┐    ┌──────────────────┐
│ Transparency │    │   Audit Log      │
│ Label Gen    │    │   (JSON file)    │
│              │    │                  │
│ 3 variants   │    │ timestamp        │
│ plain lang.  │    │ content_id       │
│              │    │ attribution      │
└──────┬───────┘    │ confidence       │
       │            │ llm_score        │
       │            │ stylo_score      │
       ▼            │ status           │
  JSON Response     └──────────────────┘


POST /appeal
    │
    ▼
┌────────────────────────────┐
│  Look up content_id        │
│  Update status →           │
│    "under_review"          │
│  Log appeal_filed event    │
│  Return confirmation       │
└────────────────────────────┘
```

---

## Detection Signals

### Signal 1: LLM-Based Classification (Groq — llama-3.3-70b-versatile)

**What it measures:** The semantic and stylistic coherence of the text as a whole. The LLM is prompted to identify AI generation markers: formulaic transitions, hedged vagueness, structural predictability, absence of personal texture, and overly balanced argumentation.

**Output format:** Float [0.0, 1.0] where 1.0 = the model believes the text is almost certainly AI-generated.

**Why it captures what it does:** Large language models are well-positioned to recognize the output of other LLMs because they share the same training data patterns. The LLM can reason holistically about semantic intent in a way no rule-based system can.

**Blind spot:** Formal human writing (academic papers, legal briefs) can appear to the LLM as AI-generated because it shares surface properties — precision, hedging, structured argument. The LLM also can't distinguish between a human who has learned to write like an AI and an AI itself.

---

### Signal 2: Stylometric Heuristics (Pure Python)

**What it measures:** Four independent statistical properties of the text's structure, independent of semantic meaning:

1. **Sentence-length variance** — AI text has suspiciously uniform sentence lengths (low standard deviation). Human writing is more jagged.
2. **Type-token ratio (TTR)** — ratio of unique words to total words. AI reuses common vocabulary more; human writing (especially informal) shows greater lexical diversity.
3. **Punctuation density** — density of non-period punctuation (commas, semicolons, dashes, parentheses). AI tends toward simpler punctuation patterns.
4. **Formulaic transition phrase frequency** — explicit count of phrases like "Furthermore", "It is important to note", "in today's world", etc.

**Output format:** Weighted float [0.0, 1.0] where 1.0 = high AI-likeness structurally.

**Why it captures what it does:** These are structural properties that AI models exhibit due to their training objectives — optimizing for fluency and coherence produces measurably different statistical distributions than human writing, which is messier and more variable.

**Blind spot:** Short texts (< 50 words) produce unreliable variance estimates — not enough data points. Academic human writing is formally structured and may score high on sentence uniformity and low on punctuation variety, triggering false positives.

---

## Uncertainty Representation

### Score Meaning

| Combined Score | Interpretation | Label Variant |
|---|---|---|
| 0.00 – 0.35 | Likely human-written | `high_confidence_human` |
| 0.36 – 0.64 | Genuinely uncertain | `uncertain` |
| 0.65 – 1.00 | Likely AI-generated | `high_confidence_ai` |

### Score Calibration Approach

The combined score is: `0.60 × llm_score + 0.40 × stylo_score`

- LLM gets 60% weight because semantic judgment outperforms structural statistics for short texts and stylistically varied inputs.
- Stylometric gets 40% because it provides independent structural corroboration that doesn't depend on the LLM's interpretation.
- **Signal disagreement penalty:** If `|llm_score - stylo_score| > 0.3`, the combined score is capped at 0.70 regardless of its arithmetic value. This forces the system into "uncertain" territory when signals conflict — matching the intuition that disagreement between independent methods is itself evidence of uncertainty.
- **False positive asymmetry:** The "likely_ai" threshold is set at 0.65 (not 0.5) because wrongly labeling a human creator's work as AI-generated is more harmful than missing an AI submission.

### What 0.6 means to the system
A score of 0.60 sits just below the `likely_ai` threshold. It produces an **"uncertain"** label. This is intentional: at 0.60, we're not confident enough to accuse a creator of using AI. The label will say "We couldn't determine with confidence..." and offer them a path to add context. A score of 0.66 — after clearing the disagreement check — would flip to "Likely AI-Generated."

---

## Transparency Label Variants

Three variants, written out exactly as displayed to users:

### Variant 1: High-Confidence AI (`combined >= 0.65`)
```
Headline: ⚠️ Likely AI-Generated
Body: Our system is {pct}% confident this content was generated by an AI writing 
      tool, not written by a person. The author can submit an appeal if this is incorrect.
CTA: Dispute this label →
```

### Variant 2: High-Confidence Human (`combined <= 0.35`)
```
Headline: ✅ Likely Human-Written
Body: Our system is {100-pct}% confident this content was written by a person. 
      We found no strong signs of AI generation.
CTA: (none)
```

### Variant 3: Uncertain (`0.35 < combined < 0.65`)
```
Headline: 🔍 Origin Unclear
Body: We couldn't determine with confidence whether this content was written by a 
      person or generated by AI. The author can add context or submit an appeal to clarify.
CTA: Add context →
```

---

## Appeals Workflow

**Who can submit:** Any creator, identified by knowing the `content_id` returned at submission time (acts as a lightweight access control — only someone who submitted the content would have its ID).

**What they provide:**
- `content_id` — the ID from the original submission
- `creator_reasoning` — free-text explanation of why they believe the classification is wrong

**What the system does on appeal:**
1. Looks up the record by `content_id`
2. Validates the content exists and isn't already under review
3. Updates `status` from `"classified"` to `"under_review"`
4. Writes an `appeal_filed` event to the audit log that includes the original attribution, original confidence, and the creator's reasoning
5. Returns a confirmation with a `filed_at` timestamp

**What a human reviewer would see:**
- The audit log filtered for `event: "appeal_filed"` entries
- Each entry contains: `content_id`, `original_attribution`, `original_confidence`, `appeal_reasoning`, and `timestamp`
- The reviewer can look up the `content_id` in the content store to see the full original text and both signal scores
- No automated re-classification occurs; the reviewer makes the final call

---

## Anticipated Edge Cases

1. **Short lyric poetry with simple vocabulary:** A haiku or 4-line poem with simple, repetitive words will score high on AI-likeness from the stylometric signal (low TTR, low sentence variance) even if a human wrote it. The LLM signal may also be confused by the lack of contextual cues. Resolution: the disagreement penalty will often soften this, and the "uncertain" bucket provides a safe fallback.

2. **Non-native English speaker writing formally:** A creator who has learned English formally may write in complete sentences, avoid contractions, use transition words like "Furthermore," and demonstrate uniform sentence structure — all AI markers. Both signals may flag this as AI-generated despite it being human-written. This is the most ethically serious false-positive case and is why the appeal workflow is prominently surfaced.

3. **Lightly edited AI output:** If a human takes AI-generated text and makes small edits (changing a few words, adding one personal anecdote), the LLM signal may drop to mid-range while the stylometric signal stays high. The disagreement causes the combined score to be capped at 0.70 — which may still trigger "likely_ai". This is an inherently hard case with no clean solution.

4. **Very long content (> 2000 chars):** The LLM signal truncates at 2000 characters. If the beginning of a long piece reads as human but the AI-generated middle is cut off, the LLM score may be misleadingly low. The stylometric signal evaluates the full text and provides a partial correction.

---

## AI Tool Plan

### M3 — Submission Endpoint + First Signal
- **Spec sections provided:** Detection signals (Signal 1 only), Architecture diagram
- **What I'll ask for:** Flask app skeleton with `POST /submit` route stub + `llm_signal()` function
- **Verification:** Call `llm_signal()` directly on 3 test inputs; confirm it returns a float between 0 and 1; confirm the Flask route returns JSON with `content_id`, `attribution`, `confidence`, `label`

### M4 — Second Signal + Confidence Scoring
- **Spec sections provided:** Detection signals (Signal 2), Uncertainty representation section, Architecture diagram
- **What I'll ask for:** `stylometric_signal()` function + `compute_confidence()` combining function
- **Verification:** Run clearly AI and clearly human text through both signals separately; check that combined scores produce different attributions; check disagreement penalty fires when signals diverge by > 0.3

### M5 — Production Layer
- **Spec sections provided:** Transparency label variants, Appeals workflow, Architecture diagram
- **What I'll ask for:** `generate_label()` function + `POST /appeal` endpoint
- **Verification:** Submit texts targeting all three score ranges and confirm all three label variants are returned; submit an appeal and call `GET /log` to confirm status is `under_review` and reasoning is present in the log