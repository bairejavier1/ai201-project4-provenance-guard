/* ── Config ──────────────────────────────────────────────── */

const BASE = 'http://127.0.0.1:5000';
let currentContentId = null;

/* ── Sample texts ────────────────────────────────────────── */

const SAMPLES = {
  ai: `Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible deployment of these powerful technologies.`,

  human: `ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium in it and i was thirsty for like three hours after. my friend got the spicy version and said it was better. probably won't go back unless someone drags me there`,

  border1: `The relationship between monetary policy and asset price inflation has been extensively studied in the literature. Central banks face a fundamental tension between their mandate for price stability and the unintended consequences of prolonged low interest rates on equity and real estate valuations.`,

  border2: `I've been thinking a lot about remote work lately. There are genuine tradeoffs — flexibility and no commute on one side, isolation and blurred work-life boundaries on the other. Studies show productivity varies widely by individual and role type.`
};

function loadSample(key) {
  document.getElementById('text-input').value = SAMPLES[key];
  clearResult();
}

/* ── Page navigation ─────────────────────────────────────── */

function showPage(name, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'log') loadLog();
}

function clearAll() {
  document.getElementById('text-input').value = '';
  clearResult();
}

function clearResult() {
  ['result-card', 'processing-steps', 'error-box'].forEach(id =>
    document.getElementById(id).classList.add('hidden')
  );
  currentContentId = null;
}

/* ── Pipeline step animation ─────────────────────────────── */

function setStep(n, status, detail) {
  const el = document.getElementById('step-' + n);
  el.classList.remove('active', 'done');
  if (status) el.classList.add(status);
  if (detail) document.getElementById('d' + n).textContent = detail;
  if (status === 'done') el.querySelector('.step-dot').textContent = '✓';
}

function resetSteps() {
  for (let i = 1; i <= 4; i++) {
    const el = document.getElementById('step-' + i);
    el.classList.remove('active', 'done');
    el.querySelector('.step-dot').textContent = i;
  }
}

/* ── Submit content for analysis ─────────────────────────── */

async function submitContent() {
  const text      = document.getElementById('text-input').value.trim();
  const creatorId = document.getElementById('creator-input').value.trim() || 'demo-user';

  if (!text)             { showError('Please enter some text to analyze.'); return; }
  if (text.length < 20)  { showError('Text too short — enter at least 20 characters.'); return; }

  const btn = document.getElementById('submit-btn');
  btn.disabled = true;
  document.getElementById('submit-spinner').classList.remove('hidden');
  document.getElementById('error-box').classList.add('hidden');
  document.getElementById('result-card').classList.add('hidden');

  // Animate pipeline steps while the API call runs in the background
  resetSteps();
  document.getElementById('processing-steps').classList.remove('hidden');
  setStep(1, 'active');
  setTimeout(() => { setStep(1, 'done', 'LLM score received');       setStep(2, 'active'); }, 700);
  setTimeout(() => { setStep(2, 'done', 'Stylometric score computed'); setStep(3, 'active'); }, 1300);
  setTimeout(() => { setStep(3, 'done', 'Signals combined');           setStep(4, 'active'); }, 1800);

  try {
    const res  = await fetch(`${BASE}/submit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, creator_id: creatorId })
    });
    const data = await res.json();

    if (!res.ok) {
      if (res.status === 429) {
        showRateLimit(data);
      } else {
        showError(data.error || 'Submission failed.');
      }
      document.getElementById('processing-steps').classList.add('hidden');
      return;
    }

    setTimeout(() => {
      setStep(4, 'done', 'Label generated');
      setTimeout(() => showResult(data), 300);
    }, 2100);

  } catch (e) {
    showError('Cannot reach server. Is Flask running on port 5000?');
    document.getElementById('processing-steps').classList.add('hidden');
  } finally {
    btn.disabled = false;
    document.getElementById('submit-spinner').classList.add('hidden');
  }
}

/* ── Render result card ──────────────────────────────────── */

function showResult(data) {
  document.getElementById('processing-steps').classList.add('hidden');
  currentContentId = data.content_id;

  const attr  = data.attribution;
  const label = data.label;

  // Headline and body text come directly from the backend label
  document.getElementById('result-headline').textContent = label.headline;
  document.getElementById('result-body').textContent     = label.body;

  // Verdict badge — text and color class
  const badge = document.getElementById('result-badge');
  badge.textContent = attr === 'likely_ai'    ? 'AI-generated'  :
                      attr === 'likely_human' ? 'Human-written' : 'Uncertain';
  badge.className   = 'verdict-badge ' +
                      (attr === 'likely_ai'    ? 'badge-ai'    :
                       attr === 'likely_human' ? 'badge-human' : 'badge-unclear');

  // Combined bar color mirrors the verdict color
  const barColor = attr === 'likely_ai'    ? 'var(--red)'   :
                   attr === 'likely_human' ? 'var(--green)' : 'var(--amber)';

  // Signal score percentages
  document.getElementById('sig-llm').textContent      = (data.llm_score   * 100).toFixed(0) + '%';
  document.getElementById('sig-stylo').textContent    = (data.stylo_score  * 100).toFixed(0) + '%';
  document.getElementById('sig-combined').textContent = (data.confidence   * 100).toFixed(0) + '%';

  // Animated bar widths
  document.getElementById('bar-llm').style.width          = (data.llm_score   * 100) + '%';
  document.getElementById('bar-stylo').style.width         = (data.stylo_score  * 100) + '%';
  document.getElementById('bar-combined').style.width      = (data.confidence   * 100) + '%';
  document.getElementById('bar-combined').style.background = barColor;

  document.getElementById('cid-box').textContent = 'Content ID: ' + data.content_id;

  // Show reliability warnings if the backend flagged any
  const warningBox = document.getElementById('warning-box');
  if (data.warnings && data.warnings.length > 0) {
    warningBox.textContent = '⚠ Reliability note: ' + data.warnings.join(' ');
    warningBox.classList.remove('hidden');
  } else {
    warningBox.classList.add('hidden');
  }

  // Appeal section: only show when verdict isn't human
  const appealSec = document.getElementById('appeal-section');
  if (attr === 'likely_human') {
    appealSec.classList.add('hidden');
  } else {
    appealSec.classList.remove('hidden');
    document.getElementById('appeal-msg').classList.add('hidden');
    document.getElementById('appeal-text').value   = '';
    document.getElementById('appeal-btn').disabled = false;
  }

  document.getElementById('result-card').classList.remove('hidden');

  // Update sidebar with quick summary
  const sideLabel = attr === 'likely_ai'    ? '⚠️ AI-generated'  :
                    attr === 'likely_human' ? '✅ Human-written' : '🔍 Uncertain';
  document.getElementById('sidebar-last').innerHTML =
    `<span style="color: var(--text)">${sideLabel}</span><br>` +
    `<span style="font-family: var(--mono); font-size: 11px;">Score: ${(data.confidence * 100).toFixed(0)}%</span>`;

  setTimeout(refreshPanel, 500);
}

/* ── Submit appeal ───────────────────────────────────────── */

async function submitAppeal() {
  if (!currentContentId) return;

  const reasoning = document.getElementById('appeal-text').value.trim();
  if (!reasoning) return;

  const btn = document.getElementById('appeal-btn');
  btn.disabled = true;
  document.getElementById('appeal-spinner').classList.remove('hidden');

  try {
    const res  = await fetch(`${BASE}/appeal`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content_id: currentContentId, creator_reasoning: reasoning })
    });
    const data = await res.json();
    const msg  = document.getElementById('appeal-msg');

    msg.classList.remove('hidden');
    if (res.ok) {
      msg.className   = 'msg-success';
      msg.textContent = 'Appeal received — your content is now under review.';
    } else {
      msg.className   = 'msg-error';
      msg.textContent = data.error || 'Appeal failed.';
      btn.disabled    = false;
    }

    setTimeout(refreshPanel, 500);

  } catch (e) {
    showError('Cannot reach server.');
    btn.disabled = false;
  } finally {
    document.getElementById('appeal-spinner').classList.add('hidden');
  }
}

/* ── Utility: show error message ─────────────────────────── */

function showError(msg) {
  const el = document.getElementById('error-box');
  el.textContent = msg;
  el.classList.remove('hidden');
}

/* ── Rate limit banner with countdown ───────────────────── */

let countdownTimer = null;

function showRateLimit(data) {
  const el  = document.getElementById('error-box');
  const btn = document.getElementById('submit-btn');

  // Read retry_after from backend response (default 60s)
  const retryAfter = parseInt(data.retry_after) || 60;

  // Disable the submit button for the full cooldown
  btn.disabled = true;

  // Clear any existing countdown
  if (countdownTimer) clearInterval(countdownTimer);

  let remaining = retryAfter;

  function updateBanner() {
    el.innerHTML =
      `<strong>⏱ Rate limit reached</strong><br>` +
      `You can submit <strong>10 requests per minute</strong> and ` +
      `<strong>100 requests per day</strong>.<br>` +
      `Please wait <strong>${remaining}s</strong> before submitting again.`;
    el.classList.remove('hidden');
  }

  updateBanner();

  countdownTimer = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      clearInterval(countdownTimer);
      countdownTimer = null;
      btn.disabled   = false;
      el.innerHTML   =
        `✅ You can submit again now.`;
      // Auto-hide the message after 3 more seconds
      setTimeout(() => el.classList.add('hidden'), 3000);
    } else {
      updateBanner();
    }
  }, 1000);
}

/* ── Right panel: live activity feed ────────────────────── */

async function refreshPanel() {
  try {
    const res     = await fetch(`${BASE}/log`);
    const data    = await res.json();
    const entries = (data.entries || []).slice(-8).reverse();
    const panel   = document.getElementById('panel-log');

    if (!entries.length) {
      panel.innerHTML = '<div style="font-size: 12px; color: var(--text-dim);">No entries yet.</div>';
      return;
    }

    panel.innerHTML = entries.map(e => {
      const isAppeal = e.event === 'appeal_filed';
      const verdict  = isAppeal ? e.original_attribution : e.attribution;
      const score    = isAppeal ? e.original_confidence  : e.confidence;
      const badgeCls = verdict === 'likely_ai'    ? 'badge-ai'    :
                       verdict === 'likely_human' ? 'badge-human' : 'badge-unclear';
      const label    = verdict === 'likely_ai'    ? 'AI'    :
                       verdict === 'likely_human' ? 'Human' : 'Uncertain';

      return `
        <div class="activity-entry">
          <div class="activity-top">
            <span class="verdict-badge ${badgeCls}" style="font-size: 10px; padding: 3px 9px;">
              ${label}
            </span>
            <span class="activity-score">
              ${score !== undefined ? (score * 100).toFixed(0) + '%' : '—'}
            </span>
          </div>
          ${isAppeal ? '<div class="appeal-pill">Appeal filed</div>' : ''}
          <div class="activity-id">${e.content_id.slice(0, 18)}...</div>
          <div class="activity-time">${new Date(e.timestamp).toLocaleTimeString()}</div>
        </div>`;
    }).join('');

  } catch (e) { /* server may not be running yet */ }
}

/* ── Audit log table ─────────────────────────────────────── */

async function loadLog() {
  const tbody = document.getElementById('log-tbody');
  tbody.innerHTML = '<tr><td colspan="9" style="color: var(--text-dim); text-align: center; padding: 20px;">Loading...</td></tr>';

  try {
    const res     = await fetch(`${BASE}/log`);
    const data    = await res.json();
    const entries = (data.entries || []).reverse();

    if (!entries.length) {
      tbody.innerHTML = '<tr><td colspan="9" style="color: var(--text-dim); text-align: center; padding: 20px;">No entries yet.</td></tr>';
      return;
    }

    tbody.innerHTML = entries.map(e => {
      const isAppeal = e.event === 'appeal_filed';
      const verdict  = isAppeal ? e.original_attribution : e.attribution;
      const score    = isAppeal ? e.original_confidence  : e.confidence;
      const badgeCls = verdict === 'likely_ai'    ? 'badge-ai'    :
                       verdict === 'likely_human' ? 'badge-human' : 'badge-unclear';
      const label    = verdict === 'likely_ai'    ? 'AI'    :
                       verdict === 'likely_human' ? 'Human' : 'Uncertain';
      const chipCls  = e.status === 'under_review' ? 'chip-review' :
                       isAppeal                    ? 'chip-appeal' : 'chip-classified';
      const chipLbl  = e.status === 'under_review' ? 'Under review' :
                       isAppeal                    ? 'Appeal'       : 'Classified';

      return `
        <tr>
          <td><span class="chip ${isAppeal ? 'chip-appeal' : 'chip-classified'}">
            ${isAppeal ? 'appeal' : 'classify'}
          </span></td>
          <td class="mono">${e.content_id.slice(0, 13)}…</td>
          <td style="color: var(--text)">${e.creator_id || '—'}</td>
          <td>
            <span class="verdict-badge ${badgeCls}" style="font-size: 10px; padding: 3px 9px;">
              ${label}
            </span>
          </td>
          <td class="mono">${score !== undefined ? (score * 100).toFixed(1) + '%' : '—'}</td>
          <td class="mono">${e.llm_score   !== undefined ? (e.llm_score   * 100).toFixed(1) + '%' : '—'}</td>
          <td class="mono">${e.stylo_score !== undefined ? (e.stylo_score * 100).toFixed(1) + '%' : '—'}</td>
          <td><span class="chip ${chipCls}">${chipLbl}</span></td>
          <td style="font-size: 11px; color: var(--text-dim);">
            ${new Date(e.timestamp).toLocaleString()}
          </td>
        </tr>`;
    }).join('');

  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="9" style="color: var(--red); text-align: center; padding: 20px;">Cannot reach server. Is Flask running?</td></tr>';
  }
}

/* ── Init: poll activity panel every 30s ─────────────────── */
refreshPanel();
setInterval(refreshPanel, 30000);