// The 3D replay of a pass (/passes/{id}/replay), drawn from /passes/{id}/replay.json (cards/replay.py) with the
// scene, jets, and cameras of lso3d.js.
import {
  $, THREE, cameraControls, createCameras, createHud, createPlat, createVoice, createWorld, buildJet, index,
  linear, poseAt,
} from '/static/lso3d.js';

const root = document.getElementById('replay');
const canvas = root.querySelector('canvas');

const data = await (await fetch(root.dataset.src)).json();
const C = data.carrier;
const track = data.track;
const groove = data.groove;
const duration = track.t[track.t.length - 1];
const jetAt = (t) => poseAt(track, t);

const world = createWorld(canvas, C, data.night);
const { scene, camera, controls, renderer } = world;
const jet = buildJet(data.aircraft.model, data.aircraft.hook, data.night);
scene.add(jet.group);

// The path flown so far.
const showGuides = { glidepath: true, path: true };
const pathPoints = track.t.map((_, i) => new THREE.Vector3(track.x[i], track.y[i], -track.z[i]));
const pathGeom = new THREE.BufferGeometry().setFromPoints(pathPoints);
const path = new THREE.Line(pathGeom, new THREE.LineBasicMaterial({ color: 0xffd040 }));
scene.add(path);

const { cameras, resetChase } = createCameras(world);
const views = cameraControls({ world, cameras, resetChase, container: $('#replay-views'), hint: $('#replay-hint'),
  root, canvas });
const updatePlat = createPlat(world, canvas);
const updateHud = createHud();
const clockStart = data.clock ? Date.parse(`${data.clock}Z`) : null;  // read as given, shown as given
const waveoffAt = data.calls.find((c) => /^wave off/i.test(c.say || ''))?.t ?? Infinity;

// -- the lineup plot ----------------------------------------------------------------------------

// The trap card's lineup plot below the replay (cards.svg.render_lineup): the track is drawn as far as the jet
// has flown, the jet's marker on it, and each lineup call shown once the jet gets to where it was made.
const lineupSvg = $('#replay-lineup svg');
const lineup = lineupSvg && (() => {
  const d = lineupSvg.dataset, num = (k) => parseFloat(d[k]);
  return {
    reveal: lineupSvg.querySelector('#lineup-reveal-rect'), now: lineupSvg.querySelector('#lineup-now'),
    marks: [...lineupSvg.querySelectorAll('.tc-mark')].map((el) => ({ el, along: parseFloat(el.dataset.along) })),
    near: num('near'), far: num('far'), left: num('padL'), width: num('plotW'),
    half: num('half'), top: num('top'), h: num('h'),
  };
})();

function updateLineup(t) {
  if (!lineup) return;
  const L = lineup, end = groove.t[groove.t.length - 1];
  const along = t > end ? -Infinity : linear(groove.t, groove.along, t) ?? Infinity;  // before the groove: not there yet
  const px = (a) => L.left + (L.far - a) / (L.far - L.near) * L.width;
  L.reveal.setAttribute('width', Math.max(0, Math.min(px(along), L.left + L.width) - L.left));
  for (const m of L.marks) m.el.classList.toggle('tc-hidden', m.along < along);
  const lateral = linear(groove.t, groove.lateral, t);
  const shown = lateral != null && along <= L.far && along >= L.near;
  L.now.setAttribute('visibility', shown ? 'visible' : 'hidden');
  if (shown) {
    L.now.setAttribute('cx', px(along));
    L.now.setAttribute('cy', L.top + (THREE.MathUtils.clamp(lateral, -L.half, L.half) + L.half) / (2 * L.half) * L.h);
  }
}

// -- the LSO's voice ----------------------------------------------------------------------------

const voice = createVoice($('#replay-sound'));
for (const c of data.calls) voice.load(c.audio);
voice.hasCalls = data.calls.some((c) => c.say);

// -- playback -----------------------------------------------------------------------------------

let t = 0, playing = true, speed = 1, last = performance.now();
const scrub = $('#replay-scrub');
scrub.max = duration;
scrub.step = 0.01;
const playBtn = $('#replay-play');
const timeText = $('#replay-time');

function setPlaying(p) {
  playing = p;
  playBtn.textContent = p ? 'Pause' : 'Play';
  if (!p) voice.hush();
}

function seek(to) {
  t = THREE.MathUtils.clamp(to, 0, duration);
  resetChase();
  voice.hush();
}

function frame(now) {
  const dt = Math.min((now - last) / 1000, 0.1);
  last = now;
  if (playing) {
    const before = t;
    t = Math.min(t + dt * speed, duration);
    for (const c of data.calls) if (before < c.t && c.t <= t) voice.say(c.say, c.audio, speed);
    if (t >= duration) setPlaying(false);
  }
  scrub.value = t;
  const j = jetAt(t);
  jet.place(j);
  const pos = jet.group.position;
  world.sea.steamed(C.speed * (data.start + t));
  const n = index(track.t, t) + 1;
  const view = views.view;
  const monitor = view === 'plat';  // the deck camera's monitor shows neither line
  pathGeom.setDrawRange(0, showGuides.path && !monitor ? n + 1 : 0);
  world.glidepath.visible = showGuides.glidepath && !monitor;
  world.resize();
  jet.group.visible = view !== 'cockpit';  // the stand-in canopy and wings would fill the view
  cameras[view].update(j, jet);
  if (view === 'free') controls.update();
  camera.updateProjectionMatrix();
  if (monitor) {
    updatePlat({ clock: clockStart == null ? null : clockStart + t * 1000, deckWindKt: data.deck_wind_kt,
      before: jetAt(t - 0.1), after: jetAt(t + 0.1), pos, waveoff: t >= waveoffAt });
  }
  const aoa = linear(groove.t, groove.aoa, t);
  jet.setAoa(aoa, data.aircraft.on_speed_aoa);
  const call = [...data.calls].reverse().find((c) => c.t <= t && t - c.t < 3);
  updateHud({ along: linear(groove.t, groove.along, t), gs: linear(groove.t, groove.glideslope, t),
    lateral: linear(groove.t, groove.lateral, t), aoa, onSpeed: data.aircraft.on_speed_aoa, call: call?.text });
  timeText.textContent = `${t.toFixed(1)} / ${duration.toFixed(1)} s`;
  updateLineup(t);
  renderer.render(scene, camera);
  requestAnimationFrame(frame);
}

// -- controls -----------------------------------------------------------------------------------

playBtn.addEventListener('click', () => {
  if (!playing && t >= duration) seek(0);
  setPlaying(!playing);
});
scrub.addEventListener('input', () => seek(parseFloat(scrub.value)));
$('#replay-speed').addEventListener('change', (e) => { speed = parseFloat(e.target.value); });
$('#replay-restart').addEventListener('click', () => { seek(0); setPlaying(true); });
$('#replay-groove').addEventListener('click', () => { seek(Math.max(data.pass[0] - 1, 0)); setPlaying(true); });
for (const box of document.querySelectorAll('[data-guide]')) {
  box.addEventListener('change', () => { showGuides[box.dataset.guide] = box.checked; });
}
document.addEventListener('keydown', (e) => {
  if (e.target.closest('input, select, textarea')) return;
  if (e.key === ' ') { e.preventDefault(); playBtn.click(); return; }
  if (e.key === 'ArrowLeft') seek(t - (e.shiftKey ? 5 : 1));
  else if (e.key === 'ArrowRight') seek(t + (e.shiftKey ? 5 : 1));
});

seek(Math.max(data.pass[0] - 1, 0));
views.set('plat');
setPlaying(true);
root.classList.add('ready');
requestAnimationFrame(frame);
