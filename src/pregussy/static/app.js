/* Shared helpers: tiny API client, local session keys, haptics, confetti. */

export const $ = (id) => document.getElementById(id);

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

export function show(node, visible) {
  node.classList.toggle("hidden", !visible);
}

/* ------------------------------------------------------------------ api -- */

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = "GET", body, token, hostToken } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["X-Player-Token"] = token;
  if (hostToken) headers["X-Host-Token"] = hostToken;

  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("No connection. Check your signal and try again.", 0);
  }

  const payload = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const detail = payload && payload.detail;
    // Our own errors are plain strings; a schema rejection arrives as pydantic's
    // list of {msg}, which still says something useful once the prefix is gone.
    const fromSchema = Array.isArray(detail) && detail[0] && detail[0].msg;
    throw new ApiError(
      typeof detail === "string" ? detail
        : fromSchema ? fromSchema.replace(/^Value error, /, "")
        : "Something went sideways. Try again.",
      res.status,
    );
  }
  return payload;
}

/* -------------------------------------------------------------- session -- */

export const session = {
  player: (code) => localStorage.getItem(`pregussy:player:${code}`),
  setPlayer: (code, token) => localStorage.setItem(`pregussy:player:${code}`, token),
  clearPlayer: (code) => localStorage.removeItem(`pregussy:player:${code}`),
  host: (code) => localStorage.getItem(`pregussy:host:${code}`),
  setHost: (code, token) => localStorage.setItem(`pregussy:host:${code}`, token),
  hostedCodes: () =>
    Object.keys(localStorage)
      .filter((k) => k.startsWith("pregussy:host:"))
      .map((k) => k.slice("pregussy:host:".length)),
};

/* --------------------------------------------------------------- naming -- */

/** Names are compared the way a human would: case- and spacing-insensitive. */
export const nameKey = (value) => value.trim().replace(/\s+/g, " ").toLowerCase();
export const cleanName = (value) => value.trim().replace(/\s+/g, " ");

/* --------------------------------------------------------------- extras -- */

export function buzz(pattern = 12) {
  if (navigator.vibrate) {
    try {
      navigator.vibrate(pattern);
    } catch {
      /* some browsers refuse without a gesture; never worth an error */
    }
  }
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function relativeTime(iso) {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.round(minutes / 60)}h ago`;
}

export function clockTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export const ordinal = (n) => {
  const suffix = ["th", "st", "nd", "rd"][(n % 100 - 20) % 10] || ["th", "st", "nd", "rd"][n % 100] || "th";
  return `${n}${suffix}`;
};

/* ------------------------------------------------------------- confetti -- */

/* Wildflowers over a park lawn: forest and gold lead, coral/lavender/teal accent. */
const CONFETTI_COLORS = [
  "#1f5c4a", "#d4a72c", "#ff7a59", "#8b7cf6", "#0ea5a4", "#4fb08d", "#f0e3b8",
];

/**
 * Festival confetti on a canvas. Returns a stop() handle. Respects
 * prefers-reduced-motion by drawing nothing at all.
 */
export function confetti(canvas, { pieces = 140, duration = 5200 } = {}) {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) return () => {};

  const ctx = canvas.getContext("2d");
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  let width = 0;
  let height = 0;

  const resize = () => {
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  resize();
  window.addEventListener("resize", resize);

  const bits = Array.from({ length: pieces }, () => ({
    x: Math.random() * width,
    y: -20 - Math.random() * height * 0.6,
    w: 6 + Math.random() * 7,
    h: 9 + Math.random() * 11,
    vy: 90 + Math.random() * 150,
    vx: -50 + Math.random() * 100,
    spin: -4 + Math.random() * 8,
    angle: Math.random() * Math.PI * 2,
    color: CONFETTI_COLORS[(Math.random() * CONFETTI_COLORS.length) | 0],
  }));

  let raf = 0;
  let last = performance.now();
  const started = last;

  const frame = (now) => {
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    ctx.clearRect(0, 0, width, height);

    const age = now - started;
    const fade = age > duration - 900 ? Math.max(0, (duration - age) / 900) : 1;

    for (const bit of bits) {
      bit.y += bit.vy * dt;
      bit.x += bit.vx * dt;
      bit.angle += bit.spin * dt;
      if (bit.y > height + 30) {
        bit.y = -20;
        bit.x = Math.random() * width;
      }
      ctx.save();
      ctx.globalAlpha = fade;
      ctx.translate(bit.x, bit.y);
      ctx.rotate(bit.angle);
      ctx.fillStyle = bit.color;
      ctx.fillRect(-bit.w / 2, -bit.h / 2, bit.w, bit.h);
      ctx.restore();
    }

    if (age < duration) raf = requestAnimationFrame(frame);
    else ctx.clearRect(0, 0, width, height);
  };
  raf = requestAnimationFrame(frame);

  return () => {
    cancelAnimationFrame(raf);
    window.removeEventListener("resize", resize);
    ctx.clearRect(0, 0, width, height);
  };
}
