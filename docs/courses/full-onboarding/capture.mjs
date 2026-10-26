import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { once } from 'node:events';
import { spawn, execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const [outputArg, toolchainArg, browserPath] = process.argv.slice(2);
if (!outputArg || !toolchainArg || !browserPath) throw new Error('Usage: node capture.mjs OUTPUT TOOLCHAIN BROWSER [--qa-only]');
const output = path.resolve(outputArg), toolchain = path.resolve(toolchainArg), qaOnly = process.argv.includes('--qa-only');
const require = createRequire(path.join(toolchain, 'package.json'));
const puppeteer = require('puppeteer-core');
const sha = value => createHash('sha256').update(value).digest('hex');
const inputs = JSON.parse(await fs.readFile(path.join(output,'inputs.json'),'utf8'));
const authoringBytes = await fs.readFile(path.join(output,'authoring.json'));
const authoring = JSON.parse(authoringBytes);
const browser = await puppeteer.launch({executablePath:browserPath,headless:true,args:['--force-color-profile=srgb','--disable-background-timer-throttling']});
let ffmpeg;
const deadline = setTimeout(()=>{ffmpeg?.kill('SIGTERM');void browser.close();},90*60*1000);
const checks=[], frames=[], errors=[], network=[];
try {
  for (const chapterRecord of inputs.chapters) {
    const directory = path.join(output,'chapters',chapterRecord.id), scene = JSON.parse(await fs.readFile(path.join(directory,'scene.json'),'utf8'));
    assert(scene.duration>0&&scene.duration<=180&&scene.turns.length===chapterRecord.turns);
    const page = await browser.newPage(); await page.setViewport({width:1280,height:900,deviceScaleFactor:1});
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion',value:'no-preference'}]);
    await page.setRequestInterception(true); page.on('request',request=>{if(request.url().startsWith('file:')||request.url().startsWith('data:'))request.continue();else{network.push(request.url().split('?')[0]);request.abort();}}); page.on('pageerror',error=>errors.push(error.message));
    await page.goto(pathToFileURL(path.join(directory,'index.html')).href,{waitUntil:'load',timeout:30000}); await page.evaluate(()=>window.presentation.ready);
    const stage=await page.$('#stage'),seek=value=>page.evaluate(time=>window.presentation.renderAt(time),value),frame=async value=>{await seek(value);return Buffer.from(await stage.screenshot({type:'png'}));};
    for (const seconds of [0,scene.turns[Math.floor(scene.turns.length/2)].start+.2,scene.duration]) {
      const first=await frame(seconds),state=await page.evaluate(()=>window.presentation.snapshot()); await seek(scene.duration-.01);await seek(0);const second=await frame(seconds),again=await page.evaluate(()=>window.presentation.snapshot()); assert.deepEqual(state,again);assert.equal(sha(first),sha(second));
      const name=`frame-${chapterRecord.id}-${frames.length}.png`;await fs.writeFile(path.join(output,name),first);frames.push({chapter:chapterRecord.id,seconds,file:name,sha256:sha(first)});
    }
    for (const turn of scene.turns) { await seek(turn.start+.05); assert(await page.evaluate(()=>{const node=document.querySelector('.caption');return node.getBoundingClientRect().bottom<=720&&node.scrollHeight<=node.clientHeight;}),`caption clips: ${turn.id}`); }
    let previous,maximum=0;
    for(let index=0;index<=Math.ceil(scene.duration*12);index++){await seek(Math.min(scene.duration,index/12));const points=await page.evaluate(()=>['sam','litai'].map(id=>{const matrix=document.querySelector(`#${id}-hand`).getCTM();return[matrix.e,matrix.f];}));if(previous)for(let actor=0;actor<2;actor++)maximum=Math.max(maximum,Math.hypot(points[actor][0]-previous[actor][0],points[actor][1]-previous[actor][1]));previous=points;}
    assert(maximum<40,`hand pose snaps in chapter ${chapterRecord.id}: ${maximum}`);
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion',value:'reduce'}]);await page.waitForFunction(()=>window.presentation.snapshot().staticMode);const firstTurn=scene.turns[0];await seek(firstTurn.start+.1);const stillA=await page.evaluate(()=>window.presentation.snapshot().html);await seek(Math.min(firstTurn.end-.05,firstTurn.start+.3));const stillB=await page.evaluate(()=>window.presentation.snapshot().html);assert.equal(stillA,stillB);
    await page.emulateMediaFeatures([{name:'prefers-reduced-motion',value:'no-preference'}]);await page.evaluate(()=>window.presentation.setStatic(false));
    if(!qaOnly){for(const turn of scene.turns){const seconds=turn.end-turn.start+.15,recording=path.join(output,'segments',`${turn.id}.mp4`);ffmpeg=spawn('ffmpeg',['-n','-hide_banner','-loglevel','error','-f','image2pipe','-framerate','24','-i','pipe:0','-an','-c:v','libx264','-threads','2','-preset','fast','-crf','20','-pix_fmt','yuv420p','-t',String(seconds),'-movflags','+faststart',recording],{stdio:['pipe','ignore','pipe']});let diagnostic='';ffmpeg.stderr.on('data',bytes=>{diagnostic=(diagnostic+bytes).slice(-4000);});ffmpeg.stdin.on('error',()=>{});const finished=new Promise((resolve,reject)=>{ffmpeg.once('error',reject);ffmpeg.once('close',code=>code===0?resolve():reject(new Error(`ffmpeg ${code}: ${diagnostic}`)));});for(let index=0;index<Math.ceil(seconds*24);index++){if(!ffmpeg.stdin.write(await frame(Math.min(scene.duration,turn.start+index/24))))await once(ffmpeg.stdin,'drain');}ffmpeg.stdin.end();await finished;ffmpeg=undefined;execFileSync('ffmpeg',['-v','error','-i',recording,'-f','null','-'],{timeout:120000});console.log(JSON.stringify({rendered:turn.id,seconds}));}}
    await page.close(); checks.push(`Chapter ${chapterRecord.id}: repeatable seek, caption bounds, continuous directed arms, and reduced motion`);
  }
  assert.equal(network.length,0,'offline chapters requested network resources');assert.deepEqual(errors,[]);
  checks.push('All chapters launch from local files with external requests blocked');
  const qa={schema:'literate-ai/full-onboarding-qa@1',checks,frames,renderer:{browser:await browser.version(),platform:process.platform,architecture:process.arch,viewport:{width:1280,height:900},stage:{width:1280,height:720},frames_per_second:24},authoring_sha256:sha(authoringBytes),external_requests:network.length,complete_listening_review:'pending',complete_viewing_review:'pending',factual_review:'pending',accessibility_review:'pending',boundary:'Automated QA and media integrity are not complete playback approval'};
  await fs.writeFile(path.join(output,'qa.json'),JSON.stringify(qa,null,2)+'\n'); console.log(JSON.stringify({checks:checks.length,frames:frames.length,qaOnly}));
} finally {clearTimeout(deadline);ffmpeg?.kill('SIGTERM');await browser.close();}
