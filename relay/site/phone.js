/* Talos Relay, the part that needs no computer: this phone asks a model itself.
   It answers the same requests as Talos on a computer (look, send, stop, hide), so the page shows
   its chats the same way. Chats and keys stay in this browser. A prompt goes straight to the
   service of the model that was picked and to nobody else: it does not pass the relay, and it is
   not sealed with a link key. It can only chat: files and steps need Talos on a computer. */
(function () {
  "use strict";
  var C = "this-phone-no-computer", K = new Array(44).join("A");   // the shape of a link; it opens nothing
  var CHATS = "talos-phone-chats", KEYS = "talos-phone-keys", LISTS = "talos-phone-models";
  var KEPT = 60, WORDS = 20000, ROOM = 48000, QUIET_MS = 90000, LIST_FRESH_MS = 24 * 3600 * 1000, LISTED = 80;
  var FIRST = "glow/auto";
  var SYSTEM = "You are Talos, answering on a phone with no computer attached. Here you cannot run tools, " +
    "read or change files, or browse: say so in one line when a task needs that, then help with what words can do. " +
    "Answer in plain words and keep it short unless more is asked for.";

  /* Glow's free AI, no key: the same routes in the same order as talos/glow.py (a test keeps them equal). */
  var OVH = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1";
  var LLM7 = "https://api.llm7.io/v1";
  var POLLINATIONS = "https://text.pollinations.ai/openai";
  var GLOW = [
    ["deepseek", OVH, "DeepSeek-V4.1-Flash-1017"],
    ["qwen", OVH, "Qwen3.8-27B"],
    ["qwen", OVH, "Qwen3.5-397B-A17B"],
    ["other", OVH, "gpt-oss-120b"],
    ["other", OVH, "Meta-Llama-3_3-70B-Instruct"],
    ["other", OVH, "Mistral-Small-3.2-24B-Instruct-2506"],
    ["other", LLM7, "default"],
    ["other", POLLINATIONS, "openai"]
  ];
  var GLOW_NAMES = ["auto", "deepseek", "qwen"];

  /* Services with a free key. `only`: the models of it that cost nothing. `check`: a request that fails with a wrong key. */
  var NOT_CHAT = /whisper|tts|guard|embed|orpheus|speech|rerank/i;
  var SERVICES = {
    openrouter: { name: "OpenRouter", base: "https://openrouter.ai/api/v1", check: "/key", only: /:free$|^openrouter\/free$/,
                  first: ["openrouter/free"], where: "openrouter.ai, Keys" },
    groq: { name: "Groq", base: "https://api.groq.com/openai/v1", first: [], where: "console.groq.com, API Keys" },
    cerebras: { name: "Cerebras", base: "https://api.cerebras.ai/v1", first: [], where: "cloud.cerebras.ai, API Keys" }
  };

  // ------------------------------------------------------------ what this phone keeps
  var memory = {};
  function read(key, fallback) {
    if (memory[key] !== undefined) return memory[key];
    var found = null;
    try { found = JSON.parse(localStorage.getItem(key) || "null"); } catch (error) { found = null; }
    memory[key] = found && typeof found === "object" ? found : fallback;
    return memory[key];
  }
  function write(key) {
    try { localStorage.setItem(key, JSON.stringify(memory[key])); return true; } catch (error) { return false; }
  }
  function chats() { return read(CHATS, {}); }
  function keys() { return read(KEYS, {}); }
  function lists() { return read(LISTS, {}); }
  function seconds() { return Math.floor(Date.now() / 1000); }
  function clip(text, most) { text = String(text); return text.length > most ? text.slice(0, most - 1) + "\u2026" : text; }
  function newId() {
    var bytes = crypto.getRandomValues(new Uint8Array(9)), text = "";
    for (var at = 0; at < bytes.length; at++) text += (bytes[at] < 16 ? "0" : "") + bytes[at].toString(16);
    return "p" + text;
  }

  var runs = {};                                     // chat id -> the answer that is being written, or how it ended
  function busy(id) { return !!(runs[id] && runs[id].running); }

  /* The newest chats are kept. When the browser has no room left, the oldest ones go first. */
  function keepChats() {
    var all = chats();
    var ids = Object.keys(all).sort(function (a, b) { return all[b].updated - all[a].updated; });
    ids.slice(KEPT).forEach(function (id) { if (!busy(id)) delete all[id]; });
    while (!write(CHATS)) {
      var rest = Object.keys(all).filter(function (id) { return !busy(id); }).sort(function (a, b) { return all[a].updated - all[b].updated; });
      if (rest.length < 2) return;
      delete all[rest[0]];
    }
  }

  // ------------------------------------------------------------ the models of this phone
  function names() {
    var found = GLOW_NAMES.map(function (name) { return "glow/" + name; });
    Object.keys(SERVICES).forEach(function (service) {
      if (!keys()[service]) return;
      var kept = lists()[service];
      (kept && kept.ids && kept.ids.length ? kept.ids : SERVICES[service].first).forEach(function (id) { found.push(service + "/" + id); });
    });
    return found;
  }

  /* Where a model is asked: one service, or for glow/... each free service in turn. */
  function routes(model) {
    var cut = model.indexOf("/"), service = model.slice(0, cut), id = model.slice(cut + 1);
    if (service === "glow") {
      return GLOW.filter(function (route) { return id === "auto" || route[0] === id; })
        .map(function (route) { return { base: route[1], id: route[2], key: "", label: route[2] }; });
    }
    if (SERVICES[service] && keys()[service]) return [{ base: SERVICES[service].base, id: id, key: keys()[service], label: model }];
    return [];
  }

  /* What a service said when it refused, in one line. */
  function reason(status, body) {
    var said = "";
    try {
      var data = JSON.parse(body), error = data.error || data;
      said = typeof error === "string" ? error : String(error.message || data.message || data.detail || "");
    } catch (failed) { said = ""; }
    said = clip((said || String(body || "")).replace(/\s+/g, " ").trim(), 220);
    if (status === 401 || status === 403) return "the key was refused (" + status + ")" + (said ? ": " + said : "");
    if (status === 429) return "too many requests for now (429)" + (said ? ": " + said : "");
    return "HTTP " + status + (said ? ": " + said : "");
  }

  async function refresh(service) {
    var kit = SERVICES[service], key = keys()[service];
    if (!kit || !key) return 0;
    var headers = { Authorization: "Bearer " + key };
    if (kit.check) {
      var allowed = await fetch(kit.base + kit.check, { headers: headers });
      if (!allowed.ok) throw new Error(reason(allowed.status, await allowed.text()));
    }
    var reply = await fetch(kit.base + "/models", { headers: headers });
    if (!reply.ok) throw new Error(reason(reply.status, await reply.text()));
    var data = (await reply.json()).data || [];
    var ids = data.map(function (item) { return String(item.id || ""); })
      .filter(function (id) { return id && !NOT_CHAT.test(id) && (!kit.only || kit.only.test(id)); }).sort().slice(0, LISTED);
    lists()[service] = { ids: ids, at: Date.now() };
    write(LISTS);
    return ids.length;
  }

  var refreshing = {};
  function refreshOld() {                            // once a day, by itself: services add and drop models
    Object.keys(SERVICES).forEach(function (service) {
      var kept = lists()[service];
      if (!keys()[service] || refreshing[service] || (kept && Date.now() - kept.at < LIST_FRESH_MS)) return;
      refreshing[service] = true;
      refresh(service).catch(function () {
        lists()[service] = { ids: (kept && kept.ids) || [], at: Date.now() - LIST_FRESH_MS + 600000 };   // again in 10 minutes
      }).then(function () { refreshing[service] = false; });
    });
  }

  function setKey(service, key) {
    key = String(key || "").trim();
    if (!SERVICES[service] || keys()[service] === key || (!key && !keys()[service])) return false;
    if (key) keys()[service] = key; else delete keys()[service];
    delete lists()[service];
    write(KEYS);
    write(LISTS);
    return true;
  }

  // ------------------------------------------------------------ one answer
  /* Ask one service and write its answer into run.text while it comes. Returns the whole answer. */
  async function ask(route, messages, run) {
    var headers = { "Content-Type": "application/json" };
    if (route.key) headers.Authorization = "Bearer " + route.key;
    var options = { method: "POST", headers: headers, body: JSON.stringify({ model: route.id, messages: messages, stream: true }) };
    if (run.stopper) options.signal = run.stopper.signal;
    var reply = await fetch(route.base + "/chat/completions", options);
    if (!reply.ok) throw new Error(reason(reply.status, await reply.text()));
    var whole = "", rest = "", said = "";
    function take(chunk) {
      run.heard = Date.now();
      whole = clip(whole + chunk, 400000);
      var lines = (rest + chunk).split("\n");
      rest = lines.pop();
      lines.forEach(function (line) {
        if (line.slice(0, 5) !== "data:") return;
        try {
          var piece = JSON.parse(line.slice(5)), choice = piece.choices && piece.choices[0];
          var part = choice && (choice.delta || choice.message);
          if (part && typeof part.content === "string") { said += part.content; run.text = said; }
        } catch (error) { /* "[DONE]" and lines that are not an answer */ }
      });
    }
    if (reply.body && reply.body.getReader) {
      var reader = reply.body.getReader(), decoder = new TextDecoder();
      for (;;) {
        var got = await reader.read();
        if (got.done) break;
        take(decoder.decode(got.value, { stream: true }));
      }
    } else take(await reply.text());
    take("\n");
    if (!said) {                                     // a service that does not send the answer piece by piece
      var data = null;
      try { data = JSON.parse(whole); } catch (error) { data = null; }
      if (data && data.error) throw new Error(reason(200, whole));
      if (data && data.choices && data.choices[0] && data.choices[0].message) said = String(data.choices[0].message.content || "");
    }
    if (!said.trim()) throw new Error("it sent no answer");
    return said;
  }

  /* The chat as the model reads it: the newest messages that fit. */
  function recent(messages) {
    var kept = [], size = 0;
    for (var at = messages.length - 1; at >= 0; at--) {
      size += messages[at].content.length;
      if (size > ROOM && kept.length) break;
      kept.unshift({ role: messages[at].role, content: messages[at].content });
    }
    return kept;
  }

  async function work(chat, run) {
    var found = routes(chat.model), why = [], said = "";
    var messages = [{ role: "system", content: SYSTEM + " Today is " + new Date().toDateString() + "." }].concat(recent(chat.messages));
    if (!found.length) why.push(chat.model + ": this phone has no key for it. Open Models and keys.");
    var watch = setInterval(function () {
      if (run.running && run.stopper && Date.now() - run.heard > QUIET_MS) { run.quiet = true; run.stopper.abort(); }
    }, 5000);
    for (var at = 0; at < found.length && !said && !run.stopped; at++) {
      run.text = "";
      run.quiet = false;
      run.heard = Date.now();
      run.stopper = typeof AbortController === "function" ? new AbortController() : null;
      try {
        said = await ask(found[at], messages, run);
      } catch (error) {
        if (run.stopped) break;
        var words = run.quiet ? "no answer for " + QUIET_MS / 1000 + " seconds"
          : error && error.name === "TypeError" ? "this phone could not reach it" : String(error && error.message || error);
        why.push(found[at].label + ": " + words);
        run.notes = why.slice(-3).concat(at + 1 < found.length ? ["Trying " + found[at + 1].label + "."] : []);
      }
    }
    clearInterval(watch);
    var written = said || (run.stopped ? run.text : "");
    if (written.trim()) chat.messages.push({ role: "assistant", content: clip(written, WORDS), t: seconds() });
    chat.updated = seconds();
    run.running = false;
    run.text = "";
    run.notes = [];
    if (said) delete runs[chat.id];
    else run.done = run.stopped ? { stopped: "you stopped it", proof: "", text: "" }
      : { stopped: "model error", proof: "", text: clip(why.join("\n"), 2000) };
    keepChats();
  }

  // ------------------------------------------------------------ the requests of the page
  function row(chat) {
    var last = chat.messages[chat.messages.length - 1];
    return { id: chat.id, title: chat.title, updated: chat.updated, started: chat.created, running: busy(chat.id), folder: "",
             source: "phone", waits: false, steps: [],
             step: busy(chat.id) ? "" : clip(last && last.role === "assistant" ? last.content.replace(/\s+/g, " ") : "", 200) };
  }
  function rows() {
    var all = chats();
    return Object.keys(all).map(function (id) { return row(all[id]); }).sort(function (a, b) { return b.updated - a.updated; });
  }
  function shown(chat) {
    var run = runs[chat.id], now = seconds();
    return {
      id: chat.id, title: chat.title, folder: "", model: chat.model, running: busy(chat.id), earlier: 0,
      items: chat.messages.map(function (message) {
        return { role: message.role === "user" ? "you" : "talos", text: message.content, t: message.t };
      }),
      live: run ? { running: run.running, text: run.text.slice(-3000), notes: run.notes, done: run.done, ask: null, steps: [],
                    since: run.since, seconds: now - run.since } : null,
      prompts: chat.messages.filter(function (message) { return message.role === "user"; }).length
    };
  }

  async function call(op, fields) {
    fields = fields || {};
    var session = String(fields.session || ""), chat = session ? chats()[session] : null;
    if (op === "look") {
      refreshOld();
      var answer = { ok: true, protocol: 1, version: api.version, computer: "This phone", model: FIRST, models: names(),
                     asks_first: false, folders: [], chats: rows(), chat: chat ? shown(chat) : null, pc: seconds(), alone: true };
      if (session && !chat) answer.gone = session;
      return answer;
    }
    if (op === "send") {
      var text = String(fields.text || "").trim();
      if (fields.img) return { error: "A photo needs Talos on a computer. This phone alone sends words only." };
      if (!text) return { error: "Type a message first." };
      if (session && !chat) return { error: "That chat is no longer on this phone." };
      if (chat && busy(chat.id)) return { error: "This chat is still working. Stop it first." };
      var model = String(fields.model || (chat && chat.model) || FIRST);
      if (names().indexOf(model) < 0) return { error: "This phone does not have " + model + ". Pick another model." };
      var now = seconds();
      if (!chat) {
        chat = { id: newId(), title: clip(text.replace(/\s+/g, " "), 60), created: now, updated: now, model: model, messages: [] };
        chats()[chat.id] = chat;
      }
      chat.model = model;
      chat.updated = now;
      chat.messages.push({ role: "user", content: clip(text, WORDS), t: now });
      keepChats();
      var run = runs[chat.id] = { running: true, text: "", notes: [], done: null, since: now, stopped: false, quiet: false,
                                  heard: Date.now(), stopper: null };
      work(chat, run);                               // the answer comes while the page looks
      return { ok: true, session: chat.id };
    }
    if (op === "stop") {
      var running = runs[session];
      if (running && running.running) {
        running.stopped = true;
        if (running.stopper) running.stopper.abort();
      }
      return { ok: !!running };
    }
    if (op === "hide") {
      if (!chat) return { ok: false, error: "That chat is no longer on this phone." };
      if (busy(chat.id)) return { ok: false, error: "This chat is still working. Stop it first." };
      delete chats()[chat.id];
      delete runs[chat.id];
      keepChats();
      return { ok: true, chats: rows() };
    }
    return { ok: false };                            // a picture, or an answer to a question: only a computer has those
  }

  var api = { c: C, k: K, version: "", services: SERVICES, glow: GLOW, call: call, keys: keys, setKey: setKey, refresh: refresh, names: names };
  window.TalosPhone = api;
}());
