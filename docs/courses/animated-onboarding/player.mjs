import { animate } from 'motion';
import { character } from './characters.mjs';

const data = JSON.parse(document.querySelector('#scene-data').textContent);
const $ = id => document.getElementById(id);
for (const id of ['sam', 'litai']) $(`${id}-slot`).innerHTML = character(id);
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
let staticMode = reduced.matches, playing = false, time = 0, epoch = 0, generation = 0;
const audio = data.audio ? new Audio(data.audio) : null;
const states = { sam: { nod: 0, arm: 24 }, litai: { nod: 0, arm: 24 } };
const sequence = [];
for (const turn of data.turns) {
  const duration = Math.min(turn.end - turn.start, 3.2);
  sequence.push([states[turn.speaker], { nod: [0, turn.gesture === 'question' ? -4 : 3, 0], arm: [24, turn.gesture === 'question' ? -8 : -20, 8, 24] }, { at: turn.start, duration, ease: 'easeInOut' }]);
}
const timeline = animate(sequence);
timeline.pause();
timeline.time = 0;

const shots = {
  existing: ['KEEP WHAT WORKS', 'Your repository', 'Original source', 'Existing build and tests', 'Onboarding starts with inspection'],
  doctor: ['CHECK THE HOST', 'litai doctor', 'Python 3.11+ · Git', 'Authenticated coding CLI', 'Language / build tools as selected'],
  plan: ['READ-ONLY FIRST', 'litai onboard adopt', './existing-project', 'Inspect files and readiness', 'Apply separately, with acknowledgement'],
  evidence: ['A PLAN IS NOT A RELEASE', 'Evidence still has to be earned', 'Build', 'Test + independent acceptance', 'Plan readiness ≠ shipping qualification'],
  next: ['YOUR NEXT STEP', 'Inspect. Review. Then decide.', 'Get the exact release wheel', 'Check the host with doctor', 'Read the onboarding plan together'],
};

function rig(id, t, turn) {
  const active = turn?.speaker === id, moving = !staticMode;
  const breath = moving ? Math.sin(t * 2.1 + (id === 'sam' ? 0 : .8)) * 1.4 : 0;
  const nod = moving ? states[id].nod : 0, arm = moving ? states[id].arm : 24;
  $(`${id}-body`).setAttribute('transform', `translate(0 ${breath.toFixed(4)})`);
  $(`${id}-head`).setAttribute('transform', `translate(0 ${breath.toFixed(4)}) rotate(${nod.toFixed(4)} 0 206)`);
  $(`${id}-arm`).setAttribute('transform', `rotate(${arm.toFixed(4)} 73 249)`);
  const phase = (t + (id === 'sam' ? .2 : 1.7)) % 4.9;
  const blink = moving && phase > 4.68 ? Math.max(.08, Math.abs(phase - 4.79) / .11) : 1;
  $(`${id}-eyes`).setAttribute('transform', `translate(0 ${(119 * (1 - blink)).toFixed(4)}) scale(1 ${blink.toFixed(4)})`);
  $(`${id}-pupils`).setAttribute('transform', 'translate(4 0)');
  $(`${id}-brows`).setAttribute('transform', `translate(0 ${moving && active && turn.gesture === 'question' ? -3 : 0})`);
  const energy = data.envelope?.[Math.min(data.envelope.length - 1, Math.floor(t * 50))] || 0;
  $(`${id}-mouth`).setAttribute('ry', moving && active && audio ? (2 + energy * 12).toFixed(4) : '2');
  $(`${id}-badge`).setAttribute('fill', active ? (id === 'sam' ? '#e6ac47' : '#69bfae') : '#dce0d8');
}

function paint(t) {
  const turn = data.turns.find(x => t >= x.start && t < x.end) || data.turns.filter(x => x.start <= t).at(-1) || data.turns[0];
  const shot = shots[turn.shot];
  $('shot-tag').textContent = shot[0]; $('panel-title').textContent = shot[1];
  $('line-1').textContent = shot[2]; $('line-2').textContent = shot[3]; $('panel-note').textContent = shot[4];
  $('caption-speaker').textContent = data.speakers[turn.speaker];
  $('caption').textContent = turn.text;
  $('seek').value = String(t); $('elapsed').textContent = `${t.toFixed(1)} / ${data.duration.toFixed(1)} s`;
  $('static-mode').checked = staticMode;
  for (const id of ['sam', 'litai']) rig(id, t, turn);
}

async function renderAt(t) {
  if (!Number.isFinite(t) || t < 0 || t > data.duration) throw new RangeError('time is outside the finite scene');
  time = t;
  timeline.time = t;
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
  snapshot() { return { time, staticMode, playing, speaker: $('caption-speaker').textContent, caption: $('caption').textContent, rigs: ['sam', 'litai'].map(id => ['head', 'body', 'arm', 'eyes', 'mouth'].map(part => $(`${id}-${part}`).outerHTML)) }; },
  duration: data.duration,
  ready: document.fonts.ready.then(() => renderAt(0)),
};
