// Multiplayer sync over WebRTC (PeerJS) — optional, no account, no config, no database.
// Solo play works unchanged; every entry point fails soft and the PeerJS library is loaded
// lazily only when you join a room (solo players download nothing).
//
// Topology: STAR. The room code maps to a well-known peer id; the first person to claim it
// becomes the hub, everyone else connects to it. The hub relays discoveries between peers.
// If the hub leaves, peers re-elect a new one (whoever reclaims the id first) and every peer
// re-sends its set, so the union survives. Each peer persists the union locally (via the game's
// save), so progress spreads through overlapping sessions over time — gossip, not a server.
//
// Consistency: reliable ordered data channels carry every discovery; on top of that, clients
// periodically send a set-hash and, on any mismatch, both sides exchange full sets and union
// (anti-entropy — catches divergence from re-election races).
const Sync = (() => {
  const LIB = "https://unpkg.com/peerjs@1.5.4/dist/peerjs.min.js";
  const COLORS = ["#ff7a59","#4aa3ff","#7dd8a0","#e0a92e","#c084fc","#f472b6","#38bdf8","#fb923c"];
  const hubId = c => "prpp-" + c.toLowerCase().replace(/[^a-z0-9]/g, "");   // room code -> well-known peer id
  const h32 = s => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return h; };
  const key = (n, la, lo) => `${n}|${la.toFixed(5)}|${lo.toFixed(5)}`;

  let peer = null, isHub = false, code = null, me = null, loading = null, reelecting = false;
  const conns = new Map();                         // peerId -> DataConnection (hub: all peers; client: just the hub)
  const found = new Map();                         // key -> {n,la,lo,by,c,t}: this room's union, our source of truth
  let setHash = 0, roster = {}, hbTimer = 0;
  let onFoundCb = () => {}, onRosterCb = () => {}, onSyncedCb = () => {};

  const available = () => true;                    // P2P needs no configuration
  function id(){
    let x = localStorage.getItem("paris-player-id");
    if (!x){ x = "u" + Math.abs(h32(navigator.userAgent + Date.now())) + "" + (Date.now() % 1e6); localStorage.setItem("paris-player-id", x); }
    return x;
  }
  const script = src => new Promise((ok, no) => {
    const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = no; document.head.appendChild(s);
  });
  function init(){
    if (typeof Peer !== "undefined") return Promise.resolve(true);
    if (!loading) loading = script(LIB).then(() => true).catch(() => false);
    return loading;
  }
  const send = (conn, k, d) => { try { conn.send({ k, d }); } catch(e){} };
  const bcast = (k, d, exceptId) => { for (const [pid, c] of conns) if (pid !== exceptId) send(c, k, d); };

  function record(e){                             // add to the union if new; keep the earliest finder. Returns true if new.
    const kk = key(e.n, e.la, e.lo), had = found.get(kk);
    if (had){ if (e.t < had.t){ found.set(kk, e); } return false; }
    found.set(kk, e); setHash ^= h32(kk); return true;
  }
  function ingest(e, live){ if (record(e)){ onFoundCb(e, live); return true; } return false; }
  function stateMsg(){ return { found: [...found.values()], roster }; }
  function pushRoster(){ onRosterCb(roster, me); if (isHub) bcast("roster", roster); }

  function onData(conn, m){
    if (m.k === "found"){ if (ingest(m.d, true) && isHub) bcast("found", m.d, conn.peer); }
    else if (m.k === "bulk"){                     // a batch (join/reconcile): reveal silently, relay only what's new
      const fresh = m.d.filter(e => ingest(e, false));
      if (isHub && fresh.length) bcast("bulk", fresh, conn.peer);
    }
    else if (m.k === "state"){ m.d.found.forEach(e => ingest(e, false)); roster = m.d.roster || roster; onRosterCb(roster, me); onSyncedCb(); }
    else if (m.k === "roster"){ roster = m.d || {}; onRosterCb(roster, me); }
    else if (m.k === "hash" && isHub){ if (m.d !== setHash){ send(conn, "state", stateMsg()); send(conn, "need", 0); } }  // divergence -> reconcile both ways
    else if (m.k === "need"){ send(conn, "bulk", [...found.values()]); }
  }
  function hubAccept(conn){
    conn.on("open", () => {
      conns.set(conn.peer, conn);
      const mt = conn.metadata || {}; if (mt.id) roster[mt.id] = { name: mt.name, color: mt.color };
      send(conn, "state", stateMsg());            // bring the newcomer up to date
      pushRoster();
    });
    conn.on("data", m => onData(conn, m));
    const drop = () => { conns.delete(conn.peer); const mt = conn.metadata || {}; if (mt.id) delete roster[mt.id]; pushRoster(); };
    conn.on("close", drop); conn.on("error", drop);
  }

  function connectRoom(){                          // try to BE the hub; if the id is taken, become a client of whoever holds it
    return new Promise(resolve => {
      roster = {}; conns.clear();
      peer = new Peer(hubId(code), { debug: 0 });
      let settled = false;
      peer.on("open", () => {                      // we claimed the well-known id -> we are the hub
        isHub = true; roster[me.id] = { name: me.name, color: me.color };
        peer.on("connection", hubAccept); onRosterCb(roster, me); onSyncedCb();
        settled = true; resolve(true);
      });
      peer.on("error", e => {
        if (e.type === "unavailable-id"){ peer.destroy(); becomeClient().then(r => { settled = true; resolve(r); }); }
        else if (!settled){ resolve(false); }
      });
      peer.on("disconnected", () => { try { peer.reconnect(); } catch(e){} });  // signaling blip -> reconnect
    });
  }
  function becomeClient(){
    return new Promise(resolve => {
      isHub = false;
      peer = new Peer({ debug: 0 });
      let settled = false;
      peer.on("open", () => {
        const conn = peer.connect(hubId(code), { reliable: true, metadata: { id: me.id, name: me.name, color: me.color } });
        conn.on("open", () => {
          conns.set(conn.peer, conn); roster[me.id] = { name: me.name, color: me.color };
          startHeartbeat();
          settled = true; resolve(true);
        });
        conn.on("data", m => onData(conn, m));
        conn.on("close", onHubLost); conn.on("error", onHubLost);
      });
      peer.on("error", e => {
        if (e.type === "peer-unavailable"){       // hub vanished before we could connect -> try to become the hub
          if (settled) return; settled = true;
          try { peer.destroy(); } catch(err){}
          setTimeout(() => resolve(connectRoom()), 200 + Math.abs(h32(me.id)) % 400);
        } else if (!settled){ settled = true; resolve(false); }
      });
      peer.on("disconnected", () => { try { peer.reconnect(); } catch(e){} });
    });
  }
  function onHubLost(){                            // hub dropped: re-elect (whoever reclaims the id wins), staggered to cut races
    if (reelecting || !code) return; reelecting = true;
    stopHeartbeat(); try { peer && peer.destroy(); } catch(e){}
    setTimeout(() => { reelecting = false; if (code) connectRoom(); }, Math.abs(h32(me.id)) % 700);
  }
  function startHeartbeat(){                       // client -> hub set-hash, for anti-entropy reconcile
    stopHeartbeat();
    hbTimer = setInterval(() => { const hub = conns.values().next().value; if (hub) send(hub, "hash", setHash); }, 5000);
  }
  function stopHeartbeat(){ if (hbTimer){ clearInterval(hbTimer); hbTimer = 0; } }

  async function join(roomCode, name){
    if (!await init()) return false;
    if (code) leave();
    code = roomCode.toUpperCase();
    me = { id: id(), name: (name || "Explorer").slice(0, 24), color: COLORS[Math.abs(h32(id())) % COLORS.length] };
    localStorage.setItem("paris-room", JSON.stringify({ code, name: me.name }));
    const ok = await connectRoom();
    if (!ok){ leave(); return false; }
    return true;
  }
  function pushOne(n, la, lo){                     // a live local discovery: record + broadcast (flashes on others)
    if (!code) return;
    const e = { n, la, lo, by: me.id, c: me.color, t: Date.now() };
    if (record(e)) bcast("found", e);
  }
  function pushMany(list){                         // seed our existing progress on join: one silent batch
    if (!code) return;
    const es = list.map(([n, la, lo]) => ({ n, la, lo, by: me.id, c: me.color, t: Date.now() })).filter(record);
    if (es.length) bcast("bulk", es);
  }
  function leave(){
    stopHeartbeat();
    try { peer && peer.destroy(); } catch(e){}
    peer = null; isHub = false; code = null; conns.clear(); found.clear(); roster = {}; setHash = 0;
    localStorage.removeItem("paris-room");
  }
  function saved(){ try { return JSON.parse(localStorage.getItem("paris-room")); } catch(e){ return null; } }

  return {
    available, join, leave, pushOne, pushMany, saved,
    get code(){ return code; }, get me(){ return me; },
    onFound(f){ onFoundCb = f; }, onRoster(f){ onRosterCb = f; }, onSynced(f){ onSyncedCb = f; },
  };
})();
