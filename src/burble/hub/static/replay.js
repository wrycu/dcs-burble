// The 3D replay of a pass (/passes/{id}/replay), drawn from /passes/{id}/replay.json (cards/replay.py).
//
// Coordinates: the replay data is carrier-local (x starboard, y up from the deck, z forward). three.js
// here is X = x, Y = y, Z = -z, so the ship's bow points along -Z, the way three's cameras look. The
// models are simple shapes built here, about the right size, not DCS's.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const root = document.getElementById('replay');
const canvas = root.querySelector('canvas');
const $ = (sel) => document.querySelector(sel);
const DEG = Math.PI / 180;
const M_TO_FT = 3.28084;
const NM = 1852;

const data = await (await fetch(root.dataset.src)).json();
const C = data.carrier;
const track = data.track;
const groove = data.groove;
const duration = track.t[track.t.length - 1];

// -- interpolation ------------------------------------------------------------------------------

function index(ts, t) {
  let lo = 0, hi = ts.length - 1;
  if (t <= ts[0]) return 0;
  if (t >= ts[hi]) return hi - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (ts[mid] <= t) lo = mid; else hi = mid; }
  return lo;
}

// Cubic Hermite through the samples, slopes from the neighbours (the server's ~5 Hz track rebuilds to
// about a centimetre this way in the groove).
function hermite(ts, vs, t) {
  const n = ts.length;
  if (n === 1) return vs[0];
  if (t <= ts[0]) return vs[0];
  if (t >= ts[n - 1]) return vs[n - 1];
  const i = index(ts, t);
  const slope = (k) => {
    const a = Math.max(k - 1, 0), b = Math.min(k + 1, n - 1);
    return (vs[b] - vs[a]) / (ts[b] - ts[a]);
  };
  const h = ts[i + 1] - ts[i], s = (t - ts[i]) / h;
  const s2 = s * s, s3 = s2 * s;
  return (2 * s3 - 3 * s2 + 1) * vs[i] + (s3 - 2 * s2 + s) * h * slope(i)
    + (-2 * s3 + 3 * s2) * vs[i + 1] + (s3 - s2) * h * slope(i + 1);
}

function linear(ts, vs, t) {
  if (!ts.length || t < ts[0] || t > ts[ts.length - 1]) return null;
  const i = index(ts, t);
  const a = vs[i], b = vs[i + 1];
  if (a == null || b == null) return a ?? b;
  return a + (b - a) * (t - ts[i]) / (ts[i + 1] - ts[i]);
}

function jetAt(t) {
  return {
    x: hermite(track.t, track.x, t), y: hermite(track.t, track.y, t), z: hermite(track.t, track.z, t),
    heading: hermite(track.t, track.heading, t), pitch: hermite(track.t, track.pitch, t),
    roll: hermite(track.t, track.roll, t),
  };
}

// A landing-area point (meters short of the aim point, right of the centerline) in three's coordinates.
function deckPoint(along, lateral = 0, y = 0) {
  const [ax, az] = C.axis;
  const x = C.aim[0] - along * ax + lateral * az, z = C.aim[1] - along * az - lateral * ax;
  return new THREE.Vector3(x, y, -z);
}

// -- scene --------------------------------------------------------------------------------------

const night = data.night;
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
const scene = new THREE.Scene();
const sky = new THREE.Color(night ? 0x070b14 : 0x9cc3e4);
scene.background = sky;
scene.fog = new THREE.Fog(sky, 2500, 16000);
scene.add(new THREE.HemisphereLight(night ? 0x334466 : 0xddeeff, night ? 0x0a0f18 : 0x3a4a5a, night ? 0.5 : 1.6));
const sun = new THREE.DirectionalLight(0xffffff, night ? 0.15 : 2.2);
sun.position.set(-400, 800, -300);
scene.add(sun);

const sea = (() => {
  const size = 256, c = document.createElement('canvas');
  c.width = c.height = size;
  const g = c.getContext('2d');
  g.fillStyle = night ? '#060b14' : '#1d4a6e';
  g.fillRect(0, 0, size, size);
  for (let i = 0; i < 1400; i++) {  // whitecaps and ripples, so the sea's motion shows
    const light = Math.random() < 0.08;
    g.fillStyle = light ? (night ? 'rgba(120,140,170,0.25)' : 'rgba(255,255,255,0.35)')
      : (night ? 'rgba(30,45,70,0.5)' : 'rgba(60,110,150,0.5)');
    g.fillRect(Math.random() * size, Math.random() * size, light ? 2 : 6 + Math.random() * 10, light ? 1 : 2);
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  const tile = 40, extent = 40000;
  tex.repeat.set(extent / tile, extent / tile);
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(extent, extent),
    new THREE.MeshLambertMaterial({ map: tex }));
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = -C.deck_altitude;
  scene.add(mesh);
  return { tex, tile };
})();

function strip(a, b, width, color, y = 0.04) {
  // A flat painted line on the deck from a to b (three Vector3s).
  const len = a.distanceTo(b);
  const m = new THREE.Mesh(new THREE.PlaneGeometry(width, len), new THREE.MeshBasicMaterial({ color }));
  m.rotation.x = -Math.PI / 2;
  m.rotation.z = Math.atan2(-(b.x - a.x), -(b.z - a.z));
  m.position.set((a.x + b.x) / 2, y, (a.z + b.z) / 2);
  return m;
}

function shapeOf(points) {  // carrier-local [x, z] points -> a three Shape in the XY plane (y = -Z)
  const s = new THREE.Shape();
  points.forEach(([x, z], i) => (i ? s.lineTo(x, z) : s.moveTo(x, z)));
  return s;
}

function slab(points, top, bottom, material) {
  const g = new THREE.ExtrudeGeometry(shapeOf(points), { depth: top - bottom, bevelEnabled: false });
  g.rotateX(-Math.PI / 2);  // shape y (forward) -> -Z, extrusion -> up
  const m = new THREE.Mesh(g, material);
  m.position.y = bottom;
  return m;
}

function buildCarrier() {
  // A Nimitz-class outline about the size of the real deck (333 by 77 m), placed by the landing area's
  // ramp and wires from geometry.data. Only the landing area, wires and lens are measured; the rest is
  // drawn to look right.
  const g = new THREE.Group();
  const deckMat = new THREE.MeshLambertMaterial({ color: night ? 0x1c1e21 : 0x45484c });
  const hullMat = new THREE.MeshLambertMaterial({ color: night ? 0x2a2e33 : 0x6c737b });
  const port = (along) => { const p = deckPoint(along, -24); return [p.x, -p.z]; };
  const deck = [[-21, -162], [34, -160], [39, -135], [39, 125], [30, 165], [6, 176], [-14, 168], [-22, 120],
    [-32, 82], port(-125), port(40), [-21, -162]];
  g.add(slab(deck, 0, -1.2, deckMat));
  const hull = [[-17, -158], [31, -158], [34, -100], [34, 110], [22, 150], [4, 168], [-12, 150], [-18, 100]];
  g.add(slab(hull, -1.2, -C.deck_altitude - 2, hullMat));
  // The island, starboard amidships.
  const island = slab([[27, -38], [37, -38], [37, 2], [27, 2]], 22, 0, hullMat);
  g.add(island);
  const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.6, 1, 14), hullMat);
  mast.position.set(32, 29, 14);
  g.add(mast);

  // Landing area: centerline dashes, edge lines, the ramp, the wires (the caught one highlighted).
  const white = night ? 0x8899aa : 0xf2f2f2, yellow = night ? 0x99883a : 0xf2d33a;
  const end = -(Math.hypot(C.landing_area_end[0] - C.aim[0], C.landing_area_end[1] - C.aim[1]));
  const rampAlong = Math.hypot(C.ramp[0] - C.aim[0], C.ramp[1] - C.aim[1]);
  for (let a = rampAlong; a > end; a -= 12) g.add(strip(deckPoint(a), deckPoint(Math.max(a - 6, end)), 0.6, white));
  for (const side of [-12.5, 12.5]) g.add(strip(deckPoint(rampAlong, side), deckPoint(end, side), 0.4, white));
  g.add(strip(deckPoint(rampAlong, -12.5), deckPoint(rampAlong, 12.5), 0.6, yellow));
  C.wires.forEach(([[px, pz], [sx, sz]], i) => {
    const caught = C.caught === i + 1;
    g.add(strip(new THREE.Vector3(px, 0, -pz), new THREE.Vector3(sx, 0, -sz), caught ? 0.35 : 0.18,
      caught ? 0xffb000 : (night ? 0x666666 : 0x222222), 0.12));
  });

  // The lens (IFLOLS) on the port side about abeam the aim point, and the LSO platform aft of it.
  const lens = new THREE.Mesh(new THREE.BoxGeometry(1.2, 3.2, 0.5), new THREE.MeshLambertMaterial({ color: 0x222222 }));
  const lp = deckPoint(-5, -27, 1.8);
  lens.position.copy(lp);
  lens.rotation.y = -C.deck_angle * DEG;
  g.add(lens);
  const ball = new THREE.Mesh(new THREE.SphereGeometry(0.3), new THREE.MeshBasicMaterial({ color: 0xffa020 }));
  ball.position.copy(lp).add(new THREE.Vector3(0, 0.2, 0.3));
  g.add(ball);
  const plat = slab([[0, 0], [4, 0], [4, 6], [0, 6]].map(([x, z]) => {
    const p = deckPoint(36 - z, -25 - x); return [p.x, -p.z];
  }), 0.02, -0.4, new THREE.MeshLambertMaterial({ color: 0x555a60 }));
  g.add(plat);
  return g;
}
scene.add(buildCarrier());

// The ship's wake astern, so it's clear which way it steams.
{
  const c = document.createElement('canvas');
  c.width = 4; c.height = 256;
  const g = c.getContext('2d');
  const grad = g.createLinearGradient(0, 0, 0, 256);
  grad.addColorStop(0, `rgba(255,255,255,${night ? 0.1 : 0.35})`);
  grad.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, 4, 256);
  const tex = new THREE.CanvasTexture(c);
  const wake = new THREE.Mesh(new THREE.PlaneGeometry(36, 1200),
    new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false }));
  wake.rotation.x = -Math.PI / 2;
  wake.position.set(8, -C.deck_altitude + 0.05, 160 + 600);
  scene.add(wake);
}

// -- the jet ------------------------------------------------------------------------------------

function plate(points, thickness) {
  // A flat surface from [span x, forward] points, lying level, `thickness` thick.
  const s = new THREE.Shape();
  points.forEach(([x, f], i) => (i ? s.lineTo(x, f) : s.moveTo(x, f)));
  const g = new THREE.ExtrudeGeometry(s, { depth: thickness, bevelEnabled: false });
  g.rotateX(-Math.PI / 2);
  g.translate(0, -thickness / 2, 0);
  return g;
}

function mirrored(points) { return points.map(([x, f]) => [-x, f]).reverse(); }

function body(profile, sx, sy) {
  // A body of revolution along the jet's axis from [forward, radius] points, squashed by sx/sy.
  const pts = [...profile].sort((a, b) => a[0] - b[0]);  // tail first, so the faces point outward
  const g = new THREE.LatheGeometry(pts.map(([f, r]) => new THREE.Vector2(r, f)), 20);
  g.rotateX(-Math.PI / 2);
  g.scale(sx, sy, 1);
  return g;
}

const SHAPES = {
  // Sizes in meters from the aircraft origin (DCS's, near the center of gravity): x right, y up,
  // forward. The wheels sit 1.8 m below it (the jets rest that high above the deck).
  hornet: {
    color: 0xa3a9ae, eye: [0, 0.75, 3.6],
    fuselage: [[8.6, 0], [7.6, 0.35], [5.6, 0.7], [3.4, 0.9], [1, 1.1], [-3, 1.15], [-6, 1.0], [-8.2, 0.8], [-8.6, 0.7]],
    fuselageScale: [1.25, 0.8],
    canopy: { at: [0, 0.62, 3.4], size: [0.5, 0.45, 1.6] },
    wing: [[1.1, 1.4], [6.15, -1.9], [6.15, -3.3], [1.1, -3.4]],
    lex: [[0.8, 5.4], [2.0, 1.4], [1.1, 1.0], [0.8, 1.0]],
    stab: [[0.9, -5.6], [3.4, -7.7], [3.4, -8.6], [0.9, -8.3]],
    fins: { x: 1.3, y: 0.6, cant: 20, points: [[0, -3.6], [2.6, -6.3], [2.6, -7.5], [0, -6.9]] },
    nozzles: { x: 0.55, f: -8.3, r: 0.45 },
    intakes: { x: 1.05, y: -0.55, from: -1, to: 2.6, size: [0.7, 0.7] },
    noseGear: 5.6, mainGear: { x: 1.55, f: -0.6 },
    navLights: [6.15, 0.1, -2.6],
  },
  tomcat: {
    color: 0xb3b7ba, eye: [0, 0.85, 5.3],
    fuselage: [[9.6, 0], [8.6, 0.35], [6.6, 0.7], [4.6, 0.95], [2, 1.05], [-2, 0.9], [-5, 0.6], [-6, 0.3]],
    fuselageScale: [1.0, 0.85],
    canopy: { at: [0, 0.7, 5.0], size: [0.5, 0.5, 1.8] },
    pancake: [[2.6, 1.5], [2.6, -8.6], [-2.6, -8.6], [-2.6, 1.5]],
    nacelles: { x: 1.65, from: 3.0, to: -9.2, r: 0.62 },
    glove: [[1.0, 6.0], [3.4, 0.4], [3.4, -1.6], [1.0, -1.0]],
    wing: [[3.2, 0.2], [9.75, -2.4], [9.75, -3.5], [3.2, -2.2]],
    stab: [[2.0, -6.9], [4.6, -8.6], [4.6, -9.4], [2.0, -9.2]],
    fins: { x: 1.65, y: 0.6, cant: 5, points: [[0, -4.6], [2.7, -7.3], [2.7, -8.3], [0, -8.4]] },
    nozzles: { x: 1.65, f: -9.4, r: 0.55 },
    noseGear: 6.6, mainGear: { x: 2.5, f: -1.4 },
    navLights: [9.75, 0.1, -3.0],
  },
};

function buildJet(kind, hook) {
  const S = SHAPES[kind] ?? SHAPES.hornet;
  const g = new THREE.Group();
  const mat = new THREE.MeshLambertMaterial({ color: S.color });
  const dark = new THREE.MeshLambertMaterial({ color: 0x3a3d40 });
  const add = (geom, m = mat, [x, y, f] = [0, 0, 0]) => {
    const mesh = new THREE.Mesh(geom, m);
    mesh.position.set(x, y, -f);
    g.add(mesh);
    return mesh;
  };
  add(body(S.fuselage, ...S.fuselageScale));
  const canopy = add(new THREE.SphereGeometry(1, 16, 10),
    new THREE.MeshLambertMaterial({ color: 0x1a2733, transparent: true, opacity: 0.85 }), S.canopy.at);
  canopy.scale.set(...S.canopy.size);
  add(plate(S.wing, 0.18), mat, [0, 0.05, 0]);
  add(plate(mirrored(S.wing), 0.18), mat, [0, 0.05, 0]);
  add(plate(S.stab, 0.12), mat, [0, -0.05, 0]);
  add(plate(mirrored(S.stab), 0.12), mat, [0, -0.05, 0]);
  if (S.lex) { add(plate(S.lex, 0.1), mat, [0, 0.15, 0]); add(plate(mirrored(S.lex), 0.1), mat, [0, 0.15, 0]); }
  if (S.glove) { add(plate(S.glove, 0.3), mat, [0, 0.05, 0]); add(plate(mirrored(S.glove), 0.3), mat, [0, 0.05, 0]); }
  if (S.pancake) add(plate(S.pancake, 0.7), mat, [0, -0.05, 0]);
  if (S.nacelles) {
    const n = S.nacelles;
    for (const side of [-1, 1]) {
      const c = new THREE.CylinderGeometry(n.r, n.r * 0.9, n.from - n.to, 14);
      c.rotateX(Math.PI / 2);
      add(c, mat, [side * n.x, -0.35, (n.from + n.to) / 2]);
    }
  }
  if (S.intakes) {
    const k = S.intakes;
    for (const side of [-1, 1]) add(new THREE.BoxGeometry(k.size[0], k.size[1], k.to - k.from), mat,
      [side * k.x, k.y, (k.from + k.to) / 2]);
  }
  for (const side of [-1, 1]) {
    const f = S.fins;
    const fin = new THREE.Mesh(plate(f.points, 0.12).rotateZ(Math.PI / 2 - side * f.cant * DEG), mat);
    fin.position.set(side * f.x, f.y, 0);
    g.add(fin);
    const noz = new THREE.CylinderGeometry(S.nozzles.r, S.nozzles.r * 1.05, 1.2, 14);
    noz.rotateX(Math.PI / 2);
    add(noz, dark, [side * S.nozzles.x, -0.1, S.nozzles.f]);
  }
  // Gear down and the hook, to the hook point the grading uses.
  const strut = (x, f, top, wheel) => {
    add(new THREE.CylinderGeometry(0.08, 0.08, top - (-1.8 + wheel)), dark, [x, (top + (-1.8 + wheel)) / 2, f]);
    const w = new THREE.CylinderGeometry(wheel, wheel, 0.3, 16);
    w.rotateZ(Math.PI / 2);
    add(w, dark, [x, -1.8 + wheel, f]);
  };
  strut(0, S.noseGear, -0.6, 0.25);
  for (const side of [-1, 1]) strut(side * S.mainGear.x, S.mainGear.f, -0.5, 0.4);
  const [hy, hz] = hook, from = new THREE.Vector3(0, -0.5, -(hz + 1.2)), to = new THREE.Vector3(0, hy, -hz);
  const rod = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, from.distanceTo(to)), dark);
  rod.position.copy(from).add(to).multiplyScalar(0.5);
  rod.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), to.clone().sub(from).normalize());
  g.add(rod);
  // Lights: red port, green starboard wingtips, and the approach light on the nose gear (AOA).
  const [lx, ly, lf] = S.navLights;
  const light = (color, at, r = 0.15) => add(new THREE.SphereGeometry(r, 8, 6), new THREE.MeshBasicMaterial({ color }), at);
  light(0xff2020, [-lx, ly, lf]);
  light(0x20ff40, [lx, ly, lf]);
  const indexer = light(0xffa020, [0, -0.9, S.noseGear], night ? 0.35 : 0.22);
  return { group: g, indexer, eye: S.eye };
}

const jet = buildJet(data.aircraft.model, data.aircraft.hook);
jet.group.rotation.order = 'YXZ';
scene.add(jet.group);

// The hook's ideal glide path from the aim point, and the path flown so far.
const showGuides = { glidepath: true, path: true };
const glidepath = new THREE.Line(new THREE.BufferGeometry().setFromPoints(
  [0, 4000].map((a) => deckPoint(a, 0, a * Math.tan(C.glideslope * DEG)))),
  new THREE.LineDashedMaterial({ color: night ? 0x66aa66 : 0x2e8b57, dashSize: 8, gapSize: 6 }));
glidepath.computeLineDistances();
scene.add(glidepath);
const pathPoints = track.t.map((_, i) => new THREE.Vector3(track.x[i], track.y[i], -track.z[i]));
const pathGeom = new THREE.BufferGeometry().setFromPoints(pathPoints);
const path = new THREE.Line(pathGeom, new THREE.LineBasicMaterial({ color: 0xffd040 }));
scene.add(path);

// -- cameras ------------------------------------------------------------------------------------

const camera = new THREE.PerspectiveCamera(30, 1, 0.3, 40000);
const controls = new OrbitControls(camera, canvas);
controls.enabled = false;
controls.target.copy(deckPoint(0));

const LSO_EYE = deckPoint(36, -26, 1.7);
const VULTURES = new THREE.Vector3(33, 18, 0);
const chase = { pos: null };

function aimFov(camPos, target, frame, zoom) {
  const dist = camPos.distanceTo(target);
  return THREE.MathUtils.clamp(2 * Math.atan(frame / 2 / dist) / DEG / zoom, 0.5, 70);
}

const CAMERAS = {
  plat: {
    label: 'PLAT', key: '1',
    hint: 'The deck camera at the aim point, looking up the glide path: the crosshair is the hook’s ideal glideslope and lineup.',
    update(j, pos) {
      const g = C.glideslope * DEG, [ax, az] = C.axis;
      const eye = deckPoint(0, 0, 0.3);
      camera.position.copy(eye);
      camera.up.set(0, 1, 0);
      camera.lookAt(eye.clone().add(new THREE.Vector3(-ax * Math.cos(g), Math.sin(g), az * Math.cos(g))));
      camera.fov = aimFov(eye, pos, 40, this.zoom);
    },
    zoom: 1,
  },
  lso: {
    label: 'LSO', key: '2', hint: 'From the LSO platform, port side aft.',
    update(j, pos) {
      camera.position.copy(LSO_EYE);
      camera.up.set(0, 1, 0);
      camera.lookAt(pos);
      camera.fov = aimFov(LSO_EYE, pos, 120, this.zoom);
    },
    zoom: 1,
  },
  vultures: {
    label: 'Vultures’ row', key: '3', hint: 'From the island.',
    update(j, pos) {
      camera.position.copy(VULTURES);
      camera.up.set(0, 1, 0);
      camera.lookAt(pos);
      camera.fov = aimFov(VULTURES, pos, 120, this.zoom);
    },
    zoom: 1,
  },
  chase: {
    label: 'Chase', key: '4', hint: 'Behind the jet.',
    update(j, pos) {
      const h = j.heading * DEG;
      const want = pos.clone().add(new THREE.Vector3(-Math.sin(h) * 45 / this.zoom, 9 / this.zoom, Math.cos(h) * 45 / this.zoom));
      chase.pos = chase.pos && chase.pos.distanceTo(want) < 60 ? chase.pos.lerp(want, 0.15) : want;
      camera.position.copy(chase.pos);
      camera.up.set(0, 1, 0);
      camera.lookAt(pos);
      camera.fov = 45;
    },
    zoom: 1,
  },
  cockpit: {
    label: 'Cockpit', key: '5', hint: 'From the pilot’s seat.',
    update() {
      const [x, y, f] = jet.eye;
      camera.position.copy(new THREE.Vector3(x, y, -f).applyEuler(jet.group.rotation).add(jet.group.position));
      camera.quaternion.copy(jet.group.quaternion);
      camera.rotateX(-6 * DEG);  // looking over the nose
      camera.fov = 70 / this.zoom;
    },
    zoom: 1,
  },
  free: {
    label: 'Free', key: '6', hint: 'Drag to orbit, right-drag to pan, scroll to zoom.',
    update() {},
    zoom: 1,
  },
};
let view = 'plat';

function setView(name) {
  if (view === 'free' && name !== 'free') controls.enabled = false;
  if (name === 'free' && view !== 'free') {
    controls.enabled = true;
    camera.fov = 50;
    camera.up.set(0, 1, 0);
  }
  view = name;
  chase.pos = null;
  for (const b of document.querySelectorAll('[data-view]')) b.setAttribute('aria-pressed', b.dataset.view === name);
  $('#replay-hint').textContent = CAMERAS[name].hint;
  root.classList.toggle('plat', name === 'plat');
}

// -- the HUD ------------------------------------------------------------------------------------

const hud = {
  range: $('#hud-range'), gs: $('#hud-gs'), lineup: $('#hud-lineup'), aoa: $('#hud-aoa'),
  ball: $('#hud-ball'), ballBox: $('#hud-ballbox'), call: $('#hud-call'), time: $('#replay-time'),
};
const [aoaLow, aoaHigh] = data.aircraft.on_speed_aoa;

function ft(m) { return Math.round(m * M_TO_FT); }

function updateHud(t) {
  const along = linear(groove.t, groove.along, t);
  const gs = linear(groove.t, groove.glideslope, t);
  const lateral = linear(groove.t, groove.lateral, t);
  const aoa = linear(groove.t, groove.aoa, t);
  const landed = along != null && along < 0;  // past the aim point: on deck, or a bolter going around
  hud.range.textContent = along == null ? '–' : landed ? `${ft(-along)} ft past the aim point`
    : along > 0.25 * NM ? `${(along / NM).toFixed(2)} nm` : `${ft(along)} ft`;
  hud.gs.textContent = gs == null || landed ? '–' : `${Math.abs(ft(gs))} ft ${gs >= 0 ? 'high' : 'low'}`;
  hud.lineup.textContent = lateral == null ? '–' : `${Math.abs(ft(lateral))} ft ${lateral >= 0 ? 'right' : 'left'}`;
  let aoaWord = '';
  if (aoa != null) aoaWord = aoa < aoaLow ? 'fast' : aoa >= aoaHigh ? 'slow' : 'on speed';
  hud.aoa.textContent = aoa == null || landed ? '–' : `${aoa.toFixed(1)}° ${aoaWord}`;
  // The approach light: amber on speed, red fast, green slow.
  jet.indexer.material.color.set(aoa == null ? 0x444444 : aoa < aoaLow ? 0xff2020 : aoa >= aoaHigh ? 0x20ff40 : 0xffa020);
  // The ball: its angle off the glideslope from the hook, up to 1.5 degrees either way, as on the lens.
  const showBall = along != null && gs != null && along > 20;
  hud.ballBox.hidden = !showBall;
  if (showBall) {
    const deg = Math.atan2(gs, along) / DEG;
    const f = THREE.MathUtils.clamp(deg / 1.5, -1, 1);
    hud.ball.style.top = `${50 - f * 45}%`;
    hud.ball.classList.toggle('low', deg < -0.9);
  }
  const call = [...data.calls].reverse().find((c) => c.t <= t && t - c.t < 3);
  hud.call.textContent = call ? call.text : '';
  hud.call.hidden = !call;
  hud.time.textContent = `${t.toFixed(1)} / ${duration.toFixed(1)} s`;
}

// -- playback -----------------------------------------------------------------------------------

let t = 0, playing = true, speed = 1, last = performance.now();
const scrub = $('#replay-scrub');
scrub.max = duration;
scrub.step = 0.01;
const playBtn = $('#replay-play');

function setPlaying(p) {
  playing = p;
  playBtn.textContent = p ? 'Pause' : 'Play';
}

function seek(to) {
  t = THREE.MathUtils.clamp(to, 0, duration);
  chase.pos = null;
}

function resize() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * renderer.getPixelRatio()) || canvas.height !== Math.round(h * renderer.getPixelRatio())) {
    renderer.setSize(w, h, false);
  }
  camera.aspect = w / h;
}

function frame(now) {
  const dt = Math.min((now - last) / 1000, 0.1);
  last = now;
  if (playing) {
    t += dt * speed;
    if (t >= duration) { t = duration; setPlaying(false); }
  }
  scrub.value = t;
  const j = jetAt(t);
  jet.group.position.set(j.x, j.y, -j.z);
  jet.group.rotation.set(j.pitch * DEG, -j.heading * DEG, -j.roll * DEG);
  const pos = jet.group.position;
  sea.tex.offset.y = (C.speed * (data.start + t) / sea.tile) % 1;
  const n = index(track.t, t) + 1;
  pathGeom.setDrawRange(0, showGuides.path ? n + 1 : 0);
  glidepath.visible = showGuides.glidepath;
  resize();
  jet.group.visible = view !== 'cockpit';  // the stand-in canopy and wings would fill the view
  CAMERAS[view].update(j, pos);
  if (view === 'free') controls.update();
  camera.updateProjectionMatrix();
  updateHud(t);
  renderer.render(scene, camera);
  requestAnimationFrame(frame);
}

// -- controls -----------------------------------------------------------------------------------

const views = $('#replay-views');
for (const [name, cam] of Object.entries(CAMERAS)) {
  const b = document.createElement('button');
  b.type = 'button';
  b.dataset.view = name;
  b.textContent = cam.label;
  b.title = `${cam.hint} (${cam.key})`;
  b.addEventListener('click', () => setView(name));
  views.append(b);
}
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
canvas.addEventListener('wheel', (e) => {
  if (view === 'free') return;  // OrbitControls zooms
  e.preventDefault();
  const cam = CAMERAS[view];
  cam.zoom = THREE.MathUtils.clamp(cam.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15), 0.3, 12);
}, { passive: false });
document.addEventListener('keydown', (e) => {
  if (e.target.closest('input, select, textarea')) return;
  if (e.key === ' ') { e.preventDefault(); playBtn.click(); return; }
  if (e.key === 'ArrowLeft') seek(t - (e.shiftKey ? 5 : 1));
  else if (e.key === 'ArrowRight') seek(t + (e.shiftKey ? 5 : 1));
  else {
    const name = Object.keys(CAMERAS).find((k) => CAMERAS[k].key === e.key);
    if (name) setView(name);
  }
});

seek(Math.max(data.pass[0] - 1, 0));
setView('plat');
setPlaying(true);
root.classList.add('ready');
requestAnimationFrame(frame);
