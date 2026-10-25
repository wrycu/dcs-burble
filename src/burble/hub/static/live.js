// The live LSO view of one carrier (/live/{agent}/{carrier}), fed by the hub's server-sent events (hub/live.py):
// every aircraft around the ship as the server agent sees it, drawn with the replay's scene (lso3d.js).
//
// It plays DELAY_S behind the newest sample, so there's always a sample after the one being drawn to interpolate
// towards (the server sends about 5 a second per jet, in batches every quarter second).
import {
  $, NM, THREE, buildJet, cameraControls, createCameras, createHud, createPlat, createVoice, createWorld, index,
  linear, poseAt,
} from '/static/lso3d.js';

const DELAY_S = 0.7;
const KEEP_S = 120;  // mission seconds of track kept per jet
const PATH_S = 60;  // the followed jet's path drawn this far back
const STALE_S = 15;  // a jet with no samples for this long has gone
const GRADED_SHOW_S = 60;  // a graded pass stays listed this long (wall clock)

const root = document.getElementById('live');
const canvas = root.querySelector('canvas');
const labels = root.querySelector('.labels');
const statusText = $('#live-status');
const followBar = $('#live-follow');
const gradedList = $('#live-graded');
const hasVoice = root.dataset.voice === '1';

let world = null, cameras = null, views = null, updatePlat = null, updateHud = null;
let anyCalls = false;  // the LSO has said something since the page opened (the sound button shows)
let C = null, state = {}, types = {};
const jets = new Map();  // id -> { id, type, pilot, track, groove, model, label, last }
let calls = [];  // { aircraft, t, text, say, by }
const spoken = new Set();
let follow = 'auto';
let clockOffsetMs = null;  // mission clock (ms) = clockOffsetMs + t * 1000
// The newest mission time received, and when (performance.now()); the playout clock `t` follows it.
let newest = null, newestAt = 0, t = null, last = performance.now();
let connected = false;
const voice = createVoice($('#live-sound'));

// -- the data -----------------------------------------------------------------------------------

function emptyTrack() { return { t: [], x: [], y: [], z: [], heading: [], pitch: [], roll: [] }; }
function emptyGroove() { return { t: [], along: [], lateral: [], glideslope: [], aoa: [] }; }

function trim(series, before) {
  const n = series.t.findIndex((v) => v >= before);
  if (n > 0) for (const k of Object.keys(series)) series[k].splice(0, n);
}

function jetFor(id, type, pilot) {
  let j = jets.get(id);
  if (!j) {
    j = { id, type, pilot, track: emptyTrack(), groove: emptyGroove(), model: null, label: null, last: -Infinity };
    jets.set(id, j);
  }
  j.pilot = pilot;
  if (j.type !== type && j.model) { world.scene.remove(j.model.group); j.model = null; }
  j.type = type;
  return j;
}

function removeJet(id) {
  const j = jets.get(id);
  if (!j) return;
  if (j.model) world.scene.remove(j.model.group);
  j.label?.remove();
  jets.delete(id);
}

function clear() {
  for (const id of [...jets.keys()]) removeJet(id);
  calls = [];
  newest = null;
  t = null;
}

function onCarrier(data) {
  if (world && data.carrier.type !== C.type) { location.reload(); return; }  // another ship: start over
  types = { ...types, ...data.types };
  state = data.state || {};
  if (!world) {
    C = data.carrier;
    build();
  }
  clear();
}

function onUpdate(u) {
  if (!world) return;
  if (u.types) Object.assign(types, u.types);
  if (u.state) state = u.state;
  if (u.clock) clockOffsetMs = Date.parse(`${u.clock}Z`) - u.now * 1000;
  for (const a of u.aircraft) {
    const j = jetFor(a.id, a.type, a.pilot);
    for (const [st, x, y, z, heading, pitch, roll] of a.track) {
      if (j.track.t.length && st <= j.track.t[j.track.t.length - 1]) continue;
      j.track.t.push(st); j.track.x.push(x); j.track.y.push(y); j.track.z.push(z);
      j.track.heading.push(heading); j.track.pitch.push(pitch); j.track.roll.push(roll);
      j.last = st;
    }
    for (const [gt, along, lateral, gs, aoa] of a.groove) {
      if (j.groove.t.length && gt <= j.groove.t[j.groove.t.length - 1]) continue;
      j.groove.t.push(gt); j.groove.along.push(along); j.groove.lateral.push(lateral);
      j.groove.glideslope.push(gs); j.groove.aoa.push(aoa);
    }
    trim(j.track, u.now - KEEP_S);
    trim(j.groove, u.now - KEEP_S);
  }
  for (const id of u.gone) removeJet(id);
  for (const c of u.calls) {
    calls.push(c);
    if (c.say && hasVoice) voice.load(audioUrl(c.say));
  }
  calls = calls.filter((c) => c.t >= u.now - KEEP_S);
  if (!anyCalls && u.calls.some((c) => c.say)) voice.hasCalls = anyCalls = true;
  if (newest == null || u.now > newest) { newest = u.now; newestAt = performance.now(); }
}

function onGraded(g) {
  const li = document.createElement('li');
  const a = document.createElement('a');
  a.href = g.url;
  a.textContent = `${g.pilot}: ${g.name}${g.text ? ` · ${g.text}` : ''}`;
  li.append(a);
  gradedList.prepend(li);
  gradedList.hidden = false;
  setTimeout(() => { li.remove(); gradedList.hidden = !gradedList.children.length; }, GRADED_SHOW_S * 1000);
}

function audioUrl(text) { return `/live/say.wav?text=${encodeURIComponent(text)}`; }

// -- the scene ----------------------------------------------------------------------------------

let pathGeom = null, path = null, pathFor = null, pathAt = 0;

function build() {
  world = createWorld(canvas, C, !!state.night);
  const made = createCameras(world);
  cameras = made.cameras;
  // The whole pattern from above and astern of the ship, the wheel zooming.
  cameras.pattern = {
    label: 'Pattern', key: '7', hint: 'The pattern from above, astern and to port of the ship.',
    update() {
      const eye = new THREE.Vector3(-2500, 3200, 6000).multiplyScalar(1 / this.zoom);
      world.camera.position.copy(eye);
      world.camera.up.set(0, 1, 0);
      world.camera.lookAt(new THREE.Vector3(0, 0, -800));
      world.camera.fov = 50;
    },
    zoom: 1,
  };
  views = cameraControls({ world, cameras, resetChase: made.resetChase, container: $('#live-views'),
    hint: $('#live-hint'), root, canvas });
  updatePlat = createPlat(world, canvas);
  updateHud = createHud();
  pathGeom = new THREE.BufferGeometry();
  path = new THREE.Line(pathGeom, new THREE.LineBasicMaterial({ color: 0xffd040 }));
  world.scene.add(path);
  views.set('lso');
  root.classList.add('ready');
  requestAnimationFrame(frame);
}

function modelFor(j) {
  if (!j.model) {
    const kind = types[j.type] || { model: 'other', hook: null };
    j.model = buildJet(kind.model, kind.hook, !!state.night);
    world.scene.add(j.model.group);
  }
  return j.model;
}

function labelFor(j) {
  if (!j.label) {
    j.label = document.createElement('span');
    j.label.className = 'label';
    labels.append(j.label);
  }
  return j.label;
}

// -- who's followed -----------------------------------------------------------------------------

function inGroove(j, at) {
  const g = j.groove;
  return g.t.length > 0 && at - g.t[g.t.length - 1] < 2 && at >= g.t[0];
}

function distance(j, at) {
  const p = poseAt(j.track, at);
  return Math.hypot(p.x - C.aim[0], p.z - C.aim[1]);
}

function followed(at) {
  const here = [...jets.values()].filter((j) => j.track.t.length && j.track.t[0] <= at);
  if (follow !== 'auto') return here.find((j) => j.id === follow) || null;
  // The jet in the groove closest to the ramp, else the one nearest the ship.
  const groove = here.filter((j) => inGroove(j, at));
  const pool = groove.length ? groove : here;
  let best = null, bestD = Infinity;
  for (const j of pool) {
    const d = groove.length ? linear(j.groove.t, j.groove.along, Math.min(at, j.groove.t[j.groove.t.length - 1])) : distance(j, at);
    if (d != null && d < bestD) { best = j; bestD = d; }
  }
  return best;
}

let followShownAt = 0;
function showFollowBar(at, current) {
  // Redrawn twice a second: "Auto" and a button per jet, nearest first.
  if (performance.now() - followShownAt < 500) return;
  followShownAt = performance.now();
  const here = [...jets.values()].filter((j) => j.track.t.length && j.track.t[0] <= at)
    .map((j) => ({ j, d: distance(j, at) })).sort((a, b) => a.d - b.d);
  const buttons = [['auto', 'Auto', 'Follow the jet in the groove, else the one nearest the ship']]
    .concat(here.map(({ j, d }) => [j.id, `${j.pilot} · ${(d / NM).toFixed(1)} nm`, j.type]));
  followBar.replaceChildren(...buttons.map(([id, text, title]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = text;
    b.title = title;
    b.setAttribute('aria-pressed', follow === id || (id !== 'auto' && follow === 'auto' && current?.id === id && 'mixed'));
    b.addEventListener('click', () => { follow = id; followShownAt = 0; });
    return b;
  }));
  if (follow !== 'auto' && !jets.has(follow)) follow = 'auto';  // the one picked has gone
}

// -- drawing ------------------------------------------------------------------------------------

const projected = new THREE.Vector3();

function frame(now) {
  const dt = Math.min((now - last) / 1000, 0.25);
  last = now;
  if (newest != null) {
    // The newest sample less DELAY_S, carried forward by the wall clock between updates; `t` runs at real time and
    // eases onto that (jumping if far off, e.g. after the mission was paused).
    const target = newest + (now - newestAt) / 1000 - DELAY_S;
    t = t == null || Math.abs(target - t) > 2 ? target : t + dt + (target - t) * 0.05;
  }
  world.resize();
  const at = t ?? 0;
  for (const j of [...jets.values()]) if (j.last < at - STALE_S) removeJet(j.id);
  const current = t == null ? null : followed(at);
  showFollowBar(at, current);
  let view = views.view;
  if (!current && view !== 'free' && view !== 'pattern') view = 'pattern';  // nothing to point at

  for (const j of jets.values()) {
    const shown = j.track.t.length && j.track.t[0] <= at;
    const model = shown ? modelFor(j) : j.model;
    if (!model) continue;
    model.group.visible = !!shown && !(j === current && view === 'cockpit');
    if (!shown) continue;
    const p = poseAt(j.track, at);
    model.place(p);
    model.setAoa(linear(j.groove.t, j.groove.aoa, at), types[j.type]?.on_speed_aoa);
  }
  world.sea.steamed((state.speed || 0) * at);

  const monitor = view === 'plat';
  world.glidepath.visible = !monitor;
  if (current) {
    if (pathFor !== current.id || now - pathAt > 250) {
      // The followed jet's path over the last PATH_S, up to now.
      const tr = current.track, from = index(tr.t, at - PATH_S), to = index(tr.t, at) + 1;
      const pts = [];
      for (let i = from; i <= to && i < tr.t.length; i++) pts.push(new THREE.Vector3(tr.x[i], tr.y[i], -tr.z[i]));
      pathGeom.setFromPoints(pts);
      pathFor = current.id;
      pathAt = now;
    }
    path.visible = !monitor;
  } else {
    path.visible = false;
  }

  const cam = cameras[view];
  cam.update(current ? poseAt(current.track, at) : null, current ? current.model : null);
  if (view === 'free') world.controls.update();
  world.camera.updateProjectionMatrix();
  world.camera.updateMatrixWorld();

  // Names over the jets (not on the PLAT monitor, which has none).
  for (const j of jets.values()) {
    if (!j.model || !j.model.group.visible || monitor) { if (j.label) j.label.hidden = true; continue; }
    const label = labelFor(j);
    projected.copy(j.model.group.position).add(new THREE.Vector3(0, 6, 0)).project(world.camera);
    const visible = projected.z < 1 && Math.abs(projected.x) <= 1.05 && Math.abs(projected.y) <= 1.05;
    label.hidden = !visible;
    if (visible) {
      label.textContent = j.pilot;
      label.classList.toggle('followed', j === current);
      label.style.left = `${(projected.x + 1) / 2 * canvas.clientWidth}px`;
      label.style.top = `${(1 - projected.y) / 2 * canvas.clientHeight}px`;
    }
  }

  // The calls, as the playout clock passes them; the followed jet's latest as the subtitle.
  for (const c of calls) {
    const key = `${c.aircraft}|${c.t}|${c.text}`;
    if (c.t <= at && !spoken.has(key)) {
      spoken.add(key);
      if (at - c.t < 2) voice.say(c.say, c.say && hasVoice ? audioUrl(c.say) : null);
    }
  }
  const mine = current ? calls.filter((c) => c.aircraft === current.id && c.t <= at && at - c.t < 3) : [];
  const call = mine[mine.length - 1];

  if (current) {
    const g = current.groove, aoa = linear(g.t, g.aoa, at);
    const groove = inGroove(current, at);
    updateHud({ along: groove ? linear(g.t, g.along, at) : null, gs: groove ? linear(g.t, g.glideslope, at) : null,
      lateral: groove ? linear(g.t, g.lateral, at) : null, aoa: groove ? aoa : null,
      onSpeed: types[current.type]?.on_speed_aoa, call: call ? `${current.pilot}: ${call.text}` : null,
      range: distance(current, at) });
    if (monitor) {
      const waveoff = calls.some((c) => c.aircraft === current.id && c.t <= at && at - c.t < 30 && /^wave off/i.test(c.say || ''));
      updatePlat({ clock: clockOffsetMs == null ? null : clockOffsetMs + at * 1000, deckWindKt: state.deck_wind_kt,
        before: poseAt(current.track, at - 0.1), after: poseAt(current.track, at + 0.1), pos: current.model.group.position,
        waveoff });
    }
  } else {
    updateHud({ along: null, gs: null, lateral: null, aoa: null, onSpeed: null, call: null, range: null });
  }
  root.classList.toggle('plat', view === 'plat');

  const behind = newest == null ? null : (now - newestAt) / 1000;
  statusText.textContent = !connected ? 'Connecting…'
    : newest == null ? 'Waiting for the server agent…'
      : behind > 5 ? `No news from the server for ${Math.round(behind)} s (the mission may be paused)`
        : `Live · ${jets.size} aircraft around the ship`;

  world.renderer.render(world.scene, world.camera);
  requestAnimationFrame(frame);
}

// -- the feed -----------------------------------------------------------------------------------

const events = new EventSource(root.dataset.src);
events.addEventListener('open', () => { connected = true; });
events.addEventListener('error', () => { connected = false; });  // the browser reconnects by itself
events.addEventListener('carrier', (e) => onCarrier(JSON.parse(e.data)));
events.addEventListener('update', (e) => onUpdate(JSON.parse(e.data)));
events.addEventListener('graded', (e) => onGraded(JSON.parse(e.data)));
// Until the ship is known there's nothing to draw; show the connection state meanwhile.
(function waiting() {
  if (world) return;
  statusText.textContent = connected ? 'Waiting for the server agent…' : 'Connecting…';
  setTimeout(waiting, 500);
})();
