// Shared helpers for pages that display live data over a websocket.
// See ui-style.md ("no buffering" over flaky wifi).

function connectWs(path, onMessage) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  connectWsUrl(`${proto}://${location.host}${path}`, onMessage);
}

// Same as connectWs, but takes a full ws(s):// URL — for a page that
// needs another service's websocket directly (e.g. aruco's Map page
// reading oxts-nav's /ws/nav), rather than always assuming same-origin.
function connectWsUrl(url, onMessage) {
  function connect() {
    const ws = new WebSocket(url);
    ws.onmessage = (event) => onMessage(JSON.parse(event.data));
    // Flaky wifi: just retry — never buffer/replay, always resume with
    // whatever is current when reconnected.
    ws.onclose = () => setTimeout(connect, 1000);
    ws.onerror = () => ws.close();
  }
  connect();
}

// Fills any element with id `${prefix}${key}` from data's entries.
// Keys with no matching element are simply ignored — a page only shows
// the fields it has a row for.
//
// A key present in an *earlier* call but missing from this one gets its
// element blanked back to "—", not left showing the old value — found
// live 2026-08-11: oxts-nav blanks its own feed to {} once the xNAV
// stops sending data (see nav_feed.py's "Staleness"), but this
// function only ever filled in keys it was given, so a page kept
// showing frozen, no-longer-true numbers with nothing to say they'd
// gone stale. `_fillFieldsSeen` remembers, per prefix, every element
// id this function has ever set, so it knows what to blank once a key
// stops showing up.
const _fillFieldsSeen = {};

function fillFields(prefix, data) {
  const seen = _fillFieldsSeen[prefix] || (_fillFieldsSeen[prefix] = new Set());
  const presentIds = new Set();
  for (const [key, value] of Object.entries(data)) {
    const id = prefix + key;
    presentIds.add(id);
    seen.add(id);
    const el = document.getElementById(id);
    if (!el) continue;
    if (typeof value === "number") {
      el.textContent = Number.isInteger(value) ? value : value.toFixed(3);
    } else {
      el.textContent = value;
    }
  }
  for (const id of seen) {
    if (presentIds.has(id)) continue;
    const el = document.getElementById(id);
    if (el) el.textContent = "—";
  }
}
