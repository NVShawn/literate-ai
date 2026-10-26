import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { spawn, execFileSync } from 'node:child_process';
import { pathToFileURL, fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const [outputArg, toolchainArg, browserPath] = process.argv.slice(2);
if (!outputArg || !toolchainArg || !browserPath) throw new Error('Usage: node capture.mjs OUTPUT IGNORED_TOOLCHAIN BROWSER_EXECUTABLE [--qa-only]');
const output = path.resolve(outputArg), toolchain = path.resolve(toolchainArg);
const require = createRequire(path.join(toolchain, 'package.json'));
const puppeteer = require('puppeteer-core');
const sha = v => createHash('sha256').update(v).digest('hex');
const scene = JSON.parse(await fs.readFile(path.join(output, 'scene.json'), 'utf8'));
const html = path.join(output, 'index.html');
const source = path.dirname(fileURLToPath(import.meta.url));
const provenanceBytes = await fs.readFile(path.join(output, 'authoring.json'));
const provenance = JSON.parse(provenanceBytes);
async function unchangedInputs() {
  async function check(file, expected) { if (!expected || sha(await fs.readFile(file)) !== expected) throw new Error(`Current input changed: ${path.basename(file)}`); }
  await check(html, provenance.html_sha256);
  await check(path.join(output, 'scene.json'), provenance.scene_sha256);
  await check(path.join(output, 'audio.wav'), provenance.audio_sha256);
  for (const [name, hash] of Object.entries(provenance.recording_assets)) {
    if (!/^assets\/[a-z][a-z0-9-]{0,39}\.wav$/.test(name)) throw new Error('Unsafe recording asset path');
    await check(path.join(output, name), hash);
  }
  for (const [name, hash] of Object.entries(provenance.source_sha256)) {
    if (name !== path.basename(name)) throw new Error('Unsafe authored source path');
    await check(path.join(source, name), hash);
  }
  await check(path.join(output, 'PRODUCTION.md'), provenance.source_sha256['PRODUCTION.md']);
  await check(path.join(output, 'source-evidence.json'), provenance.source_sha256['evidence.json']);
  await check(path.join(output, 'performance-compiled.json'), provenance.performance_compiled_sha256);
  for (const name of ['STORYBOARD.md', 'performance.json', 'cue-map.json', 'review-decision.json']) await check(path.join(output, name), provenance.source_sha256[name]);
  await check(path.join(output, 'authoring.json'), sha(provenanceBytes));
}
await unchangedInputs();
if (!Number.isFinite(scene.duration) || scene.duration <= 0 || scene.duration > 180) throw new Error('Invalid bounded pilot duration');
const browser = await puppeteer.launch({ executablePath: browserPath, headless: true, args: ['--force-color-profile=srgb', '--disable-background-timer-throttling'] });
let ffmpeg;
const deadline = setTimeout(() => { ffmpeg?.kill('SIGTERM'); void browser.close(); }, 20 * 60 * 1000);
const errors = [], network = [];
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900, deviceScaleFactor: 1 });
  await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);
  await page.setRequestInterception(true);
  page.on('request', r => {
    if (r.url().startsWith('file:') || r.url().startsWith('data:')) r.continue();
    else { network.push(r.url().split('?')[0]); r.abort(); }
  });
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(pathToFileURL(html).href, { waitUntil: 'load', timeout: 30000 });
  await page.evaluate(() => window.presentation.ready);
  const stage = await page.$('#stage');
  const seek = t => page.evaluate(t => window.presentation.renderAt(t), t);
  const frame = async t => { await seek(t); return Buffer.from(await stage.screenshot({ type: 'png' })); };
  const checks = [], frames = [];
  for (const t of [0, scene.turns[1].start + 1, scene.turns[3].start + .9, scene.turns[5].start + 1, scene.duration]) {
    const a = await frame(t), state = await page.evaluate(() => window.presentation.snapshot());
    await seek(scene.duration - .01); await seek(0);
    const b = await frame(t), again = await page.evaluate(() => window.presentation.snapshot());
    assert.deepEqual(state, again, `state depends on seek history at ${t}`);
    assert.equal(sha(a), sha(b), `pixels depend on seek history at ${t}`);
    const name = `frame-${frames.length}.png`; await fs.writeFile(path.join(output, name), a);
    frames.push({ file: name, seconds: t, mode: 'motion', reducedMotion: false, sha256: sha(a) });
  }
  checks.push('Same-time DOM and pixels repeat after seeking from different prior positions');
  for (const t of scene.turns.map(t => t.start + .1)) {
    await seek(t);
    assert(await page.evaluate(() => {
      const box = document.querySelector('.caption');
      return box.getBoundingClientRect().bottom <= 720 && box.scrollHeight <= box.clientHeight;
    }), 'caption exceeds frame or clips');
  }
  checks.push('Every dialogue caption fits inside the frame');
  const choreography = JSON.parse(await fs.readFile(path.join(output, 'performance-compiled.json'), 'utf8'));
  const cue = id => choreography.cues.find(c => c.id === id).time;
  await seek(scene.turns.find(t => t.id === 'changes').start + .3);
  const resting = await page.$eval('#sam-hand', e => e.getAttribute('transform'));
  await seek(scene.turns.find(t => t.id === 'changes').start + 1.1);
  assert.equal(await page.$eval('#sam-hand', e => e.getAttribute('transform')), resting, 'Sam waves during a deliberate listening/reading hold');
  await seek(cue('changes.changes') + .45);
  assert(await page.evaluate(() => {
    const hand = document.querySelector('#sam-hand').getBoundingClientRect(), apply = document.querySelector('#focus-apply').getBoundingClientRect();
    return hand.right < apply.left && apply.left - hand.right < 45;
  }), 'hover must remain visibly short of Apply');
  const folder = await page.$eval('#repository', e => e.innerHTML);
  for (const name of ['build', 'test', 'acceptance']) {
    await seek(cue(`earn.${name}`) + .35);
    assert(await page.evaluate(name => {
      const width = id => Number(document.querySelector(`#focus-${id}`).getAttribute('stroke-width'));
      return ['build', 'test', 'acceptance'].every(id => document.querySelector(`#gate-${id}`).textContent.includes('PENDING')) && ['build', 'test', 'acceptance'].filter(id => id !== name).every(id => width(name) > width(id));
    }, name), `missing spoken focus or fabricated pass at ${name}`);
  }
  await seek(scene.duration);
  assert.equal(await page.$eval('#repository', e => e.innerHTML), folder, 'original source-folder contents changed');
  checks.push('Directed holds, hover before Apply, unchanged source and spoken pending-gate focus are observable in the DOM');
  const maximumStep = await page.evaluate(async duration => {
    let previous, maximum = { step: 0, seconds: 0, actor: '' };
    for (let i = 0; i <= Math.ceil(duration * 24); i++) {
      await window.presentation.renderAt(Math.min(duration, i / 24));
      const points = ['sam', 'litai'].map(id => { const e = document.querySelector(`#${id}-hand`), m = e.getCTM(); return [m.e, m.f]; });
      if (previous) for (let n = 0; n < points.length; n++) { const step = Math.hypot(points[n][0] - previous[n][0], points[n][1] - previous[n][1]); if (step > maximum.step) maximum = { step, seconds: i / 24, actor: ['sam', 'litai'][n] }; }
      previous = points;
    }
    return maximum;
  }, scene.duration);
  assert(maximumStep.step < 35, `hand pose snaps between adjacent film frames: ${JSON.stringify(maximumStep)}`);
  checks.push('Hand trajectories remain continuous across the full 24 fps timeline, including dialogue boundaries');

  await seek(0);
  await page.evaluate(() => document.activeElement?.blur()); await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'play');
  await page.keyboard.press('Space'); await page.waitForFunction(() => window.presentation.snapshot().playing, { timeout: 5000 });
  await page.keyboard.press('Space'); await page.waitForFunction(() => !window.presentation.snapshot().playing, { timeout: 5000 });
  checks.push('Keyboard Tab and Space operate Play/Pause without global shortcuts');
  await page.keyboard.press('Space'); await page.waitForFunction(() => window.presentation.snapshot().playing, { timeout: 5000 });
  await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
  await page.waitForFunction(() => window.presentation.snapshot().staticMode && !window.presentation.snapshot().playing);
  await seek(1); const stillA = await page.evaluate(() => window.presentation.snapshot().rigs);
  await seek(4); const stillB = await page.evaluate(() => window.presentation.snapshot().rigs);
  assert.deepEqual(stillA, stillB, 'reduced-motion characters still move');
  const reload = await browser.newPage(); await reload.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
  await reload.goto(pathToFileURL(html).href); await reload.evaluate(() => window.presentation.ready);
  assert(await reload.evaluate(() => window.presentation.snapshot().staticMode)); await reload.close();
  checks.push('Reduced-motion preference works on load and on change; rigs remain static across time');
  await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);
  await page.waitForFunction(() => !window.presentation.snapshot().staticMode);
  await page.evaluate(() => window.presentation.setStatic(true));
  await seek(scene.turns[5].start + .1); await fs.writeFile(path.join(output, 'poster.png'), await stage.screenshot({ type: 'png' }));
  const nojs = await browser.newPage(); await nojs.setJavaScriptEnabled(false); await nojs.goto(pathToFileURL(html).href);
  assert.equal(await nojs.$$eval('.transcript li', elements => elements.length), scene.turns.length);
  assert(await nojs.$('#sam-head')); assert(await nojs.$('#litai-head')); await nojs.close();
  checks.push('Static SVG characters and complete transcript exist without JavaScript');
  const bad = await page.evaluate(async () => {
    let rejected = 0;
    for (const n of [-1, NaN, Infinity, window.presentation.duration + 1]) { try { await window.presentation.renderAt(n); } catch { rejected++; } }
    return rejected;
  }); assert.equal(bad, 4); checks.push('Negative, non-finite and out-of-range seeks fail');
  assert.equal(network.length, 0, 'offline bundle requested a network resource'); assert.deepEqual(errors, []);
  checks.push('Actual local-file launch is self-contained with external requests blocked');
  const renderer = { browser: await browser.version(), platform: process.platform, architecture: process.arch, viewport: { width: 1280, height: 900, deviceScaleFactor: 1 }, stage: { width: 1280, height: 720 }, colorProfile: 'srgb', framesPerSecond: 24 };
  if (!process.argv.includes('--qa-only')) {
    await page.evaluate(() => window.presentation.setStatic(false));
    await fs.mkdir(path.join(output, 'segments'));
    for (const turn of scene.turns) {
      const seconds = turn.end - turn.start + .15, recording = path.join(output, 'segments', turn.id + '.mp4');
      ffmpeg = spawn('ffmpeg', ['-n', '-hide_banner', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', '24', '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-threads', '2', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p', '-t', String(seconds), '-movflags', '+faststart', recording], { stdio: ['pipe', 'ignore', 'pipe'] });
      let diagnostic = ''; ffmpeg.stderr.on('data', b => { diagnostic = (diagnostic + b).slice(-4000); });
      ffmpeg.stdin.on('error', () => {});
      const finished = new Promise((resolve, reject) => { ffmpeg.once('error', reject); ffmpeg.once('close', code => code === 0 ? resolve() : reject(new Error(`ffmpeg exited ${code}: ${diagnostic}`))); });
      for (let i = 0; i < Math.ceil(seconds * 24); i++) {
        if (!ffmpeg.stdin.write(await frame(Math.min(scene.duration, turn.start + i / 24)))) await once(ffmpeg.stdin, 'drain');
      }
      ffmpeg.stdin.end(); await finished; ffmpeg = undefined;
      execFileSync('ffmpeg', ['-v', 'error', '-i', recording, '-f', 'null', '-'], { timeout: 120000 });
      console.log(JSON.stringify({ renderedTurn: turn.id, seconds }));
    }
  }
  await unchangedInputs();
  const qa = { checks, frames, renderer, trajectory_maximum: maximumStep, inputs_sha256: sha(provenanceBytes), story_sha256: provenance.story_sha256, runtime_lock_sha256: provenance.source_sha256['package-lock.json'], font_sha256: provenance.fonts, reducedMotion: 'Static characters verified on load and preference change', externalRequests: network.length, animatic: scene.animatic, complete_listening_review: 'pending', human_viewing_review: 'pending', boundary: 'Automated frame/media checks are not factual or human creative approval' };
  await fs.writeFile(path.join(output, 'qa.json'), JSON.stringify(qa, null, 2) + '\n');
  console.log(JSON.stringify({ checksPassed: checks.length, framesChecked: frames.length, animatic: scene.animatic }));
} finally {
  clearTimeout(deadline); ffmpeg?.kill('SIGTERM'); await browser.close();
}
