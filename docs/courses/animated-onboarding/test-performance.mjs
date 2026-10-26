// Fail-closed score admission checks, independent of browser aesthetics.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { validatePerformance } from './performance.mjs';
const source = path.dirname(fileURLToPath(import.meta.url));
const prepared = process.argv[2];
if (!prepared) throw new Error('Usage: node test-performance.mjs PREPARED_OUTPUT');
const read = async file => JSON.parse(await fs.readFile(file, 'utf8'));
const scene = await read(path.join(prepared, 'scene.json')), inputs = await read(path.join(prepared, 'inputs.json'));
const score = await read(path.join(source, 'performance.json')), cues = await read(path.join(source, 'cue-map.json'));
const hashes = { story: inputs.story_sha256, recordings: inputs.recording_assets };
const valid = validatePerformance(scene, score, cues, hashes);
assert.equal(new Set(valid.cues.map(c => c.turn)).size, scene.turns.length);
const cases = [
  ['voice-drift', (s, c) => { c.turns[0].audio_sha256 = '0'.repeat(64); }, /recording changed/],
  ['unknown-target', s => { s.beats[0].target = 'invented-prop'; }, /Unsupported performance beat/],
  ['unknown-pose', s => { s.beats[0].pose = { flap: 1 }; }, /Unsupported pose channel/],
  ['cue-timing-drift', (s, c) => { c.turns[0].cues[0].start += .05; }, /measured word span/],
  ['missing-turn', s => { s.beats = s.beats.filter(b => !b.cue.startsWith('inspect.')); }, /omits a spoken turn/],
  ['non-finite-pose', s => { s.beats[0].pose.gazeX = NaN; }, /Unsupported pose channel/],
  ['non-finite-offset', s => { s.beats[0].offset = NaN; }, /Invalid beat timing/],
  ['boolean-duration', s => { s.beats[0].duration = true; }, /Invalid beat timing/],
  ['unreachable-hand', s => { s.beats[0].pose.handX = 999; }, /Unsupported pose channel/],
  ['off-story-phrase', (s, c) => { c.turns[0].cues[0].phrase = 'All tests passed'; }, /Invalid phrase cue/],
];
for (const [name, mutate, error] of cases) {
  const s = structuredClone(score), c = structuredClone(cues); mutate(s, c);
  assert.throws(() => validatePerformance(scene, s, c, hashes), error, name);
}
console.log(JSON.stringify({ performanceRefusalsPassed: cases.length, admittedBeats: valid.beats.length, coveredTurns: scene.turns.length, boundary: 'Admission does not establish motion quality' }));
