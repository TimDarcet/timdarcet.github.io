// Multiplayer sync over Supabase (hosted Postgres + Realtime) — optional. Solo play works
// unchanged with no config; every entry point fails soft, and supabase-js is loaded lazily only
// when you join a room. The public "anon" key is meant to ship in client JS — access is gated by
// Row-Level Security, not secrecy.
//
// SETUP (one-time, ~5 min, needs a GitHub or email account — no Google):
//   1. supabase.com -> New project (pick a Europe region for low latency). Note the Project URL
//      and the anon/public API key (Project Settings -> API). Paste them into CONFIG below.
//   2. In the SQL editor, paste the contents of supabase.sql (next to this file) and run it.
//   The roster uses Realtime Presence (no table) — it auto-drops players when their tab closes.
const Sync = (() => {
  const CONFIG = {
    url: "https://dihnwuevhtlfhvioeorb.supabase.co",
    key: "sb_publishable_EPlGutV1JbxdnZh4bMhJIQ_z5vJ74m2",   // publishable/anon key (safe to ship: gated by RLS)
  };
  const LIB = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2";
  const COLORS = ["#ff7a59","#4aa3ff","#7dd8a0","#e0a92e","#c084fc","#f472b6","#38bdf8","#fb923c"];
  const h32 = s => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return h; };
  const key = (n, la, lo) => `${n}|${la.toFixed(5)}|${lo.toFixed(5)}`;

  let supa = null, code = null, me = null, loading = null;
  let dbChan = null, presChan = null, roster = {};
  let onFoundCb = () => {}, onRosterCb = () => {}, onSyncedCb = () => {};
  let generation = 0, historySerial = Promise.resolve(), cancelPendingJoin = null;
  const HISTORY_PAGE = 500;

  // Outbox: a durable, deduped queue of pushes. Rows are persisted BEFORE any network attempt and
  // dropped only once the server acks them, so nothing is lost to a tab-close mid-send, and a street
  // queued twice collapses to one entry. Drained on (re)connect and on "online".
  let outbox = new Map();                            // "room|key" -> row
  let flushing = false;
  const okey = r => r.room + "|" + r.key;
  const saveOutbox = () => { try { localStorage.setItem("paris-outbox", JSON.stringify([...outbox.values()])); } catch(e){} };
  const loadOutbox = () => { try { outbox = new Map((JSON.parse(localStorage.getItem("paris-outbox") || "[]")).map(r => [okey(r), r])); } catch(e){ outbox = new Map(); } };
  function queue(rows){                              // durably record rows (deduped) before we try to send them
    for (const r of rows) outbox.set(okey(r), r);
    if (outbox.size > 5000) outbox = new Map([...outbox].slice(-5000));
    saveOutbox();
  }
  async function flush(){                            // upsert queued rows in chunks; drop only what the server confirms
    if (!supa || flushing || !outbox.size) return;
    flushing = true;
    try {
      while (outbox.size){                            // re-read each round so rows queued mid-flush are included
        const chunk = [...outbox.values()].slice(0, 500);
        const { error } = await supa.from("found").upsert(chunk, { onConflict: "room,key", ignoreDuplicates: true });
        if (error) throw error;
        for (const r of chunk) outbox.delete(okey(r));  // ack: these are safely on the server now
        saveOutbox();
      }
    } catch(e){} finally { flushing = false; }
  }

  const available = () => !!(CONFIG.url && CONFIG.key);
  function id(){
    let x = localStorage.getItem("paris-player-id");
    if (!x){ x = (crypto.randomUUID ? crypto.randomUUID() : "u" + Math.abs(h32(navigator.userAgent))); localStorage.setItem("paris-player-id", x); }
    return x;
  }
  const script = src => new Promise((ok, no) => {
    const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = no; document.head.appendChild(s);
  });
  function init(){                                  // lazy-load supabase-js + create the client, once
    if (supa) return Promise.resolve(true);
    if (!available()) return Promise.resolve(false);
    if (!loading) loading = script(LIB)
      .then(() => { supa = window.supabase.createClient(CONFIG.url, CONFIG.key, { realtime: { params: { eventsPerSecond: 20 } } });
                    loadOutbox(); addEventListener("online", flush); return true; })
      .catch(() => { loading = null; return false; });
    return loading;
  }
  const rowToFound = r => ({ n: r.name, la: r.la, lo: r.lo, by: r.by, c: r.color });

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function readHistoryOnce(room, epoch){
    let after = null;
    while (epoch === generation && code === room){
      let q = supa.from("found")
        .select("key,name,la,lo,by,color")
        .eq("room", room)
        .order("key", { ascending: true })
        .limit(HISTORY_PAGE);
      if (after !== null) q = q.gt("key", after);
      const { data, error } = await q;
      if (error) throw error;
      if (epoch !== generation || code !== room) return false;
      const batch = data || [];
      if (!batch.length) return true;
      for (const r of batch) onFoundCb(rowToFound(r), false);
      after = batch[batch.length - 1].key;
    }
    return false;
  }
  async function readHistory(room, epoch){
    let lastError;
    for (let attempt = 0; attempt < 3; attempt++){
      try { return await readHistoryOnce(room, epoch); }
      catch(e){
        lastError = e;
        if (epoch !== generation || code !== room) return false;
        if (attempt < 2) await sleep(500 * Math.pow(2, attempt));
      }
    }
    throw lastError;
  }
  function reconcile(room, epoch){
    historySerial = historySerial.catch(() => {}).then(() => readHistory(room, epoch));
    return historySerial;
  }

  async function join(roomCode, name){
    if (!await init()) return false;
    if (code) leave();
    const room = String(roomCode || "").trim().toUpperCase();
    if (!/^[A-Z0-9]{1,12}$/.test(room)) return false;
    const epoch = ++generation;
    historySerial = Promise.resolve();
    code = room;
    me = { id: id(), name: String(name || "Explorer").slice(0, 24), color: COLORS[Math.abs(h32(id())) % COLORS.length] };
    localStorage.setItem("paris-room", JSON.stringify({ code, name: me.name }));

    let readyDone = false, readyResolve;
    const ready = new Promise(resolve => { readyResolve = resolve; });
    const settleReady = ok => {
      if (readyDone) return;
      readyDone = true;
      clearTimeout(readyTimer);
      if (cancelPendingJoin === cancel) cancelPendingJoin = null;
      readyResolve(ok);
    };
    const cancel = () => settleReady(false);
    cancelPendingJoin = cancel;
    const readyTimer = setTimeout(() => settleReady(false), 30000);

    // Reconcile only after Realtime confirms the subscription. Re-run the ordered, paginated
    // history read on every reconnect; duplicates are harmless because the game stores a union.
    dbChan = supa.channel("db:" + code)
      .on("postgres_changes", { event: "INSERT", schema: "public", table: "found", filter: "room=eq." + code },
          p => onFoundCb(rowToFound(p.new), true))
      .subscribe(status => {
        if (epoch !== generation) return;
        if (status === "SUBSCRIBED"){
          flush();
          reconcile(room, epoch)
            .then(ok => { if (ok && epoch === generation){ onSyncedCb(); settleReady(true); } })
            .catch(() => settleReady(false));
        } else if (["CHANNEL_ERROR", "TIMED_OUT", "CLOSED"].includes(status) && !readyDone){
          settleReady(false);
        }
      });

    // roster via Realtime Presence — auto-drops a player when their tab closes
    roster = {};
    presChan = supa.channel("room:" + code, { config: { presence: { key: me.id } } });
    presChan.on("presence", { event: "sync" }, () => {
      const st = presChan.presenceState(); roster = {};
      for (const k in st){ const m = st[k][0] || {}; roster[k] = { name: m.name, color: m.color }; }
      onRosterCb(roster, me);
    });
    presChan.subscribe(status => { if (status === "SUBSCRIBED") presChan.track({ name: me.name, color: me.color }); });

    if (await ready) return true;
    if (epoch === generation) leave();
    return false;
  }
  function row(n, la, lo){ return { room: code, key: key(n, la, lo), name: n, la, lo, by: me.id, color: me.color }; }
  function pushOne(n, la, lo){                      // a live local discovery; on-conflict-do-nothing keeps the first finder
    if (!code || !supa) return;
    queue([row(n, la, lo)]); flush();               // persisted first, so it survives a failed send
  }
  async function pushMany(list){                    // seed our existing progress on join
    if (!code || !supa || !list.length) return;
    queue(list.map(([n, la, lo]) => row(n, la, lo)));
    await flush();
  }
  function leave(keepSaved){                         // keepSaved: tear down the live channels but remember the room
    generation++;
    if (cancelPendingJoin){ const cancel = cancelPendingJoin; cancelPendingJoin = null; cancel(); }
    try { if (dbChan) supa.removeChannel(dbChan); } catch(e){}
    try { if (presChan) supa.removeChannel(presChan); } catch(e){}
    dbChan = presChan = code = null; roster = {};
    if (!keepSaved) localStorage.removeItem("paris-room");
  }
  function saved(){ try { return JSON.parse(localStorage.getItem("paris-room")); } catch(e){ return null; } }

  return {
    available, join, leave, pushOne, pushMany, saved,
    get code(){ return code; }, get me(){ return me; },
    onFound(f){ onFoundCb = f; }, onRoster(f){ onRosterCb = f; }, onSynced(f){ onSyncedCb = f; },
  };
})();
