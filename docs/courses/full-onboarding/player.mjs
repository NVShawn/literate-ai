import { cubicBezier } from 'motion';
import { character, arm } from '../animated-onboarding/characters.mjs';

const data = JSON.parse(document.querySelector('#scene-data').textContent);
const $ = id => document.getElementById(id);
for (const id of ['sam', 'litai']) { $(`${id}-slot`).innerHTML = character(id); $(`${id}-arm-slot`).innerHTML = arm(id); }
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const ease = cubicBezier(.4, 0, .2, 1);
let staticMode = reduced.matches, playing = false, time = 0, epoch = 0, generation = 0;
const audio = new Audio(data.audio);
const panels = {
  pipeline: ['EARLIER FEEDBACK', 'Keep the rigor. Move the first useful result.', ['BUILD', 'TEST', 'REQUIRED CI'], 'Eight hours is the anecdote, not a benchmark.'],
  authority: ['AUTHORITY AND EVIDENCE', 'A plan is a recipe, not a result.', ['SPEC', 'PLAN', 'BUILD', 'ACCEPTANCE'], 'Independent acceptance remains independent.'],
  adoption: ['KEEP THE REPOSITORY', 'Inspect before applying.', ['DOCTOR', 'READ-ONLY PLAN', 'ACKNOWLEDGED APPLY'], 'Existing source remains authority until evidence changes it.'],
  targets: ['TARGET ≠ WORKER', 'Choose compatible capabilities and execution placement.', ['TARGET', 'CAPABLE HOST', 'ALIGNED WORKER'], 'Catalog choices are not universal qualification.'],
  workflow: ['SUBSTANTIAL PRE-CI WORK', 'Run independent work together; keep dependencies ordered.', ['BUILD', 'INTEGRATION', 'NATIVE', 'ACCEPTANCE'], 'Required CI and native gates still apply.'],
  bookscan: ['BOOKSCAN · RETAINED SOURCE', 'Desktop capture, editing, save, and PDF behavior.', ['CAPTURE', 'EDIT', 'SAVE', 'PDF'], '126 tests: recorded snapshot, not rerun for this film.'],
  aviation: ['AVIATION · RETAINED MONOREPO', 'Mixed-language behavior with explicit analyzer limits.', ['UPLOAD', 'CLEAN TRACK', 'ANALYZE', '273 TESTS'], 'Educational analysis; not approved for flight operations.'],
  elixirssi: ['ELIXIRSSI · EMULATED CM5', 'Fresh evidence across four verifier stages.', ['BUILD', 'TEST', 'DEVICE', '3-NODE ACCEPTANCE'], 'Physical hardware remains unqualified.'],
  lertx: ['LERTX · NATIVE BOUNDARIES', 'Portable feedback and native evidence stay distinct.', ['PORTABLE', 'METAL', 'NVIDIA NATIVE'], 'No mock viewport is presented as a native frame.'],
  daemonarchy: ['DAEMONARCHY · RETAINED CONTRACTS', 'Installation, update, release, and boot claims stay separate.', ['STORAGE INTENT', 'PAYLOAD', 'INTEGRITY', 'BOOT'], 'Full native lifecycle admission remains open.'],
  ci: ['CI STILL GETS A VOTE', 'Earlier evidence prepares the change; it does not waive gates.', ['PRE-CI', 'REVIEW', 'PACKAGE', 'RELEASE'], 'No guaranteed duration reduction is claimed.'],
  'next-step': ['THE NEXT USEFUL STEP', 'Sam owns the method and the repository.', ['DOCTOR', 'REVIEW PLAN', 'ALIGN WORKERS', 'EARN EVIDENCE'], 'Coffee runs beside the graph, not inside it.'],
};
const cardNodes = [];
function setPanel(turn) {
  const [kicker, title, cards, scope] = panels[turn.shot];
  $('panel-kicker').textContent = kicker;
  const panelTitle = $('panel-title');
  panelTitle.textContent = title;
  panelTitle.setAttribute('font-size', '27');
  const measuredWidth = panelTitle.getComputedTextLength();
  if (measuredWidth > 536) panelTitle.setAttribute('font-size', String(Math.max(20, 27 * 536 / measuredWidth)));
  $('scope-label').textContent = scope;
  if ($('panel-cards').dataset.shot !== turn.shot) {
    $('panel-cards').dataset.shot = turn.shot; $('panel-cards').replaceChildren(); cardNodes.length = 0;
    cards.forEach((label, index) => {
      const width = (536 - (cards.length - 1) * 12) / cards.length, x = 372 + index * (width + 12);
      const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      group.innerHTML = `<rect x="${x}" y="294" width="${width}" height="88" rx="12" fill="#254d55" stroke="#68897f"/><text x="${x + width / 2}" y="343" text-anchor="middle" font-size="${label.length > 14 ? 13 : 16}" font-weight="600" fill="#fff7e7">${label}</text>`;
      $('panel-cards').append(group); cardNodes.push(group.querySelector('rect'));
    });
  }
}
function intention(turn) {
  const value = `${turn.performance.play} ${turn.performance.focus}`.toLowerCase();
  if (/mug|coffee|laundry|spice|garage|thinking|excuse/.test(value)) return 'props';
  if (/source|folder|repository/.test(value)) return 'source';
  if (/look|eyeline|hands still|rests?\b|without another gesture/.test(value)) return 'rest';
  return 'panel';
}
function pose(id, active, intent, reach) {
  const rest = id === 'sam' ? {x:50,y:350,e:110,ey:307} : {x:91,y:350,e:110,ey:307};
  if (!active || intent === 'rest' || staticMode) return rest;
  const target = intent === 'props' ? {x: id === 'sam' ? 150 : 185,y:330,e:125,ey:300} : intent === 'source' ? {x: id === 'sam' ? 132 : 238,y:332,e:125,ey:302} : {x: id === 'sam' ? 205 : 245,y:265,e:145,ey:275};
  const p = ease(reach);
  return {x:rest.x+(target.x-rest.x)*p,y:rest.y+(target.y-rest.y)*p,e:rest.e+(target.e-rest.e)*p,ey:rest.ey+(target.ey-rest.ey)*p};
}
function rig(id, active, intent, reach) {
  const p = pose(id, active, intent, reach), facing = id === 'sam' ? 1 : -1;
  const d = `M73 249 Q${(73+p.e)/2} ${p.ey-12} ${p.e} ${p.ey} Q${p.e+10} ${p.y-12} ${p.x} ${p.y}`;
  for (const part of ['arm-outline','sleeve']) $(`${id}-${part}`).setAttribute('d', d);
  $(`${id}-hand`).setAttribute('transform', `translate(${p.x} ${p.y})`);
  $(`${id}-pupils`).setAttribute('transform', `translate(${active ? facing*3 : -facing*4} 1)`);
  $(`${id}-brows`).setAttribute('transform', `translate(0 ${active ? -1 : 1})`);
  const energy = data.envelope[Math.min(data.envelope.length-1, Math.floor(time*50))] || 0;
  $(`${id}-mouth`).setAttribute('ry', active && !staticMode ? String(1+energy*12) : '1');
  $(`${id}-badge`).setAttribute('fill', active ? (id === 'sam' ? '#e6ac47' : '#69bfae') : '#dce0d8');
  $(`${id}-head`).setAttribute('transform', `rotate(${staticMode ? 0 : active ? -1.5*facing : 1.2*facing} 0 206)`);
  $(`${id}-body`).setAttribute('transform', 'rotate(0 0 420)');
}
function paint(t) {
  const turn = data.turns.filter(item => item.start <= t).at(-1) || data.turns[0];
  const speaking = t < turn.end, progress = Math.max(0, t-turn.start), intent = intention(turn), reach = speaking ? Math.max(0, Math.min(1, progress/1.2, (turn.end-t)/1.2)) : 0; setPanel(turn);
  cardNodes.forEach((node,index) => { const highlighted = intent === 'panel' && index === (data.turns.indexOf(turn)%cardNodes.length); node.setAttribute('stroke', highlighted && !staticMode ? '#ecc16f' : '#68897f'); node.setAttribute('stroke-width', highlighted && !staticMode ? '3' : '1'); });
  const chapter = Number(data.id); for (const [name,start] of Object.entries({coffee:1,laundry:5,spices:7,garage:11,thinking:12})) $(`prop-${name}`).setAttribute('visibility', chapter >= start ? 'visible' : 'hidden');
  $('source-folder').setAttribute('visibility', ['adoption','bookscan','aviation','daemonarchy','next-step'].includes(turn.shot) ? 'visible' : 'hidden');
  $('chapter-number').textContent = `CHAPTER ${data.id} / 12`; $('chapter-title').textContent = data.title;
  $('caption-speaker').textContent = data.speakers[turn.speaker]; $('caption').textContent = turn.text;
  for (const id of ['sam','litai']) rig(id, speaking && turn.speaker === id, intent, reach);
  $('seek').value = String(t); $('elapsed').textContent = `${t.toFixed(1)} / ${data.duration.toFixed(1)} s`; $('static-mode').checked = staticMode;
}
async function renderAt(value) { if (!Number.isFinite(value)||value<0||value>data.duration) throw new RangeError('time outside finite chapter'); time=value; await new Promise(requestAnimationFrame); paint(value); }
function pause(){playing=false;generation++;audio.pause();$('play').textContent='Play';}
async function play(){if(playing){pause();return;}if(time>=data.duration)await renderAt(0);try{audio.currentTime=time;await audio.play();}catch{$('status').textContent='Audio unavailable; use the transcript.';return;}playing=true;const token=++generation;epoch=performance.now()-time*1000;$('play').textContent='Pause';const frame=async()=>{if(!playing||token!==generation)return;await renderAt(Math.min(data.duration,audio.currentTime));if(audio.ended)pause();else requestAnimationFrame(frame);};requestAnimationFrame(frame);}
$('play').addEventListener('click',play);$('reset').addEventListener('click',async()=>{pause();await renderAt(0);});$('seek').max=data.duration;$('seek').addEventListener('input',async event=>{pause();await renderAt(Number(event.target.value));});
for(const direction of [-1,1])$(direction<0?'back':'next').addEventListener('click',async()=>{pause();const starts=data.turns.map(turn=>turn.start);await renderAt(direction>0?(starts.find(start=>start>time+.01)??data.duration):(starts.filter(start=>start<time-.01).at(-1)??0));});
$('static-mode').addEventListener('change',event=>{staticMode=event.target.checked||reduced.matches;paint(time);});reduced.addEventListener('change',event=>{staticMode=event.matches;pause();paint(time);});
window.presentation={renderAt,setStatic(value){staticMode=!!value||reduced.matches;paint(time);},snapshot(){return{time,staticMode,playing,chapter:data.id,speaker:$('caption-speaker').textContent,caption:$('caption').textContent,html:$('stage').innerHTML};},duration:data.duration,ready:document.fonts.ready.then(()=>renderAt(0))};
