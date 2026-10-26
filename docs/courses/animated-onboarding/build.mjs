// All optional dependencies resolve from the explicitly staged ignored toolchain.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { character, arm } from './characters.mjs';
import { validatePerformance } from './performance.mjs';

const source = path.dirname(fileURLToPath(import.meta.url));
const [outputArg, toolchainArg] = process.argv.slice(2);
if (!outputArg || !toolchainArg) throw new Error('Usage: node build.mjs PREPARED_OUTPUT IGNORED_TOOLCHAIN');
const output = path.resolve(outputArg), toolchain = path.resolve(toolchainArg);
const require = createRequire(path.join(toolchain, 'package.json'));
const { build } = require('esbuild');
const sha = v => createHash('sha256').update(v).digest('hex');
const inputs = JSON.parse(await fs.readFile(path.join(output, 'inputs.json'), 'utf8'));
async function requireHash(file, expected) {
  if (!expected || sha(await fs.readFile(file)) !== expected) throw new Error(`Prepared input changed: ${path.basename(file)}`);
}
await requireHash(path.join(source, 'story.json'), inputs.story_sha256);
await requireHash(path.join(output, 'scene.json'), inputs.scene_sha256);
await requireHash(path.join(output, 'audio.wav'), inputs.audio_sha256);
for (const [name, hash] of Object.entries(inputs.recording_assets || {})) {
  if (!/^assets\/[a-z][a-z0-9-]{0,39}\.wav$/.test(name)) throw new Error('Unsafe recording asset path');
  await requireHash(path.join(output, name), hash);
}
await requireHash(path.join(toolchain, 'package-lock.json'), sha(await fs.readFile(path.join(source, 'package-lock.json'))));
const scene = JSON.parse(await fs.readFile(path.join(output, 'scene.json'), 'utf8'));
if (!Number.isFinite(scene.duration) || scene.duration <= 0 || scene.duration > 180 || Object.keys(inputs.recording_assets || {}).length !== scene.turns.length) throw new Error('Invalid bounded pilot inputs');
for (const name of ['STORYBOARD.md', 'performance.json', 'cue-map.json']) await requireHash(path.join(source, name), inputs.performance_sources?.[name]);
const score = JSON.parse(await fs.readFile(path.join(source, 'performance.json'), 'utf8'));
const cues = JSON.parse(await fs.readFile(path.join(source, 'cue-map.json'), 'utf8'));
scene.performance = validatePerformance(scene, score, cues, { story: inputs.story_sha256, recordings: inputs.recording_assets });
const compiledBytes = Buffer.from(JSON.stringify(scene.performance, null, 2) + '\n');
await fs.writeFile(path.join(output, 'performance-compiled.json'), compiledBytes, { flag: 'wx' });
for (const name of ['STORYBOARD.md', 'performance.json', 'cue-map.json', 'review-decision.json']) await fs.copyFile(path.join(source, name), path.join(output, name));
const bundle = await build({ entryPoints: [path.join(source, 'player.mjs')], bundle: true, write: false, metafile: true, minify: true, format: 'iife', nodePaths: [path.join(toolchain, 'node_modules')] });
const fontRoot = path.join(toolchain, 'node_modules/@fontsource/ibm-plex-sans');
let css = '', fontHashes = {};
for (const weight of [400, 600]) {
  const name = `ibm-plex-sans-latin-${weight}-normal.woff2`;
  const data = await fs.readFile(path.join(fontRoot, 'files', name)); fontHashes[name] = sha(data);
  css += `@font-face{font-family:Plex;font-style:normal;font-weight:${weight};font-display:block;src:url(data:font/woff2;base64,${data.toString('base64')}) format('woff2')}\n`;
}
await fs.copyFile(path.join(fontRoot, 'LICENSE'), path.join(output, 'FONT-LICENSE.txt'));
const runtimePackages = new Set(Object.keys(bundle.metafile.inputs).map(name => name.match(/node_modules\/((?:@[^/]+\/)?[^/]+)/)?.[1]).filter(Boolean));
let licenses = '';
for (const name of [...runtimePackages].sort()) {
  const root = path.join(toolchain, 'node_modules', name), pkg = JSON.parse(await fs.readFile(path.join(root, 'package.json'), 'utf8'));
  let license;
  for (const file of ['LICENSE.md', 'LICENSE', 'LICENSE.txt']) {
    try { license = await fs.readFile(path.join(root, file), 'utf8'); break; } catch (e) { if (e.code !== 'ENOENT') throw e; }
  }
  if (!license) throw new Error(`Missing retained license for bundled package ${name}`);
  licenses += `\n=== ${name}@${pkg.version} (${pkg.license}) ===\n${license}\n`;
}
await fs.writeFile(path.join(output, 'THIRD-PARTY-LICENSES.txt'), licenses);
await fs.copyFile(path.join(source, 'evidence.json'), path.join(output, 'source-evidence.json'));
await fs.copyFile(path.join(source, 'PRODUCTION.md'), path.join(output, 'PRODUCTION.md'));
const esc = text => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const note = scene.animatic ? 'Silent timing animatic. No narration is present. Durations are authored placeholders; NVIDIA voice audition is pending.' : 'Recorded synthetic narration; character mouth cues use measured audio energy, not phoneme alignment. The retained audition voices are user-validated; this directed animation awaits visual validation.';
let html = await fs.readFile(path.join(source, 'stage.html'), 'utf8');
html = html.replace('/* FONT */', css)
  .replace('/* AUDIO LABEL */', scene.animatic ? 'SILENT ANIMATIC · VOICE AUDITION PENDING' : 'VOICES VALIDATED · DIRECTED ANIMATION / VISUAL REVIEW PENDING')
  .replace('/* VIEWING NOTE */', esc(note))
  .replace('/* TRANSCRIPT */', scene.turns.map(t => `<li><strong>${esc(scene.speakers[t.speaker])}:</strong> ${esc(t.text)}</li>`).join('\n'))
  .replace('<g id="sam-slot"></g>', `<g id="sam-slot">${character('sam')}</g>`)
  .replace('<g id="litai-slot"></g>', `<g id="litai-slot">${character('litai')}</g>`)
  .replace('<g id="sam-arm-slot"></g>', `<g id="sam-arm-slot">${arm('sam')}</g>`)
  .replace('<g id="litai-arm-slot"></g>', `<g id="litai-arm-slot">${arm('litai')}</g>`)
  .replace('/* SCENE DATA */', JSON.stringify(scene).replaceAll('<', '\\u003c'))
  .replace('/* PLAYER */', bundle.outputFiles[0].text.replaceAll('</script', '<\\/script'));
await fs.writeFile(path.join(output, 'index.html'), html, { flag: 'wx' });
const sources = {};
for (const name of ['story.json', 'STORYBOARD.md', 'performance.json', 'performance.mjs', 'cue-map.json', 'review-decision.json', 'stage.html', 'characters.mjs', 'player.mjs', 'prepare.py', 'speech.py', 'build.mjs', 'capture.mjs', 'package.json', 'package-lock.json', 'PRODUCTION.md', 'evidence.json']) sources[name] = sha(await fs.readFile(path.join(source, name)));
await fs.writeFile(path.join(output, 'authoring.json'), JSON.stringify({ ...inputs, source_sha256: sources, performance_compiled_sha256: sha(compiledBytes), fonts: fontHashes, runtime_licenses_sha256: sha(Buffer.from(licenses)), html_sha256: sha(Buffer.from(html)), node: process.version, runtime: 'motion@14.6.0', rendering_boundary: 'Same pinned browser/platform/viewport only; no cross-platform pixel-identity claim', publication: 'local review only' }, null, 2) + '\n');
console.log(JSON.stringify({ offlineHTML: path.join(output, 'index.html'), duration: scene.duration, animatic: scene.animatic }));
