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
scene.add(new THREE.HemisphereLight(night ? 0x334466 : 0xddeeff, night ? 0x0a0f18 : 0x3a4a5a, night ? 0.5 : 2.0));
const sun = new THREE.DirectionalLight(0xffffff, night ? 0.15 : 2.0);
sun.position.set(200, 1000, -300);  // high and a little ahead: a jet in the groove faces the ship, lit as the LSO sees it
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

// The jets, in landing configuration (gear, hook, and flaps down), from their published dimensions: the Hornet
// 17.1 m long and 12.3 m across its wingtip rails, the Tomcat 19.1 m long and 19.5 m across with its wings
// forward (20 degrees, as they are for landing). Meters from the aircraft origin (DCS's, near the center of
// gravity): x right, y up, forward. The wheels sit 1.8 m below it (the jets rest that high above the deck).
const JETS = {
  hornet: {
    color: 0x9da3a9, radome: 0x7c8288, eye: [0, 0.8, 3.7], noseGear: 5.6, mainGear: { x: 1.6, f: -0.6 },
    navLights: [6.0, 0.05, -2.3],
    build({ add, both, flap, mat, dark, glass, radome }) {
      const nose = [[8.7, 0], [8.2, 0.22], [7.2, 0.45], [6.0, 0.62], [4.8, 0.75], [3.5, 0.85], [2, 0.95], [0, 1.05],
        [-3, 1.1], [-5.5, 1.05], [-7.5, 0.9], [-8.4, 0.8]];
      add(body(nose, 1.15, 0.85), mat);
      add(body(nose.filter(([f]) => f >= 6.4).map(([f, r]) => [f, r * 1.02]), 1.15, 0.85), radome);
      add(new THREE.SphereGeometry(1, 20, 12), glass, [0, 0.72, 3.7]).scale.set(0.48, 0.5, 1.7);
      // The leading-edge extensions, from beside the cockpit to the wing root, over the intakes.
      both(plate([[0.55, 6.6], [0.9, 5.0], [1.6, 2.8], [2.25, 1.0], [2.3, 0.2], [0.6, 0.2]], 0.12), mat, [0, 0.25, 0]);
      both(new THREE.BoxGeometry(0.75, 0.85, 3.2), mat, [1.35, -0.45, 0]);
      both(plate([[1.3, 0.5], [6.0, -1.9], [6.0, -2.75], [1.3, -2.7]], 0.16), mat, [0, 0.05, 0]);
      both(flap(1.4, -2.7, 3.9, -2.72, 0.85, 40), mat, [0, 0.05, 0]);  // flaps
      both(flap(3.9, -2.72, 5.9, -2.75, 0.65, 25), mat, [0, 0.05, 0]);  // drooped ailerons
      const rail = new THREE.CylinderGeometry(0.07, 0.07, 2.8, 8).rotateX(Math.PI / 2);
      both(rail, dark, [6.05, 0.05, -1.6]);
      both(plate([[1.1, -5.7], [3.4, -7.4], [3.4, -8.3], [1.1, -8.4]], 0.1), mat, [0, -0.15, 0]);
      both(fin([[0, -3.2], [2.7, -5.7], [2.7, -6.8], [0, -6.6]], 20), mat, [1.4, 0.5, 0]);
      both(nozzle(0.5), dark, [0.62, -0.1, -8.6]);
    },
  },
  tomcat: {
    color: 0xb7bcc0, radome: 0x8f969c, eye: [0, 0.95, 5.9], noseGear: 6.8, mainGear: { x: 2.5, f: -1.0 },
    navLights: [9.75, 0.2, -2.8],
    build({ add, both, flap, mat, dark, glass, radome }) {
      const nose = [[9.9, 0], [9.4, 0.25], [8.4, 0.5], [7.0, 0.75], [5.5, 0.9], [4.0, 1.0], [2.5, 1.0], [1, 0.9], [-1, 0.7]];
      add(body(nose, 1.0, 0.9), mat);
      add(body(nose.filter(([f]) => f >= 8).map(([f, r]) => [f, r * 1.02]), 1.0, 0.9), radome);
      add(new THREE.SphereGeometry(1, 20, 12), glass, [0, 0.8, 5.0]).scale.set(0.5, 0.52, 2.3);  // two seats
      // The flat "pancake" between the engines, the fixed gloves, and the engine nacelles with box intakes.
      add(plate([[2.4, 0.8], [2.4, -7.6], [1.0, -9.6], [-1.0, -9.6], [-2.4, -7.6], [-2.4, 0.8]], 0.55), mat, [0, 0.05, 0]);
      both(plate([[0.8, 7.0], [1.6, 4.5], [2.9, 0.6], [2.9, -1.4], [0.8, -1.0]], 0.3), mat, [0, 0.2, 0]);
      both(new THREE.CylinderGeometry(0.62, 0.62, 11, 16).rotateX(Math.PI / 2), mat, [1.75, -0.45, -3.1]);
      both(new THREE.BoxGeometry(1.0, 1.35, 1.6), mat, [1.85, -0.3, 3.0]);
      // Wings forward, with the full-span flaps down.
      both(plate([[2.9, 0.3], [9.75, -2.2], [9.75, -3.4], [2.9, -2.6]], 0.16), mat, [0, 0.2, 0]);
      both(flap(3.2, -2.63, 9.5, -3.36, 0.75, 35), mat, [0, 0.2, 0]);
      both(plate([[2.2, -7.2], [4.8, -8.8], [4.8, -9.6], [2.2, -9.7]], 0.1), mat, [0, -0.2, 0]);
      both(fin([[0, -4.2], [2.9, -7.0], [2.9, -8.0], [0, -8.3]], 5), mat, [1.75, 0.45, 0]);
      both(fin([[0, -5.6], [0.8, -6.3], [0.8, -7.3], [0, -7.3]], 165), mat, [1.75, -1.0, 0]);  // ventral fins
      both(nozzle(0.58), dark, [1.75, -0.45, -9.1]);
    },
  },
};

function fin(points, cant) {
  // A fin from [height, forward] points, standing up and canted outward (to the right) by `cant` degrees.
  return plate(points, 0.12).rotateZ(Math.PI / 2 - cant * DEG);
}

function nozzle(r) {
  return new THREE.CylinderGeometry(r, r * 1.05, 1.1, 16).rotateX(Math.PI / 2);
}

function flap(x0, te0, x1, te1, chord, down) {
  // A flap hinged on the wing's trailing edge from (x0, te0) to (x1, te1) [span, forward], `down` degrees.
  const g = plate([[0, 0], [x1 - x0, te1 - te0], [x1 - x0, te1 - te0 - chord], [0, -chord]], 0.08);
  const hinge = new THREE.Vector3(x1 - x0, 0, -(te1 - te0)).normalize();
  g.applyMatrix4(new THREE.Matrix4().makeRotationAxis(hinge, down * DEG));
  return g.translate(x0, 0, -te0);
}

function mirror(geom) {
  // The geometry's mirror image across the jet's centerline, its triangles turned back to face outward.
  const g = geom.clone().scale(-1, 1, 1);
  const index = g.getIndex();
  if (index) {
    const a = index.array;
    for (let i = 0; i < a.length; i += 3) [a[i + 1], a[i + 2]] = [a[i + 2], a[i + 1]];
  } else {
    for (const attr of Object.values(g.attributes)) {
      const n = attr.itemSize, a = attr.array;
      for (let i = 0; i < attr.count; i += 3) {
        for (let k = 0; k < n; k++) [a[(i + 1) * n + k], a[(i + 2) * n + k]] = [a[(i + 2) * n + k], a[(i + 1) * n + k]];
      }
    }
  }
  return g;
}

function buildJet(kind, hook) {
  const J = JETS[kind] ?? JETS.hornet;
  const g = new THREE.Group();
  // Double-sided for the thin surfaces (flaps, fins) seen edge-on and from below.
  const material = (color, extra = {}) => new THREE.MeshLambertMaterial({ color, side: THREE.DoubleSide, ...extra });
  const mat = material(J.color), dark = material(0x34373a), radome = material(J.radome);
  const glass = material(0x1a2733, { transparent: true, opacity: 0.85 });
  const add = (geom, m = mat, [x, y, f] = [0, 0, 0]) => {
    const mesh = new THREE.Mesh(geom, m);
    mesh.position.set(x, y, -f);
    g.add(mesh);
    return mesh;
  };
  // A part on the right, and its mirror image on the left.
  const both = (geom, m, [x, y, f]) => {
    add(geom, m, [x, y, f]);
    add(mirror(geom), m, [-x, y, f]);
  };
  J.build({ add, both, flap, mat, dark, glass, radome });
  // Gear down and the hook, to the hook point the grading uses.
  const strut = (x, f, top, wheel) => {
    add(new THREE.CylinderGeometry(0.08, 0.08, top - (-1.8 + wheel)), dark, [x, (top + (-1.8 + wheel)) / 2, f]);
    add(new THREE.CylinderGeometry(wheel, wheel, 0.3, 16).rotateZ(Math.PI / 2), dark, [x, -1.8 + wheel, f]);
  };
  strut(0, J.noseGear, -0.6, 0.25);
  for (const side of [-1, 1]) strut(side * J.mainGear.x, J.mainGear.f, -0.5, 0.4);
  const [hy, hz] = hook, from = new THREE.Vector3(0, -0.5, -(hz + 1.2)), to = new THREE.Vector3(0, hy, -hz);
  const rod = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, from.distanceTo(to)), dark);
  rod.position.copy(from).add(to).multiplyScalar(0.5);
  rod.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), to.clone().sub(from).normalize());
  g.add(rod);
  // Lights: red port, green starboard wingtips, and the approach light on the nose gear (AOA).
  const [lx, ly, lf] = J.navLights;
  const light = (color, at, r = 0.15) => add(new THREE.SphereGeometry(r, 8, 6), new THREE.MeshBasicMaterial({ color }), at);
  light(0xff2020, [-lx, ly, lf]);
  light(0x20ff40, [lx, ly, lf]);
  const indexer = light(0xffa020, [0, -0.9, J.noseGear], night ? 0.35 : 0.22);
  return { group: g, indexer, eye: J.eye };
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

// The PLAT camera's vertical field of view: 10 degrees across a 4:3 (1274 x 928) picture.
const PLAT_FOV = 2 * Math.atan(Math.tan(5 * DEG) * 928 / 1274) / DEG;

function aimFov(camPos, target, frame, zoom) {
  const dist = camPos.distanceTo(target);
  return THREE.MathUtils.clamp(2 * Math.atan(frame / 2 / dist) / DEG / zoom, 0.5, 70);
}

const CAMERAS = {
  plat: {
    label: 'PLAT', key: '1',
    hint: 'The deck camera at the aim point looking up the glide path, as the LSO\u2019s PLAT monitor shows it: the '
      + 'crosshair\u2019s centre is the hook\u2019s ideal glideslope and centerline. Top: date, time, C (deck clear), '
      + 'the hull number, wind over the deck (kt). Bottom: closing speed (kt), W once waved off, range (ft), sink rate (ft/s).',
    update(j, pos) {
      const g = C.glideslope * DEG, [ax, az] = C.axis;
      const eye = deckPoint(0, 0, 0.3);
      camera.position.copy(eye);
      camera.up.set(0, 1, 0);
      camera.lookAt(eye.clone().add(new THREE.Vector3(-ax * Math.cos(g), Math.sin(g), az * Math.cos(g))));
      // A fixed lens, about 10 degrees across the 4:3 picture (the crosshair's 1-degree gap is a tenth of its
      // width on the monitor), so the jet starts small and grows as it comes in; the wheel zooms.
      camera.fov = THREE.MathUtils.clamp(PLAT_FOV / this.zoom, 0.5, 70);
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

// -- the PLAT monitor ---------------------------------------------------------------------------

// Laid out as the carrier's deck camera monitor (and DCS's Supercarrier) shows it: the picture in grey (CSS), a
// crosshair with a gap one degree wide around the jet, and the camera's data overlaid (the page's .platframe).
const plat = Object.fromEntries(['date', 'time', 'status', 'hull', 'wind', 'speed', 'waveoff', 'distance', 'sink',
  'left', 'right', 'top', 'bottom'].map((k) => [k, $(`#plat-${k}`)]));
const PLAT_ASPECT = 1274 / 928;
const hullNumber = C.type === 'Stennis' ? '74' : C.type === 'Forrestal' ? '59' : (C.type.match(/\d+/) || ['XX'])[0];
const clockStart = data.clock ? Date.parse(`${data.clock}Z`) : null;  // read as given, shown as given
const waveoffAt = data.calls.find((c) => /^wave off/i.test(c.say || ''))?.t ?? Infinity;
const digits = (n, width) => String(Math.max(0, Math.floor(n))).padStart(width, '0').slice(-width);

function place(el, x, y, w, h) {
  Object.assign(el.style, { left: `${x}px`, top: `${y}px`, width: `${Math.max(0, w)}px`, height: `${Math.max(0, h)}px` });
}

function updatePlat(t, pos) {
  if (clockStart != null) {
    const d = new Date(clockStart + t * 1000), two = (n) => String(n).padStart(2, '0');
    plat.date.textContent = `${two(d.getUTCMonth() + 1)}-${two(d.getUTCDate())}-${two(d.getUTCFullYear() % 100)}`;
    plat.time.textContent = `${two(d.getUTCHours())}:${two(d.getUTCMinutes())}:${two(d.getUTCSeconds())}`;
  }
  plat.status.textContent = 'C';
  plat.hull.textContent = hullNumber;
  plat.wind.textContent = data.deck_wind_kt == null ? '' : digits(data.deck_wind_kt, 2);
  // Closing speed and sink rate, relative to the ship.
  const a = jetAt(t - 0.1), b = jetAt(t + 0.1);
  plat.speed.textContent = digits(Math.hypot(b.x - a.x, b.y - a.y, b.z - a.z) / 0.2 * 1.94384, 3);
  plat.sink.textContent = digits(-(b.y - a.y) / 0.2 * M_TO_FT, 2);
  const dist = camera.position.distanceTo(pos);
  plat.distance.textContent = digits(Math.floor(dist * M_TO_FT / 100) * 100, 5);
  plat.waveoff.textContent = t >= waveoffAt ? 'W' : '';
  // The crosshair is fixed on the picture, as on the monitor: the camera looks up the hook's glide path, so its centre
  // is the hook's ideal glideslope and centerline (a jet on it shows its hook in the gap, its body a little above).
  const W = canvas.clientWidth, H = canvas.clientHeight;
  const pxPerTan = (H / 2) / Math.tan(camera.fov * DEG / 2);
  const gateW = 2 * Math.tan(0.5 * DEG) * pxPerTan, gateH = gateW / PLAT_ASPECT;
  const fw = Math.min(W, H * PLAT_ASPECT), cx = fw / 2, cy = H / 2, sz = Math.max(2, H * 0.0035);
  place(plat.left, 0, cy - sz / 2, cx - gateW / 2, sz);
  place(plat.right, cx + gateW / 2, cy - sz / 2, fw - cx - gateW / 2, sz);
  place(plat.top, cx - sz / 2, 0, sz, cy - gateH / 2);
  place(plat.bottom, cx - sz / 2, cy + gateH / 2, sz, H - cy - gateH / 2);
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

// -- the LSO's voice ----------------------------------------------------------------------------

// Each LSO call is spoken as the replay passes it: in the hub's voice when it has one (`audio`), else the
// browser's. Browsers only play sound after a click on the page, so the button says when it's waiting for one.
const sound = { on: true, blocked: false, playing: new Set() };
try { sound.on = localStorage.getItem('burble-replay-sound') !== 'off'; } catch (e) {}
const soundBtn = $('#replay-sound');
const clips = data.calls.map((c) => {
  if (!c.audio) return null;
  const a = new Audio(c.audio);
  a.preload = 'auto';  // asks the hub to render it now, not when it's due
  a.addEventListener('ended', () => sound.playing.delete(a));
  return a;
});
// The browser's own voice, if it has one (Firefox on Linux often has none without speech-dispatcher).
const speech = 'speechSynthesis' in window ? window.speechSynthesis : null;
const hasVoice = () => clips.some(Boolean) || (speech != null && speech.getVoices().length > 0);
speech?.addEventListener?.('voiceschanged', () => showSound());  // the list loads after the page

function showSound() {
  soundBtn.setAttribute('aria-pressed', sound.on);
  soundBtn.textContent = !sound.on ? 'Sound off' : sound.blocked ? 'Sound on (click to allow)' : 'Sound on';
  soundBtn.hidden = !data.calls.some((c) => c.say) || !hasVoice();
}

function hush() {
  for (const a of sound.playing) a.pause();
  sound.playing.clear();
  speech?.cancel();
}

function say(i) {
  const call = data.calls[i];
  if (!sound.on || !call.say) return;
  const clip = clips[i];
  if (clip) {
    clip.currentTime = 0;
    clip.playbackRate = THREE.MathUtils.clamp(speed, 0.5, 4);
    sound.playing.add(clip);
    clip.play().then(() => { if (sound.blocked) { sound.blocked = false; showSound(); } }).catch((e) => {
      sound.playing.delete(clip);
      if (e.name === 'NotAllowedError') { sound.blocked = true; showSound(); }
    });
  } else if (speech) {
    const u = new SpeechSynthesisUtterance(call.say);
    u.rate = THREE.MathUtils.clamp(speed * 1.1, 0.5, 2);
    speech.speak(u);
  }
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
  if (!p) hush();
}

function seek(to) {
  t = THREE.MathUtils.clamp(to, 0, duration);
  chase.pos = null;
  hush();
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
    const before = t;
    t = Math.min(t + dt * speed, duration);
    data.calls.forEach((c, i) => { if (before < c.t && c.t <= t) say(i); });
    if (t >= duration) setPlaying(false);
  }
  scrub.value = t;
  const j = jetAt(t);
  jet.group.position.set(j.x, j.y, -j.z);
  jet.group.rotation.set(j.pitch * DEG, -j.heading * DEG, -j.roll * DEG);
  const pos = jet.group.position;
  sea.tex.offset.y = (C.speed * (data.start + t) / sea.tile) % 1;
  const n = index(track.t, t) + 1;
  const monitor = view === 'plat';  // the deck camera's monitor shows neither line
  pathGeom.setDrawRange(0, showGuides.path && !monitor ? n + 1 : 0);
  glidepath.visible = showGuides.glidepath && !monitor;
  resize();
  jet.group.visible = view !== 'cockpit';  // the stand-in canopy and wings would fill the view
  CAMERAS[view].update(j, pos);
  if (view === 'free') controls.update();
  camera.updateProjectionMatrix();
  if (view === 'plat') updatePlat(t, pos);
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
soundBtn.addEventListener('click', () => {
  // While blocked, this click is what lets the browser play sound: keep it on.
  sound.on = sound.blocked ? true : !sound.on;
  sound.blocked = false;
  if (!sound.on) hush();
  try { localStorage.setItem('burble-replay-sound', sound.on ? 'on' : 'off'); } catch (e) {}
  showSound();
});
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
showSound();
setView('plat');
setPlaying(true);
root.classList.add('ready');
requestAnimationFrame(frame);
