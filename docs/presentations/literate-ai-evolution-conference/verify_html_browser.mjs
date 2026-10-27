#!/usr/bin/env node
/** Exercise the standalone deck in a real Chromium browser. */

import { mkdir } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import path from "node:path";

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, all) => {
  if (value.startsWith("--")) pairs.push([value.slice(2), all[index + 1]]);
  return pairs;
}, []));
if (!args.puppeteer || !args.chrome || !args.output) {
  throw new Error("usage: verify_html_browser.mjs --puppeteer PATH --chrome PATH --output DIR");
}
const { default: puppeteer } = await import(pathToFileURL(args.puppeteer));
await mkdir(args.output, { recursive: true });
const deckUrl = pathToFileURL(path.join(path.dirname(new URL(import.meta.url).pathname), "index.html")).href;
const browser = await puppeteer.launch({ headless: true, executablePath: args.chrome, args: ["--allow-file-access-from-files"] });
const findings = [];
const captures = [];
try {
  for (const profile of [
    { name: "desktop", width: 1440, height: 900, slide: 12 },
    { name: "mobile", width: 390, height: 844, slide: 11 },
  ]) {
    const page = await browser.newPage();
    await page.setViewport({ width: profile.width, height: profile.height, deviceScaleFactor: 1 });
    const consoleErrors = [];
    const failedRequests = [];
    const requests = [];
    page.on("console", event => { if (event.type() === "error") consoleErrors.push(event.text()); });
    page.on("pageerror", error => consoleErrors.push(error.message));
    page.on("request", request => requests.push(request.url()));
    page.on("requestfailed", request => failedRequests.push(`${request.url()}: ${request.failure()?.errorText}`));
    await page.goto(`${deckUrl}#1`, { waitUntil: "load" });
    await page.keyboard.press("ArrowRight");
    if ((await page.$eval(".counter", node => node.textContent.trim())) !== "2 / 14") findings.push(`${profile.name}: ArrowRight did not advance`);
    if (!page.url().endsWith("#2")) findings.push(`${profile.name}: navigation did not update URL hash`);
    await page.keyboard.press("n");
    if (!(await page.$eval("body", node => node.classList.contains("notes-open")))) findings.push(`${profile.name}: notes did not open`);
    if (!(await page.$eval(".notes-panel", node => node.textContent.trim().length > 20))) findings.push(`${profile.name}: notes panel is empty`);
    await page.keyboard.press("n");
    for (let number = 1; number <= 14; number += 1) {
      await page.evaluate(value => { window.location.hash = String(value); }, number);
      await page.waitForFunction(value => document.querySelector(".slide.active")?.dataset.slide === String(value), {}, number);
      const geometry = await page.$eval(".slide.active", slide => {
        const root = slide.getBoundingClientRect();
        const escaped = [...slide.querySelectorAll("*")].filter(node => {
          const style = getComputedStyle(node);
          if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
          const box = node.getBoundingClientRect();
          return box.width > 0 && box.height > 0 && (box.left < root.left - 2 || box.right > root.right + 2 || box.top < root.top - 2 || box.bottom > root.bottom + 2);
        }).map(node => node.className || node.tagName);
        return { width: root.width, height: root.height, escaped };
      });
      if (geometry.escaped.length) findings.push(`${profile.name} slide ${number}: escaped bounds: ${geometry.escaped.join(", ")}`);
      if (geometry.width > profile.width + 1 || geometry.height > profile.height + 1) findings.push(`${profile.name} slide ${number}: stage exceeds viewport`);
      if (profile.name === "desktop") {
        const reviewCapture = path.join(args.output, `desktop-slide-${number}.png`);
        await page.screenshot({ path: reviewCapture, fullPage: true });
        captures.push(reviewCapture);
      }
    }
    await page.evaluate(value => { window.location.hash = String(value); }, profile.slide);
    await page.waitForFunction(value => document.querySelector(".slide.active")?.dataset.slide === String(value), {}, profile.slide);
    const capture = path.join(args.output, `${profile.name}-slide-${profile.slide}.png`);
    await page.screenshot({ path: capture, fullPage: true });
    captures.push(capture);
    const external = requests.filter(url => !url.startsWith("file:") && !url.startsWith("data:"));
    if (external.length) findings.push(`${profile.name}: external requests: ${external.join(", ")}`);
    if (failedRequests.length) findings.push(`${profile.name}: failed requests: ${failedRequests.join(", ")}`);
    if (consoleErrors.length) findings.push(`${profile.name}: browser errors: ${consoleErrors.join(" | ")}`);
    const overflow = await page.evaluate(() => ({ x: document.documentElement.scrollWidth > innerWidth, y: document.documentElement.scrollHeight > innerHeight }));
    if (overflow.x || overflow.y) findings.push(`${profile.name}: document overflow ${JSON.stringify(overflow)}`);
    await page.close();
  }
} finally {
  await browser.close();
}
if (findings.length) {
  console.error(findings.join("\n"));
  process.exitCode = 1;
} else {
  console.log(JSON.stringify({ status: "pass", profiles: ["desktop", "mobile"], captures }, null, 2));
}
