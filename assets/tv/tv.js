// The browser page MusicHive casts to (src/cast_browser.py is the other side).
//
// MusicHive sends events: what to play ("load"), play, pause, seek, stop,
// volume, mute and how things should look ("style"). This page plays the song
// itself, draws the visualiser and follows the lyrics, and tells MusicHive
// every second what it is doing ("status").
//
// The music plays straight from the audio element: TV browsers stutter and drag
// when it is routed through Web Audio. The bars come from a bar map MusicHive
// makes of each song (src/cast_bars.py): how loud each part of the sound is, 30
// times a second, so looking up the moment the song has reached is all it takes.
// Without a map, the song is fetched once more and decoded here instead.
"use strict";

const audio = document.getElementById("player");
const canvas = document.getElementById("bars");
const paint = canvas.getContext("2d");
const lines = document.getElementById("lines");
const el = (id) => document.getElementById(id);

const LOWEST_HZ = 40;
const HIGHEST_HZ = 16000;
const FFT_SIZE = 1024; // as in MusicHive (src/now_playing.py)
// The decoded copy for the bars: lower than CD quality, so it takes less memory
// (a four-minute song is about 60 MB like this). Its top is 16 kHz, like the bars.
const ANALYSIS_RATE = 32000;
const ANALYSIS_MAX_BYTES = 50 * 1024 * 1024; // bigger files (long tracks) play without bars
const NO_LYRICS_TEXT = "No lyrics for this song";
const NO_LYRICS_HOLD_MS = 3000;
// Room below full scale: loud songs would otherwise clip in the TV's sound.
const HEADROOM = Math.pow(10, -1 / 20); // 1 dB
// A TV's browser is slow, and when it falls behind, the sound stutters. So the
// bars are drawn at most this wide and this often, and the browser stretches them.
const BARS_MAX_WIDTH = 1280;
const BARS_FRAME_MS = 1000 / 30;

let style = {
  bars: 12, gap: 8, opacity: 1, fall: 0.86, colours: ["#6c53a6"], shading: [130, 140],
  glow: false, coverBackground: false, blur: 100, darken: 0.5, fit: 0, barBackground: "",
  quietDb: -55, loudDb: 5, tiltDb: 3, lightText: true, window: "#151416", text: "#ffffff",
  shadow: false, shadowBlur: 8, shadowDistance: 2,
  shadowStrength: 0.86, showNoLyrics: true,
};
let current = null;      // what MusicHive loaded: {id, url, title, artist, cover, live, lyrics…}
let wantPlaying = false; // MusicHive's wish; the browser may still be waiting for "Start"
let allowed = false;     // "Start" was pressed: the browser lets us play sound
let ended = false;
let failed = "";
let volume = 1;
let muted = false;
let songGain = HEADROOM; // Level Volume's change for this song, never above full scale

// ----- talking to MusicHive -----

function state() {
  if (!current || ended || failed) return "idle";
  if (audio.paused) return wantPlaying ? "buffering" : "paused";
  return audio.readyState < 3 ? "buffering" : "playing";
}

function report() {
  if (!current) return;
  const status = {
    id: current.id,
    state: state(),
    position_ms: Math.round(audio.currentTime * 1000),
    duration_ms: isFinite(audio.duration) ? Math.round(audio.duration * 1000) : 0,
    ended: ended ? "finished" : "",
    error: failed,
  };
  fetch("status", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(status),
    keepalive: true,
  }).catch(() => {});
}
setInterval(report, 1000);
for (const name of ["playing", "pause", "waiting", "seeked", "durationchange"]) {
  audio.addEventListener(name, report);
}
audio.addEventListener("ended", () => { ended = true; report(); });
audio.addEventListener("error", () => {
  if (!current || !audio.getAttribute("src")) return;
  failed = (audio.error && audio.error.message) || "the browser cannot play this";
  report();
});

function clear() {
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
}

const events = new EventSource("events");
const on = (name, handle) =>
  events.addEventListener(name, (e) => handle(JSON.parse(e.data)));
events.onopen = () => el("waiting").classList.remove("shown");
events.onerror = () => el("waiting").classList.add("shown"); // it tries again by itself

on("load", load);
on("play", () => { wantPlaying = true; play(); report(); });
on("pause", () => { wantPlaying = false; audio.pause(); report(); });
on("seek", (d) => {
  audio.currentTime = d.position_ms / 1000;
  ended = false;
  report();
});
on("stop", () => {
  current = null;
  wantPlaying = false;
  sound = null;
  clear();
  showInfo(null);
  setLyrics(null);
});
on("volume", (d) => { volume = d.percent / 100; applyVolume(); });
on("mute", (d) => { muted = d.muted; applyVolume(); });
on("style", (d) => { style = d; applyStyle(); });
on("bars", (d) => {
  if (!current || current.id !== d.id) return;
  if (d.bars) loadBarMap(d.id, d.bars);
  else decodeForBars(current); // MusicHive could not make one: work it out here
});

function load(d) {
  current = d;
  ended = false;
  failed = "";
  wantPlaying = d.autoplay;
  songGain = Math.min(1, Math.pow(10, (d.gainDb || 0) / 20)) * HEADROOM;
  applyVolume();
  audio.src = d.url;
  barMap = null;
  sound = null;
  if (d.bars) loadBarMap(d.id, d.bars);
  else if (!d.barsComing) decodeForBars(d); // otherwise the "bars" event follows
  const start = d.position_ms / 1000;
  if (start > 0 && !d.live) {
    audio.addEventListener("loadedmetadata", () => { audio.currentTime = start; }, { once: true });
  }
  showInfo(d);
  setLyrics(d);
  if (wantPlaying) play();
  report();
}

// ----- sound -----

let sound = null; // the decoded copy for the bars: {id, samples, rate}
let barMap = null; // MusicHive's bar map: {id, data, fps, edges, floorDb, step}
let barRanges = { key: "", ranges: [] }; // the map's bands that make up each bar

async function loadBarMap(id, bars) {
  try {
    const response = await fetch(bars.url);
    if (!response.ok) throw new Error(response.statusText);
    const data = new Uint8Array(await response.arrayBuffer());
    if (!current || current.id !== id) return; // another song meanwhile
    barMap = { id, data, fps: bars.fps, edges: bars.edges, floorDb: bars.floorDb, step: bars.step };
  } catch (error) {
    if (current && current.id === id) decodeForBars(current);
  }
}

async function decodeForBars(d) {
  sound = null;
  const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  const home = new URL(d.url, location.href).origin === location.origin;
  if (d.live || !home || !Offline) return; // radio and other sites' streams: no bars
  try {
    const response = await fetch(d.url);
    const size = Number(response.headers.get("Content-Length") || 0);
    if (!response.ok || size > ANALYSIS_MAX_BYTES) return;
    const data = await response.arrayBuffer();
    if (!current || current.id !== d.id) return; // another song meanwhile
    // Only for decoding (at ANALYSIS_RATE); nothing is played through it.
    const decoder = new Offline(1, 1, ANALYSIS_RATE);
    const decoded = await decoder.decodeAudioData(data);
    if (!current || current.id !== d.id) return;
    sound = { id: d.id, samples: decoded.getChannelData(0), rate: decoded.sampleRate };
  } catch (error) {
    sound = null; // music without bars
  }
}

function applyVolume() {
  audio.volume = muted ? 0 : Math.min(1, volume * songGain);
}

function play() {
  if (!current) return;
  if (!allowed) {
    el("start").classList.add("shown");
    el("startButton").focus();
    return;
  }
  audio.play().catch(() => {
    allowed = false; // the browser wants another press
    el("start").classList.add("shown");
    el("startButton").focus();
  });
}

el("startButton").addEventListener("click", () => {
  allowed = true;
  el("start").classList.remove("shown");
  if (wantPlaying) play();
  el("playPause").focus(); // the remote's OK now plays and pauses
});

// ----- buttons: MusicHive does what its own buttons would -----

function control(action, value = 0) {
  fetch("control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, value }),
  }).catch(() => {});
}

el("previous").addEventListener("click", () => control("previous"));
el("next").addEventListener("click", () => control("next"));
el("playPause").addEventListener("click", () => {
  if (!allowed) allowed = true; // a press also lets the browser play sound
  control("playpause");
});

function seekBy(seconds) {
  if (!current || current.live || !isFinite(audio.duration)) return;
  const target = Math.max(0, Math.min(audio.duration, audio.currentTime + seconds));
  control("seek", Math.round(target * 1000));
}

el("track").addEventListener("click", (event) => {
  if (!current || current.live || !isFinite(audio.duration)) return;
  const bar = el("track").getBoundingClientRect();
  const share = Math.max(0, Math.min(1, (event.clientX - bar.left) / bar.width));
  control("seek", Math.round(share * audio.duration * 1000));
});

// A remote's media keys, and left/right to move between the buttons.
const buttons = ["previous", "playPause", "next"];
document.addEventListener("keydown", (event) => {
  const actions = {
    MediaPlayPause: () => control("playpause"),
    MediaPlay: () => { if (!wantPlaying) control("playpause"); },
    MediaPause: () => { if (wantPlaying) control("playpause"); },
    MediaTrackNext: () => control("next"),
    MediaTrackPrevious: () => control("previous"),
    MediaFastForward: () => seekBy(10),
    MediaRewind: () => seekBy(-10),
  };
  if (event.key in actions) {
    actions[event.key]();
    event.preventDefault();
    return;
  }
  if (el("start").classList.contains("shown")) return;
  const at = buttons.indexOf(document.activeElement && document.activeElement.id);
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    const step = event.key === "ArrowRight" ? 1 : -1;
    const next = at < 0 ? 1 : Math.max(0, Math.min(buttons.length - 1, at + step));
    el(buttons[next]).focus();
    event.preventDefault();
  } else if (at < 0 && (event.key === "Enter" || event.key === "ArrowUp" || event.key === "ArrowDown")) {
    el("playPause").focus(); // nothing focused yet: start at play/pause
    event.preventDefault();
  }
});

// ----- what plays -----

function showInfo(d) {
  el("title").textContent = d ? d.title : "";
  el("artist").textContent = d ? [d.artist, d.album].filter(Boolean).join(" · ") : "";
  el("cover").src = d && d.cover ? d.cover : "";
  el("progress").classList.toggle("hidden", !d || d.live);
  shownSecond = -1; // the times are written again
  drawBackground(d && d.cover && style.coverBackground ? d.cover : "");
}

// The cover behind everything: drawn small, so that stretched it looks blurred.
// More blur in MusicHive means a smaller picture here.
let backgroundFor = "";
function drawBackground(cover) {
  const background = el("background");
  const key = cover ? `${cover}|${style.blur}|${style.fit}` : "";
  if (key === backgroundFor) return;
  backgroundFor = key;
  if (!cover) {
    background.classList.remove("shown");
    return;
  }
  const picture = new Image();
  picture.onload = () => {
    if (backgroundFor !== key) return; // another song meanwhile
    const width = Math.max(8, Math.min(160, Math.round(4000 / Math.max(1, style.blur))));
    const height = Math.max(5, Math.round((width * innerHeight) / innerWidth));
    background.width = width;
    background.height = height;
    const g = background.getContext("2d");
    g.fillStyle = style.window;
    g.fillRect(0, 0, width, height);
    let w = width, h = height; // stretch
    if (style.fit !== 2) {
      const grow = (style.fit === 1 ? Math.min : Math.max)(
        width / picture.width, height / picture.height);
      w = picture.width * grow;
      h = picture.height * grow;
    }
    g.imageSmoothingQuality = "high";
    g.drawImage(picture, (width - w) / 2, (height - h) / 2, w, h);
    background.classList.add("shown");
  };
  picture.src = cover;
}

function clock(seconds) {
  if (!isFinite(seconds)) return "";
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor(s / 60) % 60;
  const rest = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${rest}` : `${m}:${rest}`;
}

let shownSecond = -1;
function showProgress() {
  const length = audio.duration;
  const second = Math.floor(audio.currentTime);
  if (second === shownSecond) return; // nothing new to show
  shownSecond = second;
  el("elapsed").textContent = clock(audio.currentTime);
  el("length").textContent = clock(length);
  const share = isFinite(length) && length > 0 ? audio.currentTime / length : 0;
  el("fill").style.transform = `scaleX(${share})`;
}

// ----- lyrics -----

let lyricTimes = [];
let currentLine = -2;
let noteTimer = 0;

function setLyrics(d) {
  clearTimeout(noteTimer);
  lines.replaceChildren();
  lines.className = "";
  lines.style.transform = "";
  lyricTimes = [];
  currentLine = -2;
  if (!d) return;
  const texts = d.lyrics ? d.lyrics.texts : [];
  if (!texts.length) {
    if (d.noLyricsNote && style.showNoLyrics) { // a song without them, not an episode
      lines.className = "note";
      addLine(NO_LYRICS_TEXT);
      centre(0);
      noteTimer = setTimeout(() => lines.classList.add("faded"), NO_LYRICS_HOLD_MS);
    }
    return;
  }
  lyricTimes = d.lyrics.times;
  if (!lyricTimes.length) lines.className = "plain";
  for (const text of texts) addLine(text || (lyricTimes.length ? "♪" : " "));
  if (lyricTimes.length) centre(0);
}

function addLine(text) {
  const line = document.createElement("div");
  line.className = "line";
  line.textContent = text;
  lines.appendChild(line);
}

function centre(index) {
  const line = lines.children[index];
  if (!line) return;
  const room = el("lyrics").clientHeight;
  lines.style.transform =
    `translateY(${room / 2 - line.offsetTop - line.offsetHeight / 2}px)`;
}

function followLyrics() {
  const ms = audio.currentTime * 1000;
  if (lyricTimes.length) {
    let low = 0, high = lyricTimes.length; // the last line that has started
    while (low < high) {
      const middle = (low + high) >> 1;
      if (lyricTimes[middle] <= ms) low = middle + 1; else high = middle;
    }
    const line = low - 1;
    if (line !== currentLine) {
      const old = lines.children[currentLine];
      if (old) old.classList.remove("current");
      currentLine = line;
      const now = lines.children[Math.max(line, 0)];
      if (line >= 0 && now) now.classList.add("current");
      // After the font change has been laid out:
      requestAnimationFrame(() => centre(Math.max(line, 0)));
    }
  } else if (lines.className === "plain" && isFinite(audio.duration) && audio.duration > 0) {
    // Plain lyrics scroll through as the song goes on.
    const room = el("lyrics").clientHeight;
    const spare = Math.max(0, lines.offsetHeight - room);
    lines.style.transform = `translateY(${-spare * (audio.currentTime / audio.duration)}px)`;
  }
}

// ----- the look -----

function applyStyle() {
  const root = document.documentElement.style;
  root.setProperty("--window", style.window);
  root.setProperty("--text", style.text);
  root.setProperty("--neutral", style.neutral || "#ffffff");
  const dimmed = style.lightText ? "rgba(255, 255, 255, 0.67)" : "rgba(0, 0, 0, 0.67)";
  root.setProperty("--dimmed", dimmed);
  if (style.shadow) {
    const shade = style.lightText ? "0, 0, 0" : "255, 255, 255";
    const d = style.shadowDistance;
    root.setProperty("--lyrics-shadow",
      `${d / 2}px ${d}px ${style.shadowBlur}px rgba(${shade}, ${style.shadowStrength})`);
  } else {
    root.setProperty("--lyrics-shadow", "none");
  }
  const dim = style.lightText ? "0, 0, 0" : "255, 255, 255";
  el("dim").style.background = style.coverBackground
    ? `rgba(${dim}, ${style.darken})` : "transparent";
  document.body.style.background = style.barBackground || style.window;
  showInfo(current);
  levels = new Array(style.bars).fill(0);
  gradients.clear();
}

// Qt's QColor.darker() and lighter(), which MusicHive's bars use.
function shade(hex, percent, lighter) {
  const n = parseInt(hex.slice(1), 16);
  let r = (n >> 16) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let v = max, s = max ? (max - min) / max : 0;
  let h = 0;
  if (max !== min) {
    if (max === r) h = ((g - b) / (max - min) + 6) % 6;
    else if (max === g) h = (b - r) / (max - min) + 2;
    else h = (r - g) / (max - min) + 4;
  }
  if (lighter) {
    v = (v * percent) / 100;
    if (v > 1) { s = Math.max(0, s - (v - 1)); v = 1; }
  } else {
    v = (v * 100) / percent;
  }
  const c = v * s, x = c * (1 - Math.abs((h % 2) - 1)), m = v - c;
  [r, g, b] = [[c, x, 0], [x, c, 0], [0, c, x], [0, x, c], [x, 0, c], [c, 0, x]][Math.floor(h) % 6];
  return [r + m, g + m, b + m].map((u) => Math.round(u * 255));
}

function rgba(rgb, alpha) {
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}

// ----- the visualiser -----

let levels = new Array(style.bars).fill(0);
let lastFrame = performance.now();
const gradients = new Map(); // made once per colour and size, not every frame

function barFill(colour, height) {
  const key = `${colour}|${height}`;
  let fill = gradients.get(key);
  if (!fill) {
    fill = paint.createLinearGradient(0, height, 0, 0);
    fill.addColorStop(0, rgba(shade(colour, style.shading[0], false), style.opacity));
    fill.addColorStop(1, rgba(shade(colour, style.shading[1], true), style.opacity));
    gradients.set(key, fill);
  }
  return fill;
}

// The FFT, as in MusicHive: a Hann window, then radix-2.
const hann = Float32Array.from({ length: FFT_SIZE },
  (_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (FFT_SIZE - 1)));
const bitsReversed = Uint16Array.from({ length: FFT_SIZE }, (_, i) => {
  let reversed = 0;
  for (let bit = 1, j = i; bit < FFT_SIZE; bit <<= 1, j >>= 1) reversed = (reversed << 1) | (j & 1);
  return reversed;
});
const cosines = Float32Array.from({ length: FFT_SIZE / 2 }, (_, i) => Math.cos((2 * Math.PI * i) / FFT_SIZE));
const sines = Float32Array.from({ length: FFT_SIZE / 2 }, (_, i) => -Math.sin((2 * Math.PI * i) / FFT_SIZE));
const real = new Float32Array(FFT_SIZE);
const imaginary = new Float32Array(FFT_SIZE);

function spectrumAt(samples, end) {
  for (let i = 0; i < FFT_SIZE; i++) {
    const at = end - FFT_SIZE + bitsReversed[i];
    real[i] = (at >= 0 && at < samples.length ? samples[at] : 0) * hann[bitsReversed[i]];
    imaginary[i] = 0;
  }
  for (let size = 2; size <= FFT_SIZE; size <<= 1) {
    const half = size >> 1;
    const step = FFT_SIZE / size;
    for (let start = 0; start < FFT_SIZE; start += size) {
      for (let k = 0, low = start; low < start + half; low++, k += step) {
        const high = low + half;
        const tReal = real[high] * cosines[k] - imaginary[high] * sines[k];
        const tImaginary = real[high] * sines[k] + imaginary[high] * cosines[k];
        real[high] = real[low] - tReal;
        imaginary[high] = imaginary[low] - tImaginary;
        real[low] += tReal;
        imaginary[low] += tImaginary;
      }
    }
  }
}

// For each bar, which bands of the map it covers: the bar's pitches are
// worked out as in MusicHive (40 Hz to 16 kHz, each bar a step higher).
function rangesFor(count, edges) {
  const key = `${count}|${edges.length}`;
  if (barRanges.key === key) return barRanges.ranges;
  const top = edges[edges.length - 1];
  const ratio = Math.pow(top / LOWEST_HZ, 1 / count);
  const ranges = [];
  for (let bar = 0; bar < count; bar++) {
    const low = LOWEST_HZ * Math.pow(ratio, bar);
    const high = low * ratio;
    let first = -1, last = -1;
    for (let band = 0; band < edges.length - 1; band++) {
      if (edges[band] < high && edges[band + 1] > low) {
        if (first < 0) first = band;
        last = band;
      }
    }
    ranges.push([Math.max(0, first), Math.max(0, last), low]);
  }
  barRanges = { key, ranges };
  return ranges;
}

function mapLevels(count) {
  const targets = new Array(count).fill(0);
  const bands = barMap.edges.length - 1;
  // The frame that ends where the song is, like the sound MusicHive's bars use.
  const frame = Math.max(0, Math.round(audio.currentTime * barMap.fps) - 1);
  const start = frame * bands;
  if (start + bands > barMap.data.length) return targets;
  rangesFor(count, barMap.edges).forEach(([first, last, low], bar) => {
    let loudest = 0;
    for (let band = first; band <= last; band++) {
      loudest = Math.max(loudest, barMap.data[start + band]);
    }
    if (!loudest) return;
    const tilt = style.tiltDb * Math.log2(low / 1000);
    const db = barMap.floorDb + loudest * barMap.step + tilt;
    targets[bar] = Math.max(0, Math.min(1, (db - style.quietDb) / (style.loudDb - style.quietDb)));
  });
  return targets;
}

function bandLevels() {
  const count = style.bars;
  const targets = new Array(count).fill(0);
  if (!current || audio.paused) return targets;
  if (barMap && barMap.id === current.id) return mapLevels(count);
  if (!sound || sound.id !== current.id) return targets;
  const rate = sound.rate;
  spectrumAt(sound.samples, Math.floor(audio.currentTime * rate));
  const hzPerBin = rate / FFT_SIZE;
  const top = Math.min(HIGHEST_HZ, rate / 2);
  const ratio = Math.pow(top / LOWEST_HZ, 1 / count);
  for (let bar = 0; bar < count; bar++) {
    const low = LOWEST_HZ * Math.pow(ratio, bar);
    const first = Math.max(1, Math.floor(low / hzPerBin));
    const end = Math.min(FFT_SIZE / 2, Math.max(first + 1, Math.floor((low * ratio) / hzPerBin)));
    let power = 0;
    for (let bin = first; bin < end; bin++) {
      power = Math.max(power, real[bin] * real[bin] + imaginary[bin] * imaginary[bin]);
    }
    const amplitude = (4 * Math.sqrt(power)) / FFT_SIZE; // 4: two sides, and the window's loss
    const tilt = style.tiltDb * Math.log2(low / 1000);
    const db = amplitude > 0 ? 20 * Math.log10(amplitude) + tilt : style.quietDb;
    targets[bar] = Math.max(0, Math.min(1, (db - style.quietDb) / (style.loudDb - style.quietDb)));
  }
  return targets;
}

function drawBars(now) {
  if (now - lastFrame < BARS_FRAME_MS - 2) return;
  // Pixels on the canvas per pixel of the page: never more than BARS_MAX_WIDTH wide.
  const scale = Math.min(window.devicePixelRatio || 1, BARS_MAX_WIDTH / canvas.clientWidth);
  const width = Math.round(canvas.clientWidth * scale);
  const height = Math.round(canvas.clientHeight * scale);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    gradients.clear();
  }
  // The fall speeds are per frame at 30 a second; frames here come as they may.
  const fall = Math.pow(style.fall, ((now - lastFrame) / 1000) * 30);
  lastFrame = now;
  const targets = bandLevels();
  if (levels.length !== style.bars) levels = new Array(style.bars).fill(0);
  for (let bar = 0; bar < levels.length; bar++) {
    let level = Math.max(targets[bar], levels[bar] * fall);
    levels[bar] = level < 0.003 ? 0 : level;
  }

  paint.clearRect(0, 0, width, height);
  const barWidth = width / levels.length;
  const gap = style.gap * scale;
  const glow = Math.max(6, Math.min(18, barWidth / 2)) * scale;
  levels.forEach((level, bar) => {
    const barHeight = level * height;
    if (barHeight < 1) return;
    const colour = style.colours[bar % style.colours.length];
    const left = bar * barWidth + gap / 2;
    const thick = Math.max(1, barWidth - gap);
    if (style.glow) {
      // A soft light around the bar from a few see-through layers: a real
      // shadow blur is far too slow for a TV.
      paint.fillStyle = colour;
      for (const [grow, alpha] of [[1, 0.06], [0.6, 0.08], [0.3, 0.12]]) {
        const out = glow * grow;
        paint.globalAlpha = alpha * style.opacity;
        paint.fillRect(left - out, height - barHeight - out, thick + 2 * out, barHeight + out);
      }
      paint.globalAlpha = 1;
    }
    paint.fillStyle = barFill(colour, height);
    paint.fillRect(left, height - barHeight, thick, barHeight);
  });
}

const playPause = el("playPause");
function frame(now) {
  const playing = Boolean(current) && wantPlaying;
  if (playPause.classList.contains("playing") !== playing) {
    playPause.classList.toggle("playing", playing);
  }
  drawBars(now);
  followLyrics();
  showProgress();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
