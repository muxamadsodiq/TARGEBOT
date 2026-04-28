// ───────────── State ─────────────
const state = {
  config: null,
  sIndex: 0,
  prize: 0,
};

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// ───────────── Boot ─────────────
async function boot() {
  try {
    const r = await fetch("/api/config");
    state.config = await r.json();
  } catch (e) {
    state.config = { name: "Quiz", subtitle: "online", photo: "", win_percent: 75, steps: [] };
  }
  $("#profileName").textContent = state.config.name || "Quiz";
  $("#profileSub").textContent = state.config.subtitle || "online";
  const photo = state.config.photo || "/static/default-avatar.svg";
  $("#profilePhoto").src = photo;
  $("#profilePhoto").onerror = () => {
    $("#profilePhoto").src =
      "data:image/svg+xml;utf8," +
      encodeURIComponent(
        `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><defs><linearGradient id='g' x1='0' x2='1' y1='0' y2='1'><stop offset='0' stop-color='%23ff5be4'/><stop offset='1' stop-color='%237c3aff'/></linearGradient></defs><rect width='64' height='64' fill='url(%23g)'/><text x='50%' y='54%' text-anchor='middle' fill='white' font-size='28' font-family='Arial' font-weight='700'>${(state.config.name||'Q')[0]}</text></svg>`
      );
  };

  renderZigzag(state.config.zigzag || []);

  const steps = state.config.steps || [];
  if (steps.length === 0) {
    addBotBubble("⚠️ Hozircha xabarlar yo'q. Admin botdan xabar qo'shing.");
    showResultButton();
    return;
  }
  setTimeout(() => playStep(), 600);
}

// ───────────── Zigzag render ─────────────
function renderZigzag(items) {
  const root = document.getElementById("zigzagList");
  if (!root) return;
  root.innerHTML = "";
  if (!items.length) {
    root.style.display = "none";
    return;
  }
  root.style.display = "";
  items.forEach((z, i) => {
    const block = document.createElement("div");
    block.className = "zig-block";

    if (z.title) {
      const h = document.createElement("h3");
      h.className = "zig-title";
      h.textContent = z.title;
      block.appendChild(h);
    }

    const row = document.createElement("div");
    row.className = "zig-row glass" + (i % 2 === 1 ? " reverse" : "");
    const txt = document.createElement("div");
    txt.className = "zig-text";
    txt.textContent = z.text || "";
    const img = document.createElement("div");
    img.className = "zig-img";
    if (z.image) {
      const im = document.createElement("img");
      im.src = z.image;
      im.alt = "";
      img.appendChild(im);
    } else {
      img.classList.add("empty");
    }
    row.appendChild(txt);
    row.appendChild(img);
    block.appendChild(row);
    root.appendChild(block);
  });
}

// ───────────── Chat helpers ─────────────
function showTyping() {
  const el = document.createElement("div");
  el.className = "bubble bot typing";
  el.innerHTML = "<span></span><span></span><span></span>";
  el.id = "typingIndicator";
  $("#chatBody").appendChild(el);
  scrollChat();
  return el;
}
function removeTyping() {
  const t = $("#typingIndicator");
  if (t) t.remove();
}
function scrollChat() {
  const body = $("#chatBody");
  body.scrollTop = body.scrollHeight;
}

function addBotBubble(text, opts = {}) {
  return new Promise((resolve) => {
    const el = document.createElement("div");
    el.className = "bubble bot";
    let inner = "";
    if (opts.image) {
      inner += `<img class="bub-img" src="${opts.image}" alt="" />`;
    }
    if (text) {
      inner += '<span class="txt"></span><span class="cursor"></span>';
    }
    if (opts.audio) {
      inner += `<audio class="bub-audio" controls preload="metadata" src="${opts.audio}"></audio>`;
    }
    el.innerHTML = inner;
    $("#chatBody").appendChild(el);
    scrollChat();

    if (!text) {
      // no typewriter — just resolve after a short pause
      setTimeout(() => resolve(el), 500);
      return;
    }
    const txtEl = el.querySelector(".txt");
    const curEl = el.querySelector(".cursor");
    let i = 0;
    const speed = 26;
    const tick = () => {
      txtEl.textContent = text.slice(0, ++i);
      scrollChat();
      if (i < text.length) setTimeout(tick, speed);
      else {
        setTimeout(() => curEl && curEl.remove(), 400);
        resolve(el);
      }
    };
    tick();
  });
}

// ───────────── Steps flow (auto) ─────────────
async function playStep() {
  const steps = state.config.steps || [];
  if (state.sIndex >= steps.length) return showResultButton();

  const s = steps[state.sIndex];
  const t = showTyping();
  await new Promise((r) => setTimeout(r, 700));
  removeTyping();
  await addBotBubble(s.text || "", { image: s.image, audio: s.audio });

  state.sIndex++;
  // pause before next step
  setTimeout(playStep, 1200);
}

function showResultButton() {
  const bar = $("#optionsBar");
  bar.innerHTML = "";
  const btn = document.createElement("button");
  btn.className = "cta-btn";
  btn.textContent = "🎁 Natijani ko'rish";
  btn.onclick = goToWheel;
  bar.appendChild(btn);
}

// ───────────── View switching ─────────────
function showView(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("locked");
  el.classList.add("unlock");
  setTimeout(() => {
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  }, 80);
}
function goToWheel() {
  showView("view-wheel");
  drawWheel();
}

// ───────────── Wheel ─────────────
const WHEEL_SLICES = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100];
const SLICE_COLORS = [
  "#ff5be4", "#7c3aff", "#3ad6ff", "#ffd84d", "#41d870",
  "#ff8a3a", "#5b8cff", "#ff3aa1", "#9b5bff", "#3affc1",
];

function drawWheel(rotation = 0) {
  const c = $("#wheel");
  const ctx = c.getContext("2d");
  const W = c.width, H = c.height;
  const cx = W / 2, cy = H / 2, r = W / 2 - 6;
  ctx.clearRect(0, 0, W, H);
  const N = WHEEL_SLICES.length;
  const slice = (Math.PI * 2) / N;

  for (let i = 0; i < N; i++) {
    const start = rotation + i * slice - Math.PI / 2;
    const end = start + slice;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, start, end);
    ctx.closePath();
    ctx.fillStyle = SLICE_COLORS[i % SLICE_COLORS.length];
    ctx.fill();
    ctx.strokeStyle = "rgba(255,255,255,.25)";
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(start + slice / 2);
    ctx.textAlign = "right";
    ctx.fillStyle = "#fff";
    ctx.font = "bold 18px -apple-system,Segoe UI,sans-serif";
    ctx.shadowColor = "rgba(0,0,0,.4)";
    ctx.shadowBlur = 4;
    ctx.fillText(WHEEL_SLICES[i] + "%", r - 14, 6);
    ctx.restore();
  }
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(255,255,255,.3)";
  ctx.lineWidth = 4;
  ctx.stroke();
}

function spin() {
  const target = state.config.win_percent ?? 75;
  let idx = 0, best = Infinity;
  WHEEL_SLICES.forEach((v, i) => {
    const d = Math.abs(v - target);
    if (d < best) { best = d; idx = i; }
  });
  const N = WHEEL_SLICES.length;
  const slice = (Math.PI * 2) / N;
  const turns = 6;
  const finalRot = turns * Math.PI * 2 - idx * slice - slice / 2;

  const dur = 5200;
  const t0 = performance.now();
  $("#spinBtn").disabled = true;

  function frame(t) {
    const p = Math.min(1, (t - t0) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    drawWheel(finalRot * eased);
    if (p < 1) requestAnimationFrame(frame);
    else {
      state.prize = WHEEL_SLICES[idx];
      $("#prizeValue").textContent = state.prize + "%";
      $("#prizeBox").classList.remove("hidden");
    }
  }
  requestAnimationFrame(frame);
}

$("#spinBtn").addEventListener("click", spin);
$("#usePrizeBtn").addEventListener("click", () => showView("view-form"));

// ───────────── Phone mask (+998 default) ─────────────
const phoneInput = document.querySelector('input[name="phone"]');
if (phoneInput) {
  const PREFIX = "+998 ";
  const formatPhone = (raw) => {
    let digits = raw.replace(/\D/g, "");
    if (digits.startsWith("998")) digits = digits.slice(3);
    digits = digits.slice(0, 9);
    let out = PREFIX;
    if (digits.length > 0) out += digits.slice(0, 2);
    if (digits.length > 2) out += " " + digits.slice(2, 5);
    if (digits.length > 5) out += " " + digits.slice(5, 7);
    if (digits.length > 7) out += " " + digits.slice(7, 9);
    return out;
  };
  phoneInput.addEventListener("input", (e) => {
    e.target.value = formatPhone(e.target.value);
  });
  phoneInput.addEventListener("focus", (e) => {
    if (!e.target.value.startsWith(PREFIX)) e.target.value = PREFIX;
    setTimeout(() => {
      const len = e.target.value.length;
      e.target.setSelectionRange(len, len);
    }, 0);
  });
  phoneInput.addEventListener("keydown", (e) => {
    if ((e.key === "Backspace" || e.key === "Delete") &&
        e.target.selectionStart <= PREFIX.length &&
        e.target.selectionEnd <= PREFIX.length) {
      e.preventDefault();
    }
  });
}

// ───────────── Form ─────────────
$("#leadForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const payload = {
    name: fd.get("name").trim(),
    surname: fd.get("surname").trim(),
    phone: fd.get("phone").trim(),
    percent: state.prize || (state.config.win_percent ?? 75),
    answers: [],
  };
  const btn = e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  btn.textContent = "Yuborilmoqda…";
  try {
    const r = await fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error("submit failed");
    $("#donePct").textContent = payload.percent + "%";
    showView("view-done");
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Yuborish ✓";
    alert("Xato: qayta urinib ko'ring");
  }
});

boot();
