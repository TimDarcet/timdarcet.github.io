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
      .then(() => { supa = window.supabase.createClient(CONFIG.url, CONFIG.key, { realtime: { params: { eventsPerSecond: 20 } } }); return true; })
      .catch(() => false);
    return loading;
  }
  const rowToFound = r => ({ n: r.name, la: r.la, lo: r.lo, by: r.by, c: r.color });

  async function join(roomCode, name){
    if (!await init()) return false;
    if (code) leave();
    code = roomCode.toUpperCase();
    me = { id: id(), name: (name || "Explorer").slice(0, 24), color: COLORS[Math.abs(h32(id())) % COLORS.length] };
    localStorage.setItem("paris-room", JSON.stringify({ code, name: me.name }));

    // live discoveries: subscribe BEFORE the history read so nothing inserted in between is missed
    // (duplicates are harmless — the game unions and skips already-revealed streets).
    dbChan = supa.channel("db:" + code)
      .on("postgres_changes", { event: "INSERT", schema: "public", table: "found", filter: "room=eq." + code },
          p => onFoundCb(rowToFound(p.new), true))
      .subscribe();

    // roster via Realtime Presence — auto-drops a player when their tab closes
    roster = {};
    presChan = supa.channel("room:" + code, { config: { presence: { key: me.id } } });
    presChan.on("presence", { event: "sync" }, () => {
      const st = presChan.presenceState(); roster = {};
      for (const k in st){ const m = st[k][0] || {}; roster[k] = { name: m.name, color: m.color }; }
      onRosterCb(roster, me);
    });
    presChan.subscribe(status => { if (status === "SUBSCRIBED") presChan.track({ name: me.name, color: me.color }); });

    // history: pull the room's existing union, reveal it silently, then signal "synced"
    const { data } = await supa.from("found").select("name,la,lo,by,color").eq("room", code);
    (data || []).forEach(r => onFoundCb(rowToFound(r), false));
    onSyncedCb();
    return true;
  }
  function row(n, la, lo){ return { room: code, key: key(n, la, lo), name: n, la, lo, by: me.id, color: me.color }; }
  function pushOne(n, la, lo){                      // a live local discovery; on-conflict-do-nothing keeps the first finder
    if (!code || !supa) return;
    supa.from("found").upsert(row(n, la, lo), { onConflict: "room,key", ignoreDuplicates: true }).then(()=>{}, ()=>{});
  }
  async function pushMany(list){                    // seed our existing progress on join (chunked to keep payloads sane)
    if (!code || !supa || !list.length) return;
    const rows = list.map(([n, la, lo]) => row(n, la, lo));
    for (let i = 0; i < rows.length; i += 500)
      await supa.from("found").upsert(rows.slice(i, i + 500), { onConflict: "room,key", ignoreDuplicates: true });
  }
  function leave(){
    try { if (dbChan) supa.removeChannel(dbChan); } catch(e){}
    try { if (presChan) supa.removeChannel(presChan); } catch(e){}
    dbChan = presChan = code = null; roster = {};
    localStorage.removeItem("paris-room");
  }
  function saved(){ try { return JSON.parse(localStorage.getItem("paris-room")); } catch(e){ return null; } }

  return {
    available, join, leave, pushOne, pushMany, saved,
    get code(){ return code; }, get me(){ return me; },
    onFound(f){ onFoundCb = f; }, onRoster(f){ onRosterCb = f; }, onSynced(f){ onSyncedCb = f; },
  };
})();
