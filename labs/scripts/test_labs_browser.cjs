// Optional real-browser checks: use an existing Playwright and Chromium installation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.LAB_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const evidence = process.env.LAB_EVIDENCE_DIR || path.resolve(root, '../_build/lab-browser');

(async () => {
  fs.mkdirSync(evidence, {recursive: true});
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined});
  const results = [];
  try {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({viewport: {width, height: 1000}, offline: true});
      const page = await context.newPage();
      const errors = [], network = [];
      page.on('pageerror', e => errors.push(String(e)));
      page.on('console', m => {if (m.type() === 'error') errors.push(m.text());});
      page.on('request', r => {if (!r.url().startsWith('file:')) network.push(r.url());});
      await page.addInitScript(() => {
        window.__dots = [];
        const arc = CanvasRenderingContext2D.prototype.arc;
        CanvasRenderingContext2D.prototype.arc = function(x, y, r, ...args) {
          window.__dots.push({x, y, r, color: this.fillStyle, alpha: this.globalAlpha});
          return arc.call(this, x, y, r, ...args);
        };
        // Step actual animation frames deterministically, without changing the lab source.
        window.requestAnimationFrame = fn => {window.__nextFrame = fn; return 1;};
      });
      const lab = path.join(root, '00-grumpy-counter');
      await page.goto(pathToFileURL(path.join(lab, 'index.html')).href);
      assert.match(await page.locator('[data-inventory]').innerText(), /1 authored lab specification · 4 illustrative roles/);
      assert.equal(await page.locator('[data-spec]').textContent(), fs.readFileSync(path.join(lab, 'component.md'), 'utf8'));
      const slider = page.getByRole('slider', {name: 'Modulus'});
      assert.equal(await slider.inputValue(), '37');
      assert.equal(await slider.getAttribute('min'), '7');
      assert.equal(await slider.getAttribute('max'), '97');
      assert.equal(await slider.getAttribute('step'), '1');
      const dots = await page.evaluate(() => window.__dots);
      assert.equal(dots.length, 239);
      const size = await page.locator('canvas').boundingBox();
      const sieve = Array(240).fill(true); sieve[0] = sieve[1] = false;
      for (let p = 2; p < 240; p++) if (sieve[p]) for (let k = p * p; k < 240; k += p) sieve[k] = false;
      for (let n = 1; n < 240; n++) {
        const dot = dots[n - 1];
        assert.equal(dot.color, n === 1 ? '#9db0c4' : sieve[n] ? '#38d6c2' : '#ff8a3d');
        assert.equal(dot.r, sieve[n] ? 3.4 : 2);
        assert.equal(dot.alpha, n === 1 ? .55 : sieve[n] ? .9 : .35);
        const radius = 22 + (n % 37) * Math.min(size.width, size.height) * .006;
        assert.ok(Math.abs(dot.x - Math.cos(n * .31) * radius) < .0001);
        assert.ok(Math.abs(dot.y - Math.sin(n * .31) * radius) < .0001);
      }
      await slider.focus();
      for (const [key, value] of [['Home', '7'], ['End', '97'], ['ArrowLeft', '96']]) {
        await slider.press(key);
        assert.equal(await slider.inputValue(), value);
        assert.equal(await page.locator('output').textContent(), value);
        assert.equal(await page.locator('[data-readout]').textContent(), `modulus ${value} · primes remain smug`);
      }
      // Actual pointer click on the native range control changes its value.
      const box = await slider.boundingBox();
      await page.mouse.click(box.x + box.width * .5, box.y + box.height * .5);
      const value = await slider.inputValue();
      assert.notEqual(value, '96');
      assert.equal(await page.locator('output').textContent(), value);
      await page.evaluate(() => {window.__dots = []; window.__nextFrame();});
      const moved = await page.evaluate(() => window.__dots);
      assert.equal(moved.length, 239);
      assert.deepEqual(moved.map(d => d.color), dots.map(d => d.color));
      assert.notEqual(moved[1].x, dots[1].x);
      await page.getByRole('button', {name: 'Number goblin'}).click();
      assert.match(await page.locator('[data-authority]').textContent(), /classifies integers/);
      await page.getByRole('button', {name: 'Criteria'}).focus();
      await page.keyboard.press('Enter');
      assert.match(await page.locator('[data-authority]').textContent(), /not an independent verifier/);
      await page.keyboard.press('Space');
      assert.equal(await page.getByRole('button', {name: 'Criteria'}).getAttribute('aria-pressed'), 'true');
      const records = await page.evaluate(() => JSON.parse(document.getElementById('lab-config').textContent).sources);
      assert.equal(records.length, 8);
      assert.equal(await page.locator('.harness-card').count(), 6);
      assert.match(await page.locator('[data-harness]').textContent(), /Flavor selection: none/);
      assert.match(await page.locator('[data-harness]').textContent(), /not selected for this browser lab/);
      assert.match(await page.locator('[data-harness]').textContent(), /canonical Component root no/);
      assert.ok(await page.locator('[data-spec] .spec-heading').count() >= 5);
      assert.equal(await page.locator('[data-spec] .spec-step').count(), 7);
      for (const source of records) {
        const bytes = fs.readFileSync(path.resolve(root, '..', source.path));
        assert.equal(source.content, bytes.toString('utf8'));
        assert.equal(source.sha256, crypto.createHash('sha256').update(bytes).digest('hex'));
        const details = page.locator('[data-source-path]').filter({has: page.locator('summary', {hasText: source.path})});
        await details.locator('summary').click();
        assert.equal(await details.locator('pre').textContent(), source.content);
      }
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({path: path.join(evidence, `grumpy-${width}.png`), fullPage: true});
      fs.writeFileSync(path.join(evidence, `grumpy-${width}.aria.txt`), await page.locator('body').ariaSnapshot());
      assert.deepEqual(errors, []); assert.deepEqual(network, []);
      results.push({width, dots: 239, sourceFiles: records.length, errors, network});
      await context.close();
    }
    // Shared-shell compatibility and offline loading for every lesson.
    for (const name of fs.readdirSync(root).filter(n => /^\d\d-/.test(n)).sort()) {
      const context = await browser.newContext({offline: true});
      const page = await context.newPage(), errors = [];
      page.on('pageerror', e => errors.push(String(e)));
      await page.goto(pathToFileURL(path.join(root, name, 'index.html')).href);
      assert.equal(await page.locator('[data-source-path]').count(), 8);
      assert.match(await page.locator('[data-inventory]').textContent(), /No resolved Component DAG/);
      assert.deepEqual(errors, []);
      await context.close();
    }
    fs.writeFileSync(path.join(evidence, 'results.json'), JSON.stringify({status: 'passed', viewports: results, sharedLabs: 6}, null, 2));
    console.log('PASS: actual offline Chromium, desktop/mobile Grumpy interactions, 239-dot arithmetic, source parity, six shared shells');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
