// Project-owned score interpreter. Motion supplies the easing; absolute-time pose
// sampling makes seeking and export independent of playback history.
export const targets = ['folder', 'partner', 'doctor', 'plan', 'apply', 'boundary', 'shipping', 'build', 'test', 'acceptance', 'receipt', 'rest'];
export const initial = {
  sam: { handX: 132, handY: 305, elbowX: 105, elbowY: 309, tilt: 0, lean: 0, gazeX: 3, gazeY: 4, brow: 0, smile: 0, palm: 0 },
  litai: { handX: 91, handY: 350, elbowX: 110, elbowY: 307, tilt: 0, lean: 0, gazeX: 4, gazeY: 0, brow: 0, smile: .2, palm: 0 },
  stage: { folderX: 0, folderY: 0, doctor: 0, plan: 0, boundary: 0, apply: 0, shipping: 0, build: 0, test: 0, acceptance: 0, receipt: 0 },
};
const norm = v => v.toLowerCase().replace(/[^a-z0-9]/g, '');
export function validatePerformance(scene, score, cues, hashes) {
  if (score.story_sha256 !== hashes.story || cues.story_sha256 !== hashes.story) throw new Error('Performance story identity changed');
  if (!Array.isArray(score.beats) || !score.beats.length || !Array.isArray(cues.turns)) throw new Error('Missing performance beats or cues');
  const covered = new Set(), ids = new Set(), cueById = new Map();
  for (const turn of scene.turns) {
    const aligned = cues.turns.find(t => t.id === turn.id);
    if (!aligned || !Number.isFinite(aligned.seconds) || aligned.seconds <= 0 || (!scene.animatic && aligned.audio_sha256 !== hashes.recordings[`assets/${turn.id}.wav`])) throw new Error(`Performance recording changed: ${turn.id}`);
    const scale = scene.animatic ? (turn.end - turn.start) / aligned.seconds : 1;
    let prior = -1;
    for (const cue of aligned.cues) {
      if (ids.has(cue.id) || !norm(turn.text).includes(norm(cue.phrase)) || !Number.isFinite(cue.start) || !Number.isFinite(cue.end) || cue.start < prior || cue.end < cue.start || cue.end > aligned.seconds + .01) throw new Error(`Invalid phrase cue: ${cue.id}`);
      const span = cue.word_span, words = aligned.words;
      if (!Array.isArray(span) || span.length !== 2 || !span.every(Number.isInteger) || span[0] < 0 || span[1] < span[0] || !words?.[span[1]] || Math.abs(cue.start - Math.min(aligned.seconds, words[span[0]].start)) > .001 || Math.abs(cue.end - Math.min(aligned.seconds, words[span[1]].end)) > .001) throw new Error(`Cue does not match measured word span: ${cue.id}`);
      ids.add(cue.id); prior = cue.start; cueById.set(cue.id, { ...cue, time: turn.start + cue.start * scale, scale, turn });
    }
  }
  const tracks = Object.fromEntries(Object.keys(initial).map(actor => [actor, Object.fromEntries(Object.entries(initial[actor]).map(([key, value]) => [key, [{ time: 0, value }]]))]));
  let previous = -1;
  for (const beat of score.beats) {
    const cue = cueById.get(beat.cue);
    if (!cue || !targets.includes(beat.target) || !Object.hasOwn(initial, beat.actor) || typeof beat.purpose !== 'string' || !beat.purpose.trim()) throw new Error(`Unsupported performance beat: ${beat.cue}`);
    const at = cue.time + (beat.offset ?? 0) * cue.scale, duration = beat.duration * cue.scale;
    if (typeof beat.duration !== 'number' || !Number.isFinite(at) || at < previous || at < cue.turn.start || !Number.isFinite(duration) || duration < .08 || duration > 1.5 || at + duration > scene.duration || !beat.pose || !Object.keys(beat.pose).length) throw new Error(`Invalid beat timing: ${beat.cue}`);
    previous = at; covered.add(cue.turn.id);
    for (const [key, value] of Object.entries(beat.pose)) {
      const track = tracks[beat.actor][key];
      const bounds = { handX: [-30, 225], handY: [210, 365], elbowX: [70, 160], elbowY: [245, 340], tilt: [-8, 8], lean: [-3, 3], gazeX: [-5, 5], gazeY: [-4, 4], brow: [-4, 4], smile: [0, 1], palm: [-90, 90], folderX: [-15, 15], folderY: [-10, 10] };
      const [minimum, maximum] = bounds[key] || [0, 1];
      if (!track || !Number.isFinite(value) || (value < minimum || value > maximum)) throw new Error(`Unsupported pose channel: ${key}`);
      const last = track.at(-1);
      if (at < last.time) throw new Error(`Overlapping pose channel: ${key}`);
      track.push({ time: at, value: last.value }, { time: at + duration, value });
    }
  }
  if (covered.size !== scene.turns.length) throw new Error('Performance omits a spoken turn');
  return { tracks, cues: [...cueById.values()].map(({ turn, ...cue }) => ({ ...cue, turn: turn.id })), beats: score.beats };
}
export function samplePerformance(performance, time, ease) {
  const result = {};
  for (const [actor, channels] of Object.entries(performance.tracks)) {
    result[actor] = {};
    for (const [key, frames] of Object.entries(channels)) {
      let value = frames[0].value;
      for (let i = 1; i < frames.length; i++) {
        const next = frames[i], previous = frames[i - 1];
        if (time >= next.time) { value = next.value; continue; }
        if (time > previous.time) {
          const progress = ease((time - previous.time) / (next.time - previous.time));
          value = previous.value + (next.value - previous.value) * progress;
        }
        break;
      }
      result[actor][key] = value;
    }
  }
  return result;
}
