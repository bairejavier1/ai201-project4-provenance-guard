import os
import json
import uuid
import math
import re
from datetime import datetime, timezone
from dotenv import load_dotenv

from flask import Flask, request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq

load_dotenv()

app = Flask(__name__)

# ── Rate Limiting ──────────────────────────────────────────────────────────────
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# ── Groq client ───────────────────────────────────────────────────────────────
_groq_api_key = os.environ.get("GROQ_API_KEY", "").strip()
client = Groq(api_key=_groq_api_key) if _groq_api_key else None

# ── Audit log ─────────────────────────────────────────────────────────────────
AUDIT_LOG_FILE = "audit_log.json"


def load_log():
    if os.path.exists(AUDIT_LOG_FILE):
        try:
            with open(AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []   # corrupted file — start fresh
    return []


def save_log(entries):
    try:
        with open(AUDIT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
    except OSError as e:
        print(f"[audit log write error] {e}")


def append_log(entry):
    entries = load_log()
    entries.append(entry)
    save_log(entries)


# ── Content store (in-memory, keyed by content_id) ────────────────────────────

def build_content_store() -> dict:
    """
    Rebuild the in-memory content store from audit_log.json on startup.
    This means appeals still work after a Flask restart — the content_id
    lookup succeeds because we've re-loaded all previous classifications.

    Only 'classification' events are loaded (not appeal events, which are
    child records of a classification). Appeal status is then applied on top
    so the store reflects the latest known state of each submission.

    Tradeoff: the entire log is read into RAM on startup. At demo scale
    (~50 entries, ~25 KB) this is instantaneous. At production scale you
    would replace this with a proper database query.
    """
    store   = {}
    entries = load_log()

    # First pass: load all classifications
    for entry in entries:
        if entry.get("event") != "classification":
            continue
        cid = entry.get("content_id")
        if not cid:
            continue
        store[cid] = {
            "content_id":          cid,
            "creator_id":          entry.get("creator_id", "anonymous"),
            "text_preview":        "",          # not stored in log for privacy
            "status":              entry.get("status", "classified"),
            "attribution":         entry.get("attribution"),
            "confidence":          entry.get("confidence"),
            "llm_score":           entry.get("llm_score"),
            "llm_reasoning":       entry.get("llm_reasoning", ""),
            "stylo_score":         entry.get("stylo_score"),
            "signal_disagreement": entry.get("signal_disagreement"),
            "label":               entry.get("label"),
            "warnings":            entry.get("warnings", []),
            "timestamp":           entry.get("timestamp"),
            "appeal":              None,
        }

    # Second pass: apply any appeals on top
    for entry in entries:
        if entry.get("event") != "appeal_filed":
            continue
        cid = entry.get("content_id")
        if cid in store:
            store[cid]["status"] = "under_review"
            store[cid]["appeal"] = {
                "reasoning": entry.get("appeal_reasoning", ""),
                "filed_at":  entry.get("timestamp"),
            }

    return store


content_store = build_content_store()

# ── Input validation constants ────────────────────────────────────────────────
MIN_TEXT_LENGTH  = 50   # ~10 words minimum for stylometric signal
MAX_TEXT_LENGTH  = 10_000   # ~2,000 words
MIN_CREATOR_LEN  = 1
MAX_CREATOR_LEN  = 100
MAX_REASONING_LEN = 2_000
LLM_CHAR_LIMIT    = 4_000   # chars sent to Groq per request


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def sanitize_text(text: str) -> str:
    """Strip null bytes and normalise whitespace. Keep newlines."""
    text = text.replace("\x00", "")          # null bytes break JSON
    text = re.sub(r"[ \t]+", " ", text)      # collapse horizontal whitespace
    return text.strip()


def is_mostly_non_latin(text: str) -> bool:
    """
    Returns True if more than 60% of word-characters are non-Latin.
    The stylometric signal is tuned on English text and is unreliable
    for other scripts — we flag this so the caller can warn the user.
    """
    chars = re.findall(r"\w", text)
    if not chars:
        return False
    non_latin = sum(1 for c in chars if ord(c) > 591)  # beyond Latin Extended-B
    return (non_latin / len(chars)) > 0.6


def looks_like_code(text: str) -> bool:
    """
    Heuristic: if >30% of lines contain code-like tokens (braces, semicolons,
    indentation patterns) the text is probably source code, not prose.
    The stylometric signal is meaningless on code.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    code_lines = sum(
        1 for l in lines
        if re.search(r"[{};]|^\s{4,}|\bdef \b|\bfunction\b|\bclass \b|//|/\*", l)
    )
    return (code_lines / len(lines)) > 0.3


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 1: LLM-based classification via Groq
# ─────────────────────────────────────────────────────────────────────────────

def llm_signal(text: str) -> tuple[float, str]:
    """
    Returns (score, reasoning_note).
    score: float [0.0, 1.0] — 1.0 = definitely AI-generated.
    Falls back to (0.5, "unavailable") on any error so the pipeline
    can still return a result via the stylometric signal alone.
    """
    if client is None:
        return 0.5, "LLM signal unavailable (no API key)"

    prompt = f"""You are an expert forensic writing analyst. Analyze the following text and determine the probability that it was generated by an AI language model rather than written by a human.

Consider these AI writing markers:
- Overly uniform sentence structure and rhythm
- Formulaic transitions ("Furthermore", "Moreover", "It is important to note")
- Hedging without specificity ("various", "numerous", "many")
- Lack of personal anecdotes, typos, or genuine emotional texture
- Perfectly balanced "on one hand / on the other hand" structures
- Generic examples rather than specific, lived details

Respond with ONLY a JSON object in this exact format (no markdown fences, no extra text):
{{"ai_probability": 0.85, "reasoning": "one sentence explanation"}}

The ai_probability must be a number between 0.0 (definitely human) and 1.0 (definitely AI).

TEXT TO ANALYZE:
\"\"\"
{text[:LLM_CHAR_LIMIT]}
\"\"\"
"""
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.1,
        )
        raw = response.choices[0].message.content.strip()

        # Strip markdown fences the model sometimes adds despite instructions
        raw = re.sub(r"```(?:json)?|```", "", raw).strip()

        # Parse JSON
        parsed = json.loads(raw)

        score = float(parsed.get("ai_probability", 0.5))
        score = max(0.0, min(1.0, score))   # clamp to [0, 1]

        reasoning = str(parsed.get("reasoning", ""))[:300]
        return score, reasoning

    except json.JSONDecodeError:
        # Model returned non-JSON despite instructions — extract a number if possible
        match = re.search(r"\b0?\.\d+\b", raw)
        if match:
            score = max(0.0, min(1.0, float(match.group())))
            return score, "parsed from non-JSON response"
        print(f"[LLM signal] JSON decode failed, raw: {raw[:200]}")
        return 0.5, "parse error"

    except Exception as e:
        print(f"[LLM signal error] {type(e).__name__}: {e}")
        return 0.5, "API error"


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL 2: Stylometric heuristics
# ─────────────────────────────────────────────────────────────────────────────

def stylometric_signal(text: str) -> float:
    """
    Returns float [0.0, 1.0] — 1.0 = high structural AI-likeness.
    Returns 0.5 (neutral) when the text is too short, is non-Latin,
    or looks like source code — all cases where the heuristics are unreliable.
    """
    word_list = text.split()

    # Guard: unreliable inputs → neutral score
    if len(word_list) < 10:
        return 0.5
    if is_mostly_non_latin(text):
        return 0.5
    if looks_like_code(text):
        return 0.5

    # 1. Sentence-length variance
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    sentences = [s for s in sentences if len(s.split()) >= 2]

    if len(sentences) < 2:
        sentence_uniformity = 0.5
    else:
        word_counts = [len(s.split()) for s in sentences]
        mean_len    = sum(word_counts) / len(word_counts)
        variance    = sum((w - mean_len) ** 2 for w in word_counts) / len(word_counts)
        std_dev     = math.sqrt(variance)
        # Low std_dev (uniform) → high AI score
        sentence_uniformity = max(0.0, min(1.0, 1.0 - (std_dev / 12.0)))

    # 2. Type-token ratio
    words = re.findall(r"\b[a-z']+\b", text.lower())
    if words:
        ttr          = len(set(words)) / len(words)
        ttr_ai_score = max(0.0, min(1.0, 1.0 - ((ttr - 0.3) / 0.5)))
    else:
        ttr_ai_score = 0.5

    # 3. Punctuation density
    varied_punct  = sum(1 for c in text if c in ',;:"\'()[]—–-')
    punct_density = varied_punct / max(len(words), 1)
    punct_ai_score = max(0.0, min(1.0, 1.0 - (punct_density / 0.15)))

    # 4. Formulaic AI transition phrases
    formulaic_patterns = [
        r"\bit is important to note\b",
        r"\bfurthermore\b",
        r"\bmoreover\b",
        r"\bin conclusion\b",
        r"\bit is worth noting\b",
        r"\bone must consider\b",
        r"\bvarious (?:aspects|factors|sectors|stakeholders)\b",
        r"\bin today'?s (?:world|society|landscape)\b",
        r"\bit is essential to\b",
        r"\bultimately\b.*\bbecause\b",
    ]
    matches        = sum(1 for p in formulaic_patterns if re.search(p, text.lower()))
    formulaic_score = min(1.0, matches / 3.0)

    combined = (
        0.35 * sentence_uniformity
        + 0.25 * ttr_ai_score
        + 0.20 * punct_ai_score
        + 0.20 * formulaic_score
    )
    return round(max(0.0, min(1.0, combined)), 4)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIDENCE SCORING
# ─────────────────────────────────────────────────────────────────────────────

def compute_confidence(llm_score: float, stylo_score: float) -> dict:
    """
    Combines both signals into a calibrated confidence score.

    Weights: LLM 60%, stylometric 40%.
    Disagreement penalty: if |llm - stylo| > 0.30, cap combined at 0.70
      so the system never sounds confidently wrong when signals conflict.
    Thresholds (false-positive-aware):
      >= 0.65 → likely_ai
      <= 0.35 → likely_human
      else    → uncertain
    """
    combined     = round(0.60 * llm_score + 0.40 * stylo_score, 4)
    disagreement = round(abs(llm_score - stylo_score), 4)

    if disagreement > 0.30:
        combined = min(combined, 0.70)

    if combined >= 0.65:
        attribution = "likely_ai"
    elif combined <= 0.35:
        attribution = "likely_human"
    else:
        attribution = "uncertain"

    return {
        "combined_score":      combined,
        "attribution":         attribution,
        "signal_disagreement": disagreement,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TRANSPARENCY LABEL
# ─────────────────────────────────────────────────────────────────────────────

def generate_label(attribution: str, combined_score: float,
                   warnings: list[str] | None = None) -> dict:
    """
    Returns a plain-language label for display to a non-technical reader.
    Optional warnings (e.g. non-Latin text, very short input) are appended
    to the body so readers understand why a result may be less reliable.
    """
    pct          = int(round(combined_score * 100))
    warning_note = (" Note: " + " ".join(warnings)) if warnings else ""

    if attribution == "likely_ai":
        return {
            "headline": "⚠️ Likely AI-Generated",
            "body": (
                f"Our system is {pct}% confident this content was generated "
                "by an AI writing tool, not written by a person. "
                f"The author can submit an appeal if this is incorrect.{warning_note}"
            ),
            "cta":     "Dispute this label →",
            "variant": "high_confidence_ai",
        }
    elif attribution == "likely_human":
        return {
            "headline": "✅ Likely Human-Written",
            "body": (
                f"Our system is {100 - pct}% confident this content was "
                f"written by a person. We found no strong signs of AI generation.{warning_note}"
            ),
            "cta":     None,
            "variant": "high_confidence_human",
        }
    else:
        return {
            "headline": "🔍 Origin Unclear",
            "body": (
                "We couldn't determine with confidence whether this content "
                "was written by a person or generated by AI. "
                f"The author can add context or submit an appeal to clarify.{warning_note}"
            ),
            "cta":     "Add context →",
            "variant": "uncertain",
        }


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/api", methods=["GET"])
def api_info():
    return jsonify({
        "service":   "Provenance Guard",
        "version":   "1.0.0",
        "status":    "running",
        "endpoints": {
            "POST /submit":             "Submit text for attribution analysis",
            "POST /appeal":             "Contest a classification result",
            "GET  /log":                "View structured audit log",
            "GET  /status/<content_id>":"Check status of a submission",
        }
    }), 200


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    # ── Parse JSON body ───────────────────────────────────────────────────────
    if not request.is_json:
        return jsonify({"error": "Request must have Content-Type: application/json"}), 415

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400

    if data is None:
        return jsonify({"error": "Empty or unparseable JSON body"}), 400

    # ── Validate and sanitise fields ──────────────────────────────────────────
    raw_text   = data.get("text", "")
    creator_id = data.get("creator_id", "anonymous")

    if not isinstance(raw_text, str):
        return jsonify({"error": "'text' must be a string"}), 400
    if not isinstance(creator_id, str):
        creator_id = "anonymous"

    text       = sanitize_text(raw_text)
    creator_id = sanitize_text(creator_id)[:MAX_CREATOR_LEN] or "anonymous"

    if not text:
        return jsonify({"error": "'text' field is required and cannot be empty"}), 400
    if len(text) < MIN_TEXT_LENGTH:
        return jsonify({
            "error": f"Text too short — minimum {MIN_TEXT_LENGTH} characters for meaningful analysis"
        }), 400
    if len(text) > MAX_TEXT_LENGTH:
        return jsonify({
            "error": f"Text too long — maximum {MAX_TEXT_LENGTH} characters ({MAX_TEXT_LENGTH // 5} words)"
        }), 400

    # ── Collect reliability warnings ──────────────────────────────────────────
    warnings = []
    if len(text.split()) < 50:
        warnings.append("Short texts produce less reliable scores.")
    if len(text) > LLM_CHAR_LIMIT:
        pct = int((LLM_CHAR_LIMIT / len(text)) * 100)
        warnings.append(
            f"Text is long ({len(text):,} chars) — the LLM signal analyzed only "
            f"the first {LLM_CHAR_LIMIT:,} characters ({pct}% of the text). "
            "The stylometric signal analyzed the full text."
        )
    if is_mostly_non_latin(text):
        warnings.append("Non-Latin text: structural analysis is tuned for English and may be inaccurate.")
    if looks_like_code(text):
        warnings.append("This looks like source code — stylometric analysis is unreliable for code.")

    # ── Run detection pipeline ────────────────────────────────────────────────
    content_id = str(uuid.uuid4())
    timestamp  = datetime.now(timezone.utc).isoformat()

    llm_score, llm_reasoning = llm_signal(text)
    stylo_score               = stylometric_signal(text)
    scoring                   = compute_confidence(llm_score, stylo_score)
    combined_score            = scoring["combined_score"]
    attribution               = scoring["attribution"]
    label                     = generate_label(attribution, combined_score, warnings or None)

    # ── Persist ───────────────────────────────────────────────────────────────
    record = {
        "content_id":          content_id,
        "creator_id":          creator_id,
        "text_preview":        text[:200],
        "status":              "classified",
        "attribution":         attribution,
        "confidence":          combined_score,
        "llm_score":           llm_score,
        "llm_reasoning":       llm_reasoning,
        "stylo_score":         stylo_score,
        "signal_disagreement": scoring["signal_disagreement"],
        "label":               label,
        "warnings":            warnings,
        "timestamp":           timestamp,
        "appeal":              None,
    }
    content_store[content_id] = record

    append_log({
        "event":               "classification",
        "content_id":          content_id,
        "creator_id":          creator_id,
        "timestamp":           timestamp,
        "attribution":         attribution,
        "confidence":          combined_score,
        "llm_score":           llm_score,
        "stylo_score":         stylo_score,
        "signal_disagreement": scoring["signal_disagreement"],
        "warnings":            warnings,
        "status":              "classified",
    })

    return jsonify({
        "content_id":          content_id,
        "attribution":         attribution,
        "confidence":          combined_score,
        "llm_score":           llm_score,
        "llm_reasoning":       llm_reasoning,
        "stylo_score":         stylo_score,
        "signal_disagreement": scoring["signal_disagreement"],
        "label":               label,
        "warnings":            warnings,
        "status":              "classified",
        "timestamp":           timestamp,
    }), 200


@app.route("/appeal", methods=["POST"])
def appeal():
    if not request.is_json:
        return jsonify({"error": "Request must have Content-Type: application/json"}), 415

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400

    if data is None:
        return jsonify({"error": "Empty or unparseable JSON body"}), 400

    content_id = sanitize_text(str(data.get("content_id", "")))
    reasoning  = sanitize_text(str(data.get("creator_reasoning", "")))

    # Validate
    if not content_id:
        return jsonify({"error": "'content_id' is required"}), 400
    if not reasoning:
        return jsonify({"error": "'creator_reasoning' is required"}), 400
    if len(reasoning) > MAX_REASONING_LEN:
        return jsonify({
            "error": f"Reasoning too long — maximum {MAX_REASONING_LEN} characters"
        }), 400

    record = content_store.get(content_id)
    if not record:
        return jsonify({
            "error": "content_id not found. The content must be submitted before appealing."
        }), 404

    if record.get("status") == "under_review":
        return jsonify({
            "error": "An appeal is already under review for this content."
        }), 409

    timestamp = datetime.now(timezone.utc).isoformat()
    record["status"] = "under_review"
    record["appeal"] = {"reasoning": reasoning, "filed_at": timestamp}

    append_log({
        "event":                "appeal_filed",
        "content_id":           content_id,
        "creator_id":           record.get("creator_id"),
        "timestamp":            timestamp,
        "original_attribution": record["attribution"],
        "original_confidence":  record["confidence"],
        "appeal_reasoning":     reasoning,
        "status":               "under_review",
    })

    return jsonify({
        "content_id": content_id,
        "status":     "under_review",
        "message":    "Your appeal has been received and is queued for human review.",
        "filed_at":   timestamp,
    }), 200


@app.route("/log", methods=["GET"])
def get_log():
    entries = load_log()
    return jsonify({"count": len(entries), "entries": entries}), 200


@app.route("/status/<content_id>", methods=["GET"])
def get_status(content_id):
    # Basic UUID format check to avoid log spam from garbage inputs
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", content_id):
        return jsonify({"error": "Invalid content_id format"}), 400

    record = content_store.get(content_id)
    if not record:
        return jsonify({"error": "content_id not found"}), 404

    return jsonify({
        "content_id": content_id,
        "status":     record["status"],
        "attribution":record["attribution"],
        "confidence": record["confidence"],
        "label":      record["label"],
        "warnings":   record.get("warnings", []),
        "appeal":     record.get("appeal"),
    }), 200


@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({
        "error":       "Rate limit exceeded",
        "message":     "Limit: 10 per minute / 100 per day. Please try again shortly.",
        "retry_after": 60,
    }), 429


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint not found"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed on this endpoint"}), 405


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    if not _groq_api_key:
        print("⚠️  WARNING: GROQ_API_KEY not set. LLM signal will return neutral 0.5 scores.")
    restored = len(content_store)
    if restored:
        print(f"✅ Restored {restored} submission(s) from audit log — appeals will work across restarts.")
    else:
        print("ℹ️  No previous submissions found in audit log — starting fresh.")
    app.run(debug=True, port=5000)