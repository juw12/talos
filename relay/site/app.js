/* Talos Relay, the phone page.

   The link holds the channel and the key after the #, which the browser sends to no server:
       https://<relay>/#<channel>.<key>
   Every request to the computer is sealed here with AES-256-GCM (the browser's own WebCrypto) and
   left at the relay; the computer (talos/relay.py) picks it up, and leaves a sealed answer.

   Text that comes from the computer is only ever shown with textContent, never as HTML.
*/
"use strict";
(function () {
  var PROTOCOL = 1;
  var KEPT = "talos-relay-link";
  var ANSWER_WAIT_MS = 20000;
  var $ = function (id) { return document.getElementById(id); };

  var link = null;                              // {channel, key}
  var base = "";
  var view = { name: "list", session: "" };
  var turn = 0;                                 // goes up with every change of view: old answers are dropped
  var shown = { items: "", ask: "" };
  var folders = [];
  var timer = 0, looking = false, again = false, sending = false, silent = 0;
  var dead = false;                             // the computer does not know this link: asking again is no use

  // ------------------------------------------------------------ sealing
  function b64(bytes) {
    var text = "";
    for (var at = 0; at < bytes.length; at += 8192) {
      text += String.fromCharCode.apply(null, bytes.subarray(at, at + 8192));
    }
    return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function unb64(text) {
    var plain = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
    var bytes = new Uint8Array(plain.length);
    for (var at = 0; at < plain.length; at++) bytes[at] = plain.charCodeAt(at);
    return bytes;
  }

  function extra(kind, id) {
    return new TextEncoder().encode("talos-relay/" + PROTOCOL + "/" + link.channel + "/" + kind + "/" + id);
  }

  async function seal(kind, id, data) {
    var nonce = crypto.getRandomValues(new Uint8Array(12));
    var sealed = new Uint8Array(await crypto.subtle.encrypt(
      { name: "AES-GCM", iv: nonce, additionalData: extra(kind, id) }, link.key,
      new TextEncoder().encode(JSON.stringify(data))));
    var whole = new Uint8Array(12 + sealed.length);
    whole.set(nonce);
    whole.set(sealed, 12);
    return b64(whole);
  }

  async function open(kind, id, text) {
    try {
      var whole = unb64(text.trim());
      var plain = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: whole.subarray(0, 12), additionalData: extra(kind, id) }, link.key, whole.subarray(12));
      return JSON.parse(new TextDecoder().decode(plain));
    } catch (error) {
      throw problem("key");
    }
  }

  // ------------------------------------------------------------ talking to the computer
  function problem(kind) {
    var error = new Error(kind);
    error.kind = kind;
    return error;
  }

  function pause(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  async function call(op, fields) {
    var id = b64(crypto.getRandomValues(new Uint8Array(12)));
    var ask = Object.assign({ op: op, t: Date.now() }, fields || {});
    var body = await seal("ask", id, ask);
    var left;
    try {
      left = await fetch(base + "/ask/" + id, { method: "POST", body: body, headers: { "Content-Type": "text/plain" } });
    } catch (error) {
      throw problem("offline");
    }
    if (left.status === 404) throw problem("gone");
    if (left.status === 429) throw problem("silent");
    if (!left.ok) throw problem("relay");
    var until = Date.now() + ANSWER_WAIT_MS;
    while (Date.now() < until) {
      var got;
      try {
        got = await fetch(base + "/answer/" + id + "?wait=6");
      } catch (error) {
        throw problem("offline");
      }
      if (got.status === 200) return open("answer", id, await got.text());
      if (got.status !== 204) await pause(1000);
    }
    throw problem("silent");
  }

  async function lastHeard() {
    try {
      var got = await fetch(base + "/state");
      if (got.status !== 200) return null;
      return await open("state", "", await got.text());
    } catch (error) {
      return null;
    }
  }

  // ------------------------------------------------------------ small helpers
  function say(text) {
    var status = $("status");
    status.textContent = text;
    clearTimeout(say.timer);
    if (text) say.timer = setTimeout(function () { status.textContent = ""; }, 5000);
  }

  function ago(seconds) {
    var passed = Math.max(0, Date.now() / 1000 - Number(seconds || 0));
    if (passed < 90) return "just now";
    if (passed < 5400) return Math.round(passed / 60) + " min ago";
    if (passed < 129600) return Math.round(passed / 3600) + " h ago";
    return Math.round(passed / 86400) + " d ago";
  }

  function make(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function nearEnd() {
    return window.innerHeight + window.scrollY > document.documentElement.scrollHeight - 160;
  }

  function toEnd() {
    window.scrollTo(0, document.documentElement.scrollHeight);
  }

  // ------------------------------------------------------------ what is shown
  function show(name, session, push) {
    turn++;
    view = { name: name, session: session || "" };
    shown = { items: "", ask: "" };
    if (push) history.pushState(view, "");
    ["unlinked", "list", "start", "chat"].forEach(function (id) { $(id).hidden = id !== name; });
    $("back").hidden = name === "list" || name === "unlinked";
    $("composer").hidden = name !== "chat" && name !== "start";
    $("title").textContent = name === "start" ? "New chat" : "Talos Relay";
    if (name === "chat") {
      $("items").textContent = "";
      ["earlier", "live", "ask", "proof", "trouble"].forEach(function (id) { $(id).hidden = true; });
    }
    if (name === "start") {
      var select = $("folder");
      select.textContent = "";
      folders.forEach(function (folder) {
        var option = make("option", "", folder);
        option.value = folder;
        select.appendChild(option);
      });
    }
    composer(false);
    window.scrollTo(0, 0);
    if (name === "list" || name === "chat") refresh();
  }

  function composer(running) {
    $("send").disabled = sending || running;
    $("text").placeholder = running ? "Talos is working. Stop it to send something else." : "Give Talos a task";
  }

  function trouble(kind) {
    var where = $("where");
    where.classList.add("off");
    var notice = $(view.name === "chat" ? "trouble" : "notice");
    var text = {
      gone: "The computer no longer knows this link. On the computer, run: talos relay pair  and scan the new code.",
      key: "This link does not have the key of the computer. On the computer, run: talos relay pair  and scan the code again.",
      offline: "This phone has no connection to the relay. It tries again by itself.",
      relay: "The relay did not take the request. It tries again by itself.",
    }[kind];
    if (kind !== "silent") {
      dead = kind === "gone" || kind === "key";
      where.textContent = dead ? "not linked" : kind === "offline" ? "no connection" : "relay trouble";
      notice.textContent = text || "Something went wrong. It tries again by itself.";
      notice.hidden = false;
      return;
    }
    silent++;
    where.textContent = "no answer";
    if (silent < 2) return;                      // one missed answer is not yet news
    lastHeard().then(function (state) {
      notice.textContent = (state && state.computer ? state.computer : "The computer") + " does not answer. " +
        (state && state.t ? "The relay last heard from it " + ago(state.t / 1000) + ". " : "") +
        "Is it on, and is  talos relay  running on it? This page keeps trying.";
      notice.hidden = false;
    });
  }

  function renderList(answer) {
    var chats = $("chats");
    chats.textContent = "";
    (answer.chats || []).forEach(function (chat) {
      var row = make("li"), button = make("button");
      button.appendChild(make("span", "name", chat.title));
      var meta = make("span", "meta");
      if (chat.running) meta.appendChild(make("span", "badge", "working \u00b7 "));
      meta.appendChild(document.createTextNode([chat.folder, ago(chat.updated)].filter(Boolean).join(" \u00b7 ")));
      button.appendChild(meta);
      button.addEventListener("click", function () { show("chat", chat.id, true); });
      row.appendChild(button);
      chats.appendChild(row);
    });
    $("empty").hidden = (answer.chats || []).length > 0;
    $("about").textContent = "Talos " + answer.version + " on " + answer.computer + ". Model: " +
      (answer.model || "none set") + ". " +
      (answer.asks_first ? "Before a step that can destroy work or leaves its folder, it asks here."
        : "It asks nobody before any step.");
  }

  function renderItems(items) {
    var mark = JSON.stringify(items);
    if (mark === shown.items) return;
    shown.items = mark;
    var box = $("items");
    box.textContent = "";
    items.forEach(function (item) {
      if (item.role === "step") {
        var step = make("div", "item step", item.name + (item.target ? "  " + item.target : ""));
        if (item.result) {
          step.appendChild(make("div", "result", item.result));
          step.addEventListener("click", function () { step.classList.toggle("open"); });
        }
        box.appendChild(step);
      } else {
        box.appendChild(make("div", "item " + (item.role === "you" ? "you" : "talos"), item.text));
      }
    });
  }

  function renderAsk(ask) {
    var box = $("ask");
    if (!ask) {
      shown.ask = "";
      box.hidden = true;
      return;
    }
    if (shown.ask === ask.id) return;
    shown.ask = ask.id;
    box.textContent = "";
    var choices = make("div", "choices");
    function choice(label, answer, cls) {
      var button = make("button", cls || "", label);
      button.addEventListener("click", function () { reply(ask.id, answer); });
      choices.appendChild(button);
    }
    if (ask.kind === "permission") {
      box.appendChild(make("p", "what", "Talos asks to use: " + ask.tool));
      if (ask.target) box.appendChild(make("div", "target", ask.target));
      if (ask.reason) box.appendChild(make("p", "soft small", ask.reason));
      choice("Allow once", "once", "primary");
      choice("Always allow", "always");
      choice("Refuse", "reject", "danger");
    } else {
      box.appendChild(make("p", "what", ask.question || "Talos has a question"));
      (ask.options || []).forEach(function (option) { choice(String(option), String(option)); });
      var input = make("input");
      input.placeholder = "Or type an answer";
      input.setAttribute("aria-label", "Your answer");
      box.appendChild(input);
      var button = make("button", "primary", "Answer");
      button.addEventListener("click", function () {
        if (input.value.trim()) reply(ask.id, input.value.trim());
      });
      choices.appendChild(button);
    }
    box.appendChild(choices);
    box.hidden = false;
  }

  function renderChat(chat) {
    var stay = nearEnd();
    $("title").textContent = chat.title;
    $("earlier").hidden = !chat.earlier;
    $("earlier").textContent = chat.earlier ? chat.earlier + " earlier parts of this chat are not shown here." : "";
    renderItems(chat.items || []);
    var live = chat.live, running = Boolean(chat.running && live);
    $("live").hidden = !running;
    if (running) {
      $("clock").textContent = "Working, " + live.seconds + " s";
      $("stream").textContent = live.text || "";
      $("notes").textContent = (live.notes || []).join("\n");
    }
    renderAsk(running ? live.ask : null);
    var done = !chat.running && live && live.done;
    $("proof").hidden = !(done && done.proof);
    $("proof").textContent = done && done.proof ? done.proof : "";
    $("trouble").hidden = !(done && done.stopped);
    $("trouble").textContent = done && done.stopped ? "Stopped: " + done.stopped + (done.text ? "\n" + done.text : "") : "";
    composer(Boolean(chat.running));
    if (stay) toEnd();
  }

  // ------------------------------------------------------------ the loop
  function schedule(running) {
    clearTimeout(timer);
    if (document.hidden || !link || dead || view.name === "start" || view.name === "unlinked") return;
    timer = setTimeout(refresh, view.name === "chat" ? (running ? 2500 : 6000) : 8000);
  }

  async function refresh() {
    if (!link || (view.name !== "list" && view.name !== "chat")) return;
    if (looking) {
      again = true;
      return;
    }
    looking = true;
    clearTimeout(timer);
    var mine = turn, running = false;
    try {
      var answer = await call("look", { session: view.name === "chat" ? view.session : "" });
      if (mine === turn) {
        silent = 0;
        folders = answer.folders || [];
        $("where").textContent = answer.computer || "";
        $("where").classList.remove("off");
        $("notice").hidden = true;
        if (view.name === "list") {
          renderList(answer);
        } else if (answer.chat) {
          renderChat(answer.chat);
          running = Boolean(answer.chat.running);
        } else {
          say("That chat is no longer on the computer.");
          show("list", "", false);
        }
      }
    } catch (error) {
      if (mine === turn) trouble(error.kind || "relay");
    }
    looking = false;
    if (again) {
      again = false;
      refresh();
    } else {
      schedule(running);
    }
  }

  async function send() {
    var text = $("text").value.trim();
    if (!text || sending) return;
    sending = true;
    composer(false);
    say("Sending to the computer\u2026");
    try {
      var answer = await call("send", {
        session: view.name === "chat" ? view.session : "", text: text,
        folder: view.name === "start" ? $("folder").value : "",
      });
      sending = false;
      if (answer.error) {
        say(answer.error);
      } else {
        say("");
        $("text").value = "";
        $("text").style.height = "";
        if (view.name === "start") {
          history.replaceState({ name: "chat", session: answer.session }, "");
          show("chat", answer.session, false);
        } else {
          refresh();
        }
        setTimeout(toEnd, 50);
      }
    } catch (error) {
      sending = false;
      say(error.kind === "silent" ? "The computer does not answer. Your message was not sent."
        : "That could not be sent. Try again.");
    }
    composer(false);
  }

  async function reply(id, answer) {
    $("ask").hidden = true;
    try {
      var done = await call("reply", { id: id, answer: answer });
      if (done.error) say(done.error);
    } catch (error) {
      shown.ask = "";
      say("The answer did not reach the computer. Try again.");
    }
    refresh();
  }

  async function stop() {
    $("stop").disabled = true;
    try {
      await call("stop", { session: view.session });
    } catch (error) {
      say("Stop did not reach the computer. Try again.");
    }
    $("stop").disabled = false;
    refresh();
  }

  // ------------------------------------------------------------ start
  async function begin() {
    var given = /^#([A-Za-z0-9_-]{16,64})\.([A-Za-z0-9_-]{43})$/.exec(location.hash);
    var kept = null;
    try {
      kept = given ? null : JSON.parse(localStorage.getItem(KEPT) || "null");
    } catch (error) {
      kept = null;
    }
    var channel = given ? given[1] : kept && kept.channel, key = given ? given[2] : kept && kept.key;
    if (!channel || !key || !window.crypto || !crypto.subtle) {
      show("unlinked", "", false);
      if (channel && key) $("unlinked").appendChild(make("p", "notice", "This browser cannot seal text on this page (it needs https)."));
      return;
    }
    try {
      localStorage.setItem(KEPT, JSON.stringify({ channel: channel, key: key }));
    } catch (error) { /* private mode: the link in the address is enough */ }
    link = {
      channel: channel,
      key: await crypto.subtle.importKey("raw", unb64(key), { name: "AES-GCM" }, false, ["encrypt", "decrypt"]),
    };
    base = "/c/" + channel;
    history.replaceState({ name: "list", session: "" }, "");
    show("list", "", false);
  }

  $("new").addEventListener("click", function () { show("start", "", true); });
  $("back").addEventListener("click", function () { history.back(); });
  $("send").addEventListener("click", send);
  $("stop").addEventListener("click", stop);
  $("text").addEventListener("input", function () {
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight + 2, 160) + "px";
  });
  $("text").addEventListener("keydown", function (event) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) send();
  });
  window.addEventListener("popstate", function (event) {
    var to = event.state || { name: "list", session: "" };
    if (link) show(to.name, to.session, false);
  });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) clearTimeout(timer);
    else refresh();
  });
  begin();
})();
