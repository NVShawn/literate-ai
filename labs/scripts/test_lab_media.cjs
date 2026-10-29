// Actual controls, audio oscillators and canvas-video downloads, without models.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.LAB_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const output = process.env.LAB_EVIDENCE_DIR || path.resolve(root, '../_build/lab-browser');

(async () => {
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined});
  const results = [];
  try {
    for (const width of [1440, 390]) for (const name of fs.readdirSync(root).filter(n => /^0[1-5]-/.test(n)).sort()) {
      const context = await browser.newContext({viewport: {width, height: 1000}, offline: true, acceptDownloads: true});
      const page = await context.newPage(), errors = [], requests = [];
      page.on('pageerror', e => errors.push(String(e)));
      page.on('console', m => {if (m.type() === 'error') errors.push(m.text());});
      page.on('request', r => {if (/^https?:/.test(r.url())) requests.push(r.url());});
      await page.addInitScript(() => {
        window.__labAudio = []; window.__labTimers = new Map();
        const create = AudioContext.prototype.createOscillator;
        AudioContext.prototype.createOscillator = function(...args) {
          const oscillator = create.apply(this, args), start = oscillator.start;
          oscillator.start = function(...values) {
            window.__labAudio.push({frequency: oscillator.frequency.value, type: oscillator.type, state: this.context.state});
            return start.apply(this, values);
          };
          return oscillator;
        };
        const interval = window.setInterval, clear = window.clearInterval;
        window.setInterval = (fn, delay, ...args) => {
          const id = interval(fn, delay, ...args); window.__labTimers.set(id, delay); return id;
        };
        window.clearInterval = id => {window.__labTimers.delete(id); return clear(id);};
        window.__draw = {arcs: 0, lines: 0, rectangles: 0};
        for (const [method, key] of [['arc', 'arcs'], ['lineTo', 'lines'], ['fillRect', 'rectangles']]) {
          const original = CanvasRenderingContext2D.prototype[method];
          CanvasRenderingContext2D.prototype[method] = function(...args) {window.__draw[key]++; return original.apply(this, args);};
        }
        const raf = window.requestAnimationFrame;
        window.requestAnimationFrame = fn => {
          if (!window.__initialDraw) window.__initialDraw = {...window.__draw};
          return raf(fn);
        };
      });
      await page.goto(pathToFileURL(path.join(root, name, 'index.html')).href);
      assert.equal(await page.evaluate(() => window.__labAudio.length), 0, 'audio must start silent');
      if (name.startsWith('01')) {
        assert.equal(await page.evaluate(() => window.__initialDraw.arcs), 3);
        const slider = page.getByRole('slider', {name: 'Gravity'});
        assert.equal(await slider.inputValue(), '42');
        await slider.press('End'); await page.waitForFunction(() => document.querySelector('[data-readout]').textContent.includes('G 0.90'));
        await slider.press('Home'); await page.waitForFunction(() => document.querySelector('[data-readout]').textContent.includes('G 0.08'));
        await page.getByRole('button', {name: 'Reset universe'}).click();
        assert.equal(await slider.inputValue(), '8', 'reset preserves chosen gravity');
      }
      if (name.startsWith('03')) {
        assert.equal(await page.evaluate(() => window.__initialDraw.lines), 15);
        const slider = page.getByRole('slider', {name: 'Wobble'});
        assert.equal(await slider.inputValue(), '40');
        await slider.press('End'); await page.waitForFunction(() => document.querySelector('[data-readout]').textContent.includes('wobble 100%'));
        await slider.press('Home'); await page.waitForFunction(() => document.querySelector('[data-readout]').textContent.includes('wobble 0%'));
      }
      if (name.startsWith('02') || name.startsWith('05')) {
        const birds = name.startsWith('02'), delay = birds ? 240 : 360;
        const start = page.getByRole('button', {name: birds ? 'Start the paperwork' : 'Wake the choir'});
        await start.click(); await start.click();
        await page.waitForFunction(() => window.__labAudio.length > 0);
        const audio = await page.evaluate(() => window.__labAudio[0]);
        assert.equal(audio.type, birds ? 'sine' : 'triangle');
        assert.equal(audio.frequency, birds ? 261.6000061035156 : 110);
        assert.equal(audio.state, 'running');
        assert.equal(await page.evaluate(d => [...window.__labTimers.values()].filter(v => v === d).length, delay), 1);
        await page.getByRole('button', {name: birds ? 'Stop issuing permits' : 'Silence the choir'}).click();
        assert.equal(await page.evaluate(d => [...window.__labTimers.values()].filter(v => v === d).length, delay), 0);
        const stopped = await page.evaluate(() => window.__labAudio.length);
        await page.waitForTimeout(450); assert.equal(await page.evaluate(() => window.__labAudio.length), stopped);
        await start.click(); await page.waitForFunction(n => window.__labAudio.length > n, stopped);
      }
      if (name.startsWith('04')) {
        assert.equal(await page.evaluate(() => window.__initialDraw.rectangles), 19);
        await page.getByRole('button', {name: 'Authorize ominous drone'}).click();
        const audio = await page.evaluate(() => window.__labAudio[0]);
        assert.equal(audio.frequency, 55); assert.equal(audio.type, 'sawtooth');
        await page.getByRole('button', {name: 'Authorize ominous drone'}).click();
        assert.equal(await page.evaluate(() => window.__labAudio.length), 1);
      }
      if (name.startsWith('05')) {
        assert.equal(await page.evaluate(() => window.__initialDraw.arcs), 90);
        assert.equal(await page.evaluate(() => window.__initialDraw.lines), 10);
      }
      if (name.startsWith('04') || name.startsWith('05')) {
        const button = page.getByRole('button', {name: name.startsWith('04') ? 'Capture 4 second aria' : 'Bottle 5 seconds'});
        const began = Date.now(), pending = page.waitForEvent('download');
        await button.click(); await button.click(); // concurrent requests must not start another capture
        const download = await pending;
        const file = path.join(output, `${name}-${width}-${download.suggestedFilename()}`);
        await download.saveAs(file);
        assert.ok(fs.statSync(file).size > 256, 'real captured media must be nonempty');
        assert.ok(Date.now() - began >= (name.startsWith('04') ? 3800 : 4800));
        // A completed capture must permit another actual recording.
        const next = page.waitForEvent('download'); await button.click();
        const again = await next;
        const second = path.join(output, `second-${name}-${width}-${again.suggestedFilename()}`);
        await again.saveAs(second); assert.ok(fs.statSync(second).size > 256);
        // Unsupported capture remains visible while animation continues.
        await page.evaluate(() => {window.MediaRecorder = undefined;});
        await button.click(); await page.waitForTimeout(100);
        assert.match(await page.getByRole('status').textContent(), /unsupported/i);
      }
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert.deepEqual(errors, []); assert.deepEqual(requests, []);
      results.push({lab: name, width, passed: true});
      await context.close();
    }
    fs.writeFileSync(path.join(output, 'media-results.json'), JSON.stringify({passed: true, checks: results}, null, 2));
    console.log('PASS: ten desktop/mobile physics, projection, real audio and canvas capture cases');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
