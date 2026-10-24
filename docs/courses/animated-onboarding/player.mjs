import { cubicBezier } from 'motion';
import { initial, samplePerformance } from './performance.mjs';
import { character, arm } from './characters.mjs';

const data = JSON.parse(document.querySelector('#scene-data').textContent);
const $ = id => document.getElementById(id);
for (const id of ['sam', 'litai']) { $(`${id}-slot`).innerHTML = character(id); $(`${id}-arm-slot`).innerHTML = arm(id); }
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
let staticMode = reduced.matches, playing = false, time = 0, epoch = 0, generation = 0;
const audio = data.audio ? new Audio(data.audio) : null;
const ease = cubicBezier(.4, 0, .2, 1);
const neutral = id => ({ ...initial[id], handX: id === 'sam' ? 50 : 91, handY: 350, elbowX: 110, elbowY: 307, tilt: 0, lean: 0, gazeX: 3, gazeY: 1, brow: 0, smile: .25, palm: 0 });
function rig(id, t, turn, pose) {
  const moving = !staticMode, active = turn?.speaker === id;
  const s = moving ? pose : neutral(id);
  $(`${id}-body`).setAttribute('transform', `rotate(${s.lean.toFixed(4)} 0 420)`);
  $(`${id}-head`).setAttribute('transform', `rotate(${s.tilt.toFixed(4)} 0 206)`);
  const d = `M73 249 Q${(73 + s.elbowX) / 2} ${s.elbowY - 12} ${s.elbowX} ${s.elbowY} Q${s.elbowX + 10} ${s.handY - 12} ${s.handX} ${s.handY}`;
  for (const part of ['arm-outline', 'sleeve']) $(`${id}-${part}`).setAttribute('d', d);
  $(`${id}-hand`).setAttribute('transform', `translate(${s.handX.toFixed(4)} ${s.handY.toFixed(4)}) rotate(${s.palm.toFixed(4)})`);
  // Blinks occur at authored pauses, once each; no generic idle animation cycle.
  const pauses = id === 'sam' ? ['doctor.yours', 'earn.next', 'surprises.agree'] : ['existing.rewrite', 'green.ship', 'surprises.agree'];
  let blink = 1;
  if (moving) for (const cue of data.performance.cues.filter(c => pauses.includes(c.id))) {
    const delta = t - cue.time - .15;
    if (delta >= 0 && delta < .16) blink = Math.max(.08, Math.abs(delta - .08) / .08);
  }
  $(`${id}-eyes`).setAttribute('transform', `translate(0 ${(119 * (1 - blink)).toFixed(4)}) scale(1 ${blink.toFixed(4)})`);
  $(`${id}-pupils`).setAttribute('transform', `translate(${s.gazeX.toFixed(4)} ${s.gazeY.toFixed(4)})`);
  $(`${id}-brows`).setAttribute('transform', `translate(0 ${s.brow.toFixed(4)})`);
  const energy = data.envelope?.[Math.min(data.envelope.length - 1, Math.floor(t * 50))] || 0;
  $(`${id}-mouth`).setAttribute('ry', moving && active && audio ? (1 + energy * 13).toFixed(4) : '1');
  $(`${id}-smile`).setAttribute('d', `M-18 171 Q0 ${175 + s.smile * 9} 18 171`);
  $(`${id}-badge`).setAttribute('fill', active ? (id === 'sam' ? '#e6ac47' : '#69bfae') : '#dce0d8');
}

function paint(t) {
  const turn = data.turns.find(x => t >= x.start && t < x.end) || data.turns.filter(x => x.start <= t).at(-1) || data.turns[0];
  const states = samplePerformance(data.performance, t, ease);
  for (const name of ['existing', 'doctor', 'plan', 'evidence', 'next']) $(`shot-${name}`).setAttribute('visibility', name === turn.shot ? 'visible' : 'hidden');
  for (const name of ['doctor', 'plan', 'boundary', 'apply', 'shipping', 'build', 'test', 'acceptance', 'receipt']) {
    const value = staticMode ? 0 : states.stage[name];
    $(`focus-${name}`).setAttribute('stroke', value > .1 ? '#ecc16f' : '#68897f');
    $(`focus-${name}`).setAttribute('stroke-width', (1 + value * 2).toFixed(4));
  }
  $('separation').setAttribute('stroke-width', staticMode ? '2' : (2 + states.stage.apply * 2).toFixed(4));
  $('repository').setAttribute('transform', staticMode ? 'translate(0 0)' : `translate(${states.stage.folderX.toFixed(4)} ${states.stage.folderY.toFixed(4)})`);
  $('caption-speaker').textContent = data.speakers[turn.speaker];
  $('caption').textContent = turn.text;
  $('seek').value = String(t); $('elapsed').textContent = `${t.toFixed(1)} / ${data.duration.toFixed(1)} s`;
  $('static-mode').checked = staticMode;
  for (const id of ['sam', 'litai']) rig(id, t, turn, states[id]);
}

async function renderAt(t) {
  if (!Number.isFinite(t) || t < 0 || t > data.duration) throw new RangeError('time is outside the finite scene');
  time = t;
  await new Promise(requestAnimationFrame);
  paint(t);
}

function pause() {
  playing = false; generation++; audio?.pause(); $('play').textContent = 'Play';
}

async function play() {
  if (playing) { pause(); return; }
  if (time >= data.duration) await renderAt(0);
  if (audio) {
    try { audio.currentTime = time; await audio.play(); }
    catch { $('status').textContent = 'Audio could not play. Read the transcript or retry playback.'; return; }
  }
  playing = true; const token = ++generation; epoch = performance.now() - time * 1000; $('play').textContent = 'Pause';
  async function frame() {
    if (!playing || token !== generation) return;
    const t = Math.min(data.duration, audio ? audio.currentTime : (performance.now() - epoch) / 1000);
    await renderAt(t);
    if (t >= data.duration || audio?.ended) pause();
    else requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

$('play').addEventListener('click', play);
$('reset').addEventListener('click', async () => { pause(); await renderAt(0); });
$('seek').max = data.duration;
$('seek').addEventListener('input', async e => { pause(); await renderAt(Number(e.target.value)); });
for (const direction of [-1, 1]) $(direction === -1 ? 'back' : 'next').addEventListener('click', async () => {
  pause(); const starts = data.turns.map(t => t.start);
  await renderAt(direction === 1 ? (starts.find(s => s > time + .01) ?? data.duration) : (starts.filter(s => s < time - .01).at(-1) ?? 0));
});
$('static-mode').addEventListener('change', e => { staticMode = e.target.checked || reduced.matches; paint(time); });
reduced.addEventListener('change', e => { staticMode = e.matches; pause(); paint(time); });
audio?.addEventListener('error', () => { pause(); $('status').textContent = 'Recorded audio is unavailable. The complete transcript remains below.'; });

window.presentation = {
  renderAt,
  setStatic(value) { staticMode = !!value || reduced.matches; paint(time); },
  snapshot() { return { time, staticMode, playing, speaker: $('caption-speaker').textContent, caption: $('caption').textContent, rigs: ['sam', 'litai'].map(id => ['head', 'body', 'arm', 'eyes', 'pupils', 'brows', 'mouth'].map(part => $(`${id}-${part}`).outerHTML)) }; },
  duration: data.duration,
  ready: document.fonts.ready.then(() => renderAt(0)),
};
