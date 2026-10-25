import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const source = path.dirname(fileURLToPath(import.meta.url));
const [outputArg, toolchainArg] = process.argv.slice(2);
if (!outputArg || !toolchainArg) throw new Error('Usage: node build.mjs PREPARED_OUTPUT IGNORED_TOOLCHAIN');
const output = path.resolve(outputArg), toolchain = path.resolve(toolchainArg);
const require = createRequire(path.join(toolchain, 'package.json'));
const { build } = require('esbuild');
const sha = value => createHash('sha256').update(value).digest('hex');
const inputsBytes = await fs.readFile(path.join(output, 'inputs.json'));
const inputs = JSON.parse(inputsBytes);
const bundle = await build({ entryPoints: [path.join(source, 'player.mjs')], bundle: true, write: false, metafile: true, minify: true, format: 'iife', nodePaths: [path.join(toolchain, 'node_modules')] });
const fontRoot = path.join(toolchain, 'node_modules/@fontsource/ibm-plex-sans');
let css = '', fonts = {};
for (const weight of [400, 600]) {
  const name = `ibm-plex-sans-latin-${weight}-normal.woff2`, bytes = await fs.readFile(path.join(fontRoot, 'files', name));
  fonts[name] = sha(bytes); css += `@font-face{font-family:Plex;font-style:normal;font-weight:${weight};font-display:block;src:url(data:font/woff2;base64,${bytes.toString('base64')}) format('woff2')}\n`;
}
await fs.copyFile(path.join(fontRoot, 'LICENSE'), path.join(output, 'FONT-LICENSE.txt'));
const esc = text => text.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const template = await fs.readFile(path.join(source, 'stage.html'), 'utf8');
const htmlHashes = {};
for (const record of inputs.chapters) {
  const directory = path.join(output, 'chapters', record.id), sceneBytes = await fs.readFile(path.join(directory, 'scene.json'));
  if (sha(sceneBytes) !== record.scene_sha256 || sha(await fs.readFile(path.join(directory, 'audio.wav'))) !== record.audio_sha256) throw new Error(`prepared chapter drifted: ${record.id}`);
  const scene = JSON.parse(sceneBytes);
  const html = template.replace('/* FONT */', css)
    .replace('/* TRANSCRIPT */', scene.turns.map(turn => `<li><strong>${esc(scene.speakers[turn.speaker])}:</strong> ${esc(turn.text)}</li>`).join('\n'))
    .replace('/* SCENE DATA */', JSON.stringify(scene).replaceAll('<','\\u003c'))
    .replace('/* PLAYER */', bundle.outputFiles[0].text.replaceAll('</script','<\\/script'));
  await fs.writeFile(path.join(directory, 'index.html'), html, { flag: 'wx' }); htmlHashes[`chapters/${record.id}/index.html`] = sha(Buffer.from(html));
}
for (const name of ['NARRATIVE.md','PRODUCTION.md','RESEARCH.md']) await fs.copyFile(path.join(source, name), path.join(output, name));
const sources = {};
for (const name of ['compile_story.py','synthesize.py','prepare.py','player.mjs','stage.html','build.mjs','capture.mjs','NARRATIVE.md','PRODUCTION.md','RESEARCH.md']) {
  try { sources[name] = sha(await fs.readFile(path.join(source, name))); } catch (error) { if (error.code !== 'ENOENT') throw error; }
}
const authoring = { schema:'literate-ai/full-onboarding-authoring@1', inputs_sha256:sha(inputsBytes), html:htmlHashes, sources, fonts, node:process.version, runtime:'motion@14.6.0', renderer:'fixed-time chapter capture', review:'pending complete listening and viewing', publication:'local review only' };
await fs.writeFile(path.join(output, 'authoring.json'), JSON.stringify(authoring,null,2)+'\n', {flag:'wx'});
console.log(JSON.stringify({chapters:inputs.chapters.length,turns:Object.keys(inputs.recording_assets).length}));
