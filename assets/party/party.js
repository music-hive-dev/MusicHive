// The Party Mode page: search the host's library, add songs, vote to skip.
// Everything other people typed (names, and tags from the files) is shown with
// textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const SEARCH_DELAY_MS = 250;

let texts = {};
let guestId = load("musichive-party-guest");
const MAX_NAME = 15; // as in party.py
let guestName = load("musichive-party-name").slice(0, MAX_NAME);
let lastState = null;
let events = null;
let searchTimer = 0;
let searchNumber = 0; // only the newest search's results are shown
let toastTimer = 0;
let votedFor = ""; // the song this guest voted to skip

if (!guestId) {
  guestId = randomId();
  save("musichive-party-guest", guestId);
}

function load(key) {
  try { return localStorage.getItem(key) || ""; } catch (e) { return ""; }
}
function save(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* private window */ }
}
function randomId() {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
function text(key, values = {}) {
  let result = texts[key] || "";
  for (const [name, value] of Object.entries(values)) {
    result = result.split(`{${name}}`).join(String(value));
  }
  return result;
}

// ----- talking to MusicHive -----

function connect() {
  events = new EventSource(`events?guest=${encodeURIComponent(guestId)}`);
  events.addEventListener("open", () => { $("banner").hidden = true; });
  events.addEventListener("error", () => {
    if (!$("endedScreen").hidden) return;
    $("banner").textContent = texts.lost || "…";
    $("banner").hidden = false;
  });
  events.addEventListener("texts", (event) => {
    texts = JSON.parse(event.data);
    showTexts();
    if (lastState) showState(lastState);
  });
  events.addEventListener("state", (event) => {
    lastState = JSON.parse(event.data);
    showState(lastState);
  });
  events.addEventListener("notice", (event) => toast(JSON.parse(event.data).message));
  events.addEventListener("ended", () => {
    events.close();
    $("banner").hidden = true;
    $("endedScreen").hidden = false;
  });
}

async function post(what, body) {
  try {
    const answer = await fetch(what, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guest: guestId, name: guestName, ...body }),
    });
    const reply = await answer.json();
    toast(reply.error || reply.message || "");
    return !reply.error;
  } catch (e) {
    toast(texts.lost || "…");
    return false;
  }
}

// ----- showing things -----

function showTexts() {
  for (const element of document.querySelectorAll("[data-text]")) {
    if (texts[element.dataset.text]) element.textContent = texts[element.dataset.text];
  }
  for (const element of document.querySelectorAll("[data-placeholder]")) {
    if (texts[element.dataset.placeholder]) element.placeholder = texts[element.dataset.placeholder];
  }
}

function showState(state) {
  const playing = Boolean(state.title);
  $("nowLabel").textContent = playing
    ? (state.playing ? text("nowPlaying") : text("paused"))
    : text("nothingPlaying");
  $("nowTitle").textContent = state.title;
  $("nowArtist").textContent = state.artist;
  showGuest($("nowGuest"), state.guest);
  $("nowCover").hidden = !state.cover;
  $("nowPlaceholder").hidden = Boolean(state.cover);
  if (state.cover && $("nowCover").getAttribute("src") !== state.cover) {
    $("nowCover").src = state.cover;
  }

  $("skip").hidden = !(state.voteSkip && playing);
  const voted = votedFor === songKey(state);
  $("voteSkip").disabled = voted;
  $("voteSkip").textContent = voted ? text("voted") : text("voteSkip");
  $("votes").textContent = state.votes
    ? text("votes", { votes: state.votes, needed: state.votesNeeded })
    : "";

  const list = $("upcoming");
  list.replaceChildren(...state.upcoming.map((song) => songRow(song, null)));
  $("nothingNext").hidden = state.upcoming.length > 0;
}

function showGuest(element, name) {
  element.hidden = !name;
  element.textContent = name ? text("addedBy", { name }) : "";
}

function songRow(song, onAdd) {
  const row = document.createElement("li");
  if (song.cover) {
    const cover = document.createElement("img");
    cover.src = song.cover;
    cover.alt = "";
    cover.loading = "lazy";
    row.append(cover);
  } else {
    const placeholder = document.createElement("div");
    placeholder.className = "cover-placeholder";
    placeholder.textContent = "♪";
    row.append(placeholder);
  }
  const words = document.createElement("div");
  words.className = "song-text";
  const title = document.createElement("div");
  title.textContent = song.title;
  const second = document.createElement("div");
  second.className = "dimmed";
  // Where the song is (the library, the server, a streaming service), as a label.
  if (song.badge && song.badge.text) {
    const badge = document.createElement("span");
    badge.className = `badge ${["library", "server", "stream"].includes(song.badge.kind) ? song.badge.kind : "server"}`;
    badge.textContent = song.badge.text;
    second.append(badge);
  }
  second.append([song.artist, song.album, song.note].filter(Boolean).join(" · "));
  words.append(title, second);
  if (song.guest) {
    const guest = document.createElement("div");
    guest.className = "guest";
    showGuest(guest, song.guest);
    words.append(guest);
  }
  row.append(words);
  if (onAdd) {
    const add = document.createElement("button");
    add.className = "pill accent";
    add.textContent = text("add") || "+";
    add.addEventListener("click", async () => {
      add.disabled = true;
      if (await onAdd()) {
        add.textContent = "✓";
      } else {
        add.disabled = false;
      }
    });
    row.append(add);
  }
  return row;
}

function toast(message) {
  if (!message) return;
  const element = $("toast");
  element.textContent = message;
  element.classList.add("shown");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("shown"), 2600);
}

// ----- searching -----

$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(search, SEARCH_DELAY_MS);
});

async function search() {
  const query = $("search").value.trim();
  const number = ++searchNumber;
  if (!query) {
    $("results").replaceChildren();
    $("noResults").hidden = true;
    return;
  }
  try {
    const answer = await fetch(`search?q=${encodeURIComponent(query)}`);
    const reply = await answer.json();
    if (number !== searchNumber) return; // a newer search is on its way
    if (reply.error) {
      toast(reply.error);
      return;
    }
    const results = reply.results || [];
    $("results").replaceChildren(
      ...results.map((song) => songRow(song, () => post("add", { id: song.id })))
    );
    $("noResults").hidden = results.length > 0;
  } catch (e) {
    toast(texts.lost || "…");
  }
}

// ----- voting and the name -----

function songKey(state) {
  return state ? `${state.title}\n${state.artist}\n${state.guest}` : "";
}

$("voteSkip").addEventListener("click", async () => {
  const key = songKey(lastState);
  if (await post("vote", {})) {
    votedFor = key;
    if (lastState) showState(lastState);
  }
});

function askName() {
  $("nameInput").value = guestName;
  $("nameDialog").hidden = false;
  $("nameInput").focus();
}

$("nameForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = $("nameInput").value.trim().replace(/\s+/g, " ").slice(0, MAX_NAME);
  if (!name) return;
  guestName = name;
  save("musichive-party-name", name);
  $("nameDialog").hidden = true;
});
$("changeName").addEventListener("click", askName);

if (!guestName) askName();
connect();
