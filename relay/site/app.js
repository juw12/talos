/* Talos Relay, the phone page. It wears the look of Vector Relay and has its parts: more than one
   computer, a 4-digit code, photos, the board of all chats, search, version history.

   The link holds the channel and the key after the #, which the browser sends to no server:
       https://<relay>/#<channel>.<key>
   Every request to the computer is sealed here with AES-256-GCM (the browser's own WebCrypto) and
   left at the relay; the computer (talos/relay.py) picks it up, and leaves a sealed answer.

   Text that comes from the computer is only ever put on the page as text nodes, never as HTML:
   an answer with headings, lists, code and tables is built node by node (see markdown).
*/
"use strict";
(function () {
  var PROTOCOL = 1;
  var APP_VERSION = "2.2.0";
  var NEW = "";                         // the chat that does not exist yet
  var DEVICES = "talos-relay-devices", ACTIVE = "talos-relay-active", OLD_LINK = "talos-relay-link";
  var VIEW = "talos-relay-view", HINT = "talos-relay-hint";
  var BUSY_EVERY_MS = 2500, QUIET_EVERY_MS = 7000, BOARD_EVERY_MS = 20000, ALONE_EVERY_MS = 500;
  var HANG_MS = 15000, ANSWER_HOLD_S = 6, ANSWER_HANG_MS = 12000, ANSWER_WAIT_MS = 20000, PHOTO_WAIT_MS = 45000;
  var PHOTO_SIDE = 1400, PHOTO_CHARS = 400000;   // the computer opens about 150,000 characters a second
  var BOARD_ENDED = 12, REMOVE_ARMED_MS = 4000;
  var $ = function (id) { return document.getElementById(id); };
  // Over plain http (the relay inside Talos, on your own network) a browser offers no cipher: lite-crypto.js is used.
  var subtle = (window.crypto && crypto.subtle) || window.TalosLiteCrypto;
  var DIRECT = location.protocol === "http:";       // the page comes from the computer itself, not from the internet
  var PHONE = window.TalosPhone;                    // this phone alone, with no computer (phone.js)
  PHONE.version = APP_VERSION;
  function alone(device) { return !!device && device.c === PHONE.c; }

  // ------------------------------------------------------------ what this phone keeps
  var memory = {};
  var store = {
    getItem: function (key) {
      try { return localStorage.getItem(key); } catch (error) { return Object.prototype.hasOwnProperty.call(memory, key) ? memory[key] : null; }
    },
    setItem: function (key, value) {
      try { localStorage.setItem(key, value); } catch (error) { memory[key] = String(value); }
    },
    removeItem: function (key) {
      try { localStorage.removeItem(key); } catch (error) { delete memory[key]; }
    },
  };
  function readStore(key, fallback) {
    try {
      var found = JSON.parse(store.getItem(key) || "null");
      return found === null || found === undefined ? fallback : found;
    } catch (error) { return fallback; }
  }

  function toBase64(bytes) {
    var text = "";
    for (var at = 0; at < bytes.length; at += 8192) text += String.fromCharCode.apply(null, bytes.subarray(at, at + 8192));
    return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function fromBase64(text) {
    var plain = atob(String(text).replace(/-/g, "+").replace(/_/g, "/"));
    var bytes = new Uint8Array(plain.length);
    for (var at = 0; at < plain.length; at += 1) bytes[at] = plain.charCodeAt(at);
    return bytes;
  }
  function randomId() { return toBase64(crypto.getRandomValues(new Uint8Array(12))); }
  function pause(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  // ------------------------------------------------------------ the computers of this phone
  /* A device is one computer: {c: its channel at the relay, k: the key, name, named: you chose the name}. */
  function decodeTicket(hash) {
    var found = /^#?([A-Za-z0-9_-]{16,64})\.([A-Za-z0-9_-]{43})$/.exec(String(hash || ""));
    return found ? { c: found[1], k: found[2] } : null;
  }
  function ticketHash(device) { return "#" + device.c + "." + device.k; }

  var devices = readStore(DEVICES, []).filter(function (d) { return d && decodeTicket("#" + d.c + "." + d.k); });
  function deviceById(id) { return devices.filter(function (d) { return d.c === id; })[0] || null; }
  function keepDevices() { store.setItem(DEVICES, JSON.stringify(devices)); }
  function freeName(wanted, but) {
    var name = wanted, count = 2;
    while (devices.some(function (d) { return d !== but && d.name.toLowerCase() === name.toLowerCase(); })) name = wanted + " " + count++;
    return name;
  }
  function remember(ticket, alias) {
    if (!ticket) return null;
    var known = deviceById(ticket.c);
    if (known) {
      known.k = ticket.k;                            // the newest link of a computer is the one that works
      if (alias) { known.name = alias; known.named = true; }
    } else {
      known = { c: ticket.c, k: ticket.k, name: alias || freeName(alone(ticket) ? "This phone" : "Computer"), named: !!alias || alone(ticket) };
      devices.push(known);
    }
    keepDevices();
    return known;
  }

  (function bringOldLink() {                         // the page before 2.0.0 kept one link, under another name
    var old = readStore(OLD_LINK, null);
    if (old && old.channel && old.key && !deviceById(old.channel)) remember({ c: old.channel, k: old.key });
    if (old) store.removeItem(OLD_LINK);
  }());

  var given = decodeTicket(location.hash);
  var active = given ? remember(given) : deviceById(store.getItem(ACTIVE)) || devices[0] || null;
  if (active) {
    store.setItem(ACTIVE, active.c);
    // The address keeps the link: it is what an icon on the Home Screen opens.
    if (!given) history.replaceState(null, "", location.pathname + location.search + ticketHash(active));
  }

  // ------------------------------------------------------------ sealed questions and answers
  function trouble(kind) {
    var error = new Error(kind);
    error.kind = kind;
    return error;
  }

  /* fetch that gives up: a request that hangs must not stop the page from asking again. */
  function reach(url, options, ms) {
    var stopper = typeof AbortController === "function" ? new AbortController() : null;
    var timer = stopper ? setTimeout(function () { stopper.abort(); }, ms) : 0;
    var asked = Object.assign({ cache: "no-store" }, options || {}, stopper ? { signal: stopper.signal } : {});
    return fetch(url, asked).then(function (reply) {
      clearTimeout(timer);
      return reply;
    }, function () {
      clearTimeout(timer);
      throw trouble("offline");
    });
  }

  var opened = {};                                   // channel -> the key as WebCrypto holds it
  function keyOf(device) {
    var id = device.c + "." + device.k;
    if (!opened[id]) opened[id] = subtle.importKey("raw", fromBase64(device.k), "AES-GCM", false, ["encrypt", "decrypt"]);
    return opened[id];
  }
  function extra(device, kind, id) {
    return new TextEncoder().encode("talos-relay/" + PROTOCOL + "/" + device.c + "/" + kind + "/" + id);
  }
  async function seal(device, kind, id, data) {
    var nonce = crypto.getRandomValues(new Uint8Array(12));
    var sealed = new Uint8Array(await subtle.encrypt(
      { name: "AES-GCM", iv: nonce, additionalData: extra(device, kind, id) }, await keyOf(device),
      new TextEncoder().encode(JSON.stringify(data))));
    var whole = new Uint8Array(12 + sealed.length);
    whole.set(nonce);
    whole.set(sealed, 12);
    return toBase64(whole);
  }
  async function unseal(device, kind, id, text) {
    try {
      var whole = fromBase64(text.trim());
      var plain = await subtle.decrypt(
        { name: "AES-GCM", iv: whole.subarray(0, 12), additionalData: extra(device, kind, id) }, await keyOf(device), whole.subarray(12));
      return JSON.parse(new TextDecoder().decode(plain));
    } catch (error) {
      throw trouble("key");
    }
  }
  function roomOf(device) { return location.origin + "/c/" + device.c; }

  /* One request to a computer and its answer. Throws trouble("gone", "key", "offline", "relay" or "silent"). */
  async function call(op, fields, device, waitMs) {
    device = device || active;
    if (alone(device)) return PHONE.call(op, fields);   // no computer and no relay: the phone answers itself
    var id = randomId(), base = roomOf(device);
    var body = await seal(device, "ask", id, Object.assign({ op: op, t: Date.now() }, fields || {}));
    var left = await reach(base + "/ask/" + id, { method: "POST", body: body, headers: { "Content-Type": "text/plain" } }, HANG_MS + (body.length > 100000 ? 30000 : 0));
    if (left.status === 404) throw trouble("gone");
    if (left.status === 429) throw trouble("silent");
    if (!left.ok) throw trouble("relay");
    var until = Date.now() + (waitMs || ANSWER_WAIT_MS);
    while (Date.now() < until) {
      var asked = Date.now();
      var got = await reach(base + "/answer/" + id + "?wait=" + ANSWER_HOLD_S, {}, ANSWER_HANG_MS);
      if (got.status === 200) return unseal(device, "answer", id, await got.text());
      if (got.status !== 204 || Date.now() - asked < 1000) await pause(800);   // a relay that does not hold the request
    }
    throw trouble("silent");
  }

  /* What the relay keeps of a computer that does not answer: its name, when it was heard, its chats. */
  async function lastHeard(device) {
    if (alone(device)) return null;
    try {
      var got = await reach(roomOf(device) + "/state", {}, HANG_MS);
      if (got.status !== 200) return null;
      return await unseal(device, "state", "", await got.text());
    } catch (error) {
      return null;
    }
  }

  // ------------------------------------------------------------ what this phone knows
  var info = { pc: 0, computer: "", version: "", model: "", models: [], asksFirst: true, folders: [], chats: [] };
  var picked = {};                                   // chat id -> the model you chose for its next prompt
  var chatsData = {};                                // chat id -> the chat as the computer last sent it
  var selected = active ? store.getItem("talos-relay-chat:" + active.c) || NEW : NEW;
  var view = store.getItem(VIEW) === "board" ? "board" : "chat";
  var pending = [];                                  // prompts on their way, or that did not arrive
  var pictures = {};                                 // "chat/seq" -> {state, url}
  var seen = active ? readStore("talos-relay-seen:" + active.c, null) : null;
  var unread = {};
  var problem = "", dead = false, silent = 0, gone = "";
  var clockOffset = 0;                               // seconds the computer's clock is ahead of this phone's
  var turn = 0, looking = false, again = false, lookTimer = 0;
  var folder = active ? store.getItem("talos-relay-folder:" + active.c) || "" : "";
  var photo = null;
  var boards = {};                                   // channel -> {rows, pc, why}
  var boardTimer = 0, openRow = "", olderRows = false, onlyWhere = "", removing = {};
  var shownPast = "", shownTail = "", listed = "", boardShown = "";
  var openSteps = {};

  function chatRow(id) { return info.chats.filter(function (c) { return c.id === id; })[0] || null; }

  function select(id) {
    turn += 1;
    selected = id;
    gone = "";
    if (active) store.setItem("talos-relay-chat:" + active.c, id);
    if (id !== NEW && chatRow(id)) markSeen(chatRow(id));
    shownPast = shownTail = listed = "";
    openSteps = {};
    render();
    $("feed").scrollTop = $("feed").scrollHeight;
    look();
  }

  function markSeen(row) {
    if (!seen) seen = {};
    seen[row.id] = row.updated;
    delete unread[row.id];
    store.setItem("talos-relay-seen:" + active.c, JSON.stringify(seen));
  }

  function merge(answer, mine) {
    info.pc = answer.pc || Math.floor(Date.now() / 1000);
    clockOffset = info.pc - Date.now() / 1000;
    info.computer = answer.computer || "";
    info.version = answer.version || "";
    info.model = answer.model || "";
    info.models = Array.isArray(answer.models) ? answer.models.map(String) : [];
    info.asksFirst = answer.asks_first !== false;
    info.folders = Array.isArray(answer.folders) ? answer.folders : [];
    info.chats = Array.isArray(answer.chats) ? answer.chats : [];
    boards[active.c] = { rows: info.chats, pc: info.pc, why: "" };
    if (!active.named && info.computer && active.name !== info.computer) {
      active.name = freeName(info.computer, active);
      keepDevices();
      deviceLabels();
    }
    var first = !seen;
    if (first) seen = {};
    info.chats.forEach(function (row) {
      var open = row.id === selected && view === "chat" && !document.hidden;
      if (first || open) seen[row.id] = row.updated;
      if (seen[row.id] === undefined || row.updated > seen[row.id]) unread[row.id] = true; else delete unread[row.id];
    });
    store.setItem("talos-relay-seen:" + active.c, JSON.stringify(seen));
    if (mine !== turn) return;                       // the answer is for a chat this page has left
    if (answer.chat) {
      chatsData[answer.chat.id] = answer.chat;
      pending = pending.filter(function (p) {        // a prompt that shows in the chat is no longer on its way
        return !(p.state === "sent" && p.chat === answer.chat.id && answer.chat.prompts > p.base);
      });
    } else if (answer.gone && answer.gone === selected) {
      gone = selected;
      delete chatsData[selected];
    }
  }

  // ------------------------------------------------------------ words on the page
  function two(number) { return (number < 10 ? "0" : "") + number; }
  function clock(seconds) { var d = new Date(seconds * 1000); return two(d.getHours()) + ":" + two(d.getMinutes()); }
  function day(seconds) {
    var d = new Date(seconds * 1000), today = new Date();
    if (d.toDateString() === today.toDateString()) return "Today";
    return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  }
  function moment(seconds) { return (day(seconds) === "Today" ? "" : day(seconds) + " ") + clock(seconds); }
  /* "now", "4m", "2h", "3d": how long ago, the way the chat list says it. */
  function since(seconds) {
    var passed = Math.max(0, Date.now() / 1000 + clockOffset - seconds);
    if (passed < 60) return "now";
    if (passed < 3600) return Math.floor(passed / 60) + "m";
    if (passed < 86400) return Math.floor(passed / 3600) + "h";
    return Math.floor(passed / 86400) + "d";
  }
  /* "for 4 min", "for 1 h 05": how long a chat has worked so far. */
  function lasted(start) {
    var minutes = Math.max(0, Math.floor((Date.now() / 1000 + clockOffset - start) / 60));
    if (minutes < 1) return "for under a minute";
    return minutes < 60 ? "for " + minutes + " min" : "for " + Math.floor(minutes / 60) + " h " + two(minutes % 60);
  }

  function element(tag, name, text) {
    var made = document.createElement(tag);
    if (name) made.className = name;
    if (text !== undefined) made.textContent = text;
    return made;
  }
  function button(name, text, action) {
    var made = element("button", name, text);
    made.type = "button";
    if (action) made.addEventListener("click", action);
    return made;
  }

  /* `code`, **bold** and [a link](address) inside one line, as nodes. */
  function inline(parent, text) {
    var parts = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\([^)\s]+\))/g, at = 0, found;
    while ((found = parts.exec(text))) {
      if (found.index > at) parent.appendChild(document.createTextNode(text.slice(at, found.index)));
      var piece = found[0];
      if (found[1]) parent.appendChild(element("code", "", piece.slice(1, -1)));
      else if (found[2]) parent.appendChild(element("strong", "", piece.slice(2, -2)));
      else {
        var cut = piece.indexOf("]("), words = piece.slice(1, cut), address = piece.slice(cut + 2, -1);
        if (/^https?:\/\//i.test(address)) {
          var link = element("a", "", words);
          link.href = address;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          parent.appendChild(link);
        } else parent.appendChild(document.createTextNode(words + " (" + address + ")"));
      }
      at = found.index + piece.length;
    }
    if (at < text.length) parent.appendChild(document.createTextNode(text.slice(at)));
    return parent;
  }

  function cells(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function (cell) { return cell.trim(); });
  }

  /* An answer of Talos as nodes: headings, lists, code blocks, tables and paragraphs. */
  function markdown(text) {
    var box = element("div", "words"), lines = String(text).replace(/\r/g, "").split("\n"), at = 0;
    var words = [], list = null;
    function flush() {
      if (words.length) box.appendChild(inline(element("p"), words.join("\n")));
      words = [];
      list = null;
    }
    while (at < lines.length) {
      var line = lines[at], found;
      if (/^\s*```/.test(line)) {
        flush();
        var code = [];
        for (at += 1; at < lines.length && !/^\s*```/.test(lines[at]); at += 1) code.push(lines[at]);
        at += 1;
        box.appendChild(element("pre")).appendChild(element("code", "", code.join("\n")));
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && at + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[at + 1])) {
        flush();
        var table = element("table"), head = table.appendChild(element("tr"));
        cells(line).forEach(function (cell) { inline(head.appendChild(element("th")), cell); });
        for (at += 2; at < lines.length && /^\s*\|.*\|\s*$/.test(lines[at]); at += 1) {
          var row = table.appendChild(element("tr"));
          cells(lines[at]).forEach(function (cell) { inline(row.appendChild(element("td")), cell); });
        }
        box.appendChild(element("div", "scroll")).appendChild(table);
        continue;
      }
      if ((found = /^\s{0,3}#{1,6}\s+(.*)$/.exec(line))) {
        flush();
        inline(box.appendChild(element("h3")), found[1].replace(/\s+#+\s*$/, ""));
      } else if ((found = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(line))) {
        var kind = /\d/.test(found[1]) ? "ol" : "ul";
        if (words.length) flush();
        if (!list || list.tagName.toLowerCase() !== kind) list = box.appendChild(element(kind));
        inline(list.appendChild(element("li")), found[2]);
      } else if (!line.trim()) {
        flush();
      } else if (list && /^\s{2,}\S/.test(line) && list.lastChild) {
        inline(list.lastChild, " " + line.trim());   // a list item that goes on in the next line
      } else {
        if (list) list = null;
        words.push(line);
      }
      at += 1;
    }
    flush();
    return box;
  }

  // ------------------------------------------------------------ the top line
  /* [dot, words, may a prompt be sent] for what this page shows. */
  function status() {
    var where = active ? active.name : "your computer";
    var own = selected === NEW ? null : chatsData[selected], row = chatRow(selected);
    if (selected === NEW) {
      if (problem) return ["bad", problem, !dead && !!info.pc];
      if (!info.pc) return ["", "Asking " + where, false];
      return ["ok", "New chat on " + where + (wantedModel() || info.model ? " \u00b7 " + (wantedModel() || info.model) : ""), true];
    }
    if (problem) return ["bad", problem, !dead && !!own && !own.running];
    if (gone) return ["bad", "That chat is no longer on " + where + ". Pick another one from the chat list.", false];
    if (!own) return ["", info.pc ? "Opening the chat" : "Asking " + where, false];
    var live = own.live;
    if (own.running && live && live.ask) return ["busy", "Talos asks you something before it goes on", false];
    if (own.running) return ["busy", "Talos is working" + (live ? " \u00b7 " + lasted(live.since) : ""), false];
    if (live && live.done && live.done.stopped) return ["bad", "Stopped: " + live.done.stopped + " \u00b7 send a prompt any time", true];
    return ["ok", (row && row.updated ? "Finished " + moment(row.updated) + " \u00b7 " : "") + "send a prompt any time", true];
  }

  // ------------------------------------------------------------ one chat
  function pictureKey(at) { return selected + "/" + at; }

  function picture(event) {
    var figure = element("figure", "pic");
    var kept = event.local ? { state: "ready", url: event.local } : pictures[pictureKey(event.img.at)];
    if (kept && kept.state === "ready") {
      var image = element("img");
      image.alt = "Photo sent with this prompt";
      image.src = kept.url;
      figure.appendChild(image);
      return figure;
    }
    var failed = kept && kept.state === "failed";
    var wait = element(failed ? "button" : "div", "wait", failed ? "The picture did not come. Try again" : "Getting the picture");
    wait.style.height = "160px";
    wait.style.width = "220px";
    if (failed) {
      wait.type = "button";
      wait.addEventListener("click", function () { delete pictures[pictureKey(event.img.at)]; shownPast = ""; render(); });
    }
    figure.appendChild(wait);
    return figure;
  }

  function promptBubble(event) {
    var item = element("article", "item prompt" + (event.state === "failed" ? " failed" : ""));
    var text = element("div", "text");
    if (event.img || event.local) text.appendChild(picture(event));
    if (event.text && !((event.img || event.local) && event.text === "(photo)")) text.appendChild(element("div", "words", event.text));
    item.appendChild(text);
    var meta = element("div", "meta", event.t ? clock(event.t) : "");
    if (event.state === "sending") meta.textContent += " \u00b7 sending";
    else if (event.state === "sent") meta.textContent += " \u00b7 picked up";
    else if (event.state === "failed") {
      meta.textContent = (event.why || "Not sent.") + " ";
      meta.appendChild(button("retry", "Try again", function () { deliver(event.prompt); }));
      meta.appendChild(document.createTextNode(" "));
      meta.appendChild(button("drop", "Remove", function () {
        pending = pending.filter(function (p) { return p !== event.prompt; });
        render();
      }));
    }
    item.appendChild(meta);
    return item;
  }

  function stepLine(event, at) {
    var item = element("article", "item update" + (event.result ? " has-result" : "") + (openSteps[at] ? " open" : ""));
    var text = element("div", "text");
    text.appendChild(element("span", "name", event.name || "step"));
    if (event.target) text.appendChild(document.createTextNode("  " + event.target));
    if (event.result) {
      text.appendChild(element("div", "result", event.result));
      item.addEventListener("click", function () {
        openSteps[at] = !openSteps[at];
        item.classList.toggle("open", openSteps[at]);
      });
    }
    item.appendChild(text);
    if (event.t) item.appendChild(element("div", "meta", clock(event.t)));
    return item;
  }

  /* One line of an answer for a list: the words without the marks of its headings, tables and code. */
  function plainWords(words) {
    return String(words).replace(/```\w*/g, " ").replace(/^\s*#{1,6}\s+/gm, "").replace(/\|[\s:|-]*\|/g, " ")
      .replace(/[*`|]+/g, " ").replace(/\s+/g, " ").trim();
  }

  function answerBubble(event) {
    var item = element("article", "item result");
    var text = element("div", "text");
    text.appendChild(markdown(event.text));
    item.appendChild(text);
    if (event.t) item.appendChild(element("div", "meta", clock(event.t)));
    return item;
  }

  /* The card of a chat that is working. It is made once and then only its words change, so the
     Stop button is never taken away from under a finger. */
  var work = null;
  function workCard(live, listed) {
    if (!work) {
      work = { card: element("article", "item work"), head: element("div", "work-head"), words: element("span", "grow"),
               steps: element("ol", "work-steps"), text: element("p", "work-text"), note: element("p", "work-note"), mark: "" };
      work.stop = button("work-stop", "Stop", stop);
      work.head.appendChild(element("span", "spin"));
      work.head.appendChild(work.words);
      work.head.appendChild(work.stop);
      [work.head, work.steps, work.text, work.note].forEach(function (part) { work.card.appendChild(part); });
    }
    work.words.textContent = "Talos is working " + lasted(live.since);
    var mark = JSON.stringify(live.steps || []);
    if (mark !== work.mark) {
      work.mark = mark;
      work.steps.textContent = "";
      (live.steps || []).forEach(function (step, at, all) {
        var line = element("li", at === all.length - 1 ? "now" : "");
        line.appendChild(element("span", "at", clock(step[0])));
        line.appendChild(element("span", "", String(step[1])));
        work.steps.appendChild(line);
      });
    }
    work.steps.hidden = listed || !(live.steps || []).length;   // steps already in the chat are not said twice
    work.text.textContent = live.text || "";
    work.text.hidden = !live.text;
    work.note.textContent = (live.notes || []).join("\n");
    work.note.hidden = !(live.notes || []).length;
    return work.card;
  }

  /* Talos asks before a step, or has a question. The card stays the same node while the question is open. */
  var asking = { id: "", card: null };
  function askCard(ask) {
    if (asking.id === ask.id) return asking.card;
    var card = element("article", "item ask"), choices = element("div", "choices");
    function choice(label, answer, name) {
      choices.appendChild(button("more" + (name ? " " + name : ""), label, function () { reply(ask.id, answer); }));
    }
    if (ask.kind === "permission") {
      card.appendChild(element("p", "what", "Talos asks to use: " + (ask.tool || "a tool")));
      if (ask.target) card.appendChild(element("div", "target", String(ask.target)));
      if (ask.reason) card.appendChild(element("p", "why", String(ask.reason)));
      choice("Allow once", "once", "primary");
      choice("Always allow", "always");
      choice("Refuse", "reject", "danger");
    } else {
      card.appendChild(element("p", "what", String(ask.question || "Talos has a question")));
      (ask.options || []).forEach(function (option) { choice(String(option), String(option)); });
      var input = element("input");
      input.placeholder = "Or type an answer";
      input.setAttribute("aria-label", "Your answer");
      card.appendChild(input);
      choices.appendChild(button("more primary", "Answer", function () {
        if (input.value.trim()) reply(ask.id, input.value.trim());
      }));
    }
    card.appendChild(choices);
    asking = { id: ask.id, card: card };
    return card;
  }

  var parts = null;                                  // the feed in four parts, each made again only when it changed
  function feedParts() {
    if (parts) return parts;
    var feed = $("feed");
    feed.textContent = "";
    parts = { past: element("div"), tail: element("div"), now: element("div"), ask: element("div") };
    [parts.past, parts.tail, parts.now, parts.ask].forEach(function (part) { feed.appendChild(part); });
    return parts;
  }

  function renderChat() {
    var own = selected === NEW ? null : chatsData[selected];
    var items = own ? own.items || [] : [];
    var waiting = pending.filter(function (p) { return p.chat === selected; });
    var live = own && own.live, running = !!(own && own.running && live);
    var done = own && !own.running && live && live.done ? live.done : null;
    var feed = $("feed"), made = feedParts();
    var atEnd = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;

    var mark = JSON.stringify([selected, own ? own.earlier : 0, !!info.pc, waiting.length, items.map(function (item) {
      return [item.role, item.t, (item.text || "").length, item.name, item.target, (item.result || "").length,
        item.img ? (pictures[pictureKey(item.img.at)] || {}).state || "none" : ""];
    })]);
    if (mark !== shownPast) {
      shownPast = mark;
      made.past.textContent = "";
      if (!items.length && !waiting.length) {
        made.past.appendChild(element("p", "empty", selected === NEW && alone(active)
          ? "Type a question. The model below the prompt box answers from this phone: no computer is needed. It only chats here. Reading files and running steps need Talos on a computer (Device, + Add device)."
          : selected === NEW
          ? "Type what the new chat should do. Talos on " + (active ? active.name : "your computer") + " starts it, and it joins the chat list. Its steps show here while it works."
            + (info.folders.length > 1 ? " Pick the folder it works in above the prompt box." : "")
          : own ? "Nothing yet. What the chat says will show here." : ""));
      }
      if (own && own.earlier) made.past.appendChild(element("p", "item earlier", own.earlier + " earlier parts of this chat are not shown here."));
      var lastDay = "";
      items.forEach(function (item, at) {
        if (item.t) {
          var label = day(item.t);
          if (label !== lastDay) { made.past.appendChild(element("p", "day", label)); lastDay = label; }
        }
        made.past.appendChild(item.role === "you" ? promptBubble(item) : item.role === "step" ? stepLine(item, at) : answerBubble(item));
      });
    }

    mark = JSON.stringify([selected, done, waiting.map(function (p) { return [p.pid, p.state, p.why]; })]);
    if (mark !== shownTail) {
      shownTail = mark;
      made.tail.textContent = "";
      if (done && done.proof) made.tail.appendChild(element("article", "item note", done.proof));
      if (done && done.stopped) {
        var stopped = element("article", "item error");
        stopped.appendChild(element("div", "text", "Stopped: " + done.stopped + (done.text ? "\n" + done.text : "")));
        made.tail.appendChild(stopped);
      }
      waiting.forEach(function (p) {
        made.tail.appendChild(promptBubble({ text: p.text, t: p.t, state: p.state, why: p.why, local: p.url || "", prompt: p }));
      });
    }

    var turnStart = items.map(function (item) { return item.role; }).lastIndexOf("you");
    var listed = items.slice(turnStart + 1).some(function (item) { return item.role === "step"; });
    var card = running ? workCard(live, listed) : null;
    if (made.now.firstChild !== card) made.now.replaceChildren.apply(made.now, card ? [card] : []);
    var question = running && live.ask ? askCard(live.ask) : null;
    if (!question) asking = { id: "", card: null };
    if (made.ask.firstChild !== question) made.ask.replaceChildren.apply(made.ask, question ? [question] : []);

    if (atEnd) feed.scrollTop = feed.scrollHeight;
    getPictures();
  }

  /* Pictures come one at a time, newest first, each as its own sealed answer. */
  var loading = false;
  async function getPictures() {
    var own = chatsData[selected];
    if (loading || problem || !own) return;
    var wanted = (own.items || []).filter(function (item) { return item.img && !pictures[pictureKey(item.img.at)]; }).pop();
    if (!wanted) return;
    var key = pictureKey(wanted.img.at), chat = selected;
    loading = true;
    pictures[key] = { state: "loading" };
    try {
      var answer = await call("image", { session: chat, at: wanted.img.at }, active, PHOTO_WAIT_MS);
      var fine = answer.ok && /^image\/(jpeg|png|webp|gif)$/.test(answer.kind || "") && /^[A-Za-z0-9+\/=]+$/.test(answer.data || "");
      pictures[key] = fine ? { state: "ready", url: "data:" + answer.kind + ";base64," + answer.data } : { state: "failed" };
    } catch (error) {
      pictures[key] = { state: "failed" };
    }
    loading = false;
    render();
  }

  // ------------------------------------------------------------ the chat list at the side
  function folderName(path) {
    var names = String(path).split(/[\\\/]+/).filter(Boolean);
    return names.length ? names[names.length - 1] : String(path);
  }

  /* New chat: one button for each folder Talos has worked in, the chosen one marked. */
  function folderPicker() {
    var row = $("agents"), show = selected === NEW && view !== "board" && info.folders.length > 1;
    if (info.folders.indexOf(folder) < 0) folder = info.folders[0] || "";
    var mark = show ? JSON.stringify([info.folders, folder]) : "";
    row.hidden = !show;
    if (row.getAttribute("data-mark") === mark) return;
    row.setAttribute("data-mark", mark);
    row.textContent = "";
    if (!show) return;
    info.folders.forEach(function (path) {
      var pick = button("agent" + (path === folder ? " chosen" : ""), folderName(path), function () {
        folder = path;
        store.setItem("talos-relay-folder:" + active.c, folder);
        render();
        $("box").focus();
      });
      pick.title = path;
      pick.setAttribute("aria-pressed", path === folder ? "true" : "false");
      row.appendChild(pick);
    });
  }

  /* The model the next prompt of this chat goes to: the one you picked here, else the one the chat
     already has, else the one you picked last on this device, else what the device uses by itself. */
  function wantedModel() {
    if (!info.models.length) return "";              // an older Talos does not say which models it has
    var own = selected === NEW ? null : chatsData[selected];
    var kept = active ? store.getItem("talos-relay-model:" + active.c) || "" : "";
    var order = [picked[selected], own && own.model, selected === NEW ? kept : "", info.model, info.models[0]];
    return order.filter(function (name) { return name && info.models.indexOf(name) >= 0; })[0] || "";
  }

  function modelPicker() {
    var picker = $("model"), wanted = wantedModel(), show = view !== "board" && info.models.length > 0;
    picker.hidden = !show;
    var mark = show ? JSON.stringify(info.models) : "";
    if (picker.getAttribute("data-mark") !== mark) {   // the list is not made again under a finger
      picker.setAttribute("data-mark", mark);
      picker.replaceChildren();
      info.models.forEach(function (name) {
        var option = element("option", "", name);
        option.value = name;
        picker.appendChild(option);
      });
    }
    if (show && document.activeElement !== picker && picker.value !== wanted) picker.value = wanted;
  }

  function runningCount() {
    return boardItems().filter(function (item) { return item.row.running; }).length;
  }

  function chips() {
    var bar = $("chats"), wanted = $("find").value.trim().toLowerCase();
    var found = info.chats.filter(function (row) {
      return !wanted || String(row.title).toLowerCase().indexOf(wanted) >= 0 || String(row.folder || "").toLowerCase().indexOf(wanted) >= 0;
    });
    $("menuNew").hidden = !Object.keys(unread).some(function (id) { return unread[id] && id !== selected; });
    $("noChats").hidden = found.length > 0;
    $("noChats").textContent = info.chats.length ? "No chat with that name." : info.pc ? "No chat yet. Start one with New chat." : "";
    $("about").textContent = !info.pc ? "" : alone(active)
      ? "Talos on this phone alone, page " + info.version + ". No computer is needed. Chats and keys stay in this browser, and a prompt goes straight to the service of its model. It only chats: files and steps need Talos on a computer."
      : "Talos " + info.version + " on " + info.computer + ". Model: " + (info.model || "none set") + ". "
      + (info.asksFirst ? "Before a step that can destroy work or leaves its folder, it asks here." : "It asks nobody before any step.");
    var running = runningCount();
    var mark = JSON.stringify([selected, view, running, unread, found.map(function (row) { return [row.id, row.title, since(row.updated), row.running, row.waits]; })]);
    if (mark === listed) return;                     // rows are not made again under a finger
    listed = mark;
    $("showBoard").classList.toggle("chosen", view === "board");
    $("showBoard").setAttribute("aria-pressed", view === "board" ? "true" : "false");
    $("boardCount").textContent = running ? running + " running" : "";
    bar.textContent = "";
    found.forEach(function (row) {
      var chosen = row.id === selected && view !== "board";
      var chip = button("chip" + (chosen ? " chosen" : ""), undefined, function () { setView("chat"); select(row.id); side(false); });
      chip.setAttribute("aria-pressed", chosen ? "true" : "false");
      chip.appendChild(element("span", "name", row.title));
      if (row.running) {
        var spin = element("span", "spin");
        spin.title = row.waits ? "Asks you something" : "Working";
        chip.appendChild(spin);
      }
      if (unread[row.id] && !chosen) {
        var fresh = element("span", "new");
        fresh.title = "Something new";
        chip.appendChild(fresh);
      }
      chip.appendChild(element("span", "when", since(row.updated)));
      bar.appendChild(chip);
    });
  }

  /* The chat list slides in over the feed on a phone; on a wide screen it is always there. */
  function side(open) {
    document.body.classList.toggle("side-open", open);
    $("shade").hidden = !open;
    $("menu").setAttribute("aria-expanded", open ? "true" : "false");
    if (open) chips();
  }

  function render() {
    if (!active) return;
    if (view === "board") { chips(); renderBoard(); return; }
    var found = status(), own = selected === NEW ? null : chatsData[selected], row = chatRow(selected);
    $("dot").className = "dot " + found[0];
    $("status").textContent = found[1];
    var title = selected === NEW ? "New chat" : own ? own.title : row ? row.title : "Talos Relay";
    $("title").textContent = title;
    document.title = title;
    $("send").disabled = !found[2];
    $("box").placeholder = selected === NEW ? "What should the new chat do?"
      : own && own.running ? "Talos is working. Stop it first." : found[2] ? "Send follow-up" : "No chat to send to";
    $("newChat").classList.toggle("chosen", selected === NEW && view !== "board");
    folderPicker();
    modelPicker();
    chips();
    renderChat();
  }

  // ------------------------------------------------------------ All chats: the board of every computer
  function boardItems() {
    var found = [];
    devices.forEach(function (device) {
      var board = boards[device.c];
      if (!board) return;
      (board.rows || []).forEach(function (row) {
        if (row && typeof row.id === "string") found.push({ key: device.c + "|" + row.id, row: row, device: device, where: device.name });
      });
    });
    return found.sort(function (a, b) { return (b.row.running ? 1 : 0) - (a.row.running ? 1 : 0) || b.row.updated - a.row.updated; });
  }

  function stateWords(item) {
    var row = item.row;
    if (row.running && row.waits) return "asks you something";
    if (row.running) return "working";
    return "ended " + moment(row.updated);
  }

  function openChat(item) {
    store.setItem("talos-relay-chat:" + item.device.c, item.row.id);
    if (item.device === active) { setView("chat"); select(item.row.id); return; }
    store.setItem(VIEW, "chat");                     // the page starts again on that computer, with this chat open
    if (!switchDevice(item.device)) store.setItem(VIEW, "board");
  }

  async function removeRow(item) {
    removing[item.key] = { state: "sending" };
    boardShown = "";
    renderBoard();
    try {
      var answer = await call("hide", { session: item.row.id }, item.device);
      if (!answer.ok) throw Object.assign(new Error("refused"), { why: answer.error || "the computer refused" });
      delete removing[item.key];
      if (boards[item.device.c]) boards[item.device.c].rows = answer.chats || [];
      if (item.device === active) {
        info.chats = answer.chats || [];
        if (selected === item.row.id) { selected = NEW; store.setItem("talos-relay-chat:" + active.c, NEW); }
      }
      openRow = "";
    } catch (error) {
      removing[item.key] = { state: "failed", why: error.why || "the computer did not answer" };
    }
    boardShown = listed = "";
    render();
  }

  function boardRow(item) {
    var row = item.row, steps = Array.isArray(row.steps) ? row.steps : [], open = openRow === item.key;
    var made = element("article", "row " + (row.running ? "live" : "done") + (row.running && row.waits ? " waits" : "") + (open ? " open" : ""));
    var head = button("row-head", undefined, function () { openRow = open ? "" : item.key; boardShown = ""; renderBoard(); });
    head.setAttribute("aria-expanded", open ? "true" : "false");
    head.appendChild(element("span", "mark"));
    head.appendChild(element("span", "row-title", String(row.title || "Chat " + row.id)));
    head.appendChild(element("span", "when", since(row.updated)));
    made.appendChild(head);
    var meta = element("p", "row-meta");
    [item.where, row.folder].forEach(function (part) { if (part) meta.appendChild(document.createTextNode(String(part) + " \u00b7 ")); });
    meta.appendChild(element("span", "state", stateWords(item)));
    made.appendChild(meta);
    if (!open) {
      if (row.step) made.appendChild(element("p", "row-step", plainWords(row.step)));
      return made;
    }
    var more = element("div", "row-more"), list = element("ol");
    steps.forEach(function (step) {
      var line = element("li");
      line.appendChild(element("time", "", moment(step[0])));
      line.appendChild(element("span", "", String(step[1])));
      list.appendChild(line);
    });
    if (steps.length) more.appendChild(list);
    else if (row.step) more.appendChild(element("p", "row-step", plainWords(row.step)));
    more.appendChild(element("p", "times", "Started " + moment(row.started) + (row.running && !steps.length ? ". It has taken no step yet." : "")));
    more.appendChild(button("open-chat", "Open this chat", function () { openChat(item); }));
    var mark = removing[item.key] || {};
    var drop = button("remove-row" + (mark.state === "armed" ? " armed" : ""),
      mark.state === "armed" ? "Tap again to remove" : mark.state === "sending" ? "Removing\u2026" : "Remove from the list", function () {
        if (mark.state === "armed") { removeRow(item); return; }
        removing[item.key] = { state: "armed" };     // one tap is easy to make by mistake: the second one removes
        setTimeout(function () {
          if (removing[item.key] && removing[item.key].state === "armed") {
            delete removing[item.key];
            boardShown = "";
            if (view === "board") renderBoard();
          }
        }, REMOVE_ARMED_MS);
        boardShown = "";
        renderBoard();
      });
    drop.disabled = mark.state === "sending";
    more.appendChild(drop);
    if (mark.state === "armed") more.appendChild(element("p", "remove-note", "The chat leaves this list. It stays on the computer: talos sessions shows it."));
    if (mark.state === "failed") more.appendChild(element("p", "remove-note bad", "Not removed: " + mark.why));
    made.appendChild(more);
    return made;
  }

  function renderBoard() {
    var every = boardItems(), names = [];
    every.forEach(function (item) { if (names.indexOf(item.where) < 0) names.push(item.where); });
    if (names.indexOf(onlyWhere) < 0) onlyWhere = "";
    var items = every.filter(function (item) { return !onlyWhere || item.where === onlyWhere; });
    var running = items.filter(function (item) { return item.row.running; });
    var ended = items.filter(function (item) { return !item.row.running; });
    var heard = devices.filter(function (device) { return boards[device.c]; });
    var quiet = heard.filter(function (device) { return boards[device.c].why; });
    $("dot").className = "dot " + (!heard.length ? "" : quiet.length ? "bad" : "ok");
    $("status").textContent = !heard.length ? "Asking the relay" :
      running.length + " running" + (names.length > 1 && !onlyWhere ? " on " + names.length + " computers" : onlyWhere ? " on " + onlyWhere : "") +
      (quiet.length ? " \u00b7 " + quiet.length + " not reached" : "");
    $("title").textContent = "All chats";
    document.title = "All chats";
    var mark = JSON.stringify([openRow, olderRows, onlyWhere, removing, devices.map(function (device) {
      var board = boards[device.c];
      return [device.name, board ? [board.why, board.pc ? since(board.pc) : ""] : null];
    }), items.map(function (item) { return [item.key, item.where, item.row.updated, item.row.running, item.row.waits, since(item.row.updated), item.row.step, (item.row.steps || []).length]; })]);
    if (mark === boardShown) return;                 // rows are not made again under a finger
    boardShown = mark;
    var place = element("div", "board-in");
    if (names.length > 1) {
      var only = element("div", "only");
      [""].concat(names).forEach(function (name) {
        var pick = button("", name || "All computers", function () { onlyWhere = name; olderRows = false; renderBoard(); });
        pick.setAttribute("aria-pressed", name === onlyWhere ? "true" : "false");
        only.appendChild(pick);
      });
      place.appendChild(only);
    }
    var title = element("h2", "group", "Running now ");
    title.appendChild(element("span", "", String(running.length)));
    place.appendChild(title);
    if (!running.length) place.appendChild(element("p", "empty", heard.length ? "No chat is running." : ""));
    running.forEach(function (item) { place.appendChild(boardRow(item)); });
    if (ended.length) {
      title = element("h2", "group", "Ended ");
      title.appendChild(element("span", "", String(ended.length)));
      place.appendChild(title);
      ended.slice(0, olderRows ? ended.length : BOARD_ENDED).forEach(function (item) { place.appendChild(boardRow(item)); });
      if (!olderRows && ended.length > BOARD_ENDED) {
        place.appendChild(button("older", "Show " + (ended.length - BOARD_ENDED) + " older", function () { olderRows = true; renderBoard(); }));
      }
    }
    place.appendChild(element("h2", "group", "Computers"));
    devices.forEach(function (device) {
      var board = boards[device.c], line = element("p", "computer" + (board && board.why ? " bad" : ""));
      line.appendChild(element("strong", "", device.name));
      var said = !board ? " \u00b7 asking" : board.why ? " \u00b7 " + board.why + (board.pc ? " (last heard " + since(board.pc) + " ago)" : "") :
        " \u00b7 heard " + (since(board.pc) === "now" ? "just now" : since(board.pc) + " ago");
      line.appendChild(element("span", "", said));
      place.appendChild(line);
    });
    place.appendChild(element("p", "how", "Every Talos chat of a linked computer is listed, the running ones first. Remove takes a chat off this list; it stays on the computer."));
    var board = $("board"), top = board.scrollTop;
    board.replaceChildren(place);
    board.scrollTop = top;
  }

  /* The other computers of this phone: asked now and then, and from the relay's copy when one is silent. */
  async function lookBoards() {
    clearTimeout(boardTimer);
    await Promise.all(devices.filter(function (device) { return device !== active; }).map(async function (device) {
      try {
        var answer = await call("look", { session: "" }, device, 12000);
        boards[device.c] = { rows: answer.chats || [], pc: answer.pc || Math.floor(Date.now() / 1000), why: "" };
        if (!device.named && answer.computer && device.name !== answer.computer) { device.name = freeName(answer.computer, device); keepDevices(); deviceLabels(); }
      } catch (error) {
        var kept = error.kind === "silent" ? await lastHeard(device) : null;
        boards[device.c] = { rows: kept && kept.chats || (boards[device.c] || {}).rows || [], pc: kept && kept.pc || (boards[device.c] || {}).pc || 0,
                             why: error.kind === "silent" ? "does not answer" : error.kind === "offline" ? "this phone has no connection" : "its link no longer works" };
      }
    }));
    listed = "";
    if (view === "board") { renderBoard(); chips(); boardTimer = setTimeout(lookBoards, BOARD_EVERY_MS); } else chips();
  }

  function setView(next) {
    if (view === next) return;
    view = next;
    store.setItem(VIEW, next);
    document.body.classList.toggle("on-board", next === "board");
    shownPast = shownTail = boardShown = listed = "";
    if (next === "board") lookBoards(); else clearTimeout(boardTimer);
    render();
    if (next === "chat") $("feed").scrollTop = $("feed").scrollHeight;
  }

  // ------------------------------------------------------------ talking to the computer
  var WHY = {
    gone: "The relay no longer knows this link. On the computer, run: talos relay pair  and scan the new code.",
    key: "This link does not have the key of the computer. On the computer, run: talos relay pair  and scan the code again.",
    offline: DIRECT ? "This phone cannot reach the computer. It must be on the same Wi-Fi. It tries again by itself."
        : "This phone cannot reach the relay. It tries again by itself.",
    relay: "The relay did not take the request. It tries again by itself.",
  };

  async function explain(error) {
    var kind = error && error.kind || "relay";
    if (kind !== "silent") {
      dead = kind === "gone" || kind === "key";
      problem = WHY[kind] || WHY.relay;
      if (boards[active.c]) boards[active.c].why = dead ? "its link no longer works" : "not reached";
      return;
    }
    silent += 1;
    if (silent < 2) return;                          // one missed answer is not yet news
    var kept = await lastHeard(active);
    problem = active.name + " does not answer. " + (kept && kept.pc ? "The relay last heard from it " + (since(kept.pc) === "now" ? "just now" : since(kept.pc) + " ago") + ". " : "")
      + "Is it on, and is  talos relay  running on it? This page keeps trying.";
    if (kept && Array.isArray(kept.chats) && !info.pc) info.chats = kept.chats;   // the list as the relay last had it
    boards[active.c] = { rows: kept && kept.chats || info.chats, pc: kept && kept.pc || info.pc, why: "does not answer" };
  }

  function schedule() {
    clearTimeout(lookTimer);
    if (document.hidden || !active || dead) return;
    var own = chatsData[selected], busy = own && own.running || pending.some(function (p) { return p.state !== "failed"; });
    lookTimer = setTimeout(look, busy ? (alone(active) ? ALONE_EVERY_MS : BUSY_EVERY_MS) : QUIET_EVERY_MS);
  }

  async function look() {
    if (!active || dead) return;
    if (looking) { again = true; return; }
    looking = true;
    clearTimeout(lookTimer);
    var mine = turn;
    try {
      var answer = await call("look", { session: selected });
      if (answer.error) throw trouble("relay");
      silent = 0;
      problem = "";
      merge(answer, mine);
    } catch (error) {
      await explain(error);
    }
    looking = false;
    listed = "";
    render();
    if (again) { again = false; look(); } else schedule();
  }

  function sendWhy(error) {
    if (error.kind === "silent") return "The computer did not answer in time. Look at the chat before you send it again.";
    if (error.kind === "offline") return "Not sent: this phone has no connection.";
    if (error.kind === "gone" || error.kind === "key") return "Not sent: this link no longer works.";
    return "Not sent.";
  }

  async function deliver(prompt) {
    prompt.state = "sending";
    prompt.why = "";
    render();
    try {
      var own = chatsData[prompt.chat];
      var fields = { session: prompt.chat, text: prompt.img && prompt.text === "(photo)" ? "" : prompt.text, folder: prompt.folder || "" };
      if (prompt.img) fields.img = prompt.img;
      if (prompt.model) fields.model = prompt.model;
      var answer = await call("send", fields, active, prompt.img ? PHOTO_WAIT_MS : ANSWER_WAIT_MS);
      if (answer.error || !answer.ok) {
        prompt.state = "failed";
        prompt.why = answer.error || "The computer did not take it.";
      } else {
        prompt.state = "sent";
        prompt.base = prompt.chat === NEW ? 0 : own ? own.prompts || 0 : 0;
        if (prompt.chat === NEW && answer.session) {
          prompt.chat = answer.session;
          if (selected === NEW) { selected = answer.session; turn += 1; store.setItem("talos-relay-chat:" + active.c, selected); shownPast = shownTail = ""; }
        }
      }
    } catch (error) {
      prompt.state = "failed";
      prompt.why = sendWhy(error);
    }
    render();
    $("feed").scrollTop = $("feed").scrollHeight;
    look();
  }

  async function reply(id, answer) {
    var card = asking.card;
    if (card) Array.prototype.forEach.call(card.querySelectorAll("button"), function (made) { made.disabled = true; });
    try {
      var done = await call("reply", { id: id, answer: answer });
      if (done.error) problem = done.error;
    } catch (error) {
      if (card) Array.prototype.forEach.call(card.querySelectorAll("button"), function (made) { made.disabled = false; });
      $("status").textContent = "The answer did not reach the computer. Try again.";
      $("dot").className = "dot bad";
      return;
    }
    look();
  }

  async function stop() {
    if (work) { work.stop.disabled = true; work.stop.textContent = "Stopping\u2026"; }
    try {
      await call("stop", { session: selected });
    } catch (error) {
      $("status").textContent = "Stop did not reach the computer. Try again.";
      $("dot").className = "dot bad";
    }
    if (work) { work.stop.disabled = false; work.stop.textContent = "Stop"; }
    look();
  }

  // ------------------------------------------------------------ a photo to send along
  function load(file) {
    return new Promise(function (resolve, reject) {
      var address = URL.createObjectURL(file), image = new Image();
      image.onload = function () { URL.revokeObjectURL(address); resolve(image); };
      image.onerror = function () { URL.revokeObjectURL(address); reject(new Error("not a picture")); };
      image.src = address;
    });
  }

  /* The photo as a JPEG small enough to travel sealed through the relay. */
  async function shrink(file) {
    var image = await load(file);
    var longest = PHOTO_SIDE, quality = 0.82, canvas = document.createElement("canvas"), url = "";
    for (var attempt = 0; attempt < 8; attempt += 1) {
      var scale = Math.min(1, longest / Math.max(image.naturalWidth, image.naturalHeight));
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      var pen = canvas.getContext("2d");
      pen.fillStyle = "#fff";
      pen.fillRect(0, 0, canvas.width, canvas.height);
      pen.drawImage(image, 0, 0, canvas.width, canvas.height);
      url = canvas.toDataURL("image/jpeg", quality);
      if (url.length - 23 <= PHOTO_CHARS) break;
      if (quality > 0.6) quality -= 0.12; else longest = Math.round(longest * 0.8);
    }
    var data = url.slice(url.indexOf(",") + 1);
    if (url.indexOf("data:image/jpeg") !== 0 || data.length > PHOTO_CHARS) throw new Error("too large");
    return { data: data, url: url };
  }

  function setPhoto(made) {
    photo = made;
    $("preview").hidden = !made;
    if (made) $("previewPicture").src = made.url; else $("previewPicture").removeAttribute("src");
    $("photo").value = "";
  }

  // ------------------------------------------------------------ a 4-digit code instead of the QR code
  /* The computer shows 4 digits (talos relay code). It and this phone each turn them into a number,
     raise it to a secret of their own and swap the results (SPEKE, in the 2048-bit group of RFC 3526).
     Only two sides that started from the same digits end with the same key, and what crosses the
     relay says nothing about the digits: a guess has to be sent to the computer, which takes three.
     The pairing link then arrives sealed with the key the two just made.
     The swap happens in a "room": a channel of the relay named by the time alone. */
  var CODE_PRIME =
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD" +
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED" +
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F" +
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B" +
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510" +
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF";
  var ROOM_EVERY_MS = 5 * 60 * 1000, ROOM_HANG_MS = 6000, CODE_WAIT_MS = 25000;
  var canCode = !!subtle && typeof BigInt === "function";

  function bytesOf(text) { return new TextEncoder().encode(text); }
  function joined(pieces) {
    var all = new Uint8Array(pieces.reduce(function (sum, piece) { return sum + piece.length; }, 0)), at = 0;
    pieces.forEach(function (piece) { all.set(piece, at); at += piece.length; });
    return all;
  }
  function digest(bytes) {
    return subtle.digest("SHA-256", bytes).then(function (made) { return new Uint8Array(made); });
  }
  function signed(key, text) {
    return subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"])
      .then(function (made) { return subtle.sign("HMAC", made, bytesOf(text)); })
      .then(function (made) { return new Uint8Array(made); });
  }
  function numberOf(bytes) {
    var hex = "0";
    for (var i = 0; i < bytes.length; i += 1) hex += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16);
    return BigInt("0x" + hex);
  }
  function wide(number) {                            // 256 bytes, the highest first
    var hex = number.toString(16), bytes = new Uint8Array(256);
    hex = new Array(513 - hex.length).join("0") + hex;
    for (var i = 0; i < 256; i += 1) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
    return bytes;
  }
  function power(base, exponent, modulus) {
    var result = BigInt(1), none = BigInt(0), one = BigInt(1);
    base %= modulus;
    while (exponent > none) {
      if (exponent & one) result = result * base % modulus;
      base = base * base % modulus;
      exponent >>= one;
    }
    return result;
  }
  async function codeBase(code, prime) {
    var pieces = [];
    for (var count = 0; count < 10; count += 1) {
      pieces.push(await digest(joined([bytesOf("talos-relay pair v1|" + code), new Uint8Array([count])])));
    }
    var number = numberOf(joined(pieces)) % prime;
    return number * number % prime;
  }

  /* The room a computer holds at this relay for this stretch of time, or null. */
  async function lookRoom(stretch) {
    var named = await digest(bytesOf("talos-relay pair room v1|" + stretch));
    var room = location.origin + "/c/pair" + toBase64(named).slice(0, 28);
    var got = await reach(room + "/state", {}, ROOM_HANG_MS);
    if (got.status !== 200) return null;
    try {
      var found = JSON.parse(await got.text());
      return found.v === 1 && typeof found.a === "string" && typeof found.t === "number" ? { base: room, a: found.a, t: found.t } : null;
    } catch (error) { return null; }
  }

  /* The pairing link of the computer that shows this code. Throws trouble("no-code", "wrong", "silent" or "unreachable"). */
  async function linkByCode(code) {
    var stretch = Math.floor(Date.now() / ROOM_EVERY_MS), looks = [];
    for (var step = -2; step <= 1; step += 1) looks.push(lookRoom(stretch + step).catch(function () { return false; }));   // the two clocks may differ
    var found = await Promise.all(looks);
    var rooms = found.filter(Boolean).sort(function (a, b) { return b.t - a.t; });
    if (!rooms.length) throw trouble(found.every(function (room) { return room === false; }) ? "unreachable" : "no-code");
    var room = rooms[0], prime = BigInt("0x" + CODE_PRIME), one = BigInt(1);
    var theirs = numberOf(fromBase64(room.a));
    if (theirs <= one || theirs >= prime - one) throw trouble("no-code");
    var secret = numberOf(crypto.getRandomValues(new Uint8Array(32))) + BigInt(2);
    var mine = power(await codeBase(code, prime), secret, prime);
    var shared = power(theirs * theirs % prime, secret, prime);
    var key = await digest(joined([bytesOf("talos-relay pair v1 key|"), wide(theirs), wide(mine), wide(shared)]));
    var proof = await signed(key, "proof"), sealKey = await signed(key, "seal");
    var id = randomId();
    var got = await reach(room.base + "/ask/" + id, {
      method: "POST", headers: { "Content-Type": "text/plain" }, body: JSON.stringify({ b: toBase64(wide(mine)), m: toBase64(proof) }),
    }, HANG_MS);
    if (!got.ok) throw trouble(got.status === 404 ? "no-code" : "silent");
    var started = Date.now();
    while (Date.now() - started < CODE_WAIT_MS) {
      var asked = Date.now();
      got = await reach(room.base + "/answer/" + id + "?wait=" + ANSWER_HOLD_S, {}, ANSWER_HANG_MS);
      if (got.status === 200) {
        var answer = JSON.parse(await got.text());
        if (!answer.ok) throw Object.assign(trouble("wrong"), { left: answer.left });
        var raw = fromBase64(answer.link);
        var opener = await subtle.importKey("raw", sealKey, "AES-GCM", false, ["decrypt"]);
        var plain = await subtle.decrypt({ name: "AES-GCM", iv: raw.slice(0, 12) }, opener, raw.slice(12));
        return JSON.parse(new TextDecoder().decode(plain)).link;
      }
      if (got.status !== 204) throw trouble("silent");
      if (Date.now() - asked < 1000) await pause(400);   // this relay did not hold the request
    }
    throw trouble("silent");
  }
  function codeWhy(error) {
    if (navigator.onLine === false || error.kind === "unreachable" || error.kind === "offline") return DIRECT ? "This phone cannot reach the computer. It must be on the same Wi-Fi as the computer."
      : "This phone cannot reach the relay. Check its internet, then try again.";
    if (error.kind === "no-code") return "No computer is showing a code right now. On that computer, run  talos relay code  and enter its 4 digits here.";
    if (error.kind === "wrong" && error.left > 0) return "That is not the code. Check the 4 digits on the computer and try again.";
    if (error.kind === "wrong") return "That is not the code, and it has stopped working now. Make a new code on the computer.";
    return "The computer did not answer. Make a new code on it and try again.";
  }
  function linkTicket(text) {
    try {
      var address = new URL(String(text).trim());
      return { ticket: decodeTicket(address.hash), here: address.origin === location.origin, host: address.host };
    } catch (error) {
      return { ticket: decodeTicket(String(text).trim()), here: true, host: "" };   // only the part after the #
    }
  }

  // ------------------------------------------------------------ devices
  function deviceLabels() {
    if (!active) return;
    var picker = $("devicePicker");
    picker.replaceChildren();
    devices.forEach(function (d) {
      var option = element("option", "", d.name);
      option.value = d.c;
      picker.appendChild(option);
    });
    if (!deviceById(PHONE.c)) {
      var own = element("option", "", "This phone (no computer)");
      own.value = PHONE.c;
      picker.appendChild(own);
    }
    picker.value = active.c;
    $("deviceControls").hidden = false;
    $("attach").hidden = alone(active);              // a photo needs a computer
    $("showKeys").hidden = !alone(active);
    $("sendTarget").textContent = "To: " + active.name;
    $("send").setAttribute("aria-label", "Send to " + active.name);
    boardShown = "";
    if (view === "board") renderBoard();             // a new name shows on its rows at once
  }
  function switchDevice(next) {
    if (!next) return false;
    if (active && next === active) { deviceLabels(); return false; }
    if (active && pending.some(function (p) { return p.state === "sending"; })) {
      window.alert("A prompt is still sending. Switch devices after it finishes.");
      deviceLabels();
      return false;
    }
    if (active && next !== active && ($("box").value.trim() || photo) && !window.confirm("Discard the unsent draft and switch devices?")) {
      deviceLabels();
      return false;
    }
    store.setItem(ACTIVE, next.c);
    $("send").disabled = true;
    history.replaceState(null, "", location.pathname + location.search + ticketHash(next));
    location.reload();                               // the feed, the timers and the pictures start again together
    return true;
  }
  var renaming = false, byCode = canCode, brought = null, linking = false;
  function showWay(code) {
    byCode = code && canCode;
    $("codeFields").hidden = !byCode;
    $("linkFields").hidden = byCode;
    $("useCode").hidden = !canCode;
    $("deviceError").hidden = true;
  }
  function editDevice(rename) {
    renaming = rename;
    $("deviceDialogTitle").textContent = rename ? "Rename device" : "Add device";
    $("deviceName").value = rename && active ? active.name : "";
    $("deviceLink").value = $("deviceCode").value = "";
    brought = null;
    showWay(true);
    $("pairFields").hidden = rename;
    $("deviceError").hidden = true;
    $("saveDevice").textContent = rename ? "Save name" : "Add and switch";
    $("deviceDialog").showModal();
  }
  function formError(message) {
    $("deviceError").textContent = message;
    $("deviceError").hidden = false;
  }
  function phoneDevice() { return deviceById(PHONE.c) || remember({ c: PHONE.c, k: PHONE.k }); }
  $("devicePicker").onchange = function () { switchDevice(this.value === PHONE.c ? phoneDevice() : deviceById(this.value)); };
  $("phoneAlone").onclick = function () { switchDevice(phoneDevice()); };
  $("addDevice").onclick = $("firstDevice").onclick = function () { editDevice(false); };
  $("renameDevice").onclick = function () { editDevice(true); };
  $("cancelDevice").onclick = function () { $("deviceDialog").close(); };
  $("useLink").onclick = function () { showWay(false); };
  $("useCode").onclick = function () { showWay(true); };
  $("deviceDialog").onclose = function () { $("deviceLink").value = $("deviceCode").value = ""; brought = null; };
  $("deviceForm").onsubmit = async function (event) {
    event.preventDefault();
    if (linking) return;
    var name = $("deviceName").value.trim();
    if (!name) return formError("Enter a name for this device.");
    if (renaming && active) {
      if (devices.some(function (d) { return d !== active && d.name.toLowerCase() === name.toLowerCase(); })) return formError("Use a distinct name so you can tell the computers apart.");
      active.name = name;
      active.named = true;
      keepDevices();
      deviceLabels();
      $("deviceDialog").close();
      return;
    }
    var ticket = null;
    if (byCode) {
      var code = $("deviceCode").value.trim(), why = "";
      if (!/^[0-9]{4}$/.test(code)) return formError("Type the 4 digits that computer shows.");
      if (!brought || brought.code !== code) {
        linking = true;
        $("saveDevice").disabled = true;
        $("saveDevice").textContent = "Linking\u2026";
        $("deviceError").hidden = true;
        try {
          brought = { code: code, ticket: linkTicket(await linkByCode(code)).ticket };
          if (!brought.ticket) throw trouble("silent");
        } catch (error) {
          brought = null;
          why = codeWhy(error);
        }
        linking = false;
        $("saveDevice").disabled = false;
        $("saveDevice").textContent = "Add and switch";
        if (!$("deviceDialog").open) { brought = null; return; }   // cancelled while the computer was asked
        if (why) return formError(why);
      }
      ticket = brought.ticket;
    } else {
      var read = linkTicket($("deviceLink").value);
      if (!read.ticket) return formError("That is not a Talos Relay pairing link. Copy the complete link from the QR code.");
      if (!read.here) return formError("That computer uses another relay (" + read.host + "). Open its link in the browser instead.");
      ticket = read.ticket;
    }
    if (devices.some(function (d) { return d.c !== ticket.c && d.name.toLowerCase() === name.toLowerCase(); })) {
      return formError("Use a distinct name so you can tell the computers apart.");
    }
    var next = remember(ticket, name);
    $("deviceDialog").close();
    if (!switchDevice(next)) deviceLabels();
  };
  window.addEventListener("hashchange", function () {
    var ticket = decodeTicket(location.hash);
    if (!ticket || (active && ticket.c === active.c && ticket.k === active.k)) return;
    var next = remember(ticket);
    if (next !== active) { switchDevice(next); return; }
    dead = false;                                    // the same computer with a new key: ask it again
    problem = "";
    look();
  });

  // ------------------------------------------------------------ the phone alone: its models and keys
  function keysNote(lines) {
    $("keysNote").textContent = lines.join("\n");
    $("keysNote").hidden = !lines.length;
  }
  /* The models of every service that has a key, asked from the service itself. Returns one line for each. */
  async function listModels() {
    var lines = [];
    await Promise.all(Object.keys(PHONE.services).map(async function (service) {
      if (!PHONE.keys()[service]) return;
      try {
        var count = await PHONE.refresh(service);
        lines.push(PHONE.services[service].name + ": " + count + (count === 1 ? " model" : " models") + (PHONE.services[service].only ? " that cost nothing" : ""));
      } catch (error) {
        lines.push(PHONE.services[service].name + ": " + (error && error.name === "TypeError" ? "this phone could not reach it" : error.message));
      }
    }));
    return lines.sort();
  }
  function showKeys() {
    var fields = $("keysFields"), from = $("keysFrom");
    fields.replaceChildren();
    Object.keys(PHONE.services).forEach(function (service) {
      var kit = PHONE.services[service], label = element("label", "", kit.name + " key (free at " + kit.where + ")");
      var input = element("input");
      input.type = "password";
      input.id = "key-" + service;
      input.autocomplete = "off";
      input.autocapitalize = "none";
      input.spellcheck = false;
      input.placeholder = "No key: its models are not listed";
      input.value = PHONE.keys()[service] || "";
      label.htmlFor = input.id;
      fields.appendChild(label);
      fields.appendChild(input);
    });
    from.replaceChildren();
    devices.filter(function (device) { return !alone(device); }).forEach(function (device) {
      from.appendChild(button("other-way", "Get the keys from " + device.name, async function () {
        keysNote(["Asking " + device.name + "\u2026"]);
        try {
          var answer = await call("keys", {}, device, 15000);
          var given = answer && answer.keys || {}, got = Object.keys(given).filter(function (service) { return PHONE.services[service]; });
          if (answer.error) return keysNote([answer.error]);
          if (!got.length) return keysNote([device.name + " hands out no key. On it, run: talos relay keys openrouter   (or the services you use), then try again."]);
          got.forEach(function (service) { PHONE.setKey(service, given[service]); $("key-" + service).value = given[service]; });
          keysNote(["Asking the services for their models\u2026"]);
          keysNote(await listModels());
          look();
        } catch (error) {
          keysNote([error.kind === "silent" ? device.name + " does not answer. Is it on, and is  talos relay  running on it?"
            : error.kind === "offline" ? "This phone has no connection." : "The link of " + device.name + " no longer works."]);
        }
      }));
    });
    keysNote([]);
    $("keysDialog").showModal();
  }
  $("showKeys").onclick = showKeys;
  $("closeKeys").onclick = function () { $("keysDialog").close(); };
  $("keysForm").onsubmit = async function (event) {
    event.preventDefault();
    Object.keys(PHONE.services).forEach(function (service) { PHONE.setKey(service, $("key-" + service).value); });
    $("saveKeys").disabled = true;
    keysNote(["Asking the services for their models\u2026"]);
    var lines = await listModels();
    $("saveKeys").disabled = false;
    keysNote(lines.length ? lines : ["No key is saved. Glow's free AI answers without one."]);
    look();
  };
  $("model").onchange = function () {
    picked[selected] = this.value;
    if (active) store.setItem("talos-relay-model:" + active.c, this.value);
    render();
  };

  // ------------------------------------------------------------ version history
  /* The list lives in versions.json, the one place versions are written down. */
  function showVersions(releases) {
    var list = $("versionsList");
    list.replaceChildren();
    if (!releases || !releases.length) {
      list.appendChild(element("p", "", "The list did not load. Check the internet on this phone, then try again."));
      return;
    }
    releases.forEach(function (release, at) {
      var running = release.version === APP_VERSION;
      var entry = element("details", running ? "version running" : "version");
      entry.open = running || (at === 0 && !releases.some(function (r) { return r.version === APP_VERSION; }));
      var head = element("summary");
      head.appendChild(element("strong", "", release.version));
      head.appendChild(element("span", "when", release.date));
      head.appendChild(element("span", "what", release.title));
      head.appendChild(element("span", running ? "mark" : "count", running ? "this version" : release.changes.length + (release.changes.length === 1 ? " change" : " changes")));
      entry.appendChild(head);
      var changes = element("ul");
      release.changes.forEach(function (change) { changes.appendChild(element("li", "", String(change).replace(/[`*]/g, ""))); });
      entry.appendChild(changes);
      list.appendChild(entry);
    });
  }
  $("showVersions").onclick = function () {
    $("versionsList").replaceChildren(element("p", "", "Loading"));
    $("versionsDialog").showModal();
    fetch("versions.json?v=" + APP_VERSION, { cache: "no-cache" })
      .then(function (got) { return got.ok ? got.json() : null; })
      .then(function (data) { showVersions(data && data.releases); })
      .catch(function () { showVersions(null); });
  };
  $("closeVersions").onclick = function () { $("versionsDialog").close(); };

  // ------------------------------------------------------------ start
  if (!active || !subtle) {
    $("none").hidden = false;
    $("feed").hidden = true;
    $("menu").hidden = true;
    $("dot").className = "dot" + (active ? " bad" : "");
    $("status").textContent = active ? "This browser cannot seal text on this page." : "Not linked";
    return;
  }
  deviceLabels();
  $("composer").hidden = false;
  document.body.classList.toggle("on-board", view === "board");
  $("showBoard").hidden = false;
  $("showBoard").addEventListener("click", function () { setView("board"); side(false); });
  $("newChat").addEventListener("click", function () {
    setView("chat");
    select(NEW);
    side(false);
    $("box").focus();
  });
  $("menu").addEventListener("click", function () { side(!document.body.classList.contains("side-open")); });
  $("shade").addEventListener("click", function () { side(false); });
  $("find").addEventListener("input", function () { listed = ""; chips(); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape") side(false); });
  var box = $("box");
  function fit() {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight + 2, window.innerHeight * 0.38) + "px";
  }
  box.addEventListener("input", fit);
  box.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $("composer").requestSubmit(); }
  });
  $("attach").addEventListener("click", function () { $("photo").click(); });
  $("previewRemove").addEventListener("click", function () { setPhoto(null); });
  $("photo").addEventListener("change", async function () {
    var file = $("photo").files && $("photo").files[0];
    if (!file) return;
    $("attach").disabled = true;
    try { setPhoto(await shrink(file)); }
    catch (error) {
      setPhoto(null);
      $("status").textContent = "This phone could not read that photo. Try another one.";
      $("dot").className = "dot bad";
    }
    $("attach").disabled = false;
  });
  $("composer").addEventListener("submit", function (event) {
    event.preventDefault();
    var text = box.value.trim();
    if ((!text && !photo) || $("send").disabled) return;
    var prompt = { pid: randomId(), text: text || "(photo)", t: Math.floor(Date.now() / 1000), chat: selected, state: "sending",
                   folder: selected === NEW ? folder : "", model: wantedModel() };
    delete picked[NEW];                              // the next new chat starts from the model you use most
    if (photo) { prompt.img = photo.data; prompt.url = photo.url; }
    pending.push(prompt);
    box.value = "";
    setPhoto(null);
    fit();
    deliver(prompt);
  });
  var ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var standalone = navigator.standalone === true || (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
  if (ios && !standalone && !store.getItem(HINT)) {
    $("hint").hidden = false;
    $("hintClose").onclick = function () { store.setItem(HINT, "1"); $("hint").hidden = true; };
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    look();
    if (view === "board") lookBoards();
  });
  window.addEventListener("online", look);
  render();
  look();
  lookBoards();                                      // once for the count beside All chats, then while the board is open
}());
