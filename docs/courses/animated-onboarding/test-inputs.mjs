// Exercise refusal at the actual builder/capture boundaries using disposable copies.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';

const [preparedArg, toolchainArg] = process.argv.slice(2);
if (!preparedArg || !toolchainArg) throw new Error('Usage: node test-inputs.mjs PREPARED_OUTPUT IGNORED_TOOLCHAIN');
const prepared = path.resolve(preparedArg), toolchain = path.resolve(toolchainArg);
const source = path.dirname(fileURLToPath(import.meta.url));
const root = await fs.mkdtemp(path.join(path.dirname(prepared), 'guard-tests-'));
const cases = ['scene', 'combined-audio', 'recording', 'story', 'staged-lock', 'score', 'cue-map', 'storyboard', 'compiled-score', 'html'];
try {
  for (const name of cases) {
    const fixture = path.join(root, name); await fs.mkdir(fixture);
    const author = path.join(fixture, 'author'), output = path.join(fixture, 'output'), tools = path.join(fixture, 'toolchain');
    await fs.cp(source, author, { recursive: true, filter: p => path.basename(p) !== '__pycache__' });
    await fs.cp(prepared, output, { recursive: true });
    await fs.mkdir(tools); await fs.copyFile(path.join(toolchain, 'package.json'), path.join(tools, 'package.json')); await fs.copyFile(path.join(toolchain, 'package-lock.json'), path.join(tools, 'package-lock.json'));
    await fs.symlink(path.join(toolchain, 'node_modules'), path.join(tools, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir');
    const invoke = (script, args) => spawnSync(process.execPath, [path.join(author, script), ...args], { encoding: 'utf8', timeout: 30000 });
    const mutate = async p => fs.appendFile(p, ' ');
    if (name === 'scene') await mutate(path.join(output, 'scene.json'));
    if (name === 'combined-audio') await mutate(path.join(output, 'audio.wav'));
    if (name === 'recording') await mutate(path.join(output, 'assets', JSON.parse(await fs.readFile(path.join(author, 'story.json'), 'utf8')).turns[0].id + '.wav'));
    if (name === 'story') await mutate(path.join(author, 'story.json'));
    if (name === 'staged-lock') await mutate(path.join(tools, 'package-lock.json'));
    for (const [kind, file] of [['score', 'performance.json'], ['cue-map', 'cue-map.json'], ['storyboard', 'STORYBOARD.md']]) if (name === kind) await mutate(path.join(author, file));
    if (name === 'html' || name === 'compiled-score') {
      const built = invoke('build.mjs', [output, tools]); assert.equal(built.status, 0, built.stderr);
      await mutate(path.join(output, name === 'html' ? 'index.html' : 'performance-compiled.json'));
      const result = invoke('capture.mjs', [output, tools, 'browser-must-not-launch', '--qa-only']);
      assert.notEqual(result.status, 0); assert.match(result.stderr, /Current input changed: (index.html|performance-compiled.json)/);
    } else {
      const result = invoke('build.mjs', [output, tools]);
      assert.notEqual(result.status, 0); assert.match(result.stderr, /Prepared input changed:/);
      await assert.rejects(fs.access(path.join(output, 'index.html')));
    }
  }
  console.log(JSON.stringify({ driftRefusalsPassed: cases.length, cases }));
} finally { await fs.rm(root, { recursive: true }); }
